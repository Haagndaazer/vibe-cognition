"""Vibe Cognition — MCP server for project knowledge graphs."""

import os

# Must precede any numpy/scipy import: OpenBLAS sizes its buffers at load.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
