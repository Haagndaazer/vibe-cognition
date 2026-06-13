"""WP-T: the get_lifespan accessor (T-9) — the narrowing wiring guard."""

from types import SimpleNamespace
from typing import cast

import pytest
from fastmcp import Context

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
