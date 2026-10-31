"""Record ChromaDBStorage outputs as the golden parity fixture for VectorStore.

Recorded once against the v0.46.0 code (commit 286b840), whose
src/vibe_cognition/embeddings/storage.py held ChromaDBStorage. To re-record, check
that commit out with chromadb installed and run:
    uv run python tests/fixtures/record_vector_store_golden.py
It writes tests/fixtures/vector_store_golden.json next to this file.
"""

import json
import random
import sys
import tempfile
from pathlib import Path

DIM = 16
OUT = Path(__file__).with_name("vector_store_golden.json")


def _vec(rng: random.Random) -> list[float]:
    return [round(rng.uniform(-1.0, 1.0), 6) for _ in range(DIM)]


def build_fixture() -> dict:
    rng = random.Random(20261031)
    entries = []
    types = ["decision", "fail", "discovery", "pattern", "task"]
    for i in range(24):
        entries.append({
            "id": f"node{i:02d}",
            "embedding": _vec(rng),
            "metadata": {
                "entity_type": types[i % len(types)],
                "summary": f"summary {i}",
                "author": "Colton Dyck",
                "timestamp": f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00",
                "context": ["a.py", f"topic{i}"],
                "chunk_count": i % 3,
                "from_agent": bool(i % 2),
                "weight": i / 7,
                "severity": None,
            },
            "document": None,
        })
    for n in range(3):
        for c in range(3):
            entries.append({
                "id": f"doc{n}#chunk-{c}",
                "embedding": _vec(rng),
                "metadata": {"node_id": f"doc{n}", "entity_type": "document", "is_chunk": True},
                "document": f"chunk {c} of doc {n}",
            })
    queries = [_vec(rng) for _ in range(5)]
    return {"dim": DIM, "entries": entries, "queries": queries}


def _hit(h: dict) -> dict:
    out = {k: v for k, v in h.items() if k not in ("updated_at", "score")}
    out["score"] = round(float(h["score"]), 5)
    out["has_updated_at"] = "updated_at" in h
    return out


def record(fixture: dict) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from vibe_cognition.embeddings.storage import ChromaDBStorage

    results: dict = {"searches": [], "counts": {}, "after_deletes": {}}
    with tempfile.TemporaryDirectory() as tmp:
        store = ChromaDBStorage(Path(tmp) / "store", embedding_model="m", embedding_dimensions=DIM)
        for e in fixture["entries"]:
            store.upsert_embedding(e["id"], e["embedding"], e["metadata"], document=e["document"])
        store.upsert_embedding(
            "node03", fixture["entries"][3]["embedding"],
            {**fixture["entries"][3]["metadata"], "summary": "overwritten"},
        )
        for qi, q in enumerate(fixture["queries"]):
            for limit in (1, 3, 10, 100):
                for et in (None, "decision", "document"):
                    hits = store.vector_search(q, limit=limit, entity_type=et)
                    results["searches"].append({
                        "query": qi, "limit": limit, "entity_type": et,
                        "hits": [_hit(h) for h in hits],
                    })
        results["counts"] = {
            "all": store.count_documents(),
            "is_chunk": store.count_documents(filter={"is_chunk": True}),
            "node_id_doc1": store.count_documents(filter={"node_id": "doc1"}),
            "entity_decision": store.count_documents(filter={"entity_type": "decision"}),
        }
        store.delete_embedding("node00")
        store.delete_by_node_id("doc2")
        hits = store.vector_search(fixture["queries"][0], limit=100)
        results["after_deletes"] = {
            "count": store.count_documents(),
            "ids": [h["_id"] for h in hits],
        }
        store.close()
    return results


def main() -> None:
    fixture = build_fixture()
    golden = {
        "_note": "Recorded from ChromaDBStorage (chromadb 1.5.5) by record_vector_store_golden.py",
        "fixture": fixture,
        "expected": record(fixture),
    }
    OUT.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
