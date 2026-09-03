#!/usr/bin/env python3
"""
headshots.py
------------
Look up player headshots on Wikipedia and cache the image URLs for the report's
player cards, into ``data/headshots/<league>.json``.

Wikimedia is the only source with clear reuse rights — Understat carries no photos, and
FBref / league CDNs serve copyrighted press images. We store the **URL only** and let the
page reference it, so nothing is downloaded or redistributed here; the page stays small
and needs no image assets beside it (headshots simply don't appear offline).

Matching a player name to the right article is the risk: bare names are ambiguous
("Rodrigo" is a name-etymology article, not a footballer). A wrong face is far more
visibly wrong than no face, so a result is only accepted when the article's own intro
reads like a footballer's, and anything doubtful is recorded as a miss instead.

Both hits and misses are cached, so re-runs cost nothing; ``--refresh`` ignores the cache.
Deliberately standalone — it is NOT part of ``update --all``, which stays fast.

Usage
-----
    venv/bin/python -m leagues.headshots eng            # one league
    venv/bin/python -m leagues.headshots all --limit 60 # every league, top 60 each
    venv/bin/python -m leagues.headshots eng --refresh  # ignore cached misses
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import LEAGUES, get_league
from .ingest import DATA_ROOT
from .wiki import api_json

HEADSHOTS_ROOT = DATA_ROOT.parent / "headshots"
THUMB_PX = 200
DEFAULT_LIMIT = 60                      # top-N players per league by G+A

# The article intro must look like a footballer's, or we treat it as a miss.
_FOOTBALL = re.compile(r"\bfootball(er)?\b|\bsoccer\b", re.I)


def _clean_url(url: str) -> str:
    """Drop the API's utm_* tracking params; the bare upload.wikimedia.org URL is what we want."""
    return re.sub(r"[?&]utm_[^&]*", "", url or "").rstrip("?&")


def _lookup(title: str) -> dict | None:
    """Article -> {img, page, file} when it has a lead image AND reads like a footballer."""
    data = api_json(action="query", prop="pageimages|extracts", piprop="thumbnail|name",
                    pithumbsize=str(THUMB_PX), exintro="1", explaintext="1",
                    redirects="1", titles=title)
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return None
    pg = pages[0]
    if pg.get("missing") or not pg.get("thumbnail"):
        return None
    extract = pg.get("extract") or ""          # full extract — never truncate before matching
    if not _FOOTBALL.search(extract):
        return None
    return {"img": _clean_url((pg.get("thumbnail") or {}).get("source", "")),
            "page": "https://en.wikipedia.org/wiki/" + (pg.get("title") or title).replace(" ", "_"),
            "file": pg.get("pageimage") or ""}


def _credit(file_name: str) -> tuple[str, str]:
    """(author, licence) for a File: page — CC-BY-SA requires attribution when displayed."""
    if not file_name:
        return "", ""
    try:
        data = api_json(action="query", prop="imageinfo", iiprop="extmetadata",
                        titles="File:" + file_name)
        pages = (data.get("query") or {}).get("pages") or []
        meta = ((pages[0].get("imageinfo") or [{}])[0].get("extmetadata") or {}) if pages else {}
    except Exception:  # noqa: BLE001 - attribution is best-effort; never fail the lookup
        return "", ""
    strip = lambda v: re.sub(r"<[^>]+>", "", str(v or "")).strip()   # noqa: E731
    return (strip((meta.get("Artist") or {}).get("value")),
            strip((meta.get("LicenseShortName") or {}).get("value")))


def top_players(key: str, limit: int) -> list[str]:
    """The league's top-N player names by G+A — the ones a reader actually opens."""
    path = DATA_ROOT / key / "players.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("player_name"):
                continue
            try:
                ga = int(r.get("goals") or 0) + int(r.get("assists") or 0)
            except ValueError:
                ga = 0
            rows.append((ga, r["player_name"]))
    rows.sort(key=lambda t: -t[0])
    return [n for _, n in rows[:limit]]


def headshot_path(key: str) -> Path:
    return HEADSHOTS_ROOT / f"{key}.json"


def build_headshots(key: str, limit: int, refresh: bool) -> Path:
    """Resolve the league's top players to Commons image URLs, caching hits and misses."""
    out = headshot_path(key)
    cache: dict = {}
    if out.exists() and not refresh:
        try:
            cache = json.loads(out.read_text(encoding="utf-8")).get("players", {})
        except ValueError:
            cache = {}

    names = top_players(key, limit)
    hits = misses = looked = 0
    for name in names:
        if name in cache and not refresh:        # negative cache too: don't re-ask for known misses
            continue
        looked += 1
        try:
            found = _lookup(name) or _lookup(f"{name} (footballer)")
        except Exception as exc:  # noqa: BLE001 - one bad name must not kill the run
            print(f"  [headshots] {name}: lookup failed ({type(exc).__name__})")
            found = None
        if found:
            author, lic = _credit(found.pop("file", ""))
            cache[name] = {**found, "by": author, "lic": lic}
            hits += 1
        else:
            cache[name] = {"img": ""}            # recorded miss -> no repeat lookups
            misses += 1

    have = sum(1 for v in cache.values() if v.get("img"))
    payload = {"key": key, "source": "wikipedia",
               "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "players": cache}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  [headshots] {key}: {looked} looked up ({hits} found, {misses} not found); "
          f"{have}/{len(cache)} cached with a photo -> {out.name}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Cache player headshot URLs from Wikipedia.")
    ap.add_argument("league", nargs="?", default="all", help="league key, or all (default)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"top-N players per league by G+A (default {DEFAULT_LIMIT})")
    ap.add_argument("--refresh", action="store_true",
                    help="re-look-up everything, ignoring cached hits and misses")
    args = ap.parse_args()

    keys = list(LEAGUES) if args.league == "all" else [get_league(args.league).key]
    for k in keys:
        build_headshots(k, args.limit, args.refresh)


if __name__ == "__main__":
    sys.exit(main())
