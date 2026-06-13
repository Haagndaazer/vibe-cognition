"""Deterministic, dependency-free text chunking for document search (WP-D2).

Chunks the agent-extracted sidecar text into overlapping windows for per-chunk
ChromaDB embedding (``<node_id>#chunk-N``). The window is measured in WORDS, a
deliberate approximation of DESIGN §3's "~1000-token windows / 100 overlap": the
agent already bears the real token cost (§2), so the server stays free of a second
tokenizer dependency and of coupling chunk boundaries to the embedding backend.
Pure + deterministic — the same text always yields the same chunk list, so re-sync
is idempotent by id. (Shrinking chunk counts are handled by delete-then-write in
the write paths, not here.)
"""

_DEFAULT_WINDOW = 1000  # words per chunk (~1000 tokens, approximate)
_DEFAULT_OVERLAP = 100   # words shared between adjacent chunks


def chunk_text(
    text: str, *, window: int = _DEFAULT_WINDOW, overlap: int = _DEFAULT_OVERLAP
) -> list[str]:
    """Split ``text`` into overlapping word-windows.

    Returns a list of chunk strings (every word lands in >=1 chunk; adjacent chunks
    share ``overlap`` words). Empty/whitespace-only text -> ``[]``; text within one
    window -> a single chunk. Deterministic: same input -> same output.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= window:
        return [" ".join(words)]

    overlap = max(0, min(overlap, window - 1))  # keep step >= 1
    step = window - overlap

    chunks: list[str] = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + window]))
        if i + window >= len(words):
            break
        i += step
    return chunks
