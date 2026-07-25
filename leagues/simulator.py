"""
simulator.py
------------
Single-pool round-robin season engine.

It plays a full home/away fixture list, fixes any already-played match to its
recorded score, draws the rest from the fitted :class:`LeagueModel`, then ranks
all teams in one table using the league's configured tiebreaker chain.

The tiebreaker resolver takes its criterion order from :class:`LeagueConfig`,
so ties are broken per league — Spain/Italy apply head-to-head before overall
goal difference, England/Germany/France the reverse.

For the Monte Carlo hot path, unplayed-fixture expected goals are computed once
and scorelines are drawn vectorised with ``rng.poisson`` (two independent
Poisson counts per match); standings are accumulated with ``np.bincount``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import LeagueConfig
from .model import LeagueModel


# ---------------------------------------------------------------------------
# Tiebreakers (configurable chain)
# ---------------------------------------------------------------------------

def _h2h_key(cluster: list[int],
             match_index: dict[frozenset, list[tuple[int, int, int, int]]] | None,
             matches: list[tuple[int, int, int, int]]) -> dict[int, tuple[int, int, int]]:
    """(h2h_pts, h2h_gd, h2h_gf) for each team using only matches among cluster."""
    idset = set(cluster)
    acc = {tid: [0, 0, 0] for tid in cluster}  # [pts, gf, ga]
    for hid, aid, hg, ag in matches:
        if hid not in idset or aid not in idset:
            continue
        acc[hid][1] += hg; acc[hid][2] += ag
        acc[aid][1] += ag; acc[aid][2] += hg
        if hg > ag:
            acc[hid][0] += 3
        elif hg < ag:
            acc[aid][0] += 3
        else:
            acc[hid][0] += 1
            acc[aid][0] += 1
    return {tid: (p, f - a, f) for tid, (p, f, a) in acc.items()}


def _rank_cluster(cluster: list[int],
                  criteria: tuple[str, ...],
                  stats: dict[int, dict[str, int]],
                  matches: list[tuple[int, int, int, int]]) -> list[int]:
    """Order a set of teams by an ordered chain of tiebreaker criteria."""
    if len(cluster) <= 1:
        return list(cluster)
    if not criteria:
        return sorted(cluster)  # deterministic final fallback

    crit, rest = criteria[0], criteria[1:]
    if crit == "h2h":
        key = _h2h_key(cluster, None, matches)
        key_fn = lambda tid: key[tid]
    elif crit == "pts":
        key_fn = lambda tid: stats[tid]["pts"]
    elif crit == "gd":
        key_fn = lambda tid: stats[tid]["gf"] - stats[tid]["ga"]
    elif crit == "gf":
        key_fn = lambda tid: stats[tid]["gf"]
    else:
        raise ValueError(f"Unknown tiebreaker criterion: {crit}")

    buckets: dict = {}
    for tid in cluster:
        buckets.setdefault(key_fn(tid), []).append(tid)
    if len(buckets) == 1:                       # this criterion split nothing
        return _rank_cluster(cluster, rest, stats, matches)

    ordered: list[int] = []
    for k in sorted(buckets, reverse=True):
        sub = buckets[k]
        ordered.extend(sub if len(sub) == 1 else _rank_cluster(sub, rest, stats, matches))
    return ordered


# ---------------------------------------------------------------------------
# Fixture preparation
# ---------------------------------------------------------------------------

class SeasonFixtures:
    """Pre-processed fixtures + per-fixture expected goals, built once."""

    def __init__(self, cfg: LeagueConfig, teams: pd.DataFrame,
                 matches: pd.DataFrame, model: LeagueModel):
        self.cfg = cfg
        self.team_ids = [int(t) for t in teams["id"].tolist()]
        self.n = len(self.team_ids)
        self.idx = {tid: i for i, tid in enumerate(self.team_ids)}

        played_mask = (matches["played"].astype(str).isin(["True", "true", "1"])
                       | (matches["played"] == True))  # noqa: E712
        self.home = matches["home_team_id"].astype(int).map(self.idx).to_numpy()
        self.away = matches["away_team_id"].astype(int).map(self.idx).to_numpy()
        self.played = played_mask.to_numpy()

        hg = pd.to_numeric(matches["home_goals"], errors="coerce").fillna(0).to_numpy(int)
        ag = pd.to_numeric(matches["away_goals"], errors="coerce").fillna(0).to_numpy(int)
        self.fixed_hg = hg
        self.fixed_ag = ag

        # Expected goals for every fixture (used only where not played).
        lam_h = np.ones(len(self.home))
        lam_a = np.ones(len(self.home))
        for k in range(len(self.home)):
            if not self.played[k]:
                lh, la = model.expected_goals(self.team_ids[self.home[k]],
                                              self.team_ids[self.away[k]])
                lam_h[k], lam_a[k] = lh, la
        self.lam_h, self.lam_a = lam_h, lam_a
        self.match_numbers = matches["match_number"].astype(int).to_numpy()

    def current_points(self) -> dict[int, dict[str, int]]:
        """Standings from played matches only (the live table)."""
        return _accumulate(self.home[self.played], self.away[self.played],
                           self.fixed_hg[self.played], self.fixed_ag[self.played],
                           self.n, self.team_ids)


def _accumulate(home, away, hg, ag, n, team_ids) -> dict[int, dict[str, int]]:
    home_win = (hg > ag).astype(int)
    away_win = (ag > hg).astype(int)
    draw = (hg == ag).astype(int)
    pts = np.bincount(home, 3 * home_win + draw, minlength=n) \
        + np.bincount(away, 3 * away_win + draw, minlength=n)
    gf = np.bincount(home, hg, minlength=n) + np.bincount(away, ag, minlength=n)
    ga = np.bincount(home, ag, minlength=n) + np.bincount(away, hg, minlength=n)
    played = np.bincount(home, minlength=n) + np.bincount(away, minlength=n)
    return {team_ids[i]: {"pts": int(pts[i]), "gf": int(gf[i]),
                          "ga": int(ga[i]), "played": int(played[i])}
            for i in range(n)}


# ---------------------------------------------------------------------------
# One simulated season
# ---------------------------------------------------------------------------

def simulate_one(fx: SeasonFixtures, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Simulate a full season; return (rank[i], pts[i]) for team index i (rank 1=top)."""
    hg = fx.fixed_hg.copy()
    ag = fx.fixed_ag.copy()
    todo = ~fx.played
    hg[todo] = rng.poisson(fx.lam_h[todo])
    ag[todo] = rng.poisson(fx.lam_a[todo])

    stats = _accumulate(fx.home, fx.away, hg, ag, fx.n, fx.team_ids)
    matches = list(zip((fx.team_ids[i] for i in fx.home),
                       (fx.team_ids[i] for i in fx.away),
                       hg.tolist(), ag.tolist()))
    order = _rank_cluster(list(fx.team_ids), fx.cfg.tiebreakers, stats, matches)

    rank = np.empty(fx.n, dtype=int)
    pts = np.empty(fx.n, dtype=int)
    for pos, tid in enumerate(order, start=1):
        rank[fx.idx[tid]] = pos
    for i, tid in enumerate(fx.team_ids):
        pts[i] = stats[tid]["pts"]
    return rank, pts


def simulate_season(cfg: LeagueConfig, teams: pd.DataFrame, matches: pd.DataFrame,
                    model: LeagueModel, seed: int | None = None) -> pd.DataFrame:
    """Convenience single-run simulation returning a tidy final table."""
    fx = SeasonFixtures(cfg, teams, matches, model)
    rng = np.random.default_rng(seed)
    hg = fx.fixed_hg.copy(); ag = fx.fixed_ag.copy()
    todo = ~fx.played
    hg[todo] = rng.poisson(fx.lam_h[todo]); ag[todo] = rng.poisson(fx.lam_a[todo])
    stats = _accumulate(fx.home, fx.away, hg, ag, fx.n, fx.team_ids)
    matchlist = list(zip((fx.team_ids[i] for i in fx.home),
                         (fx.team_ids[i] for i in fx.away), hg.tolist(), ag.tolist()))
    order = _rank_cluster(list(fx.team_ids), cfg.tiebreakers, stats, matchlist)
    name_of = dict(zip(teams["id"].astype(int), teams["team_name"]))
    rows = []
    for pos, tid in enumerate(order, start=1):
        s = stats[tid]
        rows.append({"rank": pos, "team_id": tid, "team_name": name_of.get(tid, tid),
                     "played": s["played"], "pts": s["pts"], "gf": s["gf"],
                     "ga": s["ga"], "gd": s["gf"] - s["ga"]})
    return pd.DataFrame(rows)
