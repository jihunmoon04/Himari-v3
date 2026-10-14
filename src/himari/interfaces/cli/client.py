"""The CLI's transport: a client of the one local HIMARI Web server
(the HIMARI-v2 "terminal is a client of the Web runtime" design).

The CLI never opens the database (SQLite has a single writer, the server)
and never falls back to running HIMARI itself. Rules carried over from v2:

- Only ``http://127.0.0.1``, ``http://localhost`` or ``http://[::1]`` with an
  optional port is accepted, checked before any socket is opened.
- A mutation is never retried. If no connection could be made, nothing
  was sent (``ServerUnavailable``); if a POST was sent and its answer was
  lost, it may have committed (``OutcomeUnknown``).
- Responses are bounded in size.

Authentication is the bearer derived from the operator secret, which the
CLI reads from the private data directory it shares with the server.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class ClientError(Exception):
    """A CLI transport failure (never a reason to run HIMARI locally)."""


class BadServerURL(ClientError):
    pass


class ServerUnavailable(ClientError):
    """No connection was made: nothing was sent."""


class OutcomeUnknown(ClientError):
    """A request was sent but its answer was lost; a mutation may have committed."""


class ServerRefused(ClientError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def check_url(url: str) -> str:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise BadServerURL(f"invalid port in {url!r}") from None
    if (parts.scheme != "http" or parts.hostname not in _LOOPBACK or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise BadServerURL("the server URL must be http://127.0.0.1[:port] (or localhost / [::1]) with nothing else")
    host = f"[{parts.hostname}]" if parts.hostname == "::1" else parts.hostname
    return f"http://{host}" + (f":{port}" if port is not None else "")


class ServerClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @classmethod
    def connect(cls, url: str, bearer: str) -> ServerClient:
        return cls(httpx.Client(base_url=check_url(url), headers={"authorization": f"Bearer {bearer}"},
                                timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False))

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, body=body if body is not None else {})

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PATCH", path, body=body)

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> Any:
        try:
            with self._http.stream(method, path, params=params, json=body) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ClientError("the server's answer is too large")
                status = response.status_code
        except httpx.ConnectError:
            raise ServerUnavailable("the HIMARI server is not reachable (is it running?)") from None
        except httpx.HTTPError:
            if method == "GET":
                raise ServerUnavailable("no complete answer from the server") from None
            raise OutcomeUnknown("the request was sent but its answer was lost; it may have been applied") from None
        if status == 204:
            return None
        try:
            parsed = json.loads(bytes(data))
        except ValueError:
            raise ClientError(f"unreadable answer from the server (HTTP {status})") from None
        if status >= 400:
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            raise ServerRefused(status, str(error.get("code", "http_error")), str(error.get("message", "refused")))
        return parsed
