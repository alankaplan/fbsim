#!/usr/bin/env python3
"""
players.py
----------
Ingest individual **player season statistics** into a canonical
``data/leagues/<key>/players.csv``, mirroring how fixtures are ingested — the
report then shows them without caring where they came from.

Coverage forces the source split (fixturedownload has no player data at all):

  * Big-5 European leagues  -> Understat (its own xG/xA, no browser)
  * MLS / NWSL / USL        -> FBref     (only player source for US leagues; browser)

Each league's default is :pyattr:`LeagueConfig.player_source`; pass ``--source``
to override. Player rows are linked back to ``teams.csv`` by normalised team name
so the report can group a club's squad.

Canonical columns (the common core of both sources; blanks where a source lacks
one, e.g. shots is Understat-only):

    player_name, team_name, team_code, position, matches, minutes,
    goals, assists, xg, xa, shots, yellow_cards, red_cards

Usage
-----
    venv/bin/python -m leagues.players eng                 # default source, current season
    venv/bin/python -m leagues.players mls --season 2025   # FBref (browser)
    venv/bin/python -m leagues.players esp --source fbref  # override the source
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import pandas as pd

from .config import LeagueConfig, get_league
from .ingest import DATA_ROOT, ensure_league_dict

PLAYER_FIELDS = ["player_name", "team_name", "team_code", "position", "matches",
                 "minutes", "goals", "assists", "xg", "xa", "shots",
                 "yellow_cards", "red_cards"]

# Tokens dropped when matching a player-source team name to teams.csv.
_TEAM_STOP = {"fc", "afc", "cf", "sc", "ac", "ssc", "us", "cd", "rc", "ud", "as",
              "ss", "sv", "club", "de", "the", "1", "calcio"}


def _norm_team(name: str) -> str:
    toks = [t for t in re.split(r"[\s.\-']+", str(name).lower()) if t and t not in _TEAM_STOP]
    return " ".join(toks)


def _i(v):
    v = pd.to_numeric(v, errors="coerce")
    return "" if pd.isna(v) else int(v)


def _f(v):
    v = pd.to_numeric(v, errors="coerce")
    return "" if pd.isna(v) else round(float(v), 2)


def players_from_understat(cfg: LeagueConfig, season: str) -> list[dict]:
    """Player season stats from Understat via soccerdata (no browser; Big-5 only)."""
    try:
        import soccerdata as sd
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("The 'understat' source needs soccerdata: pip install soccerdata") from exc
    us = sd.Understat(leagues=cfg.fbref_league, seasons=season)
    df = us.read_player_season_stats().reset_index()
    rows = []
    for d in df.to_dict("records"):
        rows.append({
            "player_name": d.get("player", ""), "team_name": d.get("team", ""),
            "position": d.get("position", ""),
            "matches": _i(d.get("matches")), "minutes": _i(d.get("minutes")),
            "goals": _i(d.get("goals")), "assists": _i(d.get("assists")),
            "xg": _f(d.get("xg")), "xa": _f(d.get("xa")), "shots": _i(d.get("shots")),
            "yellow_cards": _i(d.get("yellow_cards")), "red_cards": _i(d.get("red_cards")),
        })
    return rows


def _flat(col) -> str:
    """Flatten a possibly-MultiIndex column to 'Level/Sub' (or 'Level')."""
    if isinstance(col, tuple):
        parts = [str(x) for x in col if x not in ("", None) and str(x) != "nan"]
        return "/".join(parts)
    return str(col)


def players_from_fbref(cfg: LeagueConfig, season: str) -> list[dict]:
    """Player season stats from FBref 'standard' via soccerdata (browser)."""
    try:
        import soccerdata as sd
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("The 'fbref' source needs soccerdata + a browser: pip install soccerdata") from exc
    from .ingest import _ensure_fbref_league, _hidden_display
    _ensure_fbref_league(sd, cfg)
    with _hidden_display():
        fb = sd.FBref(leagues=cfg.fbref_league, seasons=season)
        try:
            df = fb.read_player_season_stats(stat_type="standard").reset_index()
        except ValueError as exc:                  # FBref returned no player table (empty/CAPTCHA)
            if "No objects to concatenate" in str(exc):
                return []
            raise
    if df.empty:
        return []
    df.columns = [_flat(c) for c in df.columns]

    def g(d, *names):
        for n in names:
            if n in d and pd.notna(d[n]):
                return d[n]
        return None

    rows = []
    for d in df.to_dict("records"):
        rows.append({
            "player_name": g(d, "player") or "", "team_name": g(d, "team") or "",
            "position": g(d, "pos") or "",
            "matches": _i(g(d, "Playing Time/MP")), "minutes": _i(g(d, "Playing Time/Min")),
            "goals": _i(g(d, "Performance/Gls")), "assists": _i(g(d, "Performance/Ast")),
            "xg": _f(g(d, "Expected/xG")), "xa": _f(g(d, "Expected/xAG", "Expected/xA")),
            "shots": "",  # not in the 'standard' table
            "yellow_cards": _i(g(d, "Performance/CrdY")), "red_cards": _i(g(d, "Performance/CrdR")),
        })
    return rows


PLAYER_SOURCES = {
    "understat": players_from_understat,
    "fbref": players_from_fbref,
}


def build_players(cfg: LeagueConfig, season: str, source: str) -> Path:
    """Fetch player season stats, link each to teams.csv, write players.csv."""
    rows = PLAYER_SOURCES[source](cfg, season)

    # Resolve team_code from teams.csv by normalised name (exact when the fixtures
    # and player sources match — always true for the Big-5, both Understat).
    data_dir = DATA_ROOT / cfg.key
    code_by_norm, name_by_norm = {}, {}
    teams_csv = data_dir / "teams.csv"
    if teams_csv.exists():
        tdf = pd.read_csv(teams_csv)
        for nm, code in zip(tdf["team_name"], tdf["code"]):
            code_by_norm[_norm_team(nm)] = code
            name_by_norm[_norm_team(nm)] = nm

    out_rows = []
    for r in rows:
        if not r["player_name"] or r["minutes"] in ("", 0):   # drop non-players / no minutes
            continue
        key = _norm_team(r["team_name"])
        r["team_code"] = code_by_norm.get(key, "")
        r["team_name"] = name_by_norm.get(key, r["team_name"])
        out_rows.append(r)
    out_rows.sort(key=lambda r: (-(r["goals"] or 0), -(r["xg"] or 0)))

    out = data_dir / "players.csv"
    if not out_rows and out.exists():              # don't clobber good data with nothing
        print(f"  [players] no player data for {cfg.name} {season} — keeping existing players.csv")
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAYER_FIELDS)
        w.writeheader()
        w.writerows(out_rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest individual player season stats.")
    ap.add_argument("league", help="league key (eng, esp, ita, de, fr, mls, nwsl, usl)")
    ap.add_argument("--season", default=None,
                    help="season (default: the league's current season for its source)")
    ap.add_argument("--source", default=None, choices=list(PLAYER_SOURCES),
                    help="player-stats source (default: the league's own — understat "
                         "for the Big-5, fbref for the US leagues)")
    args = ap.parse_args()

    ensure_league_dict()                            # register custom leagues before soccerdata import
    cfg = get_league(args.league)
    source = args.source or cfg.player_source
    season = args.season or cfg.season_for(source)
    out = build_players(cfg, season, source)
    n = sum(1 for _ in csv.DictReader(out.open(encoding="utf-8")))
    print(f"{cfg.name} {season} [{source}]: {n} players -> {out}")


if __name__ == "__main__":
    sys.exit(main())
