#!/usr/bin/env python3
"""
ingest.py
---------
Pluggable data ingestion for the league simulator. Every source writes the
same two canonical CSVs into ``data/leagues/<key>/`` so the model, simulator
and report are completely source-agnostic:

  teams.csv    : id, team_name, code
  matches.csv  : match_number, matchday, date, home_team_id, away_team_id,
                 home_goals, away_goals, xg_home, xg_away, played

Unplayed fixtures have empty goal/xg fields and ``played`` = False; the
simulator draws those, and fixes played ones to their recorded score.

Sources
-------
* ``fbref``       — the primary, xG-bearing source (via the ``soccerdata``
                    package). Requires outbound access to fbref.com, so it runs
                    locally or wherever FBref is allowlisted — not inside a
                    sandbox that blocks it.
* ``openfootball``— the openfootball GitHub JSON mirror (schedules + scores,
                    **no xG**). Reachable from restricted environments, used to
                    validate the full pipeline offline. xg columns are left
                    empty and the model falls back to goals.
* manual          — drop hand-made CSVs in the canonical schema; nothing to do.

Usage
-----
    venv/bin/python -m leagues.ingest eng --season 2024-25 --source openfootball
    venv/bin/python -m leagues.ingest eng --season 2025-26 --source fbref
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path

from .config import LeagueConfig, get_league

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "leagues"
OPENFOOTBALL_BASE = (
    "https://raw.githubusercontent.com/openfootball/football.json/master"
)

TEAM_FIELDS = ["id", "team_name", "code"]
MATCH_FIELDS = [
    "match_number", "matchday", "date", "home_team_id", "away_team_id",
    "home_goals", "away_goals", "xg_home", "xg_away", "played",
]


# ---------------------------------------------------------------------------
# Team-code generation
# ---------------------------------------------------------------------------

_STOPWORDS = {"FC", "AFC", "CF", "SC", "AC", "SSC", "US", "CD", "RC", "UD",
              "AS", "SS", "VfL", "VfB", "TSG", "SV", "1", "RCD", "OGC", "SD"}


def _make_code(name: str, taken: set[str]) -> str:
    """Derive a short, unique uppercase code from a club name."""
    words = [w for w in re.split(r"[\s.]+", name) if w and w.upper() not in _STOPWORDS]
    if not words:
        words = name.split()
    base = "".join(w[0] for w in words[:3]).upper()
    if len(base) < 3 and words:
        base = words[0][:3].upper()
    code, i = base, 1
    while code in taken or not code:
        code = f"{base[:2]}{i}"
        i += 1
    taken.add(code)
    return code


# ---------------------------------------------------------------------------
# openfootball source (GitHub mirror; schedules + scores, no xG)
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _matchday_num(round_label: str) -> int | str:
    m = re.search(r"(\d+)", round_label or "")
    return int(m.group(1)) if m else ""


def _full_time(score) -> list | None:
    """Full-time [home, away] from openfootball's dict or bare-list score shape.

    The feed mixes two encodings within a single file: ``{"ft": [h, a], ...}``
    and a bare ``[h, a]`` list (used for 0-0 results). ``None`` for either an
    absent score or a dict without an ``ft`` key.
    """
    if isinstance(score, dict):
        return score.get("ft")
    if isinstance(score, list):
        return score
    return None


def from_openfootball(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """Return (teams, matches) rows from the openfootball mirror."""
    url = f"{OPENFOOTBALL_BASE}/{season}/{cfg.openfootball_path}.json"
    payload = _fetch_json(url)
    raw_matches = payload.get("matches", [])

    # Assign stable ids in first-appearance order.
    name_to_id: dict[str, int] = {}
    codes_taken: set[str] = set()
    teams: list[dict] = []

    def team_id(name: str) -> int:
        if name not in name_to_id:
            tid = len(name_to_id) + 1
            name_to_id[name] = tid
            teams.append({"id": tid, "team_name": name,
                          "code": _make_code(name, codes_taken)})
        return name_to_id[name]

    matches: list[dict] = []
    for i, m in enumerate(raw_matches, start=1):
        home, away = m.get("team1"), m.get("team2")
        if not home or not away:
            continue
        hid, aid = team_id(home), team_id(away)
        ft = _full_time(m.get("score"))
        played = isinstance(ft, list) and len(ft) == 2 and all(g is not None for g in ft)
        matches.append({
            "match_number": i,
            "matchday": _matchday_num(m.get("round", "")),
            "date": m.get("date", ""),
            "home_team_id": hid,
            "away_team_id": aid,
            "home_goals": ft[0] if played else "",
            "away_goals": ft[1] if played else "",
            "xg_home": "",   # openfootball carries no xG
            "xg_away": "",
            "played": played,
        })

    return teams, matches


# ---------------------------------------------------------------------------
# FBref source (primary; xG-bearing) via soccerdata
# ---------------------------------------------------------------------------

def from_fbref(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """
    Return (teams, matches) rows from FBref via the soccerdata package.

    Requires ``pip install soccerdata`` and outbound access to fbref.com.
    Season is FBref-style, e.g. "2025-2026" or "2526".
    """
    try:
        import soccerdata as sd
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "The 'fbref' source needs the soccerdata package:\n"
            "    venv/bin/pip install soccerdata\n"
            "and outbound access to fbref.com. In a network-restricted "
            "environment use --source openfootball instead."
        ) from exc

    fbref = sd.FBref(leagues=cfg.fbref_league, seasons=season)
    schedule = fbref.read_schedule().reset_index()

    # soccerdata column names: 'home_team','away_team','score','date','week',
    # 'home_xg','away_xg' (xg present for played games).
    name_to_id: dict[str, int] = {}
    codes_taken: set[str] = set()
    teams: list[dict] = []

    def team_id(name: str) -> int:
        if name not in name_to_id:
            tid = len(name_to_id) + 1
            name_to_id[name] = tid
            teams.append({"id": tid, "team_name": name,
                          "code": _make_code(name, codes_taken)})
        return name_to_id[name]

    def parse_score(val) -> tuple[int, int] | None:
        if not isinstance(val, str) or "–" not in val and "-" not in val:
            return None
        sep = "–" if "–" in val else "-"
        try:
            h, a = (int(x.strip()) for x in val.split(sep)[:2])
            return h, a
        except ValueError:
            return None

    matches: list[dict] = []
    for i, row in enumerate(schedule.itertuples(index=False), start=1):
        d = row._asdict()
        home, away = d.get("home_team"), d.get("away_team")
        if not home or not away:
            continue
        hid, aid = team_id(str(home)), team_id(str(away))
        sc = parse_score(d.get("score"))
        played = sc is not None
        hx, ax = d.get("home_xg"), d.get("away_xg")
        matches.append({
            "match_number": i,
            "matchday": d.get("week", ""),
            "date": str(d.get("date", ""))[:10],
            "home_team_id": hid,
            "away_team_id": aid,
            "home_goals": sc[0] if played else "",
            "away_goals": sc[1] if played else "",
            "xg_home": round(float(hx), 3) if played and hx == hx and hx is not None else "",
            "xg_away": round(float(ax), 3) if played and ax == ax and ax is not None else "",
            "played": played,
        })

    return teams, matches


# ---------------------------------------------------------------------------
# Writing / CLI
# ---------------------------------------------------------------------------

def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_league(cfg: LeagueConfig, teams: list[dict], matches: list[dict]) -> Path:
    out_dir = DATA_ROOT / cfg.key
    _write_csv(out_dir / "teams.csv", TEAM_FIELDS, teams)
    _write_csv(out_dir / "matches.csv", MATCH_FIELDS, matches)
    return out_dir


SOURCES = {"openfootball": from_openfootball, "fbref": from_fbref}


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest league fixtures/results into canonical CSVs.")
    ap.add_argument("league", help="league key (eng, esp, ita, de, fr)")
    ap.add_argument("--season", required=True,
                    help="season, e.g. 2024-25 (openfootball) or 2025-2026 (fbref)")
    ap.add_argument("--source", default="openfootball", choices=list(SOURCES),
                    help="data source (default: openfootball)")
    args = ap.parse_args()

    cfg = get_league(args.league)
    teams, matches = SOURCES[args.source](cfg, args.season)

    n_played = sum(1 for m in matches if m["played"])
    n_xg = sum(1 for m in matches if m["xg_home"] != "")
    out_dir = write_league(cfg, teams, matches)

    print(f"{cfg.name} {args.season} [{args.source}]: "
          f"{len(teams)} teams, {len(matches)} fixtures "
          f"({n_played} played, {n_xg} with xG) -> {out_dir}")
    if n_played and not n_xg:
        print("  note: no xG in this source; the model will fall back to goals.")


if __name__ == "__main__":
    sys.exit(main())
