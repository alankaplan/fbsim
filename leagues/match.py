"""
match.py
--------
Poisson match primitive shared by the league engine: convert a fixture's two
expected-goal rates (λ_home, λ_away) into win/draw/loss probabilities.

The scoreline model is two independent Poisson counts; the Monte Carlo
simulator draws those directly with ``numpy``'s ``rng.poisson``, while this
function gives the exact analytic outcome distribution used to annotate
remaining fixtures in the report.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import poisson


def get_match_probabilities(
    lambda_a: float,
    lambda_b: float,
    max_goals: int = 10,
) -> dict[str, float]:
    """
    Calculate win/draw/win probabilities from Poisson-distributed goal expectations.

    Constructs an (max_goals+1) × (max_goals+1) scoreline probability matrix
    via the outer product of two Poisson PMFs, then sums regions without loops.
    With row i = A's goals and col j = B's goals:
      - Lower triangle (k=-1) → Team A wins  (i > j, goals_A > goals_B)
      - Main diagonal         → Draw          (goals_A = goals_B)
      - Upper triangle (k=1)  → Team B wins  (j > i, goals_B > goals_A)

    Parameters
    ----------
    lambda_a  : Expected goals for Team A.
    lambda_b  : Expected goals for Team B.
    max_goals : Goals range 0..max_goals (inclusive) for the PMF truncation.

    Returns
    -------
    dict with keys 'win_a', 'draw', 'win_b' — probabilities summing to ≈1.
    """
    goals = np.arange(0, max_goals + 1)
    pmf_a = poisson.pmf(goals, lambda_a)   # P(Team A scores k goals)
    pmf_b = poisson.pmf(goals, lambda_b)   # P(Team B scores k goals)

    # matrix[i, j] = P(A scores i) × P(B scores j)
    matrix = np.outer(pmf_a, pmf_b)

    # matrix[i, j]: row i = A's goals, col j = B's goals
    # A wins when i > j  → rows below diagonal → lower triangle (k=-1)
    # B wins when j > i  → cols right of diagonal → upper triangle (k=1)
    win_a = float(np.tril(matrix, k=-1).sum())  # A scores more than B
    draw  = float(np.trace(matrix))             # equal scores
    win_b = float(np.triu(matrix, k=1).sum())   # B scores more than A

    return {"win_a": win_a, "draw": draw, "win_b": win_b}
