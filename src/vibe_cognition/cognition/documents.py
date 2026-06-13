"""Document storage helpers (WP-D1).

Content-addressed paths, text sidecar, and sha256. Reference mode + the text
sidecar land in D1a; the opt-in content-addressed BLOB (and the extension
sanitization that guards the blob filename — the only agent-controlled path
component) land in D1b, since D1a composes no path from agent input (the sidecar
name is the server-generated sha). Stdlib-only — the agent extracts document
text, so the server never parses binaries (zero new deps).
"""

import hashlib
from pathlib import Path

_DOC_REF_PREFIX = "doc:"
_DOC_REF_SHA_LEN = 12
_READ_CHUNK = 1 << 20  # 1 MiB


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
