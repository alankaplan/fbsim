"""
wc26_simulation.py
------------------
2026 FIFA World Cup match simulator using the Elo-Difference Poisson Model.

Each team's expected goals (λ) are derived from the difference in Elo ratings
via an exponential function, so a higher-rated team always expects more goals
than its opponent, smoothly scaling with the rating gap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import poisson

# ---------------------------------------------------------------------------
# Model constants
# ---------------------------------------------------------------------------

ALPHA: float = 0.26   # Baseline: ln(1.3) ≈ 0.26, representing ~1.3 goals/team
BETA: float = 0.003   # Elo scaling factor — each 100-point gap ≈ e^0.3 ≈ 1.35× more goals
HOME_ADVANTAGE: int = 100  # Elo bonus awarded to host nations (USA, Mexico, Canada)

K_GROUP: int = 40      # Elo K-factor for group-stage matches
K_KNOCKOUT: int = 50   # Elo K-factor for knockout matches (higher stakes)


# ---------------------------------------------------------------------------
# Core model functions
# ---------------------------------------------------------------------------

def calculate_expected_goals(
    team_a_elo: float,
    team_b_elo: float,
    a_is_host: bool = False,
    b_is_host: bool = False,
) -> tuple[float, float]:
    """
    Compute expected goals (λ) for each team using the Elo-Difference Poisson model.

    λ_A = exp(α + β × (Elo_A_adj − Elo_B_adj))
    λ_B = exp(α + β × (Elo_B_adj − Elo_A_adj))

    Parameters
    ----------
    team_a_elo : Elo rating of Team A before home adjustment.
    team_b_elo : Elo rating of Team B before home adjustment.
    a_is_host  : If True, add HOME_ADVANTAGE to Team A's Elo.
    b_is_host  : If True, add HOME_ADVANTAGE to Team B's Elo.

    Returns
    -------
    (lambda_a, lambda_b) — expected goals for each team.
    """
    elo_a = team_a_elo + (HOME_ADVANTAGE if a_is_host else 0)
    elo_b = team_b_elo + (HOME_ADVANTAGE if b_is_host else 0)

    diff = elo_a - elo_b
    lambda_a = float(np.exp(ALPHA + BETA * diff))
    lambda_b = float(np.exp(ALPHA - BETA * diff))   # equiv. to exp(α + β*(elo_b - elo_a))

    return lambda_a, lambda_b


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


def simulate_match_score(
    lambda_a: float,
    lambda_b: float,
    rng: np.random.Generator | None = None,
) -> tuple[int, int]:
    """
    Draw a random scoreline by sampling from two independent Poisson distributions.

    Parameters
    ----------
    lambda_a : Expected goals for Team A.
    lambda_b : Expected goals for Team B.
    rng      : Optional numpy Generator for reproducible results.
               Pass np.random.default_rng(seed) to fix the outcome.

    Returns
    -------
    (goals_a, goals_b) — a single simulated match result.
    """
    if rng is None:
        rng = np.random.default_rng()

    goals_a = int(rng.poisson(lambda_a))
    goals_b = int(rng.poisson(lambda_b))

    return goals_a, goals_b


def elo_update(
    elo_a: float,
    elo_b: float,
    goals_a: int,
    goals_b: int,
    is_knockout: bool = False,
) -> tuple[float, float]:
    """
    Return updated (elo_a, elo_b) after an observed match result.

    Uses the model's own Poisson win probability as the expected-score
    baseline so updates are internally consistent. Home advantage is NOT
    applied — Elo reflects neutral-ground team strength.

    Parameters
    ----------
    elo_a, elo_b : Pre-match ratings.
    goals_a, goals_b : Observed full-time score (regular time).
    is_knockout : Use K_KNOCKOUT instead of K_GROUP.
    """
    lam_a, lam_b = calculate_expected_goals(elo_a, elo_b)
    probs = get_match_probabilities(lam_a, lam_b)
    expected_a = probs["win_a"] + 0.5 * probs["draw"]
    actual_a   = 1.0 if goals_a > goals_b else (0.5 if goals_a == goals_b else 0.0)
    k = K_KNOCKOUT if is_knockout else K_GROUP
    delta = k * (actual_a - expected_a)
    return elo_a + delta, elo_b - delta


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def build_teams_dataframe() -> pd.DataFrame:
    """
    Return a DataFrame of 2026 World Cup teams with Elo ratings and host flags.

    Columns
    -------
    team    : Team name.
    elo     : Elo rating.
    is_host : True for co-host nations (USA, Mexico, Canada).
    """
    data = {
        "team":    ["France", "USA",   "Mexico", "Morocco"],
        "elo":     [2100,     1800,    1750,      1700],
        "is_host": [False,    True,    True,       False],
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Main demonstration
# ---------------------------------------------------------------------------

def main() -> None:
    teams = build_teams_dataframe()
    print("=== 2026 World Cup Teams ===")
    print(teams.to_string(index=False))
    print()

    # Look up France and USA
    france  = teams.loc[teams["team"] == "France"].iloc[0]
    usa     = teams.loc[teams["team"] == "USA"].iloc[0]

    print("=== France vs. USA (USA as host) ===")

    # Expected goals
    lambda_a, lambda_b = calculate_expected_goals(
        team_a_elo=float(france["elo"]),
        team_b_elo=float(usa["elo"]),
        a_is_host=bool(france["is_host"]),
        b_is_host=bool(usa["is_host"]),
    )
    print(f"  λ France : {lambda_a:.4f} expected goals")
    print(f"  λ USA    : {lambda_b:.4f} expected goals")
    print()

    # Match outcome probabilities
    probs = get_match_probabilities(lambda_a, lambda_b)
    print("Match probabilities:")
    print(f"  France win : {probs['win_a']:.1%}")
    print(f"  Draw       : {probs['draw']:.1%}")
    print(f"  USA win    : {probs['win_b']:.1%}")
    print(f"  Total      : {sum(probs.values()):.6f}  (≈1, residual from PMF truncation)")
    print()

    # Simulated scoreline (seed=42 for reproducibility)
    rng = np.random.default_rng(42)
    goals_a, goals_b = simulate_match_score(lambda_a, lambda_b, rng=rng)
    result = "France win" if goals_a > goals_b else ("USA win" if goals_b > goals_a else "Draw")
    print(f"Simulated scoreline (seed=42): France {goals_a} – {goals_b} USA  →  {result}")


if __name__ == "__main__":
    main()
