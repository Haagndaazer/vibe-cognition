"""T-1b: instructions.py — SERVER_INSTRUCTIONS and main() coverage.

Pins: ASCII-only (Windows stdout safety), stdlib-only, main() emits
valid SessionStart JSON. All zero coverage before this WP.
"""

import io
import json

from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.instructions import SERVER_INSTRUCTIONS, main


def test_server_instructions_ascii_only():
    """SERVER_INSTRUCTIONS contains only ASCII characters.

    Fails-before: if a non-ASCII character (emoji, curly quote, em-dash) was
    added to the instructions and a Windows stdout without UTF-8 mode would
    crash on encode (the hook uses python -m vibe_cognition.instructions and
    must be safe on any locale's default encoding).
    """
    SERVER_INSTRUCTIONS.encode("ascii")  # raises UnicodeEncodeError if non-ASCII


def test_server_instructions_is_non_empty():
    """SERVER_INSTRUCTIONS is a non-empty string."""
    assert isinstance(SERVER_INSTRUCTIONS, str)
    assert len(SERVER_INSTRUCTIONS) > 0


def test_main_emits_session_start_json(tmp_path, monkeypatch):
    """instructions.main(): stdout is a single valid JSON dict with SessionStart shape.

    Fails-before: if main() emitted plain text or a different hook event name,
    making the post-compaction re-injection silently a no-op in Claude Code.

    REPO_PATH is isolated to tmp_path (WP-7): main() now also builds a
    CognitionStorage for the prime digest, and without an explicit REPO_PATH
    it falls back to cwd -- which under pytest is this repo's OWN root, so an
    unisolated run would touch the real project's .cognition/ as a side
    effect (observed: a stale .gitignore entry got silently rewritten).
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()

    out = buf.getvalue()
    data = json.loads(out)
    assert "hookSpecificOutput" in data
    hook = data["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    assert "additionalContext" in hook
    assert SERVER_INSTRUCTIONS in hook["additionalContext"]


def test_main_output_is_single_json_object(tmp_path, monkeypatch):
    """instructions.main(): emits exactly one JSON object (no trailing newlines/extra).

    Fails-before: if main() accidentally wrote multiple JSON blobs that would
    confuse the Claude Code hook parser.
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()
    out = buf.getvalue().strip()
    # Must parse as a single JSON object without wrapping in a list
    data = json.loads(out)
    assert isinstance(data, dict)


# ── WP-7 (530adc9e6f3f): compact also regenerates the prime digest ───────────


def _node(node_id, summary="s", ntype=CognitionNodeType.DECISION):
    return CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-06-21T00:00:00+00:00", author="t",
    )


def test_main_includes_prime_digest_when_graph_nonempty(tmp_path, monkeypatch):
    """The whole point of this WP: a compact must bring back the graph's
    actual backlog (open tasks, constraints, etc.), not just the static
    standing practices.

    Fails-before: main() only ever emitted SERVER_INSTRUCTIONS -- a node
    recorded before the compact was invisible in additionalContext after it.
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("n1", summary="a decision worth remembering"))
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert SERVER_INSTRUCTIONS in ctx
    assert "a decision worth remembering" in ctx
    assert "Vibe Cognition" in ctx and "Project Context" in ctx  # generate_prime's header


def test_main_omits_prime_digest_when_no_cognition_dir(tmp_path, monkeypatch):
    """No .cognition/ at all (e.g. REPO_PATH points somewhere unexpected) must
    not error -- just the standing practices, nothing appended."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert ctx.strip() == f"# Vibe Cognition - Standing Practices (re-injected after compaction)\n\n{SERVER_INSTRUCTIONS}"


def test_main_omits_prime_digest_when_graph_empty(tmp_path, monkeypatch):
    """A .cognition/ dir with zero nodes must not append an empty/onboarding
    digest on compact -- that's a startup-only concern, already shown once."""
    CognitionStorage(tmp_path / ".cognition")  # creates the dir, adds nothing
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "Project Context" not in ctx
    assert SERVER_INSTRUCTIONS in ctx


def test_main_never_raises_when_prime_generation_fails(tmp_path, monkeypatch):
    """Any failure building the digest must never suppress the standing-
    practices reinject -- that block must always get through regardless of
    digest generation errors."""
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("n1"))
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    import vibe_cognition.cognition.prime as prime_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated prime failure")

    monkeypatch.setattr(prime_module, "generate_prime", _boom)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()  # must not raise

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert SERVER_INSTRUCTIONS in ctx
