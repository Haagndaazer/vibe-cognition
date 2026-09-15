"""Personal vs project constraints (docs/wp-personal-constraints-plan.md).

A personal constraint applies only to the person who recorded it and is visible
only to them. Everything else is project scope, including every constraint
recorded before scopes existed.
"""

from typing import Any

from .models import CognitionNodeType
from .people_facts import fold_email

SCOPE_KEY = "scope"
SCOPE_PERSONAL = "personal"
SCOPE_PROJECT = "project"
SCOPES = (SCOPE_PERSONAL, SCOPE_PROJECT)


def node_scope(data: dict[str, Any]) -> str:
    if data.get("type") != CognitionNodeType.CONSTRAINT.value:
        return SCOPE_PROJECT
    meta = data.get("metadata") or {}
    return SCOPE_PERSONAL if meta.get(SCOPE_KEY) == SCOPE_PERSONAL else SCOPE_PROJECT


def recorded_by_email(data: dict[str, Any]) -> str:
    stamp = (data.get("metadata") or {}).get("recorded_by")
    if isinstance(stamp, dict) and stamp.get("email"):
        return fold_email(str(stamp["email"]))
    return ""


def visible_to(data: dict[str, Any], viewer_email: str) -> bool:
    if node_scope(data) != SCOPE_PERSONAL:
        return True
    owner = recorded_by_email(data)
    return bool(owner) and owner == fold_email(viewer_email or "")
