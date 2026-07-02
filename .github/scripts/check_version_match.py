"""pyproject.toml <-> plugin.json version-match check for CI (WP-6, b48927a30e66).

The release procedure requires bumping both files by hand (see CLAUDE.md's
"Plugin Release Procedure"); nothing previously asserted they stayed in sync.
A forgotten bump ships a plugin whose displayed version misrepresents the
pinned code. Run inside the uv environment: `uv run python <this>`.
"""

import json
import pathlib
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
PLUGIN_JSON_PATH = REPO_ROOT / ".claude-plugin" / "plugin.json"


def main() -> int:
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    try:
        pyproject_version = pyproject["project"]["version"]
    except KeyError:
        print(f"::error::{PYPROJECT_PATH}: no [project].version key found", file=sys.stderr)
        return 1

    # utf-8-sig tolerates a BOM (a Windows editor may add one) without raising.
    plugin = json.loads(PLUGIN_JSON_PATH.read_text(encoding="utf-8-sig"))
    try:
        plugin_version = plugin["version"]
    except KeyError:
        print(f"::error::{PLUGIN_JSON_PATH}: no top-level \"version\" key found", file=sys.stderr)
        return 1

    if pyproject_version != plugin_version:
        print(
            f"::error::version mismatch: pyproject.toml={pyproject_version!r} "
            f"!= .claude-plugin/plugin.json={plugin_version!r}. Both must be "
            "bumped together per the Plugin Release Procedure in CLAUDE.md.",
            file=sys.stderr,
        )
        return 1

    print(f"version match: {pyproject_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
