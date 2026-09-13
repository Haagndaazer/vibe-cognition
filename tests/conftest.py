"""Shared pytest fixtures for the vibe-cognition test suite.

Provides the three infrastructure fixtures used across the wrapper and support-module
tests (T-1a/b spec). The split between build_lc and make_ctx is intentional (B1):
build_lc owns the storage/threading state; make_ctx owns only the Context shim.
"""

import asyncio
import contextlib
import functools
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.identity import (
    identity_write_path,
    write_confirmed_identity,
)
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.project_registry import build_registry

# ── _TextKeyedGen ─────────────────────────────────────────────────────────────
#
# Orthogonal-unit-vector embedder keyed on marker words — promoted from
# test_wp_cap.py:117 (decision: constant-vector fake is tautological because it
# can't distinguish a re-embed from a stale vector, discovery 986687c1ed27).

class _TextKeyedGen:
    """Text-KEYED fake embedder: distinct marker words → distinct orthogonal unit
    vectors, so a re-embed genuinely moves the stored vector. Never loads a model."""

    _MARKERS = {
        "alpha": [1.0, 0.0, 0.0],
        "beta": [0.0, 1.0, 0.0],
        "gamma": [0.0, 0.0, 1.0],
    }

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        low = text.lower()
        for marker, vec in self._MARKERS.items():
            if marker in low:
                return list(vec)
        return [0.0, 0.0, 1.0]

    def generate_query_embedding(self, text: str) -> list[float]:
        return self.generate(text, input_type="query")


# ── _MockMcp ──────────────────────────────────────────────────────────────────
#
# Promoted from test_xp2_routing.py:297. Captures registered closures by name
# via the @mcp.tool() decorator shim without depending on FastMCP internals.
#
# WP-Wedge-2 §W2-b: every registered tool is now `async def` (dispatch_tool's
# async wrapper routing to the dedicated dispatch executor, replacing plain
# `@mcp.tool()` -- see tools/dispatch.py). mock_mcp.tools[name](ctx, ...) is
# called synchronously, directly, by hundreds of existing sync `def test_...`
# functions across the suite; asyncio.run() here is the ONE place that
# absorbs the sync/async mismatch so none of those call sites need to change.
# Safe because no test calls mock_mcp.tools[...] from inside an already-
# running event loop (grep-verified: zero `async def test_` functions do).

class _MockMcp:
    """Minimal MCP stub: .tool() captures registered closures into self.tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):  # type: ignore[override]
        def decorator(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                def sync_shim(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))
                self.tools[fn.__name__] = sync_shim
            else:
                self.tools[fn.__name__] = fn
            return fn
        return decorator


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_identity_resolution(monkeypatch, tmp_path):
    """Keep graph-identity resolution off the real machine, and make it resolvable.

    Two problems this solves. First, without it every test inherited whatever
    identity happened to be in the developer's ~/.gitconfig, so attribution
    assertions were machine-dependent. Second and sharper: since v0.37.0 writes
    are GATED on a resolvable email, so on a fresh CI runner with no global git
    identity the gate refuses and ~94 write-path tests fail -- green locally,
    red everywhere else.

    A deterministic config is written per test, so the gate always passes and the
    stamped identity is the same everywhere. Tests that need the UNRESOLVED case
    (tests/test_identity.py's `repo` fixture, the refusal tests) point these env
    vars at nonexistent paths themselves, which runs after this and wins.
    """
    gitconfig = tmp_path / "_identity_gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test User\n\temail = test-user@example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "_no_system_gitconfig"))
    # SVN is suggestion-only and must never be read off the real machine.
    monkeypatch.setenv("SVN_CONFIG_DIR", str(tmp_path / "_no_svn_config"))


TEST_IDENTITY = {"name": "Test User", "email": "test-user@example.invalid"}

TEST_PROFILE = {"role": "engineer", "seniority": "mid", "reports_to": "nobody"}


class GraphIdentity:
    """The acting graph identity for one test, and how it reaches the code.

    WP-Identity-Profiles made every graph WRITE require a CONFIRMED identity
    whose committed profile is complete. A test that only has a git config no
    longer passes the gate, so the default checkout every test gets must be an
    ONBOARDED one -- which is also the only state the product can be in once
    anyone has recorded anything.

    Seeding happens inside CognitionStorage construction rather than in one
    fixture, because tests build storage from eight different places (build_lc
    plus seven files with their own lifespan dicts) and a per-site helper would
    be forgotten by the next site added.

    Three knobs:
      * default -- identity.json + a complete profile for TEST_IDENTITY, written
        through the real write paths, so resolve_identity/require_identity run
        for real rather than being patched out.
      * acting_as(...) -- a different identity, for tests asserting what gets
        stamped. Patches _acting_identity too, so a raw (non-casefolded) address
        still reaches the tool and casefolding assertions stay meaningful.
      * unonboarded() -- seed nothing, for the refusal tests.
    """

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self._identity = dict(TEST_IDENTITY)
        self._profile = dict(TEST_PROFILE)
        self._enabled = True
        self._dirs: list[Path] = []

    def acting_as(self, name, email, seed=True, **profile):
        """Act as this identity, confirmed, with a complete profile.

        `seed=False` confirms the checkout but writes NO profile -- for testing the
        person whose profile someone else authored, or who has not answered yet.
        """
        self._identity = {"name": name, "email": email}
        self._profile.update(profile)
        self._enabled = seed
        self._monkeypatch.setattr(
            "vibe_cognition.tools.cognition_tools._acting_identity",
            lambda cognition_dir: {
                "name": name, "email": email, "source": "confirmed", "confirmed": True,
            },
        )
        for d in self._dirs:
            write_confirmed_identity(d, name, email)
            if seed:
                self._write(d)

    def unresolvable(self, name="unknown"):
        """No email resolves at all -- the case the write gate exists to refuse.

        Any already-seeded identity.json is removed, not just left behind: the
        gate's refusal payload re-resolves from disk, so a stale confirmed file
        would contradict the patched stamp and the gate would pass.
        """
        self._enabled = False
        for d in self._dirs:
            with contextlib.suppress(OSError):
                identity_write_path(d).unlink()
        self._monkeypatch.setattr(
            "vibe_cognition.tools.cognition_tools._acting_identity",
            lambda cognition_dir: {
                "name": name, "email": "", "source": "os-user", "confirmed": False,
            },
        )

    def unonboarded(self):
        """Seed nothing: a checkout nobody has confirmed an identity in."""
        self._enabled = False

    def seed(self, cognition_dir):
        self._dirs.append(Path(cognition_dir))
        if self._enabled:
            self._write(Path(cognition_dir))

    def _write(self, cognition_dir):
        from vibe_cognition.cognition.profiles import ProfileRegistry

        name, email = self._identity["name"], self._identity["email"]
        folded = email.strip().casefold()
        if not folded:
            return
        write_confirmed_identity(cognition_dir, name, email)
        registry = ProfileRegistry(cognition_dir)
        # Fold what is already on disk first: set_fields skips a field only when
        # it can see the current value, so an unfolded registry re-appends every
        # field on each re-seed.
        registry.catch_up()
        registry.set_fields(
            folded,
            {"name": name, "email": folded, **self._profile},
            {"name": name, "email": folded},
            from_agent=False,
        )


def identity_stamp(name, email, source="confirmed", confirmed=True):
    """The identity dict a write actually stamps.

    Four keys, not two: source/confirmed ride along so a later audit can tell a
    human-confirmed identity from whatever sat in a shared machine's git config.
    Assertions that spelled out {name, email} were matching the old hand-rolled
    mock's shape rather than the product's, and passed for that reason alone.
    """
    return {"name": name, "email": email, "source": source, "confirmed": confirmed}


@pytest.fixture(autouse=True)
def graph_identity(monkeypatch, _isolate_identity_resolution):
    """Every CognitionStorage a test builds starts as an onboarded checkout."""
    state = GraphIdentity(monkeypatch)
    real_init = CognitionStorage.__init__

    def init(self, cognition_dir, *args, **kwargs):
        state.seed(cognition_dir)
        real_init(self, cognition_dir, *args, **kwargs)

    monkeypatch.setattr(CognitionStorage, "__init__", init)
    return state


@pytest.fixture(autouse=True)
def _isolate_chroma_resolution(monkeypatch, tmp_path):
    """WP-Chroma-Home: keep chromadb path resolution off the real machine.

    Without this, any test that builds Settings() and touches
    cognition_chromadb_path on a dev machine resolves into the REAL
    ~/.claude/plugins/data/vibe-cognition-* dir (the discovery glob finds it —
    confirmed live during scoping). Clearing the env vars and pointing the
    discovery seam at a nonexistent tmp dir restores the legacy in-repo
    fallback for every test by default; resolution-order tests set env vars /
    seed the seam dir explicitly on top of this baseline.
    """
    from vibe_cognition import config as config_module

    for var in ("VIBE_CHROMADB_DIR", "VIBE_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        config_module, "_home_plugins_data_dir", lambda: tmp_path / "_no_plugins_data"
    )


@pytest.fixture
def fake_generator() -> EmbeddingGenerator:
    """A text-keyed fake embedder (3-D, no model load)."""
    return cast(EmbeddingGenerator, _TextKeyedGen())


@pytest.fixture
def build_lc(fake_generator: EmbeddingGenerator):  # type: ignore[type-arg]
    """Factory fixture: call with (tmp_path, embeddings_ready=False) → lc dict.

    Mirrors _make_lc in test_xp2_routing.py:33 plus the embedding_ready knob
    (B4): an unset threading.Event makes require_embeddings return an error dict,
    so happy-path embedding assertions on an unset event are vacuous. Pass
    embeddings_ready=True for the ready/happy path; leave False to test the
    not-ready error path.
    """
    def _factory(tmp_path, *, embeddings_ready: bool = False) -> dict[str, Any]:
        home_path = tmp_path / "home"
        home_path.mkdir(parents=True, exist_ok=True)
        cognition_storage = CognitionStorage(home_path / ".cognition")
        chroma = ChromaDBStorage(
            persist_directory=home_path / ".cognition" / "chromadb",
            embedding_model="m",
            embedding_dimensions=3,
        )
        config = SimpleNamespace(
            embedding_model="m",
            embedding_dimensions=3,
            repo_path=home_path,
            effective_repo_name="home",
        )
        registry = build_registry(
            home_path=home_path,
            home_tag="home",
            home_storage=cognition_storage,
            home_embeddings=chroma,
        )
        event = threading.Event()
        if embeddings_ready:
            event.set()
        return {
            "config": config,
            "cognition_storage": cognition_storage,
            "cognition_embedding_storage": chroma,
            "loaded_projects": registry,
            "embedding_generator": fake_generator,
            "embedding_ready": event,
            "embedding_error": None,
        }

    return _factory


@pytest.fixture
def make_ctx():
    """Factory fixture: call with an lc dict → fake FastMCP Context.

    Mirrors _make_ctx in test_xp2_routing.py:367. Kept separate from build_lc
    (B1: do NOT conflate lifespan-dict construction with Context wrapping).
    """
    from fastmcp import Context

    def _factory(lc: dict[str, Any]) -> Context:
        return cast(
            Context,
            SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)),
        )

    return _factory


@pytest.fixture
def mock_mcp() -> _MockMcp:
    """Fresh _MockMcp collector per test."""
    return _MockMcp()
