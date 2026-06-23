"""T-1b: instructions.py — SERVER_INSTRUCTIONS and main() coverage.

Pins: ASCII-only (Windows stdout safety), stdlib-only, main() emits
valid SessionStart JSON. All zero coverage before this WP.
"""

import io
import json

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


def test_main_emits_session_start_json(monkeypatch):
    """instructions.main(): stdout is a single valid JSON dict with SessionStart shape.

    Fails-before: if main() emitted plain text or a different hook event name,
    making the post-compaction re-injection silently a no-op in Claude Code.
    """
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


def test_main_output_is_single_json_object(monkeypatch):
    """instructions.main(): emits exactly one JSON object (no trailing newlines/extra).

    Fails-before: if main() accidentally wrote multiple JSON blobs that would
    confuse the Claude Code hook parser.
    """
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main()
    out = buf.getvalue().strip()
    # Must parse as a single JSON object without wrapping in a list
    data = json.loads(out)
    assert isinstance(data, dict)
