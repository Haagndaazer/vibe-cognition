"""WP-TC6 BACK-COMPAT regression: our startup embedding sync must tolerate a node
type it doesn't recognize (a client running a NEWER version than this server wrote
it). Peer review traced the exact defect in an OLDER version's sync: the first
person node raised inside its embed loop (unknown-to-that-version enum member),
the outer handler logged "Background initialization failed", and every node behind
it in iteration order silently never embedded, recurring on every restart until
upgrade. This WP's own sync must not inherit that shape -- one bad node is skipped
and logged; the rest of the batch still embeds.

Fails-before (pre-fix): the non_doc_missing loop in server.py's
_sync_cognition_embeddings had no per-node try/except, so a node whose type raises
inside _node_from_dict's CognitionNodeType(...) construction would abort the whole
batch, leaving every node after it in iteration order un-embedded.
"""

from __future__ import annotations

import json

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.server import _sync_cognition_embeddings


class _Spy:
    """Fixed-vector fake embedder (mirrors tests/test_reembed_on_replay.py's)."""

    DIM = 3

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        self.calls.append(text)
        return [0.1] * self.DIM


def _make_chroma(tmp_path) -> ChromaDBStorage:
    return ChromaDBStorage(
        persist_directory=tmp_path / "chroma",
        embedding_model="m",
        embedding_dimensions=_Spy.DIM,
    )


def test_sync_skips_unknown_type_node_without_aborting_the_batch(tmp_path):
    """A node whose 'type' this process doesn't recognize (simulating a future
    client's node type) must be skipped, not crash the sync -- and every OTHER
    node in the batch must still get embedded regardless of iteration order."""
    storage = CognitionStorage(tmp_path / ".cognition")
    chroma = _make_chroma(tmp_path)
    spy = _Spy()

    # A real, recognized node -- placed BEFORE the unknown one in add order so a
    # naive first-bad-node-aborts-the-loop defect would still let this one through
    # (the regression is specifically about nodes AFTER the bad one).
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    storage.add_node(CognitionNode(
        id="before1", type=CognitionNodeType.DECISION, summary="s1", detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author="a",
    ))

    # Inject a node with a type string this version's enum doesn't have, via a RAW
    # journal line (not storage.graph.add_node directly -- a direct graph mutation
    # gets silently WIPED by the next legitimate add_node's catch-up, which detects
    # the in-memory graph as ahead of the journal and re-hydrates from disk). This
    # is the exact shape journal replay produces for a node written by a newer
    # client: storage.py stores/replays 'type' as a raw string, no enum validation.
    journal_path = storage.cognition_dir / "journal.jsonl"
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "action": "add_node",
            "data": {
                "id": "unknown1", "type": "a_future_node_type_this_version_predates",
                "summary": "s2", "detail": "d", "context": [], "references": [],
                "severity": None, "timestamp": "2026-01-01T00:00:01+00:00", "author": "a",
                "metadata": {},
            },
        }) + "\n")

    # A second real node placed AFTER the unknown one -- this is the one an
    # un-guarded loop would silently never reach. Adding it (a real, journaled
    # write) also triggers the catch-up that replays the raw line written above.
    storage.add_node(CognitionNode(
        id="after1", type=CognitionNodeType.PATTERN, summary="s3", detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:02+00:00", author="a",
    ))
    assert {n["id"] for n in storage.get_all_nodes()} == {"before1", "unknown1", "after1"}

    # Must not raise.
    result = _sync_cognition_embeddings(storage, chroma, spy)  # type: ignore[arg-type]

    embedded_ids = set(chroma._collection.get()["ids"])
    assert "before1" in embedded_ids
    assert "after1" in embedded_ids, (
        "a node AFTER the unknown-type node in iteration order was never embedded -- "
        "the exact old-version defect this regression test guards against"
    )
    assert "unknown1" not in embedded_ids
    # The sync's own count only reflects what it attempted, not what raised --
    # the important invariant is that the OTHER nodes above landed in Chroma.
    assert result["nodes"] >= 2
