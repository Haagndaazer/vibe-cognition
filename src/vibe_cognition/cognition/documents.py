"""Document storage helpers (WP-D1).

Content-addressed paths, text sidecar, sha256, and (D1b) the opt-in content-
addressed BLOB store + the extension sanitization that guards the blob filename
(the only agent-controlled path component — D1a composed no path from agent input,
the sidecar name is the server-generated sha). Stdlib-only — the agent extracts
document text, so the server never parses binaries (zero new deps).
"""

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import CognitionStorage  # avoid the real circular import at runtime

_DOC_REF_PREFIX = "doc:"
_DOC_REF_SHA_LEN = 12
_READ_CHUNK = 1 << 20  # 1 MiB
_GITIGNORE_SELF = ".gitignore"  # the documents/.gitignore ignores itself (per-machine)
_MIB = 1 << 20
# Size policy for committed copy-mode blobs (§9 S1). No HARD cap (§8(b)) — these
# only flip git policy: ≥ WARN auto-local_only + warn; ≥ REFUSE default-commit
# refused (forced local_only) so a huge blob can't brick every later push of main.
BLOB_WARN_BYTES = 50 * _MIB
BLOB_REFUSE_BYTES = 95 * _MIB
# Whitelist-or-DROP: a leading-dot alnum run, <=10 chars. The ONLY agent-controlled
# path component; anything else (traversal, separators, reserved, over-long) drops to "".
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def documents_dir(cognition_dir: Path) -> Path:
    return cognition_dir / "documents"


def text_sidecar_path(cognition_dir: Path, sha: str) -> Path:
    return documents_dir(cognition_dir) / "text" / f"{sha}.txt"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Stream the file so a large referenced document isn't read into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_ref(sha: str) -> str:
    """The canonical reference key for a document: ``doc:<sha256[:12]>``."""
    return f"{_DOC_REF_PREFIX}{sha[:_DOC_REF_SHA_LEN]}"


def write_text_sidecar(cognition_dir: Path, sha: str, text: str) -> int:
    """Write the agent-extracted text sidecar; return the char count stored."""
    path = text_sidecar_path(cognition_dir, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text)


def read_text_sidecar(cognition_dir: Path, sha: str) -> str | None:
    try:
        return text_sidecar_path(cognition_dir, sha).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def remove_text_sidecar(cognition_dir: Path, sha: str) -> bool:
    """Unlink the sidecar; return True if it existed. (Reference-mode deletion
    touches ONLY managed artifacts — never the referenced original file.)"""
    try:
        text_sidecar_path(cognition_dir, sha).unlink()
        return True
    except FileNotFoundError:
        return False


# ── Opt-in content-addressed BLOB store (D1b, copy mode) ──────────────────────

def sanitize_extension(ext: str) -> str:
    """Whitelist-or-DROP the agent-supplied extension. Keep only a leading-dot
    alnum run (<=10 chars); anything else (path traversal, separators, reserved
    names, over-long) drops to "" — NEVER fail the store on a hostile/odd ext.
    This is the sole agent-controlled component of the blob path."""
    return ext if _EXT_RE.match(ext) else ""


def blob_rel_path(sha: str, ext: str) -> str:
    """The blob's path RELATIVE to the documents dir: ``<sha[:2]>/<sha><ext>``.
    This exact string is the key for both the blob file and its .gitignore line."""
    return f"{sha[:2]}/{sha}{sanitize_extension(ext)}"


def blob_path(cognition_dir: Path, sha: str, ext: str) -> Path:
    return documents_dir(cognition_dir) / blob_rel_path(sha, ext)


def write_blob(
    cognition_dir: Path, sha: str, ext: str, *,
    data: bytes | None = None, src_path: Path | None = None,
) -> Path:
    """Write the content-addressed blob, write-once + atomic. Returns its path.

    Write-once: if the blob already exists, skip (content-addressed → identical
    bytes by construction; dedup/integrity-free). Atomic: write to a temp file IN
    THE SAME DIRECTORY (same filesystem, or os.replace can fail cross-device) then
    os.replace onto the final name, so a crash never leaves a half-written blob
    under the sha name. Two concurrent writers of identical bytes both replacing is
    harmless. The exists-check TOCTOU is the accepted non-transactional model.
    Streams from ``src_path`` (uncapped files never fully read into memory) or
    writes ``data`` bytes. Binary mode throughout (WP-4 Windows TEXT-mode lesson)."""
    path = blob_path(cognition_dir, sha, ext)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if data is not None:
                f.write(data)
            elif src_path is not None:
                with open(src_path, "rb") as src:
                    shutil.copyfileobj(src, f, _READ_CHUNK)
            else:
                raise ValueError("write_blob needs data or src_path")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path


def remove_blob_rel(cognition_dir: Path, rel_path: str) -> bool:
    """Unlink a blob by its relative path; True if it existed. Managed-artifact
    only — never the referenced original file."""
    try:
        (documents_dir(cognition_dir) / rel_path).unlink()
        return True
    except FileNotFoundError:
        return False


# ── Local, self-ignoring documents/.gitignore (per-machine local_only set) ────

def documents_gitignore_path(cognition_dir: Path) -> Path:
    return documents_dir(cognition_dir) / _GITIGNORE_SELF


def _gitignore_lines(cognition_dir: Path) -> list[str]:
    try:
        return documents_gitignore_path(cognition_dir).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def gitignore_has_entry(cognition_dir: Path, rel_path: str) -> bool:
    return rel_path in _gitignore_lines(cognition_dir)


def add_gitignore_entry(cognition_dir: Path, rel_path: str) -> None:
    """Idempotently add ``rel_path`` to the LOCAL ``documents/.gitignore`` (which
    also ignores itself, so the local_only set + S3 promote/demote stay per-machine
    — a committed shared list would force a teammate's default store of the same sha
    to fight machine A's local_only line). One line per blob, never per-shard."""
    lines = _gitignore_lines(cognition_dir)
    additions = [e for e in (_GITIGNORE_SELF, rel_path) if e not in lines]
    if not additions:
        return
    path = documents_gitignore_path(cognition_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines + additions) + "\n", encoding="utf-8")


def remove_gitignore_entry(cognition_dir: Path, rel_path: str) -> bool:
    """Remove ``rel_path``'s line (keeps the self-ignoring ``.gitignore`` line).
    True if the entry existed."""
    lines = _gitignore_lines(cognition_dir)
    if rel_path not in lines:
        return False
    kept = [ln for ln in lines if ln != rel_path]
    documents_gitignore_path(cognition_dir).write_text(
        ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
    )
    return True


# ── Orphaned artifact discovery (WP-12, d999b4e3851a) ─────────────────────────

_SHA256_HEX_LEN = 64  # len(hashlib.sha256(...).hexdigest())


def find_orphaned_document_artifacts(cognition_dir: Path, storage: "CognitionStorage") -> list[str]:
    """Find sidecar/blob files under ``documents/`` that no DOCUMENT node references.

    ``_store_document`` writes the text sidecar (and, in copy mode, the blob)
    BEFORE minting/journaling the node — its metadata (``indexed_text_chars``,
    ``blob_path``, ``local_only``) is only known once those writes complete, so
    journaling the node first would need a larger two-phase (pending-then-update)
    restructure. A crash between an artifact write and the node mint leaves an
    ownerless file with no reclaim path today — ``delete_cognition_node``'s
    reclaim only walks DOWN from an existing node, it never scans ``documents/``
    for files no node claims.

    Discovery only — never deletes. Callers decide whether/how to act on the
    result; reclaiming here would mean unlinking data with no way to first rule
    out "another process is mid-write" the way the (regenerable) ChromaDB orphan
    sweep can. Best-effort: any filesystem error scanning a subdirectory yields
    "no orphans found there" rather than raising — this must never block startup.

    Returns relative paths (from ``cognition_dir``), sorted, empty if none.
    """
    orphans: list[str] = []
    doc_dir = documents_dir(cognition_dir)

    # Sidecars: documents/text/<sha>.txt
    try:
        sidecar_files = list((doc_dir / "text").glob("*.txt"))
    except OSError:
        sidecar_files = []
    for f in sidecar_files:
        sha = f.stem
        if len(sha) == _SHA256_HEX_LEN and not storage.documents_with_sha(sha):
            orphans.append(str(f.relative_to(cognition_dir)))

    # Blobs: documents/<sha[:2]>/<sha><ext>
    try:
        shard_dirs = [d for d in doc_dir.iterdir() if d.is_dir() and d.name != "text"]
    except OSError:
        shard_dirs = []
    for shard in shard_dirs:
        try:
            shard_files = [f for f in shard.iterdir() if f.is_file()]
        except OSError:
            continue
        for f in shard_files:
            if len(f.name) < _SHA256_HEX_LEN:
                continue  # not a content-addressed blob filename; ignore
            sha = f.name[:_SHA256_HEX_LEN]
            if not storage.documents_with_sha(sha):
                orphans.append(str(f.relative_to(cognition_dir)))

    return sorted(orphans)


# ── Cheap staleness signal (WP-12, db65f1568fa5) ──────────────────────────────


def freshness_by_rehash(metadata: dict) -> str:
    """Full re-hash freshness check for a document's referenced source path
    (WP-DashV2, relocated from cognition_tools._get_document — single-
    implementation doctrine). Unlike cheap_staleness_signal (path-existence
    + size only, O(1)), this reads the ENTIRE referenced file, so it is the
    only check that can detect a same-size content edit; cost scales with
    the file's size, which is why cheap_staleness_signal exists for the
    search hot path and this stays reserved for get_document / dashboard use.

    No path key -> "unchanged" (the historical default: this reflects "no
    check was possible", not a verified match — callers that need to
    distinguish "no path to check" from "checked and clean" must inspect
    metadata's path/mode themselves BEFORE calling this, e.g. the dashboard's
    documents table, which renders null rather than repeating this
    unchanged-by-default implication for mode="copy" or a pathless reference
    doc). Path present but missing/unreadable -> "missing". Present and
    readable -> "unchanged" or "modified" by sha256 comparison against
    metadata["sha256"]. Never raises.
    """
    sha = metadata.get("sha256", "")
    path = metadata.get("path")
    if not path:
        return "unchanged"
    fp = Path(path)
    if not fp.is_file():
        return "missing"
    try:
        return "unchanged" if sha256_file(fp) == sha else "modified"
    except OSError:
        return "missing"


def cheap_staleness_signal(metadata: dict) -> str | None:
    """Stat-only (no file read) staleness check for a document's referenced
    source path — for surfacing in cognition_search results, where re-hashing
    every document hit (cognition_get_document's full ``freshness`` check,
    which reads the ENTIRE file) would make search cost scale with how many
    documents it happened to match.

    This is intentionally a WEAKER guarantee than get_document's freshness:
    it can only ever detect the path being GONE or a SIZE difference (both
    O(1) syscalls, no read) — a same-size content edit is invisible to it.
    Returns "path_missing", "size_changed", or None (either no path to check,
    or the cheap check found nothing — NOT a confirmation of freshness; use
    cognition_get_document's freshness field for that).
    """
    path = metadata.get("path")
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return "path_missing"
        if p.stat().st_size != metadata.get("size"):
            return "size_changed"
    except OSError:
        return "path_missing"
    return None
