"""WP-Sidecar (P0 endgame) §2: the small, boring JSON-line protocol shared
between the server-side client (sidecar_client.py) and the sidecar entry
module (sidecar.py). Kept in one place so both sides can never drift on
field names.

Wire shape (newline-delimited JSON, one object per line):
  Request  (server -> sidecar): {"id": int, "op": "load"|"generate"|"ping",
                                  "args": {...}, "protocol_version": int}
                                 (protocol_version only required on "load" --
                                 the first message of a sidecar's life).
  Response (sidecar -> server): {"id": int, "ok": bool, "result": ..., "error": ...}
  Event    (sidecar -> server, unsolicited, no "id"):
                                 {"event": "lock_wait"|"lock_acquired"|
                                  "lock_acquired_abandoned"|"lock_wait_expired"}

Events are how the supervisor's load-timeout clock starts at ACQUISITION,
not submission (§2): "load(...)" may sit queued behind another session's
load for a while, and that queueing must not count as wedged. A response
line always has an "id" key; an event line never does -- the reader thread
tells them apart by that alone, no envelope/type field needed.
"""

from __future__ import annotations

import json
from typing import Any

# Bumped whenever the wire shape changes. Server and sidecar spawn from the
# same installed tree, but a running server may outlive a plugin update --
# the sidecar rejects a mismatched version on "load" so the client can
# respawn (picking up whatever's on disk NOW) instead of silently
# miscommunicating with stale-vs-fresh code on either end.
PROTOCOL_VERSION = 1

_LOCK_EVENTS = frozenset(
    {"lock_wait", "lock_acquired", "lock_acquired_abandoned", "lock_wait_expired"}
)


def encode_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def decode_line(line: str) -> dict[str, Any]:
    return json.loads(line)


def is_event(obj: dict[str, Any]) -> bool:
    return "id" not in obj and "event" in obj


def make_request(request_id: int, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"id": request_id, "op": op, "args": args or {}}
    if op == "load":
        req["protocol_version"] = PROTOCOL_VERSION
    return req


def make_response(request_id: int, result: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"id": request_id, "ok": error is None, "result": result, "error": error}


def make_event(name: str) -> dict[str, Any]:
    assert name in _LOCK_EVENTS, f"unknown protocol event: {name!r}"
    return {"event": name}
