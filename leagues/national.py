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

Source is **TheSportsDB's free JSON API** (browserless, no key setup — the public
test key). It gives a team's recent results and next fixtures across *all*
competitions in just two lookups per team, so there are no request bursts:

    https://www.thesportsdb.com/api/v1/json/<key>/eventslast.php?id=<teamId>   # results
    https://www.thesportsdb.com/api/v1/json/<key>/eventsnext.php?id=<teamId>   # upcoming

(ESPN's API was the original source but hard-blocks some networks with 403s.)

Usage
-----
    venv/bin/python -m leagues.national all      # USMNT + USWNT
    venv/bin/python -m leagues.national usmnt     # one team
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Keep only games this recent (or in the future), so a rare stale result can't leak in.
RECENT_DAYS = 400

# TheSportsDB free/public test key. Swap for a personal key if you have one.
TSDB_KEY = "3"
TSDB_BASE = f"https://www.thesportsdb.com/api/v1/json/{TSDB_KEY}"

# data/national/ (sibling of data/leagues/), gitignored like the rest of the data.
NATIONAL_ROOT = Path(__file__).resolve().parent.parent / "data" / "national"

# Each team: a display name, the names to search TheSportsDB for, the gender that
# disambiguates the men's vs women's national side, and an optional pinned team id
# (set tsdb_id to skip runtime resolution if it ever picks the wrong team).
NATIONAL = [
    {"key": "usmnt", "name": "USMNT", "tsdb_search": ["USA", "United States"],
     "gender": "Male", "tsdb_id": None},
    {"key": "uswnt", "name": "USWNT", "tsdb_search": ["USA", "United States"],
     "gender": "Female", "tsdb_id": None},
]

# ---------------------------------------------------------------------------
# HTTP client: throttled + retried (gentle even though TheSportsDB is friendly).
# ---------------------------------------------------------------------------
_THROTTLE_S = 0.4
_last_call = [0.0]
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, */*"}


def _get_json(url: str, tries: int = 3):
    """GET a JSON endpoint, spaced ``_THROTTLE_S`` apart and retried with backoff."""
    last_exc = None
    for attempt in range(tries):
        wait = _THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise                                  # 4xx (other than 429) — don't retry
        except Exception as exc:                       # network / decode — retry
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))             # 2s, 4s, 8s
    raise last_exc


# ---------------------------------------------------------------------------
# Team-id resolution (TheSportsDB ids aren't guessable, so resolve + log them).
# ---------------------------------------------------------------------------
def _us_national_name(name: str) -> bool:
    """True if a team name is a US national side (a country form, not a club)."""
    base = re.sub(r"\bwomen\b|\(w\)|\bu-?\d+\b", "", str(name).lower())
    base = base.replace("national team", "").strip(" .-")
    return base in ("usa", "us", "united states", "united states of america")


def _team_gender(team: dict) -> str:
    """Male/Female for a TheSportsDB team, from strGender or a name marker."""
    g = str(team.get("strGender", "")).strip().capitalize()
    if g in ("Male", "Female"):
        return g
    name = str(team.get("strTeam", "")).lower()
    return "Female" if ("women" in name or "(w)" in name) else "Male"


def _resolve_tsdb_id(entry: dict) -> str | None:
    """Resolve (and log) TheSportsDB team id for a national side; honor a pinned id."""
    if entry.get("tsdb_id"):
        return str(entry["tsdb_id"])
    seen: set[str] = set()
    for name in entry["tsdb_search"]:
        try:
            payload = _get_json(f"{TSDB_BASE}/searchteams.php?t={urllib.parse.quote(name)}")
        except Exception as exc:
            print(f"  [national] {entry['key']}: search '{name}' failed ({type(exc).__name__})")
            continue
        for t in (payload.get("teams") or []):
            tid = str(t.get("idTeam", ""))
            if tid in seen:
                continue
            seen.add(tid)
            if (str(t.get("strSport")) == "Soccer" and _us_national_name(t.get("strTeam"))
                    and _team_gender(t) == entry["gender"]):
                print(f"  [national] {entry['key']}: resolved to idTeam {tid} "
                      f"'{t.get('strTeam')}'")
                return tid
    print(f"  [national] {entry['key']}: could not resolve a TheSportsDB team id — skipped")
    return None


# ---------------------------------------------------------------------------
# Event parsing.
# ---------------------------------------------------------------------------
def _int(v):
    """Int score, or None when absent/blank/'null'."""
    if v in (None, "", "null"):
        return None
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None


def _parse_event(ev: dict, team_id: str) -> dict | None:
    """One TheSportsDB event -> a canonical game row from the USA's perspective."""
    if str(ev.get("strSport", "Soccer")) != "Soccer":
        return None
    tid, id_home, id_away = str(team_id), str(ev.get("idHomeTeam", "")), str(ev.get("idAwayTeam", ""))
    if tid == id_home:
        venue, opponent = "home", ev.get("strAwayTeam", "TBD")
        gf, ga = _int(ev.get("intHomeScore")), _int(ev.get("intAwayScore"))
    elif tid == id_away:
        venue, opponent = "away", ev.get("strHomeTeam", "TBD")
        gf, ga = _int(ev.get("intAwayScore")), _int(ev.get("intHomeScore"))
    else:
        return None                                    # event not involving this team

    completed = gf is not None and ga is not None
    result = ("W" if gf > ga else ("L" if gf < ga else "D")) if completed else ""
    ts = str(ev.get("strTimestamp") or "").strip()
    dt_date = str(ev.get("dateEvent") or "")
    datetime_utc = (ts + "Z") if (ts and not ts.endswith("Z")) else (ts or dt_date)
    return {
        "event_id": str(ev.get("idEvent", "")),
        "date": dt_date,
        "datetime_utc": datetime_utc,
        "competition": ev.get("strLeague") or "",
        "opponent": opponent or "TBD",
        "opp_code": "",
        "venue": venue,
        "gf": gf if completed else "",
        "ga": ga if completed else "",
        "result": result,
        "status": "completed" if completed else "scheduled",
    }


def fetch_games(entry: dict, season: int | None = None) -> list[dict]:
    """A national team's recent results + upcoming games (deduped by event id).

    Two lookups: ``eventslast`` (recent results) and ``eventsnext`` (upcoming). ``season``
    is accepted for signature compatibility but unused — these endpoints already return
    the current window. Games older than ``RECENT_DAYS`` are trimmed."""
    team_id = _resolve_tsdb_id(entry)
    if not team_id:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    by_id: dict[str, dict] = {}
    for endpoint in ("eventslast.php", "eventsnext.php"):
        try:
            payload = _get_json(f"{TSDB_BASE}/{endpoint}?id={team_id}")
        except Exception as exc:
            print(f"  [national] {entry['key']}: {endpoint} failed ({type(exc).__name__})")
            continue
        for ev in (payload.get("results") or payload.get("events") or []):
            row = _parse_event(ev, team_id)
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
        "key": entry["key"], "name": entry["name"], "tsdb_id": entry.get("tsdb_id"),
        "season": season or date.today().year,
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
    ap = argparse.ArgumentParser(description="Ingest USMNT/USWNT fixtures + results (TheSportsDB).")
    ap.add_argument("team", nargs="?", default="all",
                    help="usmnt, uswnt, or all (default: all)")
    ap.add_argument("--season", type=int, default=None,
                    help="accepted for compatibility; ignored (the feed returns the "
                         "current recent+upcoming window)")
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
