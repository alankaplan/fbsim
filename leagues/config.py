"""
config.py
---------
Per-league definitions for the domestic-league simulator.

Each league differs in three ways that matter to the model and the report:

  * size / fixture count (20-team leagues play 380 games, 18-team play 306);
  * European-qualification and relegation slots;
  * the tiebreaker chain used to separate teams level on points.

The tiebreaker vocabulary (applied in order until the tie is broken):

  "pts"  — competition points (always first).
  "wins" — total wins (MLS ranks on this before goal difference).
  "h2h"  — a mini-table among only the tied teams: head-to-head points, then
           head-to-head goal difference, then head-to-head goals for.
  "gd"   — overall goal difference.
  "gf"   — overall goals for.

Spain (La Liga) and Italy (Serie A) apply head-to-head *before* overall goal
difference; England, Germany and France use overall goal difference first; MLS
uses wins before goal difference. The final, always-deterministic fallback is
ascending team id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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
    ucl_slots: int                 # top-N qualify for the primary continental cup
    relegation_slots: int          # bottom-R are relegated (0 = no relegation)
    tiebreakers: tuple[str, ...]   # ordered chain from the vocabulary above
    europa_slots: int = 0          # informational second band shown in the report
    calendar_year: bool = False    # True for calendar-year leagues (MLS); else split-year
    fbref_comp_id: int = 0         # fbref.com competition id, e.g. 9 (Premier League)
    fbref_slug: str = ""           # fbref URL slug, e.g. "Premier-League"
    fixturedownload_slug: str = "" # fixturedownload.com feed slug, e.g. "epl"
    # soccerdata custom-league registration (only for leagues not built into
    # soccerdata's FBref list — the Big 5 are built in and leave these blank) --
    fbref_name: str = ""           # FBref competition display name, e.g. "Major League Soccer"
    season_start: str = ""         # first month of the season, e.g. "Feb" (soccerdata hint)
    season_end: str = ""           # last month of the season, e.g. "Dec"
    # Report display labels (European defaults; leagues like MLS override) ----
    title_label: str = "Title"     # header for the finish-1st column
    qual_label: str = "UCL"        # header for the top-`ucl_slots` band
    qual2_label: str = "Europe"    # header for the top-`ucl_slots+europa_slots` band
    drop_label: str = "Rel"        # header for the bottom-`relegation_slots` band
    qual_name: str = "Champions League"  # prose name of the qualification band
    drop_name: str = "relegation"        # prose name of the drop band

    @property
    def total_matches(self) -> int:
        return self.n_teams * (self.n_teams - 1)

    def season_start_year(self, today: date | None = None) -> int:
        """Start year of the season current *today* for this league.

        Calendar-year leagues (MLS) use the current year. Split-year (European)
        leagues roll over to the new season in July — it kicks off in August —
        so Jan–Jun still resolves to the season that started the previous year.
        """
        today = today or date.today()
        if self.calendar_year:
            return today.year
        return today.year if today.month >= 7 else today.year - 1

    def season_for(self, source: str, today: date | None = None) -> str:
        """The current season string for this league in the given source's format."""
        y = self.season_start_year(today)
        if self.calendar_year:
            return str(y)                        # MLS: same across sources (e.g. 2026)
        if source == "openfootball":
            return f"{y}-{(y + 1) % 100:02d}"    # e.g. 2026-27
        if source == "fixturedownload":
            return str(y)                        # feed uses the start year, e.g. 2026
        return f"{y}-{y + 1}"                    # fbref / fbref-http, e.g. 2026-2027


# The current big-five European leagues. Slot counts reflect the 2025-26
# allocation and are intentionally easy to edit as UEFA coefficients shift.
LEAGUES: dict[str, LeagueConfig] = {
    "eng": LeagueConfig(
        key="eng", name="Premier League", country="England", n_teams=20,
        openfootball_path="en.1", fbref_league="ENG-Premier League",
        fbref_comp_id=9, fbref_slug="Premier-League", fixturedownload_slug="epl",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
    "esp": LeagueConfig(
        key="esp", name="La Liga", country="Spain", n_teams=20,
        openfootball_path="es.1", fbref_league="ESP-La Liga",
        fbref_comp_id=12, fbref_slug="La-Liga", fixturedownload_slug="la-liga",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "h2h", "gd", "gf"),
    ),
    "ita": LeagueConfig(
        key="ita", name="Serie A", country="Italy", n_teams=20,
        openfootball_path="it.1", fbref_league="ITA-Serie A",
        fbref_comp_id=11, fbref_slug="Serie-A", fixturedownload_slug="serie-a",
        ucl_slots=5, europa_slots=2, relegation_slots=3,
        tiebreakers=("pts", "h2h", "gd", "gf"),
    ),
    "de": LeagueConfig(
        key="de", name="Bundesliga", country="Germany", n_teams=18,
        openfootball_path="de.1", fbref_league="GER-Bundesliga",
        fbref_comp_id=20, fbref_slug="Bundesliga", fixturedownload_slug="bundesliga",
        ucl_slots=4, europa_slots=2, relegation_slots=2,  # +1 relegation playoff, not modeled
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
    "fr": LeagueConfig(
        key="fr", name="Ligue 1", country="France", n_teams=18,
        openfootball_path="fr.1", fbref_league="FRA-Ligue 1",
        fbref_comp_id=13, fbref_slug="Ligue-1", fixturedownload_slug="ligue-1",
        ucl_slots=4, europa_slots=2, relegation_slots=2,  # +1 relegation playoff, not modeled
        tiebreakers=("pts", "gd", "gf", "h2h"),
    ),
    # Major League Soccer. Modeled as a single 30-team table (Supporters'
    # Shield race + playoff qualification); the two conferences and the MLS Cup
    # playoff bracket are not modeled. The season is a calendar year
    # (calendar_year=True), and MLS breaks ties by wins before GD. `ucl_slots=18`
    # approximates the 18-team playoff field (9 per conference) on a single
    # table; there is no relegation.
    "mls": LeagueConfig(
        key="mls", name="MLS", country="USA", n_teams=30,
        openfootball_path="mls", fbref_league="USA-Major League Soccer",
        fbref_comp_id=22, fbref_slug="Major-League-Soccer", fixturedownload_slug="mls",
        calendar_year=True,
        # MLS isn't a built-in soccerdata FBref league; from_fbref registers it.
        fbref_name="Major League Soccer", season_start="Feb", season_end="Dec",
        ucl_slots=18, europa_slots=0, relegation_slots=0,
        tiebreakers=("pts", "wins", "gd", "gf"),
        title_label="Shield", qual_label="Playoff",
        qual_name="playoff", drop_name="",
    ),
    # National Women's Soccer League. Same US-league treatment as MLS: a single
    # calendar-year table (NWSL Shield race + playoff qualification), no
    # relegation, ties broken by wins before goal difference. `ucl_slots=8`
    # approximates the top-8 playoff field. Data comes from the default
    # fixturedownload feed (no browser needed); FBref (comp 182) adds xG.
    "nwsl": LeagueConfig(
        key="nwsl", name="NWSL", country="USA", n_teams=16,
        openfootball_path="", fbref_league="USA-NWSL",
        fbref_comp_id=182, fbref_slug="NWSL", fixturedownload_slug="nwsl",
        calendar_year=True,
        fbref_name="NWSL", season_start="Mar", season_end="Nov",
        ucl_slots=8, europa_slots=0, relegation_slots=0,
        tiebreakers=("pts", "wins", "gd", "gf"),
        title_label="Shield", qual_label="Playoff",
        qual_name="playoff", drop_name="",
    ),
    # USL Championship. Modeled like MLS as a single calendar-year table
    # (Players' Shield race + playoff qualification); the Eastern/Western
    # conferences and the playoff bracket are not modeled, and the schedule is
    # unbalanced (conference-weighted) — the model still adjusts for opponent
    # strength from the actual fixtures. `ucl_slots=16` approximates the large
    # conference playoff field. No free fixturedownload feed exists, so its data
    # comes from FBref (comp 73) via `--source fbref` (soccerdata + browser).
    "usl": LeagueConfig(
        key="usl", name="USL Championship", country="USA", n_teams=24,
        openfootball_path="", fbref_league="USA-USL Championship",
        fbref_comp_id=73, fbref_slug="USL-Championship", fixturedownload_slug="",
        calendar_year=True,
        fbref_name="USL Championship", season_start="Mar", season_end="Nov",
        ucl_slots=16, europa_slots=0, relegation_slots=0,
        tiebreakers=("pts", "wins", "gd", "gf"),
        title_label="Shield", qual_label="Playoff",
        qual_name="playoff", drop_name="",
    ),
}


def get_league(key: str) -> LeagueConfig:
    try:
        return LEAGUES[key]
    except KeyError:
        valid = ", ".join(LEAGUES)
        raise SystemExit(f"Unknown league '{key}'. Choose one of: {valid}")
