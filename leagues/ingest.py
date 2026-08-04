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
* ``fixturedownload`` — the default. A free fixturedownload.com JSON feed
                    (schedules + final scores, **no xG**) fetched with a plain
                    request: no browser, no auth, no extra packages, and current
                    seasons. The reliable everyday source; the model falls back to
                    goals.
* ``fbref``       — xG-bearing, via the ``soccerdata`` package. It drives a real
                    (undetected) Chrome to clear fbref's Cloudflare, which blocks
                    every plain-HTTP client. On Linux the window is hidden with a
                    virtual display (``pyvirtualdisplay`` + Xvfb; see
                    ``_hidden_display``); ``FBSIM_SHOW_BROWSER=1`` shows it. Use it
                    when you want xG. Leagues soccerdata doesn't ship natively
                    (e.g. MLS) are auto-registered.
* ``fbref-http``  — the same page over plain HTTP via ``curl_cffi`` (TLS
                    impersonation) + ``pandas.read_html``. No browser, but fbref's
                    Cloudflare **currently blocks it**; kept in case that eases.
* ``openfootball``— the openfootball GitHub JSON mirror (schedules + scores,
                    **no xG**); offline-friendly but lags live seasons.
* manual          — drop hand-made CSVs in the canonical schema; nothing to do.

Usage
-----
    venv/bin/python -m leagues.ingest eng --season 2025      # fixturedownload (default)
    venv/bin/python -m leagues.ingest mls --season 2026
    venv/bin/python -m leagues.ingest eng --season 2025-2026 --source fbref   # +xG (browser)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .config import LeagueConfig, get_league

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "leagues"
OPENFOOTBALL_BASE = (
    "https://raw.githubusercontent.com/openfootball/football.json/master"
)
FBREF_BASE = "https://fbref.com"
FBREF_DELAY = 4.0  # seconds to pause between fbref requests (~20/min limit)
FIXTUREDOWNLOAD_BASE = "https://fixturedownload.com/feed/json"

TEAM_FIELDS = ["id", "team_name", "code"]
MATCH_FIELDS = [
    "match_number", "matchday", "date", "datetime_utc", "home_team_id",
    "away_team_id", "home_goals", "away_goals", "xg_home", "xg_away", "played",
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

def _fetch_json(url: str):
    """GET a URL and parse JSON (a dict or a list, depending on the endpoint)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
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
# fixturedownload.com source (plain JSON feed; schedules + scores, no xG)
# ---------------------------------------------------------------------------

def _parse_fixturedownload(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Parse a fixturedownload.com JSON feed into canonical (teams, matches).

    Each item has ``HomeTeam``, ``AwayTeam``, ``RoundNumber``, ``DateUtc``
    ("2025-08-15 19:00:00Z") and ``HomeTeamScore``/``AwayTeamScore`` (null until
    played). No xG in this feed.
    """
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
    for i, f in enumerate(items, start=1):
        home, away = f.get("HomeTeam"), f.get("AwayTeam")
        if not home or not away:
            continue
        hid, aid = team_id(str(home).strip()), team_id(str(away).strip())
        hs, as_ = f.get("HomeTeamScore"), f.get("AwayTeamScore")
        played = hs is not None and as_ is not None
        du = str(f.get("DateUtc", "")).strip()   # "2025-08-15 19:00:00Z"
        matches.append({
            "match_number": i,
            "matchday": f.get("RoundNumber", ""),
            "date": du[:10],
            "datetime_utc": du.replace(" ", "T") if du else "",  # ISO for JS Date()
            "home_team_id": hid,
            "away_team_id": aid,
            "home_goals": int(hs) if played else "",
            "away_goals": int(as_) if played else "",
            "xg_home": "",   # fixturedownload carries no xG
            "xg_away": "",
            "played": played,
        })
    return teams, matches


def from_fixturedownload(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """Return (teams, matches) from a fixturedownload.com JSON feed.

    Plain JSON over HTTP — no browser, no auth. Season is the start year, e.g.
    "2025" (2025-26) for the European leagues or "2026" for MLS. No xG.
    """
    url = f"{FIXTUREDOWNLOAD_BASE}/{cfg.fixturedownload_slug}-{season}"
    items = _fetch_json(url)
    if not isinstance(items, list):
        raise SystemExit(f"Unexpected fixturedownload response at {url} (not a list).")
    return _parse_fixturedownload(items)


# ---------------------------------------------------------------------------
# Understat source (xG-bearing, no browser) via the soccerdata package.
# Big-5 European leagues only; its own xG model, inline in the schedule.
# ---------------------------------------------------------------------------

def from_understat(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """Return (teams, matches) from Understat via soccerdata (no browser).

    Understat carries its own xG for every game right in the schedule, so this is
    the low-friction xG source for the Big-5 leagues (it covers only those). Season
    is the start year, e.g. "2025" (2025-26). Needs ``soccerdata`` (which fetches a
    small TLS helper on first use) — no browser, unlike ``fbref``.
    """
    try:
        import soccerdata as sd
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "The 'understat' source needs the soccerdata package:\n"
            "    venv/bin/pip install soccerdata\n"
            "(no browser required). Or use --source fixturedownload."
        ) from exc

    us = sd.Understat(leagues=cfg.fbref_league, seasons=season)
    schedule = us.read_schedule().reset_index()

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

    def _num(v):
        return None if v is None or v != v else v   # drop NaN

    matches: list[dict] = []
    for i, row in enumerate(schedule.itertuples(index=False), start=1):
        d = row._asdict()
        home, away = d.get("home_team"), d.get("away_team")
        if not home or not away:
            continue
        hid, aid = team_id(str(home)), team_id(str(away))
        played = bool(d.get("is_result"))
        hg, ag = _num(d.get("home_goals")), _num(d.get("away_goals"))
        hx, ax = _num(d.get("home_xg")), _num(d.get("away_xg"))
        matches.append({
            "match_number": i,
            "matchday": "",                        # Understat carries no round number
            "date": str(d.get("date", ""))[:10],
            "datetime_utc": "",                    # Understat time zone unclear — date only
            "home_team_id": hid,
            "away_team_id": aid,
            "home_goals": int(hg) if played and hg is not None else "",
            "away_goals": int(ag) if played and ag is not None else "",
            "xg_home": round(float(hx), 3) if played and hx is not None else "",
            "xg_away": round(float(ax), 3) if played and ax is not None else "",
            "played": played,
        })
    return teams, matches


# ---------------------------------------------------------------------------
# FBref sources (xG-bearing): "fbref" (soccerdata/browser) and "fbref-http"
# (plain HTTP + pandas.read_html). Both parse the same "N–M" score strings.
# ---------------------------------------------------------------------------

def _parse_score(val) -> tuple[int, int] | None:
    """Parse a fbref score string like "2–1" (en-dash) or "2-1" into (h, a)."""
    if not isinstance(val, str) or ("–" not in val and "-" not in val):
        return None
    sep = "–" if "–" in val else "-"
    try:
        h, a = (int(x.strip()) for x in val.split(sep)[:2])
        return h, a
    except ValueError:
        return None


def _ensure_fbref_league(sd, cfg: LeagueConfig) -> None:
    """Register a league soccerdata doesn't ship natively (e.g. MLS).

    soccerdata's FBref only ships the Big-5 leagues; others are added via
    ``<SOCCERDATA_DIR or ~/soccerdata>/config/league_dict.json``, which it reads
    **at import time**. So we do two things: persist the entry to that file (so
    fresh processes pick it up) and patch the already-imported ``LEAGUE_DICT`` in
    memory (so the current process sees it without a restart). Only our own key
    is touched; the rest of the file is preserved.
    """
    if not cfg.fbref_name:
        return
    entry = {"FBref": cfg.fbref_name}
    if cfg.season_start:
        entry["season_start"] = cfg.season_start
    if cfg.season_end:
        entry["season_end"] = cfg.season_end

    cfg_dir = Path(os.environ.get("SOCCERDATA_DIR", Path.home() / "soccerdata")) / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "league_dict.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if data.get(cfg.fbref_league) != entry:
        data[cfg.fbref_league] = entry
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # soccerdata reads the file only at import; patch the live dicts too.
    for mod in ("_config", "_common"):
        live = getattr(getattr(sd, mod, None), "LEAGUE_DICT", None)
        if isinstance(live, dict):
            live[cfg.fbref_league] = entry


@contextmanager
def _hidden_display():
    """Run soccerdata's browser inside a virtual X display so no window appears.

    soccerdata drives a real (non-headless) Chrome to clear fbref's Cloudflare —
    headless mode gets re-detected, so we can't just hide it that way. Instead we
    start an Xvfb virtual display (via ``pyvirtualdisplay``) that the browser
    renders into: invisible, but still a genuine browser to Cloudflare.

    Safe and optional — yields without a display (visible browser, today's
    behavior) when ``FBSIM_SHOW_BROWSER`` is set, when not on Linux, when
    ``pyvirtualdisplay``/Xvfb aren't available, or if the display fails to start.
    """
    if os.environ.get("FBSIM_SHOW_BROWSER") or sys.platform != "linux":
        yield
        return
    try:
        from pyvirtualdisplay import Display
    except ImportError:
        yield  # not installed -> visible browser (works via xvfb-run, or just shows)
        return
    disp = None
    try:
        disp = Display(visible=False, size=(1920, 1080))
        disp.start()
    except Exception:
        yield  # Xvfb binary missing / failed -> visible browser
        return
    try:
        yield
    finally:
        disp.stop()


def from_fbref(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """
    Return (teams, matches) rows from FBref via the soccerdata package.

    Requires ``pip install soccerdata`` and a Chrome/Chromium browser (soccerdata
    drives an undetected browser to clear fbref's Cloudflare). On Linux the window
    is hidden via a virtual display when ``pyvirtualdisplay`` + Xvfb are installed
    (see ``_hidden_display``). Season is FBref-style, e.g. "2025-2026" or "2526".
    Leagues soccerdata doesn't know natively (e.g. MLS) are auto-registered.
    """
    try:
        import soccerdata as sd
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "The 'fbref' source needs the soccerdata package:\n"
            "    venv/bin/pip install soccerdata\n"
            "and a Chrome/Chromium browser. In a network-restricted "
            "environment use --source openfootball instead."
        ) from exc

    _ensure_fbref_league(sd, cfg)
    with _hidden_display():
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

    matches: list[dict] = []
    for i, row in enumerate(schedule.itertuples(index=False), start=1):
        d = row._asdict()
        home, away = d.get("home_team"), d.get("away_team")
        if not home or not away:
            continue
        hid, aid = team_id(str(home)), team_id(str(away))
        sc = _parse_score(d.get("score"))
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
# FBref via plain HTTP + pandas.read_html (no browser, keeps xG)
# ---------------------------------------------------------------------------

# Text that only appears on a Cloudflare challenge/block page, not a real one.
_CF_MARKERS = ("just a moment", "attention required", "checking your browser",
               "cf-browser-verification", "cf-challenge")


def _xg_val(row, col) -> str:
    """Read and round an xG cell, returning "" when absent/blank."""
    import pandas as pd
    if col is None:
        return ""
    v = pd.to_numeric(row.get(col), errors="coerce")
    return "" if pd.isna(v) else round(float(v), 3)


def _parse_fbref_schedule(df, cfg: LeagueConfig) -> tuple[list[dict], list[dict]]:
    """Parse a fbref 'Scores & Fixtures' table into canonical (teams, matches).

    The table's columns are ``Wk, Day, Date, Time, Home, xG, Score, xG, Away,
    …``; ``read_html`` de-duplicates the two ``xG`` headers to ``xG`` (home,
    before Score) and ``xG.1`` (away, after Score). xG columns are absent for
    leagues/seasons FBref doesn't cover with expected goals.
    """
    cols = list(df.columns)
    home_xg = "xG" if "xG" in cols else None
    away_xg = "xG.1" if "xG.1" in cols else None

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
    n = 0
    for row in df.to_dict("records"):
        home, away = row.get("Home"), row.get("Away")
        # fbref repeats the header row and pads blank separator rows; skip them.
        if not isinstance(home, str) or not isinstance(away, str) or not home or not away:
            continue
        if home == "Home" or away == "Away":
            continue
        n += 1
        hid, aid = team_id(home.strip()), team_id(away.strip())
        sc = _parse_score(row.get("Score"))
        played = sc is not None
        matches.append({
            "match_number": n,
            "matchday": _matchday_num(str(row.get("Wk", ""))),
            "date": str(row.get("Date", ""))[:10],
            "home_team_id": hid,
            "away_team_id": aid,
            "home_goals": sc[0] if played else "",
            "away_goals": sc[1] if played else "",
            "xg_home": _xg_val(row, home_xg) if played else "",
            "xg_away": _xg_val(row, away_xg) if played else "",
            "played": played,
        })
    return teams, matches


def from_fbref_http(cfg: LeagueConfig, season: str) -> tuple[list[dict], list[dict]]:
    """
    Return (teams, matches) by fetching fbref's Scores & Fixtures page over plain
    HTTP (no browser) and parsing it with ``pandas.read_html``.

    fbref's Cloudflare fingerprints the TLS handshake, so a normal ``requests``/
    ``urllib`` call is dropped as a bot regardless of User-Agent. We use
    ``curl_cffi`` with ``impersonate="chrome"``, which mimics a real browser's
    TLS fingerprint and gets through. Still rate-limited (~20 req/min), so a
    delay follows each request. Needs ``curl_cffi`` and ``lxml``.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The 'fbref-http' source needs curl_cffi (and lxml):\n"
            "    venv/bin/pip install curl_cffi lxml\n"
            "curl_cffi impersonates a browser's TLS fingerprint to clear fbref's "
            "Cloudflare without a real browser. Or use --source fbref / openfootball."
        ) from exc
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("The 'fbref-http' source needs pandas + lxml.") from exc

    url = (f"{FBREF_BASE}/en/comps/{cfg.fbref_comp_id}/{season}/schedule/"
           f"{season}-{cfg.fbref_slug}-Scores-and-Fixtures")
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=45)
    except Exception as exc:  # network error
        raise SystemExit(
            f"fbref request failed: {exc}. Retry later, or use --source fbref "
            "(browser) or --source openfootball."
        ) from exc
    time.sleep(FBREF_DELAY)  # be polite; fbref rate-limits (~20 requests/minute)

    if resp.status_code == 429:
        raise SystemExit(
            "fbref rate-limited the request (HTTP 429). Wait ~a minute and retry; "
            "keep to ~20 requests/minute."
        )
    if resp.status_code in (403, 503):
        raise SystemExit(
            f"fbref blocked the request (HTTP {resp.status_code}) — Cloudflare. "
            "Retry later, or use --source fbref (browser) or --source openfootball."
        )
    if resp.status_code != 200:
        raise SystemExit(f"fbref returned HTTP {resp.status_code} for {url}.")

    html = resp.text
    if any(m in html.lower()[:4000] for m in _CF_MARKERS):
        raise SystemExit(
            "fbref returned a Cloudflare challenge page instead of data. "
            "Retry later, or use --source fbref (browser) or --source openfootball."
        )

    tables = pd.read_html(io.StringIO(html))
    sched = next((t for t in tables
                  if "Score" in t.columns and "Home" in t.columns and "Away" in t.columns), None)
    if sched is None:
        raise SystemExit(
            f"No Scores & Fixtures table found at {url}. The page layout may have "
            "changed, or the season isn't published yet."
        )
    return _parse_fbref_schedule(sched, cfg)


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


SOURCES = {
    "fixturedownload": from_fixturedownload,  # plain JSON, no browser, no xG (broad coverage)
    "understat": from_understat,     # soccerdata, no browser, has xG (Big-5 only)
    "fbref": from_fbref,             # soccerdata browser (hidden via xvfb); has xG (broad)
    "fbref-http": from_fbref_http,   # plain HTTP + read_html; currently Cloudflare-blocked
    "openfootball": from_openfootball,  # offline mirror, no xG, no dependencies
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest league fixtures/results into canonical CSVs.")
    ap.add_argument("league", help="league key (eng, esp, ita, de, fr, mls, nwsl, usl)")
    ap.add_argument("--season", required=True,
                    help="season: start year (fixturedownload, e.g. 2025 / 2026), "
                         "2025-2026 (fbref), or 2024-25 (openfootball)")
    ap.add_argument("--source", default=None, choices=list(SOURCES),
                    help="data source (default: the league's own — understat for the "
                         "Big-5, fixturedownload for MLS/NWSL, fbref for USL; "
                         "'openfootball' is the offline fallback)")
    args = ap.parse_args()

    cfg = get_league(args.league)
    source = args.source or cfg.default_source
    teams, matches = SOURCES[source](cfg, args.season)

    n_played = sum(1 for m in matches if m["played"])
    n_xg = sum(1 for m in matches if m["xg_home"] != "")
    out_dir = write_league(cfg, teams, matches)

    print(f"{cfg.name} {args.season} [{source}]: "
          f"{len(teams)} teams, {len(matches)} fixtures "
          f"({n_played} played, {n_xg} with xG) -> {out_dir}")
    if n_played and not n_xg:
        print("  note: no xG in this source; the model will fall back to goals.")


if __name__ == "__main__":
    sys.exit(main())
