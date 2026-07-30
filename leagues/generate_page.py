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
  * Top games — a cross-league schedule of the top-N most decisive games per
    league (N selectable per league), plus every remaining game of any teams
    picked from a checkbox dropdown;
  * Team detail — a team's full finishing-position distribution.

Usage
-----
    venv/bin/python -m leagues.generate_page              # all simulated leagues
    venv/bin/python -m leagues.generate_page --open
"""

from __future__ import annotations

import argparse
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
    <a data-view="schedule">Top games</a>
    <a data-view="team" id="nav-team" style="display:none">Team</a>
  </nav>
</header>
<main>
  <div id="main-view"></div>
  <div id="matrix-view" style="display:none"></div>
  <div id="fixtures-view" style="display:none"></div>
  <div id="schedule-view" style="display:none"></div>
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
  let topN = {};                                   // top games per league (schedule view)
  keys.forEach(k => topN[k] = 3);
  let selectedTeams = new Set();                   // "<leagueKey>\t<team name>" (schedule view)
  let teamDDOpen = false, teamFilter = "";
  let filter = "";
  let teamCode = null;

  const $ = (id) => document.getElementById(id);
  const pct = (x) => (x === 0 ? '<span class="zero">0</span>' : x.toFixed(1));
  const esc = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  // Sequential blue heatmap for probabilities 0..pmax.
  function heat(p, pmax) {
    if (p <= 0) return "transparent";
    const t = Math.min(1, p / pmax);
    const a = 0.08 + 0.82 * Math.sqrt(t);
    return `rgba(88,166,255,${a.toFixed(3)})`;
  }

  function leagueTabs() {
    $("league-tabs").innerHTML = keys.map(k =>
      `<button class="lg-btn ${k===cur?'active':''}" data-k="${k}">${esc(LEAGUES[k].league.name)}</button>`
    ).join("");
    $("league-tabs").querySelectorAll("button").forEach(b =>
      b.onclick = () => { cur = b.dataset.k; teamCode = null; setView(view==='team'?'main':view); });
  }

  function head() {
    if (view === "schedule") {  // cross-league view: no single league applies
      $("hdr-badge").innerHTML = "";
      $("league-tabs").style.display = "none";
      $("hdr-sub").textContent =
        `Top games across ${keys.length} leagues · kickoff in US Pacific time`;
      return;
    }
    $("league-tabs").style.display = "";
    const L = LEAGUES[cur], m = L.meta;
    $("hdr-badge").innerHTML = m.used_xg
      ? '<span class="badge xg">FBref xG</span>' : '<span class="badge">goals model</span>';
    const asOf = m.as_of ? ` · forecast from matchday ${m.as_of}` : "";
    $("hdr-sub").textContent =
      `${L.league.name} (${L.league.country}) · ${m.n_played} played, ${m.n_remaining} remaining · `
      + `${m.n_sims.toLocaleString()} simulations${asOf}`;
  }

  function setView(v) {
    view = v;
    ["main","matrix","fixtures","schedule","team"].forEach(x =>
      $(x+"-view").style.display = (x===v ? "" : "none"));
    $("nav").querySelectorAll("a").forEach(a =>
      a.classList.toggle("active", a.dataset.view===v));
    $("nav-team").style.display = (v==="team") ? "" : "none";
    head(); leagueTabs();
    if (v==="main") renderMain();
    else if (v==="matrix") renderMatrix();
    else if (v==="fixtures") renderFixtures();
    else if (v==="schedule") renderSchedule();
    else if (v==="team") renderTeam();
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
        result is known. Click a header to sort.</div>
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
  const teamKey = (k, name) => k + "\t" + name;
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
  function renderSchedule() {
    const controls = keys.map(k => {
      const max = LEAGUES[k].fixtures.length;
      return `<label class="topn"><span>${esc(LEAGUES[k].league.name)}</span>
        <input type="number" min="0" max="${max}" value="${Math.min(topN[k], max)}" data-k="${k}"></label>`;
    }).join("");
    $("schedule-view").innerHTML =
      `<div class="legend">The most title-decisive upcoming games per league (top by
        <b>Info%</b> — expected % drop in that league's title-race entropy), plus every
        remaining game of any team you pick. Set counts / teams, then click a header to sort.</div>
       <div class="topn-row">${controls}${teamDropdown()}</div>
       <div id="sched-table" class="wrap"></div>`;

    $("schedule-view").querySelectorAll("input[data-k]").forEach(inp =>
      inp.onchange = () => {
        const v = parseInt(inp.value, 10);
        topN[inp.dataset.k] = isNaN(v) ? 0 : Math.max(0, v);
        renderScheduleTable();
      });
    $("team-btn").onclick = () => {
      teamDDOpen = !teamDDOpen;
      $("team-panel").style.display = teamDDOpen ? "" : "none";
    };
    $("team-filter").oninput = (e) => { teamFilter = e.target.value; applyTeamFilter(); };
    $("team-panel").querySelectorAll("input[data-key]").forEach(cb =>
      cb.onchange = () => {
        if (cb.checked) selectedTeams.add(cb.dataset.key); else selectedTeams.delete(cb.dataset.key);
        $("team-btn").textContent = `Teams (${selectedTeams.size}) ▾`;
        renderScheduleTable();
      });
    applyTeamFilter();
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
    // (a) top-N by Info% per league
    keys.forEach(k => LEAGUES[k].fixtures.slice()
      .sort((a, b) => (b.info_pct ?? 0) - (a.info_pct ?? 0))
      .slice(0, topN[k]).forEach(f => add(k, f)));
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
      : `<tr><td colspan="${cols.length}" style="color:#8b949e;padding:16px">No games selected — raise a league's count or pick a team above.</td></tr>`;
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
  function renderTeam() {
    const L = LEAGUES[cur];
    const r = L.teams.find(t => t.code===teamCode) || L.teams[0];
    const n = L.league.n_teams;
    const pmax = Math.max(...r.position_probs, 0.05);
    const bars = r.position_probs.map((p,i) =>
      `<div class="col" style="height:${(p/pmax*100).toFixed(1)}%"
        title="#${i+1}: ${(p*100).toFixed(1)}%"></div>`).join("");
    const labels = r.position_probs.map((_,i) =>
      `<div class="lbl" style="flex:1">${(i+1)%2===1||n<=20?i+1:""}</div>`).join("");
    $("team-view").innerHTML =
      `<h2 style="margin:0 0 2px">${esc(r.name)}</h2>
       <div class="sub">Currently ${r.cur_rank}${ord(r.cur_rank)} · ${r.cur_pts} pts from ${r.played} played</div>
       <div class="kpis">
         <div class="kpi"><div class="v">${r.proj_pts.toFixed(0)}</div><div class="k">projected pts (${r.proj_pts_p10}–${r.proj_pts_p90})</div></div>
         <div class="kpi"><div class="v">${r.title_pct.toFixed(1)}%</div><div class="k">title</div></div>
         <div class="kpi"><div class="v">${r.ucl_pct.toFixed(1)}%</div><div class="k">Champions League</div></div>
         <div class="kpi"><div class="v">${r.releg_pct.toFixed(1)}%</div><div class="k">relegation</div></div>
         <div class="kpi"><div class="v">${r.exp_rank.toFixed(1)}</div><div class="k">expected finish</div></div>
       </div>
       <div class="legend">Finishing-position distribution across ${L.meta.n_sims.toLocaleString()} simulations.</div>
       <div class="dist">${bars}</div>
       <div style="display:flex">${labels}</div>`;
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


def build(leagues_data: dict) -> str:
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
