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

    # Refresh every league at its current season via fbref-http (Euro 2025-2026
    # + MLS 2026, with xG, no browser):
    venv/bin/python -m leagues.update --refresh

    # Same via the browser-based fbref source (reliably clears Cloudflare), or
    # from the offline openfootball mirror (no xG, lags live seasons):
    venv/bin/python -m leagues.update --refresh --source fbref
    venv/bin/python -m leagues.update --refresh --source openfootball

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
from .ingest import DATA_ROOT, MATCH_FIELDS, SOURCES, TEAM_FIELDS
from .model import fit_model
from .run_sims import run
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
    """Fetch and write canonical CSVs; return True if anything changed on disk."""
    teams, matches = SOURCES[source](cfg, season)
    data_dir = DATA_ROOT / cfg.key
    teams_changed = _write_if_changed(data_dir / "teams.csv", TEAM_FIELDS, teams)
    matches_changed = _write_if_changed(data_dir / "matches.csv", MATCH_FIELDS, matches)
    n_played = sum(1 for m in matches if m["played"])
    n_xg = sum(1 for m in matches if m["xg_home"] != "")
    tag = "updated" if (teams_changed or matches_changed) else "no change"
    print(f"  [ingest] {season} [{source}]: {len(teams)} teams, {len(matches)} "
          f"fixtures ({n_played} played, {n_xg} xG) — {tag}")
    return teams_changed or matches_changed


def needs_sim(matches_csv: Path, sim_json: Path, force: bool) -> bool:
    """Re-simulate when forced, when no prior result exists, or when data is newer."""
    if force:
        return True
    if not sim_json.exists():
        return True
    return matches_csv.stat().st_mtime > sim_json.stat().st_mtime


def simulate_league(cfg: LeagueConfig, n_sims: int, seed: int,
                    reg: float, recency_halflife: float | None) -> None:
    data_dir = DATA_ROOT / cfg.key
    teams = pd.read_csv(data_dir / "teams.csv")
    matches = pd.read_csv(data_dir / "matches.csv")
    model = fit_model(teams, matches, reg=reg, recency_halflife=recency_halflife)
    payload = run(cfg, teams, matches, model, n_sims, seed)
    payload["meta"]["as_of"] = None
    out = data_dir / "sim_results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    m = payload["meta"]
    print(f"  [sim] {n_sims} sims, {m['n_played']} played / {m['n_remaining']} "
          f"remaining [{'xG' if m['used_xg'] else 'goals'}] -> {out.name}")


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
                    help="league keys (default: all — eng, esp, ita, de, fr, mls)")
    ap.add_argument("--season", default=None,
                    help="ingest this season for every selected league before "
                         "simulating (e.g. 2024-25); omit to simulate from the "
                         "CSVs already on disk")
    ap.add_argument("--refresh", action="store_true",
                    help="ingest each league at its own current season "
                         "(handles mixed formats, e.g. Euro 2025-2026 + MLS 2026); "
                         "ignored when --season is given")
    ap.add_argument("--source", default="fbref-http", choices=list(SOURCES),
                    help="ingest source for --season / --refresh (default: fbref-http — "
                         "current data + xG, no browser; 'fbref' uses a browser to "
                         "clear Cloudflare; 'openfootball' is the offline fallback)")
    ap.add_argument("--sims", type=int, default=20000, help="simulations per league")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reg", type=float, default=0.05, help="model L2 shrinkage")
    ap.add_argument("--recency-halflife", type=float, default=None,
                    help="down-weight older matches (in played-match count)")
    ap.add_argument("--force", action="store_true",
                    help="re-simulate even when the data hasn't changed")
    ap.add_argument("--no-page", action="store_true", help="skip rebuilding leagues.html")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    keys = args.leagues or list(LEAGUES)
    cfgs = [get_league(k) for k in keys]

    simulated, skipped = [], []
    for cfg in cfgs:
        print(f"{cfg.name} ({cfg.key}):")
        data_dir = DATA_ROOT / cfg.key
        matches_csv = data_dir / "matches.csv"

        if args.season:
            season = args.season
        elif args.refresh:
            season = cfg.season_for(args.source)
        else:
            season = None
        if season:
            try:
                ingest_league(cfg, season, args.source)
            except Exception as exc:  # network/source failure — keep existing data
                print(f"  [ingest] skipped: {exc}")

        if not matches_csv.exists():
            print("  [skip] no matches.csv — ingest data for this league first.")
            skipped.append(cfg.key)
            continue

        if needs_sim(matches_csv, data_dir / "sim_results.json", args.force):
            simulate_league(cfg, args.sims, args.seed, args.reg, args.recency_halflife)
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
