"""The CLI's transport: a client of the one local HIMARI Web server
(the HIMARI-v2 "terminal is a client of the Web runtime" design).

The CLI never opens the database (SQLite has a single writer, the server)
and never falls back to running HIMARI itself. Rules carried over from v2:

- Only ``http://127.0.0.1``, ``http://localhost`` or ``http://[::1]`` with an
  optional port is accepted, checked before any socket is opened.
- A mutation is never retried. If no connection could be made, nothing
  was sent (``ServerUnavailable``). Once a mutation was sent, anything short
  of a readable answer — a lost connection, a success status whose body is
  unreadable or over the size bound, or a server error (5xx, which may
  follow a commit) — means it may have been applied (``OutcomeUnknown``).
  Only a 4xx is a definite refusal.
- Responses are bounded in size; a read over the bound raises
  ``ResponseTooLarge`` so the caller can ask for a smaller page.

Authentication is the bearer derived from the operator secret, which the
CLI reads from the private data directory it shares with the server.

Files (V3-P66 §9): ``upload`` sends one file as the request body (a refusal
the server states — even with a 5xx such as ``storage_full`` — is definite);
``download`` streams a file into a target the caller names from the
server's ``Content-Disposition``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
UPLOAD_TIMEOUT_SECONDS = 300.0  # a file of tens of MB, on a slow disk
DEFINITE_FILE_REFUSALS = frozenset({503, 507})  # upload_interrupted, storage_full: nothing was kept


def disposition_name(header: str) -> str | None:
    """The exact name in ``filename*=UTF-8''…`` (V3-P66 §7.2), if any."""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "filename*" and value.lower().startswith("utf-8''"):
            return unquote(value[len("utf-8''"):]) or None
    return None
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class ClientError(Exception):
    """A CLI transport failure (never a reason to run HIMARI locally)."""


class BadServerURL(ClientError):
    pass


class ServerUnavailable(ClientError):
    """No connection was made: nothing was sent."""


class OutcomeUnknown(ClientError):
    """A request was sent but its answer was lost; a mutation may have committed."""


class ResponseTooLarge(ClientError):
    """A read's answer exceeded the size bound (nothing was changed)."""


class ServerRefused(ClientError):
    def __init__(self, status: int, code: str, message: str, file_id: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code
        self.file_id = file_id  # attachment_refused: the file refused (V3-P66 §9)


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

    def post(self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """``timeout``: for a request the server may take longer to answer."""
        return self._request("POST", path, body=body if body is not None else {}, timeout=timeout)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PATCH", path, body=body)

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PUT", path, body=body)

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("DELETE", path, params=params)

    def upload(self, path: str, name: str, source: BinaryIO, content_type: str) -> Any:
        """One file as the body. 503 ``upload_interrupted`` and 507
        ``storage_full`` are refusals the server states: nothing was kept."""
        return self._request("POST", path, params={"name": name}, content=source,
                             headers={"content-type": content_type}, timeout=UPLOAD_TIMEOUT_SECONDS,
                             definite=DEFINITE_FILE_REFUSALS)

    def download(self, path: str, target: Callable[[str | None], tuple[str, BinaryIO]]) -> str:
        """Stream a file into ``target(name)``'s file (name from the server's
        ``filename*``); the path written. A failure part way removes it."""
        try:
            with self._http.stream("GET", path, timeout=UPLOAD_TIMEOUT_SECONDS) as response:
                if response.status_code != 200:
                    data = response.read()[:MAX_RESPONSE_BYTES]
                    try:
                        error = json.loads(data).get("error", {})
                    except (ValueError, AttributeError):
                        error = {}
                    raise ServerRefused(response.status_code, str(error.get("code", "http_error")),
                                        str(error.get("message", "refused")))
                written, sink = target(disposition_name(response.headers.get("content-disposition", "")))
                try:
                    with sink:
                        for chunk in response.iter_bytes():
                            sink.write(chunk)
                except BaseException as error:
                    try:
                        os.unlink(written)
                    except OSError:
                        pass
                    if isinstance(error, OSError):  # e.g. a full disk: said, not a traceback
                        raise ClientError(f"cannot write the file: {error.strerror or error}") from None
                    raise
                return written
        except httpx.ConnectError:
            raise ServerUnavailable("the HIMARI server is not reachable (is it running?)") from None
        except httpx.HTTPError:
            raise ServerUnavailable("no complete answer from the server") from None

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None, timeout: float | None = None, content: Any = None,
                 headers: dict[str, str] | None = None, definite: frozenset[int] = frozenset()) -> Any:
        mutation = method != "GET"
        unknown = "the request was sent but its answer {}; it may have been applied"
        too_large = False
        try:
            with self._http.stream(method, path, params=params, json=body if content is None else None,
                                   content=content, headers=headers,
                                   timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout) as response:
                status = response.status_code
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        too_large = True
                        break
        except httpx.ConnectError:
            raise ServerUnavailable("the HIMARI server is not reachable (is it running?)") from None
        except httpx.HTTPError:
            if not mutation:
                raise ServerUnavailable("no complete answer from the server") from None
            raise OutcomeUnknown(unknown.format("was lost")) from None
        if mutation and status >= 500 and status not in definite:
            raise OutcomeUnknown(unknown.format(f"was a server error (HTTP {status})"))
        if too_large:
            if mutation:
                raise OutcomeUnknown(unknown.format("was too large to read"))
            raise ResponseTooLarge("the server's answer is too large")
        if status == 204 and not data:
            return None
        try:
            parsed = json.loads(bytes(data))
        except ValueError:
            if mutation and status < 400:
                raise OutcomeUnknown(unknown.format(f"was unreadable (HTTP {status})")) from None
            raise ClientError(f"unreadable answer from the server (HTTP {status})") from None
        if status >= 400:
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            file_id = error.get("file_id")
            raise ServerRefused(status, str(error.get("code", "http_error")), str(error.get("message", "refused")),
                                file_id if isinstance(file_id, str) else None)
        return parsed
