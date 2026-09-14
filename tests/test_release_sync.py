"""Release metadata must not drift from the code. Hard fail, not a checklist item.

The release procedure already says to update the CHANGELOG, the whats-new bullets
and the README tables. Saying it is not enough: v0.36.0 shipped three tools with an
undocumented parameter, and this release shipped three separate pieces of prose that
still described the pre-v0.38 model — including an onboarding notice that sent agents
to a tool the write gate refuses. Every one of those was caught by a human reading it,
not by a gate.

So the version bump itself is the trigger. Bump the manifests without writing the
CHANGELOG section and this fails by name, at the point the mistake is cheap to fix,
instead of shipping docs that describe a version nobody is running.

The README's MCP tool table is checked here too. `tests/test_doc_drift.py` already
enforces the SKILL.md table; the README's equivalent was "check by hand" in the
release rule, which is the same as unchecked.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


# ── the version bump is the trigger ──────────────────────────────────────────


def test_changelog_has_a_section_for_the_current_version(version):
    """A released version with no CHANGELOG section is a release nobody can read
    the history of. The bump is what makes this fail, so it fails while the change
    is still in front of you."""
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## [{version}]"
    assert heading in changelog, (
        f"pyproject.toml is at {version} but CHANGELOG.md has no '{heading}' section. "
        "Add it before shipping — the version bump and the changelog entry belong to "
        "the same edit."
    )


def test_changelog_section_is_not_empty(version):
    """An empty section satisfies the letter of the rule and none of the point."""
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## [{version}]"
    # Its own message, not an IndexError out of the split below: a gate that fails
    # confusingly costs the reader exactly the time the gate was meant to save.
    assert heading in changelog, f"no '{heading}' section in CHANGELOG.md"
    body = changelog.split(heading, 1)[1].split("\n## [", 1)[0]
    assert re.search(r"^\s*[-*]\s+\S", body, re.M), (
        f"CHANGELOG.md's [{version}] section has no bullets. Say what changed."
    )


def test_whats_new_has_bullets_for_the_current_version(version):
    """whats-new is what a user actually sees after updating. A missing entry means
    they update and are told nothing."""
    data = json.loads((REPO / ".claude-plugin" / "whats-new.json").read_text(encoding="utf-8"))
    assert version in data, (
        f"pyproject.toml is at {version} but .claude-plugin/whats-new.json has no "
        f"'{version}' key. Users who update get no summary of what changed."
    )
    assert data[version], f"whats-new.json's {version} entry is empty."


def test_the_three_manifests_and_the_changelog_agree(version):
    """test_codex_plugin already pins the three manifests to each other; this pins
    them to the release notes, so a half-finished bump cannot pass."""
    claude = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert claude["version"] == version, (claude["version"], version)
    assert codex["version"] == version, (codex["version"], version)


# ── the README must describe the tools that actually exist ───────────────────


def _registered_tool_names() -> set[str]:
    from vibe_cognition.tools import register_all_tools

    class _Mcp:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    mcp = _Mcp()
    register_all_tools(mcp)
    return set(mcp.tools)


def _readme_documented_tools(readme: str) -> set[str]:
    """Tool names appearing in a README markdown table row, i.e. `| \\`name\\` | ... |`."""
    return set(re.findall(r"^\|\s*`(cognition_\w+|get_status)`\s*\|", readme, re.M))


def test_readme_documents_every_registered_tool():
    """The release rule said to check the README by hand, which is how a new tool
    ships undocumented. cognition_remove_person was added this release; nothing but
    a human reading the table would have caught its absence."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    missing = sorted(_registered_tool_names() - _readme_documented_tools(readme))
    assert not missing, (
        f"README.md's MCP tools table has no row for: {missing}. Every registered "
        "tool needs one — a user reading the README is entitled to a complete list."
    )


def test_readme_does_not_document_tools_that_no_longer_exist():
    """The other direction: a removed or renamed tool left in the table sends a user
    after something that will error."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    stale = sorted(_readme_documented_tools(readme) - _registered_tool_names())
    assert not stale, (
        f"README.md's MCP tools table lists tools that are not registered: {stale}. "
        "Remove the rows, or the README sends users after tools that do not exist."
    )


def test_the_cognition_readme_guide_table_lists_every_registered_tool():
    """The guide is what an agent reads when dropped into an unfamiliar project, and
    its "Tool groups" table is the quick reference. A tool missing there is a tool
    that agent will not reach for.

    This gate exists because the release-sync check shipped covering the README and
    SKILL.md tables but NOT this one -- and `cognition_remove_person` was absent from
    the guide's table for two releases, found only by reading the live output. Three
    tables, three chances to miss one; all three are checked now.

    Scoped to the TABLE deliberately. A first draft of this test looked for the name
    anywhere in the guide and passed while the table was still missing it, because
    the prose mentioned it further down -- a gate that cannot fail is worse than no
    gate, so this one was verified by deleting the row and watching it go red.
    """
    from vibe_cognition.cognition.readme import COGNITION_GUIDE

    table = COGNITION_GUIDE.split("## Tool groups", 1)[1].split("\n## ", 1)[0]
    missing = sorted(n for n in _registered_tool_names() if n not in table)
    assert not missing, (
        f"cognition_readme's guide table omits: {missing}. Add them — an agent "
        "reading the guide is entitled to the full tool surface."
    )


# ── SVN ignore guidance must cover every directory entry ─────────────────────


def test_svn_guidance_names_every_directory_ignore_entry():
    """SVN ignore globs have no directory-only meaning, so a trailing slash makes an
    entry match NOTHING -- silently. The guidance therefore has to tell SVN users to
    strip it from every directory entry, by name.

    This has already gone wrong twice. The guide said to strip it only from
    `chromadb/` after `local/` was added, and `local/` holds identity.json -- so a
    team following it committed their name and email. 0.38.3 fixed the
    cognition_readme guide and missed the README, which kept the same instruction
    for another release. Pinning every directory entry to BOTH surfaces means adding
    a new one fails here until the SVN guidance mentions it.
    """
    from vibe_cognition.cognition.git_hygiene import _GITIGNORE_ENTRIES
    from vibe_cognition.cognition.readme import COGNITION_GUIDE

    directories = [e for e in _GITIGNORE_ENTRIES if e.endswith("/")]
    assert directories, "no directory entries -- this test would be vacuous"
    surfaces = {
        "README.md": (REPO / "README.md").read_text(encoding="utf-8"),
        "cognition_readme guide": COGNITION_GUIDE,
    }
    for label, text in surfaces.items():
        for entry in directories:
            assert f"`{entry}`" in text, (
                f"{label}'s SVN guidance never names `{entry}`. Its trailing slash must "
                "be stripped for SVN or the entry silently ignores nothing."
            )
        flat = " ".join(text.split())
        assert "strip the trailing slash from `chromadb/`" not in flat, (
            f"{label} still tells SVN users to strip the slash from chromadb/ ONLY, "
            "which leaves local/ -- and the identity file inside it -- unignored."
        )


# ── prose that named the pre-v0.38 model ─────────────────────────────────────


#: Files whose prose an agent or a user reads as instructions. A term here is one
#: the v0.38 roster change made wrong; leaving it in place tells the reader to look
#: for a field or an entity that no longer holds the roster.
_PROSE_SURFACES = (
    "README.md",
    "src/vibe_cognition/cognition/readme.py",
    "skills-src/vibe-cognition/SKILL.md",
)

#: `reports_to_email` is still the LEGACY person-node field name, so the modules
#: that read legacy nodes may name it. These are the reader-facing surfaces, which
#: must describe what the tools take today: `reports_to`.
_RETIRED_TERMS = ("reports_to_email",)


@pytest.mark.parametrize("relpath", _PROSE_SURFACES)
def test_reader_facing_prose_does_not_name_retired_fields(relpath):
    text = (REPO / relpath).read_text(encoding="utf-8")
    for term in _RETIRED_TERMS:
        assert term not in text, (
            f"{relpath} still names `{term}`, which the roster move retired from the "
            "tool surface. A reader will go looking for a parameter that no longer "
            "exists — say `reports_to` instead."
        )
