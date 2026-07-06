"""WP-Sidecar (P0 endgame) §S-a: the sidecar entry module.

THE sanctioned heavy-import site going forward -- `SentenceTransformersBackend`
(the only thing that touches sentence_transformers/torch/scipy) lives HERE,
never in the main server process (src/vibe_cognition/embeddings/generator.py
no longer constructs it; see that module's SidecarBackend proxy). Run via
`uv run --no-sync --directory ... python -m vibe_cognition.embeddings.sidecar`
(same shape as plugin.json's own server launch line), spawned by
sidecar_client.py's supervisor, never by a human directly.

Why the wedge moves here safely: if this process's import of
sentence_transformers (and the torch it pulls in) wedges inside a native DLL
load, it holds ITS OWN loader lock, in ITS OWN process -- the main server
process is completely unaffected and keeps dispatching every tool
(WP-Wedge-2's spawn-free dispatch + import-free surface stay the backstop
for whatever else surprises us, but this WP removes the wedge SOURCE itself
from the server entirely). The supervisor observes this process from
outside (kill+wait, same hardened pattern WP-Wedge's now-subsumed probe
used) and can retry with a fresh process, converting "wedged forever inside
a process nothing can safely act on" into "bounded retry with a supervisor".

Protocol: newline-delimited JSON request/response + unsolicited lock
events, see _sidecar_protocol.py. Single-threaded, one request in flight at
a time (this is a synchronous request/response wire, not a concurrent
server) -- "generate" already takes a list of texts, so there is no need
for request pipelining.

Parent-death safety: arms WP-Lifecycle's OWN ancestor-death watch at
depth=1 (direct parent, no intermediary) -- the sidecar's real parent IS
the server process itself (server spawns it directly via subprocess.Popen,
no uv/shell in between for the CHILD side of this spawn, unlike the
server's own uv-intermediated launch), so the orphan problem this WP would
otherwise double is closed the same way the server's own is.
"""

from __future__ import annotations

import sys
import time

from .. import _startup_timing, lifecycle
from . import _load_mutex, _sidecar_protocol
from ._backend import EmbeddingBackend
from .generator import NOMIC_DOCUMENT_PREFIX, NOMIC_QUERY_PREFIX


class SentenceTransformersBackend(EmbeddingBackend):
    """Moved here verbatim from generator.py (WP-C's original site) -- THE
    lazy heavy import, now physically unreachable from the server process
    since generator.py no longer defines or imports this class."""

    DOCUMENT_PREFIX = NOMIC_DOCUMENT_PREFIX
    QUERY_PREFIX = NOMIC_QUERY_PREFIX

    def __init__(self, model_name: str, dimensions: int | None = None, revision: str | None = None):
        import threading

        from sentence_transformers import SentenceTransformer

        t0 = time.monotonic()
        self._model: SentenceTransformer = SentenceTransformer(
            model_name, trust_remote_code=True, revision=revision
        )
        elapsed = time.monotonic() - t0
        self._dimensions = dimensions
        self._lock = threading.Lock()
        sys.stderr.write(f"[sidecar] model loaded in {elapsed:.1f}s\n")
        sys.stderr.flush()

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        prefix = self.QUERY_PREFIX if is_query else self.DOCUMENT_PREFIX
        prefixed = [prefix + t for t in texts]
        with self._lock:
            embeddings = self._model.encode(prefixed, convert_to_numpy=True)
        if self._dimensions:
            embeddings = embeddings[:, : self._dimensions]
        return embeddings.tolist()


def _write(obj: dict) -> None:
    sys.stdout.write(_sidecar_protocol.encode_line(obj))
    sys.stdout.flush()


def _respond(request_id, result=None, error: str | None = None) -> None:
    _write(_sidecar_protocol.make_response(request_id, result=result, error=error))


def _emit(event_name: str) -> None:
    _write(_sidecar_protocol.make_event(event_name))


def _do_load(args: dict) -> EmbeddingBackend:
    """§2 stampede killer: serialize the heavy import + model load across
    every sidecar on this machine via a named mutex. Emits lock_wait before
    waiting and exactly one of lock_acquired / lock_acquired_abandoned /
    lock_wait_expired after -- the supervisor's load-timeout clock starts at
    whichever of those three arrives, not at request submission (queueing
    behind another session's load must not count as wedged)."""
    mutex_wait_timeout = float(args.get("mutex_wait_timeout", 300.0))
    model_name = args["model_name"]
    dimensions = args.get("dimensions")
    revision = args.get("revision")

    handle = _load_mutex.create_mutex()
    _startup_timing.stamp("sidecar_mutex_wait")
    _emit("lock_wait")
    outcome = _load_mutex.acquire(handle, timeout_seconds=mutex_wait_timeout)

    held_lock = True
    if outcome == _load_mutex.AcquireOutcome.ACQUIRED:
        _startup_timing.stamp("sidecar_mutex_acquired")
        _emit("lock_acquired")
    elif outcome == _load_mutex.AcquireOutcome.ACQUIRED_ABANDONED:
        # IS successful acquisition (§2 non-negotiable) -- a previous holder
        # was killed by ITS supervisor while holding this, a designed and
        # expected outcome over the mutex's lifetime, not an error.
        _startup_timing.stamp("sidecar_mutex_acquired_abandoned")
        _emit("lock_acquired_abandoned")
    else:  # TIMEOUT -- proceed WITHOUT the lock (stampede risk beats never loading)
        _startup_timing.stamp("sidecar_mutex_wait_expired")
        _emit("lock_wait_expired")
        held_lock = False

    try:
        _startup_timing.stamp_and_flush("sidecar_model_load_start")
        backend = SentenceTransformersBackend(model_name, dimensions=dimensions, revision=revision)
        _startup_timing.stamp_and_flush("sidecar_model_loaded")
        return backend
    finally:
        if held_lock:
            _load_mutex.release(handle)
        _load_mutex.close(handle)


def main() -> None:
    _startup_timing.stamp_and_flush("sidecar_start")
    lifecycle.arm_ancestor_watch(depth=1)

    backend: EmbeddingBackend | None = None

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = _sidecar_protocol.decode_line(line)
        except Exception:
            continue  # malformed line -- the server never sends one; ignore rather than crash
        request_id = req.get("id")
        op = req.get("op")
        args = req.get("args") or {}
        try:
            if op == "load":
                incoming_version = req.get("protocol_version")
                if incoming_version != _sidecar_protocol.PROTOCOL_VERSION:
                    _respond(
                        request_id,
                        error=(
                            f"protocol_version_mismatch: sidecar={_sidecar_protocol.PROTOCOL_VERSION} "
                            f"client={incoming_version}"
                        ),
                    )
                    continue
                backend = _do_load(args)
                _respond(request_id, result="ok")
            elif op == "generate":
                if backend is None:
                    _respond(request_id, error="not_loaded")
                    continue
                texts = args.get("texts", [])
                is_query = args.get("input_type") == "query"
                vectors = backend.encode(texts, is_query=is_query)
                _respond(request_id, result=vectors)
            elif op == "ping":
                _respond(request_id, result={"loaded": backend is not None})
            else:
                _respond(request_id, error=f"unknown_op: {op!r}")
        except Exception as e:
            _respond(request_id, error=str(e))


if __name__ == "__main__":
    main()
