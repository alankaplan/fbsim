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
from .prior import load_prior, PRIOR_REGRESSION
from .simulator import SeasonFixtures, simulate_one, _rank_cluster, _accumulate


LOG2 = np.log(2.0)

# Bump whenever the sim_results.json payload gains/changes fields the report
# relies on, so `update` re-simulates leagues whose on-disk result predates it.
SCHEMA_VERSION = 3


def _entropy(p: np.ndarray) -> float:
    """Shannon entropy (nats) of a probability vector, ignoring zero entries."""
    p = p[p > 0]
    return float(-(p * np.log(p)).sum())


def _clean_str(v) -> str:
    s = str(v)
    return "" if s in ("", "nan", "NaN", "NaT", "None") else s


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

    R = int((~fx.played).sum())                     # number of remaining fixtures
    pos_counts = np.zeros((n, n), dtype=np.int64)   # pos_counts[team_idx, position-1]
    pts_sum = np.zeros(n)
    pts_samples = np.zeros((n_sims, n), dtype=np.int32)
    champ = np.empty(n_sims, dtype=np.int32)         # champion (rank-1) team index per sim
    outc = np.empty((n_sims, R), dtype=np.int8)      # unplayed-fixture outcome per sim
    for s in range(n_sims):
        rank, pts, out_s = simulate_one(fx, rng, return_outcomes=True)
        pos_counts[np.arange(n), rank - 1] += 1
        pts_sum += pts
        pts_samples[s] = pts
        champ[s] = int(np.argmin(rank))              # rank 1 is the champion
        outc[s] = out_s

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

    # Kickoff date/time lookups (datetime_utc present only for sources that carry it).
    date_of = dict(zip(matches["match_number"].astype(int), matches["date"]))
    dt_of = (dict(zip(matches["match_number"].astype(int), matches["datetime_utc"]))
             if "datetime_utc" in matches.columns else {})

    # Per-fixture informativeness: expected % drop in the entropy of the
    # champion (title) distribution once this fixture's result is known
    # (mutual information between the fixture outcome and the champion identity).
    H = _entropy(pos_counts[:, 0] / n_sims)
    info_pct = np.zeros(R)
    if H > 1e-12:
        for j in range(R):
            col = outc[:, j]
            hcond = 0.0
            for x in (0, 1, 2):
                mask = col == x
                cnt = int(mask.sum())
                if cnt:
                    cond = np.bincount(champ[mask], minlength=n).astype(float) / cnt
                    hcond += (cnt / n_sims) * _entropy(cond)
            info_pct[j] = max(H - hcond, 0.0) / H * 100.0

    def _reveal(order: np.ndarray) -> np.ndarray:
        """Residual champion entropy (bits) after each fixture, revealing the
        unplayed fixtures in ``order`` (a permutation of the outcome columns) and
        tracking the *joint* conditional entropy H(champ | outcomes so far)."""
        res = np.zeros(R)
        cell = np.zeros(n_sims, dtype=np.int64)      # joint-outcome cell id per sim
        for j in order:
            # split each existing cell by this fixture's outcome, then compact ids
            _, cell = np.unique(cell * 3 + outc[:, j].astype(np.int64), return_inverse=True)
            comb = cell.astype(np.int64) * n + champ  # joint (cell, champion)
            # H(champ | cell) = H(cell, champ) - H(cell), converted nats -> bits
            hc = (_entropy(np.bincount(comb) / n_sims)
                  - _entropy(np.bincount(cell) / n_sims)) / LOG2
            res[j] = max(hc, 0.0)
        return res

    # (1) Most-informative-first: cum_bits + info_rank feed the Top-games entropy
    # threshold (pick just enough top games to drive the race below a target).
    info_order = np.argsort(-info_pct, kind="stable")
    info_rank = np.zeros(R, dtype=int)
    info_rank[info_order] = np.arange(R)
    cum_bits = _reveal(info_order)

    # (2) Chronological (kickoff order): the title-race entropy still remaining
    # after each game is played — non-increasing through the season, reaching ~0
    # once the final results are known (all games decided ⇒ champion determined).
    col_mn = fx.match_numbers[~fx.played]
    def _when(j: int):
        mn = int(col_mn[j])
        s = _clean_str(dt_of.get(mn, "")) or _clean_str(date_of.get(mn, ""))
        return (s == "", s, mn)                      # undated last, then chronological
    chrono_order = np.array(sorted(range(R), key=_when), dtype=int)
    post_bits = _reveal(chrono_order)

    # Remaining-fixture analytic W/D/L from the model (aligned to `outc` columns).
    fixtures = []
    j = 0
    for k in range(len(fx.home)):
        if fx.played[k]:
            continue
        hid = fx.team_ids[fx.home[k]]
        aid = fx.team_ids[fx.away[k]]
        mn = int(fx.match_numbers[k])
        p = get_match_probabilities(float(fx.lam_h[k]), float(fx.lam_a[k]))
        fixtures.append({
            "match_number": mn,
            "date": _clean_str(date_of.get(mn, "")),
            "datetime_utc": _clean_str(dt_of.get(mn, "")),
            "home": code_of.get(hid, ""), "away": code_of.get(aid, ""),
            "home_name": name_of.get(hid, ""), "away_name": name_of.get(aid, ""),
            "lam_home": round(float(fx.lam_h[k]), 2), "lam_away": round(float(fx.lam_a[k]), 2),
            "win": round(p["win_a"], 3), "draw": round(p["draw"], 3), "loss": round(p["win_b"], 3),
            "info_pct": round(float(info_pct[j]), 2),
            "info_rank": int(info_rank[j]),
            "cum_bits": round(float(cum_bits[j]), 3),
            "post_bits": round(float(post_bits[j]), 3),
        })
        j += 1

    return {
        "league": {"key": cfg.key, "name": cfg.name, "country": cfg.country,
                   "n_teams": n, "ucl_slots": cfg.ucl_slots,
                   "europa_slots": cfg.europa_slots, "relegation_slots": cfg.relegation_slots,
                   "tiebreakers": list(cfg.tiebreakers),
                   "title_label": cfg.title_label, "qual_label": cfg.qual_label,
                   "qual2_label": cfg.qual2_label, "drop_label": cfg.drop_label,
                   "qual_name": cfg.qual_name, "drop_name": cfg.drop_name},
        "meta": {"schema_version": SCHEMA_VERSION,
                 "n_sims": n_sims, "seed": seed, "used_xg": model.used_xg,
                 "home_adv": round(model.home_adv, 4),
                 "champ_entropy_bits": round(H / LOG2, 3),
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
    ap.add_argument("--no-prior", action="store_true",
                    help="ignore the preseason prior (prior.json) if present")
    ap.add_argument("--prior-weight", type=float, default=3.0,
                    help="strength of the preseason prior (~pseudo-matches)")
    ap.add_argument("--prior-regression", type=float, default=PRIOR_REGRESSION,
                    help="regress last season's ratings toward the mean "
                         "(1.0 = off, 0.0 = flat league)")
    args = ap.parse_args()

    cfg = get_league(args.league)
    data_dir = DATA_ROOT / cfg.key
    teams = pd.read_csv(data_dir / "teams.csv")
    matches = pd.read_csv(data_dir / "matches.csv")
    matches = apply_as_of(matches, args.as_of)

    prior = None if args.no_prior else load_prior(cfg, args.prior_regression)
    model = fit_model(teams, matches, reg=args.reg, recency_halflife=args.recency_halflife,
                      prior=prior, prior_weight=args.prior_weight)
    payload = run(cfg, teams, matches, model, args.sims, args.seed)
    payload["meta"]["as_of"] = args.as_of
    payload["meta"]["used_prior"] = prior is not None

    out = data_dir / "sim_results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    m = payload["meta"]
    print(f"{cfg.name}: {args.sims} sims, {m['n_played']} played / "
          f"{m['n_remaining']} remaining"
          + (f" (as-of MD{args.as_of})" if args.as_of else "")
          + f" [{'xG' if m['used_xg'] else 'goals'}] -> {out}")
    print(f"\n{cfg.title_label} race (top 6 by expected rank):")
    print(f"  {'Team':<24}{'Pld':>4}{'Pts':>5}{'Proj':>7}"
          f"{cfg.title_label+'%':>9}{cfg.qual_label+'%':>9}{cfg.drop_label+'%':>7}")
    for r in payload["teams"][:6]:
        print(f"  {r['name'][:23]:<24}{r['played']:>4}{r['cur_pts']:>5}{r['proj_pts']:>7}"
              f"{r['title_pct']:>9}{r['ucl_pct']:>9}{r['releg_pct']:>7}")


if __name__ == "__main__":
    sys.exit(main())
