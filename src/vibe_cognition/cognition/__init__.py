"""Cognition History Graph — captures decisions, failures, discoveries, and reasoning chains."""

from .git_identity import resolve_git_identity
from .models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    generate_node_id,
)
from .operations import delete_cognition_node
from .queries import (
    get_history_for_context,
    get_incident_resolution,
    get_reasoning_chain,
    get_superseded_chain,
    get_workflow_head,
)
from .storage import CognitionStorage

__all__ = [
    # Models
    "CognitionEdge",
    "CognitionEdgeType",
    "CognitionNode",
    "CognitionNodeType",
    "generate_node_id",
    # Identity
    "resolve_git_identity",
    # Storage
    "CognitionStorage",
    # Operations
    "delete_cognition_node",
    # Queries
    "get_history_for_context",
    "get_incident_resolution",
    "get_reasoning_chain",
    "get_superseded_chain",
    "get_workflow_head",
]
