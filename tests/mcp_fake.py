"""A fake MCP server (Streamable HTTP) as an httpx transport, for tests."""

from __future__ import annotations

import asyncio
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


VIEW_URI = "ui://fake/echo-view"
VIEW_HTML = "<!DOCTYPE html><html><body><p>echo view</p></body></html>"


def view_entry(uri: str = VIEW_URI, html: str = VIEW_HTML, **extra: Any) -> dict[str, Any]:
    """One ``resources/read`` content entry, as an MCP Apps server gives it."""
    return {"uri": uri, "mimeType": "text/html;profile=mcp-app", "text": html, **extra}


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
        # MCP Apps: resources/read answers from here (uri → the result); a
        # missing uri is a JSON-RPC error. What the client declared is kept.
        self.resources: dict[str, Any] = {}
        self.resource_reads: list[str] = []
        self.client_capabilities: dict[str, Any] | None = None
        self.resource_delay = 0.0  # seconds before answering resources/read
        self.call_delay = 0.0  # seconds before running a tools/call
        self.notification_delay = 0.0  # seconds before accepting a notification
        # V3-P60: what initialize declares, and the server's prompts.
        self.capabilities: dict[str, Any] = {"tools": {}}
        self.instructions: str | None = None
        self.prompts: list[dict[str, Any]] = []
        self.prompt_messages: dict[str, list[dict[str, Any]]] = {}
        self.methods: list[str] = []
        self.initialize_delay = 0.0

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
            if self.notification_delay:
                await asyncio.sleep(self.notification_delay)

            async def body() -> Any:  # a huge body; the client must not read it
                self.notification_body_read = True
                yield b"x" * (64 * 1024 * 1024)
            return httpx.Response(202, content=body())
        method, params, request_id = message["method"], message.get("params", {}), message["id"]
        headers: dict[str, str] = {}
        if method == "initialize":
            if self.initialize_delay:
                await asyncio.sleep(self.initialize_delay)
            self.client_capabilities = params.get("capabilities")
            self.initializations += 1
            session = f"session-{self.initializations}"
            self.sessions.add(session)
            headers["mcp-session-id"] = session
            result: dict[str, Any] = {
                "protocolVersion": self.protocol_version or params["protocolVersion"],
                "capabilities": self.capabilities,
                "serverInfo": {"name": "fake", "version": "1"},
            }
            if self.instructions is not None:
                result["instructions"] = self.instructions
            return self._answer(request_id, result, headers)
        if request.headers.get("mcp-session-id") not in self.sessions:
            return httpx.Response(404 if "mcp-session-id" in request.headers else 400)
        self.methods.append(method)
        if method == "prompts/list":
            return self._answer(request_id, {"prompts": self.prompts})
        if method == "prompts/get":
            if params["name"] not in self.prompt_messages:
                return self._error(request_id, -32602)
            return self._answer(request_id, {"messages": self.prompt_messages[params["name"]]})
        if method == "tools/list" and self.list_result is not None:
            return self._answer(request_id, self.list_result)
        if method == "tools/list":
            start = int(params.get("cursor", "0"))
            size = self.page_size or len(self.tools)
            page: dict[str, Any] = {"tools": self.tools[start : start + size]}
            if start + size < len(self.tools):
                page["nextCursor"] = str(start + size)
            return self._answer(request_id, page)
        if method == "resources/read":
            self.resource_reads.append(params["uri"])
            if self.resource_delay:
                await asyncio.sleep(self.resource_delay)
            if params["uri"] not in self.resources:
                return self._error(request_id, -32602)
            return self._answer(request_id, self.resources[params["uri"]])
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
            if self.call_delay:
                await asyncio.sleep(self.call_delay)
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

    def cancellations(self) -> list[tuple[dict[str, Any], httpx.Headers]]:
        """Every notifications/cancelled received: (params, headers)."""
        out = []
        for request in self.requests:
            message = json.loads(request.content)
            if message.get("method") == "notifications/cancelled":
                out.append((message["params"], request.headers))
        return out

    def call_ids(self) -> list[int]:
        return [json.loads(r.content)["id"] for r in self.requests
                if json.loads(r.content).get("method") == "tools/call"]

