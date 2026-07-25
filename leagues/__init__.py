"""
leagues
-------
Domestic-league season simulator for the big-five European leagues (and,
eventually, MLS), built on a Poisson attack/defense core. Team strengths are
derived from FBref expected-goals (xG) data when available, falling back to
actual goals otherwise.

Layers:

  config.py    — per-league definitions: qualification/relegation slots and
                 the tiebreaker chain (leagues rank ties differently).
  ingest.py    — pluggable data ingestion. Writes canonical CSVs into
                 data/leagues/<key>/. Sources: FBref (via soccerdata), a
                 manual CSV drop, or the openfootball GitHub mirror (used for
                 offline/sandbox validation — schedules + scores, no xG).
  model.py     — attack/defense Poisson strengths fit from xG (or goals).
  match.py     — Poisson match primitive: (λ_home, λ_away) → W/D/L probs.
  simulator.py — single-pool round-robin season engine + configurable
                 standings.
  run_sims.py  — Monte Carlo driver → sim_results.json.
  generate_page.py — self-contained leagues.html report.
"""
