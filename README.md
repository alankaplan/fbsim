# fbsim

A **domestic-league season simulator** for the big-five European leagues and
MLS. Team strengths are fit as attack/defense Poisson ratings from FBref
expected-goals (xG) data (falling back to actual goals), and full seasons are
run by Monte Carlo to produce title, qualification and relegation probabilities.

A match scoreline is two independent Poisson draws, and win/draw/loss
probabilities come from the outer product of the two Poisson PMFs.

## Requirements

- Python 3.10+
- `numpy`, `pandas`, `scipy`, `lxml`
- `lxml` powers `pandas.read_html` for the default **fbref-http** source, which
  pulls current data + xG from `fbref.com` over plain HTTP (no browser). fbref
  is behind Cloudflare and rate-limits, so a plain request is occasionally
  blocked — see the sources below for the fallbacks.
- `soccerdata` *(optional)* — only for `--source fbref`, which fetches the same
  fbref data through an undetected **Chrome/Chromium** browser that reliably
  clears Cloudflare (a visible browser window opens while it scrapes).

```bash
python -m venv venv
venv/bin/pip install numpy pandas scipy lxml
venv/bin/pip install soccerdata   # optional, for the browser-based --source fbref
```

## Quick start

One command refreshes data, re-simulates the leagues whose data changed, and
rebuilds the report:

```bash
# Refresh every league at its current season via fbref-http (current data + xG,
# no browser), simulate what changed, and open the page:
venv/bin/python -m leagues.update --refresh --open

# Re-simulate stale leagues from the CSVs already on disk (no network):
venv/bin/python -m leagues.update
```

It's incremental: ingested CSVs are only rewritten when they actually differ,
and a league is re-simulated only when its `matches.csv` is newer than its
`sim_results.json` (or the result is missing, or you pass `--force`). Re-running
when nothing has changed does no simulation work and just rebuilds `leagues.html`.

`--refresh` ingests each league at its own current season, so it handles the
different formats (European leagues on `2025-2026`, MLS on the calendar-year
`2026`). Use `--season <s>` to pin one season across the leagues it applies to.
If fbref's Cloudflare blocks the default `fbref-http` source, fall back to
`--source fbref` (browser) or `--source openfootball` (offline, no xG).

Useful flags: `--refresh` / `--season <s>` (ingest first; omit both to skip the
network), `--source`, `--sims`, `--seed`, `--force`, `--no-page`, `--open`, and
an optional list of league keys (default: all). The three stages below can also
be run individually.

## Overview

A layered pipeline under `leagues/`:

| Module | Role |
|---|---|
| `config.py` | Per-league definitions: team count, European/relegation slots, tiebreaker chain |
| `ingest.py` | Pluggable data ingestion → canonical CSVs in `data/leagues/<key>/` |
| `model.py` | Attack/defense Poisson strengths fit from xG (or goals) |
| `match.py` | Poisson match primitive: (λ_home, λ_away) → win/draw/loss probabilities |
| `simulator.py` | Single-pool round-robin season engine + configurable standings |
| `run_sims.py` | Monte Carlo driver → `sim_results.json` |
| `generate_page.py` | Self-contained `leagues.html` report |
| `update.py` | One command: ingest → simulate what changed → rebuild the report |

Supported leagues (keys): `eng` (Premier League), `esp` (La Liga),
`ita` (Serie A), `de` (Bundesliga), `fr` (Ligue 1), `mls` (MLS). Slot counts and
tiebreaker order are per-league — Spain and Italy apply head-to-head before
overall goal difference; England, Germany and France use overall goal difference
first; MLS ranks on wins before goal difference.

**MLS notes.** MLS is modeled as a single 30-team table producing the
Supporters' Shield race and playoff qualification (top-18 as a single-table
approximation of the 9-per-conference field). The two conferences and the MLS
Cup playoff bracket are not modeled, and there is no relegation, so its report
shows **Shield%** and **Playoff%** columns instead of Champions League / Europe /
relegation. Its season is a calendar year (`--season 2026`). The openfootball
MLS feed is stale (its 2025 file stops in May 2025 and there's no 2026), so use
a fbref source for current MLS data. (For the browser-based `--source fbref`,
MLS isn't one of soccerdata's built-in leagues, so it's auto-registered as a
custom league on first use — best-effort.)

### 1. Ingest data

Every source writes the same two canonical CSVs so the rest of the pipeline is
source-agnostic:

- `data/leagues/<key>/teams.csv` — `id, team_name, code`
- `data/leagues/<key>/matches.csv` — `match_number, matchday, date,
  home_team_id, away_team_id, home_goals, away_goals, xg_home, xg_away, played`

```bash
# fbref-http (default) — current data + xG over plain HTTP, no browser (needs lxml)
venv/bin/python -m leagues.ingest eng --season 2025-2026
venv/bin/python -m leagues.ingest mls --season 2026

# fbref via soccerdata — same data through a browser that clears Cloudflare
venv/bin/python -m leagues.ingest eng --season 2025-2026 --source fbref

# openfootball GitHub mirror (schedules + scores, no xG) — works offline/sandboxed
venv/bin/python -m leagues.ingest eng --season 2024-25 --source openfootball
```

Data sources (all write the same canonical CSVs):

| `--source` | Browser? | xG? | Current data? | Notes |
|---|---|---|---|---|
| `fbref-http` *(default)* | no | yes | yes | plain HTTP + `read_html`; Cloudflare may block it |
| `fbref` | yes (Chrome) | yes | yes | soccerdata; reliably clears Cloudflare |
| `openfootball` | no | no | no (lags) | static mirror; offline, no dependencies |

You can also hand-author CSVs in the canonical schema (the `data/leagues/`
directory already ships with sample data for all six leagues). Unplayed
fixtures have empty goal/xG fields and `played = False`.

### 2. Fit and simulate a season

`run_sims` fits the attack/defense model on played matches, simulates the
remainder N times, and aggregates each team's finishing-position distribution
into `data/leagues/<key>/sim_results.json`.

```bash
venv/bin/python -m leagues.run_sims eng --sims 20000 --seed 0
venv/bin/python -m leagues.run_sims eng --as-of 20    # forecast from matchday 20
```

Output per team includes the live table, projected final points
(mean + 10th/90th percentiles), the full position-1..N probability vector, and
derived title / Champions-League / any-Europe / relegation percentages. It also
prints a title-race summary to the console.

Useful flags: `--sims`, `--seed`, `--as-of <matchday>` (treat later matches as
unplayed, for forecasting or backtesting), `--reg` (L2 shrinkage), and
`--recency-halflife` (down-weight older matches).

### 3. Generate the report

```bash
venv/bin/python -m leagues.generate_page          # all simulated leagues
venv/bin/python -m leagues.generate_page --open   # and open in a browser
```

Produces a self-contained `leagues.html` (no server, no dependencies) with a
league switcher and four views: standings odds, a position-probability heatmap,
remaining fixtures with win/draw/loss probabilities, and per-team finishing
distributions.

## The model

Each club gets an attack rating `a_i` and defense rating `d_i`, with a global
home-advantage term `h` and intercept `μ`:

```
log λ_home = μ + h + a_home − d_away
log λ_away = μ     + a_away − d_home
```

Ratings are fit by minimizing a **quasi-Poisson deviance** over every played
match-side, with each side's target being its xG when present and its goals
otherwise. This reduces to ordinary Poisson MLE for integer goals, accepts
continuous xG targets directly, and uses L2 shrinkage to pull sparse teams
toward league average. Optional exponential recency weighting up-weights recent
form.

## Layout

```
leagues/
  config.py               per-league definitions & tiebreakers
  ingest.py               fbref-http / fbref / openfootball → canonical CSVs
  model.py                attack/defense Poisson fit
  match.py                Poisson match primitive (λ → W/D/L probabilities)
  simulator.py            round-robin season engine + tiebreaker resolver
  run_sims.py             Monte Carlo driver
  generate_page.py        HTML report generator
  update.py               one-command ingest + simulate + report
data/leagues/<key>/       teams.csv, matches.csv (+ generated sim_results.json)
```
