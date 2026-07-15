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

    # WP-Wedge-2 §W2-f: dispatch-stall self-forensics threshold, env-
    # overridable per existing conventions (DISPATCH_STALL_THRESHOLD).
    dispatch_stall_threshold: float = Field(
        default=30.0,
        description=(
            "Seconds an in-flight tool dispatch may run during the load "
            "window or a degraded state before an all-thread stack dump is "
            "written to stderr (once per process). Set via "
            "DISPATCH_STALL_THRESHOLD env var."
        ),
    )

    # WP-Sidecar (P0 endgame): the heavy torch/scipy/sentence_transformers
    # import lives in a child process the server supervises, never in-process
    # -- these bound that supervision, all env-overridable per existing
    # conventions.
    sidecar_load_timeout: float = Field(
        default=180.0,
        description=(
            "Seconds a single sidecar load attempt (spawn + mutex wait + "
            "model load) may run before the supervisor kills it as wedged. "
            "Generous by design -- the server no longer cares how slow a "
            "load is, only whether it's making progress. Set via "
            "SIDECAR_LOAD_TIMEOUT env var."
        ),
    )
    sidecar_request_timeout: float = Field(
        default=30.0,
        description=(
            "Seconds a single generate/ping request to a live sidecar may "
            "run before the supervisor treats it as failed and kills the "
            "sidecar. Set via SIDECAR_REQUEST_TIMEOUT env var."
        ),
    )
    sidecar_mutex_wait_timeout: float = Field(
        default=300.0,
        description=(
            "Seconds the sidecar waits to acquire the cross-process model-"
            "load mutex before proceeding WITHOUT it (stampede risk beats "
            "never loading). Set via SIDECAR_MUTEX_WAIT_TIMEOUT env var."
        ),
    )
    sidecar_max_retry_attempts: int = Field(
        default=3,
        description=(
            "In-budget kill+respawn attempts before the supervisor degrades "
            "(after which recovery is lazy-on-demand + slow periodic retry, "
            "never permanent). Set via SIDECAR_MAX_RETRY_ATTEMPTS env var."
        ),
    )
    sidecar_retry_backoff_seconds: float = Field(
        default=10.0,
        description=(
            "Seconds to wait between in-budget retry attempts. Set via "
            "SIDECAR_RETRY_BACKOFF_SECONDS env var."
        ),
    )
    sidecar_periodic_retry_interval: float = Field(
        default=300.0,
        description=(
            "Seconds between slow periodic recovery attempts once degraded "
            "-- production evidence says wedged loads eventually succeed, "
            "so recovery must never be permanent. Also the max delay before "
            "an on-demand recovery attempt is woken early by an actual "
            "embedding request. Set via SIDECAR_PERIODIC_RETRY_INTERVAL "
            "env var."
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

    # WP-P13n-2: per-git-user prime personalization. Global sections (constraints,
    # workflows, documents, patterns, decisions, incidents) are unaffected; these
    # knobs only govern the personalized task split + the new "Your Recent Activity"
    # section, both gated on `prime_personalize` and a resolvable git email.
    prime_personalize: Literal["auto", "on", "off"] = Field(
        default="auto",
        description=(
            "Prime personalization mode. 'auto': personalize only when the graph "
            "has more than one distinct stamped git-identity email (a solo graph "
            "gets the unchanged global digest); 'on': force personalized sections "
            "whenever the current git identity resolves an email; 'off': always "
            "the global digest. Set via PRIME_PERSONALIZE env var."
        ),
    )
    prime_your_tasks_cap: int = Field(
        default=5,
        description="Max tasks shown in 'Your Open Tasks' (personalized mode)",
    )
    prime_team_critical_cap: int = Field(
        default=5,
        description=(
            "Max tasks shown in 'Team Critical' (personalized mode): open "
            "critical/high tasks not already listed under 'Your Open Tasks'"
        ),
    )
    prime_your_episode_limit: int = Field(
        default=5,
        description="Max of your own recent episodes shown in 'Your Recent Activity'",
    )
    prime_your_decision_limit: int = Field(
        default=5,
        description="Max of your own recent decisions shown in 'Your Recent Activity'",
    )
    prime_your_discovery_limit: int = Field(
        default=5,
        description="Max of your own recent discoveries shown in 'Your Recent Activity'",
    )

    # WP-TC7: prime-triggered new-user onboarding.
    prime_onboard: bool = Field(
        default=True,
        description=(
            "Show the new-user onboarding notice in the session-start prime digest "
            "when the current git identity's email has no matching person node and "
            "hasn't declined (.cognition/onboard-declined). Kill switch — the "
            "per-human path is the decline file, not this env var."
        ),
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
