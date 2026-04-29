"""Middleware: token + host header validation."""

from __future__ import annotations

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


class TokenMiddleware(BaseHTTPMiddleware):
    """Reject requests without a matching token.

    - GET /: requires ?token= query param.
    - /api/*: requires X-Dashboard-Token header.
    - /static/*: no token check (assets are not sensitive).

    Also enforces Host header is a 127.0.0.1 loopback to mitigate
    DNS-rebinding attacks against the localhost service.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if not (host.startswith("127.0.0.1:") or host.startswith("localhost:")):
            return JSONResponse({"error": "invalid host"}, status_code=403)

        path = request.url.path

        if path == "/":
            if request.query_params.get("token") != self._token:
                return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        elif path.startswith("/api/"):
            if request.headers.get("x-dashboard-token") != self._token:
                return JSONResponse({"error": "missing or invalid token"}, status_code=403)

        return await call_next(request)


def build_middleware(token: str) -> list[Middleware]:
    return [Middleware(TokenMiddleware, token=token)]
