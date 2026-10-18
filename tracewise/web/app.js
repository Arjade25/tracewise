const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const COLORS = {
  Function: "--c-fn", Class: "--c-class", Module: "--c-module", Person: "--c-person", Commit: "--c-commit",
  PR: "--c-pr", Issue: "--c-issue", Release: "--c-release", Test: "--c-test",
  Session: "--c-module", Turn: "--c-commit", Fact: "--c-release", Entity: "--c-fn", Task: "--c-pr",
  Agent: "--c-person", Procedure: "--c-class", Tool: "--c-test",
};
// [question, tracks it demonstrates]. T1 Agents That Act on Connected Data, T2 Agent Memory and Coordination, T3 Company Brain.
const EXAMPLES = [
  ["What breaks if I change full_dispatch_request?", "T1"],
  ["Why does Blueprint.register look the way it does?", "T3"],
  ["Who should I ask about save_session?", "T3"],
  ["Which tests cover url_for?", "T1"],
  ["How is wsgi_app connected to handle_user_exception?", "T1"],
  ["What are the riskiest parts of the codebase?", "T1"],
  ["Review a change to url_for and Flask.make_response", "T1 · T3"],
  ["What do you remember about full_dispatch_request?", "T2"],
];
const cleanUser = (u) => String(u || "").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40);
const state = { session: null, repo: "flask", user: "guest" };
try { const u = cleanUser(localStorage.getItem("tw-user")); if (u) state.user = u; } catch {}

const md = (s) => DOMPurify.sanitize(marked.parse(s || ""));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const user = () => state.user || "guest";

async function api(path, body, method) {
  const r = await fetch(path, body ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
                                   : method ? { method } : {});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ---------------------------------------------------------------- graph
// Labels are shown only for nodes that carry the story (the target, people, PRs, releases,
// agents, entities). Everything else gets its label on hover, so dense graphs stay readable.
const LABELLED = new Set(["Person", "PR", "Release", "Issue", "Class", "Module", "Agent", "Entity", "Procedure"]);
const MAX_TESTS = 6;
const clip = (s, n = 26) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function makeGraph(el) {
  const cy = cytoscape({
    container: el, wheelSensitivity: 0.3, minZoom: 0.15, maxZoom: 3,
    style: [
      { selector: "node", style: {
        "background-color": "data(color)", label: "", "font-family": "IBM Plex Mono", "font-size": 10,
        color: css("--ink"), "text-valign": "bottom", "text-margin-y": 4, width: 14, height: 14,
        "text-background-color": css("--panel"), "text-background-opacity": 0.85, "text-background-padding": 2,
        "text-background-shape": "roundrectangle", "min-zoomed-font-size": 3 } },
      { selector: "node[?show]", style: { label: "data(short)" } },
      { selector: "node[role = 'target'], node[role = 'changed']", style: {
        label: "data(short)", width: 26, height: 26, "border-width": 3, "border-color": css("--accent"),
        "font-weight": 600, "font-size": 12, "z-index": 10 } },
      { selector: "node[label = 'More']", style: {
        shape: "round-rectangle", width: "label", height: 18, padding: 6, label: "data(short)",
        "text-valign": "center", "text-margin-y": 0, "background-color": css("--code"), color: css("--muted"),
        "border-width": 1, "border-color": css("--line"), "text-background-opacity": 0 } },
      { selector: "node.hover", style: { label: "data(name)", "z-index": 20, "font-size": 11 } },
      { selector: "edge", style: {
        width: 1.2, "line-color": css("--line"), "target-arrow-color": css("--muted"), "target-arrow-shape": "triangle",
        "arrow-scale": 0.7, "curve-style": "bezier", label: "", "font-size": 8, "font-family": "IBM Plex Mono",
        color: css("--muted"), "text-rotation": "autorotate", "text-background-color": css("--panel"),
        "text-background-opacity": 1, "text-background-padding": 1, "min-zoomed-font-size": 3 } },
      { selector: "edge.labelled, edge.hover", style: { label: "data(type)" } },
      { selector: "edge[type = 'CALLS']", style: { "line-color": css("--c-fn"), opacity: 0.7 } },
      { selector: "edge[type = 'COVERS']", style: { "line-color": css("--c-test"), "line-style": "dashed", opacity: 0.45 } },
      { selector: ".faded", style: { opacity: 0.15 } },
    ],
  });
  // Hover a node: show its full name and highlight its neighbourhood.
  cy.on("mouseover", "node", (e) => {
    const n = e.target;
    n.addClass("hover");
    n.connectedEdges().addClass("hover");
    cy.elements().not(n.closedNeighborhood()).addClass("faded");
  });
  cy.on("mouseout", "node", (e) => {
    e.target.removeClass("hover");
    cy.elements().removeClass("faded").edges().removeClass("hover");
  });
  return cy;
}

function draw(cy, ev) {
  cy.elements().remove();
  // Collapse long lists of tests into one "+N more tests" node per target.
  let nodes = ev.nodes, edges = ev.edges;
  const tests = nodes.filter((n) => n.label === "Test");
  if (tests.length > MAX_TESTS) {
    const keep = new Set(tests.slice(0, MAX_TESTS).map((n) => n.id));
    const dropped = new Set(tests.filter((n) => !keep.has(n.id)).map((n) => n.id));
    const targets = new Set(edges.filter((e) => dropped.has(e.source)).map((e) => e.target));
    nodes = nodes.filter((n) => !dropped.has(n.id));
    edges = edges.filter((e) => !dropped.has(e.source) && !dropped.has(e.target));
    nodes.push({ id: "__more_tests", label: "More", name: `+${dropped.size} more tests`, role: "" });
    for (const t of targets) edges.push({ source: "__more_tests", target: t, type: "COVERS" });
  }
  const ids = new Set();
  const els = [];
  for (const n of nodes) {
    ids.add(n.id);
    const name = String(n.name);
    els.push({ data: {
      id: n.id, name, short: clip(name, n.label === "More" ? 40 : 26), role: n.role || "", label: n.label,
      show: LABELLED.has(n.label) || ["target", "changed", "hotspot"].includes(n.role) || nodes.length <= 10,
      color: css(COLORS[n.label] || "--c-commit") } });
  }
  const edgeEls = [];
  for (const e of edges) {
    if (ids.has(e.source) && ids.has(e.target))
      edgeEls.push({ data: { id: `${e.source}>${e.target}>${e.type}`, source: e.source, target: e.target, type: e.type } });
  }
  cy.add(els.concat(edgeEls));
  if (edgeEls.length <= 14) cy.edges().addClass("labelled");
  const n = els.length;
  const layout = n <= 12
    ? { name: "breadthfirst", directed: true, spacingFactor: 1.6, padding: 40, avoidOverlap: true }
    : { name: "cose", animate: false, randomize: false, padding: 40, nodeRepulsion: () => 90000,
        idealEdgeLength: () => 70, nodeOverlap: 30, componentSpacing: 60, gravity: 0.6, numIter: 2500 };
  cy.layout(layout).run();
  cy.fit(undefined, 40);
  if (cy.zoom() < 0.75) { cy.zoom(0.75); cy.center(cy.nodes("[role = 'target'], [role = 'changed']").length ? cy.nodes("[role = 'target'], [role = 'changed']") : undefined); }
  return [...new Set(nodes.map((x) => x.label).filter((l) => l !== "More"))];
}
function legend(labels) {
  $("#legend").innerHTML = labels.map((l) => `<span><i style="background:${css(COLORS[l] || "--c-commit")}"></i>${l}</span>`).join("");
}
const cyAsk = makeGraph($("#graph"));
let cyMem = null;

// ----------------------------------------------------------------- ask
function showCypher(calls) {
  const qs = calls.flatMap((c) => c.cypher.map((q) => ({ tool: c.tool, q })));
  $("#qcount").textContent = `(${qs.length} queries)`;
  $("#cypher-body").innerHTML = qs.map((x) => `<pre><code>// ${esc(x.tool)}\n${esc(x.q)}</code></pre>`).join("");
}
// ----------------------------------------------------------- conversations
const INTRO_HTML = $("#thread").innerHTML;
function newSid() {
  const a = new Uint8Array(8);
  (window.crypto || {}).getRandomValues ? crypto.getRandomValues(a) : a.forEach((_, i) => (a[i] = Math.random() * 256));
  return [...a].map((x) => x.toString(16).padStart(2, "0")).join("");
}
function resetEvidence() {
  cyAsk.elements().remove();
  $("#legend").innerHTML = "";
  $("#cypher-body").innerHTML = "";
  $("#qcount").textContent = "";
}
// Flask has hand-written examples; any other repo gets examples built from its own riskiest functions.
const repoExamples = { flask: EXAMPLES };
async function examplesFor(repo) {
  if (repoExamples[repo]) return repoExamples[repo];
  const h = await api(`/api/tool/${encodeURIComponent(repo)}/hotspots`).catch(() => null);
  const names = (h?.result || []).map((r) => r.qname.split(".").slice(-2).join(".")).filter((n, i, a) => a.indexOf(n) === i);
  const [a, b, c] = names;
  const ex = a ? [
    [`What breaks if I change ${a}?`, "T1"],
    b && [`Why does ${b} look the way it does?`, "T3"],
    [`Who should I ask about ${c || a}?`, "T3"],
    [`Which tests cover ${a}?`, "T1"],
    b && [`How is ${a} connected to ${b}?`, "T1"],
    ["What are the riskiest parts of the codebase?", "T1"],
    [`What do you remember about ${a}?`, "T2"],
  ].filter(Boolean) : [["What are the riskiest parts of the codebase?", "T1"]];
  return (repoExamples[repo] = ex);
}
const TRACKS = { T1: "Track 01 · Agents That Act on Connected Data", T2: "Track 02 · Agent Memory and Coordination", T3: "Track 03 · Company Brain" };
const trackTitle = (t) => t.split(" · ").map((k) => TRACKS[k] || k).join("\n");
function renderIntro() {
  $("#thread").innerHTML = INTRO_HTML;
  const repo = state.repo, sid = state.session;
  examplesFor(repo).then((ex) => {
    if (state.repo !== repo || state.session !== sid || !$("#examples")) return;
    $("#examples").innerHTML = ex.map(([q, t]) =>
      `<button data-q="${esc(q)}">${esc(q)}<span class="trk" title="${esc(trackTitle(t))}">${esc(t)}</span></button>`).join("");
  });
}
// A fresh conversation: new session id, empty thread, empty evidence graph.
function newChat() {
  state.session = newSid();
  renderIntro();
  resetEvidence();
  markActiveHistory();
  $("#q").focus();
}
// Render one answer (live or from history) with its tool chips, and wire the chips to the Cypher drawer.
function renderAnswer(box, r) {
  const calls = r.tool_calls || r.tools || [];
  const memN = r.memory ? (r.memory.facts?.length || 0) + (r.memory.earlier?.length || 0) : 0;
  const nodes = r.evidence?.nodes?.length || 0;
  box.innerHTML = md(r.answer) + `<div class="trace">` +
    calls.map((c, i) => `<button class="chip" data-i="${i}" title="${c.learned ? "added from a learned procedure · " : ""}show Cypher">${c.learned ? "↻ " : ""}${esc(c.tool)} · ${(c.cypher || []).length}q${c.ms != null ? ` · ${c.ms}ms` : ""}</button>`).join("") +
    (memN ? `<span class="chip mem">recalled ${memN} memories</span>` : "") +
    `<span>${nodes} nodes${r.ms ? ` · ${r.ms}ms` : ""}${r.ts ? ` · ${new Date(r.ts).toLocaleString()}` : ""}</span>` +
    (nodes ? `<button class="chip show-graph" title="draw this answer's evidence graph">graph</button>` : "") + `</div>`;
  box.querySelectorAll(".chip[data-i]").forEach((b) => b.addEventListener("click", () => {
    showCypher([calls[+b.dataset.i]]); $("#cypher").open = true;
  }));
  box.querySelector(".show-graph")?.addEventListener("click", () => { legend(draw(cyAsk, r.evidence)); showCypher(calls); });
}
async function ask(question) {
  $(".intro")?.remove();
  const th = $("#thread");
  th.insertAdjacentHTML("beforeend", `<div class="msg-q">${esc(question)}</div>`);
  const box = document.createElement("div");
  box.className = "answer";
  box.innerHTML = `<div class="thinking">walking the graph…</div>`;
  th.appendChild(box);
  th.scrollTop = th.scrollHeight;
  if (!state.session) state.session = newSid();
  const sid = state.session, repo = state.repo;
  try {
    const r = await api("/api/ask", { question, repo, user: user(), session: sid, llm: getLLM() });
    if (state.session !== sid) return;  // the user started another chat while this one was answering
    const mode = r.mode === "llm" && r.llm_source === "browser" ? "llm · your key" : r.mode;
    setBadge(mode, r.llm_error ? "LLM failed: " + friendlyLLMError(r.llm_error) + " Click to change settings." : r.model ? "model: " + r.model : "");
    renderAnswer(box, r);
    legend(draw(cyAsk, r.evidence));
    showCypher(r.tool_calls);
    if (!$("#history").hidden) loadHistory();
  } catch (e) {
    box.innerHTML = `<b>Error:</b> ${esc(e.message)}`;
  }
  // Like a chat app: show the start of the reply, then let the user read down.
  box.previousElementSibling?.scrollIntoView({ block: "start", behavior: "smooth" });
}
$("#ask").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#q").value.trim(); if (q) { $("#q").value = ""; ask(q); } });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask").requestSubmit(); } });
// Delegated, so example buttons keep working after the intro is re-rendered.
$("#thread").addEventListener("click", (e) => { const b = e.target.closest(".examples button"); if (b) ask(b.dataset.q || b.textContent); });

async function openSession(sid) {
  const s = await api(`/api/sessions/${encodeURIComponent(user())}/${sid}`).catch((e) => { alertInline(e.message); return null; });
  if (!s) return;
  if (s.repo !== state.repo && [...$("#repo").options].some((o) => o.value === s.repo)) await loadStats(s.repo);
  const repoGone = ![...$("#repo").options].some((o) => o.value === s.repo);
  state.session = s.id;
  const th = $("#thread");
  th.innerHTML = repoGone ? `<div class="answer muted">This conversation was about <b>${esc(s.repo)}</b>, which has been removed. Add it again to keep asking about it.</div>` : "";
  for (const t of s.turns) {
    th.insertAdjacentHTML("beforeend", `<div class="msg-q">${esc(t.question)}</div>`);
    const box = document.createElement("div");
    box.className = "answer";
    renderAnswer(box, t);
    th.appendChild(box);
  }
  const last = s.turns[s.turns.length - 1];
  if (last) { legend(draw(cyAsk, last.evidence)); showCypher(last.tools); } else resetEvidence();
  th.scrollTop = th.scrollHeight;
  markActiveHistory();
}
function alertInline(msg) { $("#thread").insertAdjacentHTML("beforeend", `<div class="answer"><b>Error:</b> ${esc(msg)}</div>`); }

// In-app confirmation, styled like the rest of the UI (instead of the browser's confirm() popup).
// Resolves true only when the confirm button is pressed; Cancel, ×, Escape or a backdrop click resolve false.
function confirmDialog({ title, message, ok = "OK", danger = false, cancel = true }) {
  const d = $("#confirm");
  $("#confirm-cancel").hidden = !cancel;
  $("#confirm-title").textContent = title;
  $("#confirm-msg").textContent = message;
  const okBtn = $("#confirm-ok");
  okBtn.textContent = ok;
  okBtn.classList.toggle("danger-fill", danger);
  return new Promise((resolve) => {
    d.returnValue = "";
    const done = () => { d.removeEventListener("close", done); d.removeEventListener("click", backdrop); resolve(d.returnValue === "ok"); };
    const backdrop = (e) => { if (e.target === d) d.close(); };
    d.addEventListener("close", done);
    d.addEventListener("click", backdrop);
    d.showModal();
    $("#confirm-cancel").focus();  // a stray Enter shouldn't delete anything
  });
}
$("#confirm-ok").addEventListener("click", () => $("#confirm").close("ok"));
$("#confirm-cancel").addEventListener("click", () => $("#confirm").close());

async function clearChat() {
  const has = $("#thread").querySelector(".msg-q");
  if (!has) { newChat(); return; }
  const ok = await confirmDialog({
    title: "Clear this conversation?",
    message: "Its messages are deleted. What the agent learned from it (facts and procedures) is kept.",
    ok: "Clear", danger: true,
  });
  if (!ok) return;
  await api(`/api/sessions/${encodeURIComponent(user())}/${state.session}`, undefined, "DELETE").catch(() => {});
  newChat();
  if (!$("#history").hidden) loadHistory();
}

// ----------------------------------------------------------------- history
function ago(ts) {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString();
}
async function loadHistory() {
  const all = $("#history-scope").value === "all";
  const list = await api(`/api/sessions/${encodeURIComponent(user())}${all ? "" : `?repo=${encodeURIComponent(state.repo)}`}`).catch(() => []);
  $("#history-list").innerHTML = list.length ? list.map((x) => `
    <li data-sid="${esc(x.id)}">
      <button class="h-open" title="Open this conversation">
        <span class="h-title">${esc(x.title || "Untitled")}</span>
        <span class="h-meta">${all ? `${esc(x.repo)} · ` : ""}${x.turns} message${x.turns === 1 ? "" : "s"} · ${ago(x.last_active)}</span>
      </button>
      <button class="h-del" title="Delete this conversation" aria-label="Delete">×</button>
    </li>`).join("") : `<li class="h-empty">No conversations yet${all ? "" : ` for ${esc(state.repo)}`}.</li>`;
  markActiveHistory();
}
function markActiveHistory() {
  document.querySelectorAll("#history-list li[data-sid]").forEach((li) => li.classList.toggle("active", li.dataset.sid === state.session));
}
$("#history-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-sid]");
  if (!li) return;
  if (e.target.closest(".h-del")) {
    const title = li.querySelector(".h-title")?.textContent || "this conversation";
    const ok = await confirmDialog({
      title: "Delete conversation?",
      message: `“${title}” will be removed from History. What the agent learned from it (facts and procedures) is kept.`,
      ok: "Delete", danger: true,
    });
    if (!ok) return;
    await api(`/api/sessions/${encodeURIComponent(user())}/${li.dataset.sid}`, undefined, "DELETE").catch(() => {});
    if (li.dataset.sid === state.session) newChat();
    loadHistory();
    return;
  }
  await openSession(li.dataset.sid);
  toggleHistory(false);  // the panel overlays the thread; get it out of the way once a chat is open
});
function toggleHistory(force) {
  const h = $("#history");
  h.hidden = force === undefined ? !h.hidden : !force;
  $("#history-btn").classList.toggle("on", !h.hidden);
  if (!h.hidden) loadHistory();
}
$("#new-chat").addEventListener("click", newChat);
$("#clear-chat").addEventListener("click", clearChat);
$("#history-btn").addEventListener("click", () => toggleHistory());
$("#history-close").addEventListener("click", () => toggleHistory(false));
$("#history-scope").addEventListener("change", loadHistory);

// -------------------------------------------------------------- review
$("#rgo").addEventListener("click", async () => {
  const symbols = $("#rsyms").value.split(",").map((s) => s.trim()).filter(Boolean);
  const diff = $("#rdiff").value.trim() || null;
  $("#rout").innerHTML = `<div class="thinking">crew working…</div>`;
  $("#timeline").innerHTML = "";
  try {
    const r = await api("/api/review", { repo: state.repo, user: user(), symbols: diff ? [] : symbols, diff, crew: true });
    $("#rout").innerHTML = md(r.summary) + `<div class="trace"><span>${r.tree.subtasks.length} tasks · ${r.tree.handoffs.length} handoffs · ${r.ms}ms · task ${esc(r.task)}</span></div>`;
    const t0 = r.log.length ? r.log[0].t : 0;
    const lanes = [...new Set(r.tree.subtasks.map((s) => s.agent))];
    $("#timeline").innerHTML =
      `<div class="lane">${lanes.map((a) => `<span class="pill">${esc(a)}</span>`).join("")}</div>` +
      `<div class="tl"><span class="t">+0ms</span><span class="a">coordinator</span><span>created ${r.tree.subtasks.length} tasks in memory:${esc(user())}</span></div>` +
      r.log.map((l) => `<div class="tl"><span class="t">+${Math.round((l.t - t0) * 1000)}ms</span><span class="a">${esc(l.agent)}</span><span>${l.event} <code>${esc(l.function.split(".").slice(-2).join("."))}</code>${l.summary ? " → " + esc(l.summary) : ""}</span></div>`).join("") +
      `<h3>Handoffs (edges in the memory graph)</h3>` +
      r.tree.handoffs.map((h) => `<div class="tl"><span class="t">HANDOFF</span><span class="a">${esc(h.source)} → ${esc(h.target)}</span><span>${esc(h.note)}</span></div>`).join("");
  } catch (e) {
    $("#rout").innerHTML = `<b>Error:</b> ${esc(e.message)}`;
  }
});

// -------------------------------------------------------------- memory
async function loadMemory() {
  const u = user();
  $("#muser").textContent = u;
  const m = await api(`/api/memory/${encodeURIComponent(u)}`);
  $("#mstats").innerHTML = Object.entries(m.stats).map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join("") || "empty — ask something first";
  $("#mfacts").innerHTML = m.facts.map((f) => `<div class="fact ${f.current ? "current" : "old"}"><b>${esc(f.about)}</b> · ${esc(f.kind)}: ${esc(f.text)}<br><small>${new Date(f.ts).toLocaleString()} · ${esc(f.source)}</small></div>`).join("") || "<span class='muted'>no facts yet</span>";
  $("#mprocs").innerHTML = m.procedures.map((p) => `<div class="fact current"><b>${esc(p.intent)}</b> × ${p.uses}: ${p.steps.map(esc).join(" → ")}</div>`).join("") || "<span class='muted'>none yet</span>";
  const g = await api(`/api/memory/${encodeURIComponent(u)}/graph`);
  if (!cyMem) cyMem = makeGraph($("#mgraph"));
  draw(cyMem, g);
}
$("#mref").addEventListener("click", loadMemory);

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "memory") loadMemory();
  if (b.dataset.tab === "ask") cyAsk.resize();
}));
// ---------------------------------------------------------------- users
// The user menu lists everyone with a memory graph, plus "New user…". Switching user refreshes every
// view that shows per-user data: the chat, History, the Memory tab and the last crew review.
const NEW_USER = "__new__";
async function loadUsers() {
  const list = await api("/api/users").catch(() => []);
  const names = [...new Set([...list, user()])].sort((a, b) => a.localeCompare(b));
  $("#user-select").innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("") +
    `<option value="${NEW_USER}">＋ New user…</option>`;
  $("#user-select").value = user();
}
function resetCrew() {
  $("#rout").innerHTML = "";
  $("#timeline").innerHTML = "";
}
function activeTab() { return document.querySelector(".tab.active")?.dataset.tab; }
async function switchUser(name) {
  state.user = name;
  try { localStorage.setItem("tw-user", name); } catch {}
  await loadUsers();
  newChat();
  resetCrew();
  if (!$("#history").hidden) loadHistory();
  if (activeTab() === "memory") loadMemory();
  else $("#muser").textContent = name;
}
$("#user-select").addEventListener("change", () => {
  const v = $("#user-select").value;
  if (v === NEW_USER) { $("#user-select").value = user(); openNewUser(); return; }
  if (v !== user()) switchUser(v);
});
function openNewUser() {
  $("#newuser-name").value = "";
  flash("#newuser-msg", "", "");
  $("#newuser").showModal();
  $("#newuser-name").focus();
}
function createUser() {
  const raw = $("#newuser-name").value.trim(), name = cleanUser(raw);
  if (!name || name !== raw) { flash("#newuser-msg", "Use letters, digits, - or _ (up to 40).", "err"); return; }
  $("#newuser").close();
  if (name !== user()) switchUser(name);
}
$("#newuser-ok").addEventListener("click", createUser);
$("#newuser-cancel").addEventListener("click", () => $("#newuser").close());
$("#newuser-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); createUser(); } });

// Switching repository always starts a new conversation; the old one stays in History.
$("#repo").addEventListener("change", async () => {
  state.repo = $("#repo").value;
  await loadStats();
  newChat();
  resetCrew();
  if (!$("#history").hidden) loadHistory();
});

// Remove the current repository (its graph and clone). Memory and History are kept.
$("#del-repo").addEventListener("click", async () => {
  const name = state.repo;
  if ($("#repo").options.length <= 1) {
    await confirmDialog({ title: "Can't remove the only repository", message: "Add another repository first, then remove this one.", ok: "OK", cancel: false });
    return;
  }
  const extra = name === "flask" ? " Flask's built-in index includes measured test coverage; adding it back from the app won't." : "";
  const ok = await confirmDialog({
    title: `Remove “${name}”?`,
    message: `Its code graph is deleted. Conversations and memory about it are kept, and you can add it again any time.${extra}`,
    ok: "Remove", danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/repos/${encodeURIComponent(name)}`, undefined, "DELETE");
  } catch (e) {
    await confirmDialog({ title: "Couldn't remove it", message: e.message, ok: "OK", cancel: false });
    return;
  }
  await loadStats();
  newChat();
  resetCrew();
  if (!$("#history").hidden) loadHistory();
});

async function loadStats(select) {
  const repos = await api("/api/repos");
  const current = select || $("#repo").value || state.repo;
  $("#repo").innerHTML = repos.map((r) => `<option>${esc(r.name)}</option>`).join("");
  if (repos.some((r) => r.name === current)) $("#repo").value = current;
  state.repo = $("#repo").value || repos[0]?.name;
  const r = repos.find((x) => x.name === state.repo);
  if (!r) return;
  const n = r.nodes;
  $("#stats").innerHTML = ["Function", "Class", "Module", "Commit", "PR", "Person", "Release"]
    .filter((k) => n[k]).map((k) => `<span>${k} <b>${n[k].toLocaleString()}</b></span>`).join("") +
    `<span>edges <b>${r.edges.toLocaleString()}</b></span>`;
}

// ------------------------------------------------------------ mode badge
function setBadge(mode, title) {
  const b = $("#mode");
  b.textContent = mode;
  b.classList.toggle("llm", mode.startsWith("llm"));
  b.title = title || "";
}

// Show the configured mode on load, before any question is asked.
async function loadStatus() {
  const mine = getLLM();
  if (mine) {
    setBadge("llm · your key", `${mine.model || PRESET[mine.provider]?.model || "default model"} via ${mine.provider} (key stored in this browser)`);
    return;
  }
  try {
    const { llm } = await api("/api/status");
    state.model = llm.model;
    if (llm.forced_mode === "offline" || !llm.key_present) {
      setBadge("offline", llm.key_present ? "TRACEWISE_MODE=offline" : "No LLM key: answers come straight from the graph. Click ⚙ to add your own key.");
    } else if (llm.last_error && !(llm.last_ok_at && llm.last_ok_at > llm.last_error_at)) {
      setBadge("offline (LLM unavailable)", "LLM failed: " + llm.last_error);
    } else {
      setBadge("llm", `${llm.model || "default model"} via ${llm.base_url || llm.provider} (server key)`);
    }
  } catch { /* badge keeps its default */ }
}

// ------------------------------------------------- LLM settings (your key)
// The key lives only in this browser (localStorage) and is sent with this user's own requests.
const PRESET = {
  openrouter: { label: "OpenRouter", model: "openai/gpt-4.1-mini", base: "https://openrouter.ai/api/v1", hint: "sk-or-v1-…", models: ["openai/gpt-4.1-mini", "openai/gpt-4.1", "anthropic/claude-sonnet-4.5", "google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct"] },
  openai: { label: "OpenAI", model: "gpt-4.1-mini", base: "", hint: "sk-…", models: ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"] },
  anthropic: { label: "Anthropic", model: "claude-sonnet-5-5", base: "", hint: "sk-ant-…", models: ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"] },
  groq: { label: "Groq", model: "llama-3.3-70b-versatile", base: "https://api.groq.com/openai/v1", hint: "gsk_…", models: ["llama-3.3-70b-versatile"] },
  custom: { label: "Other (OpenAI-compatible)", model: "", base: "", hint: "your key", models: [] },
};
function getLLM() {
  try { const v = JSON.parse(localStorage.getItem("tw-llm") || "null"); return v && v.api_key ? v : null; } catch { return null; }
}
function setLLM(v) { try { v ? localStorage.setItem("tw-llm", JSON.stringify(v)) : localStorage.removeItem("tw-llm"); } catch {} }

function readSettingsForm() {
  const provider = $("#llm-provider").value;
  return { provider, api_key: $("#llm-key").value.trim(), model: $("#llm-model").value.trim() || PRESET[provider].model,
           base_url: provider === "custom" ? $("#llm-base").value.trim() : (PRESET[provider].base || null) };
}
function syncProvider() {
  const p = PRESET[$("#llm-provider").value];
  $("#llm-key").placeholder = p.hint;
  $("#llm-model").placeholder = p.model || "model id";
  $("#llm-models").innerHTML = p.models.map((m) => `<option value="${esc(m)}">`).join("");
  $("#llm-base-row").hidden = $("#llm-provider").value !== "custom";
}
async function openSettings() {
  const v = getLLM();
  $("#llm-provider").innerHTML = Object.entries(PRESET).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("");
  $("#llm-provider").value = v?.provider || "openrouter";
  syncProvider();
  $("#llm-key").value = v?.api_key || "";
  $("#llm-key").classList.add("masked");
  $("#llm-show").textContent = "show";
  $("#llm-model").value = v?.model || "";
  $("#llm-base").value = v?.base_url || "";
  $("#llm-msg").textContent = "";
  $("#llm-msg").className = "msg";
  $("#llm-remove").hidden = !v;
  try {
    const { llm } = await api("/api/status");
    $("#llm-server").textContent = llm.key_present
      ? `The server also has a key configured (${llm.model || "default model"}). Your key, if you add one, is used instead for your questions.`
      : "The server has no key configured, so without your own key Tracewise answers straight from the graph (offline mode).";
  } catch { $("#llm-server").textContent = ""; }
  $("#settings").showModal();
  $("#llm-key").focus();
}
async function testSettings() {
  const v = readSettingsForm();
  if (!v.api_key) { flash("#llm-msg", "Paste an API key first.", "err"); return null; }
  flash("#llm-msg", "Testing…", "");
  const r = await api("/api/llm/test", v).catch((e) => ({ ok: false, error: e.message }));
  if (r.ok) flash("#llm-msg", `✓ Works: ${r.model} replied in ${r.ms} ms.`, "ok");
  else flash("#llm-msg", "✗ " + friendlyLLMError(r.error), "err");
  return r.ok;
}
function friendlyLLMError(e = "") {
  if (/401|auth|invalid.*key|incorrect api key/i.test(e)) return "The key was rejected (401). Check you copied all of it, and that it matches the provider.";
  if (/402|credit|insufficient|quota/i.test(e)) return "The provider says the account is out of credit (402/quota).";
  if (/404|not.*found|model/i.test(e) && /model/i.test(e)) return "That model ID wasn't found on this provider. Pick one from the list.";
  if (/connection|timeout|resolve/i.test(e)) return "Couldn't reach the provider. Check the base URL and your network.";
  return e.slice(0, 220);
}
function flash(sel, text, kind) { const el = $(sel); el.textContent = text; el.className = "msg " + (kind || ""); }
$("#open-settings").addEventListener("click", openSettings);
$("#mode").addEventListener("click", openSettings);
$("#llm-provider").addEventListener("change", () => { $("#llm-model").value = ""; syncProvider(); });
$("#llm-show").addEventListener("click", () => {
  const k = $("#llm-key"); k.classList.toggle("masked");
  $("#llm-show").textContent = k.classList.contains("masked") ? "show" : "hide";
});
$("#llm-test").addEventListener("click", testSettings);
$("#llm-save").addEventListener("click", async () => {
  const v = readSettingsForm();
  if (!v.api_key) { flash("#llm-msg", "Paste an API key, or close this to keep using offline mode.", "err"); return; }
  if (!(await testSettings())) { flash("#llm-msg", $("#llm-msg").textContent + " Not saved.", "err"); return; }
  setLLM(v);
  $("#settings").close();
  loadStatus();
});
$("#llm-remove").addEventListener("click", () => { setLLM(null); $("#settings").close(); loadStatus(); });

// --------------------------------------------------------- add a repository
let jobTimer = null;
function openAddRepo() {
  $("#repo-url").value = "";
  $("#repo-name").value = "";
  delete $("#repo-name").dataset.touched;
  $("#repo-rev").value = "";
  $("#repo-exists").hidden = true;
  $("#repo-progress").hidden = true;
  $("#repo-log").textContent = "";
  $("#repo-go").disabled = false;
  $("#repo-open").hidden = true;
  flash("#repo-msg", "", "");
  $("#addrepo").showModal();
  $("#repo-url").focus();
}
function nameFromUrl(u) {
  return (u.trim().replace(/\/+$/, "").replace(/\.git$/, "").split("/").pop() || "").toLowerCase().replace(/[^a-z0-9_-]/g, "-").slice(0, 40);
}
$("#add-repo").addEventListener("click", openAddRepo);
// Re-using an existing name re-indexes that repository (for example at another version), so say so up front.
function noteExisting() {
  const name = $("#repo-name").value.trim().toLowerCase();
  const exists = name && [...$("#repo").options].some((o) => o.value === name);
  const rev = $("#repo-rev").value.trim();
  $("#repo-exists").hidden = !exists;
  if (exists) $("#repo-exists").textContent = `“${name}” is already indexed. Indexing again replaces its graph with ${rev ? `version ${rev}` : "the latest code"}; memory is kept, so the agent can tell you what changed.`;
}
$("#repo-url").addEventListener("input", () => { if (!$("#repo-name").dataset.touched) $("#repo-name").value = nameFromUrl($("#repo-url").value); noteExisting(); });
$("#repo-name").addEventListener("input", () => { $("#repo-name").dataset.touched = "1"; noteExisting(); });
$("#repo-rev").addEventListener("input", noteExisting);
document.querySelectorAll("[data-example-repo]").forEach((b) => b.addEventListener("click", () => {
  $("#repo-url").value = b.dataset.exampleRepo; delete $("#repo-name").dataset.touched;
  $("#repo-name").value = nameFromUrl(b.dataset.exampleRepo);
  noteExisting();
}));
$("#repo-go").addEventListener("click", async () => {
  const url = $("#repo-url").value.trim(), name = $("#repo-name").value.trim() || nameFromUrl(url);
  const rev = $("#repo-rev").value.trim() || null;
  if (!url) { flash("#repo-msg", "Paste a repository URL first.", "err"); return; }
  flash("#repo-msg", "", "");
  try {
    const job = await api("/api/repos", { url, name, rev });
    $("#repo-go").disabled = true;
    $("#repo-progress").hidden = false;
    pollJob(job.id);
  } catch (e) { flash("#repo-msg", e.message, "err"); }
});
function pollJob(id) {
  clearTimeout(jobTimer);
  api(`/api/jobs/${id}`).then((j) => {
    const steps = ["cloning", "indexing", "done"];
    $("#repo-steps").innerHTML = steps.map((s) => {
      const i = steps.indexOf(s), cur = steps.indexOf(j.step === "fetching" ? "cloning" : j.step);
      const cls = j.status === "failed" && s === j.step ? "fail" : i < cur || j.status === "done" ? "done" : i === cur ? "now" : "";
      return `<span class="step ${cls}">${s}</span>`;
    }).join("");
    $("#repo-log").textContent = j.log.join("\n");
    $("#repo-log").scrollTop = $("#repo-log").scrollHeight;
    if (j.status === "running") { jobTimer = setTimeout(() => pollJob(id), 1200); return; }
    $("#repo-go").disabled = false;
    if (j.status === "done") {
      const c = j.stats?.code || {}, h = j.stats?.history || {};
      flash("#repo-msg", `✓ ${j.name}: ${c.functions || 0} functions, ${h.commits || 0} commits, ${h.prs || 0} PRs, ${h.people || 0} people, ${h.releases || 0} releases.`, "ok");
      $("#repo-open").hidden = false;
      $("#repo-open").onclick = async () => { $("#addrepo").close(); await loadStats(j.name); newChat(); };
      loadStats(state.repo);
    } else {
      flash("#repo-msg", "✗ " + j.error, "err");
    }
  }).catch((e) => { flash("#repo-msg", e.message, "err"); $("#repo-go").disabled = false; });
}
document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

loadUsers();
loadStats();
loadStatus();
newChat();
