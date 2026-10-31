"""SQLite + numpy vector store: a regenerable cache of the journal's embeddings.

Exact cosine search over an in-memory matrix that is reloaded whenever any process
commits. Damage never takes the server down: a confirmed-corrupt generation is marked
and the next one is created, so nothing open ever has to be renamed or deleted.
"""

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STORE_FORMAT = "f32-l2-json-v1"
EMBED_SCHEME = "doc-prefix-v1"

STATE_OK = "ok"
DIMENSION_MISMATCH = "dimension mismatch"
STATE_UNAVAILABLE = "unavailable"
STATE_NEWER_SCHEMA = "newer-schema"

MODE_HOME = "home"
MODE_READONLY = "readonly"
MODE_VIEWER = "viewer"

_ID_BATCH = 500
_SCALE_WARN = 50_000
_BUSY_TIMEOUT_MS = 2000
_STARTUP_BUSY_TIMEOUT_MS = 200
_JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
_REOPEN_INTERVAL_S = 30.0
_BUSY_BREAKER_S = 5.0
_CONFIRM_COOLDOWN_S = 300.0
_MAX_GENERATION_HOPS = 3
_MAX_QUARANTINES_PER_PROCESS = 3
_MAX_QUARANTINES_PER_DAY = 3
_COLUMN_FILTERS = frozenset({"entity_type", "node_id", "is_chunk"})
_GENERATION_FILE = re.compile(r"^vectors-(\d+)\.(sqlite3(-wal|-shm)?|corrupt)$")
_REPEATED_DAMAGE = (
    "repeated corruption: this location may not be safe for SQLite "
    "(network or sync folder?); not rebuilding again automatically"
)


class VectorStoreCorruptionError(Exception):
    """The store's contents fail validation (bad blob, bad metadata, missing table)."""


class _NewerSchemaError(Exception):
    pass


class _DimensionMismatchError(Exception):
    pass


def is_corruption(exc: BaseException) -> bool:
    if isinstance(exc, VectorStoreCorruptionError):
        return True
    if isinstance(exc, sqlite3.DatabaseError):
        name = getattr(exc, "sqlite_errorname", None) or ""
        return name.startswith("SQLITE_CORRUPT") or name == "SQLITE_NOTADB"
    return False


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    name = getattr(exc, "sqlite_errorname", None) or ""
    return name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")) or "locked" in str(exc).casefold()


def db_path(directory: Path, generation: int) -> Path:
    return directory / f"vectors-{generation}.sqlite3"


def marker_path(directory: Path, generation: int) -> Path:
    return directory / f"vectors-{generation}.corrupt"


def _generation_files(directory: Path) -> tuple[set[int], set[int]]:
    seen: set[int] = set()
    marked: set[int] = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return seen, marked
    for name in names:
        match = _GENERATION_FILE.match(name)
        if match:
            generation = int(match.group(1))
            seen.add(generation)
            if match.group(2) == "corrupt":
                marked.add(generation)
    return seen, marked


def active_generation(directory: Path) -> int:
    """The highest generation with any file, plus one if it is marked; 1 when empty."""
    seen, marked = _generation_files(directory)
    if not seen:
        return 1
    top = max(seen)
    return top + 1 if top in marked else top


def quarantined_generations(directory: Path) -> list[dict[str, Any]]:
    """Every marked generation, oldest first, with what its marker recorded."""
    _, marked = _generation_files(directory)
    found: list[dict[str, Any]] = []
    for generation in sorted(marked):
        info: dict[str, Any] = {"generation": generation}
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(marker_path(directory, generation).read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                info.update({k: data[k] for k in ("at", "reason") if k in data})
        found.append(info)
    return found


def flatten_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Primitive-only metadata (lists comma-joined, None dropped) plus ``updated_at``."""
    flat: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            flat[key] = value
        elif isinstance(value, list):
            flat[key] = ",".join(str(v) for v in value)
        else:
            flat[key] = str(value)
    flat["updated_at"] = datetime.now(UTC).isoformat()
    return flat


def warm_numpy() -> None:
    """Exercise every numpy path search and reload use, before the handshake."""
    matrix = np.frombuffer(np.ones(2048 * 768, dtype="<f4").tobytes(), dtype="<f4").reshape(2048, 768)
    np.isfinite(matrix).all(axis=1)
    query = np.ones(768, dtype=np.float32)
    query = query / float(np.linalg.norm(query))
    scores = matrix @ query
    top = np.sort(np.argpartition(-scores, 3)[:4])
    np.argsort(-scores[top], kind="stable")
    np.nonzero(np.array(["a", None], dtype=object) == "a")


def _check_filter(filter: dict[str, Any] | None) -> None:
    if not filter:
        return
    for key, value in filter.items():
        if not isinstance(key, str) or key.startswith("$"):
            raise ValueError(f"unsupported filter key: {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"unsupported filter value for {key!r}: {value!r}")


def _batched(items: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(items), _ID_BATCH):
        yield items[start:start + _ID_BATCH]


def _clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    return text.encode("utf-8", "replace").decode("utf-8")


def _prepare_vector(entity_id: str, embedding: Any) -> np.ndarray | None:
    try:
        vector = np.asarray(embedding, dtype=np.float32)
    except (TypeError, ValueError) as e:
        logger.error(f"Vector for {entity_id!r} is not numeric, not stored: {e}")
        return None
    norm = float(np.linalg.norm(vector)) if vector.ndim == 1 and vector.size else 0.0
    if not np.isfinite(norm) or norm == 0.0:
        logger.error(f"Vector for {entity_id!r} is empty, zero or not finite, not stored")
        return None
    return (vector / norm).astype("<f4")


def _row(entity_id: str, vector: np.ndarray, metadata: dict[str, Any], document: str | None) -> tuple:
    flat = flatten_metadata(metadata)
    entity_type = flat.get("entity_type")
    node_id = flat.get("node_id")
    return (
        entity_id,
        vector.tobytes(),
        json.dumps(flat),
        _clean_text(document),
        entity_type if isinstance(entity_type, str) else None,
        node_id if isinstance(node_id, str) else None,
        1 if flat.get("is_chunk") is True else 0,
    )


_UPSERT_SQL = (
    "INSERT INTO vectors(id, embedding, metadata, document, entity_type, node_id, is_chunk) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
    "embedding=excluded.embedding, metadata=excluded.metadata, document=excluded.document, "
    "entity_type=excluded.entity_type, node_id=excluded.node_id, is_chunk=excluded.is_chunk"
)


class VectorStore:
    """Vector index for one project, stored in ``<directory>/vectors-<generation>.sqlite3``."""

    def __init__(
        self,
        persist_directory: Path,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        on_quarantine: Callable[[dict[str, Any]], None] | None = None,
        startup: bool = False,
    ):
        self._init_state(Path(persist_directory), MODE_HOME, embedding_model, embedding_dimensions)
        self.on_quarantine = on_quarantine
        with self._lock:
            self._open_active(_STARTUP_BUSY_TIMEOUT_MS if startup else _BUSY_TIMEOUT_MS)

    @classmethod
    def open_home(
        cls,
        persist_directory: Path,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        on_quarantine: Callable[[dict[str, Any]], None] | None = None,
        startup: bool = False,
    ) -> "VectorStore":
        """The home project's store: created and stamped if missing. Never raises."""
        return cls(persist_directory, embedding_model, embedding_dimensions, on_quarantine, startup)

    @classmethod
    def open_existing(cls, persist_directory: Path) -> "VectorStore | None":
        """Read-only attach for a foreign project. Never creates or quarantines."""
        return cls._open_without_create(Path(persist_directory), MODE_READONLY)

    @classmethod
    def open_viewer(cls, persist_directory: Path) -> "VectorStore | None":
        """Read-write attach for the dashboard. Never creates, stamps or quarantines."""
        return cls._open_without_create(Path(persist_directory), MODE_VIEWER)

    @classmethod
    def _open_without_create(cls, directory: Path, mode: str) -> "VectorStore | None":
        if not db_path(directory, active_generation(directory)).exists():
            return None
        store = cls.__new__(cls)
        store._init_state(directory, mode, None, None)
        store.on_quarantine = None
        with store._lock:
            store._open_active(_BUSY_TIMEOUT_MS)
        if store._conn is None:
            logger.warning(f"Vector store at {directory} could not be opened: {store._reason}")
            store.close()
            return None
        return store

    def _init_state(self, directory: Path, mode: str, model: str | None, dims: int | None) -> None:
        self._dir = directory
        self._mode = mode
        self._model = model
        self._dims = dims
        self._lock = threading.RLock()
        self._local = threading.local()
        self._conn: sqlite3.Connection | None = None
        self._gen = active_generation(directory)
        self._state = STATE_UNAVAILABLE
        self._reason: str | None = "not opened"
        self._closed = False
        self._last_open_attempt = 0.0
        self._busy_until = 0.0
        self._confirm_cooldown_until = 0.0
        self._quarantine_events: list[dict[str, Any]] = []
        self._resync_pending = False
        self._failed_writes = 0
        self.on_resync_needed: Callable[[], None] | None = None
        self._reset_cache()

    def _reset_cache(self) -> None:
        self._dirty = True
        self._data_version: int | None = None
        self._ids: list[str] = []
        self._matrix: np.ndarray | None = None
        self._metas: list[str] = []
        self._docs: list[str | None] = []
        self._types: np.ndarray = np.empty(0, dtype=object)

    # -- connection management --------------------------------------------

    def _connect(self, generation: int, busy_ms: int) -> sqlite3.Connection:
        path = db_path(self._dir, generation)
        access = {MODE_HOME: "rwc", MODE_VIEWER: "rw", MODE_READONLY: "ro"}[self._mode]
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={access}", uri=True,
            timeout=busy_ms / 1000, isolation_level=None, check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            if self._mode != MODE_READONLY:
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute(f"PRAGMA journal_size_limit={_JOURNAL_SIZE_LIMIT}")
            self._ensure_schema(conn)
            if busy_ms != _BUSY_TIMEOUT_MS:
                conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        except BaseException:
            conn.close()
            raise
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise _NewerSchemaError(f"store schema {version} is newer than this version supports ({SCHEMA_VERSION})")
        if version == 0:
            if self._mode != MODE_HOME:
                raise VectorStoreCorruptionError("store has no schema")
            self._create_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"vectors", "store_meta"} <= tables:
            raise VectorStoreCorruptionError("store tables are missing")
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != STORE_FORMAT:
            raise _NewerSchemaError(f"store format {row[0] if row else 'unknown'!r} is not {STORE_FORMAT!r}")

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        with self._txn(conn):
            if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
                return
            foreign = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            if foreign:
                raise VectorStoreCorruptionError("database holds tables that are not a vector store")
            conn.execute(
                "CREATE TABLE vectors (id TEXT PRIMARY KEY, embedding BLOB NOT NULL, "
                "metadata TEXT NOT NULL, document TEXT, entity_type TEXT, node_id TEXT, "
                "is_chunk INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX vectors_entity_type ON vectors(entity_type)")
            conn.execute("CREATE INDEX vectors_node_id ON vectors(node_id)")
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            meta = {
                "format": STORE_FORMAT,
                "embed_scheme": EMBED_SCHEME,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if self._model is not None:
                meta["embedding_model"] = self._model
            if self._dims is not None:
                meta["embedding_dimensions"] = str(int(self._dims))
            conn.executemany("INSERT INTO store_meta(key, value) VALUES (?, ?)", list(meta.items()))
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextlib.contextmanager
    def _txn(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._rollback(conn)
            raise
        conn.execute("COMMIT")

    def _rollback(self, conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            if conn is self._conn:
                self._close_conn()

    def _open_active(self, busy_ms: int = _BUSY_TIMEOUT_MS) -> None:
        self._last_open_attempt = time.monotonic()
        if self._mode == MODE_HOME:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._set_unavailable(STATE_UNAVAILABLE, f"cannot create {self._dir}: {e}", 1)
                return
        for _ in range(_MAX_GENERATION_HOPS):
            generation = active_generation(self._dir)
            try:
                conn = self._connect(generation, busy_ms)
            except _NewerSchemaError as e:
                self._set_unavailable(STATE_NEWER_SCHEMA, str(e), generation)
                logger.warning(f"Vector store at {db_path(self._dir, generation)} left untouched: {e}")
                return
            except Exception as e:  # noqa: BLE001 - classified below, never raised
                reason = f"{type(e).__name__}: {e}"
                if is_corruption(e):
                    if self._quarantine(generation, str(e)):
                        continue
                    if self._confirm_cooldown_until == float("inf"):
                        reason = _REPEATED_DAMAGE
                self._set_unavailable(STATE_UNAVAILABLE, reason, generation)
                logger.warning(f"Vector store unavailable at {db_path(self._dir, generation)}: {e}")
                return
            self._conn = conn
            self._gen = generation
            self._state = STATE_OK
            self._reason = None
            self._reset_cache()
            return
        self._set_unavailable(STATE_UNAVAILABLE, _REPEATED_DAMAGE, active_generation(self._dir))

    def _set_unavailable(self, state: str, reason: str, generation: int) -> None:
        self._close_conn()
        self._state = state
        self._reason = reason
        self._gen = generation
        self._reset_cache()

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def _check_generation(self) -> None:
        if self._conn is not None and marker_path(self._dir, self._gen).exists():
            old = self._gen
            self._close_conn()
            self._open_active()
            if self._gen != old:
                logger.info(f"Vector store moved from generation {old} to {self._gen}")
                self._request_resync()

    def _ensure_open(self) -> bool:
        if self._closed:
            return False
        self._check_generation()
        if (
            self._conn is None
            and self._state != STATE_NEWER_SCHEMA
            and time.monotonic() - self._last_open_attempt >= _REOPEN_INTERVAL_S
        ):
            old = self._gen
            self._open_active()
            if self._conn is not None and (self._gen != old or self._resync_pending):
                self._resync_pending = False
                self._request_resync()
        return self._conn is not None

    def ensure_open(self) -> bool:
        """Finish an open that start-up deferred (busy). True when the store is usable."""
        with self._lock:
            if self._closed:
                return False
            if self._conn is None and self._state != STATE_NEWER_SCHEMA:
                self._last_open_attempt = 0.0
            return self._ensure_open()

    def _request_resync(self) -> None:
        callback = self.on_resync_needed
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()

    def can_write(self) -> bool:
        """False while the store is closed, unavailable or backing off after a busy write."""
        with self._lock:
            return (
                not self._closed
                and self._mode != MODE_READONLY
                and self._conn is not None
                and time.monotonic() >= self._busy_until
            )

    def request_resync(self) -> None:
        """Ask the owner's background maintenance loop for a re-sync now."""
        self._request_resync()

    def request_resync_on_recovery(self) -> None:
        """Ask for a re-sync as soon as a call next succeeds (a sync pass was cut short)."""
        with self._lock:
            self._resync_pending = True

    # -- quarantine -----------------------------------------------------------

    def _quarantines_last_day(self) -> int:
        cutoff = time.time() - 86400
        count = 0
        for info in quarantined_generations(self._dir):
            with contextlib.suppress(OSError):
                if marker_path(self._dir, info["generation"]).stat().st_mtime >= cutoff:
                    count += 1
        return count

    def _confirm_damage(self, generation: int) -> str | None:
        """A second, independent verdict on a fresh connection. None means not confirmed."""
        path = db_path(self._dir, generation)
        try:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None,
            )
        except sqlite3.Error as e:
            return str(e) if is_corruption(e) else None
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                return None
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if version == 0:
                return "database holds tables that are not a vector store" if tables else None
            if not {"vectors", "store_meta"} <= tables:
                return "store tables are missing"
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'format'").fetchone()
            if row is None or row[0] != STORE_FORMAT:
                return None
            check = conn.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                return f"quick_check: {check[0][0] if check else 'no result'}"
            _validate_rows(conn)
            return None
        except Exception as e:  # noqa: BLE001 - only corruption confirms
            return str(e) if is_corruption(e) else None
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def _quarantine(self, generation: int, reason: str) -> bool:
        """Mark ``generation`` corrupt once the damage is confirmed. True when it is marked."""
        if self._mode != MODE_HOME:
            return False
        if marker_path(self._dir, generation).exists():
            return True
        if time.monotonic() < self._confirm_cooldown_until:
            return False
        confirmed = self._confirm_damage(generation)
        if confirmed is None:
            logger.warning(f"Vector store error not confirmed as corruption, not rebuilding: {reason}")
            self._confirm_cooldown_until = time.monotonic() + _CONFIRM_COOLDOWN_S
            return False
        if (
            len(self._quarantine_events) >= _MAX_QUARANTINES_PER_PROCESS
            or self._quarantines_last_day() >= _MAX_QUARANTINES_PER_DAY
        ):
            logger.error(f"Vector store damaged again ({confirmed}); {_REPEATED_DAMAGE}")
            self._confirm_cooldown_until = float("inf")
            return False
        info = {
            "generation": generation,
            "at": datetime.now(UTC).isoformat(),
            "reason": f"{reason} (confirmed: {confirmed})",
            "pid": os.getpid(),
        }
        if self._conn is not None and generation == self._gen:
            self._close_conn()
        marker = marker_path(self._dir, generation)
        temp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(info), encoding="utf-8")
            os.replace(temp, marker)
        except OSError as e:
            with contextlib.suppress(OSError):
                temp.unlink()
            logger.error(f"Could not mark vector store generation {generation} corrupt: {e}")
            self._confirm_cooldown_until = time.monotonic() + _CONFIRM_COOLDOWN_S
            return False
        logger.error(
            f"Vector store generation {generation} is damaged ({info['reason']}); "
            f"set aside and rebuilding from the journal"
        )
        self._quarantine_events.append(info)
        callback = self.on_quarantine
        if callback is not None:
            with contextlib.suppress(Exception):
                callback(info)
        return True

    # -- guarded execution ----------------------------------------------------

    def _fail(self, label: str, reason: str) -> None:
        self._local.error = reason
        logger.error(f"Vector store {label} failed: {reason}")

    def _run(
        self, op: Callable[[sqlite3.Connection], Any], default: Any, label: str, write: bool = False
    ) -> Any:
        self._local.error = None
        with self._lock:
            if write and time.monotonic() < self._busy_until:
                return self._degraded(label, "vector store is busy", default, write, quiet=True)
            if not self._ensure_open():
                return self._degraded(label, self._reason or self._state, default, write, quiet=True)
            for attempt in range(2):
                conn = self._conn
                assert conn is not None
                try:
                    result = op(conn)
                except Exception as e:  # noqa: BLE001 - a cache must never fail its caller
                    self._rollback(conn)
                    if is_corruption(e):
                        old = self._gen
                        if self._quarantine(self._gen, str(e)):
                            self._open_active()
                            if self._gen != old:
                                self._request_resync()
                            if self._conn is not None and attempt == 0:
                                continue
                        return self._degraded(label, f"store damaged: {e}", default, write)
                    if isinstance(e, _DimensionMismatchError):
                        self._fail(label, f"{DIMENSION_MISMATCH}: {e}")
                        return default
                    if _is_busy(e):
                        if write:
                            self._busy_until = time.monotonic() + _BUSY_BREAKER_S
                        return self._degraded(label, f"store busy: {e}", default, write)
                    if attempt == 0 and isinstance(e, sqlite3.OperationalError) and self._ensure_open():
                        continue
                    return self._degraded(label, f"{type(e).__name__}: {e}", default, write)
                if self._resync_pending and time.monotonic() >= self._busy_until:
                    self._resync_pending = False
                    self._request_resync()
                return result
            return default

    def _degraded(self, label: str, reason: str, default: Any, write: bool, quiet: bool = False) -> Any:
        if write:
            self._failed_writes += 1
            self._resync_pending = True
        if quiet:
            self._local.error = reason
        else:
            self._fail(label, reason)
        return default

    def last_error(self) -> str | None:
        """Why this thread's most recent call degraded, or None if it succeeded."""
        return getattr(self._local, "error", None)

    # -- cache ----------------------------------------------------------------

    def _stored_dim(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'dim'").fetchone()
        return int(row[0]) if row else None

    def _refresh_cache(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        if not self._dirty and version == self._data_version and self._matrix is not None:
            return
        rows = conn.execute(
            "SELECT id, embedding, metadata, document, entity_type FROM vectors ORDER BY rowid"
        ).fetchall()
        matrix = _rows_to_matrix(rows, self._stored_dim(conn))
        self._ids = [r[0] for r in rows]
        self._matrix = matrix
        self._metas = [r[2] for r in rows]
        self._docs = [r[3] for r in rows]
        self._types = np.array([r[4] for r in rows], dtype=object)
        self._data_version = version
        self._dirty = False
        if len(rows) > _SCALE_WARN:
            logger.warning(f"Vector store holds {len(rows)} vectors; exact search slows past {_SCALE_WARN}")

    # -- writes ---------------------------------------------------------------

    def _write_rows(self, conn: sqlite3.Connection, rows: list[tuple], dim: int) -> None:
        stored = self._stored_dim(conn)
        if stored is None:
            conn.execute("INSERT INTO store_meta(key, value) VALUES ('dim', ?)", (str(dim),))
        elif stored != dim:
            raise _DimensionMismatchError(f"vectors of dimension {dim} rejected: the store holds dimension {stored}")
        conn.executemany(_UPSERT_SQL, rows)

    def upsert_embedding(
        self,
        entity_id: str,
        embedding: list[float],
        metadata: dict[str, Any],
        document: str | None = None,
    ) -> bool:
        """Insert or replace one vector. Never raises: a failed write is logged, counted,
        and filled from the journal by a re-sync once the store works again. True when
        the vector was stored."""
        if self._mode == MODE_READONLY:
            return False
        vector = _prepare_vector(entity_id, embedding)
        if vector is None:
            return False
        row = _row(entity_id, vector, metadata, document)

        def op(conn: sqlite3.Connection) -> bool:
            self._dirty = True
            with self._txn(conn):
                self._write_rows(conn, [row], vector.size)
            return True

        return bool(self._run(op, False, "write", write=True))

    def replace_chunks(
        self, node_id: str, chunks: list[tuple[str, list[float], dict[str, Any], str | None]]
    ) -> bool:
        """Replace every chunk vector of ``node_id`` with ``chunks`` in one transaction.
        Each chunk is ``(chunk_id, embedding, metadata, document)``. Never raises; True
        when the set was stored."""
        if self._mode == MODE_READONLY:
            return False
        rows: list[tuple] = []
        dims: set[int] = set()
        for chunk_id, embedding, metadata, document in chunks:
            vector = _prepare_vector(chunk_id, embedding)
            if vector is None:
                continue
            dims.add(vector.size)
            rows.append(_row(chunk_id, vector, metadata, document))
        if len(dims) > 1:
            logger.error(f"Chunks of {node_id!r} have mixed dimensions {sorted(dims)}; not stored")
            return False
        dim = next(iter(dims)) if dims else 0

        def op(conn: sqlite3.Connection) -> bool:
            self._dirty = True
            with self._txn(conn):
                conn.execute("DELETE FROM vectors WHERE node_id = ?", (node_id,))
                if rows:
                    self._write_rows(conn, rows, dim)
            return True

        return bool(self._run(op, False, "chunk write", write=True))

    def _delete(self, sql: str, params: tuple, label: str) -> bool:
        if self._mode == MODE_READONLY:
            return False

        def op(conn: sqlite3.Connection) -> bool:
            self._dirty = True
            with self._txn(conn):
                conn.execute(sql, params)
            return True

        return bool(self._run(op, False, label, write=True))

    def delete_embedding(self, entity_id: str) -> bool:
        """True when the delete ran (whether or not the id existed); False on error."""
        return self._delete("DELETE FROM vectors WHERE id = ?", (entity_id,), "delete")

    def delete_by_node_id(self, node_id: str) -> None:
        """Delete every chunk vector tagged with ``node_id``."""
        self._delete("DELETE FROM vectors WHERE node_id = ?", (node_id,), "chunk delete")

    def delete_ids(self, ids: list[str]) -> None:
        for batch in _batched(list(ids)):
            self._delete(f"DELETE FROM vectors WHERE id IN ({','.join('?' * len(batch))})", tuple(batch), "delete")

    # -- reads ----------------------------------------------------------------

    def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        entity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Exact cosine search. Hits are ``{"_id", **metadata, "score", ["matched_text"]}``,
        best first. ``[]`` with a logged error when the query or the store is unusable;
        ``last_error()`` then says why."""
        self._local.error = None
        try:
            query = np.asarray(query_embedding, dtype=np.float32)
        except (TypeError, ValueError) as e:
            self._fail("search", f"query is not numeric: {e}")
            return []
        norm = float(np.linalg.norm(query)) if query.ndim == 1 and query.size else 0.0
        if not np.isfinite(norm) or norm == 0.0:
            self._fail("search", "query is empty, zero or not finite")
            return []
        query = query / norm
        if limit <= 0:
            return []

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            self._refresh_cache(conn)
            matrix = self._matrix
            if matrix is None or not self._ids:
                return []
            if query.size != matrix.shape[1]:
                self._fail("search", f"query dimension {query.size}, store holds {matrix.shape[1]}")
                return []
            if entity_type:
                pool = np.nonzero(self._types == entity_type)[0]
                if not pool.size:
                    return []
                scores = matrix[pool] @ query
            else:
                pool = np.arange(len(self._ids))
                scores = matrix @ query
            k = min(limit, pool.size)
            top = np.arange(pool.size) if k == pool.size else np.sort(np.argpartition(-scores, k - 1)[:k])
            order = top[np.argsort(-scores[top], kind="stable")]
            hits: list[dict[str, Any]] = []
            for j in order:
                i = int(pool[j])
                meta = _parse_meta(self._ids[i], self._metas[i])
                hit: dict[str, Any] = {"_id": self._ids[i], **meta, "score": float(scores[j])}
                if self._docs[i] is not None:
                    hit["matched_text"] = self._docs[i]
                hits.append(hit)
            return hits

        return self._run(op, [], "search")

    def count_documents(self, filter: dict[str, Any] | None = None) -> int:
        """Number of vectors, optionally matching an equality ``filter``."""
        _check_filter(filter)

        def op(conn: sqlite3.Connection) -> int:
            if not filter:
                return int(conn.execute("SELECT count(*) FROM vectors").fetchone()[0])
            return len(self._select_ids(conn, filter))

        return int(self._run(op, 0, "count"))

    def _select_ids(self, conn: sqlite3.Connection, filter: dict[str, Any]) -> list[str]:
        clauses: list[str] = []
        params: list[Any] = []
        extra: dict[str, Any] = {}
        for key, value in filter.items():
            if key == "is_chunk":
                clauses.append("is_chunk = 1" if value is True else "is_chunk != 1")
            elif key in _COLUMN_FILTERS:
                clauses.append(f"{key} = ?")
                params.append(value)
            else:
                extra[key] = value
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT id, metadata FROM vectors{where}", params).fetchall()
        if not extra:
            return [r[0] for r in rows]
        matched = []
        for row_id, raw in rows:
            meta = _parse_meta(row_id, raw)
            if all(k in meta and meta[k] == v and type(meta[k]) is type(v) for k, v in extra.items()):
                matched.append(row_id)
        return matched

    def get_existing(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Metadata of the given ids that are present."""

        def op(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            found: dict[str, dict[str, Any]] = {}
            for batch in _batched(list(ids)):
                marks = ",".join("?" * len(batch))
                rows = conn.execute(f"SELECT id, metadata FROM vectors WHERE id IN ({marks})", batch).fetchall()
                for row_id, raw in rows:
                    found[row_id] = _parse_meta(row_id, raw)
            return found

        return self._run(op, {}, "read")

    def existing_ids(self, ids: list[str]) -> set[str]:
        def op(conn: sqlite3.Connection) -> set[str]:
            found: set[str] = set()
            for batch in _batched(list(ids)):
                marks = ",".join("?" * len(batch))
                found.update(
                    r[0] for r in conn.execute(f"SELECT id FROM vectors WHERE id IN ({marks})", batch).fetchall()
                )
            return found

        return self._run(op, set(), "read")

    def all_ids(self) -> list[str]:
        return self._run(lambda conn: [r[0] for r in conn.execute("SELECT id FROM vectors").fetchall()], [], "read")

    def store_metadata(self) -> dict[str, Any]:
        """What the store was stamped with at creation (``embedding_dimensions`` as int)."""

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            meta: dict[str, Any] = dict(conn.execute("SELECT key, value FROM store_meta").fetchall())
            if "embedding_dimensions" in meta:
                meta["embedding_dimensions"] = int(meta["embedding_dimensions"])
            meta.pop("dim", None)
            return meta

        return self._run(op, {}, "read")

    def sample_dim(self) -> int | None:
        """The dimension of the stored vectors, or None for an empty store."""
        return self._run(self._stored_dim, None, "read")

    def health_check(self) -> bool:
        """Full validation (``quick_check`` plus every row). Confirmed damage is
        quarantined. True when the store is usable afterwards."""

        def op(conn: sqlite3.Connection) -> bool:
            check = conn.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise VectorStoreCorruptionError(f"quick_check: {check[0][0] if check else 'no result'}")
            self._dirty = True
            self._refresh_cache(conn)
            for i, row_id in enumerate(self._ids):
                _parse_meta(row_id, self._metas[i])
            return True

        return bool(self._run(op, False, "health check")) and self._conn is not None

    # -- status ---------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            if not self._closed:
                self._check_generation()
            return self._state

    @property
    def available(self) -> bool:
        return self.state == STATE_OK

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def path(self) -> Path:
        return db_path(self._dir, self._gen)

    @property
    def generation(self) -> int:
        return self._gen

    @property
    def quarantine_events(self) -> list[dict[str, Any]]:
        return list(self._quarantine_events)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self._closed:
                self._check_generation()
            return {
                "state": self._state,
                "generation": self._gen,
                "path": str(db_path(self._dir, self._gen)),
                "reason": self._reason,
                "quarantined": quarantined_generations(self._dir),
                "failed_writes": self._failed_writes,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_conn()
            self._reset_cache()


def _parse_meta(row_id: str, raw: str) -> dict[str, Any]:
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise VectorStoreCorruptionError(f"metadata for {row_id!r} is unreadable: {e}") from e
    if not isinstance(meta, dict):
        raise VectorStoreCorruptionError(f"metadata for {row_id!r} is not an object")
    return meta


def _rows_to_matrix(rows: list[tuple], dim: int | None) -> np.ndarray | None:
    if not rows:
        return None
    if dim is None:
        raise VectorStoreCorruptionError("vectors are stored but the store has no dimension")
    width = dim * 4
    for row in rows:
        blob = row[1]
        if not isinstance(blob, bytes) or len(blob) != width:
            size = len(blob) if isinstance(blob, bytes) else type(blob).__name__
            raise VectorStoreCorruptionError(f"vector {row[0]!r} has {size} bytes, expected {width}")
    matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype="<f4").reshape(len(rows), dim)
    finite = np.isfinite(matrix).all(axis=1)
    if not finite.all():
        bad = rows[int(np.argmin(finite))][0]
        raise VectorStoreCorruptionError(f"vector {bad!r} holds a non-finite value")
    return matrix


def _validate_rows(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM store_meta WHERE key = 'dim'").fetchone()
    dim = int(row[0]) if row else None
    rows = conn.execute("SELECT id, embedding, metadata FROM vectors").fetchall()
    _rows_to_matrix(rows, dim)
    for row_id, _blob, raw in rows:
        _parse_meta(row_id, raw)


_SEARCH_OVERQUERY_K = 5
_SEARCH_OVERQUERY_CAP = 500


def adaptive_vector_search(
    embedding_storage: Any,
    query_embedding: list[float],
    *,
    entity_type: str | None = None,
    limit: int,
    dedupe: Any,
) -> dict[str, Any]:
    """Widen n_results (doubling) until `limit` distinct deduped results, the store
    is exhausted, or the cap.

    `dedupe(results, limit) -> (list, excluded_count)` owns N1-drop + chunk-dedupe
    (+ any per-surface exclusion filter) per surface (MCP and dashboard use
    different result shapes). Unlike the pre-WP-TC10 contract, `dedupe`'s list is
    NOT capped to `limit` — it returns every live deduped (post-filter) hit for
    that round, so the caller can report the terminating round's full count
    (`total_found`, WP-TC10 M4) instead of a value truncated by dedupe itself.

    Returns {"results": at most `limit` items, "total_found": len(dedupe's list)
    for the terminating round (never accumulated across widening rounds — each
    round's dedupe call recomputes from scratch over that round's raw hits),
    "exhaustive": True iff the store returned fewer raw hits than requested that
    round (M is exact; False means M is a floor — stopped at limit or the cap),
    "excluded_count": likewise the terminating round's count, 0 when the caller's
    dedupe never excludes}."""
    n = max(limit * _SEARCH_OVERQUERY_K, limit, 1)
    while True:
        results = embedding_storage.vector_search(
            query_embedding=query_embedding, limit=n, entity_type=entity_type
        )
        formatted, excluded_count = dedupe(results, limit)
        exhaustive = len(results) < n
        if len(formatted) >= limit or exhaustive or n >= _SEARCH_OVERQUERY_CAP:
            return {
                "results": formatted[:limit],
                "total_found": len(formatted),
                "exhaustive": exhaustive,
                "excluded_count": excluded_count,
            }
        n = min(n * 2, _SEARCH_OVERQUERY_CAP)
