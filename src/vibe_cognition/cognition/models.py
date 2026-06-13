"""Data models for the Cognition History Graph."""

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# UP042 (str, Enum -> StrEnum) deferred in WP-1 §8.1: StrEnum changes str()
# semantics, so it is not a mechanical fix. noqa here and on CognitionEdgeType
# keeps `ruff check .` clean and gating in CI until that change is made deliberately.
class CognitionNodeType(str, Enum):  # noqa: UP042
    """Types of nodes in the cognition graph."""

    DECISION = "decision"
    FAIL = "fail"
    DISCOVERY = "discovery"
    ASSUMPTION = "assumption"
    CONSTRAINT = "constraint"
    INCIDENT = "incident"
    PATTERN = "pattern"
    EPISODE = "episode"
    # A stored document (client doc, spec, etc.) — episode-like in the matcher
    # (a hub for part_of links), with reference/blob storage + a text sidecar.
    DOCUMENT = "document"


class CognitionEdgeType(str, Enum):  # noqa: UP042  (see CognitionNodeType above)
    """Types of edges in the cognition graph."""

    LED_TO = "led_to"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    RELATES_TO = "relates_to"
    RESOLVED_BY = "resolved_by"
    PART_OF = "part_of"
    DUPLICATE_OF = "duplicate_of"


class CognitionNode(BaseModel):
    """A node in the cognition history graph."""

    id: str
    type: CognitionNodeType
    summary: str
    detail: str
    context: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    severity: str | None = None
    timestamp: str
    author: str
    # Structured, type-specific metadata (e.g. a document node's filename / mime /
    # size / sha256 / mode / path). Empty for ordinary nodes. Journaled + replayed
    # like any other field.
    metadata: dict[str, Any] = Field(default_factory=dict)


class CognitionEdge(BaseModel):
    """An edge in the cognition history graph."""

    from_id: str
    to_id: str
    edge_type: CognitionEdgeType
    timestamp: str
    # Historical default provenance tag, NOT an active curator (that feature was
    # removed). Real write paths pass an explicit source (deterministic, manual,
    # batch, curate-skill); this default is the fallback for legacy data.
    source: str = "curator"
    # The agent's curation rationale for this edge (why A led_to/supersedes/etc. B).
    # The edge-analyzer produces one per edge; persisted so it survives replay and
    # surfaces via get_neighbors. None for deterministic edges (no agent rationale).
    reason: str | None = None


def generate_node_id(node_type: str, summary: str, timestamp: str | None = None) -> str:
    """Generate a hash-based node ID that is conflict-free across branches/users.

    Args:
        node_type: The type of the node
        summary: The summary text
        timestamp: ISO 8601 timestamp (defaults to now)

    Returns:
        12-character hex string
    """
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()
    raw = f"{node_type}:{summary}:{timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
