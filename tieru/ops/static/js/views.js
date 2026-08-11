// tieru dashboard — subtab/db helpers, SQL console, Memory/Tools sub-views, VIEWS.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// --- sub-tabs: keep long pages short by splitting them into hash-routed tabs
// (#memory/semantic, #database/facts). Each tab is a plain link, so it's
// bookmarkable and the architecture cards can deep-link straight to one.
function subtabBar(view, tabs, active){
  return `<div class="subtabs">${tabs.map(([key,label,n]) =>
    `<a class="subtab ${key===active?"on":""}" href="#${view}/${key}">${esc(label)}${
      n!=null?`<span class="n">${n}</span>`:""}</a>`).join("")}</div>`;
}

// A raw SQLite table, scrollable, with the column names AS the (indigo) sticky
// headers so the schema lines up over its data instead of floating above it.
function dbTable(t){
  if (!t.sample.length) return `<div class="card empty">empty — no rows yet</div>`;
  const head = t.columns.map(c => `<th class="dbcol">${esc(c)}${
    t.types&&t.types[c]?`<small>${esc(t.types[c].toLowerCase())}</small>`:""}</th>`).join("");
  const body = t.sample.map(r => `<tr>${t.columns.map(c =>
    `<td class="dbcell">${esc(String(r[c]??"").slice(0,120))}</td>`).join("")}</tr>`).join("");
  return `<div class="scrolly"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
    <div class="meta" style="margin-top:6px">showing ${t.sample.length} of ${t.count} row${t.count===1?"":"s"} (newest first)</div>`;
}
const DB_DESC = {
  calendar_events: "events the create_event tool wrote (the flagship task)",
  facts: "semantic memory — durable facts (Memory ▸ Semantic)",
  episodes: "episodic memory — dated summaries (Memory ▸ Episodic)",
  graph_entities: "Memory Graph — typed entities (Memory ▸ Graph)",
  graph_relations: "Memory Graph — typed, temporal relations with provenance",
  chat_log: "every message, tagged by session_id — consolidation reads from here",
  replay_runs: "Replay — one bounded status/summary row per observed Tieru turn",
  replay_events: "Replay — normalized, sequence-ordered, redacted event metadata",
  shadow_patterns: "Shadow — bounded structural workflow aggregates",
  shadow_suggestions: "Shadow — explainable, user-controlled Forge suggestions",
  shadow_observations: "Shadow — idempotent per-Replay-run processing ledger",
  shadow_settings: "Shadow — persisted local enable/disable override",
  capsule_audits: "Capsule — safe export/import provenance and outcome counts",
};
const QUERY_EXAMPLES = [
  "SELECT role, content FROM chat_log ORDER BY id DESC LIMIT 10",
  "SELECT subject, content FROM facts",
  "SELECT session_id, COUNT(*) FROM chat_log GROUP BY session_id",
];
function dbQueryView(){
  return `<div class="meta" style="margin-bottom:10px">A read-only SQL console over <code>state.db</code>
      (the Supabase-editor idea, scoped down). Only <code>SELECT</code> runs — the file is opened read-only,
      so nothing here can change your data.</div>
    <textarea class="sqlbox" id="sqlbox" spellcheck="false" onfocus="markEditing()" oninput="markEditing()">${esc(QUERY_EXAMPLES[0])}</textarea>
    <div style="margin:8px 0"><button class="save" onclick="runQuery()">Run</button>
      <span class="meta" style="margin-left:12px">try: ${QUERY_EXAMPLES.map(q=>`<span class="qexample" onclick="qFill(this.textContent)">${esc(q)}</span>`).join(" &nbsp; ")}</span></div>
    <div id="qout"></div>`;
}

// --- read-only SQL console (item: "a simple query editor like Supabase")
function qFill(sql){ const b=document.getElementById("sqlbox"); if(b){ b.value=sql; runQuery(); } }
async function runQuery(){
  editing = true;   // keep the 5s refresh from wiping the query + results
  const sql = (document.getElementById("sqlbox")||{}).value || "";
  const out = document.getElementById("qout");
  out.innerHTML = `<div class="meta">running…</div>`;
  const r = await postJSON("/api/query", {sql});
  if (r.error){ out.innerHTML = `<div class="card empty" style="color:var(--bad)">${esc(r.error)}</div>`; return; }
  if (!r.rows.length){ out.innerHTML = `<div class="card empty">0 rows</div>`; return; }
  out.innerHTML = `<div class="scrolly"><table><thead><tr>${
    r.columns.map(c=>`<th class="dbcol">${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    r.rows.map(row=>`<tr>${row.map(v=>`<td class="dbcell">${esc(String(v).slice(0,120))}</td>`).join("")}</tr>`).join("")
    }</tbody></table></div><div class="meta" style="margin-top:6px">${r.rows.length} row(s)</div>`;
}

// --- Memory sub-tabs. Memory is the friendly, per-pillar view of what persists;
// the Data tab shows the SAME rows as raw SQLite tables (see the explainer).
function memOverview(d){
  const s = d.stats;
  const mg = d.memory_graph || {entities:[],relations:[]};
  const pillars = [
    ["Semantic","semantic",d.facts.length+" facts","durable, distilled facts about you and your people"],
    ["Episodic","episodic",d.episodes.length+" episodes","dated summaries and events — stays small on purpose"],
    ["Procedural","skills",d.skills.length+" skills","SKILL.md files loaded only when relevant — how to act"],
    ["Graph","graph",mg.relations.length+" relations","typed entities, relationships, provenance, and change over time"],
  ].map(([t,sub,n,desc]) => `<div class="box" style="min-width:0" onclick="location.hash='memory/${sub}'">
      <b>${t} <span class="meta" style="font-weight:400">· ${n}</span></b><span>${desc}</span></div>`).join("");
  return `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
      <b>Memory vs Database — two views of one file.</b>
      <div class="r">This tab is the curated, per-pillar view of what Tieru remembers. The
      <a class="reveal" onclick="location.hash='database'">Database tab</a> shows the exact same
      thing as raw SQLite tables (plus the FTS5 keyword index). Same
      <code>.tieru/state.db</code> — different altitude.
      <br><br>Tieru keeps its queryable source in <code>state.db</code> (facts + episodes,
      FTS5-searchable) <b>and</b> writes a
      human-readable ${reveal("MEMORY.md","MEMORY.md")} mirror after every turn — so you get both: a real file
      you can open, backed by a sturdy database.</div></div>
    <h2>The four memory capabilities</h2>
    <div class="tiles" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">${pillars}</div>
    <h2>Retrieval gate — does this turn even need memory?</h2>${gateSplit(s)}
    <div class="meta" style="margin-top:8px">A cheap model decides <b>if</b> a turn needs memory at all, before any lookup —
      this is memory <i>retrieval</i>, the hero decision. (The Ops tab charts the same skip/retrieve
      numbers as an operational metric; the decision itself is memory's.)</div>
    <div class="meta" style="margin-top:14px">Files: ${reveal("state.db","state.db")} · ${reveal("MEMORY.md","MEMORY.md")} · ${reveal("SOUL.md","SOUL.md")} · ${reveal("skills","skills/")}</div>`;
}
function memGraph(d){
  const mg = d.memory_graph || {entities:[],relations:[]};
  let h = `<div class="card" style="background:var(--accent-soft);border-color:var(--line2)">
    <b>Tieru Memory Graph is an additive SQLite view.</b> <span class="r">Relations are stored only
    through explicit graph-aware memory operations. Existing facts, episodes, and skills remain
    unchanged, and no conversation is converted automatically.</span></div>`;
  h += `<h2>Entities <span class="meta" style="font-weight:400">· ${mg.entities.length}</span></h2>`;
  h += table(["id","name","type","updated"], mg.entities.map(e =>
    `<tr><td class="meta">entity:${e.id}</td><td>${esc(e.canonical_name)}</td>
      <td><code>${esc(e.entity_type)}</code></td><td class="meta">${esc(e.updated_at)}</td></tr>`));
  h += `<h2>Relations <span class="meta" style="font-weight:400">· ${mg.relations.length}</span></h2>`;
  h += table(["id","subject","predicate","object","confidence","importance","status","source"],
    mg.relations.map(x => { const r=x.relation, p=x.provenance;
      return `<tr><td class="meta">relation:${r.id}</td>
        <td>${esc(x.subject.canonical_name)} <span class="meta">${esc(x.subject.entity_type)}</span></td>
        <td><code>${esc(r.predicate)}</code></td>
        <td>${esc(x.object.canonical_name)} <span class="meta">${esc(x.object.entity_type)}</span></td>
        <td class="meta">${Number(r.confidence).toFixed(2)}</td>
        <td class="meta">${Number(r.importance).toFixed(2)}</td>
        <td><span class="pill ${r.status==='active'?'pass':'skip'}">${esc(r.status)}</span></td>
        <td class="meta">${esc(p.source_type)}${p.source_ref?` · ${esc(p.source_ref)}`:""}</td></tr>`;
    }));
  return h || `<div class="card empty">no graph memory yet</div>`;
}
function memSemantic(d){
  let h = `<div class="meta" style="margin-bottom:12px">Durable facts distilled from what you tell Tieru —
    the smallest, most-reused store. Edit or forget any of them; changes are live next turn.</div>`;
  h += `<div class="card" style="padding:4px 8px"><table><tr><th>subject</th><th>fact</th><th>source</th><th></th></tr>${
    d.facts.map(f => `<tr id="fact-${f.id}">
      <td><code>${esc(f.subject)}</code></td>
      <td class="fc">${esc(f.content)}</td>
      <td class="meta">${esc(f.source)}</td>
      <td style="white-space:nowrap"><a class="reveal" onclick="editFact(${f.id})">edit</a> · <a class="reveal del" onclick="delMem('delete_fact',${f.id})">delete</a></td>
    </tr>`).join("")}</table></div>`;
  return h;
}
function memEpisodic(d){
  const src = d.episodes_source || "sqlite";
  let h = `<div class="meta" style="margin-bottom:8px">backend: <span class="srcpill">${esc(src)}</span></div>`;
  if (d.episodes_error) h += `<div class="card empty">Could not read episodes from Notion: ${esc(d.episodes_error)}</div>`;
  h += `<div class="card" style="background:var(--accent-soft);border-color:var(--line2)">
    <b>Why is this small?</b> <span class="r">Episodic memory holds one <i>distilled</i> summary per
    consolidation, not every message. The raw, blow-by-blow conversation lives in the
    <a class="reveal" onclick="location.hash='database/chat_log'"><code>chat_log</code> table</a>
    (the big one) on the Database tab — episodes are its highlights.</span></div>`;
  h += `<div class="card" style="padding:4px 8px"><table><tr><th>date</th><th>episode</th><th></th></tr>${
    d.episodes.map(e => `<tr><td class="meta">${esc(e.happened_at)}</td><td>${esc(e.summary)}</td>
      <td><a class="reveal del" onclick="delMem('delete_episode','${e.id}')">delete</a></td></tr>`).join("")}</table></div>`;
  return h;
}
function memSkills(d){
  let h = `<div class="meta" style="margin-bottom:12px">Procedural memory — markdown instructions loaded
    only when a message matches. Add your own three ways: teach Tieru in chat (it calls
    <code>create_skill</code>), edit a skill below, or drop a <code>SKILL.md</code> into ${reveal("skills","the skills folder")}.</div>`;
  h += d.skills.map((sk,i) => {
    const full = `---
name: ${sk.name}
description: ${sk.description}
---

${sk.body}`;
    return `<div class="card">
      <div class="u"><code>${esc(sk.name)}</code> <span class="meta" style="font-weight:400">· ${esc(sk.description)}</span>
        <span class="srcpill ${sk.editable?"":"apple"}" style="margin-left:6px">${sk.editable?"home":"built-in"}</span></div>
      <textarea class="editor" id="sk-${i}" style="min-height:150px;margin-top:8px" data-path="${esc(sk.path)}"
        oninput="dirty('sksave-${i}')" onfocus="markEditing()">${esc(full)}</textarea>
      <div style="margin-top:8px"><button class="save" id="sksave-${i}" disabled onclick="saveSkill(${i})">Save SKILL.md</button>
        <span class="meta" id="skmsg-${i}" style="margin-left:10px">${esc(sk.rel)}</span></div></div>`;
  }).join("") || `<div class="card empty">no skills loaded</div>`;
  return h;
}
function memSoul(d){
  return `<div class="meta" style="margin-bottom:12px">SOUL.md is Tieru's persona — the system prompt it
    loads every turn. Editing it changes who your Tieru is. Changes are live next turn.</div>
    <div class="card"><textarea id="soul" class="editor" style="min-height:260px"
      oninput="dirty('soul-save')" onfocus="markEditing()">${esc(d.soul||"")}</textarea>
    <div style="margin-top:8px"><button class="save" id="soul-save" disabled onclick="saveSoul()">Save SOUL.md</button>
      <span class="meta" id="soul-msg" style="margin-left:10px"></span></div></div>
    <div class="meta" style="margin-top:10px">${reveal("SOUL.md","open SOUL.md in your editor")}</div>`;
}
function memConsolidation(d){
  const distilled = d.facts.filter(f => f.source==="consolidation");
  let h = d.memory_write_policy !== "consolidate"
    ? `<div class="card"><b>Consolidation is disabled.</b> <span class="r">The active
      <code>explicit</code> policy keeps ordinary chats out of long-term memory. Explicit saves and
      human edits still work. Select the <code>consolidate</code> policy in YAML to opt in.</span></div>`
    : `<div class="card"><b>How it works.</b> <span class="r">Every ${d.consolidate_every} exchanges,
    a cheap model reads the unconsolidated ${"<code>chat_log</code>"} and distills it into durable
    <b>facts</b> (semantic) plus one <b>episode</b> (episodic). Batching keeps it cheap and gives the
    summarizer enough context to pick what's worth keeping.</span></div>`;
  h += `<div class="tiles" style="margin-top:12px">
    <div class="tile"><b>${d.chat_pending}</b><span>messages queued</span></div>
    <div class="tile"><b>${d.consolidate_every*2}</b><span>trigger threshold</span></div>
    <div class="tile"><b>${distilled.length}</b><span>facts from consolidation</span></div>
    <div class="tile"><b>${d.episodes.length}</b><span>episodes total</span></div></div>`;
  h += `<h2>Facts it distilled</h2>`;
  h += table(["subject","fact","when"], distilled.map(f =>
    `<tr><td><code>${esc(f.subject)}</code></td><td>${esc(f.content)}</td><td class="meta">${esc((f.created_at||"").slice(0,10))}</td></tr>`));
  h += `<div class="meta" style="margin-top:10px">This is a memory operation, shown here. Each run is also
    <a class="reveal" onclick="location.hash='ops'">traced</a> (Ops) and can be scored by the judge evals.</div>`;
  return h;
}

// Tools ▸ Results: the artifacts tool calls produced (kept distinct from the
// tools themselves — the old tab conflated capability with output).
function toolsResults(d){
  let h = `<div class="meta" style="margin-bottom:10px">What tool calls actually wrote. These are results, not the tools.</div>`;
  h += `<h2>Calendar events <span class="meta" style="font-weight:400">· from create_event</span></h2>`;
  h += table(["event","start","end","with"], d.calendar.map(e =>
    `<tr><td>${esc(e.title)}</td><td class="meta">${esc(e.start)}</td><td class="meta">${esc(e.end)}</td><td>${esc(e.attendees)}</td></tr>`));
  h += `<div class="meta" style="margin-bottom:16px">also written to <code>calendar.ics</code> — ${reveal("calendar.ics","reveal calendar.ics in Finder")} (double-click to import into Calendar.app)</div>`;
  h += `<h2>Outbox — drafted messages <span style="font-weight:400;text-transform:none;letter-spacing:0">· ${reveal("outbox","open the outbox folder")}</span></h2>`;
  h += d.outbox.length ? d.outbox.map(o=>`<div class="card"><span class="u">${esc(o.name)}</span><div class="r">${esc(o.text)}</div></div>`).join("")
                       : `<div class="card empty">no drafted messages</div>`;
  return h;
}
// Tools ▸ MCP: external connectors. Shows live status + a copy-paste config so
// anyone can plug in their own server (scalable, not a one-off).
function toolsMCP(t){
  const m = t.mcp;
  let h = `<div class="card ${m.configured?"":""}" style="border-color:${m.live?"var(--good)":"var(--line2)"}">
    <b>Model Context Protocol${m.live?" — connected":m.configured?" — configured":" — not set up"}.</b>
    <div class="r">MCP lets Tieru borrow tools from any external server (files, GitHub, a database, …),
    namespaced <code>&lt;server&gt;_&lt;tool&gt;</code>. ${m.configured
      ? `Configured servers: ${m.servers.map(s=>`<code>${esc(s)}</code>`).join(" ")}${m.live?"":" — start a chat to connect them."}`
      : "None configured yet."}</div></div>`;
  h += `<h2>Connect one (30 seconds)</h2><div class="card">
    <div class="meta">1 — install the extra: <code>pip install -e '.[mcp]'</code></div>
    <div class="meta" style="margin-top:6px">2 — create ${reveal("","the .tieru folder")}<code>/mcp.json</code>:</div>
    <pre style="font-family:var(--mono);font-size:11.5px;color:var(--ink2);white-space:pre-wrap;margin-top:8px">{"servers": [
  {"name": "fs", "command": "npx",
   "args": ["-y", "@modelcontextprotocol/server-filesystem", "${esc(D&&D.home||"")}"]}
]}</pre>
    <div class="meta" style="margin-top:8px">3 — restart the dashboard. The server's tools appear above under
      <a class="reveal" onclick="location.hash='tools/available'">Available ▸ MCP servers</a>, callable in chat.</div></div>`;
  h += `<div class="meta" style="margin-top:12px">The same pattern scales: any MCP server (yours or a vendor's)
    plugs in the same way — no code changes to Tieru. Skills work the same way — drop a <code>SKILL.md</code>
    in ${reveal("skills","skills/")}.</div>`;
  return h;
}

let REPLAY_DETAIL = null, REPLAY_LOADING = "";
async function loadReplayDetail(runId){
  if (REPLAY_LOADING === runId) return;
  REPLAY_LOADING = runId;
  try {
    const response = await fetch(`/api/replay/${encodeURIComponent(runId)}`);
    REPLAY_DETAIL = await response.json();
  } catch (e) {
    REPLAY_DETAIL = {run_id:runId, error:String(e)};
  } finally {
    REPLAY_LOADING = "";
    const [, current] = (location.hash||"").slice(1).split("/");
    if (current === runId) render();
  }
}

let FORGE_DETAIL = null, FORGE_LOADING = "";
async function loadForgeDetail(draftId){
  if (FORGE_LOADING === draftId) return;
  FORGE_LOADING = draftId;
  try {
    const response = await fetch(`/api/forge/${encodeURIComponent(draftId)}`);
    FORGE_DETAIL = await response.json();
  } catch(e){ FORGE_DETAIL = {draft_id:draftId,error:String(e)}; }
  FORGE_LOADING = ""; render();
}
async function forgeAction(action, draftId, approved=false){
  const body = {action, draft_id:draftId, approved};
  if (action === "forge"){
    body.run_ids = [...document.querySelectorAll(".forge-run:checked")].map(x=>x.value);
  }
  const result = await postJSON("/api/forge", body);
  if (result.error){ alert(result.error); return; }
  FORGE_DETAIL = result; await refresh();
  if (result.draft_id) location.hash = `forge/${result.draft_id}`;
}

let SHADOW_DETAIL = null, SHADOW_LOADING = "";
async function loadShadowDetail(suggestionId){
  if (SHADOW_LOADING === suggestionId) return;
  SHADOW_LOADING = suggestionId;
  try {
    const response = await fetch(`/api/shadow/${encodeURIComponent(suggestionId)}`);
    SHADOW_DETAIL = await response.json();
  } catch(e){ SHADOW_DETAIL = {error:String(e)}; }
  SHADOW_LOADING = ""; render();
}
async function shadowAction(action, suggestionId=""){
  const result = await postJSON("/api/shadow", {action, suggestion_id:suggestionId});
  if (result.error){ alert(result.error); return; }
  if (action === "forge" && result.draft_id){
    FORGE_DETAIL = result; await refresh(); location.hash=`forge/${result.draft_id}`; return;
  }
  SHADOW_DETAIL = null; await refresh(); location.hash="#shadow";
}

let CAPSULE_RESULT = null;
async function capsuleAction(action){
  editing = true;
  const filename = (document.getElementById("capsule-file")||{}).value || "my-tieru.tieru";
  const include = ["replay","forge","shadow"].filter(x => (document.getElementById(`cap-${x}`)||{}).checked);
  const body = {action, filename, profile:"portable", include};
  if (action === "import") {
    if (!confirm("Import the verified preview? Existing conflicts will be retained and Trust rules stay inactive.")) return;
    body.confirmed = true;
  }
  CAPSULE_RESULT = await postJSON("/api/capsule", body);
  render();
}

function capsuleView(){
  const result = CAPSULE_RESULT;
  const summary = result ? `<h2>Result</h2><pre>${esc(JSON.stringify(result,null,2))}</pre>` : "";
  return `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
    <b>Your AI identity belongs to you, not a provider or device.</b>
    <div class="r" style="margin-top:6px">Capsules are offline, selective snapshots. Secrets are structurally excluded; importing never creates a live sync link or grants authority.</div></div>
    <h2>Export</h2><div class="card"><label>Filename in <code>TIERU_HOME/capsules</code><br>
    <input id="capsule-file" value="my-tieru.tieru" onfocus="markEditing()" style="margin-top:6px;max-width:360px"></label>
    <div style="margin:10px 0"><b>Portable:</b> persona, Memory + Graph, user skills, preferences, pending-review Trust, Fabric.</div>
    <label><input type="checkbox" id="cap-replay"> Replay history</label> &nbsp;
    <label><input type="checkbox" id="cap-forge"> inactive Forge drafts</label> &nbsp;
    <label><input type="checkbox" id="cap-shadow"> advisory Shadow metadata</label>
    <div class="meta" style="margin:8px 0">API keys, credentials, auth headers, caches, traces, and temporary files are always excluded.</div>
    <button class="save" onclick="capsuleAction('export')">Export Capsule</button></div>
    <h2>Inspect / import</h2><div class="card"><div class="meta" style="margin-bottom:8px">Place a Capsule in <code>TIERU_HOME/capsules</code>, enter its filename above, then verify and preview. Execution requires a separate confirmation.</div>
    <button onclick="capsuleAction('inspect')">Inspect integrity</button>
    <button onclick="capsuleAction('preview')">Preview ImportPlan</button>
    <button class="save" onclick="capsuleAction('import')">Execute confirmed import</button></div>${summary}`;
}

const VIEWS = {
  capsule(){ return capsuleView(); },
  // Gateway: ONE unified conversation across every channel (dashboard, telegram,
  // voice, cli) — the same loop + memory answer all of them. Each message is
  // tagged with where it came in. You type in the dock on the right.
  // Gateway = an INBOX of conversations (like Slack/Intercom): one row per
  // conversation, tagged with its channel(s). Click one to open it in the chat
  // dock (the active thread). No longer a flat stream that duplicates the dock.
  gateway(d){
    const sessions = d.sessions || [];
    let h = `<div class="meta" style="margin-bottom:14px">Every conversation across every channel —
      web, phone (Telegram), voice, terminal — answered by the same brain. Click one to open it in the
      chat dock &rarr;. This is the inbox; the dock is the open thread.</div>`;
    if (!sessions.length)
      return h + `<div class="card empty">no conversations yet — say something in the chat dock &rarr;</div>`;
    h += sessions.map(s => {
      const tags = gwTags(s);
      const on = s.id === SESSION;
      return `<div class="toolcard" style="cursor:pointer${on?';border-color:var(--accent)':''}" onclick="openConversation('${esc(s.id)}')">
        <div class="tn" style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
          <span>${esc(s.title||s.id)} ${tags}</span>
          <span class="meta" style="font-weight:400;white-space:nowrap">${sessionMeta(s)}</span></div>
        <div class="td">${esc(s.last||"")}</div></div>`;
    }).join("");
    return h;
  },
  overview(d){
    const s = d.stats;
    const u = d.usage || {total_cost:0};
    const tiles = [
        [money(u.total_cost),"spent · all-time","money"],[secs(s.latency_avg),"avg turn",""],
        [s.turns,"turns",""],[s.tool_calls,"tool calls",""],
        [d.facts.length,"facts",""],[d.calendar.length,"events",""],
      ].map(([v,l,c])=>`<div class="tile"><b class="${c}">${v}</b><span>${l}</span></div>`).join("");
    return `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
      <b>Tieru — Local-first Personal AI Runtime.</b>
      <div class="r" style="margin-top:6px">One memory. Any model. Your rules. Inspect the runtime,
      memory, models, tools, permission decisions, traces, and evaluations from this local dashboard.</div>
    </div><div class="tiles">${tiles}</div>
    <h2>Retrieval gate — the hero decision</h2>${gateSplit(s)}
    <h2 style="margin-top:26px">Architecture — click any box <span class="arch-status"></span></h2>
    ${archSVG(d)}
    <h2>Graph workflows — when a turn needs shape</h2>
    ${graphPanel(d)}
    <h2>Latest turn</h2>${d.turns.length?turnCard(d.turns[0]):'<div class="card empty">no turns yet — talk to Tieru first</div>'}`;
  },
  loop(d){
    return d.turns.length ? d.turns.map(turnCard).join("") : `<div class="card empty">no turns yet</div>`;
  },
  // Graph workflows: the loop's sibling. The chart is rendered from the
  // engine's own describe() (served in d.graph.workflows) so it can never
  // show a shape the engine doesn't run. Nothing here is a mode switch —
  // the harness routes every message itself; this tab just tells the story.
  graph(d){
    const g = d.graph || {enabled:false, workflows:[], stats:{quick:0, full:0}};
    let h = `<div class="meta" style="margin-bottom:14px">The loop is one agent turn: the model picks tools until
      it stops. Some work has <b>shape</b> — steps that can run at the same time, and explicit "if this, go
      there" routing. A <b>graph workflow</b> makes that shape first-class: nodes (each does one job) connected
      by edges (what happens next). The loop did not change one line — the <code>full_agent</code> node below
      IS the same loop, running as one step. The harness routes every message itself — and workflows you
      can also call BY NAME from the chat box: type <code>/graphs</code> to see them.</div>`;
    if (!g.enabled)
      h += `<div class="card"><b>Off</b> — every turn currently runs the classic loop.
        <div class="meta" style="margin-top:6px">Switch on <b>graph workflows</b> in
        <a class="reveal" onclick="location.hash='settings'">Settings</a>, or set
        <code>TIERU_GRAPH_WORKFLOWS=1</code> in <code>.env</code>. Any failure anywhere fails open to the
        plain loop — this can never lose a reply, only save time and tokens.</div></div>`;
    // The two workflows are two different JOBS with different triggers, which is
    // the thing the page has to make obvious — otherwise two stacked charts read
    // like two options you pick between.
    const NOTE = {
      triage: `<b>Runs itself, on every message.</b> Gated by the graph-workflows flag.
        Solid arrows = always, dashed = the router's choice. <code>full_agent</code> is the
        ordinary loop running as one node — a graph does not replace the loop, it arranges calls to it.`,
      gather: `<b>Runs when you start it</b> — <code>make gather</code> or the button below — and
        ignores the flag entirely. The four scans have no dependencies on each other, so the engine
        runs them in ONE WAVE: together, not in turn. It proposes and never acts; the digest lands
        in the outbox for you to read.`,
    };
    (g.workflows || []).forEach(w => {
      if (!w) return;
      h += `<h2>${esc(w.name)} — live topology <span class="arch-status"></span></h2>`;
      const tot = g.stats.quick + g.stats.full;
      const extra = w.name === "triage" && tot
        ? ` · ${g.stats.quick} quick / ${g.stats.full} full so far` : "";
      h += `<div class="card">${graphSVG(w)}
        <div class="meta" style="margin-top:8px">${NOTE[w.name] || ""}${extra} ·
        drawn from the engine's own <code>describe()</code>, so this picture cannot drift from the code</div></div>`;
      if (w.name === "gather") h += graphRunPanel();
    });
    const gturns = (d.turns||[]).filter(t => t.graph && t.graph.route);
    h += `<h2>Graph turns</h2>`;
    h += gturns.length
      ? gturns.slice(0,20).map(t => `<div class="card">
          <div class="u">${esc(t.user_message)}</div>
          <div class="meta" style="margin-top:4px"><span class="badge ${t.graph.route==="quick"?"":"retrieve"}">graph · ${esc(t.graph.route)}</span>
            <span class="meta" style="margin:0">${esc(t.graph.reason||"")}</span></div>
          <div class="r">${renderMarkdown(t.reply||"")}</div></div>`).join("")
      : `<div class="card empty">no graph turns yet — ${g.enabled
          ? 'say "thanks!" in the chat and watch it take the quick door'
          : "switch the flag on first"}</div>`;
    return h;
  },
  memory(d, sub){
    sub = sub || "overview";
    const mg = d.memory_graph || {entities:[],relations:[]};
    const tabs = [["overview","Overview"],["semantic","Semantic",d.facts.length],
      ["episodic","Episodic",d.episodes.length],["skills","Skills",d.skills.length],
      ["graph","Graph",mg.relations.length],["soul","SOUL"],
      ["consolidation","Consolidation",d.chat_pending]];
    let h = subtabBar("memory", tabs, sub);
    if (sub==="semantic") return h + memSemantic(d);
    if (sub==="episodic") return h + memEpisodic(d);
    if (sub==="skills") return h + memSkills(d);
    if (sub==="graph") return h + memGraph(d);
    if (sub==="soul") return h + memSoul(d);
    if (sub==="consolidation") return h + memConsolidation(d);
    return h + memOverview(d);
  },
  settings(d){
    const st = d.settings || {providers:[]};
    let h = `<div class="card">Current: <b>${esc(st.provider)}</b> · loop brain <code>${esc(st.model)}</code> · gate &amp; summarizer <code>${esc(st.small_model)}</code><div class="meta" style="margin:4px 0 0">two jobs, two brains: the loop brain answers you; the small gate model decides memory retrieval and distills chats</div></div>`;
    h += yourModelsCard(st);
    h += `<h2>Provider &amp; keys (BYOK)</h2><div class="card">
      <label class="fld">Provider
        <select id="set-provider" onfocus="markEditing()">${st.providers.map(p=>`<option value="${p.name}" ${p.name===st.provider?"selected":""}>${p.name}${p.name===st.provider?` — now: ${esc(st.model)}`:` — provider default: ${esc(p.default_model)}`}</option>`).join("")}</select></label>
      ${st.base_url?`<div class="meta" style="margin:4px 0 8px">Custom endpoint active: <code>${esc(st.base_url)}</code> (TIERU_BASE_URL${st.custom_key_set?" + TIERU_API_KEY":""}). The model list below comes from it.</div>`:""}
      <details class="adv"><summary>Type a model id manually (advanced; the catalog below switches in one click)</summary>
      <label class="fld">Model (runs the loop; needs tool calling) <input id="set-model" list="model-list" onfocus="markEditing()" placeholder="blank = provider default" value="${st.model===st.providers.find(p=>p.name===st.provider)?.default_model?"":esc(st.model)}"></label>
      <label class="fld">Gate / summary model (the small model that decides whether a message needs memory, and distills chats into facts; pick something cheap and terse) <input id="set-small-model" list="model-list" onfocus="markEditing()" placeholder="blank = provider default" value="${st.small_model===st.providers.find(p=>p.name===st.provider)?.default_small_model?"":esc(st.small_model)}"></label>
      <datalist id="model-list"></datalist>
      <div class="meta" id="model-list-msg" style="margin:4px 0 8px"></div></details>${(setTimeout(loadModelList,0),"")}
      <details class="adv" ${st.providers.find(p=>p.name===st.provider)?.key_set?"":"open"}><summary>API keys (${st.providers.find(p=>p.name===st.provider)?.key_set?`${esc(st.provider)} key set`:`${esc(st.provider)} key needed`})</summary>
      <div class="meta" style="margin:10px 0 4px">Keys stay in your local <code>.env</code> — never sent back to this page (only a set/not-set status and the last 4 digits). Leave a field blank to keep the current key.</div>
      ${st.providers.map(p=>`<label class="fld"><span>${p.name} key <span class="meta">(${p.key_env})</span>
        ${p.key_set?`<span class="srcpill" style="background:var(--good-soft);color:var(--good)">set ····${esc(p.key_last4)}</span>`
                   :`<span class="srcpill apple">not set</span>`}</span>
        <input type="password" data-key="${p.key_env}" placeholder="${p.key_set?"key on file — blank keeps it":"paste key"}"></label>`).join("")}
      </details>
      <div style="margin-top:12px"><button class="save" onclick="saveSettings()">Save &amp; switch</button>
        <span class="meta" id="set-msg" style="margin-left:10px"></span></div>
    </div>
    <h2>Experimental tools</h2><div class="card">
      <div class="meta" style="margin-bottom:8px">Off by default. Turns on <code>delegate_task</code>, which hands
        a coding job to <b>pi</b> — a separate coding agent running locally, on this same model. The Arena's
        <b>coding (pi)</b> checkbox switches this on per-race; this switches it on for the <b>chat</b>.</div>
      <label class="fld">Sub-agent delegation
        <select id="set-experimental" onfocus="markEditing()">
          <option value="" ${!st.experimental?"selected":""}>off — the flagship tools only (default)</option>
          <option value="1" ${st.experimental?"selected":""}>on — chat can delegate coding to pi</option>
        </select></label>
      ${st.pi_installed
        ? `<div class="meta"><span class="srcpill" style="background:var(--good-soft);color:var(--good)">pi found</span> on this machine</div>`
        : `<div class="meta"><span class="srcpill apple">pi not installed</span> — <code>npm install -g --ignore-scripts @earendil-works/pi-coding-agent</code></div>`}
      <div style="margin-top:12px"><button class="save" onclick="saveSettings()">Save &amp; switch</button>
        <span class="meta" style="margin-left:10px">rebuilds the agent in-process — no restart</span></div>
    </div>
    <h2>Graph workflows</h2><div class="card">
      <div class="meta" style="margin-bottom:8px">Off by default. When on, <b>every</b> message is triaged
        through a graph first: a small model classifies it while today's calendar loads in parallel — trivial
        messages get a fast small-model reply, real tasks run the exact same loop as a node. This flag governs
        the AUTOMATIC door only — workflows you call by name (<code>/gather</code>) run either way. Any
        failure fails open to the plain loop. Watch it live on the
        <a class="reveal" onclick="location.hash='graph'">Graph</a> tab.</div>
      <label class="fld">Triage-first turns
        <select id="set-graph-workflows" onfocus="markEditing()">
          <option value="" ${!st.graph_workflows?"selected":""}>off — every turn runs the classic loop (default)</option>
          <option value="1" ${st.graph_workflows?"selected":""}>on — triage graph routes each message</option>
        </select></label>
      <div style="margin-top:12px"><button class="save" onclick="saveSettings()">Save &amp; switch</button>
        <span class="meta" style="margin-left:10px">rebuilds the agent in-process — no restart</span></div>
    </div>
    <h2>Episodic memory</h2><div class="card">
      <div class="meta" style="margin-bottom:8px">Where dated episode summaries live. Default is the local
        <code>state.db</code> (zero setup). Pick <code>notion</code> to store them in a Notion database instead
        (requires <code>pip install -e '.[notion]'</code>).</div>
      <label class="fld">Backend
        <select id="set-episodic-store" onfocus="markEditing()">
          <option value="sqlite" ${st.episodic_store!=="notion"?"selected":""}>sqlite — local state.db (default)</option>
          <option value="notion" ${st.episodic_store==="notion"?"selected":""}>notion — a Notion database</option>
        </select></label>
      <label class="fld"><span>Notion token <span class="meta">(NOTION_TOKEN)</span>
        ${st.notion_token_set?`<span class="srcpill" style="background:var(--good-soft);color:var(--good)">set ····${esc(st.notion_token_last4)}</span>`
                             :`<span class="srcpill apple">not set</span>`}</span>
        <input type="password" data-key="NOTION_TOKEN" placeholder="${st.notion_token_set?"key on file — blank keeps it":"paste integration token"}"></label>
      <label class="fld"><span>Notion database link <span class="meta">(paste the link from Notion)</span>
        ${st.notion_db_set?`<span class="srcpill" style="background:var(--good-soft);color:var(--good)">set ····${esc(st.notion_db_last4)}</span>`
                          :`<span class="srcpill apple">not set</span>`}</span>
        <input data-key="NOTION_EPISODES_DATABASE_ID" placeholder="${st.notion_db_set?"database link on file — blank keeps it":"paste the database link"}"></label>
      <div style="margin-top:12px"><button class="save" onclick="saveSettings()">Save &amp; switch</button>
        <span class="meta" style="margin-left:10px">rebuilds the agent in-process — a bad token leaves notion selected with the error shown on Memory ▸ Episodic; fix the token or switch back</span></div>
    </div>
    <h2 id="catalog-h" style="display:none">Model catalog: click to switch</h2>
    <div class="card" id="catalog" style="display:none"></div>
    <h2>Web search key (optional)</h2><div class="card">
      <div class="meta" style="margin-bottom:8px">A free <a class="reveal" onclick="window.open('https://tavily.com','_blank')">Tavily</a> key makes the <code>search_web</code> tool reliable (the World Cup demo). Stored in your local <code>.env</code>, same as above.</div>
      <label class="fld"><span>Tavily key <span class="meta">(${esc(st.search_key_env||"TAVILY_API_KEY")})</span>
        ${st.search_key_set?`<span class="srcpill" style="background:var(--good-soft);color:var(--good)">set ····${esc(st.search_key_last4)}</span>`
                          :`<span class="srcpill apple">not set</span>`}</span>
        <input type="password" data-key="TAVILY_API_KEY" placeholder="${st.search_key_set?"key on file — blank keeps it":"paste key"}"></label>
      <div style="margin-top:12px"><button class="save" onclick="saveSettings()">Save</button>
        <span class="meta" style="margin-left:10px">reads live — no restart needed for search</span></div>
      <div class="meta" style="margin-top:10px">Note: running terminal / voice / Telegram gateways keep their old provider until restarted.</div>
    </div>`;
    return h;
  },
  tools(d, sub){
    const t = d.tools || {catalog:[], mcp:{configured:false,servers:[],live:false}, apple_on:false};
    sub = sub || "available";
    const tabs = [["available","Available",t.catalog.length],["results","Results"],
      ["mcp","MCP",t.mcp.servers.length||null]];
    let h = subtabBar("tools", tabs, sub);
    if (sub === "results") return h + toolsResults(d);
    if (sub === "mcp") return h + toolsMCP(t);
    // Available: what the agent CAN do (grouped by origin), not just what it did.
    h += `<div class="meta" style="margin-bottom:12px">The capabilities the agent can call this turn.
      A tool is a name + description the model reads, a JSON schema, and a Python function — that's it.
      ${t.apple_on?"":"Apple tools are off (set <code>TIERU_APPLE_TOOLS=1</code>). "}Connect more via
      <a class="reveal" onclick="location.hash='tools/mcp'">MCP</a>.</div>`;
    const SRC = [["flagship","Flagship task — scheduling"],["web","Web search"],
      ["self-management","Self-management — it edits its own memory"],
      ["apple","Apple ecosystem"],["mcp","MCP servers"],["other","Other"]];
    const publicDescription = c => c.description;
    SRC.forEach(([key,label]) => {
      const items = t.catalog.filter(c => c.source === key);
      if (!items.length) return;
      h += `<h2>${label}</h2>`;
      h += items.map(c => `<div class="toolcard">
        <div class="tn">${esc(c.name)}<span class="srcpill ${key==="mcp"?"mcp":key==="apple"?"apple":""}">${esc(key)}</span></div>
        <div class="td">${esc(publicDescription(c))}</div></div>`).join("");
    });
    // Roadmap: whiteboard boxes not wired in yet — set expectations, don't over-promise.
    if ((t.planned||[]).length){
      h += `<h2>Coming soon <span class="meta" style="font-weight:400">· on the architecture chart, not wired in yet (opt in with <code>TIERU_EXPERIMENTAL=1</code>)</span></h2>`;
      h += t.planned.map(p => `<div class="toolcard" style="opacity:.7">
        <div class="tn">${esc(p.name)}<span class="srcpill apple">soon · ${esc(p.box)}</span></div>
        <div class="td">${esc(p.description)}</div></div>`).join("");
    }
    return h;
  },
  replay(d, sub){
    const replay = d.replay || {runs:[],retention:{}};
    if (sub){
      if (!REPLAY_DETAIL || REPLAY_DETAIL.run_id !== sub){
        loadReplayDetail(sub);
        return `<div class="card empty">loading Replay run <code>${esc(sub)}</code>…</div>`;
      }
      if (REPLAY_DETAIL.error)
        return `<div class="card"><span class="pill fail">unavailable</span> ${esc(REPLAY_DETAIL.error)}</div>`;
      const run = REPLAY_DETAIL, summary = run.summary || {}, events = run.events || [];
      let h = `<div style="margin-bottom:12px"><a class="reveal" onclick="location.hash='replay'">&larr; all runs</a></div>
        <div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
          <div class="tn"><code>${esc(run.run_id)}</code> <span class="pill ${run.status==='completed'?'pass':run.status==='failed'?'fail':'skip'}">${esc(run.status)}</span></div>
          <div class="meta">${esc(run.source)} · session ${esc(run.session_id)} · ${esc(run.provider||'—')} / ${esc(run.model||'—')} · ${run.latency_ms==null?'running':esc(run.latency_ms)+'ms'} · ${esc(run.iterations)} iteration(s)</div>
          ${run.error_summary?`<div class="r" style="margin-top:8px"><b>${esc(run.error_code)}</b> · ${esc(run.error_summary)}</div>`:""}
        </div>`;
      h += `<h2>Deterministic summary</h2><div class="card"><b>${esc(summary.sentence||"")}</b>
        <div class="meta" style="margin-top:7px">Tools: ${esc(JSON.stringify(summary.tools||{}))} · Trust: ${esc(JSON.stringify(summary.trust||{}))} · Memory: ${esc((summary.memory||[]).join(', ')||'none')}</div></div>`;
      h += `<h2>Ordered timeline</h2><div class="meta" style="margin-bottom:10px">Observable events and structured system decisions only. Replay never stores or invents private chain-of-thought.</div>`;
      h += events.length ? `<div class="card">${events.map(e => {
        const subject=e.tool||e.model||e.node||"";
        const dur=e.duration_ms==null?"":` · ${esc(e.duration_ms)}ms`;
        return `<div style="display:grid;grid-template-columns:42px 92px 1fr;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)">
          <div class="meta" style="font-family:var(--mono)">${String(e.sequence).padStart(2,'0')}</div>
          <div><span class="srcpill">${esc(e.category)}</span></div>
          <div><b>${esc(e.event_type)}</b>${subject?` · <code>${esc(subject)}</code>`:""}<span class="meta">${dur}</span>
            ${Object.keys(e.safe_payload||{}).length?`<details style="margin-top:5px"><summary class="meta">safe details</summary><pre>${esc(JSON.stringify(e.safe_payload,null,2))}</pre></details>`:""}</div></div>`;
      }).join("")}</div>` : `<div class="card empty">no events recorded</div>`;
      return h;
    }
    const runs = replay.runs || [], limits = replay.retention || {};
    let h = `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
      <b>Every important Tieru action should be inspectable after the run.</b>
      <div class="r" style="margin-top:6px">Replay is local, read-only observation: ordered lifecycle, memory, model, routing, Trust, tool, error, and output metadata. Viewing a run never executes tools or side effects.</div></div>`;
    h += `<div class="meta" style="margin:10px 0">Retention: ${esc(limits.max_runs||0)} runs / ${esc(limits.max_age_days||0)} days · event payloads ≤ ${esc(limits.max_event_payload_bytes||0)} bytes · tool previews ≤ ${esc(limits.max_tool_output_bytes||0)} bytes. Replay cleanup does not delete chat or Memory.</div>`;
    h += runs.length ? table(["run","time","status","source","model","duration","tools"], runs.map(r =>
      `<tr><td><a class="reveal" onclick="location.hash='replay/${esc(r.run_id)}'"><code>${esc(r.run_id)}</code></a></td>
       <td class="meta">${esc((r.started_at||'').replace('T',' ').slice(0,19))}</td>
       <td><span class="pill ${r.status==='completed'?'pass':r.status==='failed'?'fail':'skip'}">${esc(r.status)}</span></td>
       <td class="meta">${esc(r.source)}</td><td class="meta">${esc(r.model||'—')}</td>
       <td class="meta">${r.latency_ms==null?'—':esc(r.latency_ms)+'ms'}</td><td class="meta">${esc(r.tool_count)}</td></tr>`))
      : `<div class="card empty">no Replay runs yet — the next Tieru turn will appear here</div>`;
    return h;
  },
  shadow(d, sub){
    const shadow=d.shadow||{status:{},patterns:[],suggestions:[]};
    const st=shadow.status||{};
    if (sub){
      if (!SHADOW_DETAIL || (SHADOW_DETAIL.suggestion||{}).suggestion_id !== sub){
        loadShadowDetail(sub);
        return `<div class="card empty">loading Shadow suggestion <code>${esc(sub)}</code>...</div>`;
      }
      if (SHADOW_DETAIL.error) return `<div class="card"><span class="pill fail">unavailable</span> ${esc(SHADOW_DETAIL.error)}</div>`;
      const s=SHADOW_DETAIL.suggestion||{}, p=SHADOW_DETAIL.pattern||{};
      return `<div style="margin-bottom:12px"><a class="reveal" onclick="location.hash='shadow'">&larr; Shadow overview</a></div>
        <div class="card" style="border-color:var(--accent);background:var(--accent-soft)"><div class="tn">Repeated workflow detected <span class="pill skip">${esc((s.status||'ready').toUpperCase())}</span></div><div class="r" style="margin-top:6px">${esc(s.summary)}</div></div>
        <h2>Why am I seeing this?</h2><div class="card">${(s.explanation||[]).map(x=>`<div style="margin:5px 0">&bull; ${esc(x)}</div>`).join('')}</div>
        <h2>Pattern evidence</h2><div class="card"><b>${esc(p.occurrence_count)} occurrences &middot; ${esc(p.confidence)} confidence</b>
          <div class="meta" style="margin-top:7px">First seen ${esc(p.first_seen_at)} &middot; last seen ${esc(p.last_seen_at)}</div>
          <div class="meta">Representative runs: ${(s.representative_run_ids||[]).map(id=>`<code>${esc(id)}</code>`).join(' ')}</div>
          <div class="meta">Tools: ${esc((s.required_tools||[]).join(', '))}</div>
          <div class="meta">Capabilities: ${esc((s.required_capabilities||[]).join(', ')||'none')}</div></div>
        <h2>Common steps</h2>${(p.representative_operations||[]).map((op,i)=>`<div class="card"><b>${i+1}. ${esc(op)}</b>${p.representative_tools[i]?`<div class="meta">tool <code>${esc(p.representative_tools[i])}</code></div>`:''}</div>`).join('')}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"><button class="save" onclick="shadowAction('forge','${esc(s.suggestion_id)}')">Forge Skill</button><button onclick="shadowAction('ignore','${esc(s.suggestion_id)}')">Ignore</button><button onclick="shadowAction('snooze','${esc(s.suggestion_id)}')">Snooze</button><button onclick="shadowAction('dismiss','${esc(s.suggestion_id)}')">Dismiss</button></div>
        <div class="meta" style="margin-top:10px">Forge Skill creates an inactive M9 draft only. It does not validate, approve, install, execute, or grant permission.</div>`;
    }
    let h=`<div class="card" style="border-color:var(--accent);background:var(--accent-soft)"><b>Shadow observes. Forge creates. Trust authorizes. You decide.</b><div class="r" style="margin-top:6px">Shadow groups completed Tieru Replay workflows by secret-safe structure. It never watches OS activity, executes tools, creates active skills, or changes policy.</div></div>`;
    h += `<div class="tiles">${[[st.enabled?'enabled':'disabled','Shadow'],[st.pattern_count||0,'patterns'],[st.suggestion_count||0,'suggestions ready'],[st.covered_count||0,'covered']].map(([v,l])=>`<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join('')}</div>`;
    h += `<div style="margin:10px 0"><button onclick="shadowAction('${st.enabled?'disable':'enable'}')">${st.enabled?'Disable Shadow':'Enable Shadow'}</button><span class="meta" style="margin-left:10px">Default is disabled; disabling preserves existing metadata.</span></div>`;
    h += `<h2>Suggestions</h2>`;
    h += (shadow.suggestions||[]).length ? (shadow.suggestions||[]).map(s=>`<div class="toolcard" onclick="location.hash='shadow/${esc(s.suggestion_id)}'" style="cursor:pointer"><div class="tn">${esc(s.suggested_name)} <span class="pill skip">${esc(s.status)}</span></div><div class="td">${esc(s.summary)}</div><div class="meta">${esc(s.occurrence_count)} occurrences &middot; ${esc(s.confidence)} confidence</div></div>`).join('') : `<div class="card empty">no suggestions ready</div>`;
    h += `<h2>Observed patterns</h2>`;
    h += (shadow.patterns||[]).length ? table(["workflow","occurrences","first seen","last seen","confidence","status"],shadow.patterns.map(p=>`<tr><td class="meta">${esc((p.representative_operations||[]).join(' → '))}</td><td>${esc(p.occurrence_count)}</td><td class="meta">${esc(p.first_seen_at)}</td><td class="meta">${esc(p.last_seen_at)}</td><td>${esc(p.confidence)}</td><td><span class="pill skip">${esc(p.status)}</span></td></tr>`)) : `<div class="card empty">no patterns observed</div>`;
    return h;
  },
  fabric(d){
    // Compatibility truth: Model Fabric v1 uses execution-mode routing and
    // does not adaptively select cloud providers; v2 preserves that first
    // decision and adds explicit, policy-first candidate selection.
    const f=d.fabric||{modes:[],models:[],recent_routes:[],performance:[]};
    let h=`<div class="card" style="border-color:var(--accent);background:var(--accent-soft)"><b>Model Fabric v2 chooses execution mode, then the best eligible configured model.</b><div class="r" style="margin-top:6px">Availability, privacy, role, capability, and context rules run before deterministic scoring. Model choice never changes Trust permissions.</div></div>`;
    h += `<div class="tiles">${[[f.enabled?'enabled':'disabled','Fabric'],[esc((f.routing_policy||'local_first').replaceAll('_',' ')),'privacy policy'],[(f.models||[]).length,'candidate models'],[esc(f.max_fallbacks||0),'max fallbacks']].map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join('')}</div>`;
    h += `<h2>Candidate Models</h2>`;
    h += table(["alias","provider / model","location","availability","capabilities","API cost tier","preference"],(f.models||[]).map(m=>`<tr><td><code>${esc(m.id)}</code></td><td>${esc(m.provider)}<div class="meta">${esc(m.model)}</div></td><td>${m.local?'local':'cloud'}</td><td><span class="pill ${m.availability&&m.availability.available?'pass':'skip'}">${esc((m.availability&&m.availability.reason)||'unknown')}</span></td><td class="meta">${esc(Object.entries(m.capabilities||{}).map(([k,v])=>k+':'+(v===null?'unknown':v)).join(', ')||'unknown')}</td><td>${esc(m.cost_tier||'unknown')}</td><td>${esc(m.preference)}</td></tr>`));
    h += `<h2>Routing Policy</h2><div class="card"><b>${esc((f.routing_policy||'local_first').toUpperCase())}</b><div class="meta">TTL ${esc(f.availability_ttl_seconds)}s &middot; history minimum ${esc(f.min_history_samples)} &middot; max fallbacks ${esc(f.max_fallbacks)}</div><div class="meta">weights: ${esc(JSON.stringify(f.weights||{}))}</div></div>`;
    h += `<h2>Execution modes</h2>`;
    h += table(["mode","role","tokens","iterations","history","tools","memory","verification"],(f.modes||[]).map(p=>`<tr><td><b>${esc((p.mode||'').toUpperCase())}</b></td><td><code>${esc(p.role)}</code></td><td>${esc(p.max_tokens)}</td><td>${esc(p.max_iterations)}</td><td>${esc(p.history_turns)}</td><td>${p.tools_enabled?'on':'off'}</td><td>${p.memory_enabled?'on':'off'}</td><td>${p.verification_enabled?'on':'off'}</td></tr>`));
    h += `<h2>Recent Selections</h2>`;
    h += (f.recent_routes||[]).length ? table(["run","task / mode","candidate","model","score","fallback","latency","result"],f.recent_routes.map(r=>`<tr><td><a class="reveal" onclick="location.hash='replay/${esc(r.run_id)}'"><code>${esc(r.run_id)}</code></a></td><td>${esc(r.task_type||'—')}<div class="meta">${esc((r.execution_mode||'standard').toUpperCase())}</div></td><td><code>${esc(r.candidate_id||'—')}</code></td><td class="meta">${esc(r.provider)} / ${esc(r.model)}</td><td>${r.score==null?'—':esc(r.score)}</td><td>${r.fallback_count?esc(r.fallback_count):'no'}</td><td class="meta">${r.latency_ms==null?'—':esc(r.latency_ms)+'ms'}</td><td><span class="pill ${r.status==='completed'?'pass':'fail'}">${esc(r.status)}</span></td></tr>`)) : `<div class="card empty">no Fabric-enabled selections yet</div>`;
    h += `<h2>Performance</h2>`;
    h += (f.performance||[]).length ? table(["provider / model","mode / task","samples","success","median latency","routing use"],f.performance.map(p=>`<tr><td>${esc(p.provider)}<div class="meta">${esc(p.model)}</div></td><td>${esc((p.execution_mode||'any').toUpperCase())}<div class="meta">${esc(p.task_type||'any')}</div></td><td>${esc(p.sample_count)}</td><td>${p.success_rate==null?'—':esc(Math.round(p.success_rate*100))+'%'}</td><td>${p.median_latency_ms==null?'—':esc(p.median_latency_ms)+'ms'}</td><td><span class="pill ${p.sufficient_samples?'pass':'skip'}">${p.sufficient_samples?'active':'insufficient samples'}</span></td></tr>`)) : `<div class="card empty">no Replay-backed model observations yet</div>`;
    return h;
  },
  forge(d, sub){
    const forge = d.forge || {eligible_runs:[],drafts:[]};
    if (sub){
      if (!FORGE_DETAIL || FORGE_DETAIL.draft_id !== sub){
        loadForgeDetail(sub);
        return `<div class="card empty">loading Forge draft <code>${esc(sub)}</code>...</div>`;
      }
      if (FORGE_DETAIL.error){
        return `<div class="card"><span class="pill fail">unavailable</span> ${esc(FORGE_DETAIL.error)}</div>`;
      }
      const x=FORGE_DETAIL, m=x.metadata||{}, w=x.workflow||{};
      const v=x.validation||{}, e=x.evaluation||{};
      return `<div style="margin-bottom:12px"><a class="reveal" onclick="location.hash='forge'">&larr; all drafts</a></div>
        <div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
          <div class="tn">${esc(m.name||m.skill_id)} <span class="pill ${m.status==='installed'?'pass':m.status==='evaluation_failed'?'fail':'skip'}">${esc((m.status||'draft').toUpperCase())}</span></div>
          <div class="meta"><code>${esc(x.draft_id)}</code> &middot; version ${esc(m.version||1)} &middot; generator ${esc((m.generation||{}).provider||'local')} / ${esc((m.generation||{}).model||(m.generation||{}).method||'template')}</div></div>
        <h2>Provenance and requirements</h2><div class="card"><b>Source runs</b>: ${(w.source_run_ids||[]).map(id=>`<code>${esc(id)}</code>`).join(' ')}
          <div class="meta" style="margin-top:8px">Inputs: ${esc(JSON.stringify(w.inputs||[]))}</div>
          <div class="meta">Tools: ${esc((w.tools||[]).join(', ')||'none')} &middot; capabilities: ${esc((w.capabilities||[]).join(', ')||'none')}</div>
          <div class="meta">Workflow signature: <code>${esc(w.workflow_signature||'')}</code></div></div>
        <h2>Workflow steps</h2>${(w.steps||[]).map(s=>`<div class="card"><b>${esc(s.step_id)} &middot; ${esc(s.operation)}</b> <span class="pill ${s.status==='completed'?'pass':'skip'}">${esc(s.status)}</span><div class="meta">tool <code>${esc(s.tool)}</code> &middot; evidence ${(s.evidence||[]).length}</div></div>`).join('')}
        <h2>Validation and evaluation</h2><div class="card">
          <div>Validation: <span class="pill ${v.passed?'pass':Object.keys(v).length?'fail':'skip'}">${v.passed?'passed':Object.keys(v).length?'failed':'not run'}</span></div>
          <div style="margin-top:6px">Behavioral consistency: <span class="pill ${e.deterministic_pass?'pass':Object.keys(e).length?'fail':'skip'}">${e.deterministic_pass?'passed':Object.keys(e).length?'failed':'not run'}</span> &middot; judge ${esc((e.judge||{}).status||'not run')}</div>
          ${(m.warnings||[]).map(z=>`<div class="meta" style="margin-top:6px">Warning: ${esc(z)}</div>`).join('')}</div>
        <div style="margin:12px 0;display:flex;gap:8px;flex-wrap:wrap">
          <button onclick="forgeAction('validate','${esc(x.draft_id)}')">Revalidate</button>
          <button onclick="forgeAction('evaluate','${esc(x.draft_id)}')">Evaluate</button>
          <button class="save" onclick="forgeAction('install','${esc(x.draft_id)}',true)">Approve / Install</button>
          <button onclick="forgeAction('reject','${esc(x.draft_id)}')">Reject</button></div>
        <h2>SKILL.md preview</h2><pre>${esc(x.content||'')}</pre>`;
    }
    let h=`<div class="card" style="border-color:var(--accent);background:var(--accent-soft)"><b>You choose the evidence; you own the final skill.</b><div class="r" style="margin-top:6px">Forge deterministically extracts selected successful Replay runs, creates an inactive local draft, and requires validation, evaluation, review, approval, and Trust-authorized installation.</div></div>`;
    h += `<h2>Eligible Replay runs</h2>`;
    h += forge.eligible_runs.length ? `<div class="card">${forge.eligible_runs.map(r=>`<label style="display:block;padding:6px"><input class="forge-run" type="checkbox" value="${esc(r.run_id)}"> <code>${esc(r.run_id)}</code> &middot; ${esc(r.input_preview||r.source)}</label>`).join('')}<button class="save" style="margin-top:8px" onclick="forgeAction('forge','')">Generate Draft</button></div>` : `<div class="card empty">no completed Replay runs are eligible</div>`;
    h += `<h2>Drafts</h2>`;
    h += forge.drafts.length ? table(["draft","skill","status","sources"],forge.drafts.map(x=>`<tr><td><a class="reveal" onclick="location.hash='forge/${esc(x.draft_id)}'"><code>${esc(x.draft_id)}</code></a></td><td>${esc(x.skill_id)}</td><td><span class="pill skip">${esc(x.status)}</span></td><td class="meta">${esc((x.source_run_ids||[]).length)}</td></tr>`)) : `<div class="card empty">no Forge drafts yet</div>`;
    return h;
  },
  trust(d){
    const t = d.trust || {canonical_capabilities:[],risk_baseline:{},capabilities:{},recent_decisions:[]};
    let h = `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
      <b>The model may request an action. Tieru Trust Kernel decides whether it executes.</b>
      <div class="r" style="margin-top:6px">Risk classification and policy evaluation are deterministic,
      run outside the prompt, and fail closed. Approval means allow this exact action once; it never
      creates a permanent rule.</div></div>`;
    h += `<h2>Policy precedence</h2><div class="card"><code>${(t.precedence||[]).map(esc).join(" > ")}</code>
      <div class="meta" style="margin-top:8px">Default: ${esc(String(t.default||"deny"))}</div></div>`;
    h += `<h2>Canonical capabilities</h2>`;
    h += table(["capability","baseline risk","configured behavior / scope"],
      (t.canonical_capabilities||[]).map(c => {
        const rule=(t.capabilities||{})[c];
        return `<tr><td><code>${esc(c)}</code></td><td class="meta">${esc((t.risk_baseline||{})[c]||"")}</td>
          <td class="meta">${rule===undefined?"tool-declared compatible default":esc(JSON.stringify(rule))}</td></tr>`;
      }));
    h += `<h2>Recent safe decisions</h2>`;
    h += (t.recent_decisions||[]).length ? table(["tool","capability","risk","decision","reasons"],
      t.recent_decisions.map(x => `<tr><td><code>${esc(x.tool)}</code></td>
        <td class="meta">${esc((x.capability||[]).join(", "))}</td><td>${esc(x.risk)}</td>
        <td><span class="pill ${x.allowed?"pass":"skip"}">${x.allowed?"allowed":"denied"}</span></td>
        <td class="meta">${esc((x.reason_codes||[]).join(", "))}</td></tr>`))
      : `<div class="card empty">no trust decisions yet</div>`;
    h += `<div class="meta" style="margin-top:10px">Recent rows contain safe decision metadata only—never action secrets or full arguments. This is operational inspection, not Replay.</div>`;
    return h;
  },
  database(d, sub){
    // The persistence layer itself — one SQLite file, real tables, FTS5 index.
    // "Data" in the nav (plainer than "state.db"), but we keep saying state.db
    // because that's literally the filename you can open.
    const db = d.db || {tables:[], all_tables:[], fts:[], size:0, path:""};
    const tables = db.tables || [];
    sub = sub || "overview";
    const tabs = [["overview","Overview"],
      ...tables.map(t => [t.name, t.name, t.count]),
      ["query","SQL console"]];
    let h = subtabBar("database", tabs, sub);
    if (sub === "query") return h + dbQueryView();
    if (sub !== "overview"){
      const t = tables.find(x => x.name === sub);
      if (!t) return h + `<div class="card empty">no such table</div>`;
      const notionNote = (t.name === "episodes" && d.episodes_source === "notion")
        ? `<div class="meta" style="margin-bottom:10px">Episodes currently live in Notion — see
            <a class="reveal" onclick="location.hash='memory/episodic'">Memory ▸ Episodic</a>.
            The rows below are the old local copy in state.db.</div>` : "";
      return h + notionNote + `<div class="meta" style="margin-bottom:10px">${DB_DESC[t.name]||""}</div>` + dbTable(t);
    }
    const kb = (db.size/1024).toFixed(1);
    h += `<div class="card" style="border-color:var(--accent);background:var(--accent-soft)">
      <b>Database vs Memory.</b> <span class="r">This is the raw persistence layer — the literal SQLite
      tables. The <a class="reveal" onclick="location.hash='memory'">Memory tab</a> is the friendly
      view of the same rows (facts, episodes, skills, persona). One file, two altitudes. Tieru uses
      these queryable tables and mirrors them to a
      readable <code>MEMORY.md</code> too.</span></div>`;
    h += `<div class="card">
      <div class="u" style="font-family:var(--mono);font-size:12.5px;word-break:break-all">${esc(db.path)}</div>
      <div class="meta">${kb} KB on disk · SQLite + FTS5 · open it yourself: <code>sqlite3 .tieru/state.db</code></div>
      <div class="meta" style="margin-top:8px">${reveal("state.db","reveal state.db in Finder")} &nbsp;·&nbsp; ${reveal("","open the .tieru folder")}</div></div>`;
    h += `<h2>Tables — click a tab above, or a row here</h2>`;
    h += table(["table","rows","what it holds"], tables.map(t =>
      `<tr><td><a class="reveal" onclick="location.hash='database/${esc(t.name)}'"><code>${esc(t.name)}</code></a></td>
        <td class="meta">${t.count}</td><td class="meta">${DB_DESC[t.name]||""}</td></tr>`));
    h += `<h2>FTS5 — the keyword index</h2><div class="card">The <code>*_fts</code> virtual tables (and their
      <code>*_fts_data</code>/<code>*_fts_idx</code> shadows) make memory searchable by keyword — no embeddings,
      no vector DB. This is the "keyword top-k" the retrieval gate queries.
      <div class="meta" style="margin-top:8px">all ${db.all_tables.length} tables: ${db.all_tables.map(t=>`<code>${esc(t)}</code>`).join(" ")}</div></div>`;
    return h;
  },
  ops(d){
    const s = d.stats;
    const u = d.usage || {calls:0,total_in:0,total_out:0,total_cost:0,by_day:[],by_provider:[]};
    let h = `<div class="tiles">${[
        [money(u.total_cost),"spent · all-time","money"],[u.total_in.toLocaleString(),"tokens in · all-time",""],
        [u.total_out.toLocaleString(),"tokens out · all-time",""],[u.calls.toLocaleString(),"LLM calls",""],
        [secs(s.latency_avg),"avg turn",""],[`${s.tool_errors}`,"tool errors",""],
      ].map(([v,l,c])=>`<div class="tile"><b class="${c}">${v}</b><span>${l}</span></div>`).join("")}</div>`;

    h += `<h2>Spend <span class="meta" style="font-weight:400">· permanent ledger — survives a demo reset</span></h2>`;
    h += `<div class="card"><span class="r">Every LLM call's tokens are logged to
      <code>.tieru/usage.jsonl</code> (append-only, never wiped). Dollar cost is estimated from tokens
      × current pricing — the tokens are the ground truth. ${reveal("usage.jsonl","open usage.jsonl")}</span></div>`;
    if ((u.by_provider||[]).length){
      h += table(["provider","LLM calls","tokens in","tokens out","cost (est)"], u.by_provider.map(p =>
        `<tr><td><code>${esc(p.provider)}</code></td><td class="meta">${p.calls}</td>
          <td class="meta">${p.in.toLocaleString()}</td><td class="meta">${p.out.toLocaleString()}</td>
          <td class="meta">${money(p.cost)}</td></tr>`));
    }
    if ((u.by_day||[]).length){
      h += `<h2>Spend per day</h2>`;
      h += table(["day","LLM calls","tokens in","tokens out","cost (est)"], u.by_day.map(r =>
        `<tr><td class="meta">${esc(r.date)}</td><td class="meta">${r.calls}</td>
          <td class="meta">${r.in.toLocaleString()}</td><td class="meta">${r.out.toLocaleString()}</td>
          <td class="meta">${money(r.cost)}</td></tr>`));
    }

    h += `<h2>Retrieval gate — which turns used memory</h2>${gateSplit(s)}`;
    const decided = d.turns.filter(t => t.gate);
    if (decided.length){
      h += `<div class="meta" style="margin:8px 0">The actual decisions (what was skipped vs retrieved), most recent first:</div>`;
      h += table(["turn","decision","why"], decided.slice(0,10).map(t =>
        `<tr><td>${esc((t.user_message||"").slice(0,44))}</td>
          <td><span class="pill ${t.gate.decision==="skip"?"skip":"pass"}">${esc(t.gate.decision)}</span></td>
          <td class="meta">${esc(t.gate.reason||"")}</td></tr>`));
    }

    h += `<h2>Release gate <span class="meta" style="font-weight:400">· the ship/no-ship check</span></h2>`;
    h += `<div class="card"><span class="r">Before you ship a change (new prompt, swapped model, tuned
      retrieval), <code>make gate</code> runs both eval suites: deterministic must pass 100%, the judge must
      clear its threshold. It's manual — you run it — so there's one record per run. The history below grows
      each time you run it.</span></div>`;
    h += d.eval_report ? `<div class="card">
        <span class="pill ${d.eval_report.deterministic}">deterministic · ${d.eval_report.deterministic}</span>
        <span class="pill ${d.eval_report.judge==="pass"?"pass":d.eval_report.judge==="fail"?"fail":"skip"}" style="margin-left:8px">llm-judge · ${d.eval_report.judge}</span>
        <div class="meta">last run ${esc(d.eval_report.ran_at)} — re-run with <code>make gate</code></div></div>`
      : `<div class="card empty">never run yet — run <code>make gate</code> to populate this</div>`;

    if ((d.eval_history||[]).length){
      const cnt = s => s ? `${s.passed||0} pass · ${s.failed||0} fail` : "—";
      h += `<h2>Eval history</h2>`;
      h += table(["when","deterministic","llm-judge","counts"], d.eval_history.map(r =>
        `<tr><td class="meta">${esc((r.ran_at||"").replace("T"," ").slice(0,19))}</td>
         <td><span class="pill ${r.deterministic}">${esc(r.deterministic)}</span></td>
         <td><span class="pill ${r.judge==="pass"?"pass":r.judge==="fail"?"fail":"skip"}">${esc(r.judge)}</span></td>
         <td class="meta">det ${cnt(r.suites&&r.suites.deterministic)} · judge ${cnt(r.suites&&r.suites.judge)}</td></tr>`));
    }

    h += `<h2>Slowest turns</h2>`;
    const slow = [...d.turns].filter(t=>t.latency_ms!=null).sort((a,b)=>b.latency_ms-a.latency_ms).slice(0,6);
    h += table(["turn","latency","cost","tools"], slow.map(t =>
      `<tr><td>${esc((t.user_message||"").slice(0,48))}</td><td class="meta">${secs(t.latency_ms)}</td><td class="meta">${money(t.cost||0)}</td><td class="meta">${(t.tools||[]).map(x=>x.tool).join(", ")||"—"}</td></tr>`));

    h += `<h2>Tracing <span class="meta" style="font-weight:400">· every turn as JSONL, always on</span></h2>`;
    if ((d.trace_errors||[]).length){
      h += d.trace_errors.map(e => `<div class="card"><span class="pill fail">trace encoding error</span>
        <div class="meta" style="margin-top:8px"><code>${esc(e.file)}</code> — ${esc(e.error)}</div></div>`).join("");
    }
    h += `<div class="card"><span class="r">${s.trace_files} trace file(s) in <code>traces/</code>${
      d.trace_file?` (newest: <code>${esc(d.trace_file)}</code>)`:""}. ${reveal("traces","open the traces folder")}.
      A trace is just "what happened, in order" — here are the most recent lines:</span></div>`;
    h += (d.trace_tail||[]).length ? table(["event","detail","when"], d.trace_tail.map(e =>
        `<tr><td><code>${esc(e.type)}</code></td><td class="meta">${esc(String(e.detail).slice(0,60))}</td>
          <td class="meta">${esc((e.ts||"").replace("T"," ").slice(0,19))}</td></tr>`))
      : `<div class="card empty">no trace lines yet — talk to Tieru</div>`;
    h += `<div class="meta" style="margin-top:8px">Span waterfalls: <code>make trace</code> + <code>OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317</code>.</div>`;

    if (d.wake_scans.length){
      h += `<h2>Voice — wake near-misses</h2>`;
      h += table(["heard","when"], d.wake_scans.map(w =>
        `<tr><td>${esc(w.heard)}</td><td class="meta">${esc((w.ts||"").replace("T"," ").slice(0,19))}</td></tr>`));
    }
    return h;
  },
};
