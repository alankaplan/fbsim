#!/usr/bin/env python3
"""
update.py
---------
One-command pipeline that ties the three stages together: (optionally) refresh
each league's data, re-run the Monte Carlo simulation for any league whose data
has changed, and regenerate the ``leagues.html`` report.

The "check for new data" step is content-aware. Ingested CSVs are only
rewritten when they actually differ, and a league is re-simulated only when its
``matches.csv`` is newer than its ``sim_results.json`` (or the sim output is
missing, or ``--force`` is given). So re-running the command when nothing has
changed does no simulation work and simply rebuilds the page.

Usage
-----
    # Re-simulate stale leagues from existing CSVs and rebuild the page (no network):
    venv/bin/python -m leagues.update

    # Refresh every league at its current season (auto-detected from today's
    # date) via fixturedownload (plain JSON, no browser, goals only). A season
    # that hasn't started yet auto-builds a preseason prior from last season:
    venv/bin/python -m leagues.update --refresh

    # Add xG via fbref (browser, hidden on Linux; may hit a Cloudflare captcha):
    venv/bin/python -m leagues.update --refresh --source fbref

    # Fetch a specific season for the leagues it applies to, then simulate:
    venv/bin/python -m leagues.update eng esp --season 2025-2026
    venv/bin/python -m leagues.update mls --season 2026

    # Specific leagues, more sims, force a re-run, open the page:
    venv/bin/python -m leagues.update eng esp --sims 50000 --force --open
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import webbrowser
from pathlib import Path

import pandas as pd

from .config import LEAGUES, LeagueConfig, get_league
from .ingest import DATA_ROOT, MATCH_FIELDS, SOURCES, TEAM_FIELDS, ingest_dataset, ensure_league_dict
from .model import fit_model
from .prior import build_prior, load_prior, _prev_season, PRIOR_REGRESSION
from .run_sims import run, SCHEMA_VERSION
from .generate_page import OUT, build


def _render_csv(fields: list[str], rows: list[dict]) -> str:
    """Render rows to CSV text identical to ingest._write_csv's output."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _write_if_changed(path: Path, fields: list[str], rows: list[dict]) -> bool:
    """Write CSV only when content differs from disk. Return True if written.

    Compares raw bytes so CSV's ``\\r\\n`` line endings aren't masked by the
    newline translation ``read_text`` would apply.
    """
    data = _render_csv(fields, rows).encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def ingest_league(cfg: LeagueConfig, season: str, source: str) -> bool:
    """Fetch and write canonical CSVs; return True if anything changed on disk.

    Never overwrites existing data with an empty result (e.g. a source that lacks
    the upcoming season) — it keeps what's on disk and reports it."""
    teams, matches, n_added = ingest_dataset(cfg, season, source, cfg.xg_source)
    if not teams or not matches:
        print("  [ingest] source returned no data — keeping existing CSVs")
        return False
    data_dir = DATA_ROOT / cfg.key
    teams_changed = _write_if_changed(data_dir / "teams.csv", TEAM_FIELDS, teams)
    matches_changed = _write_if_changed(data_dir / "matches.csv", MATCH_FIELDS, matches)
    n_played = sum(1 for m in matches if m["played"])
    n_xg = sum(1 for m in matches if m["xg_home"] != "")
    tag = "updated" if (teams_changed or matches_changed) else "no change"
    xg_note = f" [+{n_added} xG from {cfg.xg_source}]" if n_added else ""
    print(f"  [ingest] {season} [{source}]{xg_note}: {len(teams)} teams, {len(matches)} "
          f"fixtures ({n_played} played, {n_xg} xG) — {tag}")
    return teams_changed or matches_changed


def _result_schema(sim_json: Path) -> int:
    """The schema_version stamped in an existing result, or 0 if absent/unreadable."""
    try:
        return int(json.loads(sim_json.read_text(encoding="utf-8"))
                   .get("meta", {}).get("schema_version", 0))
    except (OSError, ValueError, TypeError):
        return 0


def needs_sim(matches_csv: Path, sim_json: Path, force: bool) -> bool:
    """Re-simulate when forced, when no result exists, when the data is newer, or
    when the existing result predates the current output schema (so pulling new
    code that adds fields self-heals without needing --force)."""
    if force:
        return True
    if not sim_json.exists():
        return True
    if _result_schema(sim_json) < SCHEMA_VERSION:
        print("  [sim] result schema outdated — re-simulating")
        return True
    return matches_csv.stat().st_mtime > sim_json.stat().st_mtime


def _played_count(matches: pd.DataFrame) -> int:
    return int((matches["played"].astype(str).isin(["True", "true", "1"])
               | (matches["played"] == True)).sum())  # noqa: E712


def simulate_league(cfg: LeagueConfig, n_sims: int, seed: int,
                    reg: float, recency_halflife: float | None,
                    source: str, no_prior: bool,
                    prior_regression: float = PRIOR_REGRESSION,
                    resolution_sims: int = 250) -> None:
    data_dir = DATA_ROOT / cfg.key
    teams = pd.read_csv(data_dir / "teams.csv")
    matches = pd.read_csv(data_dir / "matches.csv")
    prior = None if no_prior else load_prior(cfg, prior_regression)

    # A not-yet-started season (0 games) has nothing to fit — auto-build a prior
    # from last season so it yields a real preseason projection, not a flat table.
    if prior is None and not no_prior and _played_count(matches) == 0:
        prev = _prev_season(source, cfg.season_for(source))
        try:
            build_prior(cfg, source, prev)
            prior = load_prior(cfg, prior_regression)
            print(f"  [prior] built from {prev} [{source}]")
        except Exception as exc:  # network/source failure — fall back to flat model
            print(f"  [prior] skipped: {exc}")

    model = fit_model(teams, matches, reg=reg, recency_halflife=recency_halflife, prior=prior)
    payload = run(cfg, teams, matches, model, n_sims, seed, resolution_sims=resolution_sims)
    payload["meta"]["as_of"] = None
    payload["meta"]["used_prior"] = prior is not None
    out = data_dir / "sim_results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    m = payload["meta"]
    print(f"  [sim] {n_sims} sims, {m['n_played']} played / {m['n_remaining']} "
          f"remaining [{'xG' if m['used_xg'] else 'goals'}]"
          f"{' +prior' if prior is not None else ''} -> {out.name}")


def build_page(open_browser: bool) -> bool:
    """Rebuild leagues.html from whatever sim_results.json files exist."""
    data = {}
    for key in LEAGUES:
        f = DATA_ROOT / key / "sim_results.json"
        if f.exists():
            data[key] = json.loads(f.read_text(encoding="utf-8"))
    if not data:
        print("No sim_results.json found — skipping page.")
        return False
    OUT.write_text(build(data), encoding="utf-8")
    print(f"[page] wrote {OUT}  ({len(data)} league(s): {', '.join(data)})")
    if open_browser:
        webbrowser.open(OUT.as_uri())
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description="One command: refresh data, simulate what changed, rebuild the report.")
    ap.add_argument("leagues", nargs="*", default=None,
                    help="league keys (default: all — eng, esp, ita, de, fr, mls, nwsl)")
    ap.add_argument("--season", default=None,
                    help="ingest this season for every selected league before "
                         "simulating (e.g. 2024-25); omit to simulate from the "
                         "CSVs already on disk")
    ap.add_argument("--refresh", action="store_true",
                    help="ingest each league at its current season (auto-detected "
                         "from today's date); ignored when --season is given")
    ap.add_argument("--source", default=None, choices=list(SOURCES),
                    help="override the schedule source for --season / --refresh; by "
                         "default each league uses its own (fixturedownload for the "
                         "Big-5/MLS/NWSL). Big-5 xG is overlaid from Understat.")
    ap.add_argument("--sims", type=int, default=20000, help="simulations per league")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reg", type=float, default=0.05, help="model L2 shrinkage")
    ap.add_argument("--recency-halflife", type=float, default=None,
                    help="down-weight older matches (in played-match count)")
    ap.add_argument("--no-prior", action="store_true",
                    help="ignore/skip the preseason prior (prior.json)")
    ap.add_argument("--prior-regression", type=float, default=PRIOR_REGRESSION,
                    help="regress last season's ratings toward the mean "
                         "(1.0 = off, 0.0 = flat league)")
    ap.add_argument("--resolution-sims", type=int, default=250,
                    help="tail forecasts per history for the 'H after' resolution "
                         "curve (0 disables it)")
    ap.add_argument("--players", action="store_true",
                    help="also refresh individual player stats (players.csv) for each "
                         "league via its player source (understat/fbref; browser for US)")
    ap.add_argument("--fbref", action="store_true",
                    help="attempt FBref-sourced data (US-league player stats); off by "
                         "default because it needs a browser/display and hits fbref "
                         "CAPTCHA headless — run under a display / xvfb-run")
    ap.add_argument("--force", action="store_true",
                    help="re-simulate even when the data hasn't changed")
    ap.add_argument("--no-page", action="store_true", help="skip rebuilding leagues.html")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    ensure_league_dict()                            # register custom leagues before soccerdata import
    keys = args.leagues or list(LEAGUES)
    cfgs = [get_league(k) for k in keys]

    simulated, skipped = [], []
    for cfg in cfgs:
        print(f"{cfg.name} ({cfg.key}):")
        data_dir = DATA_ROOT / cfg.key
        matches_csv = data_dir / "matches.csv"
        source = args.source or cfg.default_source   # per-league default unless overridden

        if args.season:
            season = args.season
        elif args.refresh:
            season = cfg.season_for(source)
        else:
            season = None
        if season:
            if source == "fbref" and not args.fbref:
                print(f"  [ingest] {cfg.key} needs FBref (browser/display) — "
                      "skipped; pass --fbref")
            else:
                try:
                    ingest_league(cfg, season, source)
                # SystemExit (a BaseException) is what the source functions raise for
                # a missing dependency / Cloudflare block, so catch it too and keep
                # the existing data instead of aborting the whole refresh.
                except (Exception, SystemExit) as exc:
                    print(f"  [ingest] skipped: {exc}")

        if args.players:
            psource = cfg.player_source
            if psource == "fbref" and not args.fbref:
                print(f"  [players] {cfg.key} needs FBref (browser/display) — "
                      "skipped; pass --fbref")
            else:
                pseason = args.season or cfg.season_for(psource)
                try:
                    from .players import build_players
                    out = build_players(cfg, pseason, psource)
                    n = sum(1 for _ in out.read_text(encoding="utf-8").splitlines()) - 1
                    print(f"  [players] {pseason} [{psource}]: {max(n, 0)} players -> {out.name}")
                except (Exception, SystemExit) as exc:
                    print(f"  [players] skipped: {exc}")

        if not matches_csv.exists():
            print("  [skip] no matches.csv — ingest data for this league first.")
            skipped.append(cfg.key)
            continue

        teams_csv = data_dir / "teams.csv"
        if (not teams_csv.exists() or pd.read_csv(teams_csv).empty
                or pd.read_csv(matches_csv).empty):
            print("  [skip] no teams/fixtures on disk — nothing to simulate.")
            skipped.append(cfg.key)
            continue

        if needs_sim(matches_csv, data_dir / "sim_results.json", args.force):
            simulate_league(cfg, args.sims, args.seed, args.reg, args.recency_halflife,
                            source, args.no_prior, args.prior_regression,
                            args.resolution_sims)
            simulated.append(cfg.key)
        else:
            print("  [sim] up to date — skipping (use --force to re-run).")
            skipped.append(cfg.key)

    print(f"\nSimulated: {', '.join(simulated) or '(none)'} | "
          f"skipped: {', '.join(skipped) or '(none)'}")

    if not args.no_page:
        build_page(args.open)


if __name__ == "__main__":
    sys.exit(main())
