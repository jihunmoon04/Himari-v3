"""HTTP transport security (adapted from HIMARI-v2 ``interfaces/web/security.py``).

Outer → inner: SecurityHeaders → TrustedHost (Starlette) → OriginGuard →
RequestBodyLimit → routes. Plain ASGI middleware, no knowledge of HIMARI.

Origin guard (v3 change): a state-changing request must carry an allowed
``Origin`` unless it authenticates with ``Authorization`` and carries no
session cookie — a browser cannot attach a bearer header cross-site
without CORS (which this server never enables), while cookies it attaches
automatically.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from starlette.datastructures import Headers
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 262_144  # 256 KiB, as in v2
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_COOKIE = "himari_session"

# No 'unsafe-inline'/'unsafe-eval': the UI uses external JS/CSS and
# addEventListener wiring only.
_CSP = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", _CSP.encode("latin-1")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)


def page_csp(frame_origins: Iterable[str] = ()) -> str:
    """The page's CSP; ``frame_origins``: where Tool views are framed from
    (the sandbox origins, V3-P58). Nothing else may be framed."""
    origins = " ".join(sorted(frame_origins))
    return f"{_CSP}; frame-src {origins}" if origins else _CSP


def error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")


async def _send_error(send: Send, status: int, code: str, message: str) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": error_body(code, message), "more_body": False})


class SecurityHeadersMiddleware:
    """Adds the fixed headers to every response, rejections included; API
    responses are never cached."""

    def __init__(self, app: ASGIApp, csp: str = _CSP) -> None:
        self._app = app
        self._headers = ((b"content-security-policy", csp.encode("latin-1")), *SECURITY_HEADERS[1:])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        is_api = scope.get("path", "").startswith("/api/")

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []), *self._headers]
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, with_headers)


def _has_session_cookie(headers: Headers) -> bool:
    return any(part.strip().startswith(f"{SESSION_COOKIE}=") for part in headers.get("cookie", "").split(";"))


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self._app = app
        self._allowed = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATION_METHODS:
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        bearer_only = "authorization" in headers and not _has_session_cookie(headers)
        if not bearer_only and headers.get("origin") not in self._allowed:
            await _send_error(send, 403, "origin_forbidden", "This request's Origin may not perform this action.")
            return
        await self._app(scope, receive, send)


class BodyLimitMiddleware:
    """Starlette's own body limit, with its 413 rewritten into the JSON
    error envelope."""

    def __init__(self, app: ASGIApp, max_body_size: int = MAX_REQUEST_BODY_BYTES,
                 exempt: Callable[[str, str], bool] | None = None) -> None:
        self._app = app
        self._inner = RequestBodyLimitMiddleware(app, max_body_size)
        self._exempt = exempt  # (method, path) → the route holds the body to its own limit (e.g. uploads)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        if self._exempt is not None and self._exempt(scope.get("method", ""), scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        state: dict[str, Any] = {"rewriting": False}

        async def rewriting(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["rewriting"] = message["status"] == 413
                if not state["rewriting"]:
                    await send(message)
                return
            if message["type"] == "http.response.body" and state["rewriting"]:
                if not message.get("more_body", False):
                    state["rewriting"] = False
                    await _send_error(send, 413, "request_too_large", "The request body is too large.")
                return
            await send(message)

        await self._inner(scope, receive, rewriting)
