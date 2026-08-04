# fbsim

A **domestic-league season simulator** for the big-five European leagues plus
three North American ones (MLS, NWSL, USL Championship). Team strengths are fit as
attack/defense Poisson ratings from FBref
expected-goals (xG) data (falling back to actual goals), and full seasons are
run by Monte Carlo to produce title, qualification and relegation probabilities.

A match scoreline is two independent Poisson draws, and win/draw/loss
probabilities come from the outer product of the two Poisson PMFs.

## Requirements

- Python 3.10+
- `numpy`, `pandas`, `scipy`
- That's it for the default **fixturedownload** source — it fetches free
  fixturedownload.com JSON feeds over plain HTTP (no browser, no auth, no extra
  packages) and covers current seasons. It carries scores but **no xG**, so the
  model falls back to goals.
- *Optional, only for xG via `--source fbref`:* `soccerdata` + a
  **Chrome/Chromium** browser (fbref's Cloudflare blocks plain-HTTP clients, so
  soccerdata drives a real browser); plus `pyvirtualdisplay` + the `xvfb` package
  on Linux to hide the window (or `xvfb-run`; `FBSIM_SHOW_BROWSER=1` shows it).

```bash
python -m venv venv
venv/bin/pip install numpy pandas scipy
# optional, only for xG via --source fbref:
venv/bin/pip install soccerdata pyvirtualdisplay   # + `sudo apt install xvfb` on Linux
```

## Quick start

One command refreshes data, re-simulates the leagues whose data changed, and
rebuilds the report:

```bash
# Refresh every league at its current season via fixturedownload (plain JSON,
# no browser), simulate what changed, and open the page:
venv/bin/python -m leagues.update --refresh --open

# Re-simulate stale leagues from the CSVs already on disk (no network):
venv/bin/python -m leagues.update
```

It's incremental: ingested CSVs are only rewritten when they actually differ,
and a league is re-simulated only when its `matches.csv` is newer than its
`sim_results.json` (or the result is missing, or its result predates the current
output schema, or you pass `--force`). Re-running when nothing has changed does no
simulation work and just rebuilds `leagues.html`. Pulling a new version that adds
report fields therefore re-simulates each league automatically on the next
`update` — no `--force` needed.

`--refresh` ingests each league at its **current season, auto-detected from
today's date** — European leagues roll to the new season in July (so they track
2026-27 from mid-2026), and MLS uses the calendar year. A season that hasn't
kicked off yet has no games to fit, so `update` auto-builds a preseason prior
from last season and shows a projection instead of crashing (see below).
Use `--season <s>` to pin a specific season, `--source fbref` to add xG
(browser), or `--source openfootball` for the offline mirror.

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
| `model.py` | Attack/defense Poisson strengths fit from xG (or goals), optionally shrunk toward a preseason prior |
| `prior.py` | Build/load a preseason prior from last season's ratings |
| `match.py` | Poisson match primitive: (λ_home, λ_away) → win/draw/loss probabilities |
| `simulator.py` | Single-pool round-robin season engine + configurable standings |
| `run_sims.py` | Monte Carlo driver → `sim_results.json` |
| `generate_page.py` | Self-contained `leagues.html` report |
| `update.py` | One command: ingest → simulate what changed → rebuild the report |

Supported leagues (keys): `eng` (Premier League), `esp` (La Liga),
`ita` (Serie A), `de` (Bundesliga), `fr` (Ligue 1), `mls` (MLS), `nwsl` (NWSL),
`usl` (USL Championship). Slot counts and tiebreaker order are per-league — Spain
and Italy apply head-to-head before overall goal difference; England, Germany and
France use overall goal difference first; the US leagues rank on wins before goal
difference.

**US-league notes.** MLS, NWSL and USL Championship are each modeled as a single
calendar-year table producing a regular-season (**Shield**) race and **Playoff**
qualification — a single-table approximation of a conference league (top-18 for
MLS, top-8 for NWSL, top-16 for USL). The conferences and the playoff brackets are
not modeled, and there is no relegation, so their reports show **Shield%** and
**Playoff%** columns instead of Champions League / Europe / relegation. Seasons are
calendar years (auto-detected — e.g. `2026` in 2026). None are soccerdata built-in
leagues; the CLIs register them in soccerdata's `league_dict.json` **before** it is
imported, so `USA-NWSL` / `USA-USL Championship` resolve correctly. **Data sources
differ (and each league picks its own by default):** MLS and NWSL default to the
free **fixturedownload** feed (no browser), while **USL Championship has no free
feed and defaults to `fbref`** (FBref comp 73, via soccerdata + a browser). NWSL is
FBref comp 182 if you want its xG.

**US-league FBref data is best-effort and needs a browser with a display.** The
Big-5 (xG + players via Understat, no browser) and MLS/NWSL *fixtures* (via
fixturedownload) work headless. But **USL fixtures and *all* US-league player stats
come from FBref**, which throws a CAPTCHA its solver can't clear in headless mode —
so on a headless server those are slow (CAPTCHA retries) and usually **skipped**
(the run continues; nothing is lost). Run them on a desktop, or under `xvfb-run`
with a real display so soccerdata's GUI CAPTCHA solver can engage. A locked-down
egress policy also blocks the soccerdata hosts entirely.

### 1. Ingest data

Every source writes the same two canonical CSVs so the rest of the pipeline is
source-agnostic:

- `data/leagues/<key>/teams.csv` — `id, team_name, code`
- `data/leagues/<key>/matches.csv` — `match_number, matchday, date,
  home_team_id, away_team_id, home_goals, away_goals, xg_home, xg_away, played`

**Each league has a default schedule source** (below), so `--source` is optional —
omit it and the right source is used automatically. For the Big-5, **fixtures come
from fixturedownload and xG is overlaid from Understat** onto played games (matched
by team) — a reliable schedule (with future fixtures) plus xG when it exists:

```bash
# Uses each league's own schedule source (+ automatic xG overlay for the Big-5):
venv/bin/python -m leagues.ingest eng  --season 2025   # fixturedownload + Understat xG
venv/bin/python -m leagues.ingest mls  --season 2026   # fixturedownload (no xG)
venv/bin/python -m leagues.ingest usl  --season 2026   # fbref (browser; xG inline)

# Override the schedule source explicitly when you want to:
venv/bin/python -m leagues.ingest eng --season 2024-25 --source openfootball  # offline
```

Note: a source that lacks a season (e.g. Understat has no *upcoming*-season
fixtures) never overwrites good CSVs — the ingest keeps what's on disk and reports
it. Preseason simply has no xG yet (there are no played games); the overlay fills it
in once matches are played.

**Individual player stats** (informational; they don't feed the model) go into
`data/leagues/<key>/players.csv` via a separate step, sourced per league — Understat
for the Big-5 (no browser), FBref for the US leagues (browser, since fixturedownload
has no player data):

```bash
venv/bin/python -m leagues.players eng           # -> understat (goals, assists, xG/xA)
venv/bin/python -m leagues.players mls            # -> fbref (browser)
venv/bin/python -m leagues.update --refresh --players   # refresh fixtures + players together
```

They surface as the report's **Top players** tab and each team's **Players** sub-tab.

Data sources (all write the same canonical CSVs):

| `--source` | Browser? | xG? | Coverage | Notes |
|---|---|---|---|---|
| `fixturedownload` | no | no | broad (incl. MLS/NWSL) | free JSON feed; reliable, has future fixtures; **default schedule for the Big-5/MLS/NWSL** |
| `understat` | no | **yes** | Big-5 only | soccerdata; own xG model; **overlaid onto the Big-5** (also serves their player stats) |
| `fbref` | yes (Chrome) | yes | broad (incl. USL) | soccerdata; clears Cloudflare via Xvfb; may hit a captcha; **default for USL**; player stats for US leagues |
| `fbref-http` | no | yes | broad | `curl_cffi` + `read_html`; **currently blocked by Cloudflare** |
| `openfootball` | no | no | Big-5 (lags) | static mirror; offline fallback |

**Per-league defaults:** the Big-5 (`eng/esp/ita/de/fr`) and `mls`/`nwsl` take
their **schedule** from `fixturedownload`; `usl` from `fbref`. The Big-5
additionally **overlay xG from `understat`** onto played games. So a plain
`python -m leagues.update --refresh` gets reliable fixtures everywhere plus xG for
the Big-5 — no `--source` needed. `understat`/`fbref` need the `soccerdata` package
(`fbref` also a browser); a locked-down egress policy may block their hosts.

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
unplayed, for forecasting or backtesting), `--reg` (L2 shrinkage),
`--recency-halflife` (down-weight older matches), and `--no-prior` /
`--prior-weight` (see below).

#### Preseason priors

By default a team's rating is fit only from *this* season's played matches, so
at the very start of a season there's nothing to fit. The model can instead be
seeded with **last season's ratings**. `update --refresh` does this for you: for
a season with no games played yet it auto-builds the prior from last season, so a
brand-new season shows a real projection out of the box. You can also build it
explicitly:

```bash
venv/bin/python -m leagues.prior eng     # snapshot the previous season -> prior.json
venv/bin/python -m leagues.update --refresh   # (and run_sims) use it automatically
```

`leagues.prior` fits the season before the current one (any `--source`, in
memory — it doesn't touch your `matches.csv`) and writes
`data/leagues/<key>/prior.json`. When present, `fit_model` shrinks each team's
attack/defense toward its prior (weight `--prior-weight`, ~that many
pseudo-matches) instead of toward league average. So early on the table reflects
last season, and the prior washes out over the first few weeks as real results
accumulate. Teams with no prior (promoted) start at league average, and a
**not-yet-started season** produces a sensible preseason table instead of
crashing. `--no-prior` restores the pure single-season fit.

### 3. Generate the report

```bash
venv/bin/python -m leagues.generate_page          # all simulated leagues
venv/bin/python -m leagues.generate_page --open   # and open in a browser
```

Produces a self-contained `leagues.html` (no server, no dependencies) with a
league switcher and six views: standings odds, a position-probability heatmap,
remaining fixtures, a cross-league **Top games** schedule, a **Top players** table
(individual season stats), and a per-team detail view (finishing distribution, full
schedule, a branching title-odds explorer, and a squad **Players** sub-tab).

Each league page also reports its **title-race entropy in bits** (in the header)
— the Shannon entropy of the simulated champion distribution, i.e. how open the
race still is (≈ 0 bits for a settled league, ≈ 2 bits for a roughly four-way
race). A **completed** season has a determined champion, so its entropy is
exactly 0 and the header reads `0 bits (decided)` — this is expected, not a bug;
an in-progress or preseason race shows a positive value.

The **fixtures** view has sortable columns (click any header) and shows, per
remaining game: kickoff in **US Pacific time** (from the fixturedownload feed;
date-only for sources without a timestamp), model expected goals, win/draw/loss
probabilities, **Info%** — how much that fixture is expected to decide the
title, measured as the percent drop in the entropy of the champion distribution
once its result is known (the mutual information between the game's outcome and
who wins the league) — and **H after**, the *expected* title-race entropy (bits)
still remaining once that game's round is played, estimated by re-forecasting the
rest of the season from sampled partial standings (`--resolution-sims`, `0`
disables). It declines gradually across the season to 0 at the end — a genuine
"how decided is the title" resolution curve. Sort by Kickoff to watch H after tick
down, or by Info% to surface the season's most decisive single games.

**Preseason ratings are regressed toward the mean.** Last season's attack/defense
ratings are scaled toward league average before seeding a new season, so the
defending champion starts as a *favorite* rather than a near-certainty and the
title race carries realistic uncertainty out of the box (this washes out as real
results accumulate). Tune it with `--prior-regression` on `update` / `run_sims`
(default `0.70`; `1.0` disables regression, `0.0` starts everyone level).

The **Top games** view (a top-level tab next to the league buttons) merges the
most title-decisive upcoming games *across all leagues* into one schedule. You
set a single **title-race entropy threshold (in bits)** that applies to every
league, and each league contributes just enough of its most decisive games
(revealed in Info% order) to pull its champion-distribution entropy **below that
threshold** — so a wide-open league shows more games than a nearly-settled one,
and a league already below the threshold shows none. A **team dropdown**
(checkboxes, grouped by league, with a filter) additionally adds *every*
remaining game of any team you pick — a "follow my teams" list on top of the
threshold picks. Both the threshold and the followed teams are remembered across
visits (`localStorage`). The combined list is sortable and defaults to
chronological order — a quick "what should I watch" board.

Clicking any team opens its **Team detail** view: the finishing-position
distribution, the team's **full schedule** (past games with results, upcoming
games with win/draw/loss predictions and — for each remaining game — how the
team's title odds move if it wins, draws, or loses), and a **branching explorer**.
The branch is interactive: click Win / Draw / Loss to walk a scenario across the
team's next games and watch its title probability update along the chosen path,
each node conditioned on that path (everything else simulated). For non-contenders
the metric automatically switches from title % to **expected finishing position**,
which stays meaningful mid-table. Branches with too few matching simulations to
trust are faded out. Because each node re-reads the team's *own* results only
(never a long joint sequence of every game), the estimates stay dense and reliable
a handful of games deep rather than collapsing to spurious certainty.

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
  ingest.py               fixturedownload / fbref / openfootball → canonical CSVs
  model.py                attack/defense Poisson fit (+ preseason prior)
  prior.py                build/load last-season prior
  match.py                Poisson match primitive (λ → W/D/L probabilities)
  simulator.py            round-robin season engine + tiebreaker resolver
  run_sims.py             Monte Carlo driver
  generate_page.py        HTML report generator
  update.py               one-command ingest + simulate + report
data/leagues/<key>/       teams.csv, matches.csv (+ generated sim_results.json)
```
