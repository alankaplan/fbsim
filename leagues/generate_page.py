#!/usr/bin/env python3
"""
generate_page.py
----------------
Render a self-contained ``leagues.html`` report from every
``data/leagues/<key>/sim_results.json`` produced by ``run_sims.py``. No server
or dependencies — open the file directly in a browser.

A hash/state-routed single-page app with a sortable/filterable table
(``data-col`` + ``sortCol``/``sortAsc``) and the simulation JSON embedded as a
JS global via ``__PLACEHOLDER__`` replacement.

Views: a league switcher across all simulated leagues, plus
  * Standings odds — projected points, title / Champions-League / any-Europe /
    relegation probabilities (sortable, filterable);
  * Position matrix — a heatmap of P(finish in each position);
  * Fixtures — remaining games with kickoff (US Pacific), W/D/L, and Info%
    (each game's title-race informativeness), sortable;
  * Top games — a cross-league schedule where each league contributes just enough
    of its most decisive games to fall below a shared title-race entropy threshold
    (in bits), plus every remaining game of any teams followed from a dropdown;
  * Top players — a league-wide, sortable table of individual player season stats
    (goals, assists, xG/xA, minutes) from ``players.csv``, when present;
  * Team detail — a Summary sub-tab (finishing-position distribution, full schedule
    with per-game title/finish swing, and an interactive branching tree of how its
    title odds shift along a win/draw/loss path) and a Players sub-tab (that club's
    squad stats).

Usage
-----
    venv/bin/python -m leagues.generate_page              # all simulated leagues
    venv/bin/python -m leagues.generate_page --open
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import webbrowser
from pathlib import Path

from .config import LEAGUES
from .ingest import DATA_ROOT
from .players import _fold, _norm_team, resolve_team_code

OUT = Path(__file__).resolve().parent.parent / "leagues.html"

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>League Season Simulator</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0d1117; color: #e6edf3;
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  a { color: #58a6ff; text-decoration: none; cursor: pointer; }
  a:hover { text-decoration: underline; }
  header { padding: 20px 24px 0; border-bottom: 1px solid #21262d; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 14px; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px;
    background: #21262d; color: #8b949e; margin-left: 6px; }
  .badge.xg { background: #1f6f43; color: #d2f7dd; }
  .leagues { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .lg-btn { padding: 5px 12px; border: 1px solid #30363d; border-radius: 6px;
    background: #161b22; color: #c9d1d9; cursor: pointer; font-size: 13px; }
  .lg-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  .lg-btn.top { margin-left: 12px; }
  .topn-row { display: flex; flex-wrap: wrap; gap: 10px 14px; margin: 12px 0 4px; }
  .topn { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #8b949e; }
  .topn input { width: 52px; background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    border-radius: 6px; padding: 3px 6px; font-size: 13px; }
  .tp-dd { position: relative; display: inline-block; }
  .tp-btn { background: #161b22; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
    padding: 4px 10px; font-size: 13px; cursor: pointer; }
  .tp-panel { position: absolute; z-index: 20; top: 110%; left: 0; width: 260px; max-height: 320px;
    overflow-y: auto; background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.5); }
  .tp-filter { width: 100%; box-sizing: border-box; margin-bottom: 6px; background: #0d1117;
    color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 4px 6px; font-size: 13px; }
  .tp-grp { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
    margin: 8px 2px 2px; }
  .tp-panel label { display: flex; align-items: center; gap: 6px; padding: 2px 4px; font-size: 13px;
    color: #c9d1d9; cursor: pointer; }
  .tp-panel label:hover { background: #21262d; border-radius: 4px; }
  td .sel { color: #f0b429; font-weight: 600; }
  nav { display: flex; gap: 4px; }
  nav a { padding: 8px 14px; color: #8b949e; border-bottom: 2px solid transparent; font-size: 13px; }
  nav a.active { color: #e6edf3; border-bottom-color: #f78166; }
  main { padding: 20px 24px 60px; max-width: 1200px; }
  .wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { padding: 6px 10px; text-align: right; white-space: nowrap; }
  th:nth-child(2), td:nth-child(2) { text-align: left; }
  thead th { position: sticky; top: 0; background: #0d1117; border-bottom: 1px solid #30363d;
    cursor: pointer; user-select: none; font-size: 12px; color: #8b949e; }
  th[data-col].sort-asc::after { content: " ↑"; color: #f78166; }
  th[data-col].sort-desc::after { content: " ↓"; color: #f78166; }
  tbody tr { border-bottom: 1px solid #161b22; }
  tbody tr:hover { background: #161b22; }
  .pos { color: #8b949e; }
  .zero { color: #484f58; }
  .bar-track { display: inline-block; width: 70px; height: 8px; background: #21262d;
    border-radius: 4px; overflow: hidden; vertical-align: middle; margin-right: 6px; }
  .bar-fill { display: block; height: 100%; background: #f78166; }
  .ucl { box-shadow: inset 3px 0 0 #1f6feb; }
  .eur { box-shadow: inset 3px 0 0 #8957e5; }
  .rel { box-shadow: inset 3px 0 0 #da3633; }
  input[type=search] { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    color: #e6edf3; padding: 6px 10px; width: 220px; margin-bottom: 12px; }
  .matrix td, .matrix th { padding: 3px 4px; font-size: 11px; text-align: center; }
  .matrix td.name { text-align: left; white-space: nowrap; padding-right: 10px; }
  .cell { border-radius: 2px; }
  .wdl { display: inline-flex; width: 160px; height: 14px; border-radius: 3px; overflow: hidden;
    vertical-align: middle; }
  .wdl span { display: block; height: 100%; }
  .w { background: #238636; } .d { background: #6e7681; } .l { background: #da3633; }
  .legend { color: #8b949e; font-size: 12px; margin: 4px 0 14px; }
  .legend b { color: #c9d1d9; font-weight: 600; }
  .dist { display: flex; align-items: flex-end; gap: 3px; height: 160px; margin: 16px 0;
    border-bottom: 1px solid #30363d; }
  .dist .col { flex: 1; background: #1f6feb; border-radius: 2px 2px 0 0; min-height: 1px; position: relative; }
  .dist .col:hover { background: #58a6ff; }
  .dist .lbl { color: #8b949e; font-size: 10px; text-align: center; }
  .kpis { display: flex; flex-wrap: wrap; gap: 20px; margin: 8px 0 16px; }
  .kpi { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 10px 16px; }
  .kpi .v { font-size: 22px; font-weight: 600; }
  .kpi .k { color: #8b949e; font-size: 12px; }
  .sec-h { font-size: 15px; font-weight: 600; margin: 22px 0 2px; }
  .subtabs { display: flex; gap: 4px; margin: 8px 0 16px; border-bottom: 1px solid #21262d; }
  .subtabs a { padding: 6px 14px; color: #8b949e; cursor: pointer; font-size: 13px;
    border-bottom: 2px solid transparent; }
  .subtabs a.active { color: #e6edf3; border-bottom-color: #f78166; }
  code { background: #161b22; border: 1px solid #21262d; border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  .tsched td, .tsched th { text-align: left; }
  .tsched tr.now td { border-top: 2px solid #f78166; }
  .res { font-weight: 600; }
  .res.w { color: #3fb950; } .res.d { color: #8b949e; } .res.l { color: #f85149; }
  .swing b { font-weight: 600; margin-right: 10px; font-variant-numeric: tabular-nums; }
  .sw-w { color: #3fb950; } .sw-d { color: #8b949e; } .sw-l { color: #f85149; }
  .crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 8px 0 14px; }
  .crumb { cursor: pointer; padding: 2px 9px; border: 1px solid #30363d; border-radius: 14px;
    background: #161b22; font-size: 12px; color: #c9d1d9; }
  .crumb:hover { border-color: #58a6ff; }
  .crumb b { color: #e6edf3; }
  .crumb-sep { color: #8b949e; }
  .reset { margin-left: 4px; font-size: 12px; }
  .next-game { color: #8b949e; font-size: 13px; margin-bottom: 8px; }
  .next-game b { color: #e6edf3; }
  .branches { display: flex; gap: 12px; flex-wrap: wrap; max-width: 620px; }
  .branch { flex: 1; min-width: 150px; background: #161b22; border: 1px solid #30363d;
    border-radius: 10px; padding: 12px 14px; cursor: pointer; }
  .branch:hover { border-color: #58a6ff; }
  .branch.win { box-shadow: inset 0 3px 0 #238636; }
  .branch.draw { box-shadow: inset 0 3px 0 #6e7681; }
  .branch.loss { box-shadow: inset 0 3px 0 #da3633; }
  .branch.leaf, .branch.low { cursor: default; }
  .branch.leaf:hover, .branch.low:hover { border-color: #30363d; }
  .branch.low { opacity: .5; }
  .branch .b-h { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: .04em; }
  .branch .b-v { font-size: 24px; font-weight: 600; margin: 2px 0; }
  .branch .b-d { font-size: 12px; font-weight: 600; }
  .branch .b-d.good { color: #3fb950; } .branch .b-d.bad { color: #f85149; }
  .branch .b-s { font-size: 11px; color: #6e7681; margin-top: 4px; }
  /* Top-games game cards (phones only). */
  .gcard { border-bottom: 1px solid #21262d; padding: 10px 2px; }
  .gc-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .gc-when { color: #e6edf3; }
  .gc-lg { color: #8b949e; font-size: 11px; text-align: right; }
  .gc-teams { margin: 5px 0; font-size: 14px; }
  .gc-teams .vs { color: #6e7681; font-size: 12px; margin: 0 6px; }
  .gc-odds { display: flex; align-items: center; gap: 8px; color: #8b949e; font-size: 12px; }
  .gc-odds .wdl { width: 130px; }
  /* Clickable player name + expandable player card (modal). */
  .pl-link { color: #e6edf3; cursor: pointer; border-bottom: 1px dotted #484f58; }
  .pl-link:hover { color: #58a6ff; border-bottom-color: #58a6ff; text-decoration: none; }
  .pcard-back { position: fixed; inset: 0; background: rgba(1,4,9,.72); z-index: 100;
    display: flex; align-items: center; justify-content: center; padding: 16px; }
  .pcard { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    width: min(440px, 94vw); max-height: 88vh; overflow-y: auto;
    box-shadow: 0 12px 40px rgba(0,0,0,.6); }
  .pcard-h { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;
    padding: 16px 18px 12px; border-bottom: 1px solid #21262d; }
  .pc-id { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .pc-photo { width: 56px; height: 56px; border-radius: 50%; flex: 0 0 auto;
    object-fit: cover; background: #0d1117; border: 1px solid #30363d; }
  .pc-initials { display: flex; align-items: center; justify-content: center;
    color: #8b949e; font-size: 18px; font-weight: 600; letter-spacing: .02em; }
  .pcard-h .nm { font-size: 19px; font-weight: 600; }
  .pcard-h .mt { color: #8b949e; font-size: 12px; margin-top: 3px; }
  .pcard-x { cursor: pointer; color: #8b949e; font-size: 22px; line-height: 1;
    background: none; border: none; padding: 0 2px; }
  .pcard-x:hover { color: #e6edf3; }
  .pcard-body { padding: 12px 18px 18px; }
  .pcard-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .pstat { background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 9px 8px; text-align: center; }
  .pstat .v { font-size: 18px; font-weight: 600; }
  .pstat .k { color: #8b949e; font-size: 11px; margin-top: 2px; }
  .pcard-sub { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
    margin: 14px 2px 6px; }
  .pcard-note { color: #8b949e; font-size: 12px; margin-top: 12px; }
  .pc-n { border-top: 1px solid #21262d; padding: 9px 0 2px; }
  .pc-n:first-of-type { border-top: none; }
  .pc-n .t { color: #c9d1d9; font-size: 13px; }
  .pc-n .q { color: #8b949e; font-size: 12px; font-style: italic; margin-top: 4px;
    border-left: 2px solid #30363d; padding-left: 8px; }
  .pc-n .s { color: #6e7681; font-size: 11px; margin-top: 4px; }
  .pc-tag { display: inline-block; font-size: 10px; text-transform: uppercase;
    letter-spacing: .04em; padding: 1px 6px; border-radius: 9px; margin-right: 5px;
    background: #21262d; color: #8b949e; vertical-align: 1px; }
  .pc-tag.injury, .pc-tag.absence { background: #4a1d1d; color: #f8b4b4; }
  .pc-tag.breakout, .pc-tag.form { background: #10331f; color: #7ee2a8; }
  .pc-tag.error, .pc-tag.weakness { background: #45260a; color: #f0b429; }
  .pc-tag.transfer { background: #12283f; color: #79c0ff; }
  .pc-tag.retirement { background: #2b1f45; color: #c4a5f5; }
  /* Phone-friendly: tighten spacing, wrap control rows, drop secondary columns. */
  @media (max-width: 640px) {
    header { padding: 14px 14px 0; }
    main { padding: 14px 14px 48px; }
    h1 { font-size: 17px; }
    body { font-size: 13px; }
    nav { flex-wrap: wrap; gap: 2px; }
    nav a { padding: 7px 10px; }
    .lg-btn.top { margin-left: 0; }
    table { font-size: 12px; }
    th, td { padding: 4px 6px; }
    td { white-space: normal; }          /* let long team/player names wrap instead of scroll */
    .col-sec { display: none; }
    .tp-panel { width: min(260px, 86vw); }
    input[type=search] { width: 100%; }
    .bar-track { display: none; }        /* Title% bar: show the number alone on phones */
  }
</style>
</head>
<body>
<header>
  <h1>League Season Simulator <span id="hdr-badge"></span></h1>
  <div class="sub" id="hdr-sub"></div>
  <div class="leagues" id="league-tabs"></div>
  <nav id="nav">
    <a data-view="main" class="active">Standings odds</a>
    <a data-view="matrix">Position matrix</a>
    <a data-view="fixtures">Fixtures</a>
    <a data-view="players">Top players</a>
    <a data-view="team" id="nav-team" style="display:none">Team</a>
  </nav>
</header>
<main>
  <div id="main-view"></div>
  <div id="matrix-view" style="display:none"></div>
  <div id="fixtures-view" style="display:none"></div>
  <div id="schedule-view" style="display:none"></div>
  <div id="players-view" style="display:none"></div>
  <div id="players_all-view" style="display:none"></div>
  <div id="national-view" style="display:none"></div>
  <div id="competitions-view" style="display:none"></div>
  <div id="team-view" style="display:none"></div>
</main>
<script>
const LEAGUES = __DATA_PLACEHOLDER__;
const NATIONAL = __NATIONAL_PLACEHOLDER__;
const COMPETITIONS = __COMPETITIONS_PLACEHOLDER__;
(function () {
  const keys = Object.keys(LEAGUES);
  let cur = keys[0];
  let view = "main";
  let sortCol = "exp_rank", sortAsc = true;
  let fixtSortCol = "kickoff", fixtSortAsc = true;
  let schedSortCol = "kickoff", schedSortAsc = true;
  // Top games: one champion-entropy threshold (bits) for all leagues; each league
  // shows just enough of its most-decisive games to fall below it. Default ~ the
  // median league baseline, floored, and remembered across visits (localStorage).
  const round1 = (x) => Math.round(x * 10) / 10;
  let threshold;
  {
    const h0 = keys.map(k => (LEAGUES[k].meta.champ_entropy_bits ?? 0)).sort((a, b) => a - b);
    const med = h0.length ? h0[Math.floor((h0.length - 1) / 2)] : 1;
    threshold = Math.max(0.5, round1(med));
    try {
      const s = parseFloat(localStorage.getItem("fbsim.entropyThreshold"));
      if (isFinite(s) && s >= 0) threshold = s;
    } catch (e) {}
  }
  function saveThreshold() {
    try { localStorage.setItem("fbsim.entropyThreshold", String(threshold)); } catch (e) {}
  }
  const teamKey = (k, name) => k + "\t" + name;
  let selectedTeams = new Set();                   // "<leagueKey>\t<team name>" (schedule view)
  try {                                            // restore picked teams (localStorage; file:// ok)
    const valid = new Set();
    keys.forEach(k => (LEAGUES[k].teams || []).forEach(t => valid.add(teamKey(k, t.name))));
    (NATIONAL || []).forEach(t => valid.add(teamKey("__national__", t.name)));
    JSON.parse(localStorage.getItem("fbsim.topTeams") || "[]")
      .forEach(x => { if (valid.has(x)) selectedTeams.add(x); });
  } catch (e) {}
  function saveTeams() {
    try { localStorage.setItem("fbsim.topTeams", JSON.stringify([...selectedTeams])); } catch (e) {}
  }
  const compKey = (k, i) => k + "\t" + i;            // "<compKey>\t<roundIndex>" (schedule view)
  let selectedRounds = new Set();
  try {                                              // restore picked competition stages
    const valid = new Set();
    (COMPETITIONS || []).forEach(c => (c.rounds || []).forEach((r, i) => valid.add(compKey(c.key, i))));
    JSON.parse(localStorage.getItem("fbsim.topRounds") || "[]")
      .forEach(x => { if (valid.has(x)) selectedRounds.add(x); });
  } catch (e) {}
  function saveRounds() {
    try { localStorage.setItem("fbsim.topRounds", JSON.stringify([...selectedRounds])); } catch (e) {}
  }
  let teamDDOpen = false, teamFilter = "";
  let compDDOpen = false;
  let filter = "";
  let teamCode = null;
  let treePath = [], treeTeam = null;              // odds-tree drill-down state
  let teamTab = "summary";                         // team page sub-tab: summary | players
  let plSort = "goals", plAsc = false;             // players-table sort
  let taSort = "ga90", taAsc = false;              // cross-league Top Players sort

  const $ = (id) => document.getElementById(id);
  const pct = (x) => (x === 0 ? '<span class="zero">0</span>' : x.toFixed(1));
  const fmtBits = (x) => String(+(+x).toFixed(2));   // trims zeros: 0.02, 0.5, 1.91
  const esc = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  // Sequential blue heatmap for probabilities 0..pmax.
  function heat(p, pmax) {
    if (p <= 0) return "transparent";
    const t = Math.min(1, p / pmax);
    const a = 0.08 + 0.82 * Math.sqrt(t);
    return `rgba(88,166,255,${a.toFixed(3)})`;
  }

  const anyPlayers = () => Object.keys(LEAGUES).some(k => (LEAGUES[k].players || []).length);
  function leagueTabs() {
    // Top games / National / Competitions / Top players are cross-league (no single league applies).
    const crossLeague = (view==='schedule' || view==='national' || view==='competitions' || view==='players_all');
    const lg = keys.map(k =>
      `<button class="lg-btn ${(!crossLeague&&k===cur)?'active':''}" data-k="${k}">${esc(LEAGUES[k].league.name)}</button>`
    ).join("");
    const top = `<button class="lg-btn top ${view==='schedule'?'active':''}" data-top="1">Top games</button>`;
    const pl = anyPlayers()
      ? `<button class="lg-btn top ${view==='players_all'?'active':''}" data-players="1">Top Players</button>` : "";
    const nat = (NATIONAL && NATIONAL.length)
      ? `<button class="lg-btn top ${view==='national'?'active':''}" data-nat="1">National teams</button>` : "";
    const cup = (COMPETITIONS && COMPETITIONS.length)
      ? `<button class="lg-btn top ${view==='competitions'?'active':''}" data-cup="1">Competitions</button>` : "";
    $("league-tabs").innerHTML = lg + top + pl + nat + cup;
    $("league-tabs").querySelectorAll("button").forEach(b =>
      b.onclick = () => {
        if (b.dataset.top) { setView("schedule"); return; }
        if (b.dataset.players) { setView("players_all"); return; }
        if (b.dataset.nat) { setView("national"); return; }
        if (b.dataset.cup) { setView("competitions"); return; }
        cur = b.dataset.k; teamCode = null;
        setView((crossLeague||view==='team') ? 'main' : view);
      });
  }

  function head() {
    if (view === "schedule") {  // cross-league view: no single league applies
      $("hdr-badge").innerHTML = "";
      $("hdr-sub").textContent =
        `Top games across ${keys.length} leagues · kickoff in US Pacific time`;
      return;
    }
    if (view === "national") {  // national teams: results + schedule, no simulation
      $("hdr-badge").innerHTML = "";
      $("hdr-sub").textContent =
        `National-team results and upcoming games · kickoff in US Pacific time`;
      return;
    }
    if (view === "competitions") {  // cup competitions: standings + results, no simulation
      $("hdr-badge").innerHTML = "";
      $("hdr-sub").textContent =
        `Cup standings, results and upcoming fixtures · kickoff in US Pacific time`;
      return;
    }
    if (view === "players_all") {  // cross-league top players
      $("hdr-badge").innerHTML = "";
      $("hdr-sub").textContent = `Top 100 players across all leagues · by goals + assists`;
      return;
    }
    const L = LEAGUES[cur], m = L.meta;
    $("hdr-badge").innerHTML = m.used_xg
      ? '<span class="badge xg">FBref xG</span>' : '<span class="badge">goals model</span>';
    const asOf = m.as_of ? ` · forecast from matchday ${m.as_of}` : "";
    const ent = (m.champ_entropy_bits == null) ? ""
      : ` · ${L.league.title_label} race entropy ${fmtBits(m.champ_entropy_bits)} bits`
        + (m.n_remaining === 0 ? " (decided)" : "");
    $("hdr-sub").textContent =
      `${L.league.name} (${L.league.country}) · ${m.n_played} played, ${m.n_remaining} remaining · `
      + `${m.n_sims.toLocaleString()} simulations${asOf}${ent}`;
  }

  function setView(v) {
    view = v;
    ["main","matrix","fixtures","schedule","players","players_all","national","competitions","team"].forEach(x =>
      $(x+"-view").style.display = (x===v ? "" : "none"));
    $("nav").querySelectorAll("a").forEach(a =>
      a.classList.toggle("active", a.dataset.view===v));
    $("nav-team").style.display = (v==="team") ? "" : "none";
    // cross-cutting views (Top games / Top players / National / Competitions) skip the per-league tabs
    $("nav").style.display = (v==="schedule"||v==="players_all"||v==="national"||v==="competitions") ? "none" : "";
    head(); leagueTabs();
    if (v==="main") renderMain();
    else if (v==="matrix") renderMatrix();
    else if (v==="fixtures") renderFixtures();
    else if (v==="schedule") renderSchedule();
    else if (v==="players") renderPlayers();
    else if (v==="players_all") renderTopPlayers();
    else if (v==="national") renderNational();
    else if (v==="competitions") renderCompetitions();
    else if (v==="team") renderTeam();
  }

  // ---- National teams (display-only results + schedule; no simulation) ----
  function renderNational() {
    const venLabel = { home: "H", away: "A", neutral: "N" };
    const cards = NATIONAL.map(t => {
      const games = (t.games || []);
      const firstFut = games.findIndex(g => g.status !== "completed");
      const rows = games.map((g, i) => {
        const nowCls = i === firstFut ? ' class="now"' : '';
        const opp = `${esc(g.opponent)} <span class="pos">(${venLabel[g.venue] || "?"})</span>`;
        const when = (g.status === "completed") ? esc(g.date || "") : esc(kickoff(g));
        const res = (g.status === "completed")
          ? `<span class="res ${String(g.result).toLowerCase()}">${g.gf}–${g.ga} ${g.result}</span>` : "";
        return `<tr${nowCls}><td>${when}</td><td>${opp}</td>`
          + `<td>${esc(g.competition || "")}</td><td>${res}</td></tr>`;
      }).join("");
      const body = games.length ? rows
        : `<tr><td colspan="4" class="pos">No games — run <code>python -m leagues.national ${esc(t.key)}</code>.</td></tr>`;
      return `<div class="sec-h">${esc(t.name)}</div>
        <div class="wrap"><table class="tsched"><thead><tr>
          <th>When</th><th>Opponent</th><th>Competition</th><th>Result</th>
        </tr></thead><tbody>${body}</tbody></table></div>`;
    }).join("");
    $("national-view").innerHTML =
      `<div class="legend">Results and upcoming games for the US national teams, across all
        competitions (Wikipedia). These are shown for reference only — national teams aren't
        simulated. The <span style="color:#f78166">orange line</span> marks the next game.</div>`
      + cards;
  }

  // ---- Competitions (display-only cup standings + results; no simulation) ----
  function renderCompetitions() {
    function standingsTable(grp) {
      const rows = (grp.rows || []).map((r, i) =>
        `<tr><td class="pos">${esc(r.pos || (i + 1))}</td><td>${esc(r.team || "")}</td>`
        + `<td>${r.pld ?? ""}</td>`
        + `<td class="col-sec">${r.w ?? ""}</td><td class="col-sec">${r.d ?? ""}</td><td class="col-sec">${r.l ?? ""}</td>`
        + `<td class="col-sec">${r.gf ?? ""}</td><td class="col-sec">${r.ga ?? ""}</td><td class="col-sec">${r.gd ?? ""}</td>`
        + `<td><strong>${r.pts ?? ""}</strong></td></tr>`).join("");
      const cap = grp.title ? `<div class="sec-h">${esc(grp.title)}</div>` : "";
      return cap + `<div class="wrap"><table><thead><tr>
        <th>#</th><th>Team</th><th>Pld</th>
        <th class="col-sec">W</th><th class="col-sec">D</th><th class="col-sec">L</th>
        <th class="col-sec">GF</th><th class="col-sec">GA</th><th class="col-sec">GD</th>
        <th>Pts</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    function roundTable(rd) {
      const rows = (rd.matches || []).map(m => {
        const done = m.status === "completed";
        const when = done ? esc(m.date || "") : esc(kickoff(m));
        let score;
        if (done) {
          const cls = m.hs > m.as ? "w" : (m.hs < m.as ? "l" : "d");
          score = `<span class="res ${cls}">${m.hs}–${m.as}</span>`;
        } else { score = '<span class="pos">v</span>'; }
        return `<tr><td>${when}</td><td>${esc(m.home || "")}</td>`
          + `<td>${score}</td><td>${esc(m.away || "")}</td></tr>`;
      }).join("");
      return `<div class="sec-h">${esc(rd.name || "Fixtures")}</div>
        <div class="wrap"><table class="tsched"><thead><tr>
          <th>Date</th><th>Home</th><th>Score</th><th>Away</th>
        </tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    const cards = COMPETITIONS.map(c => {
      const head = `<div class="sec-h" style="font-size:17px">${esc(c.name)}`
        + (c.season ? ` <span class="pos">${esc(c.season)}</span>` : "") + `</div>`;
      const st = (c.standings || []).map(standingsTable).join("");
      const rounds = (c.rounds || []).map(roundTable).join("");
      const body = (st || rounds) ? (st + rounds)
        : `<div class="pos">No data — run <code>python -m leagues.competitions ${esc(c.key)}</code>.</div>`;
      return head + body;
    }).join("");
    $("competitions-view").innerHTML =
      `<div class="legend">Standings, results and upcoming fixtures for cup competitions
        (Wikipedia). Shown for reference only — these aren't simulated. Kickoff/dates in US
        Pacific time.</div>` + cards;
  }

  // ---- Players (league-wide "Top players" + per-team squad) ----
  // [key, label, align, cell(p), sortVal(p), teamViewHidden?, secondary?]
  function playerCols() {
    return [
      ["player_name","Player","left", p=>`<a class="pl-link">${esc(p.player_name)}</a>`, p=>p.player_name],
      ["team","Team","left", p=>`<a data-team="${p.team_code}">${esc(p.team_code||p.team_name)}</a>`, p=>p.team_name, true],
      ["position","Pos","left", p=>esc(p.position||""),                      p=>p.position||"",  false, true],
      ["matches","Apps","right", p=>p.matches,                               p=>p.matches,       false, true],
      ["minutes","Min","right", p=>p.minutes,                                p=>p.minutes,       false, true],
      ["goals","G","right", p=>p.goals,                                      p=>p.goals],
      ["assists","A","right", p=>p.assists,                                  p=>p.assists],
      ["pct","Lg%","right", p=>p.pct==null?"":p.pct.toFixed(1),               p=>p.pct==null?-1:p.pct],
      ["tough","Tough z","right", p=>p.tough==null?"":(p.tough>=0?"+":"")+p.tough.toFixed(2), p=>p.tough==null?-99:p.tough],
      ["xg","xG","right", p=>p.xg.toFixed(1),                                p=>p.xg,            false, true],
      ["xa","xA","right", p=>p.xa.toFixed(1),                                p=>p.xa,    false, true],
      ["shots","Sh","right", p=>(p.shots||""),                               p=>p.shots, false, true],
    ];
  }
  function renderPlayersTable(elId, players, teamView) {
    const el = $(elId); if (!el) return;
    if (!players.length) {
      el.innerHTML = `<div class="legend">No player data for ${esc(LEAGUES[cur].league.name)} yet — `
        + `run <code>python -m leagues.players ${cur}</code> to fetch it.</div>`;
      return;
    }
    const cols = playerCols().filter(c => !(teamView && c[5]));
    const sc = cols.find(c => c[0]===plSort) || cols[0];
    const sorted = players.slice().sort((a,b) => {
      const va = sc[4](a), vb = sc[4](b);
      const r = (typeof va==="number" && typeof vb==="number")
        ? va - vb : String(va).localeCompare(String(vb));
      return plAsc ? r : -r;
    });
    const th = cols.map(c =>
      `<th data-col="${c[0]}" style="text-align:${c[2]}" class="${c[6]?'col-sec ':''}${c[0]===plSort?(plAsc?'sort-asc':'sort-desc'):''}">${c[1]}</th>`).join("");
    const body = sorted.map(p =>
      `<tr>${cols.map(c => `<td class="${c[6]?'col-sec':''}" style="text-align:${c[2]}">${c[3](p)}</td>`).join("")}</tr>`).join("");
    el.innerHTML = `<div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    el.querySelectorAll("th[data-col]").forEach(h => h.onclick = () => {
      const c = h.dataset.col;
      if (c===plSort) plAsc = !plAsc; else { plSort = c; plAsc = false; }
      renderPlayersTable(elId, players, teamView);
    });
    el.querySelectorAll("a[data-team]").forEach(a => a.onclick = () => {
      teamCode = a.dataset.team; teamTab = "summary"; setView("team");
    });
    el.querySelectorAll("tbody .pl-link").forEach((a, i) => a.onclick = () => showPlayerCard(sorted[i]));
  }
  function renderPlayers() {
    const L = LEAGUES[cur];
    $("players-view").innerHTML =
      `<div class="sec-h">Top players — ${esc(L.league.name)}</div>
       <div class="legend">Season totals${L.meta.used_xg ? " (with xG)" : ""}. Click a player for their
         card, a header to sort, a team to open it.</div>
       <div id="players-table"></div>`;
    renderPlayersTable("players-table", leagueMetrics(cur), false);
  }

  // ---- Player card (click a player name in any table to expand this) ----
  // Display-only: every value comes from the player's existing stat row — no model,
  // no fetch. Built to also host qualitative notes later without reworking the shell.
  function _pv(v) { return (v === null || v === undefined || v === "") ? 0 : +v || 0; }
  function _pcardEsc(e) { if (e.key === "Escape") closePlayerCard(); }
  function closePlayerCard() {
    const b = document.querySelector(".pcard-back");
    if (b) b.remove();
    document.removeEventListener("keydown", _pcardEsc);
  }
  function showPlayerCard(p) {
    if (!p) return;
    closePlayerCard();
    const mins = _pv(p.minutes), g = _pv(p.goals), a = _pv(p.assists);
    const xg = _pv(p.xg), xa = _pv(p.xa), sh = _pv(p.shots), ga = g + a;
    const lk = p._lk || cur;
    const leagueName = p._league || (LEAGUES[lk] ? LEAGUES[lk].league.name : "");
    const teamName = p.team_name || p.team_code || "";
    const teamLink = p.team_code
      ? `<a class="pl-team" data-team="${p.team_code}" data-lk="${lk}">${esc(teamName)}</a>`
      : esc(teamName);
    const per90 = v => mins > 0 ? (v * 90 / mins).toFixed(2) : "—";
    const tile = (v, k) => `<div class="pstat"><div class="v">${v}</div><div class="k">${k}</div></div>`;
    const sub = [esc(p.position || "—"), teamLink, esc(leagueName)].filter(Boolean).join(" · ");
    // Headshot, when one was cached for this player. Remote URL, so it simply doesn't load
    // offline — fall back to initials on error rather than showing a broken-image icon.
    const initials = String(p.player_name || "?").split(/\s+/).filter(Boolean)
      .slice(0, 2).map(w => w[0]).join("").toUpperCase();
    const avatar = p.img
      ? `<img class="pc-photo" src="${esc(p.img)}" alt="">`
      : `<div class="pc-photo pc-initials">${esc(initials)}</div>`;
    const hasXg = xg > 0 || xa > 0 || sh > 0;
    const sections = [
      `<div class="pcard-grid">${tile(g, "Goals")}${tile(a, "Assists")}${tile("<b>" + ga + "</b>", "G+A")}</div>`,
    ];
    if (hasXg) sections.push(
      `<div class="pcard-sub">Expected</div>
       <div class="pcard-grid">${tile(xg.toFixed(1), "xG")}${tile(xa.toFixed(1), "xA")}${tile(sh || "—", "Shots")}</div>`);
    sections.push(
      `<div class="pcard-sub">Playing time</div>
       <div class="pcard-grid">${tile(_pv(p.matches), "Apps")}${tile(mins, "Min")}${tile(_pv(p.yellow_cards) + " / " + _pv(p.red_cards), "Y / R")}</div>`);
    if (mins > 0) sections.push(
      `<div class="pcard-sub">Per 90</div>
       <div class="pcard-grid">${tile(per90(g), "G/90")}${tile(per90(a), "A/90")}${tile(per90(ga), "G+A/90")}</div>`);
    // Curated briefing notes. Display-only context: it never touches the model, and each note
    // carries its date and source (plus the verbatim quote when there is one) so the reader can
    // weigh it rather than take it as fact.
    if ((p.notes || []).length) sections.push(
      `<div class="pcard-sub">Notes</div>` + p.notes.map(n => {
        const tags = (n.tags || []).map(t =>
          `<span class="pc-tag ${esc(t)}">${esc(t)}</span>`).join("");
        const q = n.quote ? `<div class="q">&ldquo;${esc(n.quote)}&rdquo;</div>` : "";
        const meta = [n.src, n.date, n.conf && n.conf !== "high" ? n.conf + " confidence" : ""]
          .filter(Boolean).join(" &middot; ");
        return `<div class="pc-n"><div class="t">${tags}${esc(n.note)}</div>${q}` +
               (meta ? `<div class="s">${meta}</div>` : "") + `</div>`;
      }).join(""));
    if (p.img && (p.img_by || p.img_lic))          // CC-BY-SA: attribution is required
      sections.push(`<div class="pcard-note">Photo: ${p.img_page
        ? `<a href="${esc(p.img_page)}" target="_blank" rel="noopener">${esc(p.img_by || "Wikipedia")}</a>`
        : esc(p.img_by || "Wikipedia")}${p.img_lic ? " &middot; " + esc(p.img_lic) : ""}</div>`);
    if (xg > 0) sections.push(
      `<div class="pcard-note">Finishing: <b>${g - xg >= 0 ? "+" : ""}${(g - xg).toFixed(1)}</b> vs xG</div>`);
    const back = document.createElement("div");
    back.className = "pcard-back";
    back.innerHTML =
      `<div class="pcard">
         <div class="pcard-h">
           <div class="pc-id">${avatar}
             <div><div class="nm">${esc(p.player_name)}</div><div class="mt">${sub}</div></div>
           </div>
           <button class="pcard-x" title="Close">×</button>
         </div>
         <div class="pcard-body">${sections.join("")}</div>
       </div>`;
    back.onclick = e => { if (e.target === back) closePlayerCard(); };
    document.body.appendChild(back);
    document.addEventListener("keydown", _pcardEsc);
    back.querySelector(".pcard-x").onclick = closePlayerCard;
    const photo = back.querySelector("img.pc-photo");
    if (photo) photo.onerror = () => {                // offline / dead URL -> initials
      const d = document.createElement("div");
      d.className = "pc-photo pc-initials";
      d.textContent = initials;
      photo.replaceWith(d);
    };
    const tl = back.querySelector(".pl-team");
    if (tl) tl.onclick = () => {
      closePlayerCard();
      cur = tl.dataset.lk; teamCode = tl.dataset.team; teamTab = "summary"; setView("team");
    };
  }

  // ---- Top Players (cross-league: top 100 currently playing, by G+A per 90) ----
  // Ranking by raw cumulative G+A floods the list with whichever league has played
  // the most games (e.g. MLS, a calendar-year season, mid-summer vs a just-started
  // European one). Instead rank by G+A per 90 minutes, and gate eligibility on a
  // minutes floor set *relative to each league's own progress* (a fraction of that
  // league's busiest player) so a "regular" in a 3-game-old league qualifies just
  // like one in a 25-game-old league — an absolute floor would re-bias to the deeper.
  const TP_REL = 0.4;                                // must play >= 40% of a league regular's minutes
  const TP_ABS = 90;                                 // ...and at least one full match, to steady per-90
  const TP_K = 6;                                     // empirical-Bayes prior strength (~pseudo-matches)
  // PLACEHOLDER league strength for the cross-league "Tough z" column, as a handicap in standard
  // deviations (0 = the toughest league). Applied as a SUBTRACTION rather than a percentile scale:
  // percentiles compress the tails, so scaling them leaves a weak league's stars indistinguishable
  // and caps them below their league's score forever. An SD handicap has no ceiling, so a dominant
  // player in a weaker league can still outrank a good one in a stronger league.
  // Eyeballed guesses, to be replaced with a real measure (UEFA coefficients, cross-league cup
  // results, ...). NOTE nwsl is a women's league — putting it on the men's axis is
  // apples-to-oranges; its number is a placeholder, not a claim.
  const TP_LEAGUE_HANDICAP = { eng: 0, esp: 0.15, ita: 0.25, de: 0.3, fr: 0.5, mls: 1.2, nwsl: 1.4 };
  // Per-league player metrics (Lg% + Tough z), computed over the WHOLE league and memoised.
  // Kept league-wide on purpose: a club's squad view must still rank its players against the
  // league, not against team-mates. Memoised because the percentile step is O(N^2) and every
  // sort click re-renders. Returns the league's minutes>0 players, each annotated.
  const _lgMetrics = {};
  function leagueMetrics(k) {
    if (_lgMetrics[k]) return _lgMetrics[k];
    const ps = (LEAGUES[k].players || []).filter(p => p.minutes > 0);
    if (!ps.length) return (_lgMetrics[k] = []);
    const exp = ps.map(p => p.minutes / 90);
    const ev = ps.map(p => (p.goals || 0) + (p.assists || 0));
    const sumEv = ev.reduce((a, b) => a + b, 0), sumExp = exp.reduce((a, b) => a + b, 0);
    const m0 = sumExp > 0 ? sumEv / sumExp : 0;      // league minutes-weighted mean G+A/90
    const sm = ps.map((p, i) => (ev[i] + m0 * TP_K) / (exp[i] + TP_K));
    const N = ps.length;
    const mu = sm.reduce((a, b) => a + b, 0) / N;
    const sd = Math.sqrt(sm.reduce((a, b) => a + (b - mu) * (b - mu), 0) / N);
    const hcap = TP_LEAGUE_HANDICAP[k] != null ? TP_LEAGUE_HANDICAP[k] : 0;
    const out = ps.map((p, i) => {
      let below = 0, eq = 0;
      for (const v of sm) { if (v < sm[i]) below++; else if (v === sm[i]) eq++; }
      return Object.assign({}, p, {
        _lk: k, _league: LEAGUES[k].league.name, ga: ev[i],
        ga90: ev[i] * 90 / p.minutes,
        pct: N > 1 ? 100 * (below + 0.5 * eq) / N : 100,
        tough: (sd > 0 ? (sm[i] - mu) / sd : 0) - hcap });
    });
    return (_lgMetrics[k] = out);
  }
  function topPlayersData() {
    const rows = [];
    Object.keys(LEAGUES).forEach(k => {
      const ps = leagueMetrics(k);
      if (!ps.length) return;
      const lgMax = ps.reduce((m, p) => Math.max(m, p.minutes), 0);
      const floor = Math.max(TP_ABS, TP_REL * lgMax);   // scales with this league's season stage
      ps.forEach(p => {
        if (p.minutes < floor) return;               // not a regular for this league's stage
        rows.push(p);                                // already carries ga/ga90/pct/tough
      });
    });
    return rows;
  }
  function renderTopPlayers() {
    const cols = [
      ["_league","League","left", p=>esc(p._league),                          p=>p._league, true],
      ["player_name","Player","left", p=>`<a class="pl-link">${esc(p.player_name)}</a>`, p=>p.player_name],
      ["team","Team","left", p=>p.team_code ? `<a data-team="${p.team_code}" data-lk="${p._lk}">${esc(p.team_code)}</a>` : esc(p.team_name||""), p=>p.team_name],
      ["position","Pos","left", p=>esc(p.position||""),                       p=>p.position||"", true],
      ["matches","Apps","right", p=>p.matches,                                p=>p.matches, true],
      ["minutes","Min","right", p=>p.minutes,                                 p=>p.minutes, true],
      ["goals","G","right", p=>p.goals,                                       p=>p.goals],
      ["assists","A","right", p=>p.assists,                                   p=>p.assists],
      ["ga","G+A","right", p=>p.ga,                                          p=>p.ga],
      ["ga90","G+A/90","right", p=>`<b>${p.ga90.toFixed(2)}</b>`,             p=>p.ga90],
      ["pct","Lg%","right", p=>p.pct.toFixed(1),                              p=>p.pct],
      ["tough","Tough z","right", p=>(p.tough>=0?"+":"")+p.tough.toFixed(2),  p=>p.tough],
      ["xg","xG","right", p=>p.xg.toFixed(1),                                 p=>p.xg, true],
      ["xa","xA","right", p=>p.xa.toFixed(1),                                 p=>p.xa, true],
      ["shots","Sh","right", p=>(p.shots||""),                               p=>p.shots, true],
    ];
    // Fixed membership: the top 100 by G+A per 90 (rate, not season-stage-biased total);
    // then re-sort for display by whatever column is clicked.
    let pool = topPlayersData().sort((a,b) =>
      (b.ga90-a.ga90) || (b.ga-a.ga) || String(a.player_name).localeCompare(String(b.player_name)));
    pool = pool.slice(0, 100);
    if (!pool.length) {
      $("players_all-view").innerHTML = `<div class="legend">No player data loaded — run
        <code>python -m leagues.update --players</code> to fetch it.</div>`;
      return;
    }
    const sc = cols.find(c => c[0]===taSort) || cols[0];
    const shown = pool.slice().sort((a,b) => {
      const va=sc[4](a), vb=sc[4](b);
      let r = (typeof va==="number" && typeof vb==="number") ? va-vb : String(va).localeCompare(String(vb));
      r = taAsc ? r : -r;
      return r || (b.ga90-a.ga90) || (b.ga-a.ga) || String(a.player_name).localeCompare(String(b.player_name));
    });
    const th = `<th>#</th>` + cols.map(c =>
      `<th data-col="${c[0]}" style="text-align:${c[2]}" class="${c[5]?'col-sec ':''}${c[0]===taSort?(taAsc?'sort-asc':'sort-desc'):''}">${c[1]}</th>`).join("");
    const body = shown.map((p,i) =>
      `<tr><td class="pos">${i+1}</td>` + cols.map(c =>
        `<td class="${c[5]?'col-sec':''}" style="text-align:${c[2]}">${c[3](p)}</td>`).join("") + `</tr>`).join("");
    $("players_all-view").innerHTML =
      `<div class="sec-h">Top Players</div>
       <div class="legend">The top 100 players across all leagues, ranked by <b>G+A/90</b>
         (goals + assists per 90 minutes) among regular players — a rate, so leagues further
         into their season don't crowd out ones just starting. <b>Lg%</b> is the percentile of
         each player's sample-smoothed G+A/90 within their own league (100 = best in league);
         <b>Tough z</b> is that rate in standard deviations above an average player, after
         subtracting their league's strength handicap (0 = the toughest league's baseline) — so
         unlike a percentile it has no ceiling, and a dominant player in a weaker league can still
         outrank a good one in a stronger league.
         Click a player for their card, a header to sort, a team to open it.</div>
       <div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("players_all-view").querySelectorAll("th[data-col]").forEach(h => h.onclick = () => {
      const c = h.dataset.col;
      if (c===taSort) taAsc = !taAsc; else { taSort = c; taAsc = (c==="player_name"||c==="_league"||c==="position"); }
      renderTopPlayers();
    });
    $("players_all-view").querySelectorAll("a[data-team]").forEach(a => a.onclick = () => {
      cur = a.dataset.lk; teamCode = a.dataset.team; teamTab = "summary"; setView("team");
    });
    $("players_all-view").querySelectorAll("tbody .pl-link").forEach((a, i) => a.onclick = () => showPlayerCard(shown[i]));
  }

  // ---- Standings odds table ----
  // Columns are built per league so the odds headers carry league-specific
  // labels (Title vs Shield, UCL vs Playoff) and the Europe / relegation bands
  // are dropped for leagues that don't have them (e.g. MLS).
  function columnsFor(L, barFn) {
    // [key, label, cell(r), secondary?] — secondary columns (and their cells) drop on phones.
    const g = L.league;
    const cols = [
      ["_xrank","#", r=>`<td class="pos">${r._xrank}</td>`],
      ["name","Team", r=>`<td><a data-team="${r.code}">${esc(r.name)}</a></td>`],
      ["played","Pld", r=>`<td>${r.played}</td>`],
      ["cur_pts","Pts", r=>`<td>${r.cur_pts}</td>`],
      ["cur_gd","GD", r=>`<td class="col-sec">${r.cur_gd>0?'+':''}${r.cur_gd}</td>`, true],
      ["proj_pts","Proj", r=>`<td class="col-sec">${r.proj_pts.toFixed(1)}</td>`, true],
      ["title_pct",g.title_label+"%", r=>`<td>${barFn(r)}${pct(r.title_pct)}</td>`],
      ["ucl_pct",g.qual_label+"%", r=>`<td class="ucl">${pct(r.ucl_pct)}</td>`],
    ];
    if (g.europa_slots>0)
      cols.push(["europe_pct",g.qual2_label+"%", r=>`<td class="eur col-sec">${pct(r.europe_pct)}</td>`, true]);
    if (g.relegation_slots>0)
      cols.push(["releg_pct",g.drop_label+"%", r=>`<td class="rel col-sec">${pct(r.releg_pct)}</td>`, true]);
    cols.push(["exp_rank","xRank", r=>`<td class="col-sec">${r.exp_rank.toFixed(2)}</td>`, true]);
    return cols;
  }
  function sortedTeams() {
    const rows = LEAGUES[cur].teams.filter(r =>
      !filter || r.name.toLowerCase().includes(filter) || r.code.toLowerCase().includes(filter));
    rows.sort((a,b) => {
      let x=a[sortCol], y=b[sortCol];
      if (typeof x==="string") return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
      return sortAsc ? x-y : y-x;
    });
    return rows;
  }
  function renderMain() {
    const L = LEAGUES[cur], g = L.league;
    // Projected finishing position (1..N by expected rank) shown in the '#' column.
    L.teams.slice().sort((a,b)=>a.exp_rank-b.exp_rank).forEach((t,i)=>{ t._xrank = i+1; });
    const maxTitle = Math.max(...L.teams.map(t=>t.title_pct), 1);
    const barFn = r => `<span class="bar-track"><span class="bar-fill" style="width:${(r.title_pct/maxTitle*100).toFixed(1)}%"></span></span>`;
    const cols = columnsFor(L, barFn);
    const th = cols.map(([c,l,,sec]) =>
      `<th data-col="${c}" class="${sec?'col-sec ':''}${c===sortCol?(sortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const body = sortedTeams().map(r =>
      `<tr>${cols.map(([,,cell]) => cell(r)).join("")}</tr>`).join("");
    const bands = [`<b>${g.title_label}%</b> finish 1st`,
                   `<b>${g.qual_label}%</b> top ${g.ucl_slots}`];
    if (g.europa_slots>0) bands.push(`<b>${g.qual2_label}%</b> top ${g.ucl_slots+g.europa_slots}`);
    if (g.relegation_slots>0) bands.push(`<b>${g.drop_label}%</b> bottom ${g.relegation_slots}`);
    $("main-view").innerHTML =
      `<input type="search" id="flt" placeholder="filter teams…" value="${esc(filter)}">
       <div class="legend"><b>#</b> projected finish · <b>Proj</b> mean final points · ${bands.join(" · ")} ·
         <b>xRank</b> expected finishing position (mean)</div>
       <div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("flt").oninput = (e) => { filter = e.target.value.toLowerCase(); renderMain(); };
    $("main-view").querySelectorAll("th[data-col]").forEach(h =>
      h.onclick = () => {
        const c = h.dataset.col;
        if (c===sortCol) sortAsc = !sortAsc;
        else { sortCol = c; sortAsc = (c==="name"||c==="exp_rank"||c==="_xrank"); }
        renderMain();
      });
    $("main-view").querySelectorAll("a[data-team]").forEach(a =>
      a.onclick = () => { teamCode = a.dataset.team; setView("team"); });
  }

  // ---- Position matrix (heatmap) ----
  function renderMatrix() {
    const L = LEAGUES[cur], n = L.league.n_teams;
    const rows = L.teams.slice().sort((a,b)=>a.exp_rank-b.exp_rank);
    const pmax = Math.max(...rows.flatMap(r=>r.position_probs), 0.05);
    const hdr = `<th class="name">Team</th>` +
      Array.from({length:n}, (_,i)=>`<th>${i+1}</th>`).join("");
    const body = rows.map(r => {
      const cells = r.position_probs.map((p,i) => {
        const cls = i < L.league.ucl_slots ? "ucl-col" : (i >= n-L.league.relegation_slots ? "rel-col" : "");
        const txt = p>=0.005 ? Math.round(p*100) : "";
        return `<td class="cell ${cls}" style="background:${heat(p,pmax)}" title="P(#${i+1})=${(p*100).toFixed(1)}%">${txt}</td>`;
      }).join("");
      return `<tr><td class="name"><a data-team="${r.code}">${esc(r.name)}</a></td>${cells}</tr>`;
    }).join("");
    const g = L.league;
    let note = `Columns left of the line are ${g.qual_name} places`;
    if (g.relegation_slots>0) note += `, right are ${g.drop_name}`;
    note += ".";
    $("matrix-view").innerHTML =
      `<div class="legend">Each cell = probability (%) a team finishes in that position.
        Darker = more likely. ${note}</div>
       <div class="wrap"><table class="matrix"><thead><tr>${hdr}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("matrix-view").querySelectorAll("a[data-team]").forEach(a =>
      a.onclick = () => { teamCode = a.dataset.team; setView("team"); });
  }

  // ---- Remaining fixtures ----
  const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function fmtDate(s) {  // "YYYY-MM-DD" -> "Sep 26" (no timezone shift), else the raw string
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
    return m ? `${MON[+m[2]-1]} ${+m[3]}` : (s || "");
  }
  function kickoff(f) {  // UTC timestamp -> Pacific date+time; date-only -> "Mon D"
    if (f.datetime_utc && f.datetime_utc.includes("T")) {
      const d = new Date(f.datetime_utc);
      if (!isNaN(d)) return d.toLocaleString("en-US", { timeZone: "America/Los_Angeles",
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }
    return fmtDate(f.date || f.datetime_utc);
  }
  function kickoffSort(f) {  // sortable UTC key; date-only games get a midday-UTC time so
    const dt = f.datetime_utc || "";  // they interleave with Pacific-displayed timed games
    if (dt.includes("T")) return dt;
    const d = f.date || dt;
    return d ? d + "T12:00:00Z" : "";
  }
  const isPlayed = f => f.home_goals != null && f.home_goals !== "";
  function scoreCell(f) {  // played -> actual colored score (home perspective); else gray xG forecast
    if (isPlayed(f)) {
      const gf = +f.home_goals, ga = +f.away_goals;
      const cls = gf > ga ? "w" : (gf < ga ? "l" : "d");
      return `<span class="res ${cls}">${gf}–${ga}</span>`;
    }
    return f.lam_home == null ? "" : `<span style="color:#8b949e">${f.lam_home.toFixed(1)}–${f.lam_away.toFixed(1)}</span>`;
  }
  function fixtureCols() {
    // [key, label, align, cell(f), sortVal(f), secondary?] — forecast cells blank for played rows.
    return [
      ["kickoff", "Kickoff", "left",   f => esc(kickoff(f)),                 f => kickoffSort(f)],
      ["home",    "Home",    "right",  f => esc(f.home_name),                f => f.home_name],
      ["xg",      "xG / Score","center",scoreCell,                           f => isPlayed(f) ? 99 : (f.lam_home==null ? -1 : f.lam_home - f.lam_away)],
      ["away",    "Away",    "left",   f => esc(f.away_name),                f => f.away_name],
      ["wdl",     "W / D / L","center",f => f.win==null ? "" : `<span class="wdl"><span class="w" style="width:${f.win*100}%"></span><span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`, f => f.win ?? -1, true],
      ["win",     "H",       "right",  f => f.win==null ? "" : `${(f.win*100).toFixed(0)}%`,   f => f.win ?? -1, true],
      ["draw",    "D",       "right",  f => f.draw==null ? "" : `${(f.draw*100).toFixed(0)}%`, f => f.draw ?? -1, true],
      ["loss",    "A",       "right",  f => f.loss==null ? "" : `${(f.loss*100).toFixed(0)}%`, f => f.loss ?? -1, true],
      ["info_pct","Info%",   "right",  f => f.info_pct==null ? "" : `${f.info_pct.toFixed(2)}%`, f => (f.info_pct ?? -1), true],
      ["post_bits","H after", "right",  f => f.post_bits==null ? "" : `${fmtBits(f.post_bits)}`,  f => (f.post_bits ?? -1), true],
    ];
  }
  function renderFixtures() {
    const L = LEAGUES[cur];
    const all = (L.results || []).concat(L.fixtures || []);
    if (!all.length) {
      $("fixtures-view").innerHTML = `<div class="legend">No fixtures or results loaded for this league.</div>`;
      return;
    }
    const cols = fixtureCols();
    const sv = (cols.find(c => c[0] === fixtSortCol) || cols[0])[4];
    const rows = all.slice().sort((a, b) => {
      let x = sv(a), y = sv(b);
      if (typeof x === "string" || typeof y === "string")
        return fixtSortAsc ? String(x).localeCompare(String(y)) : String(y).localeCompare(String(x));
      return fixtSortAsc ? x - y : y - x;
    });
    // Divider before the first upcoming game (only meaningful in the default kickoff-ascending view).
    const firstFut = (fixtSortCol === "kickoff" && fixtSortAsc) ? rows.findIndex(f => !isPlayed(f)) : -1;
    const th = cols.map(([c, l, al, , , sec]) =>
      `<th data-col="${c}" style="text-align:${al}" class="${sec?'col-sec ':''}${c===fixtSortCol?(fixtSortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const body = rows.map((f, i) => {
      const nowCls = i === firstFut ? ' class="now"' : '';
      return `<tr${nowCls}>${cols.map(([, , al, cell, , sec]) =>
        `<td class="${sec?'col-sec':''}" style="text-align:${al}">${cell(f)}</td>`).join("")}</tr>`;
    }).join("");
    $("fixtures-view").innerHTML =
      `<div class="legend">All league games — played results (final score) and remaining fixtures
        with kickoff (US Pacific), model expected goals,
        <span style="color:#3fb950">home</span> / <span style="color:#8b949e">draw</span> /
        <span style="color:#f85149">away</span> win probabilities, and
        <b>Info%</b> — the expected % drop in the title race's uncertainty (entropy) once the
        result is known — and <b>H after</b>, the expected title-race entropy (bits) still
        remaining once this game's round is played, declining to 0 by season's end. The
        <span style="color:#f78166">orange line</span> marks the next game. Click a header to sort.</div>
       <div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("fixtures-view").querySelectorAll("th[data-col]").forEach(h =>
      h.onclick = () => {
        const c = h.dataset.col;
        if (c === fixtSortCol) fixtSortAsc = !fixtSortAsc;
        else { fixtSortCol = c; fixtSortAsc = (c === "kickoff" || c === "home" || c === "away"); }
        renderFixtures();
      });
  }

  // ---- Top games (cross-league schedule) ----
  function selName(f, name) {  // highlight a name when its team is selected
    const s = esc(name);
    return selectedTeams.has(teamKey(f._lk, name)) ? `<span class="sel">${s}</span>` : s;
  }
  function scheduleCols() {
    // National rows carry no model output (xG / W-D-L / Info%), so those cells blank out.
    return [
      ["kickoff", "Kickoff", "left",   f => esc(kickoff(f)),  f => kickoffSort(f)],
      ["league",  "League",  "left",   f => esc(f._league),   f => f._league, true],
      ["home",    "Home",    "right",  f => selName(f, f.home_name), f => f.home_name],
      ["xg",      "xG",      "center", f => f.lam_home==null ? "" : `<span style="color:#8b949e">${f.lam_home.toFixed(1)}–${f.lam_away.toFixed(1)}</span>`, f => f.lam_home==null ? -1 : f.lam_home - f.lam_away, true],
      ["away",    "Away",    "left",   f => selName(f, f.away_name), f => f.away_name],
      ["wdl",     "W / D / L","center",f => f.win==null ? "" : `<span class="wdl"><span class="w" style="width:${f.win*100}%"></span><span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`, f => f.win ?? -1, true],
      ["win",     "H",       "right",  f => f.win==null ? "" : `${(f.win*100).toFixed(0)}%`,  f => f.win ?? -1, true],
      ["draw",    "D",       "right",  f => f.draw==null ? "" : `${(f.draw*100).toFixed(0)}%`, f => f.draw ?? -1, true],
      ["loss",    "A",       "right",  f => f.loss==null ? "" : `${(f.loss*100).toFixed(0)}%`, f => f.loss ?? -1, true],
      ["info_pct","Info%",   "right",  f => f.info_pct==null ? "" : `${f.info_pct.toFixed(2)}%`, f => (f.info_pct ?? -1), true],
    ];
  }
  function teamDropdown() {
    const groups = keys.map(k => {
      const items = LEAGUES[k].teams.slice()
        .sort((a, b) => a.name.localeCompare(b.name))
        .map(t => {
          const key = teamKey(k, t.name), on = selectedTeams.has(key) ? " checked" : "";
          return `<label data-name="${esc(t.name.toLowerCase())}"><input type="checkbox"
            data-key="${esc(key)}"${on}>${esc(t.name)}</label>`;
        }).join("");
      return `<div class="tp-grp">${esc(LEAGUES[k].league.name)}</div>${items}`;
    }).join("");
    let natGroup = "";
    if ((NATIONAL || []).length) {
      const items = NATIONAL.map(t => {
        const key = teamKey("__national__", t.name), on = selectedTeams.has(key) ? " checked" : "";
        return `<label data-name="${esc(t.name.toLowerCase())}"><input type="checkbox"
          data-key="${esc(key)}"${on}>${esc(t.name)}</label>`;
      }).join("");
      natGroup = `<div class="tp-grp">National teams</div>${items}`;
    }
    return `<div class="tp-dd">
      <button class="tp-btn" id="team-btn">Teams (${selectedTeams.size}) ▾</button>
      <div class="tp-panel" id="team-panel" style="display:${teamDDOpen ? '' : 'none'}">
        <input class="tp-filter" id="team-filter" placeholder="filter teams…" value="${esc(teamFilter)}">
        ${groups}${natGroup}
      </div></div>`;
  }
  function applyTeamFilter() {
    const q = teamFilter.toLowerCase();
    $("team-panel").querySelectorAll("label[data-name]").forEach(l =>
      l.style.display = (!q || l.dataset.name.includes(q)) ? "" : "none");
    $("team-panel").querySelectorAll(".tp-grp").forEach(g => g.style.display = q ? "none" : "");
  }
  // Dropdown of competition stages: each round is "<round> and beyond" for its competition.
  function compDropdown() {
    if (!(COMPETITIONS || []).length) return "";
    const groups = COMPETITIONS.map(c => {
      const items = (c.rounds || []).map((r, i) => {
        const key = compKey(c.key, i), on = selectedRounds.has(key) ? " checked" : "";
        return `<label><input type="checkbox" data-round="${esc(key)}"${on}>${esc(r.name)}</label>`;
      }).join("");
      return items ? `<div class="tp-grp">${esc(c.name)}</div>${items}` : "";
    }).join("");
    if (!groups) return "";
    return `<div class="tp-dd">
      <button class="tp-btn" id="comp-btn">Competitions (${selectedRounds.size}) ▾</button>
      <div class="tp-panel" id="comp-panel" style="display:${compDDOpen ? '' : 'none'}">
        <div class="legend" style="margin:2px 4px 6px">Adds all upcoming games in that stage and beyond.</div>
        ${groups}
      </div></div>`;
  }
  // A league's most-decisive upcoming games, in Info% order, taken until its
  // champion-race entropy falls to/below the threshold (nothing if already below).
  function topGamesFor(k) {
    const H0 = (LEAGUES[k].meta.champ_entropy_bits ?? 0);
    if (H0 <= threshold) return [];
    const out = [];
    LEAGUES[k].fixtures.slice()
      .sort((a, b) => (a.info_rank ?? 1e9) - (b.info_rank ?? 1e9))
      .some(f => { out.push(f); return (f.cum_bits ?? 0) <= threshold; });
    return out;
  }
  function renderReadout() {
    const el = $("sched-readout");
    if (!el) return;
    el.innerHTML = keys.map(k => {
      const H0 = (LEAGUES[k].meta.champ_entropy_bits ?? 0);
      const below = H0 <= threshold, n = topGamesFor(k).length;
      return `<span class="topn"><b style="color:#e6edf3">${esc(LEAGUES[k].league.name)}</b> `
        + `${fmtBits(H0)}b ${below ? "· below threshold" : "→ " + n + (n === 1 ? " game" : " games")}</span>`;
    }).join("");
  }
  function renderSchedule() {
    $("schedule-view").innerHTML =
      `<div class="legend">Each league's most title-decisive upcoming games (ranked by
        <b>Info%</b>) — just enough to pull its title-race <b>entropy</b> below the threshold you
        set, so a league in a tight race contributes more games than a settled one — plus every
        remaining game of any team you follow (including the national teams, whose games are
        shown for reference without model odds), and every upcoming game of any competition
        stage you pick (that round and beyond). Click a header to sort.</div>
       <div class="topn-row">
         <label class="topn"><span>Title-race entropy ≤</span>
           <input type="number" min="0" step="0.1" value="${threshold}" id="thr-input">
           <span>bits</span></label>
         ${teamDropdown()}
         ${compDropdown()}
       </div>
       <div id="sched-readout" class="topn-row"></div>
       <div id="sched-table" class="wrap"></div>`;

    $("thr-input").oninput = (e) => {
      const v = parseFloat(e.target.value);
      threshold = (isFinite(v) && v >= 0) ? v : 0;
      saveThreshold();
      renderReadout();
      renderScheduleTable();
    };
    $("team-btn").onclick = () => {
      teamDDOpen = !teamDDOpen;
      $("team-panel").style.display = teamDDOpen ? "" : "none";
    };
    $("team-filter").oninput = (e) => { teamFilter = e.target.value; applyTeamFilter(); };
    $("team-panel").querySelectorAll("input[data-key]").forEach(cb =>
      cb.onchange = () => {
        if (cb.checked) selectedTeams.add(cb.dataset.key); else selectedTeams.delete(cb.dataset.key);
        saveTeams();
        $("team-btn").textContent = `Teams (${selectedTeams.size}) ▾`;
        renderScheduleTable();
      });
    if ($("comp-btn")) {
      $("comp-btn").onclick = () => {
        compDDOpen = !compDDOpen;
        $("comp-panel").style.display = compDDOpen ? "" : "none";
      };
      $("comp-panel").querySelectorAll("input[data-round]").forEach(cb =>
        cb.onchange = () => {
          if (cb.checked) selectedRounds.add(cb.dataset.round); else selectedRounds.delete(cb.dataset.round);
          saveRounds();
          $("comp-btn").textContent = `Competitions (${selectedRounds.size}) ▾`;
          renderScheduleTable();
        });
    }
    applyTeamFilter();
    renderReadout();
    renderScheduleTable();
  }
  function renderScheduleTable() {
    const cols = scheduleCols();
    const seen = new Set();
    let rows = [];
    const add = (k, f) => {
      const id = k + "::" + f.match_number;
      if (seen.has(id)) return;
      seen.add(id);
      rows.push(Object.assign({ _league: LEAGUES[k].league.name, _lk: k }, f));
    };
    // (a) enough top games per league to pull it below the entropy threshold
    keys.forEach(k => topGamesFor(k).forEach(f => add(k, f)));
    // (b) every remaining game of each selected league team
    selectedTeams.forEach(key => {
      const [k, name] = key.split("\t");
      if (k === "__national__") return;
      (LEAGUES[k] ? LEAGUES[k].fixtures : []).forEach(f => {
        if (f.home_name === name || f.away_name === name) add(k, f);
      });
    });
    // (c) upcoming games of each followed national team (display-only rows)
    selectedTeams.forEach(key => {
      const [k, name] = key.split("\t");
      if (k !== "__national__") return;
      const nt = (NATIONAL || []).find(t => t.name === name);
      if (!nt) return;
      (nt.games || []).forEach(g => {
        if (g.status !== "scheduled") return;          // future games only
        const id = "nat::" + name + "::" + g.event_id;
        if (seen.has(id)) return;
        seen.add(id);
        const home = g.venue !== "away";               // home/neutral -> US listed first
        rows.push({
          _league: name, _lk: "__national__", _national: true,
          date: g.date, datetime_utc: "",
          home_name: home ? name : g.opponent,
          away_name: home ? g.opponent : name,
        });
      });
    });
    // (d) upcoming games of each followed competition stage — that round and beyond
    selectedRounds.forEach(key => {
      const [ck, iStr] = key.split("\t");
      const comp = (COMPETITIONS || []).find(c => c.key === ck);
      if (!comp) return;
      (comp.rounds || []).slice(+iStr).forEach(rd => {         // this stage onward
        (rd.matches || []).forEach(g => {
          if (g.status !== "scheduled") return;                // upcoming only
          const id = "cmp::" + ck + "::" + g.event_id;
          if (seen.has(id)) return;
          seen.add(id);
          rows.push({
            _league: comp.name + " · " + rd.name, _lk: "__competition__", _competition: true,
            date: g.date, datetime_utc: g.datetime_utc || "",
            home_name: g.home, away_name: g.away,
          });
        });
      });
    });
    // (e) a followed club's cup games, even when that competition/stage isn't picked —
    // following a team should surface everything it plays, not just its league fixtures.
    // (home_lk/home_team are tagged server-side, so this is a plain equality check.)
    selectedTeams.forEach(key => {
      const [k, name] = key.split("\t");
      if (k === "__national__") return;
      (COMPETITIONS || []).forEach(comp => (comp.rounds || []).forEach(rd => {
        (rd.matches || []).forEach(g => {
          if (g.status !== "scheduled") return;                // upcoming only, as in (d)
          if (!((g.home_lk === k && g.home_team === name) ||
                (g.away_lk === k && g.away_team === name))) return;
          const id = "cmp::" + comp.key + "::" + g.event_id;   // same id as (d) -> dedupes
          if (seen.has(id)) return;
          seen.add(id);
          rows.push({
            _league: comp.name + " · " + rd.name, _lk: "__competition__", _competition: true,
            date: g.date, datetime_utc: g.datetime_utc || "",
            home_name: g.home, away_name: g.away,
          });
        });
      }));
    });

    const sv = (cols.find(c => c[0] === schedSortCol) || cols[0])[4];
    rows.sort((a, b) => {
      let x = sv(a), y = sv(b);
      if (typeof x === "string") return schedSortAsc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      return schedSortAsc ? x - y : y - x;
    });

    // Phones: render each game as a stacked card (date · league, teams, compact odds bar).
    const mobile = window.matchMedia && window.matchMedia("(max-width:640px)").matches;
    if (mobile) {
      $("sched-table").innerHTML = rows.length ? rows.map(f => {
        const odds = f.win == null ? "" :
          `<div class="gc-odds"><span class="wdl"><span class="w" style="width:${f.win*100}%"></span>`
          + `<span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`
          + `<span>${(f.win*100).toFixed(0)}/${(f.draw*100).toFixed(0)}/${(f.loss*100).toFixed(0)}%</span></div>`;
        return `<div class="gcard"><div class="gc-top"><span class="gc-when">${esc(kickoff(f))}</span>`
          + `<span class="gc-lg">${esc(f._league)}</span></div>`
          + `<div class="gc-teams">${selName(f, f.home_name)} <span class="vs">v</span> ${selName(f, f.away_name)}</div>`
          + `${odds}</div>`;
      }).join("")
      : `<div style="color:#8b949e;padding:16px">No games at this threshold — lower it to include more, or follow a team above.</div>`;
      return;
    }

    const th = cols.map(([c, l, al, , , sec]) =>
      `<th data-col="${c}" style="text-align:${al}" class="${sec?'col-sec ':''}${c===schedSortCol?(schedSortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const bd = rows.length
      ? rows.map(f => `<tr>${cols.map(([, , al, cell, , sec]) => `<td class="${sec?'col-sec':''}" style="text-align:${al}">${cell(f)}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${cols.length}" style="color:#8b949e;padding:16px">No games at this threshold — lower it to include more, or follow a team above.</td></tr>`;
    $("sched-table").innerHTML = `<table><thead><tr>${th}</tr></thead><tbody>${bd}</tbody></table>`;
    $("sched-table").querySelectorAll("th[data-col]").forEach(h =>
      h.onclick = () => {
        const c = h.dataset.col;
        if (c === schedSortCol) schedSortAsc = !schedSortAsc;
        else { schedSortCol = c; schedSortAsc = (c === "kickoff" || c === "league" || c === "home" || c === "away"); }
        renderScheduleTable();
      });
  }

  // ---- Team detail ----
  function teamSchedule(L, code) {              // past results + future fixtures for a team
    const rows = [];
    (L.results || []).forEach(f => {
      if (f.home !== code && f.away !== code) return;
      const home = f.home === code, gf = home ? f.home_goals : f.away_goals,
            ga = home ? f.away_goals : f.home_goals;
      rows.push({ past: true, sort: f.datetime_utc || f.date || "", f, home,
                  opp: home ? f.away_name : f.home_name,
                  gf, ga, res: gf > ga ? "W" : gf === ga ? "D" : "L" });
    });
    (L.fixtures || []).forEach(f => {
      if (f.home !== code && f.away !== code) return;
      const home = f.home === code;
      rows.push({ past: false, sort: f.datetime_utc || f.date || "￿", f, home,
                  opp: home ? f.away_name : f.home_name });
    });
    rows.sort((a, b) => a.sort < b.sort ? -1 : a.sort > b.sort ? 1 : 0);
    return rows;
  }

  function renderTeam() {
    const L = LEAGUES[cur];
    const r = L.teams.find(t => t.code===teamCode) || L.teams[0];
    const n = L.league.n_teams;
    if (r.code !== treeTeam) { treePath = []; treeTeam = r.code; teamTab = "summary"; }
    $("team-view").innerHTML =
      `<h2 style="margin:0 0 2px">${esc(r.name)}</h2>
       <div class="sub">Currently ${r.cur_rank}${ord(r.cur_rank)} · ${r.cur_pts} pts from ${r.played} played</div>
       <div class="subtabs">
         <a class="${teamTab==='summary'?'active':''}" data-tt="summary">Summary</a>
         <a class="${teamTab==='players'?'active':''}" data-tt="players">Players</a>
       </div>
       <div id="team-body"></div>`;
    $("team-view").querySelectorAll(".subtabs a").forEach(a =>
      a.onclick = () => { teamTab = a.dataset.tt; renderTeam(); });
    if (teamTab === "players") {
      // filter AFTER leagueMetrics so Lg%/Tough z rank against the league, not team-mates
      const players = leagueMetrics(cur).filter(p => p.team_code === r.code);
      $("team-body").innerHTML = `<div class="sec-h">Squad — season totals</div>`
        + `<div class="legend">Individual player stats for this club. Click a player for their card, a header to sort.</div>`
        + `<div id="team-players"></div>`;
      renderPlayersTable("team-players", players, true);
      return;
    }
    renderTeamSummary(L, r, n);
  }

  function renderTeamSummary(L, r, n) {
    const pmax = Math.max(...r.position_probs, 0.05);
    const bars = r.position_probs.map((p,i) =>
      `<div class="col" style="height:${(p/pmax*100).toFixed(1)}%"
        title="#${i+1}: ${(p*100).toFixed(1)}%"></div>`).join("");
    const labels = r.position_probs.map((_,i) =>
      `<div class="lbl" style="flex:1">${(i+1)%2===1||n<=20?i+1:""}</div>`).join("");

    // Metric that drives the swings/tree: title% for contenders, else expected finish.
    const useTitle = r.title_pct >= 1.0;
    const minSup = Math.max(25, Math.round(L.meta.n_sims * 0.004));
    const getV = nd => useTitle ? nd.title : nd.exp_finish;
    const fmtV = v => v == null ? "–" : (useTitle ? v.toFixed(1) + "%" : v.toFixed(1));
    const metricName = useTitle ? "title %" : "expected finish";

    // Schedule table (past results + future predictions with per-game swing).
    const sw = r.future_swings || {};
    const swCell = f => {
      const s = sw[f.match_number]; if (!s) return "";
      const v = o => fmtV(useTitle ? s[o].title : s[o].exp_finish);
      return `<span class="swing"><b class="sw-w">${v('w')}</b><b class="sw-d">${v('d')}</b><b class="sw-l">${v('l')}</b></span>`;
    };
    const sched = teamSchedule(L, r.code);
    const firstFut = sched.findIndex(x => !x.past);
    const schBody = sched.map((row, i) => {
      const f = row.f, opp = `${esc(row.opp)} <span class="pos">(${row.home ? 'H' : 'A'})</span>`;
      const nowCls = i === firstFut ? ' class="now"' : '';
      if (row.past)
        return `<tr${nowCls}><td>${esc(f.date || "")}</td><td>${opp}</td>`
          + `<td><span class="res ${row.res.toLowerCase()}">${row.gf}–${row.ga} ${row.res}</span></td><td></td></tr>`;
      const wdl = `<span class="wdl"><span class="w" style="width:${f.win*100}%"></span>`
        + `<span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`;
      return `<tr${nowCls}><td>${esc(kickoff(f))}</td><td>${opp}</td><td>${wdl}</td><td>${swCell(f)}</td></tr>`;
    }).join("");
    const schedHtml = sched.length
      ? `<div class="sec-h">Schedule</div>
         <div class="legend">Past results and upcoming games. For remaining games, the last column shows
           this team's <b>${metricName}</b> if they <b class="sw-w">win</b> / <b class="sw-d">draw</b> /
           <b class="sw-l">lose</b> that game (all else simulated).</div>
         <div class="wrap"><table class="tsched"><thead><tr><th>When</th><th>Opponent</th>
           <th>Result / W-D-L</th><th>${useTitle ? "title" : "finish"} if W / D / L</th></tr></thead>
           <tbody>${schBody}</tbody></table></div>`
      : "";

    $("team-body").innerHTML =
      `<div class="kpis">
         <div class="kpi"><div class="v">${r.proj_pts.toFixed(0)}</div><div class="k">projected pts (${r.proj_pts_p10}–${r.proj_pts_p90})</div></div>
         <div class="kpi"><div class="v">${r.title_pct.toFixed(1)}%</div><div class="k">title</div></div>
         <div class="kpi"><div class="v">${r.ucl_pct.toFixed(1)}%</div><div class="k">Champions League</div></div>
         <div class="kpi"><div class="v">${r.releg_pct.toFixed(1)}%</div><div class="k">relegation</div></div>
         <div class="kpi"><div class="v">${r.exp_rank.toFixed(1)}</div><div class="k">expected finish</div></div>
       </div>
       <div class="legend">Finishing-position distribution across ${L.meta.n_sims.toLocaleString()} simulations.</div>
       <div class="dist">${bars}</div>
       <div style="display:flex">${labels}</div>
       ${schedHtml}
       <div class="sec-h">Branching: ${metricName} after each game</div>
       <div class="legend">Walk a scenario — click <b class="sw-w">Win</b>, <b class="sw-d">Draw</b> or
         <b class="sw-l">Loss</b> to see this team's ${metricName} conditioned on that path over its next
         few games (everything else simulated). Faint branches have too few matching simulations to trust.</div>
       <div id="odds-tree"></div>`;

    // Interactive drill-down tree over r.odds_tree, driven by treePath.
    function renderTree() {
      const el = $("odds-tree"); if (!el) return;
      const root = r.odds_tree;
      if (!root) { el.innerHTML = `<div class="legend">No games remaining.</div>`; return; }
      const label = { win: "W", draw: "D", loss: "L" }, word = { win: "Win", draw: "Draw", loss: "Loss" };
      let node = root;
      const crumbs = [`<span class="crumb" data-i="0">Now <b>${fmtV(getV(root))}</b></span>`];
      for (let i = 0; i < treePath.length && node.branches; i++) {
        const g = node.game, o = treePath[i];
        node = node.branches[o];
        crumbs.push(`<span class="crumb-sep">→</span>`
          + `<span class="crumb" data-i="${i+1}">${label[o]} v ${esc(g.opp)} <b>${fmtV(getV(node))}</b></span>`);
      }
      let body;
      if (node.branches && node.game) {
        const g = node.game, base = getV(node);
        const cards = ["win", "draw", "loss"].map(o => {
          const c = node.branches[o], v = getV(c), d = (v == null || base == null) ? 0 : v - base;
          const good = useTitle ? d > 0 : d < 0;
          const dStr = (d > 0 ? "+" : "") + (useTitle ? d.toFixed(1) + "pp" : d.toFixed(2));
          const drillable = !!c.branches, low = c.support < minSup;
          const cls = "branch " + o + (low ? " low" : (drillable ? "" : " leaf"));
          return `<div class="${cls}" ${(drillable && !low) ? `data-o="${o}"` : ""}>
              <div class="b-h">${word[o]}</div><div class="b-v">${fmtV(v)}</div>
              <div class="b-d ${good ? "good" : "bad"}">${c.support ? dStr : ""}</div>
              <div class="b-s">n=${c.support.toLocaleString()}</div></div>`;
        }).join("");
        body = `<div class="next-game">Next: <b>${esc(g.opp_name || g.opp)}</b> (${g.ha}) · ${esc(kickoff(g))}</div>
                <div class="branches">${cards}</div>`;
      } else {
        body = `<div class="legend">${node.support < minSup
          ? "Too few matching simulations to branch further along this path."
          : "End of the branching horizon."}</div>`;
      }
      el.innerHTML = `<div class="crumbs">${crumbs.join(" ")}`
        + `${treePath.length ? ' <a class="reset">reset</a>' : ''}</div>${body}`;
      el.querySelectorAll(".branch[data-o]").forEach(b =>
        b.onclick = () => { treePath.push(b.dataset.o); renderTree(); });
      el.querySelectorAll(".crumb").forEach(c =>
        c.onclick = () => { treePath = treePath.slice(0, +c.dataset.i); renderTree(); });
      const rs = el.querySelector(".reset");
      if (rs) rs.onclick = () => { treePath = []; renderTree(); };
    }
    renderTree();
  }
  function ord(n){ const s=["th","st","nd","rd"], v=n%100; return s[(v-20)%10]||s[v]||s[0]; }

  $("nav").querySelectorAll("a").forEach(a =>
    a.onclick = () => { if (a.dataset.view!=="team" || teamCode) setView(a.dataset.view); });

  leagueTabs();
  setView("main");
  // Re-render Top games when crossing the phone breakpoint (table <-> cards).
  if (window.matchMedia)
    window.matchMedia("(max-width:640px)").addEventListener("change", () => {
      if (view === "schedule") renderScheduleTable();
    });
})();
</script>
</body>
</html>
"""


def _team_lookup(key: str) -> tuple[dict, dict]:
    """Build normalised-name -> (code, canonical name) maps from a league's teams.csv."""
    code_by_norm, name_by_norm = {}, {}
    path = DATA_ROOT / key / "teams.csv"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for t in csv.DictReader(f):
                nm, code = t["team_name"], t["code"]
                code_by_norm[_norm_team(nm)] = code
                name_by_norm[_norm_team(nm)] = nm
    return code_by_norm, name_by_norm


def read_players(key: str) -> list[dict]:
    """Load data/leagues/<key>/players.csv (if present) with numeric fields typed.

    Re-resolves each row's team_code from its team_name against the current teams.csv
    (accent-folding + short-name tolerance), so an already-fetched players.csv whose
    codes were blanked by a name mismatch is repaired here — no FBref re-fetch needed."""
    path = DATA_ROOT / key / "players.csv"
    if not path.exists():
        return []
    code_by_norm, name_by_norm = _team_lookup(key)
    ints = ("matches", "minutes", "goals", "assists", "shots", "yellow_cards", "red_cards")
    out = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ints:
                r[k] = int(r[k]) if r.get(k) not in ("", None) else 0
            for k in ("xg", "xa"):
                r[k] = float(r[k]) if r.get(k) not in ("", None) else 0.0
            raw = r.get("team_name", "")
            code, canon = resolve_team_code(_norm_team(raw), code_by_norm, name_by_norm)
            if code:
                r["team_code"], r["team_name"] = code, canon
            out.append(r)
    return out


def _read_json_dir(name: str) -> list[dict]:
    """Load every data/<name>/*.json (display-only artifacts), sorted by filename."""
    root = DATA_ROOT.parent / name
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def read_national() -> list[dict]:
    """Load every data/national/*.json (USMNT/USWNT schedules)."""
    return _read_json_dir("national")


def read_headshots(key: str) -> dict:
    """player name -> {img, page, by, lic} from data/headshots/<key>.json (may not exist).

    Only URLs are stored; the page references them, so nothing is downloaded or
    redistributed and the report stays a single small file."""
    path = DATA_ROOT.parent / "headshots" / f"{key}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("players", {}) or {}
    except (ValueError, OSError):
        return {}


def read_player_notes() -> dict:
    """Curated briefing notes from data/notes/players.json (hand-reviewed, committed)."""
    path = DATA_ROOT.parent / "notes" / "players.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def attach_player_notes(leagues_data: dict) -> None:
    """Hang each curated note on the player it names, when that player is unambiguous.

    Briefings name people loosely — full names, bare surnames, and outright transcription
    errors — so a note is attached only when exactly one player in the loaded leagues matches
    (full name first, then surname). Anything ambiguous is reported and dropped rather than
    pinned on the wrong player, which is the failure mode that actually embarrasses a report.
    """
    doc = read_player_notes()
    notes = doc.get("players") or []
    if not notes:
        return
    sources = doc.get("sources") or {}
    by_full: dict[str, list] = {}
    by_last: dict[str, list] = {}
    for ld in leagues_data.values():
        for pl in ld.get("players", []):
            folded = _fold(pl.get("player_name", ""))
            if not folded:
                continue
            by_full.setdefault(folded, []).append(pl)
            by_last.setdefault(folded.split()[-1], []).append(pl)

    unmatched = []
    for n in notes:
        folded = _fold(n.get("name", ""))
        hits = by_full.get(folded) or []
        if len(hits) != 1 and folded:                 # fall back to a *unique* surname
            hits = by_last.get(folded.split()[-1], [])
        if len(hits) != 1:
            unmatched.append(n.get("name", "?"))
            continue
        src = sources.get(n.get("source"), {})
        hits[0].setdefault("notes", []).append({
            "note": n.get("note", ""), "quote": n.get("quote", ""),
            "tags": n.get("tags", []), "date": n.get("date", ""),
            "conf": n.get("confidence", ""), "src": src.get("show", ""),
        })
    if unmatched:
        shown = ", ".join(f"'{u}'" for u in sorted(set(unmatched))[:8])
        extra = len(set(unmatched)) - 8
        print(f"  [notes] {len(unmatched)} note(s) not attached (no unique player match): "
              + shown + (f", +{extra} more" if extra > 0 else ""))


def read_competitions() -> list[dict]:
    """Load every data/competitions/*.json (Leagues Cup / Champions League)."""
    return _read_json_dir("competitions")


def tag_competition_teams(competitions: list[dict], league_keys) -> list[dict]:
    """Tag each cup match with the league team it corresponds to, when we can identify one.

    Cup articles name clubs their own way ("Inter Miami", "Bayern Munich") while the league
    tables use the fixtures source's names, so matching them needs the same fuzzy resolver the
    player tables use. Doing it here — once, in Python — lets the report show a followed club's
    cup games with a plain equality check instead of re-implementing name matching in the page.
    A name is only tagged when exactly one league claims it, so an ambiguous name stays untagged
    rather than being attributed to the wrong club.
    """
    lookups = {k: _team_lookup(k) for k in league_keys}

    def resolve(name: str) -> tuple[str, str]:
        """(league key, canonical name) for a cup entrant, or ("", "") if not confidently ours.

        Deliberately stricter than the player-table resolver: a cup field is full of clubs that
        aren't in any league we model, so the >=4-char stem tier would mis-assign them (Liga MX's
        "Atlas" stem-matches "Atlanta United"). Here a wrong tag is worse than no tag, so we take
        an exact normalised match first, then a *unique* token-subset match, and never a stem.
        """
        key = _norm_team(name or "")
        if not key:
            return "", ""
        exact = [(k, nbn[key]) for k, (cbn, nbn) in lookups.items() if key in cbn]
        if exact:                                  # exact wins over any looser reading
            return exact[0] if len(exact) == 1 else ("", "")
        toks = set(key.split())
        subset = []
        for k, (cbn, nbn) in lookups.items():
            m = [c for c in cbn if set(c.split()) and (toks <= set(c.split()) or set(c.split()) <= toks)]
            if len(m) == 1:                        # unambiguous within that league
                subset.append((k, nbn[m[0]]))
        return subset[0] if len(subset) == 1 else ("", "")

    for comp in competitions:
        for rnd in comp.get("rounds", []):
            for g in rnd.get("matches", []):
                g["home_lk"], g["home_team"] = resolve(g.get("home", ""))
                g["away_lk"], g["away_team"] = resolve(g.get("away", ""))
    return competitions


def build(leagues_data: dict) -> str:
    for key, ld in leagues_data.items():           # attach player tables (independent of sims)
        ld.setdefault("players", read_players(key))
        shots = read_headshots(key)                # optional headshot URLs for the player card
        for pl in ld["players"]:
            hit = shots.get(pl.get("player_name", ""))
            if hit and hit.get("img"):
                pl["img"] = hit["img"]
                pl["img_page"], pl["img_by"], pl["img_lic"] = (
                    hit.get("page", ""), hit.get("by", ""), hit.get("lic", ""))
    attach_player_notes(leagues_data)              # curated briefing notes -> player cards
    competitions = tag_competition_teams(read_competitions(), list(leagues_data))
    return (HTML_TEMPLATE
            .replace("__DATA_PLACEHOLDER__", json.dumps(leagues_data))
            .replace("__NATIONAL_PLACEHOLDER__", json.dumps(read_national()))
            .replace("__COMPETITIONS_PLACEHOLDER__", json.dumps(competitions)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Render leagues.html from sim_results.json files.")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    data: dict = {}
    for key in LEAGUES:
        f = DATA_ROOT / key / "sim_results.json"
        if f.exists():
            data[key] = json.loads(f.read_text(encoding="utf-8"))

    if not data:
        raise SystemExit("No sim_results.json found. Run leagues.run_sims first.")

    OUT.write_text(build(data), encoding="utf-8")
    print(f"Wrote {OUT}  ({len(data)} league(s): {', '.join(data)})")
    if args.open:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    sys.exit(main())
