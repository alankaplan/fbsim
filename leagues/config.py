"""
config.py
---------
Per-league definitions for the domestic-league simulator.

Each league differs in three ways that matter to the model and the report:

  * size / fixture count (20-team leagues play 380 games, 18-team play 306);
  * European-qualification and relegation slots;
  * the tiebreaker chain used to separate teams level on points.

The tiebreaker vocabulary (applied in order until the tie is broken):

  "pts" — competition points (always first).
  "h2h" — a mini-table among only the tied teams: head-to-head points, then
          head-to-head goal difference, then head-to-head goals for.
  "gd"  — overall goal difference.
  "gf"  — overall goals for.

Spain (La Liga) and Italy (Serie A) apply head-to-head *before* overall goal
difference; England, Germany and France use overall goal difference first.
The final, always-deterministic fallback is ascending team id.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LeagueConfig:
    key: str                       # short slug, also the data/leagues/<key>/ dir
    name: str                      # display name
    country: str
    n_teams: int
    # Data source identifiers ------------------------------------------------
    openfootball_path: str         # e.g. "en.1" -> <season>/en.1.json on the mirror
    fbref_league: str              # soccerdata league id, e.g. "ENG-Premier League"
    # Table semantics --------------------------------------------------------
    ucl_slots: int                 # top-N qualify for the Champions League
    relegation_slots: int          # bottom-R are relegated
    tiebreakers: tuple[str, ...]   # ordered chain from the vocabulary above
    europa_slots: int = 0          # informational band shown in the report

    @property
    def total_matches(self) -> int:
        return self.n_teams * (self.n_teams - 1)


# The current big-five European leagues. Slot counts reflect the 2025-26
# allocation and are intentionally easy to edit as UEFA coefficients shift.
LEAGUES: dict[str, LeagueConfig] = {
    "eng": LeagueConfig(
        key="eng", name="Premier League", country="England", n_teams=20,
        openfootball_path="en.1", fbref_league="ENG-Premier League",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
    "esp": LeagueConfig(
        key="esp", name="La Liga", country="Spain", n_teams=20,
        openfootball_path="es.1", fbref_league="ESP-La Liga",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "h2h", "gd", "gf"),
    ),
    "ita": LeagueConfig(
        key="ita", name="Serie A", country="Italy", n_teams=20,
        openfootball_path="it.1", fbref_league="ITA-Serie A",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "h2h", "gd", "gf"),
    ),
    "de": LeagueConfig(
        key="de", name="Bundesliga", country="Germany", n_teams=18,
        openfootball_path="de.1", fbref_league="GER-Bundesliga",
        ucl_slots=4, europa_slots=2, relegation_slots=2,  # +1 relegation playoff, not modeled
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
    "fr": LeagueConfig(
        key="fr", name="Ligue 1", country="France", n_teams=18,
        openfootball_path="fr.1", fbref_league="FRA-Ligue 1",
        ucl_slots=4, europa_slots=2, relegation_slots=2,  # +1 relegation playoff, not modeled
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
}


def get_league(key: str) -> LeagueConfig:
    try:
        return LEAGUES[key]
    except KeyError:
        valid = ", ".join(LEAGUES)
        raise SystemExit(f"Unknown league '{key}'. Choose one of: {valid}")
