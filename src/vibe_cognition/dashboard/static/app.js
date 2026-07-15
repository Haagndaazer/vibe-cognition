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

// Renders the server-resolved identity if present, else the free-text author
// fallback marked unverified. Never silently upgrades an author string to look
// like a verified chip.
function identityChipHTML(resolved, author) {
  return resolved ? personChipHTML(resolved) : (author ? personChipHTML(author, { unverified: true }) : "");
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
  const contradicts = preds.find(p => p.edge_type === "contradicts");
  if (contradicts) {
    return `<div class="warnbanner">⚠ <b>Conflict:</b> contradicted by
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
      ${identityChipHTML(meta.created_by, node.author)}
      ${fromAgentChipHTML(meta.from_agent)}
    </div>`);
    rows.push(`<div class="row" style="margin-bottom:6px">
      <span class="meta" style="min-width:90px">claimed by</span>
      ${meta.claimed_by ? personChipHTML(meta.claimed_by) : '<span class="meta">— unclaimed</span>'}
    </div>`);
  } else {
    rows.push(`<div class="row" style="margin-bottom:6px">
      <span class="meta" style="min-width:90px">recorded by</span>
      ${identityChipHTML(meta.recorded_by, node.author)}
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
let boardTasksCache = [];
let boardMode = "columns"; // "columns" | "tree"

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
  if (boardMode === "tree") renderBoardTree(boardTasksCache, showCancelled);
  else renderBoardColumns(boardTasksCache, showCancelled);
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
    <div class="sum">${escapeHTML(t.summary || t.id)}</div>
    <div class="row">
      <span class="chip ${severityChipClass(t.priority)}">${escapeHTML(t.priority || "normal")}</span>
      ${identityChipHTML(t.created_by, t.author)}
      ${fromAgentChipHTML(t.from_agent)}
      ${claim}
    </div>
  </div>`;
}

function boardColumnHTML(label, items, total) {
  const count = total != null ? total : items.length;
  const capNote = (total != null && total > items.length)
    ? ` <span class="meta">(showing ${items.length})</span>` : "";
  const cards = items.length ? items.map(taskCardHTML).join("")
    : '<div class="meta" style="padding:8px 4px">none</div>';
  return `<div class="col"><h4>${escapeHTML(label)} · ${count}${capNote}</h4>${cards}</div>`;
}

function wireCardClicks(container) {
  for (const el of container.querySelectorAll(".tcard[data-id]")) {
    el.addEventListener("click", () => openDrawer(el.dataset.id));
  }
}

function renderBoardColumns(tasks, showCancelled) {
  document.getElementById("board-tree").hidden = true;
  const board = document.getElementById("board-columns");
  board.hidden = false;

  const cols = { open: [], in_progress: [], blocked: [], done: [], cancelled: [] };
  for (const t of tasks) (cols[t.status] || cols.open).push(t);
  cols.done.sort((a, b) => (b.last_transition_at || "").localeCompare(a.last_transition_at || ""));
  const doneTotal = cols.done.length;
  const doneShown = cols.done.slice(0, DONE_CAP);

  const parts = [
    boardColumnHTML("Open", cols.open),
    boardColumnHTML("In progress", cols.in_progress),
    boardColumnHTML("Blocked", cols.blocked),
    boardColumnHTML("Done", doneShown, doneTotal),
  ];
  if (showCancelled) parts.push(boardColumnHTML("Cancelled", cols.cancelled));

  board.style.gridTemplateColumns = `repeat(${parts.length}, minmax(0, 1fr))`;
  board.innerHTML = parts.join("");
  wireCardClicks(board);
}

function renderBoardTree(tasks, showCancelled) {
  document.getElementById("board-columns").hidden = true;
  const tree = document.getElementById("board-tree");
  tree.hidden = false;

  const rows = tasks.filter(t => showCancelled || t.status !== "cancelled");
  rows.sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""));
  tree.innerHTML = rows.length ? rows.map(t => `
    <li data-id="${escapeHTML(t.id)}">
      <span class="crumb-indent">${"—".repeat(t.depth)}</span>
      <span class="st ${escapeHTML(t.status)}">${escapeHTML(t.status)}</span>
      <span class="grow">${escapeHTML(t.summary || t.id)}</span>
      <span class="chip ${severityChipClass(t.priority)}">${escapeHTML(t.priority || "normal")}</span>
    </li>`).join("") : '<li class="empty">no tasks</li>';
  wireRowClicks(tree);
}

function toggleBoardMode() {
  boardMode = boardMode === "columns" ? "tree" : "columns";
  document.getElementById("board-tree-toggle").textContent = boardMode === "columns" ? "Tree view" : "Board view";
  renderBoard();
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
      <span class="grow">${escapeHTML(r.summary || r.id)}</span>
      ${identityChipHTML(r.recorded_by, r.author)}
      ${fromAgentChipHTML(r.from_agent)}
      <span class="meta">${escapeHTML(formatTimestamp(r.timestamp))}</span>
    </li>`).join("");
  wireRowClicks(el);
}

function wireActivityFilters() {
  document.getElementById("activity-author-filter").addEventListener("input", debounce(renderActivity, 150));
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
  document.getElementById("board-tree-toggle").addEventListener("click", toggleBoardMode);
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
