"""Observation-only regression checks; no subprocesses, watches or disk cleanup.

Run with unittest to avoid the full suite's process fixtures and timeout plugin.
"""

import ast
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_cognition import _startup_timing, lifecycle


class IdentityLoggingTests(unittest.TestCase):
    def test_identity_survives_existing_flush_without_pre_yield_disk_io(self):
        evidence = {"verdict": "ok", "peer_pid": 123, "peer_image": "client.exe"}
        with (
            patch.object(_startup_timing, "breadcrumbs", []),
            patch.object(lifecycle.sys, "stderr", io.StringIO()),
            patch.dict(lifecycle.os.environ, {"VIBE_HARNESS": "codex"}),
            patch.object(Path, "mkdir") as mkdir,
            patch.object(Path, "write_text") as write,
        ):
            lifecycle._log_identity("pipe_peer_resolved", evidence)
            mkdir.assert_not_called()
            write.assert_not_called()
            _startup_timing.flush_to_disk()
            text = write.call_args.args[0]
            record = json.loads(text.split("lifecycle_identity ", 1)[1].rsplit(" t=", 1)[0])
            self.assertEqual(record["verdict"], "ok")
            self.assertEqual(record["peer_pid"], 123)
            self.assertEqual(record["peer_image"], "client.exe")
            self.assertEqual(record["harness"], "codex")
            self.assertEqual(record["pid"], lifecycle.os.getpid())
            self.assertIn("observed_at_unix", record)

    def test_broken_stderr_does_not_lose_in_memory_evidence(self):
        class BrokenStderr:
            def write(self, _text):
                raise OSError("closed")

        with (
            patch.object(_startup_timing, "breadcrumbs", []) as crumbs,
            patch.object(lifecycle.sys, "stderr", BrokenStderr()),
        ):
            lifecycle._log_identity("supervisor_pid_resolved", {"verdict": "supervisor_gone"})
            self.assertEqual(len(crumbs), 1)
            self.assertIn('"verdict": "supervisor_gone"', crumbs[0][0])

    def test_sidecar_persists_identity_before_processing_load_requests(self):
        # Inspect ordering without importing/running the sidecar or its watches.
        path = Path(__file__).parents[1] / "src/vibe_cognition/embeddings/sidecar.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [
            ast.unparse(n.value.func)
            for n in main.body
            if isinstance(n, (ast.Expr, ast.Assign)) and isinstance(n.value, ast.Call)
        ]
        i = calls.index("lifecycle.log_supervisor_identity")
        self.assertEqual(calls[i + 1], "_startup_timing.flush_to_disk")
        arm = next(
            n.value
            for n in main.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and ast.unparse(n.value.func) == "lifecycle.arm_ancestor_watch"
        )
        self.assertIn("extra_entries", [k.arg for k in arm.keywords])
        self.assertIn("supervisor_entry", ast.unparse(arm))
        self.assertIn("lifecycle.supervisor_watch_entry(supervisor_result)", ast.unparse(main))

    def test_server_arms_the_client_watch_on_claude_code_only(self):
        path = Path(__file__).parents[1] / "src/vibe_cognition/server.py"
        source = path.read_text(encoding="utf-8")
        gate = source.index("if harness.is_codex():")
        arm = source.index("lifecycle.client_watch_entry(peer_result)")
        self.assertLess(source.index("lifecycle.resolve_stdin_pipe_peer()"), gate)
        self.assertLess(gate, arm)
        self.assertLess(arm, source.index("lifecycle.arm_ancestor_watch(extra_entries=owner_entries)"))


if __name__ == "__main__":
    unittest.main()
