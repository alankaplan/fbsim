#!/usr/bin/env python3
"""
prior.py
--------
Preseason priors: snapshot a league's *previous* season as ``prior.json`` so the
next season starts from informed team ratings instead of a blank slate.

``build_prior`` fetches the previous season's results (via the same ingest
sources, in memory — it never touches the current ``matches.csv``), fits the
attack/defense model, and writes ``data/leagues/<key>/prior.json``:

    {"source": ..., "season": ..., "n_played": ...,
     "intercept": ..., "home_adv": ...,
     "teams": {"<team name>": {"attack": .., "defense": ..}, ...}}

``load_prior`` reads it back into a :class:`leagues.model.LeaguePrior`, which
``fit_model`` shrinks toward (matched by team name; promoted teams absent from
the prior fall back to league average). Run once per season:

    venv/bin/python -m leagues.prior eng            # fit the season before the current one
    venv/bin/python -m leagues.prior mls --source fixturedownload --season 2025
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .config import LeagueConfig, get_league
from .model import LeaguePrior, fit_model

# How much of last season's attack/defense separation carries into a fresh
# season. Ratings are centered near league average (0), so scaling them by this
# factor regresses every team toward average — last season's champion becomes a
# favorite, not a preseason lock. 1.0 = no regression, 0.0 = flat league.
PRIOR_REGRESSION = 0.70


def _prev_season(source: str, current: str) -> str:
    """The season before ``current`` in the given source's format."""
    if source == "openfootball":
        start = int(current.split("-")[0])           # "2025-26" -> 2025
        return f"{start - 1}-{start % 100:02d}"      # -> "2024-25"
    if "-" in current:                               # fbref "2025-2026" -> "2024-2025"
        a, b = current.split("-")
        return f"{int(a) - 1}-{int(b) - 1}"
    return str(int(current) - 1)                     # fixturedownload "2025" -> "2024"


def prior_path(cfg: LeagueConfig) -> Path:
    from .ingest import DATA_ROOT
    return DATA_ROOT / cfg.key / "prior.json"


def build_prior(cfg: LeagueConfig, source: str, season: str) -> Path:
    """Fit ``season`` for ``cfg`` and write it as the preseason prior."""
    from .ingest import SOURCES
    teams_rows, matches_rows = SOURCES[source](cfg, season)
    teams_df = pd.DataFrame(teams_rows)
    matches_df = pd.DataFrame(matches_rows)
    model = fit_model(teams_df, matches_df)  # no prior — a plain single-season fit
    name_of = dict(zip(teams_df["id"].astype(int), teams_df["team_name"]))

    teams_out = {}
    for tid in model.team_ids:
        r = model.rating(tid)
        teams_out[name_of[tid]] = {"attack": round(r["attack"], 4),
                                   "defense": round(r["defense"], 4)}
    payload = {
        "source": source,
        "season": season,
        "n_played": int(sum(1 for m in matches_rows if m["played"])),
        "used_xg": model.used_xg,
        "intercept": round(model.intercept, 4),
        "home_adv": round(model.home_adv, 4),
        "teams": teams_out,
    }
    out = prior_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def load_prior(cfg: LeagueConfig,
               regression: float = PRIOR_REGRESSION) -> LeaguePrior | None:
    """Read ``prior.json`` into a LeaguePrior, or None if absent/unreadable.

    ``regression`` scales last season's attack/defense toward the league mean
    (see :data:`PRIOR_REGRESSION`); the raw ``prior.json`` is left untouched so
    the factor stays a run-time knob.
    """
    path = prior_path(cfg)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        teams = d.get("teams", {})
        k = float(regression)
        return LeaguePrior(
            attack={name: k * float(v["attack"]) for name, v in teams.items()},
            defense={name: k * float(v["defense"]) for name, v in teams.items()},
            intercept=float(d["intercept"]),
            home_adv=float(d["home_adv"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def main() -> None:
    from .ingest import SOURCES
    ap = argparse.ArgumentParser(
        description="Build a preseason prior from a league's previous season.")
    ap.add_argument("league", help="league key (eng, esp, ita, de, fr, mls, nwsl, usl)")
    ap.add_argument("--source", default=None, choices=list(SOURCES),
                    help="data source for the prior season (default: the league's own "
                         "— understat for the Big-5, fixturedownload for MLS/NWSL, "
                         "fbref for USL)")
    ap.add_argument("--season", default=None,
                    help="season to fit as the prior; default = the season before "
                         "the current one for this source")
    args = ap.parse_args()

    cfg = get_league(args.league)
    source = args.source or cfg.default_source
    season = args.season or _prev_season(source, cfg.season_for(source))
    out = build_prior(cfg, source, season)
    d = json.loads(out.read_text(encoding="utf-8"))
    print(f"{cfg.name}: prior from {season} [{source}] — {len(d['teams'])} teams, "
          f"{d['n_played']} played [{'xG' if d['used_xg'] else 'goals'}] -> {out}")


if __name__ == "__main__":
    sys.exit(main())
