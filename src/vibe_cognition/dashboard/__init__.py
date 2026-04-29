"""Local web dashboard for the cognition graph."""

from .server import build_app, run_dashboard_blocking, start_dashboard

__all__ = ["build_app", "run_dashboard_blocking", "start_dashboard"]
