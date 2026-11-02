"use strict";
// Agent Tillu web client. Plain JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const S = { agents: [], templates: {}, groups: [], current: null, view: "today", tab: "memory", messages: [], inbox: null, state: null };

const EFFECT_LABEL = { read: "read", workspace: "own files", browse: "web read", system: "shell", external: "sends out", money: "money", destructive: "can't undo", blocked: "blocked" };
const GROUP_HELP = {
  core: "Memory and asking you questions", files: "Read/write its own workspace folder", web: "Search and read web pages",
  http: "Call APIs (POST asks first)", browser: "Drive its own Chromium", shell: "Run commands (asks first)",
  email: "Send email (always asks)", skills: "Read shared playbooks", routines: "Schedule its own work",
  team: "Hand work to coworkers", foreman: "Hire coworkers", mcp: "Tools from your MCP servers",
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Tillu": "1" } };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  if (r.status === 401) { location.reload(); throw new Error("signed out"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
  return data;
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3200); }
function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "now"; if (s < 3600) return `${Math.floor(s / 60)}m`; if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return new Date(ts * 1000).toLocaleDateString();
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

// Small, safe markdown: escape first, then format.
function md(src) {
  const blocks = [];
  let s = esc(src).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, code) => { blocks.push(`<pre><code>${code}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*(\S(?:[^*\n]*\S)?)\*(?!\w)/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  const out = []; let list = null;
  for (const line of s.split("\n")) {
    const li = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
    const h = line.match(/^#{1,4}\s+(.*)/);
    if (li) { if (!list) { list = []; } list.push(`<li>${li[1]}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (h) out.push(`<h4>${h[1]}</h4>`);
    else if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

// ---------------------------------------------------------------- state & sidebar
async function loadState() {
  const st = await api("/api/state");
  S.state = st; S.agents = st.agents; S.templates = st.templates; S.groups = st.groups;
  renderSidebar();
  if (S.current) { const a = agent(S.current); if (!a) { S.current = null; show("today"); } else renderHeader(); }
}
const agent = id => S.agents.find(a => a.id === id);

function renderSidebar() {
  const list = $("#agent-list");
  list.innerHTML = S.agents.map(a => {
    const preview = a.state === "working" ? "Working…" : a.state === "waiting" ? "Waiting for you" : a.last ? (a.last.role === "user" ? "You: " : "") + a.last.content.replace(/\s+/g, " ").slice(0, 70) : a.role;
    return `<button class="nav-item ${S.view === "chat" && S.current === a.id ? "active" : ""}" data-agent="${esc(a.id)}">
      <span class="avatar">${esc(a.emoji)}<i class="dot ${a.state}"></i></span>
      <span class="meta"><b>${esc(a.name)}</b><small>${esc(preview)}</small></span>
      ${a.pending ? `<span class="badge">${a.pending}</span>` : `<small class="muted">${a.last ? ago(a.last.created_at) : ""}</small>`}
    </button>`;
  }).join("");
  $$("[data-agent]", list).forEach(b => b.onclick = () => openAgent(b.dataset.agent));
  const total = S.agents.reduce((n, a) => n + a.pending, 0);
  const badge = $("#today-badge"); badge.hidden = !total; badge.textContent = total;
  $("#nav-today").classList.toggle("active", S.view === "today");
  document.title = total ? `(${total}) Agent Tillu` : "Agent Tillu";
}

async function checkModel() {
  const pill = $("#model-pill");
  try {
    const r = await api("/api/models");
    const m = S.state?.default_model || "";
    pill.textContent = m === "stub" ? "stub model" : r.ok ? m : "model offline";
    pill.className = "pill " + (m === "stub" ? "" : r.ok && r.models.includes(m) ? "ok" : "bad");
    pill.title = r.ok ? (r.models.includes(m) || m === "stub" ? `Using ${m}` : `${m} isn't installed. Run: ollama pull ${m}`) : r.error;
  } catch { pill.textContent = "?"; }
}

// ---------------------------------------------------------------- views
function show(view) {
  S.view = view;
  $$(".view").forEach(v => (v.hidden = v.id !== `view-${view}`));
  $("#panel-btn").hidden = view !== "chat";
  $("#stop-btn").hidden = true;
  if (view !== "chat") { $("#panel").hidden = true; }
  $("#sidebar").classList.remove("open");
  if (view === "today") { $("#title").textContent = "Today"; $("#subtitle").textContent = "Everything waiting on you, in one place."; renderToday(); }
  if (view === "audit") { $("#title").textContent = "Activity log"; $("#subtitle").textContent = "Every tool call, what the firewall decided, and how it went."; renderAudit(); }
  renderSidebar();
}

async function openAgent(id) {
  S.current = id; S.view = "chat";
  show("chat");
  renderHeader();
  S.messages = await api(`/api/agents/${id}/messages`);
  renderThread(true);
  let wantPanel = false;
  try { wantPanel = localStorage.getItem("tillu.panel") === "1"; } catch {}
  if (!$("#panel").hidden || (window.innerWidth > 1100 && wantPanel)) openPanel(S.tab);
  $("#input").focus();
}

function renderHeader() {
  const a = agent(S.current); if (!a) return;
  $("#title").textContent = `${a.emoji} ${a.name}`;
  $("#subtitle").innerHTML = `<span>${esc(a.role)}</span><span class="chip">${esc(a.model_effective)}</span><span class="chip" title="Autonomy">${esc(a.autonomy)}</span>`;
  $("#stop-btn").hidden = !(a.state === "working" || a.state === "waiting");
}

// ---------------------------------------------------------------- thread
function renderThread(scroll) {
  const th = $("#thread");
  const near = th.scrollHeight - th.scrollTop - th.clientHeight < 120;
  const parts = [];
  let steps = null;
  const flush = () => { if (steps) { parts.push(stepsHtml(steps)); steps = null; } };
  for (const m of S.messages) {
    if (m.role === "activity") { (steps ||= []).push(m); continue; }
    flush();
    parts.push(msgHtml(m));
  }
  flush();
  const a = agent(S.current);
  if (a && a.state === "working") parts.push(`<div class="typing">${esc(a.name)} is working</div>`);
  if (!S.messages.length) parts.push(`<div class="empty">Say hello to ${esc(a?.name || "your coworker")}. Tell it what you need in plain words.</div>`);
  th.innerHTML = parts.join("");
  bindCards(th);
  if (scroll || near) th.scrollTop = th.scrollHeight;
}

function stepsHtml(steps) {
  const blocked = steps.filter(s => s.meta.blocked).length;
  const failed = steps.filter(s => s.meta.ok === false).length;
  const label = `🔧 ${steps.length} step${steps.length > 1 ? "s" : ""}` + (blocked ? ` · ⛔ ${blocked} blocked` : "") + (failed ? ` · ⚠️ ${failed} failed` : "");
  return `<details class="steps"><summary>${label}</summary>${steps.map(s => `
    <div class="step ${s.meta.blocked ? "blocked" : ""}" title="${esc(s.meta.why || s.meta.preview || "")}">
      <span class="effect ${esc(s.meta.effect || (s.meta.blocked ? "blocked" : "read"))}">${esc(s.meta.tool || "")}</span>
      <span class="s-text">${esc(s.content)}</span>
      <span class="muted">${s.meta.decision === "rule" ? "rule" : s.meta.decision === "approved" ? "approved" : ""}${s.meta.ms ? ` ${s.meta.ms}ms` : ""}${s.meta.ok === false ? " ⚠️" : ""}</span>
    </div>`).join("")}</details>`;
}

function msgHtml(m, opts = {}) {
  const t = `<span class="time">${clock(m.created_at)}</span>`;
  switch (m.role) {
    case "user": return `<div class="msg user">${esc(m.content)}</div>`;
    case "agent": { const a = agent(m.agent_id); return `<div class="msg agent"><div class="who">${esc(a?.name || "")}${m.meta.origin && m.meta.origin !== "operator" ? ` · ${esc(m.meta.origin)}` : ""}${t}</div>${md(m.content)}</div>`; }
    case "approval": return approvalCard({ id: m.meta.approval_id, summary: m.content, effect: m.meta.effect, why: m.meta.why, status: m.meta.status || "pending", rule_eligible: m.meta.rule_eligible, tool: m.meta.tool, target: m.meta.target, provenance: m.meta.provenance, reverify: m.meta.reverify }, opts);
    case "question": return questionCard({ id: m.meta.question_id, question: m.content, options: m.meta.options || [], status: m.meta.status || "pending" }, opts);
    case "relay": return `<div class="msg relay"><b>Task from ${esc(m.meta.from_name)}</b>${t}\n${esc(m.content)}</div>`;
    case "relay_result": return `<div class="msg relay_result"><b>Reply from ${esc(m.meta.from_name)}</b>${t}\n${esc(m.content)}</div>`;
    case "screenshot": return `<div class="msg agent"><div class="who">Screenshot · ${esc(m.meta.url || "")}</div><img src="${esc(m.content)}" alt="screenshot" loading="lazy"></div>`;
    case "memory": return `<div class="msg memory">${esc(m.content)}</div>`;
    case "decision": return `<div class="msg decision">${esc(m.content)}</div>`;
    case "answer": return `<div class="msg decision">You answered: ${esc(m.content)}</div>`;
    default: return `<div class="msg system">${esc(m.content)}</div>`;
  }
}

function approvalCard(ap, { agentTag } = {}) {
  const pending = ap.status === "pending";
  const risky = ap.effect === "money" || ap.effect === "destructive" || ap.provenance === "outside" || ap.reverify;
  const title = { external: "Wants to send something out", money: "Wants to spend money", destructive: "Wants to do something that can't be undone", system: "Wants to run a command" }[ap.effect] || "Needs your OK";
  return `<div class="card e-${esc(ap.effect)} ${pending ? "" : "done"}" data-approval="${ap.id}">
    <h4><span class="effect ${esc(ap.effect)}">${esc(EFFECT_LABEL[ap.effect] || ap.effect)}</span>${esc(title)}${agentTag ? `<span class="agent-tag">· ${agentTag}</span>` : ""}</h4>
    <pre>${esc(ap.summary)}</pre>
    <div class="why">Why it asks: ${esc(ap.why)}</div>
    ${ap.provenance === "you" ? `<div class="prov you">✓ Destination appears in your own message</div>`
      : ap.provenance === "outside" ? `<div class="prov outside">⚠️ You never mentioned this destination. It appeared after the coworker read outside content (a web page, webhook or tool result). Treat it as a possible injection.</div>`
      : ap.provenance === "agent" ? `<div class="prov agent">ℹ️ The coworker chose this destination itself; you didn't name it.</div>` : ""}
    ${pending ? `<div class="actions">
      ${risky ? `<button class="primary deny" data-act="deny">Deny</button><button data-act="approve">Approve anyway</button>`
              : `<button class="primary" data-act="approve">Approve</button><button data-act="deny">Deny</button>`}
      ${ap.rule_eligible ? `<select data-act="always-sel" title="Let it repeat this exact action to ${esc(ap.target)} without asking"><option value="">Always allow…</option><option value="1">for 1 hour</option><option value="24">for 24 hours</option><option value="0">until I revoke it</option></select>` : ""}
      <input data-note placeholder="Optional note (e.g. why you denied)">
    </div>` : `<div class="why"><b>${esc(ap.status)}</b></div>`}
  </div>`;
}

function questionCard(q, { agentTag } = {}) {
  const pending = q.status === "pending";
  return `<div class="card question ${pending ? "" : "done"}" data-question="${q.id}">
    <h4>❓ Question${agentTag ? `<span class="agent-tag">· ${agentTag}</span>` : ""}</h4>
    <div>${md(q.question)}</div>
    ${pending ? `<div class="actions">${q.options.map(o => `<button data-answer="${esc(o)}">${esc(o)}</button>`).join("")}
      <input data-free placeholder="Or type an answer and press Enter"></div>` : `<div class="why"><b>${esc(q.status)}</b></div>`}
  </div>`;
}

function bindCards(root) {
  $$("[data-approval]", root).forEach(card => {
    const id = card.dataset.approval;
    const note = () => $("[data-note]", card)?.value || "";
    const send = async (decision, hours) => {
      $$("button, select", card).forEach(b => (b.disabled = true));
      try { await api(`/api/approvals/${id}`, { method: "POST", body: { decision, note: note(), hours } }); }
      catch (e) { toast(e.message); }
      refreshAfterDecision();
    };
    $("[data-act=approve]", card)?.addEventListener("click", () => send("approve"));
    $("[data-act=deny]", card)?.addEventListener("click", () => send("deny"));
    $("[data-act=always-sel]", card)?.addEventListener("change", e => { if (e.target.value !== "") send("always", +e.target.value || null); });
  });
  $$("[data-question]", root).forEach(card => {
    const id = card.dataset.question;
    const send = async answer => {
      $$("button, input", card).forEach(b => (b.disabled = true));
      try { await api(`/api/questions/${id}`, { method: "POST", body: { answer } }); } catch (e) { toast(e.message); }
      refreshAfterDecision();
    };
    $$("[data-answer]", card).forEach(b => b.onclick = () => send(b.dataset.answer));
    $("[data-free]", card)?.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.value.trim()) send(e.target.value.trim()); });
  });
}

async function refreshAfterDecision() {
  await loadState();
  if (S.view === "today") renderToday();
  if (S.view === "chat" && S.current) { S.messages = await api(`/api/agents/${S.current}/messages`); renderThread(); }
}

// ---------------------------------------------------------------- today
async function renderToday() {
  const box = $("#view-today");
  const ib = await api("/api/inbox");
  const n = ib.approvals.length + ib.questions.length;
  const tag = x => `${esc(x.emoji)} ${esc(x.agent_name)}`;
  box.innerHTML = `<div class="today-wrap">
    ${n ? `<h3 class="sec">Waiting on you (${n})</h3>` : `<div class="empty">☀️ Nothing needs you right now.<br><small>Approvals and questions from every coworker land here.</small></div>`}
    ${ib.approvals.map(a => approvalCard({ ...a, rule_eligible: !!a.rule_eligible }, { agentTag: tag(a) })).join("")}
    ${ib.questions.map(q => questionCard(q, { agentTag: tag(q) })).join("")}
    ${ib.stuck.length ? `<h3 class="sec">Stuck</h3>${ib.stuck.map(a => `<div class="item"><div class="row"><b>${esc(a.emoji)} ${esc(a.name)}</b><button data-open="${esc(a.id)}">Open</button></div><div class="sub">${esc(a.last?.content || "")}</div></div>`).join("")}` : ""}
    ${ib.routines.length ? `<h3 class="sec">Recent routine runs</h3>${ib.routines.map(r => `<div class="item"><div class="row"><b>${esc(r.emoji)} ${esc(r.agent_name)} · ${esc(r.name)}</b><span class="sub">${ago(r.last_run_at)}</span></div><div class="sub">${esc((r.last_result || "running…").slice(0, 240))}</div></div>`).join("")}` : ""}
  </div>`;
  bindCards(box);
  $$("[data-open]", box).forEach(b => b.onclick = () => openAgent(b.dataset.open));
}

// ---------------------------------------------------------------- audit
async function renderAudit() {
  const rows = await api("/api/audit?limit=300");
  const name = id => agent(id)?.name || id;
  $("#view-audit").innerHTML = rows.length ? `<table><thead><tr><th>When</th><th>Who</th><th>Tool</th><th>Effect</th><th>Decision</th><th>What</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${new Date(r.created_at * 1000).toLocaleString()}</td><td>${esc(name(r.agent_id))}</td><td>${esc(r.tool)}</td>
    <td><span class="effect ${esc(r.effect)}">${esc(r.effect)}</span></td><td>${esc(r.decision)}${r.ok ? "" : " ⚠️"}</td><td class="mono">${esc(r.summary)}</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">No actions yet.</div>`;
}

// ---------------------------------------------------------------- side panel
function openPanel(tab) {
  S.tab = tab || S.tab;
  $("#panel").hidden = false;
  try { localStorage.setItem("tillu.panel", "1"); } catch {}
  $$(".tabs [data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === S.tab));
  renderPanel();
}

async function renderPanel() {
  const id = S.current, body = $("#panel-body");
  if (!id || $("#panel").hidden) return;
  if (S.tab === "memory") {
    const { memories, journal } = await api(`/api/agents/${id}/memory`);
    body.innerHTML = `<p class="hint">What ${esc(agent(id).name)} knows. Rules are always in its prompt; other memories fade if unused but stay searchable.</p>
      <form id="mem-add"><div class="grid2"><select name="kind"><option>rule</option><option>preference</option><option selected>fact</option><option>note</option></select><button class="primary">Add</button></div>
      <textarea name="content" rows="2" placeholder="e.g. Never email clients on weekends" style="margin-top:6px"></textarea></form>
      <div style="margin-top:12px">${memories.map(m => `<div class="item"><div class="row"><span class="chip">${esc(m.kind)}${m.pinned ? " · 📌" : ""}</span><span><button class="icon-btn" data-pin="${m.id}" data-v="${m.pinned ? 0 : 1}" title="${m.pinned ? "Unpin" : "Pin: always in prompt"}">📌</button><button class="icon-btn" data-del="${m.id}" title="Forget">🗑</button></span></div>
        <div>${esc(m.content)}</div><div class="strength" title="strength ${m.strength}"><i style="width:${Math.round(m.strength * 100)}%"></i></div></div>`).join("") || `<div class="muted">Nothing yet.</div>`}</div>
      <div class="row-end"><a href="/api/agents/${esc(id)}/memory.md" target="_blank"><button>Export as Markdown</button></a></div>
      ${journal.length ? `<h3 class="sec">Change log</h3>${journal.slice(0, 15).map(j => `<div class="sub" style="font-size:12px;margin-bottom:4px">${ago(j.created_at)} · ${esc(j.by)} ${esc(j.action)}: ${esc(j.content.slice(0, 90))}</div>`).join("")}` : ""}`;
    $("#mem-add").onsubmit = async e => { e.preventDefault(); const f = new FormData(e.target); if (!f.get("content").trim()) return; await api(`/api/agents/${id}/memory`, { method: "POST", body: { content: f.get("content"), kind: f.get("kind") } }); renderPanel(); };
    $$("[data-del]", body).forEach(b => b.onclick = async () => { await api(`/api/agents/${id}/memory/${b.dataset.del}`, { method: "DELETE" }); renderPanel(); });
    $$("[data-pin]", body).forEach(b => b.onclick = async () => { await api(`/api/agents/${id}/memory/${b.dataset.pin}`, { method: "PATCH", body: { pinned: !!+b.dataset.v } }); renderPanel(); });
  }
  if (S.tab === "routines") {
    const rs = await api(`/api/agents/${id}/routines`);
    body.innerHTML = `<p class="hint">Scheduled prompts. They never overlap, survive restarts, and catch up once if Agent Tillu was off. Watch routines are read-only and only speak up when something changed.</p>
      ${rs.map(r => `<div class="item"><div class="row"><b>${esc(r.name)}</b><label style="margin:0;display:flex;gap:4px;align-items:center"><input type="checkbox" data-toggle="${r.id}" ${r.enabled ? "checked" : ""} style="width:auto"> on</label></div>
        <div class="sub">${esc(r.human)} · ${esc(r.mode)}${r.running ? " · running" : ""}${r.next_run_at && r.cron ? ` · next ${new Date(r.next_run_at * 1000).toLocaleString()}` : ""}</div>
        <div style="margin:4px 0">${esc(r.instructions)}</div>
        ${r.webhook_url ? `<div class="sub">Webhook: <code style="word-break:break-all">${esc(r.webhook_url)}</code></div>` : ""}
        ${r.last_result ? `<div class="sub">Last: ${esc(r.last_result.slice(0, 160))}</div>` : ""}
        <div class="row-end" style="margin-top:6px"><button data-run="${r.id}">Run now</button><button class="danger-outline" data-rdel="${r.id}">Delete</button></div></div>`).join("") || `<div class="muted">No routines yet. You can also just say “every weekday at 8, …” in the chat.</div>`}
      <h3 class="sec">New routine</h3>
      <form id="rt-add"><label>Name</label><input name="name" placeholder="Morning brief" required>
      <label>When</label><select name="preset"><option value="0 8 * * 1-5">Weekdays 08:00</option><option value="0 9 * * *">Every day 09:00</option><option value="0 * * * *">Every hour</option><option value="*/30 * * * *">Every 30 minutes</option><option value="0 9 * * 1">Mondays 09:00</option><option value="custom">Custom cron…</option><option value="webhook">On webhook</option></select>
      <input name="cron" placeholder="m h dom mon dow" hidden style="margin-top:6px">
      <label>Mode</label><select name="mode"><option value="act">Act — do the work</option><option value="watch">Watch — read-only, quiet unless something changed</option></select>
      <label>Instructions</label><textarea name="instructions" rows="3" required placeholder="Check my reviews page and draft replies to new ones"></textarea>
      <div class="row-end"><button class="primary">Create</button></div></form>`;
    const f = $("#rt-add");
    f.preset.onchange = () => (f.cron.hidden = f.preset.value !== "custom");
    f.onsubmit = async e => {
      e.preventDefault();
      const p = f.preset.value;
      try {
        await api(`/api/agents/${id}/routines`, { method: "POST", body: { name: f.name.value, instructions: f.instructions.value, mode: f.mode.value, webhook: p === "webhook", cron: p === "webhook" ? "" : p === "custom" ? f.cron.value : p } });
        renderPanel(); toast("Routine created");
      } catch (err) { toast(err.message); }
    };
    $$("[data-toggle]", body).forEach(c => c.onchange = () => api(`/api/routines/${c.dataset.toggle}`, { method: "PATCH", body: { enabled: c.checked } }));
    $$("[data-run]", body).forEach(b => b.onclick = async () => { const r = await api(`/api/routines/${b.dataset.run}/run`, { method: "POST" }); toast(r.started ? "Started" : "Already running"); });
    $$("[data-rdel]", body).forEach(b => b.onclick = async () => { if (confirm("Delete this routine?")) { await api(`/api/routines/${b.dataset.rdel}`, { method: "DELETE" }); renderPanel(); } });
  }
  if (S.tab === "rules") {
    const rules = await api(`/api/agents/${id}/rules`);
    body.innerHTML = `<p class="hint">Standing approvals you granted with “Always allow”. Each covers one tool to one exact destination. They never cover money, deletions or shell commands, and they switch off for any run that has read outside content (web pages, webhooks), so a page can't approve its own request.</p>
      ${rules.map(r => `<div class="item"><div class="row"><b>${esc(r.tool)}</b><button class="danger-outline" data-revoke="${r.id}">Revoke</button></div><div class="sub">→ ${esc(r.target)} · ${r.expires_at ? "until " + new Date(r.expires_at * 1000).toLocaleString() : "until revoked"}</div></div>`).join("") || `<div class="muted">No standing approvals.</div>`}`;
    $$("[data-revoke]", body).forEach(b => b.onclick = async () => { await api(`/api/rules/${b.dataset.revoke}`, { method: "DELETE" }); renderPanel(); });
  }
  if (S.tab === "profile") {
    const a = agent(id);
    let models = [];
    try { models = (await api("/api/models")).models; } catch {}
    body.innerHTML = `<form id="prof">
      <div class="grid2"><div><label>Name</label><input name="name" value="${esc(a.name)}"></div><div><label>Emoji</label><input name="emoji" value="${esc(a.emoji)}"></div></div>
      <label>Job</label><input name="role" value="${esc(a.role)}">
      <label>Personality & instructions</label><textarea name="soul" rows="6">${esc(a.soul)}</textarea>
      <label>Model</label><select name="model"><option value="">Default (${esc(S.state.default_model)})</option>${models.map(m => `<option ${m === a.model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>
      <label>Autonomy</label><div class="radio">
        ${[["watch", "Watch", "Reads pages and files and keeps notes in its own folder; never acts outside it"], ["ask", "Ask", "Works in its own folder; asks before shell, sending, paying or deleting"], ["autopilot", "Autopilot", "Also runs plain shell commands. Still asks before sending, paying or deleting"]]
          .map(([v, l, h]) => `<label><input type="radio" name="autonomy" value="${v}" ${a.autonomy === v ? "checked" : ""}><span>${l}<small>${h}</small></span></label>`).join("")}</div>
      <label>Tools</label><div class="checks">${S.groups.filter(g => g !== "foreman" || a.is_foreman).map(g => `<label title="${esc(GROUP_HELP[g] || "")}"><input type="checkbox" name="tools" value="${g}" ${a.tools.includes(g) ? "checked" : ""} ${g === "core" ? "disabled checked" : ""} ${g === "browser" && !S.state.browser ? "disabled" : ""}> ${g}</label>`).join("")}</div>
      <label><input type="checkbox" name="shell_enabled" ${a.shell_enabled ? "checked" : ""} style="width:auto"> Allow shell commands at all</label>
      <label>Daily token budget (0 = no limit)</label><input name="daily_token_budget" type="number" min="0" value="${a.daily_token_budget}">
      <div class="row-end"><button type="button" class="danger-outline" id="fire">Remove coworker</button><button class="primary">Save</button></div></form>`;
    const f = $("#prof");
    f.onsubmit = async e => {
      e.preventDefault();
      const tools = ["core", ...$$("input[name=tools]:checked", f).map(c => c.value).filter(v => v !== "core")];
      await api(`/api/agents/${id}`, { method: "PATCH", body: { name: f.name.value, emoji: f.emoji.value, role: f.role.value, soul: f.soul.value, model: f.model.value,
        autonomy: f.autonomy.value, tools, shell_enabled: f.shell_enabled.checked, daily_token_budget: +f.daily_token_budget.value || 0 } });
      await loadState(); toast("Saved");
    };
    $("#fire").onclick = async () => {
      if (!confirm(`Remove ${a.name}? Its chat, memory and routines are deleted. Its workspace folder stays on disk.`)) return;
      await api(`/api/agents/${id}`, { method: "DELETE" }); S.current = null; await loadState(); show("today");
    };
  }
}

// ---------------------------------------------------------------- modals
function modal(html) { $("#modal-body").innerHTML = html; $("#modal").showModal(); return $("#modal-body"); }
function closeModal() { $("#modal").close(); }

function hireModal() {
  let sel = "";
  const body = modal(`<h2>Hire a coworker</h2><p class="hint">Pick a template or describe the job. You can change everything later.</p>
    <div class="tpl-grid">${Object.entries(S.templates).map(([k, t]) => `<button class="tpl" data-tpl="${k}"><span class="e">${esc(t.emoji)}</span><span><b>${esc(t.name)}</b><small>${esc(t.role)}</small></span></button>`).join("")}
    <button class="tpl" data-tpl=""><span class="e">✨</span><span><b>Custom</b><small>Describe it yourself</small></span></button></div>
    <form id="hire"><div class="grid2"><div><label>Name</label><input name="name" placeholder="Name"></div><div><label>Job</label><input name="role" placeholder="What it does"></div></div>
    <label>Extra instructions</label><textarea name="instructions" rows="3" placeholder="Anything specific: tone, clients, tools, what to never do"></textarea>
    <div class="row-end"><button type="button" id="cancel">Cancel</button><button class="primary">Hire</button></div></form>
    <p class="hint">Or just tell the Foreman what you need and it will hire for you.</p>`);
  $$("[data-tpl]", body).forEach(b => b.onclick = () => {
    sel = b.dataset.tpl; $$("[data-tpl]", body).forEach(x => x.classList.toggle("sel", x === b));
    const t = S.templates[sel]; const f = $("#hire"); f.name.value = t ? t.name : ""; f.role.value = t ? t.role : ""; f.name.focus();
  });
  $("#cancel").onclick = closeModal;
  $("#hire").onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    if (!sel && !f.role.value.trim()) return toast("Give it a job");
    const a = await api("/api/agents", { method: "POST", body: { template: sel, name: f.name.value, role: f.role.value, instructions: f.instructions.value } });
    closeModal(); await loadState(); openAgent(a.id);
  };
}

async function settingsModal() {
  const s = await api("/api/settings");
  const body = modal(`<h2>Settings</h2>
    <form id="set">
    <h3 class="sec" style="margin-top:0">Model (runs on this computer)</h3>
    <label>OpenAI-compatible server URL</label><input name="model_base_url" value="${esc(s.model_base_url)}">
    <p class="hint">Ollama: http://127.0.0.1:11434/v1 · LM Studio: http://127.0.0.1:1234/v1 · llama.cpp: http://127.0.0.1:8080/v1</p>
    <div class="grid2"><div><label>Default model</label><input name="default_model" list="models" value="${esc(s.default_model)}"><datalist id="models"></datalist></div>
    <div><label>Embedding model (memory search)</label><input name="embed_model" value="${esc(s.embed_model)}" placeholder="nomic-embed-text, or none"></div></div>
    <div id="model-check" class="hint">Checking the model server…</div>
    <div class="grid2"><div><label>API key (only if your server needs one)</label><input name="model_api_key" value="${esc(s.model_api_key)}"></div><div><label>Max steps per task</label><input name="max_steps" type="number" min="4" max="100" value="${esc(s.max_steps)}"></div>
    <div><label>Context window (Ollama only)</label><input name="num_ctx" type="number" min="4096" step="1024" value="${esc(s.num_ctx)}"></div></div>
    <p class="hint">Ollama's default window is too small for tools, so Agent Tillu asks for this many tokens through Ollama's own API. 16384 suits 8B models on 16 GB; raise it if you have the memory.</p>
    <h3 class="sec">Web & browser</h3>
    <label>SearXNG URL for web search (optional, self-hosted; empty = DuckDuckGo HTML)</label><input name="searxng_url" value="${esc(s.searxng_url)}" placeholder="http://127.0.0.1:8888">
    <label><input type="checkbox" name="browser_headless" ${s.browser_headless ? "checked" : ""} style="width:auto"> Run coworker browsers in the background (headless)</label>
    <p class="hint">${S.state.browser ? "Browser tools available." : "Browser tools need Playwright: <code>pip install playwright && playwright install chromium</code>"}</p>
    <h3 class="sec">Email (SMTP) — optional</h3>
    <p class="hint">Without it, approved emails are saved as .eml files in the coworker's outbox folder.</p>
    <div class="grid2"><div><label>Host</label><input name="smtp_host" value="${esc(s.smtp.host || "")}" placeholder="smtp.gmail.com"></div><div><label>Port</label><input name="smtp_port" value="${esc(s.smtp.port || "587")}"></div>
    <div><label>User</label><input name="smtp_user" value="${esc(s.smtp.user || "")}"></div><div><label>Password / app password</label><input name="smtp_password" type="password" value="${esc(s.smtp.password || "")}"></div></div>
    <label>From</label><input name="smtp_from" value="${esc(s.smtp.from || "")}" placeholder="You &lt;you@example.com&gt;">
    <h3 class="sec">MCP servers (local tools)</h3>
    <p class="hint">JSON list. Example: <code>[{"name":"files","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/Users/me/Docs"],"safe_tools":["read_file","list_directory"]}]</code></p>
    <textarea name="mcp_servers" rows="4" style="font-family:var(--mono);font-size:12px">${esc(JSON.stringify(s.mcp_servers, null, 1))}</textarea>
    <div class="hint">${(S.state.mcp || []).map(m => `${esc(m.name)}: ${m.error ? "⚠️ " + esc(m.error) : m.tools.length + " tools"}`).join(" · ")}</div>
    <div class="row-end"><a href="/api/export"><button type="button">Export all data</button></a><button type="button" id="cancel">Cancel</button><button class="primary">Save</button></div>
    </form>`);
  $("#cancel").onclick = closeModal;
  api("/api/models").then(r => {
    $("#models", body).innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join("");
    $("#model-check", body).innerHTML = r.ok ? `✅ Connected. Installed: ${r.models.filter(m => m !== "stub").map(esc).join(", ") || "none — run <code>ollama pull qwen3:8b</code>"}` : `⚠️ ${esc(r.error)}. Is Ollama running? Use model <code>stub</code> to try Agent Tillu without one.`;
  });
  $("#set").onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    let mcp;
    try { mcp = JSON.parse(f.mcp_servers.value || "[]"); } catch { return toast("MCP servers: invalid JSON"); }
    await api("/api/settings", { method: "PUT", body: {
      model_base_url: f.model_base_url.value.trim(), default_model: f.default_model.value.trim(), embed_model: f.embed_model.value.trim(),
      model_api_key: f.model_api_key.value, max_steps: +f.max_steps.value || 24, num_ctx: +f.num_ctx.value || 16384, searxng_url: f.searxng_url.value.trim(), browser_headless: f.browser_headless.checked,
      smtp: { host: f.smtp_host.value.trim(), port: +f.smtp_port.value || 587, user: f.smtp_user.value.trim(), password: f.smtp_password.value, from: f.smtp_from.value.trim() },
      mcp_servers: mcp } });
    closeModal(); await loadState(); checkModel(); toast("Settings saved");
  };
}

// ---------------------------------------------------------------- live events
function connect() {
  const es = new EventSource("/api/events");
  let reloadTimer = null, everOpened = false;
  es.onopen = async () => {
    if (!everOpened) { everOpened = true; return; }
    await loadState();  // reconnected: catch up on anything missed while offline
    if (S.view === "today") renderToday();
    if (S.view === "chat" && S.current) { S.messages = await api(`/api/agents/${S.current}/messages`); renderThread(); }
  };
  const soon = fn => { clearTimeout(reloadTimer); reloadTimer = setTimeout(fn, 150); };
  es.onmessage = ev => {
    const { type, data } = JSON.parse(ev.data);
    if (type === "message") {
      const a = agent(data.agent_id);
      if (a) { a.last = { role: data.role, content: data.content, created_at: data.created_at }; }
      if (S.view === "chat" && data.agent_id === S.current) { S.messages.push(data); renderThread(); if (data.role === "memory" && S.tab === "memory") renderPanel(); }
      soon(async () => { await loadState(); if (S.view === "today") renderToday(); });
    } else if (type === "status") {
      const a = agent(data.agent_id);
      if (a) { a.state = data.state; renderSidebar(); if (data.agent_id === S.current) { renderHeader(); renderThread(); } }
    } else if (type === "approvals" || type === "agents") {
      soon(async () => { await loadState(); if (S.view === "today") renderToday(); if (S.view === "chat" && S.current) { S.messages = await api(`/api/agents/${S.current}/messages`); renderThread(); } });
    } else if (type === "routines") {
      if (data.agent_id === S.current && S.tab === "routines") renderPanel();
    }
  };
}

// ---------------------------------------------------------------- wiring
function wire() {
  $("#composer").onsubmit = async e => {
    e.preventDefault();
    const input = $("#input"), text = input.value.trim();
    if (!text || !S.current) return;
    input.value = ""; input.style.height = "";
    try { await api(`/api/agents/${S.current}/messages`, { method: "POST", body: { text } }); } catch (err) { toast(err.message); input.value = text; }
  };
  $("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#input").addEventListener("input", e => { e.target.style.height = "auto"; e.target.style.height = Math.min(200, e.target.scrollHeight) + "px"; });
  $("#nav-today").onclick = () => { S.current = null; show("today"); };
  $$("[data-view=audit]").forEach(b => b.onclick = () => { S.current = null; show("audit"); });
  $("#hire-btn").onclick = hireModal;
  $("#settings-btn").onclick = settingsModal;
  $("#model-pill").onclick = settingsModal;
  $("#stop-btn").onclick = async () => { await api(`/api/agents/${S.current}/stop`, { method: "POST" }); toast("Stopping"); };
  $("#panel-btn").onclick = () => { if ($("#panel").hidden) openPanel(); else { $("#panel").hidden = true; try { localStorage.setItem("tillu.panel", "0"); } catch {} } };
  $("#close-panel").onclick = () => { $("#panel").hidden = true; try { localStorage.setItem("tillu.panel", "0"); } catch {} };
  $$(".tabs [data-tab]").forEach(b => b.onclick = () => openPanel(b.dataset.tab));
  $("#open-side").onclick = () => $("#sidebar").classList.add("open");
  $("#close-side").onclick = () => $("#sidebar").classList.remove("open");
  $("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
}

(async function init() {
  wire();
  await loadState();
  show("today");
  checkModel();
  connect();
})();
