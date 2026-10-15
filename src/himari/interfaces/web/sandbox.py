"""The sandbox origin for Tool views (MCP Apps, V3-P58, docs/mcp-apps-v0.1.md §3.4).

A second, minimal server on its own port. It serves a fixed proxy page and
its script, nothing else: no API, no session, no data. The proxy frames a
view (sent by the page over postMessage) in an opaque ``srcdoc`` iframe, and
the view inherits the proxy's CSP — so the proxy's response header *is* the
view's CSP.

That CSP comes from the page's API as a signed parameter:

- ``p``: base64url of the canonical JSON ``{"v": 1, "csp": {...}, "host":
  <the page origin>, "exp": <unix seconds>}``;
- ``sig``: HMAC-SHA256 of ``p`` under a key made when the process starts and
  kept in memory. The operator's policy changes only when the process
  starts, so a restart voids every URL signed under an older policy.

A bad or expired signature, an unknown version or a malformed payload is a
400; the proxy is never served with a CSP HIMARI did not sign.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

VERSION = 1
LIFETIME_SECONDS = 600
MAX_PARAM_CHARS = 8 * 1024
# An origin as it may appear in a CSP source list (the MCP config checks the
# operator's entries; this checks them again before they become a header).
_ORIGIN = re.compile(r"(https|wss|http|ws)://(\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::[0-9]{1,5})?|"
                     r"http://\[[0-9a-f:]+\](?::[0-9]{1,5})?")
_HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff"}


@dataclass(frozen=True)
class SandboxGrant:
    """What a signed URL allows: a proxy with this CSP, framed by ``host``."""

    host: str
    connect_domains: tuple[str, ...] = ()
    resource_domains: tuple[str, ...] = ()


class SandboxSigner:
    def __init__(self, key: bytes | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self._key = key if key is not None else secrets.token_bytes(32)
        self._clock = clock

    def query(self, grant: SandboxGrant) -> str:
        """``p=…&sig=…`` for the proxy URL; valid for LIFETIME_SECONDS."""
        payload = {"v": VERSION, "host": grant.host, "exp": int(self._clock()) + LIFETIME_SECONDS,
                   "csp": {"connect_domains": list(grant.connect_domains),
                           "resource_domains": list(grant.resource_domains)}}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        p = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"p={p}&sig={self._sign(p)}"

    def verify(self, p: str, sig: str) -> SandboxGrant | None:
        if len(p) > MAX_PARAM_CHARS or not hmac.compare_digest(self._sign(p), sig):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        except (binascii.Error, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != VERSION:
            return None
        exp, host, csp = payload.get("exp"), payload.get("host"), payload.get("csp")
        if not isinstance(exp, int) or exp < self._clock() or not isinstance(host, str) or not isinstance(csp, dict):
            return None
        connect, resource = _origins(csp.get("connect_domains")), _origins(csp.get("resource_domains"))
        if connect is None or resource is None or _origins([host]) is None:
            return None
        return SandboxGrant(host, connect, resource)

    def _sign(self, p: str) -> str:
        return hmac.new(self._key, p.encode(), hashlib.sha256).hexdigest()


def _origins(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) and _ORIGIN.fullmatch(v) for v in value):
        return None
    return tuple(value)


def view_csp(grant: SandboxGrant) -> str:
    """The MCP Apps CSP template (2026-01-26) with the granted origins.
    No nested frames, no other base URI, framed only by the page."""
    resource = "".join(f" {d}" for d in grant.resource_domains)
    connect = "".join(f" {d}" for d in grant.connect_domains)
    return (f"default-src 'none'; script-src 'self' 'unsafe-inline'{resource}; "
            f"style-src 'self' 'unsafe-inline'{resource}; connect-src 'self'{connect}; "
            f"img-src 'self' data:{resource}; font-src 'self'{resource}; media-src 'self' data:{resource}; "
            f"frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'none'; "
            f"frame-ancestors {grant.host}")


def create_sandbox_app(allowed_hosts: Sequence[str], signer: SandboxSigner) -> Starlette:
    static = resources.files("himari.interfaces.web") / "static"
    page = (static / "sandbox.html").read_text()
    script = (static / "sandbox.js").read_bytes()

    async def proxy(request: Request) -> Response:
        grant = signer.verify(request.query_params.get("p", ""), request.query_params.get("sig", ""))
        if grant is None:
            return Response("This view link is not valid (it may have expired).", status_code=400,
                            media_type="text/plain; charset=utf-8", headers=_HEADERS)
        body = page.replace("{HOST}", grant.host)  # an origin checked by the pattern: no quotes or markup
        return Response(body, media_type="text/html; charset=utf-8",
                        headers={**_HEADERS, "content-security-policy": view_csp(grant)})

    async def proxy_script(request: Request) -> Response:
        return Response(script, media_type="text/javascript; charset=utf-8", headers=_HEADERS)

    async def not_found(request: Request, exc: Exception) -> Response:
        return Response("", status_code=404, headers=_HEADERS)

    handlers: Mapping[Any, Callable[..., Any]] = {404: not_found, 405: not_found}
    return Starlette(
        routes=[Route("/sandbox", proxy), Route("/sandbox.js", proxy_script)],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=sorted(allowed_hosts))],
        exception_handlers=handlers,
    )
