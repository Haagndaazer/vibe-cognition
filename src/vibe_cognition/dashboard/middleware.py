"""Middleware: token + host header validation."""

from __future__ import annotations

import secrets

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


class TokenMiddleware(BaseHTTPMiddleware):
    """Reject requests without a matching token.

    - GET / and document download links: require ?token= query param (browser
      navigations / <a download> can't set a header).
    - other /api/*: require X-Dashboard-Token header.
    - /static/*: no token check (assets are not sensitive).

    Also enforces Host header is a 127.0.0.1 loopback to mitigate
    DNS-rebinding attacks against the localhost service.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if not (host.startswith("127.0.0.1:") or host.startswith("localhost:") or host.startswith("[::1]:")):
            return JSONResponse({"error": "invalid host"}, status_code=403)

        path = request.url.path

        # Browser-navigated GETs (index + document download links) authenticate via
        # ?token= since an <a> can't send the header; the token is already in the
        # page URL. All other /api/* require the X-Dashboard-Token header.
        query_token_path = path == "/" or (
            path.startswith("/api/document/") and path.endswith("/download")
        )
        if query_token_path:
            if not self._token_ok(request.query_params.get("token")):
                return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        elif path.startswith("/api/") and not self._token_ok(
            request.headers.get("x-dashboard-token")
        ):
            return JSONResponse({"error": "missing or invalid token"}, status_code=403)

        return await call_next(request)

    def _token_ok(self, provided: str | None) -> bool:
        # Constant-time compare to avoid leaking the token via timing.
        return provided is not None and secrets.compare_digest(provided, self._token)


def build_middleware(token: str) -> list[Middleware]:
    return [Middleware(TokenMiddleware, token=token)]
