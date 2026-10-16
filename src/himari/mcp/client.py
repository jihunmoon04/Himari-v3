"""A minimal MCP client (the initialize-handshake revisions, 2025-03-26 .. 2025-11-25).

Three layers (V3-P59 §5.2): ``McpClient`` holds the operations HIMARI uses
over a ``Channel`` (how messages travel: ``HttpChannel`` here, a stdio
channel beside it); ``revision`` says how a connection starts; ``jsonrpc``
matches answers to requests and says what an answer means.

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

The rest of this note is about ``HttpChannel`` (Streamable HTTP).

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

Cancelling is best effort (MCP; V3-P59 §5.5): when a caller stops waiting
for a request that was sent — Stop, or a read or write timeout — the channel
sends ``notifications/cancelled`` with that request's id, session id and
protocol version, as they were when it was sent (a renewed session does not
change them). A background task sends it, bounded by ``CANCEL_SECONDS``, so
the Stop does not wait; whatever comes of it, the call's verdict stays as
above. ``initialize`` is never cancelled. Neither is a request that was not
sent: the HTTP transport reports, through httpcore's trace events, when it
starts sending a request's headers; a Stop before that (waiting for a pool
slot, connecting, TLS) sends nothing. A transport that reports no such
events (one passed in, as in tests) cannot say, and its requests count as
sent. The transport spec says a dropped connection is not a cancellation:
the notification is. A request that timed out waiting for a pool slot was
never sent: refused, not uncertain.

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

from typing import Protocol

from himari.common import JsonValue
from himari.mcp.jsonrpc import McpError as McpError
from himari.mcp.jsonrpc import McpRefused as McpRefused
from himari.mcp.jsonrpc import McpUncertain as McpUncertain
from himari.mcp.jsonrpc import notification, parse, request, result_of
from himari.mcp.revision import CLIENT_INFO as CLIENT_INFO
from himari.mcp.revision import REVISION

PROTOCOL_VERSION = REVISION.version
SUPPORTED_VERSIONS = REVISION.supported
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_LIST_BYTES = 8 * 1024 * 1024  # every tools/list page together
MAX_LISTED_TOOLS = 1000
MAX_LISTED_PROMPTS = 100  # per server (V3-P60 §4)
_LINE_END = re.compile(rb"[\r\n]")
# Statuses that reject the request itself, before any Tool could run.
REFUSED_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 413, 415})
CONNECT_SECONDS = 5.0
CANCEL_SECONDS = 5.0  # sending one cancellation notification
MAX_TOOL_PAGES = 20
UI_EXTENSION = "io.modelcontextprotocol/ui"  # MCP Apps (V3-P58)
UI_MIME_TYPE = "text/html;profile=mcp-app"


class _SessionExpired(Exception):
    pass


def capabilities(ui: bool) -> dict[str, Any]:
    """What HIMARI declares: nothing, or the MCP Apps extension for a server
    whose views are shown (V3-P58), which asks for nothing back either."""
    return {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME_TYPE]}}} if ui else {}


class Channel(Protocol):
    """How one server's messages travel. ``start`` connects (and returns the
    server's ``initialize`` result); ``request`` sends one request and
    returns its result, starting or renewing the connection as needed.
    ``key`` is the calling Conversation (a stdio server may run a process
    per Conversation); ``take_restart`` says, once, that the key's calls
    moved to a new connection since its previous answer."""

    async def start(self) -> Mapping[str, Any]: ...

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]: ...

    def take_restart(self, key: str | None) -> bool: ...

    async def aclose(self) -> None: ...


class McpClient:
    """The operations HIMARI uses, over one channel."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    async def aclose(self) -> None:
        await self._channel.aclose()

    async def initialize(self) -> Mapping[str, Any]:
        """Starts a session; returns the server's ``initialize`` result."""
        return await self._channel.start()

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

    async def list_prompts(self) -> list[Mapping[str, Any]]:
        """Every prompt the server offers (V3-P60 §4), within the bounds."""
        prompts: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = await self.request("prompts/list", {} if cursor is None else {"cursor": cursor})
            page = result.get("prompts")
            if not isinstance(page, list) or not all(isinstance(p, dict) for p in page):
                raise McpRefused("prompts/list answered without a list of prompts")
            if len(prompts) + len(page) > MAX_LISTED_PROMPTS:
                raise McpRefused(f"prompts/list is larger than {MAX_LISTED_PROMPTS} prompts")
            prompts.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None or cursor == "":
                return prompts
            if not isinstance(cursor, str):
                raise McpRefused("prompts/list answered with an invalid nextCursor")
        raise McpRefused(f"prompts/list has more than {MAX_TOOL_PAGES} pages")

    async def get_prompt(self, name: str, arguments: Mapping[str, str], *, key: str | None = None) -> Mapping[str, Any]:
        """``prompts/get`` in the Conversation's session (``key``, V3-P60 C2)."""
        return await self._channel.request("prompts/get", {"name": name, "arguments": dict(arguments)}, key=key)

    async def call_tool(self, name: str, arguments: Mapping[str, JsonValue], *,
                        key: str | None = None) -> Mapping[str, Any]:
        return await self._channel.request("tools/call", {"name": name, "arguments": dict(arguments)}, key=key)

    def take_restart(self, key: str | None) -> bool:
        return self._channel.take_restart(key)

    async def read_resource(self, uri: str, *, key: str | None = None) -> Mapping[str, Any]:
        return await self.request("resources/read", {"uri": uri}, key=key)

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]:
        return await self._channel.request(method, params, key=key)


def http_client(url: str, token: str | None, *, timeout_seconds: float,
                transport: httpx.AsyncBaseTransport | None = None, ui: bool = False) -> McpClient:
    return McpClient(HttpChannel(url, token, timeout_seconds=timeout_seconds, transport=transport, ui=ui))


class HttpChannel:
    """Streamable HTTP: one POST per request; the session id the server
    assigns is sent on later requests and renewed once when forgotten."""

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
        # httpx's own transport (httpcore) reports when a request starts going out.
        self._traced = transport is None or isinstance(transport, httpx.AsyncHTTPTransport)
        self._http = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(timeout_seconds, connect=CONNECT_SECONDS),
            follow_redirects=False, trust_env=False, transport=transport,
        )
        self._capabilities = capabilities(ui)
        self._session: str | None = None
        self._version: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()  # one (re)initialization at a time
        self._cancelling: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        await asyncio.gather(*self._cancelling, return_exceptions=True)  # each bounded by CANCEL_SECONDS
        await self._http.aclose()

    async def start(self) -> Mapping[str, Any]:
        async with self._lock:
            return await self._initialize()

    def take_restart(self, key: str | None) -> bool:
        return False  # a renewed HTTP session is the same server process: nothing to tell

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]:
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
        result = await self._rpc(REVISION.initialize, REVISION.initialize_params(self._capabilities),
                                 initializing=True)
        self._version = REVISION.accept(result)
        await self._notify(REVISION.initialized)
        return result

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._version is not None:
            headers["mcp-protocol-version"] = self._version
        if self._session is not None:
            headers["mcp-session-id"] = self._session
        return headers

    async def _notify(self, method: str) -> None:
        body = json.dumps(notification(method)).encode()
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
        body = json.dumps(request(request_id, method, params)).encode()
        sent_session, sent_version = self._session, self._version
        connected = False
        sending = [not self._traced]  # unknown on an untraced transport: counts as sent

        async def trace(event: str, info: Mapping[str, Any]) -> None:
            if event.endswith("send_request_headers.started"):
                sending[0] = True

        try:
            async with self._http.stream("POST", self._url, content=body, headers=self._headers(),
                                         extensions={"trace": trace}) as response:
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
                    message = parse(await _read_bounded(response))
                else:
                    raise McpUncertain(f"{method}: unexpected content type {kind or 'none'}")
        except httpx.ConnectError as error:
            raise McpRefused(f"{method}: cannot connect ({type(error).__name__})") from None
        except httpx.HTTPError as error:
            if not connected and isinstance(error, httpx.ConnectTimeout):
                raise McpRefused(f"{method}: cannot connect (timeout)") from None
            if isinstance(error, httpx.PoolTimeout):  # never left the client (PR #37 review C1)
                raise McpRefused(f"{method}: no free connection in time") from None
            if isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout)) and not initializing:
                self._cancel_later(request_id, sent_session, sent_version, "timed out")
            raise McpUncertain(f"{method}: no complete answer ({type(error).__name__})") from None
        except asyncio.CancelledError:
            if not initializing and sending[0]:  # MCP: initialize is never cancelled; nor what was not sent
                self._cancel_later(request_id, sent_session, sent_version, "cancelled by the user")
            raise
        return result_of(message, request_id, method)

    def _cancel_later(self, request_id: int, session: str | None, version: str | None, reason: str) -> None:
        task = asyncio.create_task(self._send_cancelled(request_id, session, version, reason))
        self._cancelling.add(task)
        task.add_done_callback(self._cancelling.discard)

    async def _send_cancelled(self, request_id: int, session: str | None, version: str | None, reason: str) -> None:
        """One best-effort notification, as the request was sent; its outcome
        changes nothing (202, 404 for a session gone, an error, a timeout)."""
        headers = {"content-type": "application/json"}
        if version is not None:
            headers["mcp-protocol-version"] = version
        if session is not None:
            headers["mcp-session-id"] = session
        body = json.dumps(notification("notifications/cancelled", {"requestId": request_id, "reason": reason})).encode()
        try:
            async with asyncio.timeout(CANCEL_SECONDS):
                async with self._http.stream("POST", self._url, content=body, headers=headers):
                    pass  # the body is never read
        except Exception:  # noqa: BLE001 — best effort: nothing to report
            pass

    async def _from_stream(self, response: httpx.Response, request_id: int) -> object:
        """The first message in the stream that answers ``request_id``."""
        data: list[bytes] = []
        async for line in _bounded_lines(response):
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif line == b"" and data:
                message = parse(b"\n".join(data))
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
