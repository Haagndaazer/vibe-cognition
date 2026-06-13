"""WP-D2: deterministic word-window chunking."""

from vibe_cognition.cognition.chunking import chunk_text


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == [], "whitespace-only should yield no chunks"


def test_short_text_is_one_chunk():
    assert chunk_text("a b c", window=10, overlap=2) == ["a b c"]
    # Exactly window-sized stays a single chunk.
    words = " ".join(str(i) for i in range(10))
    assert chunk_text(words, window=10, overlap=2) == [words]


def test_exact_chunk_count_and_boundaries():
    """25 words, window=10, overlap=2 -> step 8 -> chunks at [0:10], [8:18], [16:25]."""
    words = [str(i) for i in range(25)]
    chunks = chunk_text(" ".join(words), window=10, overlap=2)
    assert len(chunks) == 3, f"expected 3 chunks, got {len(chunks)}"
    assert chunks[0] == " ".join(words[0:10])
    assert chunks[1] == " ".join(words[8:18])
    assert chunks[2] == " ".join(words[16:25]), "last chunk should run to the end, not pad"


def test_adjacent_chunks_overlap():
    words = [str(i) for i in range(25)]
    chunks = chunk_text(" ".join(words), window=10, overlap=2)
    # The last 2 words of chunk 0 are the first 2 of chunk 1.
    assert chunks[0].split()[-2:] == chunks[1].split()[:2], "overlap words not shared"


def test_every_word_covered():
    words = [str(i) for i in range(57)]
    chunks = chunk_text(" ".join(words), window=10, overlap=3)
    seen = {w for c in chunks for w in c.split()}
    assert seen == set(words), "some words were dropped between windows"


def test_deterministic():
    words = " ".join(str(i) for i in range(200))
    assert chunk_text(words, window=30, overlap=5) == chunk_text(words, window=30, overlap=5)


def test_overlap_clamped_when_ge_window():
    """overlap >= window would make step <= 0 (infinite loop); it is clamped."""
    words = " ".join(str(i) for i in range(50))
    chunks = chunk_text(words, window=10, overlap=10)
    assert len(chunks) >= 1 and all(c for c in chunks), "clamp failed (empty or hang)"
