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
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Keep only games this recent (or in the future) so a bare tournament fetch can't
# surface a stale past edition (e.g. World Cup 2022) alongside current fixtures.
RECENT_DAYS = 400

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# ESPN rate-limits bursts (a batch of quick requests gets 403'd wholesale), so keep a
# minimum spacing between calls and retry with backoff when it does push back.
_THROTTLE_S = 0.6
_last_call = [0.0]
_ESPN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/",
}


def _espn_get(url: str, tries: int = 3):
    """GET an ESPN JSON endpoint, throttled and retried with backoff.

    Spaces requests ``_THROTTLE_S`` apart (bursts get 403'd wholesale) and retries on
    403/429/5xx and transient network errors with 2s/4s/8s backoff, so a short-lived
    rate-limit cools off instead of failing the whole run."""
    last_exc = None
    for attempt in range(tries):
        wait = _THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=_ESPN_HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (403, 429, 500, 502, 503, 504):
                raise                                  # a real 404 etc. — don't retry
        except Exception as exc:                       # network / decode — retry
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))             # 2s, 4s, 8s
    raise last_exc

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
            ("fifa.worldq.concacaf", "World Cup Qual"),
            ("concacaf.gold", "Gold Cup"),
            ("fifa.world", "World Cup"),
            ("fifa.olympics", "Olympics"),
        ],
    },
    {
        "key": "uswnt", "name": "USWNT", "espn_id": None,
        "slugs": [
            ("fifa.friendly.w", "Friendly"),
            ("fifa.shebelieves", "SheBelieves Cup"),
            ("concacaf.w.gold", "W Gold Cup"),
            ("fifa.wwc", "World Cup"),
            ("fifa.w.olympics", "Olympics"),
        ],
    },
]

_US_NAMES = ("united states", "usa")


def _is_us(team: dict) -> bool:
    name = str(team.get("displayName", "")).lower()
    abbr = str(team.get("abbreviation", "")).lower()
    return any(n in name for n in _US_NAMES) or abbr in ("usa", "us")


def _resolve_team_id(slugs: list[tuple[str, str]]) -> int | None:
    """Find ESPN's team id for the USA, trying each competition's /teams list in turn.

    Friendly "leagues" often don't populate /teams, so we fall through to the next slug
    (a real tournament like concacaf.w.gold does list its teams)."""
    for slug, _label in slugs:
        try:
            payload = _espn_get(f"{ESPN_BASE}/{slug}/teams")
        except Exception:
            continue
        for sport in payload.get("sports", []):
            for lg in sport.get("leagues", []):
                for t in lg.get("teams", []):
                    team = t.get("team", t)
                    if _is_us(team):
                        try:
                            return int(team["id"])
                        except (KeyError, ValueError, TypeError):
                            pass
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


def _season_set(explicit: int | None) -> list[int | None]:
    """Which seasons to request per slug. Default = the bare (current) season plus this
    year — and next year only late in the season — so upcoming fixtures are caught even
    when ESPN's bare default lags the calendar, while keeping the request count low
    (ESPN 403s bursts). An explicit --season forces just that one year."""
    if explicit:
        return [explicit]
    today = date.today()
    seasons: list[int | None] = [None, today.year]
    if today.month >= 9:                               # late in the year, pull next year's early fixtures too
        seasons.append(today.year + 1)
    return seasons


def fetch_games(entry: dict, season: int | None = None) -> list[dict]:
    """Merge a national team's games across its competition slugs (deduped by event id).

    For each slug we request a *superset* of seasons (see :func:`_season_set`) and merge:
    the bare call gives ESPN's default season, the ``?season=`` calls catch upcoming
    fixtures the default may omit. A slug/season the team isn't in just 404s and is
    skipped. Games older than ``RECENT_DAYS`` are dropped so a stale past edition can't
    leak in."""
    team_id = entry.get("espn_id") or _resolve_team_id(entry["slugs"])
    if not team_id:
        print(f"  [national] {entry['key']}: could not resolve ESPN team id — skipped")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    seasons = _season_set(season)
    by_id: dict[str, dict] = {}
    for slug, label in entry["slugs"]:
        seen, dates, last_err = set(), [], None
        for s in seasons:
            url = f"{ESPN_BASE}/{slug}/teams/{team_id}/schedule"
            if s:
                url += f"?season={s}"
            try:
                payload = _espn_get(url)
            except urllib.error.HTTPError as exc:      # slug/season the team isn't in
                last_err = f"HTTP {exc.code}"
                continue
            except Exception as exc:                   # network / decode / other
                last_err = type(exc).__name__
                continue
            for event in payload.get("events", []):
                row = _parse_event(event, team_id, label)
                if not row or not row["event_id"]:
                    continue
                if (row["date"] or "9999") < cutoff:   # too old to be "current" — drop
                    continue
                if row["event_id"] not in seen:        # count each game once per slug
                    seen.add(row["event_id"])
                    if row["date"]:
                        dates.append(row["date"])
                by_id.setdefault(row["event_id"], row)
        if dates:                                      # one concise line per competition
            print(f"  [national] {entry['key']}: {slug} — {len(dates)} games "
                  f"({min(dates)} → {max(dates)})")
        else:
            print(f"  [national] {entry['key']}: {slug} — no current games"
                  f"{f' ({last_err})' if last_err else ''}")
    games = list(by_id.values())
    games.sort(key=lambda g: g["datetime_utc"] or g["date"])
    return games


def national_path(key: str) -> Path:
    return NATIONAL_ROOT / f"{key}.json"


def build_national(entry: dict, season: int | None = None) -> Path:
    """Fetch a team's calendar and write data/national/<key>.json (non-destructive on empty).

    ``season`` is optional: omit it (the default) to fetch each competition's current
    season; pass a year to force ``?season=`` on every request."""
    games = fetch_games(entry, season)
    out = national_path(entry["key"])
    if not games and out.exists():                     # don't clobber good data with nothing
        print(f"  [national] no games for {entry['name']} — keeping existing {out.name}")
        return out
    payload = {
        "key": entry["key"], "name": entry["name"], "espn_id": entry.get("espn_id"),
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
    ap = argparse.ArgumentParser(description="Ingest USMNT/USWNT fixtures + results (ESPN).")
    ap.add_argument("team", nargs="?", default="all",
                    help="usmnt, uswnt, or all (default: all)")
    ap.add_argument("--season", type=int, default=None,
                    help="force a specific calendar year via ?season= (default: each "
                         "competition's current season — recent + upcoming games)")
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
