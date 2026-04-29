"""Standalone CLI entry point for the dashboard.

Lets a developer iterate on the dashboard against any project's
`.cognition/` directory without reinstalling the MCP plugin.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from ..config import Settings, setup_logging

logger = logging.getLogger(__name__)


def _build_lifespan_ctx(repo_path: Path, load_embeddings: bool) -> dict[str, Any]:
    """Construct the same lifespan_ctx shape the MCP server builds."""
    from ..cognition import CognitionStorage
    from ..embeddings import ChromaDBStorage

    os.environ["REPO_PATH"] = str(repo_path)
    config = Settings()

    cognition_storage = CognitionStorage(config.cognition_dir)
    cognition_embedding_storage = ChromaDBStorage(
        persist_directory=config.cognition_chromadb_path,
        collection_name="cognition_embeddings",
    )

    ready_event = threading.Event()
    ctx: dict[str, Any] = {
        "config": config,
        "cognition_storage": cognition_storage,
        "cognition_embedding_storage": cognition_embedding_storage,
        "embedding_generator": None,
        "cognition_curator": None,
        "embedding_ready": ready_event,
        "embedding_error": None,
    }

    if load_embeddings:
        from ..embeddings import EmbeddingGenerator

        logger.info(f"Loading embedding model ({config.embedding_backend})…")
        try:
            ctx["embedding_generator"] = EmbeddingGenerator.from_config(config)
            ready_event.set()
            logger.info("Embedding model ready")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            ctx["embedding_error"] = str(e)
            ready_event.set()

    return ctx


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-dashboard",
        description="Local dashboard for the cognition graph.",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="Path to the project repo (default: $REPO_PATH or CWD)",
    )
    parser.add_argument("--port", type=int, default=7842)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip loading the embedding model — search will return 503 (faster startup for UI work)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)

    repo_path = args.repo_path or Path(os.environ.get("REPO_PATH") or Path.cwd())
    repo_path = repo_path.resolve()

    if not repo_path.exists() or not repo_path.is_dir():
        print(f"error: repo path is not a directory: {repo_path}", file=sys.stderr)
        return 2

    cognition_dir = repo_path / ".cognition"
    if not cognition_dir.exists():
        print(
            f"error: {cognition_dir} not found. "
            f"Initialize the project with the cognition MCP server first.",
            file=sys.stderr,
        )
        return 2

    logger.info(f"Repo path: {repo_path}")
    ctx = _build_lifespan_ctx(repo_path, load_embeddings=not args.no_embeddings)

    from .server import run_dashboard_blocking

    try:
        run_dashboard_blocking(
            ctx,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
