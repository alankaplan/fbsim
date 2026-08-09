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

Source is **Wikipedia** (no key). The national-team articles carry a
"Results and fixtures" section built from ``{{Football box}}`` templates, which
render to ``<table class="footballbox">`` — recent results (with scores) *and*
upcoming fixtures (no score), across all competitions. We pull that section via the
MediaWiki parse API and read the boxes.

(Free APIs were all dead ends here: ESPN hard-blocks the network; TheSportsDB free
is home-events-only; API-Football free has no access to the current season.)

Usage
-----
    venv/bin/python -m leagues.national all      # USMNT + USWNT
    venv/bin/python -m leagues.national usmnt     # one team
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Keep only games this recent (or in the future); the section already scopes to ~a year.
RECENT_DAYS = 400

WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/html"
REPO_ROOT = Path(__file__).resolve().parent.parent
NATIONAL_ROOT = REPO_ROOT / "data" / "national"

# Each team: display name, Wikipedia article, and the text that marks the US side within
# a match row (so we can tell home from away and pick the opponent).
NATIONAL = [
    {"key": "usmnt", "name": "USMNT",
     "article": "United States men's national soccer team"},
    {"key": "uswnt", "name": "USWNT",
     "article": "United States women's national soccer team"},
]

_US_RE = re.compile(r"united states|(^|\W)usa(\W|$)", re.I)

# ---------------------------------------------------------------------------
# HTTP (throttled + retried).
# ---------------------------------------------------------------------------
_THROTTLE_S = 0.4
_last_call = [0.0]
_HEADERS = {"User-Agent": "fbsim/1.0 (national-team fixtures; contact via repo)"}


def _article_html(article: str, tries: int = 3) -> str:
    """The article's full HTML via the Wikipedia REST API (Parsoid markup)."""
    url = f"{WIKI_REST}/{urllib.parse.quote(article, safe='')}"
    last_exc = None
    for attempt in range(tries):
        wait = _THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))
    raise last_exc


# ---------------------------------------------------------------------------
# Section slicing + Football box parsing (modern {{Football box collapsible}}).
# ---------------------------------------------------------------------------
def _section_slice(doc: str) -> str:
    """The 'Results and fixtures' match-list region (down to the All-time summary/next h2)."""
    i = doc.find('id="Results_and_fixtures"')
    if i < 0:
        m = re.search(r'id="[^"]*(?:Fixtures|Recent_results)[^"]*"', doc)
        i = m.start() if m else -1
    if i < 0:
        return ""
    rest = doc[i + 1:]
    ends = [m.start() for m in (re.search(r'id="All-time_results"', rest),
                                re.search(r'<h2\b', rest)) if m]
    return rest[:min(ends)] if ends else rest


def _text(fragment: str) -> str:
    """HTML fragment -> plain text (strip tags, refs, entities, extra space)."""
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    fragment = re.sub(r"\[\d+\]", "", fragment)        # [1] ref marks
    return re.sub(r"\s+", " ", fragment).strip()


def _team_name(td_html: str) -> str:
    """Team name from a `.vcard attendee` cell, with flag-icon spans removed."""
    frag = re.sub(r'<span class="flagicon.*?</span>\s*</span>', " ", td_html, flags=re.I | re.S)
    frag = re.sub(r'<span class="flagicon[^"]*">.*?</span>', " ", frag, flags=re.I | re.S)
    return _text(frag)


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


def _infer_iso(date_txt: str, played: bool, today: date) -> str:
    """A Month-Day (no year) -> ISO date, inferring the year from played/upcoming.

    Wikipedia's fixtures list shows the last ~12 months + upcoming, without years. A
    played game in a month after this one must be last year; an upcoming game in a month
    before this one must be next year."""
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})", date_txt)
    if not m or m.group(1) not in _MONTHS:
        return ""
    mm, dd = _MONTHS[m.group(1)], int(m.group(2))
    if played:
        year = today.year if mm <= today.month else today.year - 1
    else:
        year = today.year if mm >= today.month else today.year + 1
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _parse_boxes(section_html: str, today: date | None = None) -> list[dict]:
    """Parse `tmpl-football-box-collapsible` match tables into canonical rows."""
    today = today or datetime.now(timezone.utc).date()
    rows: list[dict] = []
    for tbl in re.finditer(r'<table([^>]*)>(.*?)</table>', section_html, re.S):
        if "tmpl-football-box-collapsible" not in tbl.group(1):
            continue
        body = tbl.group(2)
        trm = re.search(r'<tr[^>]*>(.*?)</tr>', body, re.S)
        if not trm:
            continue
        tr = trm.group(1)
        tds = re.findall(r'<td([^>]*)>(.*?)</td>', tr, re.S)
        vcards = [c for a, c in tds if "vcard attendee" in a]
        if len(vcards) < 2:
            continue
        home, away = _team_name(vcards[0]), _team_name(vcards[1])
        us_home, us_away = bool(_US_RE.search(home)), bool(_US_RE.search(away))
        if not (us_home or us_away):
            continue
        opponent = (away if us_home else home) or "TBD"
        venue = "home" if us_home else "away"
        td0 = tds[0][1] if tds else ""
        small = re.search(r"<small[^>]*>(.*?)</small>", td0, re.S)
        comp = _text(small.group(1)) if small else ""
        date_txt = _text(re.sub(r"<small.*?</small>", " ", td0, flags=re.S))
        score = ""
        for a, c in tds:
            if "text-align:center" in a:
                score = _text(c)
                break
        sm = re.search(r"(\d+)\s*[–−\-]\s*(\d+)", score)
        completed = bool(sm)
        if completed:
            hs, as_ = int(sm.group(1)), int(sm.group(2))
            gf, ga = (hs, as_) if us_home else (as_, hs)
            result = "W" if gf > ga else ("L" if gf < ga else "D")
        else:
            gf = ga = None
            result = ""
        iso = _infer_iso(date_txt, completed, today)
        rows.append({
            "event_id": f"{iso}|{venue}|{re.sub(r'[^a-z]', '', opponent.lower())}",
            "date": iso,
            "datetime_utc": iso,                       # date only (no kickoff time)
            "competition": comp,
            "opponent": opponent,
            "opp_code": "",
            "venue": venue,
            "gf": gf if completed else "",
            "ga": ga if completed else "",
            "result": result,
            "status": "completed" if completed else "scheduled",
        })
    return rows


def fetch_games(entry: dict, season: int | None = None) -> list[dict]:
    """A national team's results + upcoming games, scraped from its Wikipedia article."""
    article = entry["article"]
    try:
        doc = _article_html(article)
    except Exception as exc:
        print(f"  [national] {entry['key']}: Wikipedia fetch failed ({type(exc).__name__})")
        return []
    section = _section_slice(doc)
    if not section:
        print(f"  [national] {entry['key']}: no 'Results and fixtures' section on '{article}'")
        return []

    n_boxes = len(re.findall(r'<table[^>]*tmpl-football-box-collapsible', section))
    rows = _parse_boxes(section)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    by_id: dict[str, dict] = {}
    for r in rows:
        if r["date"] and r["date"] < cutoff:
            continue
        by_id.setdefault(r["event_id"], r)
    games = list(by_id.values())
    games.sort(key=lambda g: g["date"] or "9999")
    dates = [g["date"] for g in games if g["date"]]
    played = sum(1 for g in games if g["status"] == "completed")
    span = f"{min(dates)} → {max(dates)}" if dates else "no dates"
    print(f"  [national] {entry['key']}: section had {n_boxes} match boxes → "
          f"{len(games)} games ({played} played, {len(games) - played} upcoming; {span})")
    return games


def national_path(key: str) -> Path:
    return NATIONAL_ROOT / f"{key}.json"


def build_national(entry: dict, season: int | None = None) -> Path:
    """Fetch a team's games and write data/national/<key>.json (non-destructive on empty)."""
    games = fetch_games(entry, season)
    out = national_path(entry["key"])
    if not games and out.exists():                     # don't clobber good data with nothing
        print(f"  [national] no games parsed for {entry['name']} — keeping existing {out.name}")
        return out
    payload = {
        "key": entry["key"], "name": entry["name"], "source": "wikipedia",
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
    ap = argparse.ArgumentParser(description="Ingest USMNT/USWNT fixtures + results (Wikipedia).")
    ap.add_argument("team", nargs="?", default="all", help="usmnt, uswnt, or all (default: all)")
    ap.add_argument("--season", type=int, default=None, help="accepted for compatibility; ignored")
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
