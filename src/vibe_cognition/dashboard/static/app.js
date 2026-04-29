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
};

const TOKEN = new URL(location.href).searchParams.get("token") || "";

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

let cy;
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
        style: {
          "text-opacity": 1,
          "font-size": 13,
          "z-index": 10,
        },
      },
      {
        selector: "node:selected",
        style: { "border-color": "#fff", "border-width": 2, "width": 28, "height": 28 },
      },
      {
        selector: "node.search-hit",
        style: {
          "border-color": "#f4c060",
          "border-width": 3,
          "z-index": 5,
        },
      },
      {
        selector: ".faded",
        style: { "opacity": 0.15 },
      },
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
        style: {
          "line-color": "#5cb1ff",
          "target-arrow-color": "#5cb1ff",
          "width": 2,
          "text-opacity": 1,
        },
      },
    ],
    layout: {
      name: "fcose",
      animate: false,
      randomize: true,
      nodeRepulsion: 8000,
      idealEdgeLength: 90,
    },
  });

  cy.on("tap", "node", async (evt) => {
    const id = evt.target.id();
    await selectNode(id);
  });

  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
      setActiveEpisode(null);
      clearDetail();
    }
  });
}

function highlightNeighborhood(id) {
  const node = cy.$id(id);
  if (node.empty()) return;
  const nh = node.closedNeighborhood();
  cy.elements().addClass("faded").removeClass("highlighted").removeClass("label-on");
  nh.removeClass("faded");
  nh.addClass("label-on");
  nh.edges().addClass("highlighted");
}

async function selectNode(id) {
  currentNodeId = id;
  highlightNeighborhood(id);
  setActiveEpisode(id);
  cy.center(cy.$id(id));
  try {
    const data = await api(`/api/node/${encodeURIComponent(id)}`);
    renderDetail(data);
  } catch (err) {
    if (err.status === 404) {
      toast(`Node ${id} no longer exists`, "error");
      cy.remove(cy.$id(id));
      clearDetail();
    } else {
      toast(`Load failed: ${err.message}`, "error");
    }
  }
}

function clearDetail() {
  currentNodeId = null;
  const aside = document.getElementById("detail");
  aside.innerHTML = '<div class="detail-empty">Click a node to see details</div>';
}

function renderDetail(node) {
  const tpl = document.getElementById("detail-tpl");
  const frag = tpl.content.cloneNode(true);
  const root = frag.querySelector(".detail");
  const type = node.type || "";
  const typeEl = root.querySelector(".detail-type");
  typeEl.textContent = type;
  typeEl.style.color = TYPE_COLORS[type] || "#aaa";
  root.querySelector(".detail-id").textContent = node.id;
  root.querySelector(".detail-summary").textContent = node.summary || "(no summary)";
  root.querySelector(".detail-body").textContent = node.detail || "(no detail)";

  const meta = root.querySelector(".detail-meta");
  const metaItems = [
    ["author", node.author],
    ["timestamp", node.timestamp],
    ["context", (node.context || []).join(", ")],
    ["severity", node.severity],
  ].filter(([, v]) => v);
  meta.innerHTML = metaItems.map(([k, v]) => `<div><strong>${k}:</strong> ${escapeHTML(String(v))}</div>`).join("");

  const refs = root.querySelector(".detail-references");
  refs.innerHTML = (node.references || []).map(r => `<li>${escapeHTML(r)}</li>`).join("") || '<li class="detail-empty">none</li>';

  const succ = root.querySelector(".detail-successors");
  succ.innerHTML = (node.successors || []).map(s =>
    `<li data-id="${escapeHTML(s.id)}"><span class="edge-type">${escapeHTML(s.edge_type)}</span>${escapeHTML(s.id)}</li>`
  ).join("") || '<li class="detail-empty">none</li>';

  const pred = root.querySelector(".detail-predecessors");
  pred.innerHTML = (node.predecessors || []).map(p =>
    `<li data-id="${escapeHTML(p.id)}"><span class="edge-type">${escapeHTML(p.edge_type)}</span>${escapeHTML(p.id)}</li>`
  ).join("") || '<li class="detail-empty">none</li>';

  for (const li of root.querySelectorAll(".detail-section li[data-id]")) {
    li.addEventListener("click", () => {
      const targetId = li.dataset.id;
      if (cy.$id(targetId).length) selectNode(targetId);
      else toast(`Node ${targetId} not in current graph`, "error");
    });
  }

  root.querySelector(".detail-delete").addEventListener("click", () => deleteNode(node.id));

  const aside = document.getElementById("detail");
  aside.innerHTML = "";
  aside.appendChild(frag);
}

async function deleteNode(id) {
  if (!confirm(`Delete node ${id}? This cannot be undone.`)) return;
  try {
    await api(`/api/node/${encodeURIComponent(id)}`, { method: "DELETE" });
    cy.remove(cy.$id(id));
    removeEpisodeFromList(id);
    clearDetail();
    toast("Node deleted", "success");
    refreshStats();
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "error");
  }
}

function buildEpisodeList(nodes) {
  const episodes = nodes
    .map(n => n.data)
    .filter(d => d.type === "episode")
    .sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""));

  document.getElementById("episode-count").textContent = `(${episodes.length})`;
  const list = document.getElementById("episode-list");
  if (!episodes.length) {
    list.innerHTML = '<li class="episode-empty">No episodes yet</li>';
    return;
  }
  list.innerHTML = episodes.map(d => `
    <li class="episode-item" data-id="${escapeHTML(d.id)}">
      <div class="episode-summary">${escapeHTML((d.summary || d.id).slice(0, 140))}</div>
      <div class="episode-meta">${escapeHTML(formatTimestamp(d.timestamp))}</div>
    </li>
  `).join("");

  for (const li of list.querySelectorAll(".episode-item")) {
    li.addEventListener("click", () => selectNode(li.dataset.id));
  }
}

function setActiveEpisode(id) {
  const list = document.getElementById("episode-list");
  if (!list) return;
  for (const li of list.querySelectorAll(".episode-item")) {
    li.classList.toggle("active", id != null && li.dataset.id === id);
  }
}

function removeEpisodeFromList(id) {
  const li = document.querySelector(`.episode-item[data-id="${CSS.escape(id)}"]`);
  if (li) li.remove();
  const countEl = document.getElementById("episode-count");
  const remaining = document.querySelectorAll(".episode-item").length;
  if (countEl) countEl.textContent = `(${remaining})`;
}

function formatTimestamp(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

function clearSearchHits() {
  if (!cy) return;
  cy.nodes().removeClass("search-hit");
  cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
  // Restore selection's neighborhood highlight if a node is currently selected
  if (currentNodeId && cy.$id(currentNodeId).length) {
    highlightNeighborhood(currentNodeId);
  }
}

function applySearchHits(ids) {
  if (!cy) return;
  // Reset all visual state — search supersedes any prior selection fade
  cy.nodes().removeClass("search-hit");
  cy.elements().removeClass("faded").removeClass("highlighted").removeClass("label-on");
  if (!ids.length) return;

  const hitSet = new Set(ids);
  let visible = cy.collection();
  for (const id of ids) {
    const node = cy.$id(id);
    if (node.length) visible = visible.union(node.closedNeighborhood());
  }
  visible.nodes().forEach(n => {
    if (hitSet.has(n.id())) n.addClass("search-hit");
  });
  cy.elements().not(visible).addClass("faded");
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

const debounce = (fn, ms) => {
  let h;
  return (...a) => {
    clearTimeout(h);
    h = setTimeout(() => fn(...a), ms);
  };
};

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
    if (err.status === 503) {
      toast("Embedding model not ready yet", "error");
    } else {
      toast(`Search failed: ${err.message}`, "error");
    }
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
      const id = el.dataset.id;
      resultsEl.hidden = true;
      if (cy.$id(id).length) selectNode(id);
      else toast(`Node ${id} not in current graph`, "error");
    });
  }
}

function setSearchEnabled(enabled, message) {
  const search = document.getElementById("search");
  const banner = document.getElementById("banner");
  search.disabled = !enabled;
  if (enabled) {
    search.placeholder = "Search nodes by meaning…";
    banner.style.display = "none";
  } else {
    search.placeholder = message || "Loading embedding model…";
    banner.textContent = message || "Loading embedding model…";
    banner.className = "banner-loading";
    banner.style.display = "inline-block";
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

async function pollEmbeddingReady() {
  const delays = [2000, 3000, 5000, 8000, 10000];
  let i = 0;
  while (true) {
    const data = await refreshStats();
    if (data && data.embedding_ready) {
      setSearchEnabled(true);
      toast("Search ready", "success");
      return;
    }
    if (data && data.embedding_error) {
      setSearchEnabled(false, `Embedding error: ${data.embedding_error}`);
      document.getElementById("banner").className = "banner-error";
      return;
    }
    await new Promise(r => setTimeout(r, delays[Math.min(i, delays.length - 1)]));
    i++;
  }
}

async function init() {
  try {
    const stats = await refreshStats();
    if (stats && stats.embedding_ready) {
      setSearchEnabled(true);
    } else if (stats && stats.embedding_error) {
      setSearchEnabled(false, `Embedding error: ${stats.embedding_error}`);
      document.getElementById("banner").className = "banner-error";
    } else {
      pollEmbeddingReady();
    }

    const graph = await api("/api/graph");
    const elements = [...graph.nodes, ...graph.edges];
    buildCy(elements);
    buildEpisodeList(graph.nodes);

    const search = document.getElementById("search");
    search.addEventListener("input", debounce(e => runSearch(e.target.value.trim()), 220));
    search.addEventListener("focus", () => {
      const r = document.getElementById("search-results");
      if (r.innerHTML) r.hidden = false;
    });
    document.addEventListener("click", e => {
      const wrap = document.getElementById("search-wrap");
      if (!wrap.contains(e.target)) {
        document.getElementById("search-results").hidden = true;
      }
    });
  } catch (err) {
    toast(`Init failed: ${err.message}`, "error");
  }
}

init();
