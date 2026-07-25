# fbsim

Poisson-based football match and season simulators. Two engines share a common
statistical core:

- **`wc26_simulation.py`** — a 2026 FIFA World Cup match simulator using an
  **Elo-difference Poisson model**: each team's expected goals scale
  exponentially with the Elo gap between the two sides.
- **`leagues/`** — a **domestic-league season simulator** for the big-five
  European leagues. Team strengths are fit as attack/defense Poisson ratings
  from FBref expected-goals (xG) data (falling back to actual goals), and full
  seasons are run by Monte Carlo to produce title, European-qualification and
  relegation probabilities.

Both use the same match primitives — a scoreline is two independent Poisson
draws, and win/draw/loss probabilities come from the outer product of the two
Poisson PMFs.

## Requirements

- Python 3.10+
- `numpy`, `pandas`, `scipy`
- `soccerdata` *(optional)* — only for pulling fresh FBref data with xG

```bash
python -m venv venv
venv/bin/pip install numpy pandas scipy
venv/bin/pip install soccerdata   # optional, for the fbref ingest source
```

## The World Cup simulator

A standalone, dependency-light match model.

```bash
venv/bin/python wc26_simulation.py
```

The model (`wc26_simulation.py`):

```
λ_A = exp(α + β · (Elo_A − Elo_B))
λ_B = exp(α + β · (Elo_B − Elo_A))
```

with `α = 0.26` (≈1.3 baseline goals), `β = 0.003` (each 100 Elo points ≈
1.35× goals) and a `+100` Elo home bonus for host nations. Key functions:

| Function | Purpose |
|---|---|
| `calculate_expected_goals` | Elo → (λ_home, λ_away) |
| `get_match_probabilities` | (λ_a, λ_b) → win/draw/loss probabilities |
| `simulate_match_score` | Draw a random scoreline (Poisson) |
| `elo_update` | Post-match Elo update, K=40 group / K=50 knockout |

These same primitives are reused by the league engine.

## The league simulator

A layered pipeline under `leagues/`, mirroring the World Cup package:

| Module | Role |
|---|---|
| `config.py` | Per-league definitions: team count, European/relegation slots, tiebreaker chain |
| `ingest.py` | Pluggable data ingestion → canonical CSVs in `data/leagues/<key>/` |
| `model.py` | Attack/defense Poisson strengths fit from xG (or goals) |
| `simulator.py` | Single-pool round-robin season engine + configurable standings |
| `run_sims.py` | Monte Carlo driver → `sim_results.json` |
| `generate_page.py` | Self-contained `leagues.html` report |

Supported leagues (keys): `eng` (Premier League), `esp` (La Liga),
`ita` (Serie A), `de` (Bundesliga), `fr` (Ligue 1). Slot counts and tiebreaker
order are per-league — Spain and Italy apply head-to-head before overall goal
difference; England, Germany and France use overall goal difference first.

### 1. Ingest data

Every source writes the same two canonical CSVs so the rest of the pipeline is
source-agnostic:

- `data/leagues/<key>/teams.csv` — `id, team_name, code`
- `data/leagues/<key>/matches.csv` — `match_number, matchday, date,
  home_team_id, away_team_id, home_goals, away_goals, xg_home, xg_away, played`

```bash
# FBref (primary, carries xG) — needs soccerdata + outbound access to fbref.com
venv/bin/python -m leagues.ingest eng --season 2025-2026 --source fbref

# openfootball GitHub mirror (schedules + scores, no xG) — works offline/sandboxed
venv/bin/python -m leagues.ingest eng --season 2024-25 --source openfootball
```

You can also hand-author CSVs in the canonical schema (the `data/leagues/`
directory already ships with sample data for all five leagues). Unplayed
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
wc26_simulation.py        World Cup Elo-difference Poisson match model
leagues/
  config.py               per-league definitions & tiebreakers
  ingest.py               fbref / openfootball / manual → canonical CSVs
  model.py                attack/defense Poisson fit
  simulator.py            round-robin season engine + tiebreaker resolver
  run_sims.py             Monte Carlo driver
  generate_page.py        HTML report generator
data/leagues/<key>/       teams.csv, matches.csv (+ generated sim_results.json)
```
