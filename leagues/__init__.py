"""
leagues
-------
Domestic-league season simulator for the big-five European leagues (and,
eventually, MLS), built on the same Elo/Poisson core as the World Cup
simulator. Team strengths are derived from FBref expected-goals (xG) data
when available, falling back to actual goals otherwise.

Layers (mirroring the World Cup package):

  config.py    — per-league definitions: qualification/relegation slots and
                 the tiebreaker chain (leagues rank ties differently).
  ingest.py    — pluggable data ingestion. Writes canonical CSVs into
                 data/leagues/<key>/. Sources: FBref (via soccerdata), a
                 manual CSV drop, or the openfootball GitHub mirror (used for
                 offline/sandbox validation — schedules + scores, no xG).
  model.py     — attack/defense Poisson strengths fit from xG (or goals).
  simulator.py — single-pool round-robin season engine + configurable
                 standings, reusing the World Cup match primitives.
  run_sims.py  — Monte Carlo driver → league_sim_results.json.
  generate_page.py — self-contained league.html report.
"""
