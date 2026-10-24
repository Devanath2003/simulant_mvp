/* Simulant dashboard: hash routes  #/  ·  #/new  ·  #/run/<id>  ·  #/run/<id>/report */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const view = $("#view");
const SEV = ["critical", "high", "medium", "low"];
const VITALS = ["usability", "accessibility", "performance", "functional", "integrity"];
const SOURCE = { sensor: "Measured", oracle: "Rule check", llm: "AI-observed" };
const STATUS = { success: "Completed", abandoned: "Abandoned", failure: "Did not finish", error: "Error", running: "Running", planning: "Planning", done: "Done", failed: "Failed" };
let PERSONAS = {};
let socket = null;

// ---------------------------------------------------------------- helpers
const host = u => { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; } };
const pathOf = u => { try { const x = new URL(u); return x.host.replace(/^www\./, "") + (x.pathname === "/" ? "" : x.pathname); } catch { return u || ""; } };
const short = name => (name || "").split(" - ")[0];
// our navigator's own mistakes (bad element id, malformed action): not something the site did to the user
const navErr = t => t.agent_error ?? (/^FAILED:/.test(t.result || "") && !/covering|disabled/.test(t.result));
const role = name => (name || "").split(" - ")[1] || "";
const band = v => v >= 80 ? "" : v >= 60 ? "warn" : "bad";
const bandWord = v => v >= 80 ? "Healthy" : v >= 60 ? "Needs work" : "At risk";
const meter = (v, cls = "") => `<div class="meter ${band(v)} ${cls}" role="img" aria-label="${v} out of 100"><i style="width:${Math.max(2, v)}%"></i></div>`;
const statusTag = s => `<span class="tag st-${esc(s)}"><span class="dot"></span>${esc(STATUS[s] || s)}</span>`;
const sevTag = s => `<span class="tag sev-${s}"><span class="dot"></span>${s}</span>`;
function when(runId) {
  const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)/.exec(runId || "");
  if (!m) return "";
  const d = new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]), s = (Date.now() - d) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " · " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 5200); }
function zoom(src) { const lb = $("#lightbox"); $("img", lb).src = src; lb.classList.add("show"); }
$("#lightbox").onclick = () => $("#lightbox").classList.remove("show");
async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
async function loadPersonas() {
  if (Object.keys(PERSONAS).length) return;
  const order = ["senior", "mobile_3g", "deal_hunter"];
  (await api("/api/personas")).sort((a, b) => (order.indexOf(a.id) + 1 || 99) - (order.indexOf(b.id) + 1 || 99)).forEach(p => PERSONAS[p.id] = p);
}
const pName = id => PERSONAS[id]?.name || id;
const deviceText = p => !p ? "" : p.device === "desktop" ? (p.zoom > 1 ? `Desktop · ${Math.round(p.zoom * 100)}% zoom` : "Desktop") : p.device;
const netText = p => !p ? "" : p.network === "slow_3g" ? "Slow 3G" : "Normal network";

// ---------------------------------------------------------------- router
async function route() {
  if (socket) { socket.close(); socket = null; }
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  $$(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === (parts[0] === "new" ? "new" : parts[0] ? "" : "overview")));
  window.scrollTo(0, 0);
  try {
    await loadPersonas();
    if (parts[0] === "new") return renderNew();
    if (parts[0] === "run" && parts[1]) return renderRun(parts[1], parts[2] === "report" ? "report" : "live");
    return renderOverview();
  } catch (e) {
    view.innerHTML = `<div class="empty"><h3>Can't reach the Simulant server</h3><p class="muted">${esc(e.message)}</p><p class="muted">Start it with <code>python serve.py</code></p></div>`;
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- overview
async function renderOverview() {
  const runs = await api("/api/runs");
  const done = runs.filter(r => r.status === "done");
  const avg = done.length ? Math.round(done.reduce((s, r) => s + r.score, 0) / done.length) : null;
  const totalF = done.reduce((s, r) => s + r.findings, 0);
  const crit = done.reduce((s, r) => s + (r.severity?.critical || 0), 0), high = done.reduce((s, r) => s + (r.severity?.high || 0), 0);
  const sites = new Set(runs.map(r => host(r.url))).size;
  view.innerHTML = `
    <div class="page-head spread">
      <div><div class="eyebrow">Dashboard</div><h1 style="margin-top:12px">Overview</h1>
        <p class="graphite">Every simulation you've run, newest first. Open one to replay the journeys or read its report.</p></div>
      <a class="btn btn-blue arrow" href="#/new">New simulation</a>
    </div>
    <section class="kpis" aria-label="Key figures">
      <div class="kpi hero"><div class="label">Average score</div><div class="v">${avg ?? "—"}<span class="muted" style="font-size:22px">${avg != null ? " / 100" : ""}</span></div>
        <div class="sub">${done.length ? `across ${done.length} finished report${done.length > 1 ? "s" : ""}` : "no finished reports yet"}</div>
        ${avg != null ? `<div style="margin-top:16px">${meter(avg)}</div>` : ""}</div>
      <div class="kpi"><div class="label">Simulations</div><div class="v">${runs.length}</div><div class="sub">${runs.filter(r => r.status === "failed").length} failed to start or crashed</div></div>
      <div class="kpi"><div class="label">Findings</div><div class="v">${totalF}</div><div class="sub">${crit} critical · ${high} high</div></div>
      <div class="kpi"><div class="label">Sites tested</div><div class="v">${sites}</div><div class="sub">${esc([...new Set(runs.map(r => host(r.url)))].slice(0, 2).join(", "))}</div></div>
    </section>
    <section style="margin-top:64px">
      <div class="spread" style="margin-bottom:20px"><h2 class="h-sm">Run history</h2><span class="label">${runs.length} run${runs.length === 1 ? "" : "s"}</span></div>
      ${runs.length ? `<table class="table">
        <thead><tr><th>Site &amp; task</th><th>Simulated users</th><th>Score</th><th class="num">Findings</th><th>When</th><th></th></tr></thead>
        <tbody>${runs.map(r => `
          <tr class="click" data-href="#/run/${r.run_id}${r.status === "done" ? "/report" : ""}">
            <td><div class="site">${esc(host(r.url))}</div><div class="muted" style="font-size:13px;margin-top:6px;max-width:420px">${esc(r.goal || "Main flow, chosen by each persona")}</div></td>
            <td><div class="outcomes">${(r.personas || []).map(p => {
              const st = r.outcomes?.[p] || (r.status === "running" ? "running" : r.status === "failed" ? "error" : "");
              return `<span class="tag st-${st}" title="${esc(STATUS[st] || st)}"><span class="dot"></span>${esc(short(pName(p)))}</span>`;
            }).join("")}</div></td>
            <td>${r.score != null ? `<span style="font-weight:500">${r.score}</span><span class="mini-meter">${meter(r.score)}</span>` : statusTag(r.status)}</td>
            <td class="num">${r.findings != null ? `${r.findings}<div class="muted" style="font-size:12px;margin-top:4px">${r.severity.critical} crit · ${r.severity.high} high</div>` : "—"}</td>
            <td class="muted">${esc(when(r.run_id))}</td>
            <td style="text-align:right"><span class="link-arrow">Open</span></td>
          </tr>`).join("")}</tbody></table>`
      : `<div class="empty"><h3>No simulations yet</h3><p class="graphite" style="max-width:520px">Point three simulated users at a site and give them a task. The first run takes about a minute and a half.</p><a class="btn btn-blue arrow" href="#/new">Start the first one</a></div>`}
    </section>`;
  $$("tr.click").forEach(tr => tr.onclick = () => location.hash = tr.dataset.href);
}

// ---------------------------------------------------------------- new simulation
function renderNew() {
  const ids = Object.keys(PERSONAS);
  view.innerHTML = `
    <div class="page-head"><div class="eyebrow">New simulation</div><h1 style="margin-top:12px">Who should try your site today?</h1>
      <p class="graphite" style="max-width:720px">Give a URL and, if you like, a task in plain English. Each persona plans its own path, runs in its own browser, and reports what got in its way.</p></div>
    <form id="newForm" style="display:grid;gap:40px">
      <section class="card" style="display:grid;gap:24px">
        <div class="grid-2" style="gap:24px">
          <label class="field"><span class="label">Site URL</span><input class="input" id="url" type="url" required placeholder="https://staging.your-product.com"></label>
          <label class="field"><span class="label">Task <span style="text-transform:none">(optional)</span></span><input class="input" id="goal" placeholder="e.g. Create an account and add a phone case to the cart"></label>
        </div>
        <p class="muted" style="margin:0;font-size:13px">No task? Each persona picks the site's main flow, for a shop: choosing a product and checking out.</p>
      </section>
      <section>
        <div class="spread" style="margin-bottom:20px"><h2 class="h-sm">Simulated users</h2><span class="label" id="pcount"></span></div>
        <div class="choose">${ids.map(id => {
          const p = PERSONAS[id];
          return `<div class="pcard on" data-id="${id}" role="checkbox" aria-checked="true" tabindex="0">
            <span class="tick">✓</span>
            <div><div class="label">${p.engine === "integrity" ? "Integrity engine" : "Empathy engine"}</div><h3 style="margin-top:8px">${esc(short(p.name))}</h3>
              <div class="graphite" style="font-size:14px;margin-top:6px">${esc(role(p.name))}</div></div>
            <p class="graphite" style="margin:0;font-size:14px">${esc(p.description)}</p>
            <dl class="spec"><dt>Device</dt><dd>${esc(deviceText(p))}</dd><dt>Network</dt><dd>${esc(netText(p))}</dd>
              <dt>Patience</dt><dd>${p.patience_s} s${p.rage_click ? ", then re-taps" : ""}</dd><dt>Gives up</dt><dd>${p.abandon_at >= 100 ? "Never" : "At frustration " + p.abandon_at}</dd></dl>
          </div>`; }).join("")}</div>
      </section>
      <section class="card" style="display:grid;gap:20px">
        <label class="check hidden" id="authRow"><input type="checkbox" id="auth"><span>I own this site or I'm authorised to test it. The Deal-Hunter will deliberately try to abuse its business rules — coupons, quantities, prices, payment flow.</span></label>
        <div class="spread" style="align-items:center">
          <span class="muted" id="formMsg" style="font-size:14px">Three personas take about a minute and a half.</span>
          <button class="btn btn-blue arrow" id="go" type="submit">Start simulation</button>
        </div>
      </section>
    </form>`;
  const sync = () => {
    const on = $$(".pcard.on").map(c => c.dataset.id);
    $("#pcount").textContent = `${on.length} selected`;
    $("#authRow").classList.toggle("hidden", !on.some(id => PERSONAS[id].engine === "integrity"));
    $("#go").disabled = !on.length;
  };
  $$(".pcard").forEach(c => {
    const flip = () => { c.classList.toggle("on"); c.setAttribute("aria-checked", c.classList.contains("on")); sync(); };
    c.onclick = flip; c.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
  });
  sync();
  $("#newForm").onsubmit = async ev => {
    ev.preventDefault();
    const personas = $$(".pcard.on").map(c => c.dataset.id);
    $("#go").disabled = true; $("#formMsg").textContent = "Checking the site is reachable…";
    try {
      const j = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: $("#url").value.trim(), goal: $("#goal").value.trim(), personas, authorized: $("#auth").checked }) });
      location.hash = "#/run/" + j.run_id;
    } catch (e) {
      $("#formMsg").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; $("#go").disabled = false;
    }
  };
}

// ---------------------------------------------------------------- run: live + report
function renderRun(id, tab) {
  const S = { id, tab, personas: [], lanes: {}, report: null, status: "running", url: "", goal: "", t: 0, ended: false };
  view.innerHTML = `
    <div class="page-head spread">
      <div style="min-width:0"><div class="eyebrow">Simulation · ${esc(id)}</div><h1 style="margin-top:12px" id="runSite">Connecting…</h1>
        <p class="graphite" id="runGoal" style="max-width:820px"></p></div>
      <div class="row" id="runStatus">${statusTag("running")}<span class="label" id="clock"></span></div>
    </div>
    <nav class="tabs" style="margin:0 0 40px;margin-left:0" id="runTabs">
      <a href="#/run/${id}" data-t="live">Live journeys</a><a href="#/run/${id}/report" data-t="report" id="repTab">Report</a>
    </nav>
    <div id="liveView"><div class="lanes" id="lanes"></div><p class="muted" id="runNote" style="margin-top:24px;font-size:13px"></p></div>
    <div id="reportView" class="hidden"></div>`;
  const showTab = t => {
    S.tab = t;
    $$("#runTabs a").forEach(a => a.classList.toggle("on", a.dataset.t === t));
    $("#liveView").classList.toggle("hidden", t !== "live");
    $("#reportView").classList.toggle("hidden", t !== "report");
    if (t === "report") renderReport(S);
  };
  showTab(tab);
  // finished runs: fetch the report straight away (the event replay below rebuilds the lanes)
  api(`/api/runs/${id}/report`).then(r => { S.report = r; if (S.tab === "report") renderReport(S); }).catch(() => {});
  socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/" + id);
  socket.onmessage = m => onEvent(S, JSON.parse(m.data));
  socket.onclose = () => { if (!S.ended && S.status === "running") $("#runNote").textContent = "Connection closed. Reload to reconnect."; };
}

function lane(S, pid) {
  if (S.lanes[pid]) return S.lanes[pid];
  const p = PERSONAS[pid] || { name: pid };
  const div = document.createElement("article");
  div.className = "lane";
  div.innerHTML = `
    <div class="lane-head"><div class="row" style="gap:14px;flex-wrap:nowrap"><span class="avatar sm av-${esc(pid)}">${esc(short(p.name).replace(/^The /, "")[0] || "?")}</span>
      <div><h3>${esc(short(p.name))}</h3><div class="label" style="margin-top:6px">${esc(role(p.name))}</div></div></div><span class="st">${statusTag("planning")}</span></div>
    <div class="row" style="gap:8px"><span class="tag">${esc(deviceText(p))}</span><span class="tag">${esc(netText(p))}</span><span class="tag">${p.engine === "integrity" ? "Integrity" : "Empathy"}</span></div>
    <p class="graphite goal" style="margin:0;font-size:13px">Planning a goal…</p>
    <div class="screen ${p.device && p.device !== "desktop" ? "phone" : ""}"><span class="ph">Waiting for the first screen</span></div>
    <div class="now waiting">Opening the site…</div>
    <div class="frus"><div class="spread" style="align-items:baseline"><span class="label">Frustration</span><span class="label fval">0 / ${p.abandon_at >= 100 ? "∞" : p.abandon_at}</span></div><div class="meter"><i style="width:0%"></i></div></div>
    <div class="feed" aria-live="polite"></div>`;
  $("#lanes").appendChild(div);
  return (S.lanes[pid] = { el: div, p, findings: 0 });
}
function feed(L, html) { const f = $(".feed", L.el); f.insertAdjacentHTML("beforeend", html); f.scrollTop = f.scrollHeight; }
function setFrustration(L, v) {
  const lim = L.p.abandon_at >= 100 ? 100 : L.p.abandon_at, pct = Math.min(100, Math.round(v / lim * 100));
  const m = $(".frus .meter", L.el);
  m.className = "meter " + (pct >= 75 ? "bad" : pct >= 40 ? "warn" : "");
  $("i", m).style.width = Math.max(1, pct) + "%";
  $(".fval", L.el).textContent = `${v} / ${L.p.abandon_at >= 100 ? "∞" : L.p.abandon_at}`;
}

function onEvent(S, ev) {
  const L = ev.persona ? lane(S, ev.persona) : null;
  if (ev.t) { S.t = ev.t; $("#clock").textContent = `${Math.round(ev.t)} s`; }
  switch (ev.type) {
    case "run_start":
      S.url = ev.url; S.goal = ev.goal;
      $("#runSite").textContent = host(ev.url);
      $("#runGoal").textContent = ev.goal ? `Task — ${ev.goal}` : "Task — the site's main flow, chosen by each persona";
      ev.personas.forEach(p => lane(S, p.id));
      break;
    case "persona_start":
      $(".goal", L.el).innerHTML = `<b style="font-weight:500">Goal</b> — ${esc(ev.goal)}`;
      $(".st", L.el).innerHTML = statusTag("running");
      break;
    case "screen": {
      const sc = $(".screen", L.el);
      sc.innerHTML = `<img alt="Screen at step ${ev.step}" src="/runs-files/${S.id}/${ev.shot}"><span class="stamp tag">#${ev.step} · ${esc(new URL(ev.url).pathname)}</span>`;
      $("img", sc).onclick = e => zoom(e.target.src);
      break;
    }
    case "thought":
      $(".now", L.el).classList.remove("waiting");
      $(".now", L.el).textContent = ev.thought + "”";
      feed(L, `<div class="st"><span class="n">#${String(ev.step).padStart(2, "0")}</span><span>${esc(ev.action)}</span></div>`);
      setFrustration(L, ev.frustration);
      break;
    case "action":
      feed(L, ev.agent_error
        ? `<div class="st"><span></span><span class="res">↺ navigator retry (not a site issue)</span></div>`
        : `<div class="st"><span></span><span class="res">→ ${esc(ev.result)}${ev.rage_clicks ? ` · re-tapped ${ev.rage_clicks}×` : ""}</span></div>`);
      setFrustration(L, ev.frustration);
      break;
    case "finding":
      L.findings++;
      feed(L, `<div class="fd sev-${ev.finding.severity}"><span class="dot"></span><span>${esc(ev.finding.title)}</span></div>`);
      break;
    case "persona_end":
      $(".st", L.el).innerHTML = statusTag(ev.status);
      if (ev.reason) { const n = $(".now", L.el); n.classList.add("waiting"); n.textContent = ev.reason; }
      break;
    case "log":
      if (L) feed(L, `<div class="st"><span></span><span class="res">· ${esc(ev.msg)}</span></div>`);
      break;
    case "report":
      S.report = ev.report;
      if (S.tab === "report") renderReport(S);
      else $("#runNote").innerHTML = `The report is ready — <a href="#/run/${S.id}/report">read it</a>.`;
      break;
    case "error":
      S.status = "failed"; $("#runStatus").innerHTML = statusTag("failed");
      $("#runNote").innerHTML = `<span style="color:var(--bad)">${esc(ev.message)}</span>`;
      break;
    case "run_end":
      S.ended = true;
      if (ev.status !== "error") { S.status = "done"; $("#runStatus").innerHTML = statusTag("done") + `<span class="label">${Math.round(S.t)} s</span>`; }
      break;
  }
}

// ---------------------------------------------------------------- report
// how the run itself went: navigator retries and model health. Kept apart from findings, never part of the score.
function diagnostics(r) {
  const errs = r.journeys.flatMap(j => j.timeline.filter(navErr).map(t => ({ ...t, who: short(pName(j.persona)) })));
  const models = Object.entries(r.llm_usage || {}).filter(([, v]) => v.calls);
  if (!errs.length && !models.length) return "";
  return `<section class="diag" style="margin-top:80px"><details>
    <summary><span class="h-xs serif">Run diagnostics</span><span class="label">Not site issues · not part of the score · ${errs.length} navigator retries</span></summary>
    ${errs.length ? `<div class="label" style="margin:24px 0 8px">Navigator retries</div>
      <div class="timeline">${errs.map(t => `<div class="tl"><span class="n">#${t.step}</span><span>${esc(t.who)} · ${esc(t.desc)}</span><span class="muted">${esc(t.result.replace(/^FAILED:\s*/, ""))}</span><span class="f"></span></div>`).join("")}</div>` : ""}
    ${models.length ? `<div class="label" style="margin:28px 0 8px">Model calls</div>
      <div class="timeline">${models.map(([k, v]) => `<div class="tl"><span class="n"></span><span>${esc(k)}</span><span class="muted">${v.calls} calls${v.fails ? ` · ${v.fails} failed` : ""}${(v.recent_errors || [])[0] ? ` · last: ${esc(String(v.recent_errors[0]).slice(0, 90))}` : ""}</span><span class="f"></span></div>`).join("")}</div>` : ""}
  </details></section>`;
}

function renderReport(S) {
  const box = $("#reportView");
  const r = S.report;
  if (!r) {
    box.innerHTML = `<div class="empty"><h3>The report is being written</h3><p class="graphite">It appears here as soon as every persona has finished. You can watch them on the live tab meanwhile.</p></div>`;
    return;
  }
  const sc = r.score, F = r.findings;
  // the panel's colour and word follow the weakest vital: an average of 83 must not read "healthy" while one vital is at 62
  const weak = Object.entries(sc.vitals).sort((a, b) => a[1] - b[1])[0] || ["", sc.overall];
  const advice = (r.release_advice || "").split(/\s[-–—]\s/);
  const count = (key, val) => F.filter(f => key === "sev" ? f.severity === val : key === "vital" ? f.vital === val : f.source === val).length;
  box.innerHTML = `
    <section class="report-head">
      <div><h2 class="h-lg" style="margin:0 0 14px">Liability report</h2>
        <div class="row" style="gap:10px"><span class="tag tag-solid">${esc((advice[0] || "").toUpperCase())}</span><span class="graphite" style="font-size:14px">${esc(advice.slice(1).join(" — "))}</span></div></div>
      <div class="row"><a class="btn" href="#/run/${S.id}">Replay journeys</a><a class="btn btn-blue arrow" href="/api/runs/${S.id}/report.pdf" download>Download PDF</a></div>
    </section>
    <section class="score-panel" style="margin-top:40px" aria-label="Scores">
      <div class="score-hero ${band(weak[1])}"><div class="label">Overall score</div>
        <div class="v">${sc.overall}<small> / 100</small></div>
        <div><span class="tag"><span class="dot" style="background:var(--${band(weak[1]) || "good"})"></span>${bandWord(weak[1])}${band(weak[1]) ? ` · ${weak[0]} ${weak[1]}` : ""}</span>
        <p class="muted" style="font-size:12px;margin:14px 0 0">${r.journeys.length} simulated users · ${r.elapsed_s} s · ${F.length} findings</p></div></div>
      <div class="vitals">${VITALS.map(v => `<div class="vital"><span style="text-transform:capitalize">${v}</span>${meter(sc.vitals[v])}<span class="val">${sc.vitals[v]}</span></div>`).join("")}</div>
    </section>
    <section class="summary-grid" style="margin-top:16px">
      <div class="card"><div class="label" style="margin-bottom:16px">Summary</div><p style="font:400 26px/1.3 var(--serif);letter-spacing:-0.015em;color:var(--off-black);margin:0">${esc(r.summary)}</p></div>
      <div class="card card-clay risks"><div class="label" style="margin-bottom:16px">Top risks</div><ol>${(r.top_risks || []).map(t => `<li>${esc(t)}</li>`).join("")}</ol></div>
    </section>
    <section style="margin-top:80px" class="journeys">
      <h2 class="h-sm" style="margin-bottom:8px">Journeys</h2>
      ${r.journeys.map(j => {
        const p = PERSONAS[j.persona] || { name: j.persona };
        return `<details><summary><span class="who"><span class="avatar sm av-${esc(j.persona)}">${esc(short(p.name).replace(/^The /, "")[0] || "?")}</span>
              <span class="t">${esc(short(p.name))} <span class="muted" style="font-size:18px">— ${esc(role(p.name))}</span></span></span>
            <span class="row" style="gap:8px">${statusTag(j.status)}<span class="tag">${j.steps} steps</span><span class="tag">Frustration ${j.frustration}</span></span></summary>
          <p class="graphite" style="margin:18px 0 6px;font-size:14px"><b style="font-weight:500">Goal</b> — ${esc(j.goal)}</p>
          ${j.reason ? `<p class="graphite" style="margin:0;font-size:14px"><b style="font-weight:500">Ended</b> — ${esc(j.reason)}</p>` : ""}
          <div class="timeline">${j.timeline.filter(t => !navErr(t)).map(t => `<div class="tl"><span class="n">#${t.step}</span><span>${esc(t.desc)}</span><span class="muted">${esc(t.result)}</span><span class="f">F ${t.frustration}</span></div>`).join("")}</div>
          ${j.timeline.some(navErr) ? `<p class="muted" style="font-size:12px;margin:12px 0 0">${j.timeline.filter(navErr).length} navigator retries hidden: our agent's own mistakes, not the site's. Listed under Run diagnostics.</p>` : ""}
        </details>`; }).join("")}
    </section>
    <section style="margin-top:80px">
      <div class="spread" style="margin-bottom:20px"><h2 class="h-sm">Findings <span class="muted">(${F.length})</span></h2>
        <div class="legend-note"><span>Measured — browser checks</span><span>Rule check — business-rule judge</span><span>AI-observed — vision opinion</span></div></div>
      <div class="filters" id="filters">
        <button class="chip on" data-k="all">All <span class="c">${F.length}</span></button>
        <span class="grp">Severity</span>
        ${SEV.filter(s => count("sev", s)).map(s => `<button class="chip" data-k="sev" data-v="${s}"><span class="dot sev-${s}"></span>${s} <span class="c">${count("sev", s)}</span></button>`).join("")}
        <span class="grp">Vital</span>
        ${VITALS.filter(v => count("vital", v)).map(v => `<button class="chip" data-k="vital" data-v="${v}">${v} <span class="c">${count("vital", v)}</span></button>`).join("")}
        <span class="grp">Source</span>
        ${Object.keys(SOURCE).filter(s => count("src", s)).map(s => `<button class="chip" data-k="src" data-v="${s}">${SOURCE[s]} <span class="c">${count("src", s)}</span></button>`).join("")}
      </div>
      <div id="findingList" style="margin-top:24px"></div>
    </section>
    ${diagnostics(r)}`;
  const list = $("#findingList");
  const draw = (k, v) => {
    const shown = F.filter(f => k === "all" || (k === "sev" ? f.severity === v : k === "vital" ? f.vital === v : f.source === v));
    list.innerHTML = shown.map(f => {
      const shot = f.screenshot && !/^chrome-error:/.test(f.url || "");    // a browser error page is not evidence
      const who = f.personas.map(p => short(pName(p))).join(", ");
      return `
      <article class="finding${shot ? "" : " no-shot"}">
        <header class="fhead"><span class="sev-badge sev-${f.severity}"><span class="dot"></span>${f.severity}</span>
          <span>${esc(f.id)}</span><span>${esc(f.vital)} · ${SOURCE[f.source] || esc(f.source)} · ${esc(who)}</span>
          ${f.url && !/^chrome-error:/.test(f.url) ? `<span class="where" title="${esc(f.url)}">${esc(pathOf(f.url))}</span>` : ""}</header>
        <div class="fbody">
          <div>
            <h4>${esc(f.title)}</h4>
            ${f.detail ? `<div class="blk"><span class="label">Evidence</span><span class="txt">${esc(f.detail)}</span></div>` : ""}
            ${f.persona_quote ? `<div class="blk"><span class="label">In their words</span><span class="say">“${esc(f.persona_quote)}”</span></div>` : ""}
            ${f.suggested_fix ? `<div class="blk fix"><span class="label">Suggested fix</span><span class="txt">${esc(f.suggested_fix)}</span></div>` : ""}
          </div>
          ${shot ? `<div><div class="shot"><img loading="lazy" alt="Screenshot for ${esc(f.id)}" src="/runs-files/${S.id}/${f.screenshot}"></div>
            <div class="shot-cap">${f.step != null ? `Step ${f.step} · ` : ""}click to enlarge</div></div>` : ""}
        </div>
      </article>`; }).join("") || `<p class="muted">Nothing in this filter.</p>`;
    $$(".shot img", list).forEach(i => i.onclick = () => zoom(i.src));
  };
  $$("#filters .chip").forEach(c => c.onclick = () => { $$("#filters .chip").forEach(x => x.classList.remove("on")); c.classList.add("on"); draw(c.dataset.k, c.dataset.v); });
  draw("all");
}

route();
