"""FastMCP server for Vibe Cognition — project knowledge graph."""

# FIRST two imports, deliberately, in this order:
#  1. _startup_timing stamps server_module_import_start as a side effect
#     (see that module's docstring) before any of the heavier imports below.
#  2. _venv_guard (WP-B) fail-fasts with a clear message on a broken/missing
#     venv, BEFORE the `from .embeddings import ...` line below crashes with
#     a raw ImportError (see that module's docstring for why this can't live
#     inside main() as originally scoped).
from . import _startup_timing  # noqa: I001 - ORDER IS LOAD-BEARING, see comment above; do not let isort resort this block
from . import _venv_guard  # noqa: F401 - imported for its check_or_exit() side effect

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .cognition import CognitionNodeType, CognitionStorage
from .cognition.documents import find_orphaned_document_artifacts, read_text_sidecar
from .config import Settings, setup_logging
from .embeddings import ChromaDBStorage, EmbeddingGenerator
from .instructions import SERVER_INSTRUCTIONS
from . import lifecycle
from .tools import register_all_tools
from .tools.dispatch import prewarm_dispatch_executor
from .tools.cognition_tools import (
    _embed_document,
    _embed_entity_node,
    _embed_workflow,
    _node_from_dict,
)
from .tools.project_registry import build_registry, compute_model_guard

_startup_timing.stamp("server_module_import_done")

logger = logging.getLogger(__name__)

# ── WP-Wedge (P0): bound the bg-thread heavy-import wedge ──────────────────
# Tunables are plain module constants but every helper below takes them as
# parameters too, so tests can drive fast/parameterized variants (AC1/AC2/AC6)
# without monkeypatching module state.
_IMPORT_PROBE_TIMEOUT = 300.0
_IMPORT_PROBE_RETRY_BACKOFF = 60.0
# WP-Wedge-2 §W2-d: raised from 120s -- observed healthy load max was 119.7s,
# which fired the watchdog on a legitimately healthy load (pid 44288, harmless
# via late recovery but noisy, and needlessly puts the degraded branch in
# play). 300s = 2.5x that observed max. Env-overridable via
# Settings.wedge_watchdog_timeout (WEDGE_WATCHDOG_TIMEOUT); this constant
# remains the direct-call default for tests that construct their own context.
_WATCHDOG_TIMEOUT = 300.0
_WATCHDOG_POLL_INTERVAL = 5.0
_HEARTBEAT_INTERVAL = 3.0
_HEARTBEAT_WARM_COUNT = 4


def _run_subprocess_import_probe(
    cmd: list[str] | None = None,
    timeout: float = _IMPORT_PROBE_TIMEOUT,
    retry_backoff: float = _IMPORT_PROBE_RETRY_BACKOFF,
) -> bool:
    """WP-Wedge §3a: pay the heavy native-import cost (Defender scan, disk I/O,
    OS file-cache warm) in a THROWAWAY SUBPROCESS before doing the same import
    for real in-process. On Windows this import can wedge for minutes inside a
    native DLL load (``create_module``, observed via py-spy on two live-wedged
    servers) -- while wedged in-process, the bg thread holds the Windows loader
    lock (blocking new-thread creation process-wide) and every per-module import
    lock in the chain, freezing tool dispatch server-wide. Paying that cost out
    of process first shrinks the in-process import window from minutes to
    milliseconds (warm OS file cache, warm Defender verdict).

    ``stdin``/``stdout``/``stderr`` are ALL ``DEVNULL`` -- load-bearing, not a
    style choice. This repo was already burned by the PIPE variant: a piped
    subprocess in this same detached-server context blocks forever in the pipe
    drain, where ``timeout=`` cannot fire (see ``cognition/git_identity.py``'s
    WHY NO SUBPROCESS docstring, the v0.12.1 incident). Only the return code
    is ever consumed; never reintroduce a pipe here "to see the error text".

    Returns True if the probe subprocess exited (any exit code) within
    ``timeout`` on the first OR the retried attempt -- i.e. it is now safe to
    attempt the in-process import. False means it wedged twice in a row (the
    full ``timeout + retry_backoff + timeout`` bound elapsed); the caller must
    degrade WITHOUT attempting the in-process import this session.
    """
    if cmd is None:
        cmd = [sys.executable, "-c", "import sentence_transformers"]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    # WP-Wedge-2 §W2-e: stamp_and_flush, not stamp -- this function only ever
    # runs on the bg thread (_load_embeddings_and_sync's only caller), so every
    # stamp here must hit disk immediately or a mid-wedge breadcrumb file
    # under-reports (Incident B's exact defect).
    _startup_timing.stamp_and_flush("import_probe_start")
    for attempt in range(2):
        proc = subprocess.Popen(  # noqa: S603 - fixed interpreter + literal script, no shell
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            proc.wait(timeout=timeout)
            _startup_timing.stamp_and_flush("import_probe_ok")
            return True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            _startup_timing.stamp_and_flush("import_probe_killed")
            if attempt == 0:
                time.sleep(retry_backoff)
    return False


async def _warm_worker_batch(count: int = _HEARTBEAT_WARM_COUNT) -> None:
    """Force-spawn/keep-warm ``count`` AnyIO worker threads concurrently — a
    single submission only keeps AnyIO's LIFO-reused head warm, so a batch is
    required to keep the whole small pool warm (WP-Wedge §3c)."""
    await asyncio.gather(*(anyio.to_thread.run_sync(lambda: None) for _ in range(count)))


async def _worker_heartbeat(
    context: dict[str, Any],
    interval: float = _HEARTBEAT_INTERVAL,
    batch_size: int = _HEARTBEAT_WARM_COUNT,
) -> None:
    """WP-Wedge §3c: keep ``batch_size`` AnyIO worker threads warm for the
    whole embedding-load window. AnyIO idles a worker out after 10s
    (``MAX_IDLE_TIME``); a tick that finds no idle worker would spawn one via
    ``Thread.start()`` ON THE EVENT-LOOP THREAD, which under a held loader
    lock would freeze the loop and disable the §3b watchdog with it.

    Each tick's batch runs as its own task (not awaited inline) so the tick
    cadence isn't stretched by how long ``to_thread.run_sync`` takes to get
    scheduled. The in-flight guard SKIPS a tick outright when the previous
    batch hasn't finished (worker starvation, or a residual in-process wedge)
    rather than stacking a second spawn-triggering batch on top of the first.
    Exits as soon as ``embedding_ready`` sets (either the normal-load or the
    watchdog/probe-degrade path) — it exists only for the load window.
    """
    ready: threading.Event = context["embedding_ready"]
    inflight = False
    pending: asyncio.Task | None = None

    async def _run_batch() -> None:
        nonlocal inflight
        try:
            await _warm_worker_batch(batch_size)
        finally:
            inflight = False

    try:
        while not ready.is_set():
            await asyncio.sleep(interval)
            if ready.is_set():
                break
            if inflight:
                continue  # previous batch still in flight -- skip, don't stack
            inflight = True
            pending = asyncio.create_task(_run_batch())
    finally:
        # Benign-case cleanup ONLY, not a wedge mitigation: anyio's to_thread.run_sync
        # defaults to abandon_on_cancel=False, so cancelling `pending` while a batch is
        # genuinely stuck in Thread.start() under the loader lock does NOT interrupt
        # it -- cancellation just waits for the thread call to finish before
        # propagating. And if the loop itself is frozen by that same wedge, this
        # `finally` never gets scheduled to run at all. This only matters for the
        # ordinary case: the heartbeat's own task is cancelled (e.g. server shutdown)
        # while its last batch is still healthily in flight.
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending


async def _watchdog(
    context: dict[str, Any],
    timeout: float = _WATCHDOG_TIMEOUT,
    poll_interval: float = _WATCHDOG_POLL_INTERVAL,
) -> None:
    """WP-Wedge §3b: bound the mid-session freeze from an in-process import
    wedge the §3a probe didn't fully avoid. Fires (signal, never kill — a
    thread stuck in ``LoadLibrary`` cannot be killed and its locks cannot be
    released) ``timeout`` seconds after ``bg_model_load_start`` if the bg
    thread still hasn't finished. The event loop stays alive even when the bg
    thread is wedged (py-spy confirmed on the incident processes), so this
    task keeps running and can signal degraded-search even though the bg
    thread itself cannot be recovered from here.

    The clock starts at ``bg_model_load_start``, NOT at task-start — a
    legitimately slow (but bounded) §3a probe must never trip this; while
    ``bg_model_load_start_time`` is still unset, re-arm and check again later.

    Atomicity rule (mandatory, see module docs / WP brief §3b): this
    test-and-fire and the bg thread's complete/except paths each run under
    the SAME ``context["_wedge_lock"]``. Without it there is a stranding
    interleaving: bg reads ``watchdog_fired`` as False → this fires in that
    same instant (ready not yet set) → bg proceeds without clearing → a
    healthy, generator-installed session fails every gated call forever on a
    stale ``embedding_error``. The critical section is tiny and bounded, so
    holding the lock briefly on the event loop is acceptable.
    """
    ready: threading.Event = context["embedding_ready"]
    lock: threading.Lock = context["_wedge_lock"]
    while not ready.is_set():
        start = context.get("bg_model_load_start_time")
        if start is None:
            await asyncio.sleep(poll_interval)
            continue
        remaining = timeout - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(remaining, poll_interval))
            continue
        with lock:
            if not ready.is_set():
                context["watchdog_fired"] = True
                context["embedding_error"] = (
                    "embedding load slow/wedged; search temporarily degraded"
                )
                ready.set()
                _startup_timing.stamp("watchdog_fired")
        return


def _missing_deterministic_edge(cognition_storage: CognitionStorage, node_id: str, references: list) -> bool:
    """True if ``node_id`` shares a reference with some OTHER node it has no
    edge to/from yet — the exact condition ``create_deterministic_edges``
    would try to fill.

    WP-5 (7c1899fe59ed): diffs against the reference index rather than
    replicating the six-pair type-matching truth table
    (``_deterministic_edge_for_pair``) — a false positive here just costs one
    harmless, idempotent ``create_deterministic_edges`` call (it independently
    re-checks the real rules and no-ops if no edge is actually warranted for
    that pair), so this only needs to be a cheap, conservative OVER-approximation,
    not a byte-exact replay of the matching logic.
    """
    connected = {t for t, _ in cognition_storage.get_successors(node_id)}
    connected |= {s for s, _ in cognition_storage.get_predecessors(node_id)}
    for ref in references:
        for other_id in cognition_storage.find_nodes_by_ref(ref):
            if other_id != node_id and other_id not in connected:
                return True
    return False


def _create_deterministic_edges_for_edgeless(
    cognition_storage: CognitionStorage,
) -> None:
    """Run deterministic part_of matching for nodes missing an edge their
    references warrant.

    This catches nodes created by paths that bypass cognition_record (and
    thus skip deterministic matching), AND (WP-5, 7c1899fe59ed) nodes that
    already have SOME edge but are still missing a link to a reference-
    sharing peer that appeared later (e.g. a teammate's episode for the same
    commit, merged in after this node already had an unrelated edge) — the
    old has-ANY-edge predicate skipped those permanently, since this sweep
    only runs once per server startup and there's no other repair path.
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return

    total_created = 0
    for node_data in all_nodes:
        node_id = node_data["id"]
        references = node_data.get("references", [])
        # Skip nodes with no references (can't match)
        if not references:
            continue
        if not _missing_deterministic_edge(cognition_storage, node_id, references):
            continue
        total_created += cognition_storage.create_deterministic_edges(node_id)

    if total_created:
        logger.info(
            f"Startup deterministic matching: created {total_created} part_of edge(s)"
        )


def _sync_cognition_embeddings(
    cognition_storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
    generator: EmbeddingGenerator,
) -> dict[str, int]:
    """Sync cognition nodes from JSONL into ChromaDB if missing.

    This handles the case where a teammate pulled new JSONL entries via Git
    but the local ChromaDB doesn't have their embeddings yet.

    WP-4 (3e82d4ebc004): routes through the SAME shared embed paths
    _record_node uses (_embed_entity_node / _embed_workflow / _embed_document)
    instead of a drifted inline copy. The old inline non-doc branch omitted
    task status/owner from the embed text/metadata, and workflow nodes never
    got chunked here at all (no _embed_workflow call) — a workflow embedded
    ONLY by this reconciler (e.g. created in the 2-30s model-load window
    where _record_node defers) stayed permanently un-chunked with no repair
    path (in-place workflow edits are refused; versioning is by supersession).
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return {"nodes": 0, "workflows": 0, "documents": 0}

    # Get existing IDs + metadata (WP-4, 41ced8d1fa63: metadata carries each
    # node's chunk_count for the completeness check below) in one call.
    existing_ids: set[str] = set()
    existing_meta: dict[str, Any] = {}
    try:
        results = embedding_storage._collection.get(
            ids=[n["id"] for n in all_nodes], include=["metadatas"]
        )
        existing_ids = set(results["ids"])
        existing_meta = dict(zip(results["ids"], results["metadatas"] or [], strict=False))
    except Exception:
        pass  # If collection is empty or IDs not found, treat all as missing

    doc_type = CognitionNodeType.DOCUMENT.value
    wf_type = CognitionNodeType.WORKFLOW.value
    cognition_dir = cognition_storage.cognition_dir
    doc_nodes = [n for n in all_nodes if n.get("type") == doc_type]
    wf_nodes = [n for n in all_nodes if n.get("type") == wf_type]

    # WP-4 (41ced8d1fa63): verify the FULL chunk set via each node's stored
    # chunk_count metadata, not just chunk-0 presence — a crash mid-write-loop
    # used to read as "fully synced" the instant chunk-0 landed, permanently
    # hiding chunks 1..N. One batched probe across ALL doc+workflow nodes'
    # expected chunk ids (same cost shape as the old chunk-0-only probe).
    expected_chunk_ids: list[str] = []
    for n in doc_nodes + wf_nodes:
        if n["id"] not in existing_ids:
            continue
        count = int((existing_meta.get(n["id"]) or {}).get("chunk_count") or 0)
        expected_chunk_ids.extend(f"{n['id']}#chunk-{i}" for i in range(count))
    chunk_present: set[str] = set()
    if expected_chunk_ids:
        try:
            probe = embedding_storage._collection.get(ids=expected_chunk_ids)
            chunk_present = set(probe["ids"])
        except Exception:
            pass

    def _chunks_complete(node_id: str) -> bool:
        meta = existing_meta.get(node_id) or {}
        if "chunk_count" not in meta:
            # Legacy vector (written before this WP started stamping
            # chunk_count unconditionally, len(chunks) incl. explicit 0):
            # chunk state is UNKNOWN, not "zero expected" — key-absent must
            # NOT be conflated with an explicit 0 (redirect from Vince's
            # gate review) or a legacy text-bearing doc/workflow with a
            # missing/incomplete chunk set reads as permanently complete.
            # Force one re-embed; it stamps chunk_count and converges.
            return False
        count = int(meta.get("chunk_count") or 0)
        if count == 0:
            return True  # explicitly zero expected (e.g. empty sidecar) -> complete
        return all(f"{node_id}#chunk-{i}" in chunk_present for i in range(count))

    # Plain (non-document, non-workflow) nodes: embed node-level if missing.
    non_doc_missing = [
        n for n in all_nodes
        if n.get("type") not in (doc_type, wf_type) and n["id"] not in existing_ids
    ]

    # Workflows: missing entirely, OR present but with an incomplete chunk set
    # (WP-4, 41ced8d1fa63 — previously any presence at all was "synced").
    wf_to_embed = [
        n for n in wf_nodes
        if n["id"] not in existing_ids or not _chunks_complete(n["id"])
    ]

    # Documents (WP-D2): a document is fully synced iff its NODE vector exists AND
    # (its sidecar is empty/absent OR its FULL chunk set is present). The "empty
    # sidecar" branch is load-bearing — without it a text-less document looks
    # "missing" forever and re-embeds every boot. This replaces D1a's blanket
    # document-skip and backfills documents created in the D1a/D1b interim
    # (deliberately never embedded then).
    docs_to_embed: list[tuple[dict[str, Any], str]] = []
    for n in doc_nodes:
        node_present = n["id"] in existing_ids
        sha = n.get("metadata", {}).get("sha256")
        sidecar = read_text_sidecar(cognition_dir, sha) if sha else None
        has_text = bool(sidecar and sidecar.strip())
        complete = node_present and (not has_text or _chunks_complete(n["id"]))
        if not complete:
            docs_to_embed.append((n, sidecar or ""))

    if non_doc_missing:
        logger.info(f"Syncing {len(non_doc_missing)} cognition nodes to ChromaDB...")
        for n in non_doc_missing:
            _embed_entity_node(embedding_storage, generator, _node_from_dict(n["id"], n))

    if wf_to_embed:
        logger.info(f"Syncing {len(wf_to_embed)} workflow node(s) to ChromaDB...")
        for n in wf_to_embed:
            _embed_workflow(embedding_storage, generator, _node_from_dict(n["id"], n))

    # Documents: node vector + sidecar chunks via the shared _embed_document (the same
    # delete-then-write path store-time uses — no chunk-contract drift). A sidecar-less
    # reference doc (teammate pulled the journal but not the sidecar) embeds the node
    # only (sidecar_text == "" -> zero chunks); not an error.
    for node, sidecar_text in docs_to_embed:
        _embed_document(
            embedding_storage, generator, node["id"],
            node.get("summary", ""), node.get("detail", ""), sidecar_text,
        )

    if non_doc_missing or wf_to_embed or docs_to_embed:
        logger.info(
            f"Cognition embedding sync: {len(non_doc_missing)} nodes + "
            f"{len(wf_to_embed)} workflows + {len(docs_to_embed)} documents (re)embedded"
        )
    else:
        logger.info("Cognition embeddings: all nodes already synced")

    # Always reconcile orphans (independent of the add passes above).
    _reconcile_orphan_embeddings(cognition_storage, embedding_storage)

    # WP-4 item 3 (5340ae677931, code half): coarse progress counts for
    # get_status's "syncing" state — how much this pass actually (re)embedded.
    return {
        "nodes": len(non_doc_missing),
        "workflows": len(wf_to_embed),
        "documents": len(docs_to_embed),
    }


def _reconcile_orphan_embeddings(
    cognition_storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
) -> None:
    """N1 startup reclamation (§9 N1b): delete Chroma ids (incl. ``#chunk-*``) whose
    node is absent from the graph. A node deleted on another machine replays as a
    remove_node tombstone (graph-only) and is NEVER un-embedded (the sync only ADDS),
    so its vector lingers and would surface in search.

    Best-effort RECLAMATION only — the query-time has_node filter in cognition_search
    is the correctness guarantee (it never returns a ghost regardless of this sweep).
    Ordering-hardened (peer review): the graph snapshot is freshly caught-up
    (get_all_nodes -> _synced), orphans are computed against it, enumeration uses the
    no-arg get() (a get/delete with ids=[] RAISES on an empty list), and delete is
    skipped on an empty orphan set. A residual cross-process TOCTOU remains (a write
    landing between catch-up and delete) — accepted under the non-transactional model;
    it can cause only a transiently-late reclamation, never a wrong search result.
    """
    try:
        all_ids = embedding_storage._collection.get()["ids"]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Orphan-embedding sweep: enumerate failed: {e}")
        return
    if not all_ids:
        return
    graph_ids = {n["id"] for n in cognition_storage.get_all_nodes()}
    orphans = [cid for cid in all_ids if cid.split("#chunk-")[0] not in graph_ids]
    if not orphans:
        return
    try:
        embedding_storage._collection.delete(ids=orphans)
        logger.info(f"Orphan-embedding sweep: removed {len(orphans)} stale vector(s)")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Orphan-embedding sweep: delete failed: {e}")


def _load_embeddings_and_sync(config: Settings, context: dict[str, Any]) -> None:
    """Background thread: load embedding model, then sync embeddings + edges.

    This runs after the MCP handshake completes so the server starts fast.
    Semantic curation is NOT done here — it is the agent's job via the
    `/vibe-curate` skill. The only automatic edges are the deterministic
    `part_of` edges created from shared references.

    WP-A 1b (decision 9022f7de94e9): this thread is where startup breadcrumbs
    get PERSISTED to disk. It starts concurrently with (not blocking) the
    handshake, so the flush here never touches the synchronous pre-yield path
    the HEISENBUG GUARD protects. Flushed once immediately (captures every
    breadcrumb up to and including handshake_yield, best-effort on ordering
    against the main thread) and again at the end (captures the model-load +
    sync breadcrumbs too), so the file is useful even if this thread errors
    partway through. Also prunes the per-PID log directory here (once per
    server startup, same off-critical-path constraint) so it never grows
    unbounded across N concurrent agents x many sessions x every project.
    """
    _startup_timing.stamp_and_flush("bg_thread_start")
    _startup_timing.prune_old_logs()

    try:
        # Home model/dim drift guard (WP-2): a cheap metadata comparison (no
        # model load needed), run FIRST so it can't add to embedding_ready
        # latency below (that stays gated on model load only, unchanged —
        # known-intentional) and so get_status/cognition_search see the guard
        # state as early as possible. Reuses the SAME check the foreign-attach
        # path uses (compute_model_guard) — do not invent a second guard.
        # Unlike the foreign path, home's embeddings handle is never closed on
        # a mismatch: it's the actively-written index and new writes must
        # still land regardless of a stale stamp.
        home_chroma = context.get("cognition_embedding_storage")
        if home_chroma is not None:
            guard, guard_warning, _ = compute_model_guard(
                home_chroma, config.embedding_model, config.embedding_dimensions,
                config.effective_repo_name,
            )
            loaded_projects = context.get("loaded_projects")
            if loaded_projects is not None:
                home_entry = loaded_projects.get(config.repo_path)
                if home_entry is not None:
                    home_entry.model_guard = guard
            context["home_model_guard"] = guard
            context["home_model_guard_warning"] = guard_warning
            if guard in ("dim-mismatch", "model-mismatch"):
                logger.warning(f"Home embedding collection drift: {guard_warning}")

        # WP-Wedge §3a: subprocess-warm the heavy native import chain BEFORE doing
        # it for real in-process, so a Windows DLL-load wedge pays its cost in a
        # throwaway subprocess instead of holding this process's loader lock +
        # import locks. Only the sentence-transformers backend touches
        # sentence_transformers/torch/scipy at all — gated on backend so an
        # ollama-configured server (which never constructs
        # SentenceTransformersBackend) never pays the probe's worst-case ~11min
        # bound for an import it doesn't need.
        if config.embedding_backend != "ollama" and not _run_subprocess_import_probe():
            logger.error(
                "Background: embedding import probe wedged twice; degrading "
                "without attempting the in-process import this session"
            )
            context["embedding_error"] = (
                "embedding import wedged twice (probe killed at "
                f"{_IMPORT_PROBE_TIMEOUT:.0f}s); search degraded — reduce "
                "concurrent sessions or reconnect"
            )
            context["embedding_ready"].set()
            context["embedding_sync_done"].set()
            _startup_timing.flush_to_disk()
            return

        # Load embedding model (the bottleneck: 2-30s)
        t_start = _startup_timing.stamp_and_flush("bg_model_load_start")
        context["bg_model_load_start_time"] = t_start
        logger.info(f"Background: loading embedding model ({config.embedding_backend})...")
        embedding_generator = EmbeddingGenerator.from_config(config)
        t_model = _startup_timing.stamp_and_flush("bg_model_loaded")
        logger.info(f"Background: embedding model loaded in {t_model - t_start:.1f}s")

        # WP-Wedge §3b: install the generator, late-recovery clear (only if the
        # watchdog fired), and the ready signal all happen ATOMICALLY under the
        # same lock the watchdog's test-and-fire uses (mandatory — see _watchdog's
        # docstring for the stranding interleaving this closes). Stamp INSIDE the
        # lock (memory-only, doesn't extend the critical section); §W2-e's
        # flush happens just after release, not inside -- disk I/O has no
        # business extending a lock the watchdog also needs.
        late_recovery = False
        wedge_lock: threading.Lock | None = context.get("_wedge_lock")
        with wedge_lock if wedge_lock is not None else contextlib.nullcontext():
            context["embedding_generator"] = embedding_generator
            if context.get("watchdog_fired"):
                context["embedding_error"] = None
                context["watchdog_fired"] = False
                _startup_timing.stamp("bg_late_recovery")
                late_recovery = True
                logger.info("Background: embedding generator installed after watchdog fired (late recovery)")
            context["embedding_ready"].set()
        if late_recovery:
            _startup_timing.flush_to_disk()
        logger.info("All tools now available")

        # Run deterministic part_of matching for edgeless nodes
        # (catches hook-created episodes that bypassed cognition_record)
        cognition_storage = context.get("cognition_storage")
        if cognition_storage:
            _create_deterministic_edges_for_edgeless(cognition_storage)

        # WP-12 (d999b4e3851a): log-only sweep for document sidecar/blob files no
        # node references (a crash between _store_document's artifact writes and
        # its node mint orphans them — see find_orphaned_document_artifacts'
        # docstring for why this is discovery-only, never auto-reclaimed). Cheap
        # (a directory walk), runs once at background-init time; never blocks
        # startup or raises.
        if cognition_storage:
            try:
                orphans = find_orphaned_document_artifacts(
                    cognition_storage.cognition_dir, cognition_storage
                )
                if orphans:
                    logger.warning(
                        f"Found {len(orphans)} orphaned document artifact(s) with no "
                        f"owning node (not auto-deleted, review manually): {orphans[:10]}"
                        + ("..." if len(orphans) > 10 else "")
                    )
            except Exception as e:  # pragma: no cover - defensive, must never block startup
                logger.warning(f"Orphaned-document-artifact sweep failed: {e}")

        # Sync cognition embeddings (backfill any missing from JSONL)
        cognition_embedding_storage = context.get("cognition_embedding_storage")

        if cognition_storage and cognition_embedding_storage and embedding_generator:
            # E-3 one-time migration: if the collection lacks the doc-prefix-v1 marker,
            # drop and recreate it so the sync below rebuilds all vectors document-prefixed.
            # Crash mid-rebuild self-heals: the additive sync re-adds only what's missing.
            # live_embed_scheme() (WP-3, b35e15766c6b), NOT self._collection.metadata:
            # a process-cached handle can go stale if another process already
            # recreated the collection, which used to let two racing startups
            # both decide "needs migration" and double-delete-recreate.
            # recreate_collection() itself is file-locked against that race too.
            if cognition_embedding_storage.live_embed_scheme() != "doc-prefix-v1":
                logger.info("E-3 migration: recreating collection with doc-prefix stamp")
                cognition_embedding_storage.recreate_collection()

            logger.info("Syncing cognition embeddings...")
            context["embedding_sync_progress"] = _sync_cognition_embeddings(
                cognition_storage, cognition_embedding_storage, embedding_generator
            )

        # WP-4 item 3 (5340ae677931, code half): embedding_ready.set() above
        # fires BEFORE this backfill sync — deliberately unchanged (known-
        # intentional) — so a teammate joining an existing graph could see
        # embedding_status "ready" while historical nodes were still
        # un-embedded and search silently incomplete. embedding_sync_done is
        # a SEPARATE signal get_status uses to report "syncing" in that
        # window instead of a falsely-confident "ready"; it never gates tool
        # availability.
        context["embedding_sync_done"].set()
        _startup_timing.stamp_and_flush("bg_sync_done")

    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        # WP-Wedge §3b clobber-guard: under the SAME lock as the watchdog and the
        # success path's late-recovery, so a genuine error can never race a
        # concurrent watchdog fire into an inconsistent tuple. A genuine error
        # here always wins — it is NEVER cleared by anything downstream (the
        # late-recovery clear only lives in the success path, which this
        # exception means we never reached).
        wedge_lock: threading.Lock | None = context.get("_wedge_lock")
        with wedge_lock if wedge_lock is not None else contextlib.nullcontext():
            context["embedding_error"] = str(e)
            context["embedding_ready"].set()  # Signal so tools don't hang forever
        context["embedding_sync_done"].set()  # Sync phase is over either way
        _startup_timing.stamp_and_flush("bg_thread_error")


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage server lifecycle - initialize and cleanup resources."""
    _startup_timing.stamp("lifespan_enter")

    # Load configuration — reads REPO_PATH from env (set by the plugin's
    # mcpServers block in plugin.json; config.py also falls back to
    # CLAUDE_PROJECT_DIR). There is no per-project .mcp.json.
    try:
        config = Settings()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        raise

    setup_logging(config.log_level)
    logger.info(f"Starting Vibe Cognition for repository: {config.effective_repo_name}")
    logger.info(f"Repository path: {config.repo_path}")

    # ── Fast init (blocking, <500ms) ───────────────────────────────

    # Initialize cognition graph
    logger.info(f"Initializing cognition graph at {config.cognition_dir}...")
    try:
        cognition_storage = CognitionStorage(config.cognition_dir)
    except Exception as e:
        # WP-11 (38a5914e6dc6): a corrupted journal or a .cognition/ permission
        # error previously killed the server with a raw traceback Claude Code may
        # never surface, leaving zero cognition tools and no explanation. Log the
        # failing component + path diagnosably, then re-raise (FastMCP still
        # fails the server — that's correct; this is about being able to find
        # WHY, not about staying up in a broken state).
        logger.error(f"Failed to initialize cognition graph at {config.cognition_dir}: {e}")
        raise

    # Initialize cognition ChromaDB
    logger.info(f"Initializing cognition ChromaDB at {config.cognition_chromadb_path}...")
    _startup_timing.stamp("chroma_open_start")
    try:
        cognition_embedding_storage = ChromaDBStorage(
            persist_directory=config.cognition_chromadb_path,
            collection_name="cognition_embeddings",
            embedding_model=config.embedding_model,
            embedding_dimensions=config.embedding_dimensions,
        )
    except Exception as e:
        logger.error(
            f"Failed to initialize cognition ChromaDB at {config.cognition_chromadb_path}: {e}"
        )
        raise
    _startup_timing.stamp("chroma_open_done")

    # WP-Wedge-2 §W2-c (INV-2): pre-EXERCISE chromadb's own count/get code
    # paths pre-yield -- third-party conditional imports (chromadb telemetry/
    # embedding-function first-use paths) can't be pre-enumerated by grep
    # alone the way the four known function-body-import sites were (this is
    # NOT the torch/scipy/sentence_transformers chain -- that stays lazy and
    # gated on backend, unchanged). Same two calls get_status already makes
    # in production (a bare count_documents(), then an is_chunk-filtered
    # count_documents() -- the filtered form calls _collection.get(where=...)
    # under the hood, but both are COUNT calls at the public API this exists
    # to exercise), so this exercises exactly the tool-reachable code path,
    # nothing extra. Chroma
    # init already does pre-yield disk I/O (HEISENBUG GUARD constrains the
    # breadcrumb flush path, not startup work per se), so this doesn't cross
    # a new line. Best-effort + budgeted: a failure or overrun here must
    # never break startup, only log -- the real calls still run (possibly
    # un-pre-warmed) once tools actually dispatch.
    _pre_exercise_start = time.monotonic()
    try:
        cognition_embedding_storage.count_documents()
        cognition_embedding_storage.count_documents(filter={"is_chunk": True})
    except Exception as e:
        logger.warning(f"Chroma pre-exercise failed (non-fatal): {e}")
    _pre_exercise_elapsed = time.monotonic() - _pre_exercise_start
    if _pre_exercise_elapsed > 0.2:
        logger.warning(
            f"Chroma pre-exercise took {_pre_exercise_elapsed:.3f}s (budget 200ms)"
        )

    # Build project registry — home is always pinned
    loaded_projects = build_registry(
        home_path=config.repo_path,
        home_tag=config.effective_repo_name,
        home_storage=cognition_storage,
        home_embeddings=cognition_embedding_storage,
    )

    # Build context for tools
    context: dict[str, Any] = {
        "config": config,
        "cognition_storage": cognition_storage,
        "cognition_embedding_storage": cognition_embedding_storage,
        "loaded_projects": loaded_projects,
        "embedding_generator": None,  # Set by background thread
        "embedding_ready": threading.Event(),
        "embedding_error": None,
        # Optimistic default until the background thread's cheap drift check
        # runs (WP-2); matches add_home's default and is never search-visible
        # earlier than this, since search is gated behind embedding_ready
        # which the background thread only sets AFTER the drift check.
        "home_model_guard": "match",
        "home_model_guard_warning": None,
        # WP-4 item 3 (5340ae677931, code half): set once the historical
        # backfill sync finishes (success or error) — independent of
        # embedding_ready, which fires earlier and is frozen (known-
        # intentional). get_status derives "syncing" from ready-but-not-done.
        "embedding_sync_done": threading.Event(),
        "embedding_sync_progress": None,
        # WP-Wedge: shared lock for the watchdog <-> bg-thread atomicity rule
        # (§3b), the late-recovery flag, and the bg thread's own load-start
        # timestamp (read by the watchdog to arm its clock at bg_model_load_start,
        # not at task-start — a legitimately slow §3a probe must never trip it).
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": None,
    }

    # ── Background init (2-30s for model, then sync + curation) ────

    # WP-Wedge §3c: force-spawn warm AnyIO worker threads WHILE spawning is
    # still safe — before the bg import thread opens its wedge window. Pre-yield
    # but touches no disk, so the HEISENBUG GUARD (no disk I/O on this path) is
    # unaffected. UNCHANGED by WP-Wedge-2 (see §W2-b dispatch.py's module
    # docstring): this keeps the stdio TRANSPORT's anyio pool warm (reader +
    # writer), independent of tool dispatch, which no longer uses this pool
    # at all once the dispatch executor below is warm.
    await _warm_worker_batch(_HEARTBEAT_WARM_COUNT)

    # WP-Wedge-2 §W2-b (INV-1): pre-warm the DEDICATED dispatch executor the
    # SAME way, same reason -- forces all its threads to exist while spawning
    # is still safe, so no tool dispatch can ever need Thread.start() again.
    await prewarm_dispatch_executor()

    # WP-Lifecycle §L-a/§L-c: arm both self-exit watches BEFORE the bg import
    # thread starts -- same reasoning as the warm-ups above, so the watch
    # threads' OS threads exist before any loader-lock wedge can block thread
    # creation. §L-a (ancestor-death) is the primary guarantee (works even
    # mid-wedge, since it doesn't ride the event loop); §L-b (stdin-pipe-
    # closure) is the loop-independent secondary path -- the MCP-conventional
    # stdin-EOF shutdown rides the event loop and is exactly what a frozen
    # loop can never fire.
    lifecycle.arm_ancestor_watch()
    lifecycle.arm_stdin_watch()

    bg_thread = threading.Thread(
        target=_load_embeddings_and_sync,
        args=(config, context),
        daemon=True,
    )
    bg_thread.start()
    context["_bg_thread"] = bg_thread

    # WP-Wedge §3b/§3c: armed for the whole load window, cancelled at shutdown
    # (and each exits itself as soon as embedding_ready sets, whichever path).
    watchdog_task = asyncio.create_task(
        _watchdog(context, timeout=config.wedge_watchdog_timeout)
    )
    heartbeat_task = asyncio.create_task(_worker_heartbeat(context))
    context["_watchdog_task"] = watchdog_task
    context["_heartbeat_task"] = heartbeat_task

    logger.info("Vibe Cognition ready (embedding model loading in background)")
    _startup_timing.stamp("handshake_yield")

    yield context

    # ── Cleanup ───────────────────────────────────────────────────

    logger.info("Shutting down Vibe Cognition...")

    # WP-Wedge cleanup, NOT a wedge mitigation: both tasks self-exit once
    # embedding_ready sets, so this cancel is a no-op in the common (already-
    # finished) case; it only matters for a shutdown that races a STILL-RUNNING
    # load window, e.g. the server exits mid-load. A bg thread genuinely wedged
    # in a native DLL load leaves the event loop ALIVE (see _watchdog's
    # docstring -- py-spy confirmed this on the incident processes), so this
    # cleanup runs fine and cancels the watchdog/heartbeat tasks normally even
    # then. The loop only freezes if something ON the loop attempts
    # Thread.start() during the wedge (a heartbeat tick, or tool dispatch
    # needing a fresh worker) -- in THAT case this shutdown code is what never
    # gets scheduled to run, same as everything else on the loop. Never
    # touches disk either way, so no HEISENBUG GUARD concern.
    for task in (context.get("_watchdog_task"), context.get("_heartbeat_task")):
        if task is not None:
            task.cancel()
    for task in (context.get("_watchdog_task"), context.get("_heartbeat_task")):
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Stop dashboard server if running
    if context.get("dashboard"):
        try:
            # wp2-import-free: sanctioned -- hoisting this to server.py's module
            # top level creates a genuine circular import (dashboard.server ->
            # dashboard.api -> tools.cognition_tools -> tools/__init__.py ->
            # dashboard_tool.py -> dashboard.server, still mid-init). Provably
            # safe as-is: dashboard_tool.py's OWN top-level import already
            # fully loads dashboard.server during server.py's normal module
            # import (via `from .tools import register_all_tools`, well before
            # this shutdown code ever runs) -- this is always a sys.modules
            # cache hit, never a fresh import, regardless of AST appearance.
            from .dashboard.server import (
                stop_dashboard,  # wp2-import-free: sanctioned (see comment above)
            )
            stop_dashboard(context, join_timeout=3.0)
        except Exception as e:
            logger.warning(f"Dashboard shutdown error: {e}")

    # Give background thread a chance to finish
    bg_thread = context.get("_bg_thread")
    if bg_thread:
        bg_thread.join(timeout=5.0)

    cognition_embedding_storage.close()
    logger.info("Shutdown complete")


def _dump_all_thread_stacks(file) -> None:
    """Write every live thread's current stack to ``file`` (stderr in
    production). Uses ``sys._current_frames()`` rather than
    ``faulthandler.dump_traceback`` -- the latter requires a real OS file
    descriptor (``.fileno()``), which production stderr has but a captured/
    wrapped stream (tests, some log-shipping setups) does not; this must work
    in both."""
    names = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        print(f"--- thread {names.get(tid, tid)} ---", file=file)
        print("".join(traceback.format_stack(frame)), file=file)
    with contextlib.suppress(AttributeError):
        file.flush()


class _DispatchStallForensics(Middleware):
    """WP-Wedge-2 §W2-f: production self-forensics for a mode-(a) stall
    (docs/wp-wedge2-plan.md rev 4, replacing WP2-AC2 after the §W2-a negative
    result -- Incident A's exact blocking site couldn't be pinned from pure
    Python, so the next REAL occurrence must pin its own stack).

    Detection mechanism (implementer's craft, per the brief): races each
    tool dispatch against a per-call timeout INSIDE the dispatch seam
    (FastMCP's public ``on_call_tool`` middleware hook, first-party API, no
    monkeypatching) rather than a periodic sampling loop -- ties the stall
    signal exactly to the call that's actually stuck, with zero overhead on
    the healthy-serving common case (no wrapping/racing at all unless the
    load window or a degraded state is active). Runs entirely on the event
    loop; ``asyncio.ensure_future``/``asyncio.wait`` never spawn an OS thread,
    so this cannot itself trip INV-1.

    Once-per-process and stderr-only are NOT implementer's craft (brief,
    verbatim): the stack dump never touches disk (thread-context rule, same
    as loop-side breadcrumbs) and fires at most once per process
    (``_startup_timing.first_occurrence``, shared primitive with §W2-e's
    ``tool_served_degraded``). The in-flight call itself is NEVER cancelled
    (same rule as the heartbeat's cleanup) -- only observed and reported;
    ``await task`` after the dump still waits for the real result.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        fastmcp_ctx = context.fastmcp_context
        lc = fastmcp_ctx.lifespan_context if fastmcp_ctx is not None else None
        if lc is None:
            return await call_next(context)

        ready: threading.Event | None = lc.get("embedding_ready")
        loading = ready is not None and not ready.is_set()
        degraded = bool(lc.get("embedding_error")) or bool(lc.get("watchdog_fired"))
        if not (loading or degraded):
            return await call_next(context)

        config = lc.get("config")
        threshold = getattr(config, "dispatch_stall_threshold", _DISPATCH_STALL_THRESHOLD_DEFAULT)

        task = asyncio.ensure_future(call_next(context))
        done, _pending = await asyncio.wait({task}, timeout=threshold)
        if task not in done:
            if _startup_timing.first_occurrence("dispatch_stall_detected"):
                tool_name = getattr(context.message, "name", "?")
                print(
                    f"[vibe-cognition] DISPATCH STALL: tool={tool_name!r} exceeded "
                    f"{threshold:.0f}s in flight during a load/degraded window -- "
                    "dumping all-thread stacks",
                    file=sys.stderr,
                    flush=True,
                )
                _dump_all_thread_stacks(sys.stderr)
            await task  # never cancel -- same rule as _worker_heartbeat's cleanup
        return task.result()


# Fallback only for a lifespan context missing "config" (shouldn't happen in
# a real server; defensive for hand-built test contexts) -- keep in sync with
# Settings.dispatch_stall_threshold's own default.
_DISPATCH_STALL_THRESHOLD_DEFAULT = 30.0


# Create the MCP server. SERVER_INSTRUCTIONS (single source of truth in instructions.py,
# also used by the post-compact re-injection hook) is surfaced to the agent every session
# via the MCP `initialize` handshake ("MCP Server Instructions").
mcp = FastMCP("Vibe Cognition", instructions=SERVER_INSTRUCTIONS, lifespan=lifespan)

# Register all tools
register_all_tools(mcp)

# WP-Wedge-2 §W2-f: dispatch-stall self-forensics, registered once against
# the module-level server singleton (see _DispatchStallForensics' docstring).
mcp.add_middleware(_DispatchStallForensics())


def main():
    """Entry point for the Vibe Cognition MCP server.

    The venv-health guard (WP-B, see _venv_guard.py) already ran at module
    import time, above -- by the time main() is reached the heavy natives
    already imported successfully, so this stays a plain mcp.run() call.
    """
    mcp.run()


if __name__ == "__main__":
    main()
