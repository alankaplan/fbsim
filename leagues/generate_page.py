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
  <div id="team-view" style="display:none"></div>
</main>
<script>
const LEAGUES = __DATA_PLACEHOLDER__;
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
    JSON.parse(localStorage.getItem("fbsim.topTeams") || "[]")
      .forEach(x => { if (valid.has(x)) selectedTeams.add(x); });
  } catch (e) {}
  function saveTeams() {
    try { localStorage.setItem("fbsim.topTeams", JSON.stringify([...selectedTeams])); } catch (e) {}
  }
  let teamDDOpen = false, teamFilter = "";
  let filter = "";
  let teamCode = null;
  let treePath = [], treeTeam = null;              // odds-tree drill-down state
  let teamTab = "summary";                         // team page sub-tab: summary | players
  let plSort = "goals", plAsc = false;             // players-table sort

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

  function leagueTabs() {
    const lg = keys.map(k =>
      `<button class="lg-btn ${(view!=='schedule'&&k===cur)?'active':''}" data-k="${k}">${esc(LEAGUES[k].league.name)}</button>`
    ).join("");
    const top = `<button class="lg-btn top ${view==='schedule'?'active':''}" data-top="1">Top games</button>`;
    $("league-tabs").innerHTML = lg + top;
    $("league-tabs").querySelectorAll("button").forEach(b =>
      b.onclick = () => {
        if (b.dataset.top) { setView("schedule"); return; }
        cur = b.dataset.k; teamCode = null;
        setView((view==='schedule'||view==='team') ? 'main' : view);
      });
  }

  function head() {
    if (view === "schedule") {  // cross-league view: no single league applies
      $("hdr-badge").innerHTML = "";
      $("hdr-sub").textContent =
        `Top games across ${keys.length} leagues · kickoff in US Pacific time`;
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
    ["main","matrix","fixtures","schedule","players","team"].forEach(x =>
      $(x+"-view").style.display = (x===v ? "" : "none"));
    $("nav").querySelectorAll("a").forEach(a =>
      a.classList.toggle("active", a.dataset.view===v));
    $("nav-team").style.display = (v==="team") ? "" : "none";
    $("nav").style.display = (v==="schedule") ? "none" : "";  // per-league tabs don't apply
    head(); leagueTabs();
    if (v==="main") renderMain();
    else if (v==="matrix") renderMatrix();
    else if (v==="fixtures") renderFixtures();
    else if (v==="schedule") renderSchedule();
    else if (v==="players") renderPlayers();
    else if (v==="team") renderTeam();
  }

  // ---- Players (league-wide "Top players" + per-team squad) ----
  // [key, label, align, cell(p), sortVal(p), teamViewHidden?]
  function playerCols() {
    return [
      ["player_name","Player","left", p=>esc(p.player_name),                 p=>p.player_name],
      ["team","Team","left", p=>`<a data-team="${p.team_code}">${esc(p.team_code||p.team_name)}</a>`, p=>p.team_name, true],
      ["position","Pos","left", p=>esc(p.position||""),                      p=>p.position||""],
      ["matches","Apps","right", p=>p.matches,                               p=>p.matches],
      ["minutes","Min","right", p=>p.minutes,                                p=>p.minutes],
      ["goals","G","right", p=>p.goals,                                      p=>p.goals],
      ["assists","A","right", p=>p.assists,                                  p=>p.assists],
      ["xg","xG","right", p=>p.xg.toFixed(1),                                p=>p.xg],
      ["xa","xA","right", p=>p.xa.toFixed(1),                                p=>p.xa],
      ["shots","Sh","right", p=>(p.shots||""),                               p=>p.shots],
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
      `<th data-col="${c[0]}" style="text-align:${c[2]}" class="${c[0]===plSort?(plAsc?'sort-asc':'sort-desc'):''}">${c[1]}</th>`).join("");
    const body = sorted.map(p =>
      `<tr>${cols.map(c => `<td style="text-align:${c[2]}">${c[3](p)}</td>`).join("")}</tr>`).join("");
    el.innerHTML = `<div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    el.querySelectorAll("th[data-col]").forEach(h => h.onclick = () => {
      const c = h.dataset.col;
      if (c===plSort) plAsc = !plAsc; else { plSort = c; plAsc = false; }
      renderPlayersTable(elId, players, teamView);
    });
    el.querySelectorAll("a[data-team]").forEach(a => a.onclick = () => {
      teamCode = a.dataset.team; teamTab = "summary"; setView("team");
    });
  }
  function renderPlayers() {
    const L = LEAGUES[cur];
    $("players-view").innerHTML =
      `<div class="sec-h">Top players — ${esc(L.league.name)}</div>
       <div class="legend">Season totals${L.meta.used_xg ? " (with xG)" : ""}. Click a header to sort;
         click a team to open it.</div>
       <div id="players-table"></div>`;
    renderPlayersTable("players-table", L.players || [], false);
  }

  // ---- Standings odds table ----
  // Columns are built per league so the odds headers carry league-specific
  // labels (Title vs Shield, UCL vs Playoff) and the Europe / relegation bands
  // are dropped for leagues that don't have them (e.g. MLS).
  function columnsFor(L, barFn) {
    const g = L.league;
    const cols = [
      ["cur_rank","#", r=>`<td class="pos">${r.cur_rank}</td>`],
      ["name","Team", r=>`<td><a data-team="${r.code}">${esc(r.name)}</a></td>`],
      ["played","Pld", r=>`<td>${r.played}</td>`],
      ["cur_pts","Pts", r=>`<td>${r.cur_pts}</td>`],
      ["cur_gd","GD", r=>`<td>${r.cur_gd>0?'+':''}${r.cur_gd}</td>`],
      ["proj_pts","Proj", r=>`<td>${r.proj_pts.toFixed(1)}</td>`],
      ["title_pct",g.title_label+"%", r=>`<td>${barFn(r)}${pct(r.title_pct)}</td>`],
      ["ucl_pct",g.qual_label+"%", r=>`<td class="ucl">${pct(r.ucl_pct)}</td>`],
    ];
    if (g.europa_slots>0)
      cols.push(["europe_pct",g.qual2_label+"%", r=>`<td class="eur">${pct(r.europe_pct)}</td>`]);
    if (g.relegation_slots>0)
      cols.push(["releg_pct",g.drop_label+"%", r=>`<td class="rel">${pct(r.releg_pct)}</td>`]);
    cols.push(["exp_rank","xRank", r=>`<td>${r.exp_rank.toFixed(2)}</td>`]);
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
    const maxTitle = Math.max(...L.teams.map(t=>t.title_pct), 1);
    const barFn = r => `<span class="bar-track"><span class="bar-fill" style="width:${(r.title_pct/maxTitle*100).toFixed(1)}%"></span></span>`;
    const cols = columnsFor(L, barFn);
    const th = cols.map(([c,l]) =>
      `<th data-col="${c}" class="${c===sortCol?(sortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const body = sortedTeams().map(r =>
      `<tr>${cols.map(([,,cell]) => cell(r)).join("")}</tr>`).join("");
    const bands = [`<b>${g.title_label}%</b> finish 1st`,
                   `<b>${g.qual_label}%</b> top ${g.ucl_slots}`];
    if (g.europa_slots>0) bands.push(`<b>${g.qual2_label}%</b> top ${g.ucl_slots+g.europa_slots}`);
    if (g.relegation_slots>0) bands.push(`<b>${g.drop_label}%</b> bottom ${g.relegation_slots}`);
    $("main-view").innerHTML =
      `<input type="search" id="flt" placeholder="filter teams…" value="${esc(filter)}">
       <div class="legend"><b>Proj</b> mean final points · ${bands.join(" · ")} ·
         <b>xRank</b> expected finishing position</div>
       <div class="wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("flt").oninput = (e) => { filter = e.target.value.toLowerCase(); renderMain(); };
    $("main-view").querySelectorAll("th[data-col]").forEach(h =>
      h.onclick = () => {
        const c = h.dataset.col;
        if (c===sortCol) sortAsc = !sortAsc;
        else { sortCol = c; sortAsc = (c==="name"||c==="exp_rank"||c==="cur_rank"); }
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
  function kickoff(f) {  // UTC timestamp -> Pacific date+time; date-only fallback
    if (f.datetime_utc) {
      const d = new Date(f.datetime_utc);
      if (!isNaN(d)) return d.toLocaleString("en-US", { timeZone: "America/Los_Angeles",
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }
    return f.date || "";
  }
  function fixtureCols() {
    // [key, label, align, cell(f), sortVal(f)]
    return [
      ["kickoff", "Kickoff", "left",   f => esc(kickoff(f)),                 f => f.datetime_utc || f.date || ""],
      ["home",    "Home",    "right",  f => esc(f.home_name),                f => f.home_name],
      ["xg",      "xG",      "center", f => `<span style="color:#8b949e">${f.lam_home.toFixed(1)}–${f.lam_away.toFixed(1)}</span>`, f => f.lam_home - f.lam_away],
      ["away",    "Away",    "left",   f => esc(f.away_name),                f => f.away_name],
      ["wdl",     "W / D / L","center",f => `<span class="wdl"><span class="w" style="width:${f.win*100}%"></span><span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`, f => f.win],
      ["win",     "H",       "right",  f => `${(f.win*100).toFixed(0)}%`,    f => f.win],
      ["draw",    "D",       "right",  f => `${(f.draw*100).toFixed(0)}%`,   f => f.draw],
      ["loss",    "A",       "right",  f => `${(f.loss*100).toFixed(0)}%`,   f => f.loss],
      ["info_pct","Info%",   "right",  f => `${(f.info_pct ?? 0).toFixed(2)}%`, f => (f.info_pct ?? 0)],
      ["post_bits","H after", "right",  f => `${fmtBits(f.post_bits ?? 0)}`,     f => (f.post_bits ?? 0)],
    ];
  }
  function renderFixtures() {
    const L = LEAGUES[cur];
    if (!L.fixtures.length) {
      $("fixtures-view").innerHTML = `<div class="legend">Season complete — no remaining fixtures.</div>`;
      return;
    }
    const cols = fixtureCols();
    const sv = (cols.find(c => c[0] === fixtSortCol) || cols[0])[4];
    const rows = L.fixtures.slice().sort((a, b) => {
      let x = sv(a), y = sv(b);
      if (typeof x === "string") return fixtSortAsc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      return fixtSortAsc ? x - y : y - x;
    });
    const th = cols.map(([c, l, al]) =>
      `<th data-col="${c}" style="text-align:${al}" class="${c===fixtSortCol?(fixtSortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const body = rows.map(f =>
      `<tr>${cols.map(([, , al, cell]) => `<td style="text-align:${al}">${cell(f)}</td>`).join("")}</tr>`).join("");
    $("fixtures-view").innerHTML =
      `<div class="legend">Remaining fixtures with kickoff (US Pacific), model expected goals,
        <span style="color:#3fb950">home</span> / <span style="color:#8b949e">draw</span> /
        <span style="color:#f85149">away</span> win probabilities, and
        <b>Info%</b> — the expected % drop in the title race's uncertainty (entropy) once the
        result is known — and <b>H after</b>, the expected title-race entropy (bits) still
        remaining once this game's round is played, declining to 0 by season's end. Sort by
        Kickoff to watch H after tick down. Click a header to sort.</div>
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
    return [
      ["kickoff", "Kickoff", "left",   f => esc(kickoff(f)),  f => f.datetime_utc || f.date || ""],
      ["league",  "League",  "left",   f => esc(f._league),   f => f._league],
      ["home",    "Home",    "right",  f => selName(f, f.home_name), f => f.home_name],
      ["xg",      "xG",      "center", f => `<span style="color:#8b949e">${f.lam_home.toFixed(1)}–${f.lam_away.toFixed(1)}</span>`, f => f.lam_home - f.lam_away],
      ["away",    "Away",    "left",   f => selName(f, f.away_name), f => f.away_name],
      ["wdl",     "W / D / L","center",f => `<span class="wdl"><span class="w" style="width:${f.win*100}%"></span><span class="d" style="width:${f.draw*100}%"></span><span class="l" style="width:${f.loss*100}%"></span></span>`, f => f.win],
      ["win",     "H",       "right",  f => `${(f.win*100).toFixed(0)}%`,  f => f.win],
      ["draw",    "D",       "right",  f => `${(f.draw*100).toFixed(0)}%`, f => f.draw],
      ["loss",    "A",       "right",  f => `${(f.loss*100).toFixed(0)}%`, f => f.loss],
      ["info_pct","Info%",   "right",  f => `${(f.info_pct ?? 0).toFixed(2)}%`, f => (f.info_pct ?? 0)],
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
    return `<div class="tp-dd">
      <button class="tp-btn" id="team-btn">Teams (${selectedTeams.size}) ▾</button>
      <div class="tp-panel" id="team-panel" style="display:${teamDDOpen ? '' : 'none'}">
        <input class="tp-filter" id="team-filter" placeholder="filter teams…" value="${esc(teamFilter)}">
        ${groups}
      </div></div>`;
  }
  function applyTeamFilter() {
    const q = teamFilter.toLowerCase();
    $("team-panel").querySelectorAll("label[data-name]").forEach(l =>
      l.style.display = (!q || l.dataset.name.includes(q)) ? "" : "none");
    $("team-panel").querySelectorAll(".tp-grp").forEach(g => g.style.display = q ? "none" : "");
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
        remaining game of any team you follow. Click a header to sort.</div>
       <div class="topn-row">
         <label class="topn"><span>Title-race entropy ≤</span>
           <input type="number" min="0" step="0.1" value="${threshold}" id="thr-input">
           <span>bits</span></label>
         ${teamDropdown()}
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
    // (b) every remaining game of each selected team
    selectedTeams.forEach(key => {
      const [k, name] = key.split("\t");
      (LEAGUES[k] ? LEAGUES[k].fixtures : []).forEach(f => {
        if (f.home_name === name || f.away_name === name) add(k, f);
      });
    });

    const sv = (cols.find(c => c[0] === schedSortCol) || cols[0])[4];
    rows.sort((a, b) => {
      let x = sv(a), y = sv(b);
      if (typeof x === "string") return schedSortAsc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      return schedSortAsc ? x - y : y - x;
    });

    const th = cols.map(([c, l, al]) =>
      `<th data-col="${c}" style="text-align:${al}" class="${c===schedSortCol?(schedSortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const bd = rows.length
      ? rows.map(f => `<tr>${cols.map(([, , al, cell]) => `<td style="text-align:${al}">${cell(f)}</td>`).join("")}</tr>`).join("")
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
      const players = (L.players || []).filter(p => p.team_code === r.code);
      $("team-body").innerHTML = `<div class="sec-h">Squad — season totals</div>`
        + `<div class="legend">Individual player stats for this club. Click a header to sort.</div>`
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
})();
</script>
</body>
</html>
"""


def read_players(key: str) -> list[dict]:
    """Load data/leagues/<key>/players.csv (if present) with numeric fields typed."""
    path = DATA_ROOT / key / "players.csv"
    if not path.exists():
        return []
    ints = ("matches", "minutes", "goals", "assists", "shots", "yellow_cards", "red_cards")
    out = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ints:
                r[k] = int(r[k]) if r.get(k) not in ("", None) else 0
            for k in ("xg", "xa"):
                r[k] = float(r[k]) if r.get(k) not in ("", None) else 0.0
            out.append(r)
    return out


def build(leagues_data: dict) -> str:
    for key, ld in leagues_data.items():           # attach player tables (independent of sims)
        ld.setdefault("players", read_players(key))
    return HTML_TEMPLATE.replace("__DATA_PLACEHOLDER__", json.dumps(leagues_data))


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
