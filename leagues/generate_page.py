#!/usr/bin/env python3
"""
generate_page.py
----------------
Render a self-contained ``leagues.html`` report from every
``data/leagues/<key>/sim_results.json`` produced by ``run_sims.py``. No server
or dependencies — open the file directly in a browser.

Reuses the World Cup page's patterns: a hash/state-routed single-page app, the
sortable/filterable table (``data-col`` + ``sortCol``/``sortAsc``), and JSON
embedded as a JS global via ``__PLACEHOLDER__`` replacement.

Views: a league switcher across all simulated leagues, plus
  * Standings odds — projected points, title / Champions-League / any-Europe /
    relegation probabilities (sortable, filterable);
  * Position matrix — a heatmap of P(finish in each position);
  * Fixtures — remaining games with win/draw/loss probabilities;
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
    <a data-view="team" id="nav-team" style="display:none">Team</a>
  </nav>
</header>
<main>
  <div id="main-view"></div>
  <div id="matrix-view" style="display:none"></div>
  <div id="fixtures-view" style="display:none"></div>
  <div id="team-view" style="display:none"></div>
</main>
<script>
const LEAGUES = __DATA_PLACEHOLDER__;
(function () {
  const keys = Object.keys(LEAGUES);
  let cur = keys[0];
  let view = "main";
  let sortCol = "exp_rank", sortAsc = true;
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
    ["main","matrix","fixtures","team"].forEach(x =>
      $(x+"-view").style.display = (x===v ? "" : "none"));
    $("nav").querySelectorAll("a").forEach(a =>
      a.classList.toggle("active", a.dataset.view===v));
    $("nav-team").style.display = (v==="team") ? "" : "none";
    head(); leagueTabs();
    if (v==="main") renderMain();
    else if (v==="matrix") renderMatrix();
    else if (v==="fixtures") renderFixtures();
    else if (v==="team") renderTeam();
  }

  // ---- Standings odds table ----
  const COLS = [
    ["cur_rank","#"], ["name","Team"], ["played","Pld"], ["cur_pts","Pts"], ["cur_gd","GD"],
    ["proj_pts","Proj"], ["title_pct","Title%"], ["ucl_pct","UCL%"], ["europe_pct","Europe%"],
    ["releg_pct","Rel%"], ["exp_rank","xRank"],
  ];
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
    const L = LEAGUES[cur];
    const maxTitle = Math.max(...L.teams.map(t=>t.title_pct), 1);
    const th = COLS.map(([c,l]) =>
      `<th data-col="${c}" class="${c===sortCol?(sortAsc?'sort-asc':'sort-desc'):''}">${l}</th>`).join("");
    const body = sortedTeams().map(r => {
      const bar = `<span class="bar-track"><span class="bar-fill" style="width:${(r.title_pct/maxTitle*100).toFixed(1)}%"></span></span>`;
      return `<tr>
        <td class="pos">${r.cur_rank}</td>
        <td><a data-team="${r.code}">${esc(r.name)}</a></td>
        <td>${r.played}</td><td>${r.cur_pts}</td><td>${r.cur_gd>0?'+':''}${r.cur_gd}</td>
        <td>${r.proj_pts.toFixed(1)}</td>
        <td>${bar}${pct(r.title_pct)}</td>
        <td class="ucl">${pct(r.ucl_pct)}</td>
        <td class="eur">${pct(r.europe_pct)}</td>
        <td class="rel">${pct(r.releg_pct)}</td>
        <td>${r.exp_rank.toFixed(2)}</td></tr>`;
    }).join("");
    $("main-view").innerHTML =
      `<input type="search" id="flt" placeholder="filter teams…" value="${esc(filter)}">
       <div class="legend"><b>Proj</b> mean final points ·
         <b>Title/UCL/Europe/Rel%</b> chance of finishing 1st / top ${L.league.ucl_slots} /
         top ${L.league.ucl_slots+L.league.europa_slots} / bottom ${L.league.relegation_slots} ·
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
    $("matrix-view").innerHTML =
      `<div class="legend">Each cell = probability (%) a team finishes in that position.
        Darker = more likely. Columns left of the line are Champions League places,
        right are relegation.</div>
       <div class="wrap"><table class="matrix"><thead><tr>${hdr}</tr></thead><tbody>${body}</tbody></table></div>`;
    $("matrix-view").querySelectorAll("a[data-team]").forEach(a =>
      a.onclick = () => { teamCode = a.dataset.team; setView("team"); });
  }

  // ---- Remaining fixtures ----
  function renderFixtures() {
    const L = LEAGUES[cur];
    if (!L.fixtures.length) {
      $("fixtures-view").innerHTML = `<div class="legend">Season complete — no remaining fixtures.</div>`;
      return;
    }
    const body = L.fixtures.map(f => {
      const w=f.win*100, d=f.draw*100, l=f.loss*100;
      return `<tr>
        <td class="pos">${f.match_number}</td>
        <td style="text-align:right">${esc(f.home_name)}</td>
        <td style="text-align:center;color:#8b949e">${f.lam_home.toFixed(1)}–${f.lam_away.toFixed(1)}</td>
        <td style="text-align:left">${esc(f.away_name)}</td>
        <td style="text-align:center"><span class="wdl">
          <span class="w" style="width:${w}%"></span><span class="d" style="width:${d}%"></span>
          <span class="l" style="width:${l}%"></span></span></td>
        <td>${w.toFixed(0)}%</td><td class="pos">${d.toFixed(0)}%</td><td>${l.toFixed(0)}%</td></tr>`;
    }).join("");
    $("fixtures-view").innerHTML =
      `<div class="legend">Remaining fixtures with model expected goals and
        <span style="color:#3fb950">home win</span> /
        <span style="color:#8b949e">draw</span> /
        <span style="color:#f85149">away win</span> probabilities.</div>
       <div class="wrap"><table><thead><tr><th>#</th><th style="text-align:right">Home</th>
         <th style="text-align:center">xG</th><th style="text-align:left">Away</th>
         <th style="text-align:center">W / D / L</th><th>H</th><th>D</th><th>A</th></tr></thead>
         <tbody>${body}</tbody></table></div>`;
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
