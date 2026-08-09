#!/usr/bin/env python3
"""
national.py
-----------
Ingest **national-team fixtures and results** (USMNT / USWNT) into
``data/national/<key>.json`` for a *display-only* view in the report.

National teams are deliberately **not** modeled as leagues: they play friendlies
and tournaments against a rotating opponent set, with no league table, so nothing
here touches the season simulator (``run_sims``). This module only fetches a team's
recent results + upcoming games and writes them out; ``generate_page`` renders them
as a plain "National teams" tab.

Source is **API-Football** (api-sports.io v3). Its free tier (100 requests/day)
returns a national team's fixtures — home and away, results and upcoming — across
all competitions in two lookups per team. It needs a free API key: create one at
https://dashboard.api-football.com and put it in a file named ``api.key`` at the
repo root (gitignored). Without the key the tab simply stays empty.

(Earlier sources were dropped: ESPN hard-blocks some networks with 403; TheSportsDB's
free tier only returns home events and no women's senior team.)

Usage
-----
    venv/bin/python -m leagues.national all      # USMNT + USWNT
    venv/bin/python -m leagues.national usmnt     # one team
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Keep only games this recent (or in the future), so a rare stale result can't leak in.
RECENT_DAYS = 400

APIF_BASE = "https://v3.football.api-sports.io"
REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY_FILE = REPO_ROOT / "api.key"

# data/national/ (sibling of data/leagues/), gitignored like the rest of the data.
NATIONAL_ROOT = REPO_ROOT / "data" / "national"

# API-Football fixture status codes that mean the match is over (has a final score).
_FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}

# Each team: a display name, the API-Football search term, whether it's the women's
# side (to disambiguate the two "USA" national teams), and an optional pinned team id
# (set apif_id to skip runtime resolution if it ever picks the wrong team).
NATIONAL = [
    {"key": "usmnt", "name": "USMNT", "search": "USA", "women": False, "apif_id": None},
    {"key": "uswnt", "name": "USWNT", "search": "USA", "women": True, "apif_id": None},
]


def _read_key() -> str | None:
    """API-Football key from the api.key file (preferred) or API_FOOTBALL_KEY env."""
    try:
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    return os.environ.get("API_FOOTBALL_KEY") or None


# ---------------------------------------------------------------------------
# HTTP client: throttled + retried, sends the API-Football key header.
# ---------------------------------------------------------------------------
_THROTTLE_S = 0.4
_last_call = [0.0]


def _apif_get(path: str, key: str, tries: int = 3) -> dict:
    """GET an API-Football endpoint (path incl. query), spaced + retried with backoff."""
    url = f"{APIF_BASE}/{path}"
    headers = {"x-apisports-key": key, "Accept": "application/json"}
    last_exc = None
    for attempt in range(tries):
        wait = _THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:                       # network / decode — retry
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))             # 2s, 4s, 8s
    raise last_exc


# ---------------------------------------------------------------------------
# Team-id resolution (log the pick so a wrong id is visible / pinnable).
# ---------------------------------------------------------------------------
def _api_errors(payload: dict) -> str:
    """A human string for API-Football's in-body ``errors`` (200 OK but a problem), else ''."""
    errs = payload.get("errors")
    if isinstance(errs, dict) and errs:
        return "; ".join(f"{k}: {v}" for k, v in errs.items())
    if isinstance(errs, list) and errs:
        return "; ".join(str(e) for e in errs)
    return ""


def _resolve_team_id(entry: dict, key: str) -> int | None:
    """Resolve (and log) the API-Football team id for a US national side."""
    if entry.get("apif_id"):
        return int(entry["apif_id"])
    q = urllib.parse.urlencode({"search": entry["search"]})
    try:
        payload = _apif_get(f"teams?{q}", key)
    except Exception as exc:
        print(f"  [national] {entry['key']}: team search failed ({type(exc).__name__})")
        return None
    err = _api_errors(payload)
    if err:
        print(f"  [national] {entry['key']}: team search — API says: {err}")
    want_women = entry["women"]
    for item in payload.get("response", []):
        team = item.get("team", {})
        if not team.get("national"):
            continue
        if str(team.get("country", "")).lower() not in ("usa", "united states"):
            continue
        name = str(team.get("name", ""))
        is_women = ("women" in name.lower() or name.lower().endswith(" w")
                    or " w " in name.lower())
        if is_women == want_women:
            print(f"  [national] {entry['key']}: resolved to team {team.get('id')} "
                  f"'{name}'")
            return int(team["id"])
    print(f"  [national] {entry['key']}: no matching US national team found — skipped")
    return None


# ---------------------------------------------------------------------------
# Fixture parsing.
# ---------------------------------------------------------------------------
def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _parse_fixture(item: dict, team_id: int) -> dict | None:
    """One API-Football fixture -> a canonical game row from the USA's perspective."""
    fx = item.get("fixture", {})
    teams = item.get("teams", {})
    goals = item.get("goals", {})
    home, away = teams.get("home", {}), teams.get("away", {})
    if home.get("id") == team_id:
        venue, opponent = "home", away.get("name", "TBD")
        gf, ga = _int(goals.get("home")), _int(goals.get("away"))
    elif away.get("id") == team_id:
        venue, opponent = "away", home.get("name", "TBD")
        gf, ga = _int(goals.get("away")), _int(goals.get("home"))
    else:
        return None

    status = str(((fx.get("status") or {}).get("short")) or "")
    completed = status in _FINISHED and gf is not None and ga is not None
    result = ("W" if gf > ga else ("L" if gf < ga else "D")) if completed else ""
    dt = str(fx.get("date") or "")                     # ISO 8601 with offset (usually Z)
    return {
        "event_id": str(fx.get("id", "")),
        "date": dt[:10],
        "datetime_utc": dt,
        "competition": (item.get("league", {}) or {}).get("name", "") or "",
        "opponent": opponent or "TBD",
        "opp_code": "",
        "venue": venue,
        "gf": gf if completed else "",
        "ga": ga if completed else "",
        "result": result,
        "status": "completed" if completed else "scheduled",
    }


def fetch_games(entry: dict, season: int | None = None) -> list[dict]:
    """A national team's recent results + upcoming games (deduped by fixture id).

    Two lookups: ``fixtures?team={id}&last=12`` and ``&next=15``. ``season`` is accepted
    for signature compatibility but unused (last/next already scope the window). Games
    older than ``RECENT_DAYS`` are trimmed."""
    key = _read_key()
    if not key:
        print(f"  [national] {entry['key']}: no API-Football key — add it to "
              f"{API_KEY_FILE.name} (free at dashboard.api-football.com)")
        return []
    team_id = _resolve_team_id(entry, key)
    if not team_id:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    by_id: dict[str, dict] = {}
    for window in ("last=12", "next=15"):
        try:
            payload = _apif_get(f"fixtures?team={team_id}&{window}", key)
        except Exception as exc:
            print(f"  [national] {entry['key']}: fixtures {window} failed ({type(exc).__name__})")
            continue
        err = _api_errors(payload)
        if err:
            print(f"  [national] {entry['key']}: fixtures {window} — API says: {err}")
        elif not payload.get("response"):
            print(f"  [national] {entry['key']}: fixtures {window} — 0 fixtures returned")
        for item in payload.get("response", []):
            row = _parse_fixture(item, team_id)
            if not row or not row["event_id"]:
                continue
            if (row["date"] or "9999") < cutoff:       # too old to be "current" — drop
                continue
            by_id.setdefault(row["event_id"], row)
    games = list(by_id.values())
    games.sort(key=lambda g: g["datetime_utc"] or g["date"])
    if games:
        dates = [g["date"] for g in games if g["date"]]
        played = sum(1 for g in games if g["status"] == "completed")
        span = f"{min(dates)} → {max(dates)}" if dates else "?"
        print(f"  [national] {entry['key']}: {len(games)} games "
              f"({played} played, {len(games) - played} upcoming; {span})")
    return games


def national_path(key: str) -> Path:
    return NATIONAL_ROOT / f"{key}.json"


def build_national(entry: dict, season: int | None = None) -> Path:
    """Fetch a team's games and write data/national/<key>.json (non-destructive on empty)."""
    games = fetch_games(entry, season)
    out = national_path(entry["key"])
    if not games and out.exists():                     # don't clobber good data with nothing
        print(f"  [national] no games for {entry['name']} — keeping existing {out.name}")
        return out
    payload = {
        "key": entry["key"], "name": entry["name"], "apif_id": entry.get("apif_id"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    ap = argparse.ArgumentParser(description="Ingest USMNT/USWNT fixtures + results (API-Football).")
    ap.add_argument("team", nargs="?", default="all",
                    help="usmnt, uswnt, or all (default: all)")
    ap.add_argument("--season", type=int, default=None,
                    help="accepted for compatibility; ignored (last/next scope the window)")
    args = ap.parse_args()

    entries = NATIONAL if args.team == "all" else [_entry(args.team)]
    for entry in entries:
        out = build_national(entry, args.season)
        d = json.loads(out.read_text(encoding="utf-8"))
        games = d["games"]
        played = sum(1 for g in games if g["status"] == "completed")
        print(f"{entry['name']}: {len(games)} games ({played} played, "
              f"{len(games) - played} upcoming) -> {out}")


if __name__ == "__main__":
    sys.exit(main())
