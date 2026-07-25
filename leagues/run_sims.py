#!/usr/bin/env python3
"""
run_sims.py
-----------
Monte Carlo driver for a league season. Fits the attack/defense model on played
matches, simulates the remainder N times, and aggregates each team's
finishing-position distribution into ``data/leagues/<key>/sim_results.json``:

  * current live table (played matches only);
  * projected final points (mean + 10th/90th percentiles);
  * probability of each finishing position 1..N;
  * derived title %, Champions-League %, any-Europe %, relegation %;
  * expected finishing rank;
  * per-remaining-fixture win/draw/loss probabilities (analytic, from the model).

An optional ``--as-of`` matchday cutoff treats every later match as unplayed —
used to forecast a partial season or to backtest against a known final table.

Usage
-----
    venv/bin/python -m leagues.run_sims eng --sims 20000 --seed 0
    venv/bin/python -m leagues.run_sims eng --as-of 20        # forecast from MD20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .match import get_match_probabilities
from .config import LeagueConfig, get_league
from .ingest import DATA_ROOT
from .model import fit_model, LeagueModel
from .simulator import SeasonFixtures, simulate_one, _rank_cluster, _accumulate


def apply_as_of(matches: pd.DataFrame, as_of: int | None) -> pd.DataFrame:
    """Return a copy with matches after ``as_of`` matchday marked unplayed."""
    m = matches.copy()
    if as_of is None:
        return m
    md = pd.to_numeric(m["matchday"], errors="coerce")
    future = (md > as_of).fillna(False)
    for col in ["home_goals", "away_goals", "xg_home", "xg_away"]:
        m[col] = pd.to_numeric(m[col], errors="coerce")
        m.loc[future, col] = np.nan
    m["played"] = (~future) & m["played"].astype(str).isin(["True", "true", "1"])
    return m


def current_table(fx: SeasonFixtures) -> dict[int, dict]:
    """Live standings and rank from played matches only."""
    stats = fx.current_points()
    played_mask = fx.played
    matchlist = list(zip((fx.team_ids[i] for i in fx.home[played_mask]),
                         (fx.team_ids[i] for i in fx.away[played_mask]),
                         fx.fixed_hg[played_mask].tolist(),
                         fx.fixed_ag[played_mask].tolist()))
    order = _rank_cluster(list(fx.team_ids), fx.cfg.tiebreakers, stats, matchlist)
    cur_rank = {tid: pos for pos, tid in enumerate(order, start=1)}
    return {tid: {**stats[tid], "rank": cur_rank[tid]} for tid in fx.team_ids}


def run(cfg: LeagueConfig, teams: pd.DataFrame, matches: pd.DataFrame,
        model: LeagueModel, n_sims: int, seed: int) -> dict:
    fx = SeasonFixtures(cfg, teams, matches, model)
    n = fx.n
    rng = np.random.default_rng(seed)

    pos_counts = np.zeros((n, n), dtype=np.int64)   # pos_counts[team_idx, position-1]
    pts_sum = np.zeros(n)
    pts_samples = np.zeros((n_sims, n), dtype=np.int32)
    for s in range(n_sims):
        rank, pts = simulate_one(fx, rng)
        pos_counts[np.arange(n), rank - 1] += 1
        pts_sum += pts
        pts_samples[s] = pts

    live = current_table(fx)
    name_of = dict(zip(teams["id"].astype(int), teams["team_name"]))
    code_of = dict(zip(teams["id"].astype(int), teams["code"]))

    ucl, europe = cfg.ucl_slots, cfg.ucl_slots + cfg.europa_slots
    releg_from = n - cfg.relegation_slots  # positions releg_from+1..n are relegated

    team_rows = []
    for i, tid in enumerate(fx.team_ids):
        probs = pos_counts[i] / n_sims
        exp_rank = float(np.dot(np.arange(1, n + 1), probs))
        team_rows.append({
            "team_id": tid,
            "name": name_of.get(tid, str(tid)),
            "code": code_of.get(tid, ""),
            "played": live[tid]["played"],
            "cur_pts": live[tid]["pts"],
            "cur_gd": live[tid]["gf"] - live[tid]["ga"],
            "cur_rank": live[tid]["rank"],
            "proj_pts": round(float(pts_sum[i] / n_sims), 1),
            "proj_pts_p10": int(np.percentile(pts_samples[:, i], 10)),
            "proj_pts_p90": int(np.percentile(pts_samples[:, i], 90)),
            "title_pct": round(100 * float(probs[0]), 2),
            "ucl_pct": round(100 * float(probs[:ucl].sum()), 2),
            "europe_pct": round(100 * float(probs[:europe].sum()), 2),
            "releg_pct": round(100 * float(probs[releg_from:].sum()), 2),
            "exp_rank": round(exp_rank, 2),
            "position_probs": [round(float(x), 4) for x in probs],
        })
    team_rows.sort(key=lambda r: r["exp_rank"])

    # Remaining-fixture analytic W/D/L from the model.
    fixtures = []
    for k in range(len(fx.home)):
        if fx.played[k]:
            continue
        hid = fx.team_ids[fx.home[k]]
        aid = fx.team_ids[fx.away[k]]
        p = get_match_probabilities(float(fx.lam_h[k]), float(fx.lam_a[k]))
        fixtures.append({
            "match_number": int(fx.match_numbers[k]),
            "home": code_of.get(hid, ""), "away": code_of.get(aid, ""),
            "home_name": name_of.get(hid, ""), "away_name": name_of.get(aid, ""),
            "lam_home": round(float(fx.lam_h[k]), 2), "lam_away": round(float(fx.lam_a[k]), 2),
            "win": round(p["win_a"], 3), "draw": round(p["draw"], 3), "loss": round(p["win_b"], 3),
        })

    return {
        "league": {"key": cfg.key, "name": cfg.name, "country": cfg.country,
                   "n_teams": n, "ucl_slots": cfg.ucl_slots,
                   "europa_slots": cfg.europa_slots, "relegation_slots": cfg.relegation_slots,
                   "tiebreakers": list(cfg.tiebreakers)},
        "meta": {"n_sims": n_sims, "seed": seed, "used_xg": model.used_xg,
                 "home_adv": round(model.home_adv, 4),
                 "n_played": int(fx.played.sum()), "n_remaining": int((~fx.played).sum())},
        "teams": team_rows,
        "fixtures": fixtures,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Monte Carlo league-season simulation.")
    ap.add_argument("league", help="league key (eng, esp, ita, de, fr)")
    ap.add_argument("--sims", type=int, default=20000, help="number of simulations")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--as-of", type=int, default=None,
                    help="treat matches after this matchday as unplayed (forecast/backtest)")
    ap.add_argument("--reg", type=float, default=0.05, help="model L2 shrinkage")
    ap.add_argument("--recency-halflife", type=float, default=None,
                    help="down-weight older matches (in played-match count)")
    args = ap.parse_args()

    cfg = get_league(args.league)
    data_dir = DATA_ROOT / cfg.key
    teams = pd.read_csv(data_dir / "teams.csv")
    matches = pd.read_csv(data_dir / "matches.csv")
    matches = apply_as_of(matches, args.as_of)

    model = fit_model(teams, matches, reg=args.reg, recency_halflife=args.recency_halflife)
    payload = run(cfg, teams, matches, model, args.sims, args.seed)
    payload["meta"]["as_of"] = args.as_of

    out = data_dir / "sim_results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    m = payload["meta"]
    print(f"{cfg.name}: {args.sims} sims, {m['n_played']} played / "
          f"{m['n_remaining']} remaining"
          + (f" (as-of MD{args.as_of})" if args.as_of else "")
          + f" [{'xG' if m['used_xg'] else 'goals'}] -> {out}")
    print("\nTitle race (top 6 by expected rank):")
    print(f"  {'Team':<24}{'Pld':>4}{'Pts':>5}{'Proj':>7}{'Title%':>8}{'UCL%':>7}{'Rel%':>7}")
    for r in payload["teams"][:6]:
        print(f"  {r['name'][:23]:<24}{r['played']:>4}{r['cur_pts']:>5}{r['proj_pts']:>7}"
              f"{r['title_pct']:>8}{r['ucl_pct']:>7}{r['releg_pct']:>7}")


if __name__ == "__main__":
    sys.exit(main())
