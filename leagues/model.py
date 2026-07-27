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
class LeaguePrior:
    """A preseason prior: last season's ratings, keyed by team name.

    ``attack``/``defense`` map ``team_name`` → rating; teams absent from the maps
    (e.g. promoted) fall back to league average (0). ``intercept``/``home_adv``
    seed those global terms before any games are played.
    """
    attack: dict[str, float]
    defense: dict[str, float]
    intercept: float
    home_adv: float


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
    prior: LeaguePrior | None = None,
    prior_weight: float = 3.0,
) -> LeagueModel:
    """
    Fit the attack/defense model from played matches.

    Parameters
    ----------
    teams   : DataFrame with ``id`` and ``team_name`` columns (all league teams).
    matches : DataFrame with home_team_id, away_team_id, played and either
              xg_home/xg_away or home_goals/away_goals for played rows.
    reg     : L2 shrinkage strength for teams with no prior (toward average).
    recency_halflife : if set, matches this many rows before the last played
              game get half weight (exponential decay by played-match order).
    prior   : optional preseason prior (last season's ratings). When given,
              attack/defense shrink toward the prior (weight ``prior_weight``,
              ~that many pseudo-matches) instead of toward 0, and a season with
              no played matches returns the prior directly instead of raising.
    prior_weight : shrinkage strength toward the prior for teams it covers;
              higher = the prior persists longer before real games wash it out.
    """
    team_ids = [int(t) for t in teams["id"].tolist()]
    idx = {tid: i for i, tid in enumerate(team_ids)}
    names = [str(t) for t in teams["team_name"].tolist()]  # aligned to team_ids
    n = len(team_ids)

    # Prior targets (a0/d0) and per-team penalty weights. Without a prior this is
    # a0=d0=0 with weight `reg` everywhere — identical to shrinking toward average.
    a0 = np.zeros(n)
    d0 = np.zeros(n)
    pw = np.full(n, reg)
    if prior is not None:
        for i, nm in enumerate(names):
            if nm in prior.attack:
                a0[i] = float(prior.attack[nm])
                d0[i] = float(prior.defense.get(nm, 0.0))
                pw[i] = prior_weight
    prior_mu = prior.intercept if prior is not None else None
    prior_h = prior.home_adv if prior is not None else None

    played = matches[matches["played"].astype(str).isin(["True", "true", "1"])
                     | (matches["played"] == True)].copy()  # noqa: E712
    if played.empty:
        # No games yet: fall back to the prior (or a flat league-average model),
        # so a not-yet-started season simulates instead of crashing.
        mu = (prior_mu if prior_mu is not None else float(np.log(1.35)))
        h = prior_h if prior_h is not None else 0.15
        mu = mu + float(a0.mean()) - float(d0.mean())
        return LeagueModel(team_ids=team_ids, attack=a0 - a0.mean(),
                           defense=d0 - d0.mean(), home_adv=float(h),
                           intercept=float(mu), used_xg=False)

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
        dev += np.sum(pw * ((atk - a0) ** 2 + (dfn - d0) ** 2))  # shrink toward prior
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
        g[2:2 + n] = g_atk + 2 * pw * (atk - a0)
        g[2 + n:2 + 2 * n] = g_dfn + 2 * pw * (dfn - d0)
        return g

    p0 = np.zeros(2 + 2 * n)
    p0[0] = (prior_mu if prior_mu is not None
             else np.log(max((y_home.sum() + y_away.sum()) / (2 * m), 0.3)))
    p0[1] = prior_h if prior_h is not None else 0.15
    p0[2:2 + n] = a0
    p0[2 + n:2 + 2 * n] = d0
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
