"""WP-T: the get_lifespan accessor (T-9) + the node_type/direction parsers (T-6)."""

from types import SimpleNamespace
from typing import cast

import pytest
from fastmcp import Context

from vibe_cognition.cognition.models import CognitionNodeType
from vibe_cognition.tools.cognition_tools import _parse_node_type, _validate_direction
from vibe_cognition.tools.utils import get_lifespan


def test_get_lifespan_returns_lifespan_context():
    ctx = cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context={"k": "v"})))
    assert get_lifespan(ctx) == {"k": "v"}


def test_get_lifespan_raises_when_request_context_is_none():
    """The accessor exists to narrow fastmcp's Optional request_context — when it IS
    None (never inside a live tool call) it must raise, not return None-attribute soup."""
    ctx = cast(Context, SimpleNamespace(request_context=None))
    with pytest.raises(RuntimeError):
        get_lifespan(ctx)


def test_parse_node_type_valid_none_and_bad():
    """T-6: one parser, one error shape — bad type returns an error dict, never raises
    (the old get_uncurated did a bare CognitionNodeType(node_type) that RAISED)."""
    assert _parse_node_type("decision") == (CognitionNodeType.DECISION, None)
    assert _parse_node_type(None) == (None, None)
    nt, err = _parse_node_type("bogus")
    assert nt is None
    assert err is not None and "Invalid node_type" in err["error"]
    # Fails-before contrast: the bare enum call the old tool used does raise.
    with pytest.raises(ValueError):
        CognitionNodeType("bogus")


def test_validate_direction():
    """T-6: an unknown direction is rejected, not silently treated as incoming /
    returned as an empty success."""
    assert _validate_direction("outgoing", ("outgoing", "incoming")) is None
    assert _validate_direction("both", ("incoming", "outgoing", "both")) is None
    err = _validate_direction("sideways", ("incoming", "outgoing", "both"))
    assert err is not None and "Invalid direction" in err["error"]
