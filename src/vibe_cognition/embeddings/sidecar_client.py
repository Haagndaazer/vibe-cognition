"""WP-Sidecar (P0 endgame) §S-b: server-side sidecar client + supervisor.

Replaces the in-process model-load path (the old direct
`SentenceTransformersBackend()` construction) with a supervised child
process (embeddings/sidecar.py) the server talks to over stdin/stdout JSON
lines. Three pieces:

  _SidecarProcess -- one live subprocess: spawn, dedicated reader + writer
    drain threads, request/response correlation, unsolicited-event dispatch.
  SidecarSupervisor -- owns _SidecarProcess across its whole lifetime:
    in-budget kill+respawn+backoff retries, degrade, lazy-on-demand +
    slow-periodic recovery. THE SOLE WRITER of context["embedding_generator"/
    "embedding_error"] and the one that sets context["embedding_ready"] --
    replaces WP-Wedge's _wedge_lock atomicity discipline (two racing
    writers, bg thread + watchdog) with a single owner instead (no watchdog
    exists anymore to race).
  SidecarBackend -- the EmbeddingBackend proxy EmbeddingGenerator.from_config
    constructs for the non-ollama case; `.encode()` is a thin call into the
    supervisor.

IPC non-negotiables (v0.12.1 lesson, the design constraint not a footnote):
dedicated reader AND writer threads, a bounded outbound queue, round-trip
timeout owned by the SUPERVISOR (never the possibly-blocked requesting
thread) whose kill demonstrably closes handles and unblocks a stuck write.
stdin/stdout are PIPEs -- the sanctioned exception to the DEVNULL-only
subprocess rule, BECAUSE of the dedicated drain threads; stderr stays
DEVNULL (never a second undrained pipe).
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from .. import _startup_timing
from . import _sidecar_protocol
from ._backend import EmbeddingBackend

if TYPE_CHECKING:
    from ..config import Settings

_LOCK_EVENT_NAMES = ("lock_acquired", "lock_acquired_abandoned", "lock_wait_expired")


class SidecarError(RuntimeError):
    """A sidecar request timed out, failed, or the process is unavailable."""


class _SidecarProcess:
    """One live sidecar subprocess. See module docstring for the IPC shape."""

    _OUTBOUND_QUEUE_MAXSIZE = 16

    def __init__(self, python_exe: str, module: str, env: dict[str, str], cwd: str | None = None):
        self._proc = subprocess.Popen(  # noqa: S603 - fixed interpreter + literal module, no shell
            [python_exe, "-m", module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._outbound: queue.Queue = queue.Queue(maxsize=self._OUTBOUND_QUEUE_MAXSIZE)
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._next_id_lock = threading.Lock()
        self._event_callback = None  # set via set_event_callback

        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="vibe-sidecar-writer"
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="vibe-sidecar-reader"
        )
        self._writer_thread.start()
        self._reader_thread.start()

    def set_event_callback(self, callback) -> None:
        self._event_callback = callback

    def _writer_loop(self) -> None:
        while True:
            item = self._outbound.get()
            if item is None:  # shutdown sentinel (kill())
                return
            try:
                self._proc.stdin.write(_sidecar_protocol.encode_line(item))
                self._proc.stdin.flush()
            except Exception as e:
                # A broken pipe (process killed/crashed) is expected and
                # silent; anything else is a real bug -- log it loudly
                # rather than swallow it (a silent writer-thread death here
                # once hid a real TypeError from a missing text=True on the
                # Popen call, discovered only because every pending waiter
                # then hung on ITS OWN timeout with no clue why).
                if self._proc.poll() is None:
                    sys.stderr.write(f"[vibe-sidecar-client] writer thread failed unexpectedly: {e!r}\n")
                    sys.stderr.flush()
                return

    def _reader_loop(self) -> None:
        try:
            for raw_line in self._proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = _sidecar_protocol.decode_line(line)
                except Exception:
                    continue
                if _sidecar_protocol.is_event(obj):
                    if self._event_callback is not None:
                        self._event_callback(obj["event"])
                    continue
                request_id = obj.get("id")
                with self._pending_lock:
                    entry = self._pending.pop(request_id, None)
                if entry is not None:
                    event, box = entry
                    box[0] = obj
                    event.set()
        except Exception:
            pass
        finally:
            # stdout ended (process crashed/killed) -- wake every still-
            # pending waiter rather than leaving it blocked until ITS OWN
            # timeout (still correct either way, but this is faster and more
            # honest about why).
            with self._pending_lock:
                pending = list(self._pending.items())
                self._pending.clear()
            for _request_id, (event, box) in pending:
                box[0] = {"ok": False, "error": "sidecar_process_ended"}
                event.set()

    def _register(self) -> tuple[int, threading.Event, list]:
        with self._next_id_lock:
            self._next_id += 1
            request_id = self._next_id
        event = threading.Event()
        box: list = [None]
        with self._pending_lock:
            self._pending[request_id] = (event, box)
        return request_id, event, box

    def _deregister(self, request_id: int) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def send(self, op: str, args: dict, timeout: float) -> Any:
        """Enqueue a request and wait up to `timeout` for its response.
        Raises SidecarError on timeout, on an error response, or if the
        outbound queue itself is full (the writer thread appears stuck --
        this bounds how long a CALLER can be delayed by that, distinct from
        the writer thread's own unbounded blocking write, which only the
        supervisor's kill() can unblock)."""
        request_id, event, box = self._register()
        try:
            req = _sidecar_protocol.make_request(request_id, op, args)
            try:
                self._outbound.put(req, timeout=timeout)
            except queue.Full as e:
                raise SidecarError(
                    f"sidecar outbound queue full -- writer thread appears stuck ({op})"
                ) from e

            if not event.wait(timeout=timeout):
                raise SidecarError(f"sidecar request timed out ({op}, {timeout}s)")
        finally:
            self._deregister(request_id)

        response = box[0]
        if not response.get("ok", False):
            raise SidecarError(response.get("error") or "sidecar request failed")
        return response.get("result")

    def send_load(self, args: dict, mutex_wait_timeout: float, load_timeout: float) -> Any:
        """Special-cased wait for the 'load' op: the timeout clock starts at
        lock_acquired/lock_acquired_abandoned/lock_wait_expired (whichever
        arrives first), NOT at request submission -- queueing behind
        another session's load must not count as wedged (§2). Bounded
        overall by mutex_wait_timeout (bounds the wait for one of those
        three events, with slack for the round trip) + load_timeout (bounds
        the model load itself, from that point)."""
        request_id, event, box = self._register()
        clock_started = threading.Event()

        outer_callback = self._event_callback

        def _combined(name: str) -> None:
            if name in _LOCK_EVENT_NAMES:
                clock_started.set()
            if outer_callback is not None:
                outer_callback(name)

        self._event_callback = _combined
        try:
            req = _sidecar_protocol.make_request(request_id, "load", args)
            try:
                self._outbound.put(req, timeout=mutex_wait_timeout)
            except queue.Full as e:
                raise SidecarError("sidecar outbound queue full -- writer thread appears stuck (load)") from e

            # Bound the wait for a lock event with generous slack over the
            # sidecar's own mutex_wait_timeout (its round trip + our IPC).
            if not clock_started.wait(timeout=mutex_wait_timeout + 15.0):
                raise SidecarError("sidecar never signaled lock acquisition (mutex wait timeout exceeded)")

            if not event.wait(timeout=load_timeout):
                raise SidecarError(f"sidecar load timed out ({load_timeout}s after lock acquisition)")
        finally:
            self._event_callback = outer_callback
            self._deregister(request_id)

        response = box[0]
        if not response.get("ok", False):
            raise SidecarError(response.get("error") or "sidecar load failed")
        return response.get("result")

    def kill(self) -> None:
        """probe-style kill+wait (WP-Wedge's now-subsumed probe pattern) --
        demonstrably closes handles, unblocking any thread (the writer
        thread, most likely) stuck in a blocking read/write on this
        process's pipes."""
        with contextlib.suppress(Exception):
            self._proc.kill()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=10)
        with contextlib.suppress(Exception):
            self._outbound.put_nowait(None)  # wake the writer thread's queue.get()

    def poll(self) -> int | None:
        return self._proc.poll()


class SidecarSupervisor:
    """Owns one sidecar's whole lifecycle. See module docstring."""

    def __init__(self, config: Settings, context: dict[str, Any]):
        self._config = config
        self._context = context
        self._state_lock = threading.RLock()
        self._process: _SidecarProcess | None = None
        self._degraded = False
        self._loading = False
        self._last_lock_event: str | None = None
        self._shutdown_event = threading.Event()
        self._demand_event = threading.Event()
        self._recovery_thread: threading.Thread | None = None

    # ── spawn/load ────────────────────────────────────────────────────────

    def _on_lock_event(self, name: str) -> None:
        self._last_lock_event = name
        _startup_timing.stamp(f"sidecar_client_{name}")

    def _spawn(self) -> _SidecarProcess:
        env = dict(os.environ)
        proc = _SidecarProcess(
            python_exe=sys.executable,
            module="vibe_cognition.embeddings.sidecar",
            env=env,
        )
        proc.set_event_callback(self._on_lock_event)
        return proc

    def _attempt_load(self) -> bool:
        """One spawn+load attempt. Kills+discards the process on ANY
        failure (timeout or error response) so the next attempt starts
        fresh (probe-style kill+wait, matching WP-Wedge's now-subsumed
        _run_subprocess_import_probe)."""
        proc = self._spawn()
        with self._state_lock:
            self._process = proc
        try:
            proc.send_load(
                {
                    "model_name": self._config.embedding_model,
                    "dimensions": self._config.embedding_dimensions,
                    "revision": self._config.embedding_revision,
                    "mutex_wait_timeout": self._config.sidecar_mutex_wait_timeout,
                },
                mutex_wait_timeout=self._config.sidecar_mutex_wait_timeout,
                load_timeout=self._config.sidecar_load_timeout,
            )
            return True
        except SidecarError as e:
            _startup_timing.stamp("sidecar_load_attempt_failed")
            sys.stderr.write(f"[vibe-sidecar-client] load attempt failed: {e}\n")
            proc.kill()
            with self._state_lock:
                if self._process is proc:
                    self._process = None
            return False

    def ensure_ready(self) -> None:
        """Called ONCE, from the bg thread (_load_embeddings_and_sync).
        Blocks through the in-budget retry loop (bounded by
        sidecar_max_retry_attempts), then writes context["embedding_
        generator"]/["embedding_error"] and sets context["embedding_ready"]
        -- in that order, so ready=True is never observable with generator
        still unset (this WP's replacement for the old watchdog-race
        stranding guard: there is no second writer to race anymore).
        Never raises -- a degraded outcome is recorded, not propagated, so
        the caller's existing exception handling stays focused on genuinely
        unexpected errors elsewhere in the bg thread."""
        ok = False
        attempts = max(1, self._config.sidecar_max_retry_attempts)
        for attempt in range(attempts):
            if self._attempt_load():
                ok = True
                break
            if attempt < attempts - 1:
                time.sleep(self._config.sidecar_retry_backoff_seconds)

        # wp2-import-free: sanctioned -- EmbeddingGenerator.from_config (module
        # top level) constructs SidecarBackend from THIS module, so hoisting
        # this import back to sidecar_client.py's own top level creates a
        # genuine circular import (generator.py -> sidecar_client.py ->
        # generator.py). Provably safe as a function-body import:
        # generator.py's own top-level import already fully loads this
        # module during the server's normal import, well before ensure_ready
        # ever runs -- always a sys.modules cache hit, never a fresh import.
        from .generator import EmbeddingGenerator  # wp2-import-free: sanctioned

        with self._state_lock:
            self._context["embedding_generator"] = EmbeddingGenerator(SidecarBackend(self))
            if ok:
                self._context["embedding_error"] = None
                self._degraded = False
            else:
                self._context["embedding_error"] = (
                    f"sidecar embedding load failed after {attempts} attempt(s); "
                    "search degraded -- will keep retrying in the background"
                )
                self._degraded = True
        self._context["embedding_ready"].set()

        self._recovery_thread = threading.Thread(
            target=self._recovery_loop, daemon=True, name="vibe-sidecar-recovery"
        )
        self._recovery_thread.start()

    # ── recovery (lazy-on-demand + slow periodic) ────────────────────────

    def _recovery_loop(self) -> None:
        """Runs for the server's whole lifetime. Production evidence says
        wedged loads eventually complete -- recovery must never be
        permanent (a supervisor that gives up after the in-budget attempts
        would be strictly worse than v0.15.4's late recovery). Wakes either
        on its own periodic schedule OR early when `notify_demand()` pokes
        it (an actual embedding request arrived while degraded)."""
        while not self._shutdown_event.is_set():
            self._demand_event.wait(timeout=self._config.sidecar_periodic_retry_interval)
            self._demand_event.clear()
            if self._shutdown_event.is_set():
                return
            with self._state_lock:
                degraded = self._degraded
            if not degraded:
                continue
            if self._attempt_load():
                from .generator import EmbeddingGenerator  # wp2-import-free: sanctioned

                with self._state_lock:
                    self._context["embedding_generator"] = EmbeddingGenerator(SidecarBackend(self))
                    self._context["embedding_error"] = None
                    self._degraded = False
                _startup_timing.stamp("sidecar_late_recovery")

    def notify_demand(self) -> None:
        """Cheap poke: wake the recovery loop NOW instead of waiting for its
        next scheduled tick, because an actual embedding request just
        arrived while degraded. Called from require_embeddings/get_lifespan
        -- never blocks, never itself retries."""
        self._demand_event.set()

    # ── live requests ─────────────────────────────────────────────────────

    def generate(self, texts: list[str], is_query: bool) -> list[list[float]]:
        with self._state_lock:
            degraded = self._degraded
            proc = self._process
        if degraded or proc is None:
            raise SidecarError(self._context.get("embedding_error") or "sidecar unavailable")
        try:
            return proc.send(
                "generate",
                {"texts": texts, "input_type": "query" if is_query else "document"},
                timeout=self._config.sidecar_request_timeout,
            )
        except SidecarError:
            # Round-trip timeout owned by the supervisor, not the requesting
            # thread: kill so the NEXT call (or the recovery loop) starts
            # fresh, and mark degraded so get_status reflects reality.
            proc.kill()
            with self._state_lock:
                if self._process is proc:
                    self._process = None
                self._degraded = True
            raise

    # ── status / shutdown ────────────────────────────────────────────────

    def status(self) -> str:
        """spawning | loading | waiting-for-load-lock | ready | error: ...
        -- for get_status (§S-d)."""
        with self._state_lock:
            if self._degraded:
                return f"error: {self._context.get('embedding_error')}"
            if self._process is None:
                return "spawning"
            if self._last_lock_event == "lock_wait":
                return "waiting-for-load-lock"
            if self._context["embedding_ready"].is_set():
                return "ready"
            return "loading"

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._demand_event.set()  # wake the recovery thread so it can exit
        with self._state_lock:
            proc = self._process
            self._process = None
        if proc is not None:
            proc.kill()


class SidecarBackend(EmbeddingBackend):
    """Thin proxy EmbeddingGenerator.from_config constructs for the non-
    ollama case -- `.encode()` is a straight call into the supervisor."""

    def __init__(self, supervisor: SidecarSupervisor):
        self._supervisor = supervisor

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        return self._supervisor.generate(texts, is_query=is_query)


_standalone_supervisor: SidecarSupervisor | None = None
_standalone_supervisor_lock = threading.Lock()


def get_or_create_standalone_supervisor(config: Settings) -> SidecarSupervisor:
    """For callers that construct an EmbeddingGenerator directly, outside
    the MCP server's lifespan() (dashboard/cli.py's dev tool; any future
    standalone/test code) -- there is no real request-scoped context to
    attach to, so this builds a throwaway one with its own embedding_ready
    Event. Lazy-recovery still works internally (the supervisor is a real,
    long-lived object once created), it just has nothing external to update
    on recovery, which is fine for a standalone caller.

    The REAL MCP server never calls this -- lifespan() constructs its own
    context-attached SidecarSupervisor directly (server.py), and
    _load_embeddings_and_sync drives it via ensure_ready()/context without
    going through EmbeddingGenerator.from_config at all, so its supervisor's
    lazy-recovery correctly updates the real, live request context.
    """
    global _standalone_supervisor
    with _standalone_supervisor_lock:
        if _standalone_supervisor is None:
            throwaway_context: dict[str, Any] = {
                "embedding_ready": threading.Event(),
                "embedding_error": None,
                "embedding_generator": None,
            }
            _standalone_supervisor = SidecarSupervisor(config, throwaway_context)
        return _standalone_supervisor
