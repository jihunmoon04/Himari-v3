"""A minimal MCP client over Streamable HTTP (the initialize-handshake revisions, 2025-03-26 .. 2025-11-25).

Only what HIMARI uses: ``initialize``, ``tools/list``, ``tools/call`` and
``resources/read`` (a Tool's view, MCP Apps). The client declares no
capabilities of its own, so a server has no reason to send requests back;
any it sends inside a response stream, and every notification, is ignored.
For a server whose views are shown it declares the MCP Apps extension
(``capabilities.extensions``), which tells the server it may describe
views; that asks for nothing back either.

Each JSON-RPC request is one ``POST`` answered by ``application/json`` or
a ``text/event-stream`` that carries the response. The session id the
server assigns is sent on later requests; when the server forgets it (404)
the client starts a new session and sends the request once more — the
server did not process a request for an unknown session.

Errors say what is known about a ``tools/call``:

- ``McpRefused``: no connection was made, or the server rejected the
  request itself — an HTTP status in ``REFUSED_STATUSES`` (authentication,
  routing, format: decided before a Tool runs) or a JSON-RPC parse /
  invalid request / unknown method / invalid params error: the Tool did
  not run.
- ``McpUncertain``: anything else after the request was sent — a timeout,
  a lost connection, any other status (e.g. 408 or 429, which do not prove
  the Tool did not run, a redirect, 5xx), an unreadable or oversized
  response, any other JSON-RPC error: the Tool may have run.

Size bounds are counted on the bytes as they arrive, before any line
splitting or decoding (an SSE stream is split into lines here, never by a
buffer that grows until a newline). Responses are asked for uncompressed
and one that is compressed anyway is not used, so no decompression can
multiply what arrives. One response is at most ``MAX_RESPONSE_BYTES``, a whole
``tools/list`` at most ``MAX_LIST_BYTES`` and ``MAX_LISTED_TOOLS``; a
notification's response body is never read.

Credentials: the token is sent only as ``Authorization: Bearer`` to the
configured URL. Redirects are not followed and proxy settings from the
environment are not used, so the token never goes anywhere else. Nothing
here records requests or headers.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from himari.common import JsonValue

PROTOCOL_VERSION = "2025-11-25"  # the newest revision with the initialize handshake
SUPPORTED_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26"})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_LIST_BYTES = 8 * 1024 * 1024  # every tools/list page together
MAX_LISTED_TOOLS = 1000
_LINE_END = re.compile(rb"[\r\n]")
# Statuses that reject the request itself, before any Tool could run.
REFUSED_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 413, 415})
CONNECT_SECONDS = 5.0
MAX_TOOL_PAGES = 20
CLIENT_INFO = {"name": "himari", "version": "3"}
UI_EXTENSION = "io.modelcontextprotocol/ui"  # MCP Apps (V3-P58)
UI_MIME_TYPE = "text/html;profile=mcp-app"


class McpError(Exception):
    pass


class McpRefused(McpError):
    """The server did not run the request."""


# JSON-RPC errors that mean the request was not carried out.
_REFUSALS = frozenset({-32700, -32600, -32601, -32602})


class McpUncertain(McpError):
    """The request was sent; whether it took effect is unknown."""


class _SessionExpired(Exception):
    pass


class McpClient:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        ui: bool = False,  # declare the MCP Apps extension (V3-P58)
    ) -> None:
        headers = {"accept": "application/json, text/event-stream", "accept-encoding": "identity"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._url = url
        self._http = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(timeout_seconds, connect=CONNECT_SECONDS),
            follow_redirects=False, trust_env=False, transport=transport,
        )
        self._capabilities: dict[str, Any] = (
            {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME_TYPE]}}} if ui else {})
        self._session: str | None = None
        self._version: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()  # one (re)initialization at a time

    async def aclose(self) -> None:
        await self._http.aclose()

    async def initialize(self) -> Mapping[str, Any]:
        """Starts a session; returns the server's ``initialize`` result."""
        async with self._lock:
            return await self._initialize()

    async def list_tools(self) -> list[Mapping[str, Any]]:
        tools: list[Mapping[str, Any]] = []
        cursor: str | None = None
        size = 0
        for _ in range(MAX_TOOL_PAGES):
            result = await self.request("tools/list", {} if cursor is None else {"cursor": cursor})
            page = result.get("tools")
            if not isinstance(page, list) or not all(isinstance(t, dict) for t in page):
                raise McpRefused("tools/list answered without a list of tools")
            size += len(json.dumps(page))
            if size > MAX_LIST_BYTES or len(tools) + len(page) > MAX_LISTED_TOOLS:
                raise McpRefused(f"tools/list is larger than {MAX_LISTED_TOOLS} tools or {MAX_LIST_BYTES} bytes")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None or cursor == "":
                return tools
            if not isinstance(cursor, str):  # a broken cursor must not look like the end of the list
                raise McpRefused("tools/list answered with an invalid nextCursor")
        raise McpRefused(f"tools/list has more than {MAX_TOOL_PAGES} pages")

    async def call_tool(self, name: str, arguments: Mapping[str, JsonValue]) -> Mapping[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": dict(arguments)})

    async def read_resource(self, uri: str) -> Mapping[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._version is None:
            async with self._lock:
                if self._version is None:  # nobody started it meanwhile
                    await self._initialize()
        session = self._session
        try:
            return await self._rpc(method, params)
        except _SessionExpired:
            async with self._lock:
                if self._session == session:  # nobody renewed it meanwhile
                    await self._initialize()
            try:
                return await self._rpc(method, params)
            except _SessionExpired:
                raise McpRefused("the server does not keep a session") from None

    # --- internals ---------------------------------------------------------

    async def _initialize(self) -> Mapping[str, Any]:
        self._session, self._version = None, None
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": self._capabilities, "clientInfo": CLIENT_INFO,
        }, initializing=True)
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise McpRefused(f"unsupported MCP protocol version {version!r}")
        self._version = version
        await self._notify("notifications/initialized")
        return result

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._version is not None:
            headers["mcp-protocol-version"] = self._version
        if self._session is not None:
            headers["mcp-session-id"] = self._session
        return headers

    async def _notify(self, method: str) -> None:
        body = json.dumps({"jsonrpc": "2.0", "method": method}).encode()
        try:
            async with self._http.stream("POST", self._url, content=body, headers=self._headers()) as response:
                status = response.status_code  # the body is never read
        except httpx.HTTPError as error:
            raise McpRefused(f"{method}: {type(error).__name__}") from None
        if status >= 400:
            raise McpRefused(f"{method}: HTTP {status}")

    async def _rpc(self, method: str, params: Mapping[str, Any], *, initializing: bool = False) -> Mapping[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        sent_session = self._session
        connected = False
        try:
            async with self._http.stream("POST", self._url, content=body, headers=self._headers()) as response:
                connected = True
                status = response.status_code
                if status == 404 and sent_session is not None:
                    raise _SessionExpired
                if status in REFUSED_STATUSES:
                    raise McpRefused(f"{method}: HTTP {status}")
                if 300 <= status < 400:  # not followed: it could carry the token elsewhere
                    raise McpUncertain(f"{method}: HTTP {status} (redirects are not followed)")
                if status != 200:
                    raise McpUncertain(f"{method}: HTTP {status}")
                if initializing:
                    session = response.headers.get("mcp-session-id")
                    self._session = session if session and session.isascii() and session.isprintable() else None
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise McpUncertain(f"{method}: a compressed response was not asked for")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    message = await self._from_stream(response, request_id)
                elif kind == "application/json":
                    message = _parse(await _read_bounded(response))
                else:
                    raise McpUncertain(f"{method}: unexpected content type {kind or 'none'}")
        except httpx.ConnectError as error:
            raise McpRefused(f"{method}: cannot connect ({type(error).__name__})") from None
        except httpx.HTTPError as error:
            if not connected and isinstance(error, httpx.ConnectTimeout):
                raise McpRefused(f"{method}: cannot connect (timeout)") from None
            raise McpUncertain(f"{method}: no complete answer ({type(error).__name__})") from None
        return _result(message, request_id, method)

    async def _from_stream(self, response: httpx.Response, request_id: int) -> object:
        """The first message in the stream that answers ``request_id``."""
        data: list[bytes] = []
        async for line in _bounded_lines(response):
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif line == b"" and data:
                message = _parse(b"\n".join(data))
                data = []
                if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                    return message
        raise McpUncertain("the response stream ended without an answer")


async def _bounded_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """The stream's lines without their ending (CRLF, LF or CR, as SSE
    allows), reading at most MAX_RESPONSE_BYTES raw bytes in all.

    Linear in what arrives, however it is chunked: each byte is searched
    once (``scan`` marks where the last search stopped), and the buffer
    keeps only the unfinished line. A CR that ends the buffer may be the
    first half of a CRLF, so it waits for the next byte — or for the end of
    the stream, where it ends its line."""
    size = 0
    pending = bytearray()
    scan = 0  # pending[:scan] holds no line ending
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise McpUncertain("the response stream is too large")
        pending.extend(chunk)
        start = 0
        while (found := _LINE_END.search(pending, scan)) is not None:
            cut = found.start()
            if pending[cut] == 0x0D and cut == len(pending) - 1:
                scan = cut  # CR or CRLF: decided by the next byte
                break
            yield bytes(pending[start:cut])
            start = scan = cut + (2 if pending[cut : cut + 2] == b"\r\n" else 1)
        else:
            scan = len(pending)
        if start:
            del pending[:start]  # only bytes after the last ending, all from this chunk
            scan -= start
    if pending.endswith(b"\r"):
        yield bytes(pending[:-1])  # a CR at the very end ends its line
    elif pending:
        yield bytes(pending)  # an unfinished last line (dispatches nothing by itself)


async def _read_bounded(response: httpx.Response) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():  # identity only (checked): the bytes as they arrive
        data.extend(chunk)
        if len(data) > MAX_RESPONSE_BYTES:
            raise McpUncertain("the response is too large")
    return bytes(data)


def _parse(data: bytes) -> object:
    try:
        return json.loads(data)
    except (ValueError, RecursionError):  # RecursionError: nested deeper than the parser can follow
        raise McpUncertain("the response is not usable JSON") from None


def _result(message: object, request_id: int, method: str) -> Mapping[str, Any]:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise McpUncertain(f"{method}: the response does not answer the request")
    if "error" in message:
        error = message["error"]
        code = error.get("code") if isinstance(error, dict) else None
        text = error.get("message") if isinstance(error, dict) else None
        failure = McpRefused if code in _REFUSALS else McpUncertain
        raise failure(f"{method}: server error {code}: {str(text)[:200]}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpUncertain(f"{method}: the response has no result object")
    return result
