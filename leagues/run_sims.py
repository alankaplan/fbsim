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
from .simulator import (SeasonFixtures, simulate_one, _rank_cluster, _accumulate,
                        standings_batch, champion_batch)


LOG2 = np.log(2.0)

# Bump whenever the sim_results.json payload gains/changes fields the report
# relies on, so `update` re-simulates leagues whose on-disk result predates it.
SCHEMA_VERSION = 5

RES_HISTORIES = 30      # sampled partial-season histories per round cutoff
RES_MAXCUT = 18         # cap on distinct round cutoffs (interpolate the rest)
TREE_DEPTH = 5          # games deep for the per-team title-odds drill-down tree


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


def _resolution_curve(fx: SeasonFixtures, round_val: np.ndarray,
                      rng: np.random.Generator, n_fore: int,
                      n_hist: int = RES_HISTORIES, maxcut: int = RES_MAXCUT) -> np.ndarray:
    """Expected champion entropy (bits) remaining after each round is played.

    For each round cutoff we sample ``n_hist`` partial-season histories (the games
    in rounds ≤ cutoff), then for each history re-forecast the rest of the season
    ``n_fore`` times and measure the entropy of the resulting champion
    distribution; averaging over histories gives ``H(cutoff)``. Re-forecasting
    (rather than binning the fixed sample) is what avoids the finite-sample
    shattering that would otherwise drive the estimate to a false 0 after a few
    games. Returns a value per remaining fixture (aligned to ``round_val``),
    interpolated across cutoffs, declining from ≈ the baseline toward 0 at season's
    end.
    """
    n = fx.n
    R = round_val.shape[0]
    if R == 0:
        return np.zeros(0)
    rem = ~fx.played
    home_r, away_r = fx.home[rem], fx.away[rem]
    lamh_r, lama_r = fx.lam_h[rem], fx.lam_a[rem]
    criteria = fx.cfg.tiebreakers

    # Fixed standings from already-played games (added into every history).
    pl = fx.played
    bp, bw, bgf, bga = standings_batch(fx.home[pl], fx.away[pl],
                                       fx.fixed_hg[pl][None, :], fx.fixed_ag[pl][None, :], n)
    base_pts, base_wins, base_gf, base_ga = bp[0], bw[0], bgf[0], bga[0]

    rounds = np.unique(round_val)
    if len(rounds) > maxcut:
        rounds = np.unique(rounds[np.linspace(0, len(rounds) - 1, maxcut).round().astype(int)])
    H_at = np.zeros(len(rounds))
    for ci, c in enumerate(rounds):
        hist, tail = round_val <= c, round_val > c
        nh, nt = int(hist.sum()), int(tail.sum())
        if nh:
            hg = rng.poisson(lamh_r[hist], size=(n_hist, nh))
            ag = rng.poisson(lama_r[hist], size=(n_hist, nh))
            hp, hw, hgf, hga = standings_batch(home_r[hist], away_r[hist], hg, ag, n)
        else:
            hp = hw = hgf = hga = np.zeros((n_hist, n))
        hp = hp + base_pts; hw = hw + base_wins; hgf = hgf + base_gf; hga = hga + base_ga
        ents = np.zeros(n_hist)
        for kk in range(n_hist):
            if nt:
                tg = rng.poisson(lamh_r[tail], size=(n_fore, nt))
                ta = rng.poisson(lama_r[tail], size=(n_fore, nt))
                tp, tw, tgf, tga = standings_batch(home_r[tail], away_r[tail], tg, ta, n)
                pts, wins = tp + hp[kk], tw + hw[kk]
                gf, ga = tgf + hgf[kk], tga + hga[kk]
            else:                                     # nothing left ⇒ champion fixed
                pts, wins = hp[kk][None, :], hw[kk][None, :]
                gf, ga = hgf[kk][None, :], hga[kk][None, :]
            champ = champion_batch(pts, wins, gf - ga, gf, criteria)
            counts = np.bincount(champ, minlength=n).astype(float)
            ents[kk] = _entropy(counts / counts.sum()) / LOG2
        H_at[ci] = float(ents.mean())
    # The true curve is non-increasing (more games played ⇒ no more uncertainty);
    # clamp Monte-Carlo wobble so the displayed series never ticks back up.
    H_at = np.minimum.accumulate(H_at)
    return np.interp(round_val, rounds, H_at)


def run(cfg: LeagueConfig, teams: pd.DataFrame, matches: pd.DataFrame,
        model: LeagueModel, n_sims: int, seed: int, resolution_sims: int = 250) -> dict:
    fx = SeasonFixtures(cfg, teams, matches, model)
    n = fx.n
    rng = np.random.default_rng(seed)

    R = int((~fx.played).sum())                     # number of remaining fixtures
    pos_counts = np.zeros((n, n), dtype=np.int64)   # pos_counts[team_idx, position-1]
    pts_sum = np.zeros(n)
    pts_samples = np.zeros((n_sims, n), dtype=np.int32)
    champ = np.empty(n_sims, dtype=np.int32)         # champion (rank-1) team index per sim
    outc = np.empty((n_sims, R), dtype=np.int8)      # unplayed-fixture outcome per sim
    rank_samples = np.empty((n_sims, n), dtype=np.int16)  # finishing position per team per sim
    for s in range(n_sims):
        rank, pts, out_s = simulate_one(fx, rng, return_outcomes=True)
        pos_counts[np.arange(n), rank - 1] += 1
        pts_sum += pts
        pts_samples[s] = pts
        rank_samples[s] = rank
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

    # (2) Resolution curve (post_bits): the *expected* champion entropy still
    # remaining after each round is played, estimated by re-forecasting the rest
    # of the season from sampled partial standings. Unlike a per-game reveal of the
    # fixed sample, this doesn't shatter into false-0 after a matchday — it declines
    # gradually and only reaches ~0 near the end. Keyed by round (matchday), or by
    # chronological order when the source carries no matchday.
    col_mn = fx.match_numbers[~fx.played]
    round_val = pd.to_numeric(matches["matchday"], errors="coerce").to_numpy()[~fx.played].astype(float)
    if R and np.isnan(round_val).any():              # no matchdays: fall back to date order
        keys = [(_clean_str(dt_of.get(int(mn), "")) or _clean_str(date_of.get(int(mn), "")), int(mn))
                for mn in col_mn]
        dense = {k: i for i, k in enumerate(sorted(set(keys)))}
        round_val = np.array([dense[k] for k in keys], dtype=float)
    post_bits = (_resolution_curve(fx, round_val, rng, resolution_sims)
                 if (resolution_sims and R and H > 1e-12) else np.zeros(R))

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

    # Played games (with final scores) — the team-detail schedule's "past" half.
    results = []
    for k in range(len(fx.home)):
        if not fx.played[k]:
            continue
        hid, aid = fx.team_ids[fx.home[k]], fx.team_ids[fx.away[k]]
        mn = int(fx.match_numbers[k])
        results.append({
            "match_number": mn,
            "date": _clean_str(date_of.get(mn, "")),
            "datetime_utc": _clean_str(dt_of.get(mn, "")),
            "home": code_of.get(hid, ""), "away": code_of.get(aid, ""),
            "home_name": name_of.get(hid, ""), "away_name": name_of.get(aid, ""),
            "home_goals": int(fx.fixed_hg[k]), "away_goals": int(fx.fixed_ag[k]),
        })

    # Per-team title odds: how a team's championship chance (and expected finish)
    # shifts with each upcoming result. Two robust reads, both from the existing
    # sample: a per-game marginal *swing* (condition on one game — always dense) and
    # a compounding *drill-down tree* over the next TREE_DEPTH games (condition on
    # the team's own path; shallow, so cells stay dense and don't shatter).
    def _kick(mn: int):
        s = _clean_str(dt_of.get(mn, "")) or _clean_str(date_of.get(mn, ""))
        return (s == "", s, mn)                      # undated last, then chronological

    rem_list, jc = [], 0
    for k in range(len(fx.home)):
        if fx.played[k]:
            continue
        rem_list.append((jc, k, int(fx.match_numbers[k])))
        jc += 1

    min_support = max(30, int(0.004 * n_sims))
    team_odds: dict[int, dict] = {}
    for i, tid in enumerate(fx.team_ids):
        games = [(jj, mn, bool(fx.home[k] == i),
                  int(fx.away[k] if fx.home[k] == i else fx.home[k]))
                 for (jj, k, mn) in rem_list if i in (fx.home[k], fx.away[k])]
        games.sort(key=lambda g: _kick(g[1]))
        if not games:
            team_odds[tid] = {"future_swings": {}, "odds_tree": None}
            continue
        # team-perspective outcome per column: 0 win, 1 draw, 2 loss
        tcol = {g[0]: (outc[:, g[0]] if g[2] else 2 - outc[:, g[0]]) for g in games}
        is_champ, ranks_i = (champ == i), rank_samples[:, i]

        def _stats(mask: np.ndarray) -> dict:
            c = int(mask.sum())
            if not c:
                return {"title": None, "exp_finish": None, "support": 0}
            return {"title": round(100 * float(is_champ[mask].mean()), 2),
                    "exp_finish": round(float(ranks_i[mask].mean()), 2), "support": c}

        future_swings = {str(mn): {key: _stats(tcol[jj] == o)
                                   for key, o in (("w", 0), ("d", 1), ("l", 2))}
                         for (jj, mn, _, _) in games}

        def _node(mask: np.ndarray, depth: int) -> dict:
            node = _stats(mask)
            if depth < TREE_DEPTH and depth < len(games) and node["support"] >= min_support:
                jj, mn, is_home, opp = games[depth]
                otid = fx.team_ids[opp]
                node["game"] = {"match_number": mn, "opp": code_of.get(otid, ""),
                                "opp_name": name_of.get(otid, ""),
                                "ha": "H" if is_home else "A",
                                "date": _clean_str(date_of.get(mn, "")),
                                "datetime_utc": _clean_str(dt_of.get(mn, ""))}
                node["branches"] = {name: _node(mask & (tcol[jj] == o), depth + 1)
                                    for name, o in (("win", 0), ("draw", 1), ("loss", 2))}
            return node

        team_odds[tid] = {"future_swings": future_swings,
                          "odds_tree": _node(np.ones(n_sims, dtype=bool), 0)}

    for row in team_rows:
        o = team_odds.get(row["team_id"], {})
        row["future_swings"] = o.get("future_swings", {})
        row["odds_tree"] = o.get("odds_tree")

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
        "results": results,
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
    ap.add_argument("--resolution-sims", type=int, default=250,
                    help="tail forecasts per history for the 'H after' resolution "
                         "curve (0 disables it)")
    args = ap.parse_args()

    cfg = get_league(args.league)
    data_dir = DATA_ROOT / cfg.key
    teams = pd.read_csv(data_dir / "teams.csv")
    matches = pd.read_csv(data_dir / "matches.csv")
    matches = apply_as_of(matches, args.as_of)

    prior = None if args.no_prior else load_prior(cfg, args.prior_regression)
    model = fit_model(teams, matches, reg=args.reg, recency_halflife=args.recency_halflife,
                      prior=prior, prior_weight=args.prior_weight)
    payload = run(cfg, teams, matches, model, args.sims, args.seed,
                  resolution_sims=args.resolution_sims)
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
