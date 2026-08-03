#!/usr/bin/env python3
"""
test_soccerdata_usl.py
----------------------
A smoke test / diagnostic for pulling **USL Championship** data out of FBref via
the ``soccerdata`` package — the same path ``leagues.ingest --source fbref`` uses.
It exercises three things and reports what came back:

  1. Competition information — the season schedule/results (``read_schedule``)
     and team season stats (``read_team_season_stats``).
  2. Player information & stats — season player stats for every stat type
     FBref publishes for this competition (``read_player_season_stats``).
  3. (optional) Per-match player stats for one game (``read_player_match_stats``).

USL Championship is not one of soccerdata's built-in leagues, so the script first
registers it (reusing ``leagues.ingest._ensure_fbref_league``) exactly like the
real ingest does, then drives the reads. On Linux the scraping browser is hidden
behind an Xvfb virtual display (``leagues.ingest._hidden_display``).

Usage
-----
    # full run against a completed season (needs network access to fbref.com)
    venv/bin/python scripts/test_soccerdata_usl.py --season 2024

    # also pull per-match player stats for the first game
    venv/bin/python scripts/test_soccerdata_usl.py --season 2024 --match-stats

    # validate the script end-to-end WITHOUT hitting the network (registration,
    # league-dict wiring, API surface) — safe in a locked-down sandbox
    venv/bin/python scripts/test_soccerdata_usl.py --dry-run

    # dump a CSV sample of each table for inspection
    venv/bin/python scripts/test_soccerdata_usl.py --season 2024 --out /tmp/usl

Notes
-----
* FBref sits behind Cloudflare and rate-limits (~20 req/min); soccerdata clears it
  with a real browser, so a full run makes several requests and is not instant.
* If every network read fails with a 403 / tunnel / Cloudflare error, the host
  running this can't reach fbref.com (e.g. a restricted egress policy). Run it
  where fbref is reachable, or use ``--dry-run`` to check everything else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import pandas as pd

# Make the project package importable no matter the working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Stat types FBref publishes for a non-Big-5 competition like USL (soccerdata
# validates against exactly this set for season player/team stats).
SEASON_STAT_TYPES = ["standard", "keeper", "shooting", "playing_time", "misc"]
MATCH_STAT_TYPES = ["summary", "keepers"]

pd.set_option("display.max_columns", 40)
pd.set_option("display.width", 200)


class Runner:
    """Runs labeled checks, captures pass/fail + a preview, prints a summary."""

    def __init__(self, out_dir: Path | None):
        self.results: list[tuple[str, bool, str]] = []
        self.out_dir = out_dir
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    def check(self, label: str, fn, *, preview: bool = True, save: str | None = None):
        print(f"\n{'='*72}\n▶ {label}\n{'='*72}")
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - diagnostic tool: report everything
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  ✗ FAILED — {msg}")
            hint = self._hint(exc)
            if hint:
                print(f"    ↳ {hint}")
            if os.environ.get("FBSIM_TRACE"):
                traceback.print_exc()
            self.results.append((label, False, msg))
            return None

        note = ""
        if isinstance(result, pd.DataFrame):
            df = result
            note = f"{df.shape[0]} rows × {df.shape[1]} cols"
            print(f"  ✓ OK — {note}")
            self._describe(df, preview)
            if save and self.out_dir is not None:
                path = self.out_dir / save
                df.to_csv(path)
                print(f"    saved → {path}")
        else:
            note = repr(result)
            print(f"  ✓ OK — {note}")
        self.results.append((label, True, note))
        return result

    @staticmethod
    def _hint(exc: Exception) -> str:
        s = f"{type(exc).__name__} {exc}".lower()
        if any(k in s for k in ("403", "tunnel", "cloudflare", "connect", "proxy",
                                "max retries", "connection", "timed out", "timeout")):
            return ("Looks like a network/Cloudflare block — this host may not be able "
                    "to reach fbref.com. Run where fbref is reachable, or use --dry-run.")
        if "chromedriver" in s or "webdriver" in s or "selenium" in s or "session" in s:
            return ("Browser/driver problem — soccerdata needs a working Chrome/Chromium. "
                    "Set FBSIM_SHOW_BROWSER=1 to watch it, or check the driver install.")
        if "no tables found" in s or "empty" in s:
            return "FBref returned no table — the season may be unpublished or too new."
        return ""

    @staticmethod
    def _describe(df: pd.DataFrame, preview: bool) -> None:
        cols = [" / ".join(str(x) for x in c) if isinstance(c, tuple) else str(c)
                for c in df.columns]
        idx = list(df.index.names)
        print(f"    index: {idx}")
        print(f"    columns[{len(cols)}]: {cols[:14]}{' …' if len(cols) > 14 else ''}")
        if preview and len(df):
            with pd.option_context("display.max_rows", 6):
                print("    ── sample ──")
                for line in df.head(5).to_string().splitlines():
                    print("    " + line)

    def summary(self) -> int:
        print(f"\n{'#'*72}\n# SUMMARY\n{'#'*72}")
        passed = sum(1 for _, ok, _ in self.results if ok)
        for label, ok, note in self.results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label:<46} {note}")
        print(f"\n  {passed}/{len(self.results)} checks passed")
        return len(self.results) - passed


def register_usl(sd, league_key: str):
    """Register the league with soccerdata (reusing the project's helper) and
    return its soccerdata league id. Verifies the live + on-disk wiring."""
    from leagues.config import get_league
    from leagues.ingest import _ensure_fbref_league

    cfg = get_league(league_key)
    _ensure_fbref_league(sd, cfg)

    live = getattr(sd._config, "LEAGUE_DICT", {})
    if cfg.fbref_league not in live:
        raise RuntimeError(f"{cfg.fbref_league!r} not in live LEAGUE_DICT after registration")
    cfg_dir = Path(os.environ.get("SOCCERDATA_DIR", Path.home() / "soccerdata")) / "config"
    on_disk = json.loads((cfg_dir / "league_dict.json").read_text()) if (cfg_dir / "league_dict.json").exists() else {}
    print(f"  registered {cfg.fbref_league!r} -> {live.get(cfg.fbref_league)}")
    print(f"  on disk in {cfg_dir/'league_dict.json'}: {cfg.fbref_league in on_disk}")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default="usl", help="project league key (default: usl)")
    ap.add_argument("--season", default="2024",
                    help="season to fetch, FBref-style (default: 2024 — a completed season)")
    ap.add_argument("--stats", default=",".join(SEASON_STAT_TYPES),
                    help=f"comma list of season stat types (choices: {SEASON_STAT_TYPES})")
    ap.add_argument("--match-stats", action="store_true",
                    help="also pull per-match player stats for the first scheduled game")
    ap.add_argument("--dry-run", action="store_true",
                    help="register + wire up + list the API without any network reads")
    ap.add_argument("--out", default=None, help="directory to dump a CSV sample of each table")
    args = ap.parse_args()

    try:
        import soccerdata as sd
    except ImportError:
        print("soccerdata is not installed. Install it with:\n"
              "    venv/bin/pip install soccerdata\n"
              "(and have a Chrome/Chromium browser available).")
        return 2

    from leagues.ingest import _hidden_display

    print(f"soccerdata {sd.__version__}  ·  league={args.league}  season={args.season}"
          f"{'  [DRY RUN]' if args.dry_run else ''}")
    run = Runner(Path(args.out) if args.out else None)

    # ---- 0. Registration / wiring (no network) --------------------------------
    cfg = run.check("register league with soccerdata",
                    lambda: register_usl(sd, args.league), preview=False)
    if cfg is None:
        return run.summary() or 1
    league_id = cfg.fbref_league

    if args.dry_run:
        print(f"\n{'='*72}\n▶ API surface (dry run — no fetching)\n{'='*72}")
        print("  read_* methods:", [m for m in dir(sd.FBref) if m.startswith("read_")])
        print("  season stat types:", SEASON_STAT_TYPES)
        print("  match  stat types:", MATCH_STAT_TYPES)
        print("\n  Dry run OK — registration and API wiring look correct.")
        print("  Re-run without --dry-run on a host that can reach fbref.com to fetch data.")
        return run.summary()

    stat_types = [s.strip() for s in args.stats.split(",") if s.strip()]
    bad = [s for s in stat_types if s not in SEASON_STAT_TYPES]
    if bad:
        print(f"Ignoring invalid stat types {bad}; valid: {SEASON_STAT_TYPES}")
        stat_types = [s for s in stat_types if s in SEASON_STAT_TYPES]

    # One reader for all reads (shares soccerdata's on-disk cache).
    with _hidden_display():
        fbref = sd.FBref(leagues=league_id, seasons=args.season)

        # ---- 1. Competition information -----------------------------------
        run.check("read_leagues()", fbref.read_leagues, preview=False, save="leagues.csv")
        run.check("read_seasons()", fbref.read_seasons, preview=False, save="seasons.csv")
        schedule = run.check("read_schedule()  [USL matches/results]",
                             fbref.read_schedule, save="schedule.csv")
        if isinstance(schedule, pd.DataFrame) and len(schedule):
            played = schedule[schedule.get("score").notna()] if "score" in schedule else schedule
            print(f"    → {len(played)} played of {len(schedule)} scheduled; "
                  f"xg columns present: {[c for c in schedule.columns if 'xg' in str(c).lower()]}")

        for st in stat_types:
            run.check(f"read_team_season_stats('{st}')  [team info/standings]",
                      lambda st=st: fbref.read_team_season_stats(stat_type=st),
                      save=f"team_{st}.csv")

        # ---- 2. Player information & stats --------------------------------
        for st in stat_types:
            run.check(f"read_player_season_stats('{st}')  [player info/stats]",
                      lambda st=st: fbref.read_player_season_stats(stat_type=st),
                      save=f"player_{st}.csv")

        # ---- 3. Per-match player stats (optional) -------------------------
        if args.match_stats:
            def first_game_match_stats():
                sched = fbref.read_schedule()
                gid = None
                if "game_id" in sched.columns:
                    ids = sched["game_id"].dropna()
                    gid = ids.iloc[0] if len(ids) else None
                return fbref.read_player_match_stats(stat_type="summary", match_id=gid)
            run.check("read_player_match_stats('summary', first game)",
                      first_game_match_stats, save="player_match_summary.csv")

    return run.summary()


if __name__ == "__main__":
    sys.exit(main())
