"""
model.py
--------
Attack/defense Poisson strength model for a domestic league, fit from FBref
expected goals (xG) when available and from actual goals otherwise.

Each club gets an attack rating ``a_i`` and a defense rating ``d_i``; a single
global home-advantage term ``h`` and intercept ``mu`` complete the model:

    log λ_home = mu + h + a_home - d_away
    log λ_away = mu     + a_away - d_home

Ratings are estimated by minimising a **quasi-Poisson** deviance over every
played match-side::

    NLL = Σ w · (λ - y·log λ)         (+ L2 shrinkage on a, d)

with per-match target ``y`` = the side's xG when present, else its goals. This
has three convenient properties:

  * it reduces to ordinary Poisson MLE when the targets are integer goals;
  * it accepts continuous xG targets directly (no rounding);
  * L2 shrinkage toward zero identifies the otherwise shift-degenerate
    attack/defense split and gracefully pulls sparse teams to league average,
    so no separate "too few games" fallback is needed.

Optional exponential recency weighting up-weights recent form.

The fitted model exposes :meth:`expected_goals`, which returns ``(λ_home,
λ_away)`` for any fixture — the exact input the Poisson match primitive
(:func:`leagues.match.get_match_probabilities`) and the simulator's
``rng.poisson`` scoreline draws expect.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize


@dataclass
class LeagueModel:
    team_ids: list[int]            # external team ids, in ratings order
    attack: np.ndarray             # a_i, sum-to-~zero
    defense: np.ndarray            # d_i
    home_adv: float                # h
    intercept: float               # mu
    used_xg: bool                  # True if any target came from xG

    def __post_init__(self) -> None:
        self._idx = {tid: i for i, tid in enumerate(self.team_ids)}

    # -- inference ----------------------------------------------------------
    def expected_goals(self, home_id: int, away_id: int) -> tuple[float, float]:
        """Return (λ_home, λ_away) expected goals for a fixture."""
        h, a = self._idx[home_id], self._idx[away_id]
        lam_home = np.exp(self.intercept + self.home_adv + self.attack[h] - self.defense[a])
        lam_away = np.exp(self.intercept + self.attack[a] - self.defense[h])
        return float(lam_home), float(lam_away)

    def rating(self, team_id: int) -> dict[str, float]:
        i = self._idx[team_id]
        return {"attack": float(self.attack[i]), "defense": float(self.defense[i])}

    def strength_table(self) -> pd.DataFrame:
        """Diagnostic: one row per team with attack/defense/overall strength."""
        rows = [{"team_id": tid,
                 "attack": float(self.attack[i]),
                 "defense": float(self.defense[i]),
                 "overall": float(self.attack[i] + self.defense[i])}
                for i, tid in enumerate(self.team_ids)]
        return pd.DataFrame(rows).sort_values("overall", ascending=False, ignore_index=True)


def _targets(matches: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, bool]:
    """Per-match (home_target, away_target); prefer xG, fall back to goals."""
    hx = pd.to_numeric(matches.get("xg_home"), errors="coerce")
    ax = pd.to_numeric(matches.get("xg_away"), errors="coerce")
    hg = pd.to_numeric(matches["home_goals"], errors="coerce")
    ag = pd.to_numeric(matches["away_goals"], errors="coerce")
    used_xg = bool(hx.notna().any())
    y_home = hx.where(hx.notna(), hg).to_numpy(dtype=float)
    y_away = ax.where(ax.notna(), ag).to_numpy(dtype=float)
    return y_home, y_away, used_xg


def fit_model(
    teams: pd.DataFrame,
    matches: pd.DataFrame,
    *,
    reg: float = 0.05,
    recency_halflife: float | None = None,
) -> LeagueModel:
    """
    Fit the attack/defense model from played matches.

    Parameters
    ----------
    teams   : DataFrame with an ``id`` column (all league teams).
    matches : DataFrame with home_team_id, away_team_id, played and either
              xg_home/xg_away or home_goals/away_goals for played rows.
    reg     : L2 shrinkage strength on attack/defense ratings.
    recency_halflife : if set, matches this many rows before the last played
              game get half weight (exponential decay by played-match order).
    """
    team_ids = [int(t) for t in teams["id"].tolist()]
    idx = {tid: i for i, tid in enumerate(team_ids)}
    n = len(team_ids)

    played = matches[matches["played"].astype(str).isin(["True", "true", "1"])
                     | (matches["played"] == True)].copy()  # noqa: E712
    if played.empty:
        raise ValueError("No played matches to fit the model on.")

    home = played["home_team_id"].astype(int).map(idx).to_numpy()
    away = played["away_team_id"].astype(int).map(idx).to_numpy()
    y_home, y_away, used_xg = _targets(played)

    # Recency weights (older matches down-weighted), normalised to mean 1.
    m = len(played)
    if recency_halflife and recency_halflife > 0:
        age = np.arange(m)[::-1]                      # 0 = most recent
        w = 0.5 ** (age / float(recency_halflife))
    else:
        w = np.ones(m)
    w = w / w.mean()

    # Parameter layout: [mu, home_adv, attack(n), defense(n)]
    def unpack(p):
        return p[0], p[1], p[2:2 + n], p[2 + n:2 + 2 * n]

    def neg_ll(p):
        mu, h, atk, dfn = unpack(p)
        eta_h = mu + h + atk[home] - dfn[away]
        eta_a = mu + atk[away] - dfn[home]
        lam_h, lam_a = np.exp(eta_h), np.exp(eta_a)
        dev = np.sum(w * (lam_h - y_home * eta_h)) + np.sum(w * (lam_a - y_away * eta_a))
        dev += reg * (np.sum(atk ** 2) + np.sum(dfn ** 2))
        return dev

    def grad(p):
        mu, h, atk, dfn = unpack(p)
        eta_h = mu + h + atk[home] - dfn[away]
        eta_a = mu + atk[away] - dfn[home]
        lam_h, lam_a = np.exp(eta_h), np.exp(eta_a)
        rh = w * (lam_h - y_home)          # d NLL / d eta_h
        ra = w * (lam_a - y_away)          # d NLL / d eta_a
        g = np.zeros_like(p)
        g[0] = rh.sum() + ra.sum()                              # mu
        g[1] = rh.sum()                                         # home_adv
        g_atk = np.zeros(n)
        np.add.at(g_atk, home, rh)         # attack of home team
        np.add.at(g_atk, away, ra)         # attack of away team
        g_dfn = np.zeros(n)
        np.add.at(g_dfn, away, -rh)        # defense of away team
        np.add.at(g_dfn, home, -ra)        # defense of home team
        g[2:2 + n] = g_atk + 2 * reg * atk
        g[2 + n:2 + 2 * n] = g_dfn + 2 * reg * dfn
        return g

    p0 = np.zeros(2 + 2 * n)
    p0[0] = np.log(max((y_home.sum() + y_away.sum()) / (2 * m), 0.3))  # mu ~ log avg goals
    p0[1] = 0.15
    res = minimize(neg_ll, p0, jac=grad, method="L-BFGS-B",
                   options={"maxiter": 500, "ftol": 1e-9})

    mu, h, atk, dfn = unpack(res.x)
    # Centre attack/defense for interpretability; fold the shift into the
    # intercept so every predicted λ is left exactly unchanged
    # (a_H - d_A = a'_H - d'_A + (mean(a) - mean(d))).
    mu = mu + float(atk.mean()) - float(dfn.mean())
    atk = atk - atk.mean()
    dfn = dfn - dfn.mean()
    return LeagueModel(team_ids=team_ids, attack=atk, defense=dfn,
                       home_adv=float(h), intercept=float(mu), used_xg=used_xg)
