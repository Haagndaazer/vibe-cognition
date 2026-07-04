"""Configuration management for Vibe Cognition."""

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_repo_path() -> Path:
    """Default repository path when REPO_PATH is not set explicitly.

    Claude Code injects ``CLAUDE_PROJECT_DIR`` into a plugin's spawned MCP
    server environment, so prefer it; fall back to the current working
    directory for non-plugin / manual launches.
    """
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env_dir) if env_dir else Path.cwd()


def resolve_repo_path_env(*, default: Path | None = None) -> Path:
    """Read the REPO_PATH env var directly, absent-OR-empty-safe.

    THE single such read (WP-6, b603f667130f) — prime.py, backfill.py, and
    dashboard/cli.py each read this independently for their standalone-CLI
    entry points (they don't go through the full ``Settings`` class), and
    only cli.py's ``or``-guarded read actually handled an explicitly-empty
    value; the others used ``os.environ.get("REPO_PATH", Path.cwd())``, whose
    default only fires when the KEY IS ABSENT — an explicitly-empty
    ``REPO_PATH`` (e.g. an unresolved ``${CLAUDE_PROJECT_DIR}`` substitution)
    passed straight through as ``""``. ``Path("")`` silently aliases to the
    current working directory (a pathlib quirk — ``Path("") == Path(".")``),
    which for the plugin's ``uv run --directory`` launch IS the plugin's own
    install root, not the user's project: ``.cognition/`` would get created
    in the wrong place, silently. (The ``Settings.repo_path`` field itself
    gets the equivalent protection via ``env_ignore_empty`` below — this
    helper is for the non-Settings entry points.)
    """
    value = os.environ.get("REPO_PATH")
    if value:
        return Path(value)
    return default if default is not None else Path.cwd()


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        # No env_file: it would resolve against the process's cwd, which for
        # every project's server is the SHARED plugin root (`uv run
        # --directory`) — a project-level .env would be inert, and a
        # plugin-root .env would silently become global config for every
        # project (WP-6, d4a153f23a4c). Config is env-var-only; document that
        # rather than resolve a per-project .env path (ambiguous/circular:
        # repo_path isn't known until this class is built).
        env_ignore_empty=True,  # WP-6 (b603f667130f): "" is absent, not an override
        extra="ignore",
    )

    # Repository settings
    repo_path: Path = Field(
        default_factory=_default_repo_path,
        description="Path to the repository to index",
    )
    repo_name: str | None = Field(
        default=None,
        description="Name of the repository (defaults to directory name)",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )

    # Git hygiene settings
    vibe_cognition_no_git_hygiene: bool = Field(
        default=False,
        description=(
            "Set to a truthy value to suppress the one-time git-hygiene pass "
            "(disables auto-writing .gitattributes merge=union and "
            ".cognition/.gitignore chromadb/ on startup). Use this in "
            "single-shared-checkout repos where union-merge is the wrong "
            "topology (worktree-flush protocol is used instead)."
        ),
    )

    # Embedding backend settings
    embedding_backend: Literal["sentence-transformers", "ollama"] = Field(
        default="sentence-transformers",
        description="Embedding backend to use",
    )
    embedding_model: str = Field(
        default="nomic-ai/nomic-embed-text-v1.5",
        description="Model name for sentence-transformers backend",
    )
    embedding_dimensions: int = Field(
        default=768,
        description="Embedding vector dimensions",
    )
    embedding_revision: str | None = Field(
        default=None,
        description=(
            "HuggingFace Hub revision (branch, tag, or full commit SHA) for the "
            "sentence-transformers model. When set, pins the remote code loaded via "
            "trust_remote_code=True to a specific commit — recommended for production. "
            "Set via EMBEDDING_REVISION env var. Default None = use the model hub HEAD."
        ),
    )

    # Ollama settings (embeddings backend only)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server base URL",
    )
    ollama_model: str = Field(
        default="nomic-embed-text",
        description="Ollama model name for embeddings",
    )

    # Session-start prime digest settings (see cognition/prime.py PrimeConfig).
    # Defaults here MUST equal PrimeConfig's defaults — a Settings build failure
    # falls back to PrimeConfig() and must degrade to the same trimmed output.
    prime_constraint_limit: int = Field(
        default=5,
        description="Max constraints shown in the session-start prime digest",
    )
    prime_task_cap: int = Field(
        default=5,
        description="Max open tasks shown in the session-start prime digest",
    )
    prime_pattern_limit: int = Field(
        default=3,
        description="Max recent patterns shown in the session-start prime digest",
    )
    prime_decision_limit: int = Field(
        default=3,
        description="Max recent decisions shown in the session-start prime digest",
    )
    prime_incident_days: int = Field(
        default=14,
        description="Incident lookback window (days) for the session-start prime digest",
    )
    prime_summary_maxlen: int = Field(
        default=110,
        description="Max chars per bullet summary in the prime digest (0 = no truncation)",
    )
    prime_incident_min_severity: Literal["critical", "high", "normal", "low"] = Field(
        default="high",
        description="Minimum incident severity shown in the prime digest (severity or higher)",
    )
    prime_workflow_limit: int = Field(
        default=5,
        description="Max workflow HEAD titles shown in the session-start prime digest",
    )

    @field_validator("repo_path", mode="before")
    @classmethod
    def validate_repo_path(cls, v: str | Path) -> Path:
        """Convert string to Path and validate it exists.

        WP-6 (b603f667130f) defense in depth: reject an explicitly-empty
        string BEFORE it reaches ``Path()`` — ``Path("")`` silently aliases
        to ``Path(".")`` (a pathlib quirk), which trivially exists and is a
        directory, so the checks below would NOT have caught it; the
        resolved cwd (the plugin's own install root under ``uv run
        --directory``, not the user's project) would have passed straight
        through. ``env_ignore_empty`` on ``model_config`` already stops an
        empty ``REPO_PATH`` env var from reaching this validator in the
        normal case; this catches it too if ``Settings`` is ever constructed
        with an explicit empty value some other way.
        """
        if isinstance(v, str) and not v.strip():
            raise ValueError(
                "repo_path resolved to an empty string — Path('') silently "
                "aliases to the current working directory (a pathlib quirk), "
                "which is almost certainly NOT the intended project root"
            )
        path = Path(v) if isinstance(v, str) else v
        if not path.exists():
            raise ValueError(f"Repository path does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Repository path is not a directory: {path}")
        return path.resolve()

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is valid."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return upper_v

    @property
    def effective_repo_name(self) -> str:
        """Get the repository name, defaulting to directory name."""
        return self.repo_name or self.repo_path.name

    @property
    def cognition_dir(self) -> Path:
        """Get the cognition graph storage directory (Git-committed)."""
        return self.repo_path / ".cognition"

    @property
    def cognition_chromadb_path(self) -> Path:
        """Get the cognition ChromaDB storage path (gitignored, regenerable)."""
        return self.repo_path / ".cognition" / "chromadb"


def setup_logging(level: str) -> None:
    """Configure logging for the application."""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
