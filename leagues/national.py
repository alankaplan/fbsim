#!/usr/bin/env python3
"""
national.py
-----------
Ingest **national-team fixtures and results** (USMNT / USWNT) into
``data/national/<key>.json`` for a *display-only* view in the report.

National teams are deliberately **not** modeled as leagues: they play friendlies
and tournaments against a rotating opponent set, with no league table, so nothing
here touches the season simulator (``run_sims``). This module only fetches a team's
schedule + results and writes them out; ``generate_page`` renders them as a plain
"National teams" tab.

Source is **ESPN's public JSON API** (browserless, no key) — the same simple GET
the league ingest uses. ESPN's team-schedule endpoint is *per competition*, so a
team's full calendar is assembled by fetching a fixed set of competition slugs and
merging the events (deduped by ESPN event id):

    https://site.api.espn.com/apis/site/v2/sports/soccer/<slug>/teams/<id>/schedule?season=<year>

Usage
-----
    venv/bin/python -m leagues.national all               # USMNT + USWNT, current year
    venv/bin/python -m leagues.national usmnt              # one team
    venv/bin/python -m leagues.national uswnt --season 2026
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .ingest import _fetch_json

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# data/national/ (sibling of data/leagues/), gitignored like the rest of the data.
NATIONAL_ROOT = Path(__file__).resolve().parent.parent / "data" / "national"

# Each team: the competitions to merge, as (ESPN league slug, readable label). The
# slug list is easy to extend; a slug a team isn't in for a given season just yields
# no events and is skipped. espn_id is ESPN's team id (660 = USA men, documented);
# None is resolved at runtime from a slug's /teams list.
NATIONAL = [
    {
        "key": "usmnt", "name": "USMNT", "espn_id": 660,
        "slugs": [
            ("fifa.friendly", "Friendly"),
            ("concacaf.nations.league", "Nations League"),
            ("concacaf.nations.league_qual", "Nations League Qual"),
            ("fifa.worldq.concacaf", "World Cup Qual"),
            ("concacaf.gold", "Gold Cup"),
            ("fifa.world", "World Cup"),
        ],
    },
    {
        "key": "uswnt", "name": "USWNT", "espn_id": None,
        "slugs": [
            ("fifa.friendly.w", "Friendly"),
            ("concacaf.w.gold", "W Gold Cup"),
            ("concacaf.w.championship", "W Championship"),
            ("fifa.wwc", "World Cup"),
            ("fifa.olympics.w", "Olympics"),
        ],
    },
]

_US_NAMES = ("united states", "usa")


def _is_us(team: dict) -> bool:
    name = str(team.get("displayName", "")).lower()
    abbr = str(team.get("abbreviation", "")).lower()
    return any(n in name for n in _US_NAMES) or abbr in ("usa", "us")


def _resolve_team_id(slug: str) -> int | None:
    """Find ESPN's team id for the USA within a competition's /teams list."""
    try:
        payload = _fetch_json(f"{ESPN_BASE}/{slug}/teams")
    except Exception:
        return None
    for sport in payload.get("sports", []):
        for lg in sport.get("leagues", []):
            for t in lg.get("teams", []):
                team = t.get("team", t)
                if _is_us(team):
                    try:
                        return int(team["id"])
                    except (KeyError, ValueError, TypeError):
                        return None
    return None


def _score(competitor: dict):
    """A competitor's numeric score, or None when not yet played."""
    s = competitor.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    if s in (None, ""):
        return None
    try:
        return int(round(float(s)))
    except (ValueError, TypeError):
        return None


def _parse_event(event: dict, team_id: int, label: str) -> dict | None:
    """One ESPN schedule event -> a canonical game row from the USA's perspective."""
    comps = event.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors") or []
    us = next((c for c in competitors if str(c.get("team", {}).get("id")) == str(team_id)), None)
    if us is None:
        us = next((c for c in competitors if _is_us(c.get("team", {}))), None)
    opp = next((c for c in competitors if c is not us), None)
    if us is None or opp is None:
        return None

    dt = event.get("date") or comp.get("date") or ""
    neutral = bool(comp.get("neutralSite"))
    venue = "neutral" if neutral else ("home" if us.get("homeAway") == "home" else "away")
    completed = bool(((comp.get("status") or {}).get("type") or {}).get("completed"))
    gf, ga = _score(us), _score(opp)
    if completed and gf is not None and ga is not None:
        result = "W" if gf > ga else ("L" if gf < ga else "D")
    else:
        completed = False
        result = ""

    opp_team = opp.get("team", {})
    return {
        "event_id": str(event.get("id", "")),
        "date": dt[:10] if dt else "",
        "datetime_utc": dt,
        "competition": label,
        "opponent": opp_team.get("displayName", opp_team.get("name", "TBD")),
        "opp_code": opp_team.get("abbreviation", ""),
        "venue": venue,
        "gf": gf if completed else "",
        "ga": ga if completed else "",
        "result": result,
        "status": "completed" if completed else "scheduled",
    }


def fetch_games(entry: dict, year: int) -> list[dict]:
    """Merge a national team's games across its competition slugs (deduped by event id)."""
    team_id = entry.get("espn_id") or _resolve_team_id(entry["slugs"][0][0])
    if not team_id:
        print(f"  [national] {entry['key']}: could not resolve ESPN team id — skipped")
        return []
    by_id: dict[str, dict] = {}
    for slug, label in entry["slugs"]:
        url = f"{ESPN_BASE}/{slug}/teams/{team_id}/schedule?season={year}"
        try:
            payload = _fetch_json(url)
        except Exception as exc:                       # a slug the team isn't in / 404 / 403
            print(f"  [national] {entry['key']}: {slug} unavailable ({type(exc).__name__}) — skipped")
            continue
        for event in payload.get("events", []):
            row = _parse_event(event, team_id, label)
            if row and row["event_id"] and row["event_id"] not in by_id:
                by_id[row["event_id"]] = row
    games = list(by_id.values())
    games.sort(key=lambda g: g["datetime_utc"] or g["date"])
    return games


def national_path(key: str) -> Path:
    return NATIONAL_ROOT / f"{key}.json"


def build_national(entry: dict, year: int) -> Path:
    """Fetch a team's calendar and write data/national/<key>.json (non-destructive on empty)."""
    games = fetch_games(entry, year)
    out = national_path(entry["key"])
    if not games and out.exists():                     # don't clobber good data with nothing
        print(f"  [national] no games for {entry['name']} {year} — keeping existing {out.name}")
        return out
    payload = {
        "key": entry["key"], "name": entry["name"], "espn_id": entry.get("espn_id"),
        "season": year, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "games": games,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def _entry(key: str) -> dict:
    for e in NATIONAL:
        if e["key"] == key:
            return e
    raise SystemExit(f"Unknown national team '{key}'. Choose: {', '.join(e['key'] for e in NATIONAL)}, all")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest USMNT/USWNT fixtures + results (ESPN).")
    ap.add_argument("team", nargs="?", default="all",
                    help="usmnt, uswnt, or all (default: all)")
    ap.add_argument("--season", type=int, default=None,
                    help="calendar year to fetch (default: current year)")
    args = ap.parse_args()

    year = args.season or date.today().year
    entries = NATIONAL if args.team == "all" else [_entry(args.team)]
    for entry in entries:
        out = build_national(entry, year)
        d = json.loads(out.read_text(encoding="utf-8"))
        games = d["games"]
        played = sum(1 for g in games if g["status"] == "completed")
        print(f"{entry['name']} {year}: {len(games)} games ({played} played, "
              f"{len(games) - played} upcoming) -> {out}")


if __name__ == "__main__":
    sys.exit(main())
