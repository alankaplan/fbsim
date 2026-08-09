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

WIKI_API = "https://en.wikipedia.org/w/api.php"
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


def _wiki_get(params: dict, tries: int = 3) -> dict:
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
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
                raise
        except Exception as exc:
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))
    raise last_exc


# ---------------------------------------------------------------------------
# Wikipedia section + Football box parsing.
# ---------------------------------------------------------------------------
def _find_section(article: str) -> int | None:
    """Index of the article's results/fixtures section (prefer one naming 'fixtures')."""
    data = _wiki_get({"action": "parse", "page": article, "prop": "sections",
                      "format": "json"})
    sections = data.get("parse", {}).get("sections", [])
    best = None
    for s in sections:
        line = str(s.get("line", "")).lower()
        if "fixture" in line:                          # "Results and fixtures" / "Fixtures"
            return int(s["index"])
        if best is None and ("result" in line or "schedule" in line):
            best = int(s["index"])
    return best


def _section_html(article: str, idx: int) -> str:
    data = _wiki_get({"action": "parse", "page": article, "section": idx,
                      "prop": "text", "format": "json"})
    return data.get("parse", {}).get("text", {}).get("*", "")


def _text(fragment: str) -> str:
    """HTML fragment -> plain text (strip tags, refs, flags, entities, extra space)."""
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    fragment = re.sub(r"\[\d+\]", "", fragment)        # [1] ref marks
    return re.sub(r"\s+", " ", fragment).strip()


def _cell(row_html: str, cls: str) -> str:
    m = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</t[hd]>', row_html,
                  re.I | re.S)
    return _text(m.group(1)) if m else ""


def _team_cell(row_html: str, cls: str) -> str:
    """A team cell's name, with flag-icon spans removed so only the team text remains."""
    m = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</t[hd]>', row_html,
                  re.I | re.S)
    if not m:
        return ""
    frag = re.sub(r'<span class="flagicon".*?</span>', " ", m.group(1), flags=re.I | re.S)
    return _text(frag)


_DATE_PATS = [
    (re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})"), "%B %d, %Y"),   # June 7, 2026
    (re.compile(r"(\d{1,2} [A-Z][a-z]+ \d{4})"), "%d %B %Y"),     # 7 June 2026
]


def _parse_date(text: str) -> str:
    for pat, fmt in _DATE_PATS:
        m = pat.search(text)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


def _parse_boxes(section_html: str) -> list[dict]:
    """Parse the section's <table class="footballbox"> boxes into canonical rows.

    Competition is taken from the most recent heading (<h3>/<h4>) above each box."""
    rows: list[dict] = []
    comp = ""
    # Walk headings and football boxes in document order.
    token = re.compile(
        r'<h[234][^>]*>(?P<head>.*?)</h[234]>'
        r'|<table[^>]*class="[^"]*\bfootballbox\b[^"]*"[^>]*>(?P<box>.*?)</table>',
        re.I | re.S)
    for m in token.finditer(section_html):
        if m.group("head") is not None:
            head = m.group("head")
            hl = re.search(r'class="[^"]*\bmw-headline\b[^"]*"[^>]*>(.*?)</span>', head,
                           re.I | re.S)
            comp = _text(hl.group(1) if hl else head)
            continue
        box = m.group("box")
        date_txt = _cell(box, "fdate") or _cell(box, "fdatetime")
        home = _team_cell(box, "fhome")
        away = _team_cell(box, "faway")
        score = _cell(box, "fscore")
        if not home and not away:
            continue
        us_home = bool(_US_RE.search(home))
        us_away = bool(_US_RE.search(away))
        if not (us_home or us_away):
            continue                                   # not a US match (stray box)
        opponent = away if us_home else home
        venue = "home" if us_home else "away"
        sm = re.search(r"(\d+)\s*[–−\-]\s*(\d+)", score)
        if sm:
            hs, as_ = int(sm.group(1)), int(sm.group(2))
            gf, ga = (hs, as_) if us_home else (as_, hs)
            result = "W" if gf > ga else ("L" if gf < ga else "D")
            completed = True
        else:
            gf = ga = None
            result, completed = "", False
        iso = _parse_date(date_txt)
        rows.append({
            "event_id": f"{iso}|{venue}|{re.sub(r'[^a-z]', '', opponent.lower())}",
            "date": iso,
            "datetime_utc": iso,                       # date only (Wikipedia has no kickoff)
            "competition": comp,
            "opponent": re.sub(r"\s*\(.*?\)\s*$", "", opponent).strip() or "TBD",
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
        idx = _find_section(article)
    except Exception as exc:
        print(f"  [national] {entry['key']}: Wikipedia fetch failed ({type(exc).__name__})")
        return []
    if idx is None:
        print(f"  [national] {entry['key']}: no results/fixtures section found on "
              f"'{article}'")
        return []
    try:
        section = _section_html(article, idx)
    except Exception as exc:
        print(f"  [national] {entry['key']}: section fetch failed ({type(exc).__name__})")
        return []

    n_boxes = len(re.findall(r'class="[^"]*\bfootballbox\b', section))
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
