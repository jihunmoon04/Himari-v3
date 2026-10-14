"""A fake MCP server (Streamable HTTP) as an httpx transport, for tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

URL = "http://127.0.0.1:8790/mcp"
ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"echo: {arguments.get('text')}"}]}


class FakeMcpServer:
    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        *,
        token: str | None = None,
        sse: bool = False,
        page_size: int | None = None,
        protocol_version: str | None = None,
        url: str = URL,
    ) -> None:
        self.url = url
        self.tools = tools if tools is not None else [
            {"name": "echo", "description": "Echo the text back.", "inputSchema": ECHO_SCHEMA},
        ]
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {"echo": echo}
        self.token = token
        self.sse = sse
        self.page_size = page_size
        self.protocol_version = protocol_version
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: set[str] = set()
        self.initializations = 0
        self.down = False
        # One-shot behaviours for the next tools/call:
        self.next_status: int | None = None
        self.next_error: Exception | None = None  # raised after the call ran
        self.next_rpc_error: int | None = None
        self.list_result: Any = None  # if set: tools/list answers exactly this result
        self.notification_body_read = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def forget_sessions(self) -> None:
        self.sessions.clear()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down or str(request.url) != self.url:  # nothing else listens
            raise httpx.ConnectError("connection refused", request=request)
        if self.token is not None and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        message = json.loads(request.content)
        if "id" not in message:
            async def body() -> Any:  # a huge body; the client must not read it
                self.notification_body_read = True
                yield b"x" * (64 * 1024 * 1024)
            return httpx.Response(202, content=body())
        method, params, request_id = message["method"], message.get("params", {}), message["id"]
        headers: dict[str, str] = {}
        if method == "initialize":
            self.initializations += 1
            session = f"session-{self.initializations}"
            self.sessions.add(session)
            headers["mcp-session-id"] = session
            result: dict[str, Any] = {
                "protocolVersion": self.protocol_version or params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
            return self._answer(request_id, result, headers)
        if request.headers.get("mcp-session-id") not in self.sessions:
            return httpx.Response(404 if "mcp-session-id" in request.headers else 400)
        if method == "tools/list" and self.list_result is not None:
            return self._answer(request_id, self.list_result)
        if method == "tools/list":
            start = int(params.get("cursor", "0"))
            size = self.page_size or len(self.tools)
            page: dict[str, Any] = {"tools": self.tools[start : start + size]}
            if start + size < len(self.tools):
                page["nextCursor"] = str(start + size)
            return self._answer(request_id, page)
        if method == "tools/call":
            name, arguments = params["name"], params.get("arguments", {})
            if self.next_status is not None:
                status, self.next_status = self.next_status, None
                return httpx.Response(status)
            if self.next_rpc_error is not None:
                code, self.next_rpc_error = self.next_rpc_error, None
                return self._error(request_id, code)
            if name not in self.handlers:
                return self._error(request_id, -32602)
            self.calls.append((name, arguments))
            if self.next_error is not None:
                error, self.next_error = self.next_error, None
                raise error
            return self._answer(request_id, self.handlers[name](arguments))
        return self._error(request_id, -32601)

    def _error(self, request_id: int, code: int) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id,
                                         "error": {"code": code, "message": "nope"}})

    def _answer(self, request_id: int, result: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if not self.sse:
            return httpx.Response(200, json=message, headers=headers)
        # A progress notification first, then the answer split over two data lines.
        text = json.dumps(message)
        half = text.index(",") + 1  # between two tokens: the joined lines are still JSON
        stream = (f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress'})}\n\n"
                  f"data: {text[:half]}\ndata: {text[half:]}\n\n")
        return httpx.Response(200, content=stream.encode(),
                              headers={"content-type": "text/event-stream", **(headers or {})})
