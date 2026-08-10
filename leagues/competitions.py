#!/usr/bin/env python3
"""
competitions.py
---------------
Ingest **cup competitions** — the Leagues Cup and the UEFA Champions League —
into ``data/competitions/<key>.json`` for a *display-only* view in the report.

These don't fit the season simulator: the 2026 Leagues Cup is a phase-one table
per league feeding a single-elimination knockout, and the 2026–27 Champions
League is a 36-team Swiss "league phase" feeding a knockout bracket. Neither is a
home-and-away round-robin, so nothing here touches ``run_sims``. This module only
fetches each competition's current standings and its results + upcoming fixtures
(grouped by round) and writes them out; ``generate_page`` renders them as a plain
"Competitions" tab — no predictions, no simulation.

Source is **Wikipedia** (no key), via the shared ``wiki`` client. Each article
carries standings ``wikitable``s and ``{{Football box collapsible}}`` match tables
under round headings, which we read directly.

Usage
-----
    venv/bin/python -m leagues.competitions all         # Leagues Cup + UCL
    venv/bin/python -m leagues.competitions ucl         # one competition
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .wiki import article_html, text

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPETITIONS_ROOT = REPO_ROOT / "data" / "competitions"

# Each competition: display name + the Wikipedia article to scrape.
COMPETITIONS = [
    {"key": "leaguescup", "name": "Leagues Cup", "article": "2026 Leagues Cup"},
    {"key": "ucl", "name": "Champions League", "article": "2026–27 UEFA Champions League"},
]

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}

# Standings header text -> canonical column key.
_COL = {
    "pos": "pos", "r": "pos", "rk": "pos", "#": "pos", "": "pos",
    "team": "team", "club": "team", "teamvte": "team",
    "pld": "pld", "mp": "pld", "p": "pld",
    "w": "w", "d": "d", "l": "l",
    "gf": "gf", "ga": "ga", "gd": "gd", "gr": "gd",
    "pts": "pts", "points": "pts",
}


# ---------------------------------------------------------------------------
# Dates.
# ---------------------------------------------------------------------------
def _season_years(article: str) -> tuple[int, ...]:
    """Season year(s) implied by the article title, e.g. '2026–27 …' -> (2026, 2027)."""
    m = re.match(r"\s*(\d{4})(?:[–—-](\d{2,4}))?", article)
    if not m:
        return (datetime.now(timezone.utc).year,)
    y0 = int(m.group(1))
    if not m.group(2):
        return (y0,)
    tail = m.group(2)
    y1 = int(tail) if len(tail) == 4 else (y0 // 100) * 100 + int(tail)
    return (y0, y1)


def _parse_date(date_txt: str, years: tuple[int, ...]) -> str:
    """A football-box date cell -> ISO date. Handles full dates and, failing that,
    a bare day/month whose year is inferred from the season (Aug+ = first year)."""
    def _iso(dd: int, month: str, yy: int | None) -> str:
        mm = _MONTHS.get(month)
        if not mm:
            return ""
        if yy is None:
            yy = years[0] if (len(years) == 1 or mm >= 7) else years[1]
        return f"{yy:04d}-{mm:02d}-{dd:02d}"

    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})", date_txt)      # 16 September 2026
    if m:
        return _iso(int(m.group(1)), m.group(2), int(m.group(3)))
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", date_txt)    # September 16, 2026
    if m:
        return _iso(int(m.group(2)), m.group(1), int(m.group(3)))
    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]+)", date_txt)               # 16 September
    if m:
        return _iso(int(m.group(1)), m.group(2), None)
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})", date_txt)               # September 16
    if m:
        return _iso(int(m.group(2)), m.group(1), None)
    return ""


# ---------------------------------------------------------------------------
# HTML helpers.
# ---------------------------------------------------------------------------
def _clean_team(cell_html: str) -> str:
    """Team name from a table cell, flag-icon spans and trailing note markers removed."""
    frag = re.sub(r'<span class="flagicon.*?</span>\s*</span>', " ", cell_html, flags=re.I | re.S)
    frag = re.sub(r'<span class="flagicon[^"]*">.*?</span>', " ", frag, flags=re.I | re.S)
    name = text(frag)
    return re.sub(r"\s*\((?:[A-Z]|title holder|host)\)\s*$", "", name).strip()


def _headings(doc: str) -> list[tuple[int, str]]:
    """(start-offset, text) for every h2/h3/h4 heading, in document order."""
    out = []
    for m in re.finditer(r"<(h[234])\b[^>]*>(.*?)</\1>", doc, re.S):
        out.append((m.start(), text(m.group(2))))
    return out


def _round_for(pos: int, heads: list[tuple[int, str]]) -> str:
    """The nearest heading text preceding `pos` (the round a match sits under)."""
    name = ""
    for start, txt in heads:
        if start < pos:
            name = txt
        else:
            break
    return name


# ---------------------------------------------------------------------------
# Standings (wikitable with Pld + Pts columns).
# ---------------------------------------------------------------------------
def _parse_standings(doc: str) -> list[dict]:
    """Every league/group table on the page -> [{title, rows:[{pos,team,pld..pts}]}]."""
    heads = _headings(doc)
    groups: list[dict] = []
    for tbl in re.finditer(r'<table([^>]*)>(.*?)</table>', doc, re.S):
        if "wikitable" not in tbl.group(1):
            continue
        body = tbl.group(2)
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        if not trs:
            continue
        headers = [text(c).lower() for c in re.findall(r"<th[^>]*>(.*?)</th>", trs[0], re.S)]
        cmap = {i: _COL[h] for i, h in enumerate(headers) if h in _COL}
        if "pts" not in cmap.values() or "pld" not in cmap.values() or "team" not in cmap.values():
            continue
        rows = []
        for tr in trs[1:]:
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
            if len(cells) < 3:
                continue
            row: dict = {}
            for i, cell in enumerate(cells):
                key = cmap.get(i)
                if not key:
                    continue
                if key == "team":
                    row["team"] = _clean_team(cell)
                elif key == "pos":
                    row["pos"] = re.sub(r"\D", "", text(cell))
                else:
                    v = text(cell)
                    m = re.search(r"-?\d+", v)
                    row[key] = int(m.group()) if m else None
            if row.get("team") and row.get("pts") is not None:
                rows.append(row)
        if rows:
            groups.append({"title": _round_for(tbl.start(), heads), "rows": rows})
    return groups


# ---------------------------------------------------------------------------
# Matches (modern {{Football box collapsible}}), grouped by round.
# ---------------------------------------------------------------------------
def _parse_matches(doc: str, years: tuple[int, ...]) -> list[dict]:
    """Every football-box match -> canonical two-sided rows tagged with their round."""
    heads = _headings(doc)
    rows: list[dict] = []
    for tbl in re.finditer(r'<table([^>]*)>(.*?)</table>', doc, re.S):
        if "tmpl-football-box-collapsible" not in tbl.group(1):
            continue
        body = tbl.group(2)
        trm = re.search(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        if not trm:
            continue
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", trm.group(1), re.S)
        vcards = [c for a, c in tds if "vcard attendee" in a]
        if len(vcards) < 2:
            continue
        home, away = _clean_team(vcards[0]), _clean_team(vcards[1])
        td0 = tds[0][1] if tds else ""
        date_txt = text(re.sub(r"<small.*?</small>", " ", td0, flags=re.S))
        iso = _parse_date(date_txt, years)
        score = ""
        for a, c in tds:
            if "text-align:center" in a:
                score = text(c)
                break
        sm = re.search(r"(\d+)\s*[–−\-]\s*(\d+)", score)
        completed = bool(sm)
        hs = int(sm.group(1)) if completed else None
        as_ = int(sm.group(2)) if completed else None
        rnd = _round_for(tbl.start(), heads)
        rows.append({
            "event_id": f"{iso}|{re.sub(r'[^a-z]', '', home.lower())}|{re.sub(r'[^a-z]', '', away.lower())}",
            "date": iso,
            "datetime_utc": iso,                       # date only (no kickoff time)
            "round": rnd,
            "home": home,
            "away": away,
            "hs": hs if completed else "",
            "as": as_ if completed else "",
            "status": "completed" if completed else "scheduled",
        })
    return rows


# ---------------------------------------------------------------------------
# Fetch + write.
# ---------------------------------------------------------------------------
def fetch_competition(entry: dict) -> dict:
    """A competition's standings + round-grouped matches, scraped from Wikipedia."""
    article = entry["article"]
    years = _season_years(article)
    try:
        doc = article_html(article)
    except Exception as exc:  # noqa: BLE001
        print(f"  [competitions] {entry['key']}: Wikipedia fetch failed ({type(exc).__name__})")
        return {"standings": [], "rounds": []}

    standings = _parse_standings(doc)
    matches = _parse_matches(doc, years)

    # Group matches by round, preserving first-seen order.
    rounds: list[dict] = []
    index: dict[str, dict] = {}
    seen: set[str] = set()
    for m in matches:
        if m["event_id"] in seen:
            continue
        seen.add(m["event_id"])
        name = m["round"] or "Fixtures"
        grp = index.get(name)
        if grp is None:
            grp = {"name": name, "matches": []}
            index[name] = grp
            rounds.append(grp)
        grp["matches"].append(m)

    n_played = sum(1 for m in matches if m["status"] == "completed")
    print(f"  [competitions] {entry['key']}: {len(standings)} standings tables, "
          f"{len(seen)} matches across {len(rounds)} rounds ({n_played} played)")
    return {"standings": standings, "rounds": rounds}


def competition_path(key: str) -> Path:
    return COMPETITIONS_ROOT / f"{key}.json"


def build_competition(entry: dict) -> Path:
    """Fetch a competition and write data/competitions/<key>.json (non-destructive on empty)."""
    data = fetch_competition(entry)
    out = competition_path(entry["key"])
    empty = not data["standings"] and not data["rounds"]
    if empty and out.exists():                         # don't clobber good data with nothing
        print(f"  [competitions] no data parsed for {entry['name']} — keeping existing {out.name}")
        return out
    payload = {
        "key": entry["key"], "name": entry["name"], "article": entry["article"],
        "season": "–".join(str(y) for y in _season_years(entry["article"])),
        "source": "wikipedia",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standings": data["standings"],
        "rounds": data["rounds"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def _entry(key: str) -> dict:
    for e in COMPETITIONS:
        if e["key"] == key:
            return e
    raise SystemExit(f"Unknown competition '{key}'. Choose: "
                     f"{', '.join(e['key'] for e in COMPETITIONS)}, all")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Leagues Cup / Champions League (Wikipedia).")
    ap.add_argument("competition", nargs="?", default="all",
                    help="leaguescup, ucl, or all (default: all)")
    args = ap.parse_args()

    entries = COMPETITIONS if args.competition == "all" else [_entry(args.competition)]
    for entry in entries:
        out = build_competition(entry)
        d = json.loads(out.read_text(encoding="utf-8"))
        n_matches = sum(len(r["matches"]) for r in d["rounds"])
        print(f"{entry['name']}: {len(d['standings'])} tables, {n_matches} matches -> {out}")


if __name__ == "__main__":
    sys.exit(main())
