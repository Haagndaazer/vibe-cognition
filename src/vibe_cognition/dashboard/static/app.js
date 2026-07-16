"use strict";

const TYPE_COLORS = {
  decision: "#4f9bff",
  fail: "#ff6b6b",
  discovery: "#5fd17a",
  assumption: "#f4c060",
  constraint: "#b97cff",
  incident: "#ff9444",
  pattern: "#4adcc8",
  episode: "#b6bdc9",
  workflow: "#e87ecb",
  task: "#ffd166",
  person: "#7ee787",
};

const TOKEN = new URL(location.href).searchParams.get("token") || "";
const DONE_CAP = 20;
const STALE_CLAIM_DAYS = 5;

function authHeaders(extra) {
  return { "X-Dashboard-Token": TOKEN, ...(extra || {}) };
}

async function api(path, opts) {
  const init = { ...(opts || {}) };
  init.headers = authHeaders(init.headers);
  const res = await fetch(path, init);
  if (!res.ok) {
    let body = {};
    try { body = await res.json(); } catch (_) {}
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

function toast(message, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " toast-" + kind : "");
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function formatTimestamp(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

function relativeDays(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  return Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
}

function isStaleClaim(ts) {
  const days = relativeDays(ts);
  return days != null && days >= STALE_CLAIM_DAYS;
}

const debounce = (fn, ms) => {
  let h;
  return (...a) => {
    clearTimeout(h);
    h = setTimeout(() => fn(...a), ms);
  };
};

// ── Trust-class provenance chips (design doc §4.7 / decision 6be2e867f91e) ──
// A person chip backed by server-resolved identity (recorded_by / created_by /
// claimed_by) renders solid; a free-text `author` fallback (pre-P13n nodes with
// no server-resolved field) renders dashed + "(unverified)" — the two must never
// look identical, anywhere in the UI (list chips included, not just the drawer).
function personChipHTML(who, opts) {
  if (!who) return "";
  const unverified = !!(opts && opts.unverified);
  const name = typeof who === "object" ? (who.name || who.email || "?") : String(who);
  const cls = "chip person" + (unverified ? " unverified" : "");
  const suffix = unverified ? " (unverified)" : "";
  return `<span class="${cls}">${escapeHTML(name)}${escapeHTML(suffix)}</span>`;
}

// ── Roster join for seniority chips (WP-DashV3, §5 item 1) ─────────────────
// Fetched ONCE lazily -- the first render that needs it kicks off the fetch,
// cached module-level, never refetched per view switch/poll. Deliberately NOT
// the boardTasksCache/activityCache idiom (those refetch every activation) --
// a roster is small and stable within a session, so that idiom would be
// wasted round-trips here.
let _rosterByEmail = null; // null = not yet loaded; Map<lowercased email, person row> once loaded
let _rosterLoading = null;

async function ensureRosterLoaded() {
  if (_rosterByEmail || _rosterLoading) return _rosterLoading;
  _rosterLoading = (async () => {
    try {
      const data = await api("/api/people");
      const map = new Map();
      for (const p of (data.people || [])) {
        if (p.email) map.set(p.email.toLowerCase(), p);
      }
      _rosterByEmail = map;
    } catch (_) {
      _rosterByEmail = new Map(); // degrade silently -- no seniority chips, no error surfaced
    }
  })();
  return _rosterLoading;
}

// Seniority chip: SOLID style (never dashed) -- the mockup's dashed
// .chip.seniority CSS collides with the reserved unverified signal
// (.chip.person.unverified is dashed = untrusted free-text identity); a
// verified identity's seniority chip must never be confused with that. Text
// pin: the seniority word alone in list rows (`full` falsy); the fuller
// "role · seniority" form only in the drawer provenance block and People
// cards (`full` true) -- see identityChipHTML's `full` param.
function seniorityChipHTML(seniority, role, full) {
  const text = full && role ? `${role} · ${seniority}` : seniority;
  return `<span class="chip seniority">${escapeHTML(text)}</span>`;
}

// Trust-boundary lookup: only ever called with a RESOLVED (server-stamped)
// identity, never a free-text author -- an unverified name string must not
// borrow a registered person's authority (matches identityChipHTML's own
// trust-class doctrine, and prime's never-match-free-text precedent).
// Casefolded both sides (`.toLowerCase()` -- the closest vanilla-JS
// approximation of the server's Unicode casefold; the roster's own keys are
// already server-casefolded, so this is belt-and-suspenders on that side and
// load-bearing on the resolved-identity side, whose email is a raw git-config
// value that may be mixed-case).
function seniorityChipForResolved(resolved, full) {
  if (!resolved || typeof resolved !== "object" || !resolved.email) return "";
  ensureRosterLoaded(); // fire-and-forget; lazy, idempotent, no polling
  if (!_rosterByEmail) return ""; // not loaded yet -- no retroactive re-render (WP-DashV3 pin)
  const person = _rosterByEmail.get(resolved.email.toLowerCase());
  return person && person.seniority ? seniorityChipHTML(person.seniority, person.role, full) : "";
}

// Renders the server-resolved identity if present, else the free-text author
// fallback marked unverified. Never silently upgrades an author string to look
// like a verified chip. A resolved identity additionally gains a seniority
// chip when its email matches a registered person on the roster (WP-DashV3);
// pass `full=true` for the drawer's "role · seniority" form (default: the
// seniority word alone, for list rows).
function identityChipHTML(resolved, author, full) {
  if (resolved) return personChipHTML(resolved) + seniorityChipForResolved(resolved, full);
  return author ? personChipHTML(author, { unverified: true }) : "";
}

// from_agent (WP-TC6): a bool key present on the node's metadata, caller-declared
// and unverified. Absent key (pre-TC6 node) renders NOTHING -- never coerced to a
// badge either way.
function fromAgentChipHTML(fromAgent) {
  if (fromAgent === null || fromAgent === undefined) return "";
  return fromAgent
    ? `<span class="chip agent">via agent</span>`
    : `<span class="chip">by human</span>`;
}

function severityChipClass(sev) {
  if (sev === "critical") return "crit";
  if (sev === "high") return "high";
  return "norm";
}

// List-level conflict indicator (WP-DashV3, §4.7): a small ⚠ on Board cards
// and Activity rows when the row's `conflicted` flag is true (see api.py
// _is_conflicted -- bidirectional contradicts + incoming-only supersedes).
// The drawer (conflictBannerHTML) is where names/details live; this is just
// the affordance that something needs a closer look.
function conflictIndicatorHTML(conflicted) {
  return conflicted
    ? ' <span class="chip warn" title="has a conflict — open for details">⚠</span>'
    : "";
}

function wireRowClicks(container) {
  for (const el of container.querySelectorAll("[data-id]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.id));
  }
}

// ── Drawer (shared detail surface: Board cards, Graph nodes, Overview rows) ──
let drawerNodeId = null;

async function openDrawer(id) {
  drawerNodeId = id;
  try {
    const node = await api(`/api/node/${encodeURIComponent(id)}`);
    renderDrawer(node);
    document.getElementById("drawer").classList.add("open");
    if (cy && cy.$id(id).length) {
      highlightNeighborhood(id);
      cy.center(cy.$id(id));
    }
  } catch (err) {
    if (err.status === 404) toast(`Node ${id} no longer exists`, "error");
    else toast(`Load failed: ${err.message}`, "error");
  }
}

function closeDrawer() {
  drawerNodeId = null;
  document.getElementById("drawer").classList.remove("open");
}

function conflictBannerHTML(node) {
  const preds = node.predecessors || [];
  const succs = node.successors || [];
  // WP-DashV3 (peer-review BLOCKING repair): contradicts edges are stored
  // ONE-WAY with ARBITRARY direction (cognition_add_edge: "either direction",
  // no reciprocal edge is ever minted) -- an incoming-only check showed no
  // banner at all for a node on the OUTGOING side of a contradicts edge, even
  // though it IS in conflict. Both sides now render (different wording so the
  // direction stays legible: "contradicted by" for incoming, "contradicts"
  // for outgoing).
  const contradictedBy = preds.find(p => p.edge_type === "contradicts");
  if (contradictedBy) {
    return `<div class="warnbanner">⚠ <b>Conflict:</b> contradicted by
      <span class="node-ref" data-id="${escapeHTML(contradictedBy.id)}">${escapeHTML(contradictedBy.id)}</span></div>`;
  }
  const contradicts = succs.find(s => s.edge_type === "contradicts");
  if (contradicts) {
    return `<div class="warnbanner">⚠ <b>Conflict:</b> contradicts
      <span class="node-ref" data-id="${escapeHTML(contradicts.id)}">${escapeHTML(contradicts.id)}</span></div>`;
  }
  const supersededBy = preds.find(p => p.edge_type === "supersedes");
  if (supersededBy) {
    return `<div class="warnbanner">⚠ <b>Outdated:</b> superseded by
      <span class="node-ref" data-id="${escapeHTML(supersededBy.id)}">${escapeHTML(supersededBy.id)}</span>
      — this is not the current version</div>`;
  }
  return "";
}

function provenanceHTML(node) {
  const meta = node.metadata || {};
  const isTask = node.type === "task";
  const rows = [];
  if (isTask) {
    rows.push(`<div class="row" style="margin-bottom:6px">
      <span class="meta" style="min-width:90px">created by</span>
      ${identityChipHTML(meta.created_by, node.author, true)}
      ${fromAgentChipHTML(meta.from_agent)}
    </div>`);
    rows.push(`<div class="row" style="margin-bottom:6px">
      <span class="meta" style="min-width:90px">claimed by</span>
      ${meta.claimed_by ? personChipHTML(meta.claimed_by) : '<span class="meta">— unclaimed</span>'}
    </div>`);
  } else {
    rows.push(`<div class="row" style="margin-bottom:6px">
      <span class="meta" style="min-width:90px">recorded by</span>
      ${identityChipHTML(meta.recorded_by, node.author, true)}
      ${fromAgentChipHTML(meta.from_agent)}
    </div>`);
  }
  return `<h3>Provenance</h3>${rows.join("")}`;
}

function timelineHTML(node) {
  if (node.type !== "task") return "";
  const transitions = (node.metadata && node.metadata.transitions) || [];
  if (!transitions.length) return "";
  const items = transitions.map(t => {
    const who = t.by && typeof t.by === "object" ? (t.by.name || t.by.email) : t.by;
    const note = t.note ? `<span class="note">${escapeHTML(t.note)}</span>` : "";
    return `<div class="ev"><b>${escapeHTML(t.status)}</b>
      <span class="meta">· ${escapeHTML(formatTimestamp(t.at))}${who ? " · by " + escapeHTML(who) : ""}</span>${note}</div>`;
  }).join("");
  return `<div class="detail-section"><h3>Transition timeline</h3><div class="timeline">${items}</div></div>`;
}

const _LOUD_EDGE_TYPES = new Set(["supersedes", "contradicts"]);
const _EDGE_TYPE_ORDER = ["supersedes", "contradicts", "part_of", "led_to", "resolved_by", "relates_to"];

function relatedNodesHTML(node) {
  const all = [
    ...(node.successors || []).map(s => ({ ...s, dir: "→" })),
    ...(node.predecessors || []).map(p => ({ ...p, dir: "←" })),
  ];
  if (!all.length) return '<li class="empty">none</li>';
  const groups = {};
  for (const r of all) (groups[r.edge_type] || (groups[r.edge_type] = [])).push(r);
  const keys = Object.keys(groups).sort((a, b) => {
    const ia = _EDGE_TYPE_ORDER.indexOf(a), ib = _EDGE_TYPE_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return keys.map(k => groups[k].map(r => {
    const cls = "edge-type" + (_LOUD_EDGE_TYPES.has(k) ? " loud" : "");
    return `<li class="node-ref" data-id="${escapeHTML(r.id)}">
      <span class="${cls}">${escapeHTML(k)} ${r.dir}</span>${escapeHTML(r.id)}</li>`;
  }).join("")).join("");
}

// Person-node drilldown (task 5d4e2bd60d17): renders INSIDE the shared drawer
// via type-conditional rendering, not a new surface — node counts by type,
// last-active timestamp, currently claimed tasks, open tasks created.
// Read-only: no register/edit/deregister affordance anywhere here.
function personActivityHTML(node) {
  if (node.type !== "person") return "";
  const act = node.person_activity || {};
  const counts = act.node_counts || {};
  const countChips = Object.keys(counts).length
    ? Object.entries(counts).map(([t, n]) => `<span class="chip type">${escapeHTML(t)} · ${n}</span>`).join(" ")
    : '<span class="meta">no stamped activity</span>';
  const taskListHTML = (tasks) => tasks.length
    ? `<ul>${tasks.map(t => `<li class="node-ref" data-id="${escapeHTML(t.id)}">${escapeHTML(t.summary || t.id)}</li>`).join("")}</ul>`
    : '<div class="meta">none</div>';
  return `<div class="detail-section">
    <h3>Activity</h3>
    <div class="row" style="margin-bottom:8px;flex-wrap:wrap">${countChips}</div>
    <div class="meta" style="margin-bottom:8px">last active: ${act.last_active ? escapeHTML(formatTimestamp(act.last_active)) : "never"}</div>
    <div class="meta" style="margin-bottom:4px">currently claimed tasks</div>
    ${taskListHTML(act.claimed_tasks || [])}
    <div class="meta" style="margin:8px 0 4px">open tasks created</div>
    ${taskListHTML(act.created_tasks || [])}
  </div>`;
}

function renderDrawer(node) {
  const type = node.type || "";
  const severity = node.severity;
  const html = `
    <div class="detail-header">
      <span>
        <span class="chip type">${escapeHTML(type)}</span>
        ${severity ? `<span class="chip ${severityChipClass(severity)}">${escapeHTML(severity)}</span>` : ""}
      </span>
      <span class="detail-id">${escapeHTML(node.id)}</span>
    </div>
    ${conflictBannerHTML(node)}
    <div class="detail-summary">${escapeHTML(node.summary || "(no summary)")}</div>
    <pre class="detail-body">${escapeHTML(node.detail || "(no detail)")}</pre>
    ${provenanceHTML(node)}
    ${personActivityHTML(node)}
    ${timelineHTML(node)}
    <div class="detail-section">
      <h3>Related nodes</h3>
      <ul>${relatedNodesHTML(node)}</ul>
    </div>
    <button class="detail-delete" id="drawer-delete">Delete node</button>
  `;
  const content = document.getElementById("drawer-content");
  content.innerHTML = html;
  for (const el of content.querySelectorAll(".node-ref[data-id]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.id));
  }
  document.getElementById("drawer-delete").addEventListener("click", () => deleteNode(node.id));
}

async function deleteNode(id) {
  if (!confirm(`Delete node ${id}? This cannot be undone.`)) return;
  try {
    await api(`/api/node/${encodeURIComponent(id)}`, { method: "DELETE" });
    closeDrawer();
    if (cy) cy.remove(cy.$id(id));
    boardTasksCache = boardTasksCache.filter(t => t.id !== id);
    renderBoard();
    toast("Node deleted", "success");
    refreshStats();
    loadOverview();
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "error");
  }
}

// ── Overview ─────────────────────────────────────────────────────────────
async function loadOverview() {
  let data;
  try {
    data = await api("/api/overview");
  } catch (err) {
    toast(`Overview load failed: ${err.message}`, "error");
    return;
  }
  renderOverviewTiles(data);
  renderOverviewConstraints(data.constraints || []);
  renderOverviewAttention(data.needs_attention || {});
  renderOverviewFeed("overview-episodes", data.recent_episodes || []);
  renderOverviewFeed("overview-incidents", data.recent_incidents || []);
}

function renderOverviewTiles(data) {
  const t = data.tasks || {};
  const tiles = [
    { n: t.open || 0, l: "Open tasks" },
    { n: t.in_progress || 0, l: "In progress" },
    { n: t.blocked || 0, l: "Blocked" },
    { n: t.done_this_week || 0, l: "Done this week" },
    { n: data.documents || 0, l: "Documents" },
    { n: data.workflows || 0, l: "Workflows" },
  ];
  document.getElementById("overview-tiles").innerHTML = tiles.map(x =>
    `<div class="tile"><div class="n">${x.n}</div><div class="l">${escapeHTML(x.l)}</div></div>`
  ).join("");
}

function renderOverviewConstraints(list) {
  const el = document.getElementById("overview-constraints");
  if (!list.length) {
    el.innerHTML = '<li class="empty">none active</li>';
    return;
  }
  el.innerHTML = list.map(c => `
    <li class="row clickable" data-id="${escapeHTML(c.id)}">
      <span class="chip ${severityChipClass(c.severity)}">${escapeHTML(c.severity || "normal")}</span>
      <span class="grow">${escapeHTML(c.summary || c.id)}</span>
      ${identityChipHTML(c.recorded_by, c.author)}
    </li>`).join("");
  wireRowClicks(el);
}

function renderOverviewAttention(na) {
  const el = document.getElementById("overview-attention");
  const rows = [];
  for (const s of (na.stale_claims || [])) {
    const days = relativeDays(s.claimed_at);
    rows.push(`<li class="row clickable" data-id="${escapeHTML(s.id)}">
      <span class="st in_progress">in progress</span>
      <span class="grow">${escapeHTML(s.summary || s.id)}</span>
      <span class="meta">claimed ${days != null ? days + "d" : "?"} ago</span>
      ${s.claimed_by ? personChipHTML(s.claimed_by) : ""}
    </li>`);
  }
  for (const b of (na.blocked || [])) {
    rows.push(`<li class="row clickable" data-id="${escapeHTML(b.id)}">
      <span class="st blocked">blocked</span>
      <span class="grow">${escapeHTML(b.summary || b.id)}</span>
    </li>`);
  }
  el.innerHTML = rows.length ? rows.join("") : '<li class="empty">nothing needs attention</li>';
  wireRowClicks(el);
}

function renderOverviewFeed(elId, list) {
  const el = document.getElementById(elId);
  if (!list.length) {
    el.innerHTML = '<li class="empty">none</li>';
    return;
  }
  el.innerHTML = list.map(n => `
    <li class="row clickable" data-id="${escapeHTML(n.id)}">
      <span class="grow">${escapeHTML(n.summary || n.id)}</span>
      ${identityChipHTML(n.recorded_by, n.author)}
      ${fromAgentChipHTML(n.from_agent)}
      <span class="meta">${escapeHTML(formatTimestamp(n.timestamp))}</span>
    </li>`).join("");
  wireRowClicks(el);
}

// ── Board ────────────────────────────────────────────────────────────────
// Column (kanban) view ONLY (Colton's ruling, task e984f2c7c65a) — the former
// Tree-view toggle rendered the graph constellation instead of a task tree
// (root cause: the flat tree's list rows carried no card chrome at all, so
// on a screen with many rows it read as an undifferentiated dot-per-row list
// indistinguishable at a glance from the Graph tab's node list; not a wiring
// bug, just a view nobody wants) — removed rather than fixed.
let boardTasksCache = [];

async function loadBoard() {
  let data;
  try {
    data = await api("/api/tasks");
  } catch (err) {
    toast(`Board load failed: ${err.message}`, "error");
    return;
  }
  boardTasksCache = data.tasks || [];
  renderBoard();
}

function renderBoard() {
  const showCancelled = document.getElementById("board-show-cancelled").checked;
  renderBoardColumns(boardTasksCache, showCancelled);
}

// Top-level ancestor walk (mirrors the server's _task_row depth walk, but
// returns the root id itself, cached per render since boardTasksCache is
// static for the duration of one renderBoard() call).
function topAncestorId(taskId, byId, cache) {
  if (cache.has(taskId)) return cache.get(taskId);
  const seen = new Set();
  let ancestor = taskId;
  let cur = (byId.get(taskId) || {}).parent_id;
  while (cur && byId.has(cur) && !seen.has(cur)) {
    seen.add(cur);
    ancestor = cur;
    cur = byId.get(cur).parent_id;
  }
  cache.set(taskId, ancestor);
  return ancestor;
}

// An id counts as an "epic" only if some OTHER task's ancestor walk resolves
// to it -- i.e. it has at least one real descendant. A childless top-level
// task (parent_id null, nothing points through it) is just an ordinary task
// and falls into the trailing "(no epic)" group, not a header of one.
function findEpicIds(tasks, byId, ancestorCache) {
  const epics = new Set();
  for (const t of tasks) {
    const anc = topAncestorId(t.id, byId, ancestorCache);
    if (anc !== t.id) epics.add(anc);
  }
  return epics;
}

function epicHeaderHTML(epic) {
  return `<div class="epic-header" data-id="${escapeHTML(epic.id)}">
    <span class="chip ${severityChipClass(epic.priority)}">${escapeHTML(epic.priority || "normal")}</span>
    <span class="grow">${escapeHTML(epic.summary || epic.id)}</span>
  </div>`;
}

function taskCardHTML(t) {
  const crumb = t.parent_id ? `<div class="crumb">↳ ${escapeHTML(t.parent_id)}</div>` : "";
  const ghost = (t.status === "done" || t.status === "cancelled") ? " ghost" : "";
  let claim = "";
  if (t.status === "in_progress" && t.claimed_at) {
    const days = relativeDays(t.claimed_at);
    claim = `<span class="meta">claimed ${days != null ? days + "d" : "?"} ago${isStaleClaim(t.claimed_at) ? " ⚠ stale" : ""}</span>`;
  }
  return `<div class="tcard${ghost}" data-id="${escapeHTML(t.id)}">
    ${crumb}
    <div class="sum">${escapeHTML(t.summary || t.id)}${conflictIndicatorHTML(t.conflicted)}</div>
    <div class="row">
      <span class="chip ${severityChipClass(t.priority)}">${escapeHTML(t.priority || "normal")}</span>
      ${identityChipHTML(t.created_by, t.author)}
      ${fromAgentChipHTML(t.from_agent)}
      ${claim}
    </div>
  </div>`;
}

const BOARD_SEVERITY_ORDER = { critical: 0, high: 1, normal: 2, low: 3 };

// Groups `items` (one status column's tasks) by top-level-ancestor epic:
// epic groups first (ordered by epic priority then recency), a trailing
// "(no epic)" group always last (task e984f2c7c65a acceptance: "'(no epic)'
// group present and trailing").
function boardColumnHTML(label, items, byId, epicIds, ancestorCache, total) {
  const count = total != null ? total : items.length;
  const capNote = (total != null && total > items.length)
    ? ` <span class="meta">(showing ${items.length})</span>` : "";

  if (!items.length) {
    return `<div class="col"><h4>${escapeHTML(label)} · ${count}${capNote}</h4>
      <div class="meta" style="padding:8px 4px">none</div></div>`;
  }

  const groups = new Map(); // epicId -> member tasks (non-epic descendants only)
  const noEpic = [];
  for (const t of items) {
    if (epicIds.has(t.id)) continue; // epics render ONLY as headers, never as cards
    const anc = topAncestorId(t.id, byId, ancestorCache);
    if (epicIds.has(anc)) {
      if (!groups.has(anc)) groups.set(anc, []);
      groups.get(anc).push(t);
    } else {
      noEpic.push(t);
    }
  }

  const epicOrder = [...groups.keys()].sort((a, b) => {
    const ea = byId.get(a) || {}, eb = byId.get(b) || {};
    const sa = BOARD_SEVERITY_ORDER[ea.priority || "normal"] ?? 2;
    const sb = BOARD_SEVERITY_ORDER[eb.priority || "normal"] ?? 2;
    if (sa !== sb) return sa - sb;
    return (eb.timestamp || "").localeCompare(ea.timestamp || ""); // recency, newest first
  });

  const sections = epicOrder.map(epicId => {
    const epic = byId.get(epicId) || { id: epicId, summary: epicId };
    return `<div class="epic-group">${epicHeaderHTML(epic)}${groups.get(epicId).map(taskCardHTML).join("")}</div>`;
  });
  sections.push(`<div class="epic-group no-epic">
    <div class="epic-header no-epic-header"><span class="grow">(no epic)</span></div>
    ${noEpic.length ? noEpic.map(taskCardHTML).join("") : '<div class="meta" style="padding:4px">none</div>'}
  </div>`);

  return `<div class="col"><h4>${escapeHTML(label)} · ${count}${capNote}</h4>${sections.join("")}</div>`;
}

function wireCardClicks(container) {
  for (const el of container.querySelectorAll(".tcard[data-id]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.id));
  }
  for (const el of container.querySelectorAll(".epic-header[data-id]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.id));
  }
}

function renderBoardColumns(tasks, showCancelled) {
  const board = document.getElementById("board-columns");

  const byId = new Map(tasks.map(t => [t.id, t]));
  const ancestorCache = new Map();
  const epicIds = findEpicIds(tasks, byId, ancestorCache);

  const cols = { open: [], in_progress: [], blocked: [], done: [], cancelled: [] };
  for (const t of tasks) (cols[t.status] || cols.open).push(t);
  cols.done.sort((a, b) => (b.last_transition_at || "").localeCompare(a.last_transition_at || ""));
  const doneTotal = cols.done.length;
  const doneShown = cols.done.slice(0, DONE_CAP);

  const parts = [
    boardColumnHTML("Open", cols.open, byId, epicIds, ancestorCache),
    boardColumnHTML("In progress", cols.in_progress, byId, epicIds, ancestorCache),
    boardColumnHTML("Blocked", cols.blocked, byId, epicIds, ancestorCache),
    boardColumnHTML("Done", doneShown, byId, epicIds, ancestorCache, doneTotal),
  ];
  if (showCancelled) parts.push(boardColumnHTML("Cancelled", cols.cancelled, byId, epicIds, ancestorCache));

  board.style.gridTemplateColumns = `repeat(${parts.length}, minmax(0, 1fr))`;
  board.innerHTML = parts.join("");
  wireCardClicks(board);
}

// ── Workflows library (WP-DashV2) ───────────────────────────────────────
// Fetches fresh on EVERY activation (not a one-time lazy build like Graph,
// not on the 30s poll either — matches the design doc's "no polling" call
// for the new V2 views while still showing current data when visited).
async function loadWorkflows() {
  let data;
  try {
    data = await api("/api/workflows");
  } catch (err) {
    toast(`Workflows load failed: ${err.message}`, "error");
    return;
  }
  renderWorkflows(data.workflows || []);
}

function workflowChainHTML(chain) {
  // Newest -> oldest per the endpoint; render "v3 ⟵ v2 ⟵ v1" newest-first,
  // each hop re-opens the shared drawer on that specific version's node.
  return chain.map((c, i) => `${i > 0 ? '<span class="meta"> ⟵ </span>' : ""}<span class="chip person node-ref" data-id="${escapeHTML(c.id)}" title="${escapeHTML(c.summary || c.id)}">v${chain.length - i}</span>`).join("");
}

function renderWorkflows(list) {
  const el = document.getElementById("workflows-grid");
  if (!list.length) {
    el.innerHTML = '<div class="meta" style="padding:14px 0">no workflows recorded</div>';
    return;
  }
  el.innerHTML = list.map(w => `
    <div class="card cardgrid-item clickable" data-id="${escapeHTML(w.id)}">
      <div class="sum">${escapeHTML(w.summary || w.id)}</div>
      <div class="row" style="margin:8px 0">
        <span class="meta">by ${escapeHTML(w.author || "unknown")} · ${escapeHTML(formatTimestamp(w.timestamp))}</span>
      </div>
      <div class="chainstrip" data-nostop>${workflowChainHTML(w.chain || [])}</div>
    </div>`).join("");

  // Card click opens the drawer on the HEAD; a chain-hop click (inside the
  // strip) must open its OWN node instead and not also trigger the card's
  // handler — stopPropagation on the hop, not on the whole strip.
  for (const card of el.querySelectorAll(".cardgrid-item[data-id]")) {
    card.addEventListener("click", () => openDrawer(card.dataset.id));
  }
  for (const hop of el.querySelectorAll(".chainstrip .node-ref[data-id]")) {
    hop.addEventListener("click", (evt) => {
      evt.stopPropagation();
      openDrawer(hop.dataset.id);
    });
  }
}

// ── Documents table (WP-DashV2) ─────────────────────────────────────────
const FRESHNESS_LABEL = { unchanged: "unchanged", modified: "modified", missing: "missing" };

async function loadDocuments() {
  let data;
  try {
    data = await api("/api/documents");
  } catch (err) {
    toast(`Documents load failed: ${err.message}`, "error");
    return;
  }
  renderDocuments(data.documents || []);
}

function freshnessBadgeHTML(freshness) {
  if (!freshness) return '<span class="meta">n/a</span>';
  const cls = freshness === "unchanged" ? "st done" : freshness === "modified" ? "st blocked" : "st cancelled";
  return `<span class="${cls}">${escapeHTML(FRESHNESS_LABEL[freshness] || freshness)}</span>`;
}

function humanSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderDocuments(list) {
  const tbody = document.getElementById("documents-tbody");
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="meta" style="text-align:center;padding:14px 0">no documents stored</td></tr>';
    return;
  }
  tbody.innerHTML = list.map(d => {
    // Inert-doc flag is DERIVED client-side, no separate API field.
    const uncited = (d.cited_by || 0) === 0
      ? '<span class="chip" style="margin-left:6px">uncited</span>' : "";
    return `<tr data-id="${escapeHTML(d.node_id)}">
      <td class="clickable-cell" data-nav="${escapeHTML(d.node_id)}">${escapeHTML(d.filename || d.summary || d.node_id)}</td>
      <td>${escapeHTML(d.mode || "")}</td>
      <td>${escapeHTML(humanSize(d.size))}</td>
      <td class="meta">${escapeHTML(formatTimestamp(d.timestamp))}</td>
      <td>${freshnessBadgeHTML(d.freshness)}</td>
      <td>${d.cited_by || 0}${uncited}</td>
      <td><a class="mini-toggle" href="/api/document/${encodeURIComponent(d.node_id)}/download?token=${encodeURIComponent(TOKEN)}" download>Download</a></td>
    </tr>`;
  }).join("");
  for (const el of tbody.querySelectorAll("[data-nav]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.nav));
  }
}

// ── Activity feed (WP-DashV2) ───────────────────────────────────────────
// §8 risk acceptance: fetch-on-activation only, no polling. Filters are
// client-side over the already-fetched window (no round-trip per click).
let activityCache = [];
let activityTypeFilter = null; // null = all types
const ACTIVITY_TYPES = ["episode", "decision", "fail", "discovery", "incident", "constraint", "pattern", "assumption"];

async function loadActivity() {
  let data;
  try {
    data = await api("/api/activity");
  } catch (err) {
    toast(`Activity load failed: ${err.message}`, "error");
    return;
  }
  activityCache = data.activity || [];
  renderActivityTypeFilter();
  renderActivity();
}

function renderActivityTypeFilter() {
  const el = document.getElementById("activity-type-filter");
  const chip = (t, label) => `<span class="chip filterchip${activityTypeFilter === t ? " active" : ""}" data-type="${t || ""}">${escapeHTML(label)}</span>`;
  el.innerHTML = chip(null, "All") + ACTIVITY_TYPES.map(t => chip(t, t)).join("");
  for (const c of el.querySelectorAll("[data-type]")) {
    c.addEventListener("click", () => {
      activityTypeFilter = c.dataset.type || null;
      renderActivityTypeFilter();
      renderActivity();
    });
  }
}

function renderActivity() {
  const el = document.getElementById("activity-list");
  const authorQuery = (document.getElementById("activity-author-filter").value || "").trim().toLowerCase();
  const rows = activityCache.filter(r => {
    if (activityTypeFilter && r.type !== activityTypeFilter) return false;
    if (authorQuery) {
      const author = (r.author || "").toLowerCase();
      const recorded = (r.recorded_by && (r.recorded_by.name || r.recorded_by.email) || "").toLowerCase();
      if (!author.includes(authorQuery) && !recorded.includes(authorQuery)) return false;
    }
    return true;
  });
  if (!rows.length) {
    el.innerHTML = '<li class="empty">no matching activity</li>';
    return;
  }
  el.innerHTML = rows.map(r => `
    <li class="row clickable" data-id="${escapeHTML(r.id)}">
      <span class="chip type">${escapeHTML(r.type || "")}</span>
      ${r.severity ? `<span class="chip ${severityChipClass(r.severity)}">${escapeHTML(r.severity)}</span>` : ""}
      <span class="grow">${escapeHTML(r.summary || r.id)}${conflictIndicatorHTML(r.conflicted)}</span>
      ${identityChipHTML(r.recorded_by, r.author)}
      ${fromAgentChipHTML(r.from_agent)}
      <span class="meta">${escapeHTML(formatTimestamp(r.timestamp))}</span>
    </li>`).join("");
  wireRowClicks(el);
}

function wireActivityFilters() {
  document.getElementById("activity-author-filter").addEventListener("input", debounce(renderActivity, 150));
}

// ── People (WP-DashV3) ───────────────────────────────────────────────────
// Fetches fresh on every activation, same as Workflows/Documents/Activity
// (V2 pattern) -- NOT on the 30s poll (§8 acceptance, unchanged conditions).
// Also seeds the roster cache directly from this response, so a seniority
// chip render right after visiting this view doesn't cost a second round-trip.
async function loadPeople() {
  try {
    const data = await api("/api/people");
    const list = data.people || [];
    const map = new Map();
    for (const p of list) if (p.email) map.set(p.email.toLowerCase(), p);
    _rosterByEmail = map;
    renderPeople(list, map);
  } catch (err) {
    toast(`People load failed: ${err.message}`, "error");
  }
  try {
    const unreg = await api("/api/people/unregistered");
    renderUnregisteredWriters(unreg.unregistered_writers || []);
  } catch (err) {
    toast(`Unregistered writers load failed: ${err.message}`, "error");
  }
}

function renderUnregisteredWriters(list) {
  const el = document.getElementById("people-unregistered");
  if (!list.length) {
    el.innerHTML = '<li class="empty">none — every stamped writer is registered</li>';
    return;
  }
  el.innerHTML = list.map(w => `
    <li class="row">
      <span class="grow">${escapeHTML(w.email)}</span>
      <span class="meta">${escapeHTML((w.names || []).join(", "))}</span>
      <span class="meta">${w.node_count} node${w.node_count === 1 ? "" : "s"}</span>
      <span class="meta">${escapeHTML(formatTimestamp(w.first_seen))} – ${escapeHTML(formatTimestamp(w.last_seen))}</span>
    </li>`).join("");
}

function personCardHTML(p, byEmail) {
  const roleSeniority = p.seniority
    ? `<span class="chip seniority">${escapeHTML(p.role || "")}${p.role ? " · " : ""}${escapeHTML(p.seniority)}</span>`
    : "";
  let reportsTo = '<span class="meta">— top of chain</span>';
  if (p.reports_to_email) {
    const mgr = byEmail.get(p.reports_to_email.toLowerCase());
    reportsTo = mgr
      ? `<span class="chip person node-ref" data-id="${escapeHTML(mgr.id)}">${escapeHTML(mgr.name || mgr.email)}</span>`
      : `<span class="meta">${escapeHTML(p.reports_to_email)}</span> <span class="chip">unregistered</span>`;
  }
  return `<div class="card cardgrid-item clickable" data-id="${escapeHTML(p.id)}">
    <div class="row" style="margin-bottom:6px">
      <span class="chip person">${escapeHTML(p.name || p.email || p.id)}</span>
      ${roleSeniority}
    </div>
    <div class="meta" style="margin-bottom:6px">${escapeHTML(p.email || "")}</div>
    <div class="row" style="margin-bottom:8px">
      <span class="meta" style="min-width:80px">reports to</span>${reportsTo}
    </div>
    <button class="mini-toggle" type="button" data-activity="${escapeHTML(p.name || p.email || "")}">View activity</button>
  </div>`;
}

function renderPeople(list, byEmail) {
  const el = document.getElementById("people-grid");
  if (!list.length) {
    el.innerHTML = '<div class="meta" style="padding:14px 0">no people registered — register with cognition_register_person</div>';
    return;
  }
  el.innerHTML = list.map(p => personCardHTML(p, byEmail)).join("");

  for (const card of el.querySelectorAll(".cardgrid-item[data-id]")) {
    card.addEventListener("click", () => openDrawer(card.dataset.id));
  }
  for (const hop of el.querySelectorAll(".node-ref[data-id]")) {
    hop.addEventListener("click", (evt) => {
      evt.stopPropagation();
      openDrawer(hop.dataset.id);
    });
  }
  for (const btn of el.querySelectorAll("[data-activity]")) {
    btn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      viewPersonActivity(btn.dataset.activity);
    });
  }
}

// Switches to Activity and pre-fills its (existing, client-side) author text
// filter with the person's name -- no new API, the filter already matches
// author OR recorded_by name/email substrings (renderActivity).
function viewPersonActivity(name) {
  document.getElementById("activity-author-filter").value = name;
  activateView("activity");
}

// ── Graph (constellation, lazy — D-3 / WP-TC11) ─────────────────────────
// The tab's BUILD (cytoscape() construction) happens exactly once, on first
// activation. The 30s poll below refreshes data (stats/overview always;
// graph elements only if the tab has already been built) via cy.json(), never
// by constructing a new instance -- the pre-redesign bug this replaces rebuilt
// a fresh cytoscape() every tick regardless of which view was showing.
let cy = null;
let graphLoaded = false;
let currentNodeId = null;

function buildCy(elements) {
  cy = cytoscape({
    container: document.getElementById("cy"),
    elements,
    minZoom: 0.1,
    maxZoom: 2.5,
    wheelSensitivity: 0.2,
    style: [
      {
        selector: "node",
        style: {
          "background-color": (e) => TYPE_COLORS[e.data("type")] || "#7a8290",
          "label": "data(label)",
          "color": "#e6ebf2",
          "font-size": 12,
          "text-opacity": 0,
          "text-wrap": "wrap",
          "text-max-width": "140px",
          "text-valign": "bottom",
          "text-margin-y": 4,
          "border-color": "#0c0e13",
          "border-width": 1,
          "width": 22,
          "height": 22,
        },
      },
      {
        selector: "node.label-on, node:selected, node.search-hit",
        style: { "text-opacity": 1, "font-size": 13, "z-index": 10 },
      },
      {
        selector: "node:selected",
        style: { "border-color": "#fff", "border-width": 2, "width": 28, "height": 28 },
      },
      {
        selector: "node.search-hit",
        style: { "border-color": "#f4c060", "border-width": 3, "z-index": 5 },
      },
      { selector: ".faded", style: { "opacity": 0.15 } },
      {
        selector: "edge",
        style: {
          "width": 1.2,
          "line-color": "#3a4253",
          "target-arrow-color": "#3a4253",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "label": "data(type)",
          "font-size": 9,
          "text-opacity": 0,
          "color": "#6f7787",
          "text-rotation": "autorotate",
          "text-background-color": "#0c0e13",
          "text-background-opacity": 0.8,
          "text-background-padding": 2,
        },
      },
      {
        selector: "edge.highlighted",
        style: { "line-color": "#5cb1ff", "target-arrow-color": "#5cb1ff", "width": 2, "text-opacity": 1 },
      },
    ],
    layout: { name: "fcose", animate: false, randomize: true, nodeRepulsion: 8000, idealEdgeLength: 90 },
  });

  cy.on("tap", "node", (evt) => openDrawer(evt.target.id()));
  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
      currentNodeId = null;
    }
  });
}

function highlightNeighborhood(id) {
  currentNodeId = id;
  const node = cy.$id(id);
  if (node.empty()) return;
  const nh = node.closedNeighborhood();
  cy.elements().addClass("faded").removeClass("highlighted").removeClass("label-on");
  nh.removeClass("faded");
  nh.addClass("label-on");
  nh.edges().addClass("highlighted");
}

async function ensureGraphLoaded() {
  if (graphLoaded) return;
  graphLoaded = true;
  try {
    const graph = await api("/api/graph");
    buildCy([...graph.nodes, ...graph.edges]);
  } catch (err) {
    graphLoaded = false; // allow a retry on next activation
    toast(`Graph load failed: ${err.message}`, "error");
  }
}

async function refreshGraphInPlace() {
  if (!graphLoaded || !cy) return;
  try {
    const graph = await api("/api/graph");
    cy.json({ elements: [...graph.nodes, ...graph.edges] });
    if (currentNodeId && cy.$id(currentNodeId).length) highlightNeighborhood(currentNodeId);
  } catch (_) {
    // non-fatal: the graph tab just shows slightly stale data until the next tick
  }
}

function clearSearchHits() {
  if (!cy) return;
  cy.nodes().removeClass("search-hit");
  cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
  if (currentNodeId && cy.$id(currentNodeId).length) highlightNeighborhood(currentNodeId);
}

function applySearchHits(ids) {
  if (!cy) return;
  cy.nodes().removeClass("search-hit");
  cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
  if (!ids.length) return;
  const hitSet = new Set(ids);
  let visible = cy.collection();
  for (const id of ids) {
    const node = cy.$id(id);
    if (node.length) visible = visible.union(node.closedNeighborhood());
  }
  visible.nodes().forEach(n => { if (hitSet.has(n.id())) n.addClass("search-hit"); });
  cy.elements().not(visible).addClass("faded");
}

// ── Nav ──────────────────────────────────────────────────────────────────
function activateView(view) {
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "graph") ensureGraphLoaded();
  // WP-DashV2: unlike Graph's one-time lazy build, these three fetch fresh on
  // EVERY activation (cheap JSON reads) but are deliberately NOT added to the
  // 30s pollTick loop (§8 risk acceptance — no polling for the new views).
  else if (view === "workflows") loadWorkflows();
  else if (view === "documents") loadDocuments();
  else if (view === "activity") loadActivity();
  // WP-DashV3: same fetch-on-activation, no-poll pattern as the V2 views above.
  else if (view === "people") loadPeople();
}

function wireNav() {
  document.querySelectorAll(".nav-btn[data-view]").forEach(b =>
    b.addEventListener("click", () => activateView(b.dataset.view))
  );
}

// ── Search (unchanged behavior, opens the shared drawer instead of an inline pane) ──
let lastSearchSeq = 0;
async function runSearch(query) {
  const resultsEl = document.getElementById("search-results");
  if (!query) {
    resultsEl.hidden = true;
    resultsEl.innerHTML = "";
    clearSearchHits();
    return;
  }
  const seq = ++lastSearchSeq;
  try {
    const data = await api("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, limit: 25 }),
    });
    if (seq !== lastSearchSeq) return;
    const results = data.results || [];
    renderSearchResults(results);
    applySearchHits(results.map(r => r._id));
  } catch (err) {
    if (err.status === 503) toast("Embedding model not ready yet", "error");
    else toast(`Search failed: ${err.message}`, "error");
  }
}

function renderSearchResults(results) {
  const resultsEl = document.getElementById("search-results");
  if (!results.length) {
    resultsEl.innerHTML = '<div class="search-result"><div class="res-summary">No results</div></div>';
    resultsEl.hidden = false;
    return;
  }
  resultsEl.innerHTML = results.map(r => {
    const id = r._id;
    const sum = r.summary || id;
    const type = r.entity_type || "";
    const score = (r.score != null) ? r.score.toFixed(3) : "";
    return `<div class="search-result" data-id="${escapeHTML(id)}">
      <div class="res-summary">${escapeHTML(sum)}</div>
      <div class="res-meta">${escapeHTML(type)} · ${escapeHTML(id)} · score ${score}</div>
    </div>`;
  }).join("");
  resultsEl.hidden = false;
  for (const el of resultsEl.querySelectorAll(".search-result[data-id]")) {
    el.addEventListener("click", () => {
      resultsEl.hidden = true;
      openDrawer(el.dataset.id);
    });
  }
}

function wireSearch() {
  const search = document.getElementById("search");
  search.addEventListener("input", debounce(e => runSearch(e.target.value.trim()), 220));
  search.addEventListener("focus", () => {
    const r = document.getElementById("search-results");
    if (r.innerHTML) r.hidden = false;
  });
  document.addEventListener("click", e => {
    const wrap = document.getElementById("search-wrap");
    if (!wrap.contains(e.target)) document.getElementById("search-results").hidden = true;
  });
}

// ── Embedding status banner (D-3: persistent, visible --no-embeddings state) ──
function setSearchEnabled(enabled, message, kind) {
  const search = document.getElementById("search");
  const banner = document.getElementById("banner");
  search.disabled = !enabled;
  if (enabled) {
    search.placeholder = "Search nodes by meaning…";
    banner.style.display = "none";
    banner.className = "";
  } else {
    search.placeholder = message || "Loading embedding model…";
    banner.textContent = message || "Loading embedding model…";
    banner.className = `banner-${kind || "loading"}`;
    banner.style.display = "inline-block";
  }
}

function applyEmbeddingBannerState(stats) {
  if (!stats) return false;
  if (stats.embedding_ready) {
    setSearchEnabled(true);
    return true;
  }
  if (stats.embedding_error) {
    setSearchEnabled(false, `Embedding error: ${stats.embedding_error}`, "error");
    return true;
  }
  if (stats.embedding_status === "disabled") {
    // D-3 remainder: this state is terminal for the session (server started with
    // --no-embeddings) -- stays visible, does not get polled away like "loading" does.
    setSearchEnabled(false, "Search disabled — server started with --no-embeddings", "disabled");
    return true;
  }
  return false;
}

async function pollEmbeddingReady() {
  const delays = [2000, 3000, 5000, 8000, 10000];
  let i = 0;
  while (true) {
    const data = await refreshStats();
    if (applyEmbeddingBannerState(data)) {
      if (data.embedding_ready) toast("Search ready", "success");
      return;
    }
    await new Promise(r => setTimeout(r, delays[Math.min(i, delays.length - 1)]));
    i++;
  }
}

async function refreshStats() {
  try {
    const data = await api("/api/stats");
    const g = data.graph || {};
    document.getElementById("stats").textContent =
      `${g.nodes || 0} nodes · ${g.edges || 0} edges · ${data.embeddings || 0} embeddings`;
    return data;
  } catch (err) {
    return null;
  }
}

// ── Auto-poll (D-3): stats + overview refresh every tick regardless of the
// active view; board refreshes too (cheap); graph refreshes IN PLACE only if
// it was already built -- never constructs a new cytoscape() instance here. ──
let _pollInFlight = false;
async function pollTick() {
  if (_pollInFlight) return;
  _pollInFlight = true;
  try {
    const stats = await refreshStats();
    applyEmbeddingBannerState(stats);
    await loadOverview();
    await loadBoard();
    await refreshGraphInPlace();
  } finally {
    _pollInFlight = false;
  }
}

async function init() {
  wireNav();
  wireSearch();
  wireActivityFilters();
  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
  document.getElementById("board-show-cancelled").addEventListener("change", renderBoard);
  document.getElementById("refresh-btn").addEventListener("click", () => {
    refreshStats().then(applyEmbeddingBannerState);
    loadOverview();
    loadBoard();
    refreshGraphInPlace();
  });

  try {
    const stats = await refreshStats();
    if (!applyEmbeddingBannerState(stats)) pollEmbeddingReady();
  } catch (err) {
    toast(`Init failed: ${err.message}`, "error");
  }

  try {
    await loadOverview();
  } catch (err) {
    toast(`Overview load failed: ${err.message}`, "error");
  }
  try {
    await loadBoard();
  } catch (err) {
    toast(`Board load failed: ${err.message}`, "error");
  }

  setInterval(pollTick, 30000);
}

init();
