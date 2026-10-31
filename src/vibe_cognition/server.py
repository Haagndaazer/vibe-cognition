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
from . import _heavy_import_guard

import asyncio
import contextlib
import logging
import random
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
from .cognition.storage import JournalWriterUnavailableError
from .config import Settings, setup_logging
from .cognition.loss_notices import VECTOR_STORE_REBUILT_KIND, replace_kind
from .cognition.local_paths import write_path as local_write_path
from .cognition.storage import REHYDRATE_FLAG_FILENAME
from .embeddings import EmbeddingGenerator, VectorStore
from .embeddings.vector_store import DIMENSION_MISMATCH, warm_numpy
from .embeddings import sidecar_client
from .instructions import SERVER_INSTRUCTIONS
from . import lifecycle
from .running_version import stamp_running_server
from .stale_sweep import run_stale_sweep
from .tools import register_all_tools
from .tools.dispatch import prewarm_dispatch_executor
from .tools.cognition_tools import (
    _embed_document,
    _embed_entity_node,
    _embed_workflow,
    _node_from_dict,
    _store_last_error,
)
from .tools.project_registry import build_registry, compute_model_guard

_startup_timing.stamp("server_module_import_done")

logger = logging.getLogger(__name__)

# ── WP-Wedge (P0): bound the bg-thread heavy-import wedge ──────────────────
# WP-Sidecar (P0 endgame) subsumes _run_subprocess_import_probe and _watchdog
# entirely -- the heavy import no longer happens in this process at all, so
# there is nothing left to probe or watch a timeout on here; the sidecar
# supervisor (embeddings/sidecar_client.py) owns that now. The warm-pool
# machinery below is UNCHANGED (it keeps the stdio transport's anyio workers
# warm, independent of the embedding load).
_HEARTBEAT_INTERVAL = 3.0
_HEARTBEAT_WARM_COUNT = 4


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
    if not cognition_storage.can_write():
        # No confirmed identity: no shard to record the edges in. Deterministic edges
        # are idempotent, so the next confirmed session creates them.
        return
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
        try:
            total_created += cognition_storage.create_deterministic_edges(node_id)
        except JournalWriterUnavailableError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup deterministic matching skipped %s: %s", node_id, exc)

    if total_created:
        logger.info(
            f"Startup deterministic matching: created {total_created} part_of edge(s)"
        )


_RESYNC_MIN_INTERVAL_S = 30.0
_RESYNC_MAX_INTERVAL_S = 600.0


class _SyncAbortedError(Exception):
    """The vector store stopped taking reads or writes partway through a sync pass."""


def _check_store(embedding_storage: Any, during: str) -> None:
    error = _store_last_error(embedding_storage)
    if error:
        raise _SyncAbortedError(f"{during}: {error}")


def _check_writable(embedding_storage: Any) -> None:
    can_write = getattr(embedding_storage, "can_write", None)
    if callable(can_write) and can_write() is False:
        raise _SyncAbortedError("the vector store is not taking writes right now")


def _resync_when_store_recovers(embedding_storage: Any) -> None:
    request = getattr(embedding_storage, "request_resync_on_recovery", None)
    if callable(request):
        request()


def _sync_cognition_embeddings(
    cognition_storage: CognitionStorage,
    embedding_storage: VectorStore,
    generator: EmbeddingGenerator,
) -> dict[str, int]:
    """Embed every journal node the vector store is missing, unless the store cannot
    take writes: an unusable store is never read as an empty one (which would embed
    the whole graph for nothing). A pass the store cuts short stops at once and asks
    for a re-sync when the store next works."""
    empty = {"nodes": 0, "workflows": 0, "documents": 0}
    if getattr(embedding_storage, "available", True) is False:
        logger.warning("Embedding sync skipped: the vector store is not usable right now")
        _resync_when_store_recovers(embedding_storage)
        return empty
    try:
        return _sync_pass(cognition_storage, embedding_storage, generator)
    except _SyncAbortedError as e:
        if str(e).split(": ", 1)[-1].startswith(DIMENSION_MISMATCH):
            logger.warning(f"Embedding sync stopped: {e}")
        else:
            logger.warning(f"Embedding sync stopped early; it resumes when the vector store recovers ({e})")
            _resync_when_store_recovers(embedding_storage)
        return empty


def _sync_pass(
    cognition_storage: CognitionStorage,
    embedding_storage: VectorStore,
    generator: EmbeddingGenerator,
) -> dict[str, int]:
    """Embed every journal node the vector store is missing.

    This handles a teammate's pulled journal entries, the first start on a new
    store, and a rebuild after a damaged store was set aside. Plain nodes are
    taken in a per-process shuffled order and re-checked just before each embed,
    so several sessions syncing one project split the work instead of repeating it.

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
    existing_meta: dict[str, Any] = embedding_storage.get_existing([n["id"] for n in all_nodes])
    _check_store(embedding_storage, "reading the stored vectors")
    existing_ids: set[str] = set(existing_meta)

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
    chunk_present: set[str] = (
        embedding_storage.existing_ids(expected_chunk_ids) if expected_chunk_ids else set()
    )
    if expected_chunk_ids:
        _check_store(embedding_storage, "reading the stored chunks")

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
        logger.info(f"Syncing {len(non_doc_missing)} cognition nodes to the vector store...")
        random.shuffle(non_doc_missing)
        for n in non_doc_missing:
            present = embedding_storage.existing_ids([n["id"]])
            _check_store(embedding_storage, "re-checking a node")
            if present:
                continue
            _check_writable(embedding_storage)
            # WP-TC6 (BACK-COMPAT): per-node tolerance for a type this process
            # doesn't recognize (e.g. a NEWER client wrote a node type this
            # server predates — the exact defect discovered in an OLDER
            # version's sync during this WP's peer review, where the first
            # unknown-type node raised and silently starved every node behind
            # it in iteration order). One bad node is logged and skipped; the
            # rest of the batch still syncs.
            try:
                _embed_entity_node(embedding_storage, generator, _node_from_dict(n["id"], n))
            except Exception as e:  # noqa: BLE001 - a single node must never abort the batch
                logger.warning(f"Cognition embedding sync: skipped node {n['id']!r}: {e}")
                continue
            _check_store(embedding_storage, f"writing {n['id']}")

    if wf_to_embed:
        logger.info(f"Syncing {len(wf_to_embed)} workflow node(s) to the vector store...")
        for n in wf_to_embed:
            _check_writable(embedding_storage)
            _embed_workflow(embedding_storage, generator, _node_from_dict(n["id"], n))
            _check_store(embedding_storage, f"writing workflow {n['id']}")

    # Documents: node vector + sidecar chunks via the shared _embed_document (the same
    # delete-then-write path store-time uses — no chunk-contract drift). A sidecar-less
    # reference doc (teammate pulled the journal but not the sidecar) embeds the node
    # only (sidecar_text == "" -> zero chunks); not an error.
    for node, sidecar_text in docs_to_embed:
        _check_writable(embedding_storage)
        _embed_document(
            embedding_storage, generator, node["id"],
            node.get("summary", ""), node.get("detail", ""), sidecar_text,
            from_agent=node.get("metadata", {}).get("from_agent"),
        )
        _check_store(embedding_storage, f"writing document {node['id']}")

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
    embedding_storage: VectorStore,
) -> None:
    """N1 startup reclamation (§9 N1b): delete vector ids (incl. ``#chunk-*``) whose
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
    all_ids = embedding_storage.all_ids()
    if not all_ids:
        return
    graph_ids = {n["id"] for n in cognition_storage.get_all_nodes()}
    orphans = [cid for cid in all_ids if cid.split("#chunk-")[0] not in graph_ids]
    if not orphans:
        return
    embedding_storage.delete_ids(orphans)
    logger.info(f"Orphan-embedding sweep: removed {len(orphans)} stale vector(s)")


def _maintain_vector_store(
    context: dict[str, Any],
    cognition_storage: CognitionStorage,
    vector_store: VectorStore,
    generator: EmbeddingGenerator,
) -> None:
    """Re-run the embedding sync when the store asks for it (a new generation, or
    writes that failed and the store has recovered).

    Runs on the existing background thread after the start-up sync, so no thread
    is started after the handshake. Passes are at least _RESYNC_MIN_INTERVAL_S
    apart, doubling up to _RESYNC_MAX_INTERVAL_S while passes keep being cut short,
    so a store that peers keep busy can never make sessions spin. Returns when the
    lifespan sets the stop flag.
    """
    resync: threading.Event = context["vector_resync"]
    interval = _RESYNC_MIN_INTERVAL_S
    last_end: float | None = None
    while True:
        resync.wait()
        if context.get("_vector_maintenance_stop"):
            return
        while last_end is not None and time.monotonic() < last_end + interval:
            if context.get("_vector_maintenance_stop"):
                return
            time.sleep(min(1.0, last_end + interval - time.monotonic()))
        if context.get("_vector_maintenance_stop"):
            return
        resync.clear()
        logger.info("Vector store asked for a re-sync; embedding what it is missing from the journal")
        context["embedding_sync_done"].clear()
        aborted = False
        try:
            context["embedding_sync_progress"] = _sync_cognition_embeddings(
                cognition_storage, vector_store, generator
            )
            aborted = bool(_store_last_error(vector_store)) or (
                getattr(vector_store, "can_write", lambda: True)() is False
            )
        except Exception as e:  # noqa: BLE001 - the loop must survive a failed pass
            logger.warning(f"Vector store re-sync failed: {e}")
            aborted = True
        finally:
            context["embedding_sync_done"].set()
        interval = min(interval * 2, _RESYNC_MAX_INTERVAL_S) if aborted else _RESYNC_MIN_INTERVAL_S
        last_end = time.monotonic()


def _record_vector_store_rebuilt(cognition_dir: Any, info: dict[str, Any]) -> None:
    """Leave a one-time notice for the next session start that the index was rebuilt."""
    try:
        flag = local_write_path(cognition_dir, REHYDRATE_FLAG_FILENAME)
        replace_kind(flag, (VECTOR_STORE_REBUILT_KIND,), {"kind": VECTOR_STORE_REBUILT_KIND, **info})
    except Exception as e:  # noqa: BLE001 - a notice must never break the store
        logger.warning(f"Could not record the vector store rebuild notice: {e}")


def _warn_if_in_repo_store_unignored(config: Settings) -> None:
    store_dir = config.cognition_vectors_path
    try:
        store_dir.resolve().relative_to(config.cognition_dir.resolve())
    except (ValueError, OSError):
        return
    ignore = config.cognition_dir / ".gitignore"
    try:
        lines = {line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()}
    except OSError:
        lines = set()
    if not lines & {"local/", "local", "/local/", "/local"}:
        logger.warning(
            f"The vector store is inside the repo at {store_dir} and .cognition/.gitignore "
            "does not ignore local/; do not commit it"
        )


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

    # WP-Lifecycle-2 Stage 1 (F4): machine-wide stale-sibling sweep, LOG-ONLY
    # (never acts on anything; auto-kill needs an explicit human ruling and is
    # out of scope). Execution site pinned here — never pre-yield (enumeration
    # costs 100ms-2s: handshake latency), never a new post-yield thread
    # (WP-Wedge INV-1). Doubles as leak-recurrence telemetry for Stage 2.
    try:
        sweep = run_stale_sweep()
        context["stale_server_sweep"] = sweep
        logger.info(f"Stale-sibling sweep (machine-wide, log-only): {sweep}")
    except Exception as e:  # pragma: no cover - defensive, must never block startup
        context["stale_server_sweep"] = {"error": str(e)}

    vector_store: VectorStore | None = context.get("cognition_embedding_storage")
    if vector_store is not None:
        try:
            vector_store.ensure_open()
            vector_store.health_check()
        except Exception as e:  # pragma: no cover - defensive, the store never raises
            logger.warning(f"Vector store health check failed: {e}")
        _startup_timing.stamp_and_flush("vectors_health_done")

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
        if vector_store is not None:
            guard, guard_warning, _ = compute_model_guard(
                vector_store, config.embedding_model, config.embedding_dimensions,
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

        # Load embedding model (the bottleneck: 2-30s, unbounded-but-
        # supervised for the sidecar path).
        t_start = _startup_timing.stamp_and_flush("bg_model_load_start")
        logger.info(f"Background: loading embedding model ({config.embedding_backend})...")
        if config.embedding_backend == "ollama":
            # Structurally untouched by WP-Sidecar (ollama already talks to
            # an external process over HTTP) -- unchanged direct call.
            context["embedding_generator"] = EmbeddingGenerator.from_config(config)
            context["embedding_error"] = None
            context["embedding_ready"].set()
        else:
            # WP-Sidecar: the supervisor (constructed in lifespan(), pre-
            # yield) IS the sole writer of embedding_generator/embedding_
            # error and the one that sets embedding_ready -- it never
            # raises, recording a degraded outcome instead of propagating
            # one, since a failed load here is an EXPECTED, supervised
            # outcome (not a bug), unlike the exceptions this function's own
            # except-block below still exists to catch.
            context["_sidecar_supervisor"].ensure_ready()
        t_model = _startup_timing.stamp_and_flush("bg_model_loaded")
        logger.info(f"Background: embedding model loaded in {t_model - t_start:.1f}s")
        logger.info("All tools now available")

        if context.get("embedding_error"):
            # Degraded (in-budget retries exhausted, or ollama construction
            # failed) -- mirrors the old probe-degrade early-return: skip the
            # edge/sync steps below, which need a genuinely working
            # generator, not the always-present-but-degraded proxy WP-Sidecar
            # installs so lazy/periodic recovery has something to retry on.
            context["embedding_sync_done"].set()
            _startup_timing.flush_to_disk()
            return

        embedding_generator = context["embedding_generator"]

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

        sync_allowed = context.get("home_model_guard") not in ("dim-mismatch", "model-mismatch")
        if cognition_storage and cognition_embedding_storage and embedding_generator:
            if sync_allowed:
                logger.info("Syncing cognition embeddings...")
                context["embedding_sync_progress"] = _sync_cognition_embeddings(
                    cognition_storage, cognition_embedding_storage, embedding_generator
                )
            else:
                logger.warning(
                    "Embedding sync skipped: the vector store was built with a different "
                    "embedding model or dimension (see get_status home_model_drift). To "
                    "rebuild with the configured model, close every session in this project "
                    "and delete the vector store folder"
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

        resync = context.get("vector_resync")
        if resync is not None and cognition_storage and cognition_embedding_storage and sync_allowed:
            _maintain_vector_store(context, cognition_storage, cognition_embedding_storage, embedding_generator)

    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        # No watchdog racing this anymore (WP-Sidecar) -- a plain write is
        # sufficient; a genuine error here always wins, matching the old
        # clobber-guard's outcome without needing a lock to get there.
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
    stamp_running_server(config.cognition_dir)
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

    logger.info(f"Opening the vector store at {config.cognition_vectors_path}...")
    _startup_timing.stamp("vectors_open_start")
    cognition_embedding_storage = VectorStore.open_home(
        config.cognition_vectors_path,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
        on_quarantine=lambda info: _record_vector_store_rebuilt(config.cognition_dir, info),
        startup=True,
    )
    _startup_timing.stamp("vectors_open_done")
    if not cognition_embedding_storage.available:
        logger.warning(
            f"Vector store not ready at start-up ({cognition_embedding_storage.status()['reason']}); "
            "graph tools work, and the background thread retries the open"
        )
    _warn_if_in_repo_store_unignored(config)

    # WP-Wedge-2 §W2-c (INV-2): pre-exercise the numpy and SQLite paths search
    # and the background thread use, so their lazy setup (BLAS threads, numpy code
    # paths) happens pre-yield, before the bg import thread opens its wedge window.
    # Best-effort and budgeted: a failure or overrun is logged, never fatal.
    _pre_exercise_start = time.monotonic()
    try:
        warm_numpy()
        cognition_embedding_storage.count_documents()
        cognition_embedding_storage.count_documents(filter={"is_chunk": True})
    except Exception as e:
        logger.warning(f"Vector store pre-exercise failed (non-fatal): {e}")
    _pre_exercise_elapsed = time.monotonic() - _pre_exercise_start
    if _pre_exercise_elapsed > 0.2:
        logger.warning(
            f"Vector store pre-exercise took {_pre_exercise_elapsed:.3f}s (budget 200ms)"
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
        "curation_sessions": {},
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
        # WP-Sidecar: the ONE writer of embedding_generator/embedding_error/
        # embedding_ready for the non-ollama case (replaces WP-Wedge's
        # _wedge_lock atomicity discipline -- there is no second writer
        # racing it anymore, since the watchdog is gone). None for ollama
        # (structurally untouched by this WP).
        "_sidecar_supervisor": None,
        "vector_resync": threading.Event(),
    }
    cognition_embedding_storage.on_resync_needed = context["vector_resync"].set
    if config.embedding_backend != "ollama":
        context["_sidecar_supervisor"] = sidecar_client.SidecarSupervisor(config, context)

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
    # Defensive degrade-don't-abort (gate finding): an unexpected failure
    # arming either watch (e.g. thread creation itself failing under extreme
    # resource exhaustion) must not crash the ENTIRE server startup over an
    # optional safety net -- consistent with this WP's own degraded-arm
    # philosophy elsewhere (NULL-grandparent, ACCESS_DENIED) of "even if
    # part of this doesn't work, keep serving."
    try:
        lifecycle.arm_ancestor_watch()
    except Exception as e:
        logger.warning(f"Failed to arm ancestor-death watch (non-fatal): {e}")
    try:
        lifecycle.arm_stdin_watch()
    except Exception as e:
        logger.warning(f"Failed to arm stdin-pipe watch (non-fatal): {e}")

    # WP-Lifecycle-2 Stage 1 (report-only): identify the stdin pipe PEER --
    # the real client (claude.exe: uv passes stdio handles through, it does
    # not re-pipe) -- and breadcrumb pid + image + verdict. IDENTIFICATION
    # ONLY: the resolved peer is NOT watched and no exit behavior changes in
    # this release; Stage 2 arms it only after field logs prove the
    # identification correct (docs/wp-lifecycle2-plan.md rev 4). Handle-opens
    # only, closed before return -- pre-yield-safe, no new threads (INV-1).
    try:
        lifecycle.resolve_stdin_pipe_peer()
    except Exception as e:
        logger.warning(f"Pipe-peer identification failed (non-fatal, log-only): {e}")

    bg_thread = threading.Thread(
        target=_load_embeddings_and_sync,
        args=(config, context),
        daemon=True,
    )
    bg_thread.start()
    context["_bg_thread"] = bg_thread

    # WP-Wedge §3c: armed for the whole load window, exits itself as soon as
    # embedding_ready sets. WP-Sidecar subsumes the old §3b watchdog task --
    # the supervisor (constructed above) now KNOWS the sidecar's real state
    # and can act, which a fixed-timeout watchdog polling from outside never
    # could; there is no separate task to create here anymore.
    heartbeat_task = asyncio.create_task(_worker_heartbeat(context))
    context["_heartbeat_task"] = heartbeat_task

    logger.info("Vibe Cognition ready (embedding model loading in background)")
    _startup_timing.stamp("handshake_yield")
    # WP-Sidecar §S-c: runtime companion to the static AST guard -- confirms
    # the heavy chain genuinely never loaded in THIS process by the time the
    # handshake completes (the bg thread hasn't even started the sidecar
    # load yet at this point, so this should always be clean; a violation
    # here would mean something else entirely pulled the chain in).
    _heavy_import_guard.check_and_log("handshake")

    yield context

    # ── Cleanup ───────────────────────────────────────────────────

    logger.info("Shutting down Vibe Cognition...")

    # WP-Wedge cleanup, NOT a wedge mitigation: the heartbeat task self-exits
    # once embedding_ready sets, so this cancel is a no-op in the common
    # (already-finished) case; it only matters for a shutdown that races a
    # STILL-RUNNING load window, e.g. the server exits mid-load. Never
    # touches disk, so no HEISENBUG GUARD concern.
    heartbeat_task = context.get("_heartbeat_task")
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    # WP-Sidecar: kill the sidecar subprocess (if any) cleanly on shutdown --
    # best-effort, mirrors the probe-style kill+wait this WP subsumes.
    supervisor = context.get("_sidecar_supervisor")
    if supervisor is not None:
        with contextlib.suppress(Exception):
            supervisor.shutdown()

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

    context["_vector_maintenance_stop"] = True
    context["vector_resync"].set()

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
        degraded = bool(lc.get("embedding_error"))
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
