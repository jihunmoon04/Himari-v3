"""HTTP cancellation is best effort (V3-P59 §5.5, PR #35): a Stop or a timeout
sends notifications/cancelled with the request's own id, session id and
protocol version, from a bounded background task, and never changes the
call's verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from mcp_fake import URL, FakeMcpServer

from himari.mcp import client as client_module
from himari.mcp.client import McpClient, McpRefused, McpUncertain, http_client


def run(scenario: Callable[[FakeMcpServer, McpClient], Awaitable[None]], server: FakeMcpServer | None = None) -> None:
    server = server or FakeMcpServer()

    async def go() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        try:
            await asyncio.wait_for(scenario(server, client), 20)
        finally:
            await client.aclose()

    asyncio.run(go())


async def stopped_call(client: McpClient, server: FakeMcpServer) -> None:
    await client.initialize()
    server.call_delay = 10
    call = asyncio.create_task(client.call_tool("echo", {"text": "x"}))
    await asyncio.sleep(0.05)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)


def test_a_stop_sends_the_cancellation_of_that_request_in_its_session() -> None:
    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await stopped_call(client, server)
        await asyncio.sleep(0.05)
        ((params, headers),) = server.cancellations()
        assert params == {"requestId": server.call_ids()[0], "reason": "cancelled by the user"}
        assert headers["mcp-session-id"] == "session-1"
        assert headers["mcp-protocol-version"] == "2025-11-25"

    run(scenario)


def test_a_read_timeout_sends_the_cancellation_and_stays_uncertain() -> None:
    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        server.next_error = httpx.ReadTimeout("slow")
        with pytest.raises(McpUncertain, match="no complete answer"):
            await client.call_tool("echo", {"text": "x"})
        await asyncio.sleep(0.05)
        ((params, _),) = server.cancellations()
        assert params["reason"] == "timed out"

    run(scenario)


def test_the_cancellation_keeps_the_session_the_request_was_sent_in() -> None:
    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        server.call_delay = 10
        slow = asyncio.create_task(client.call_tool("echo", {"text": "old"}))
        await asyncio.sleep(0.05)
        server.call_delay = 0
        server.forget_sessions()
        await client.call_tool("echo", {"text": "new"})  # renews the session: session-2
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)
        await asyncio.sleep(0.05)
        ((params, headers),) = server.cancellations()
        assert headers["mcp-session-id"] == "session-1"  # not the renewed one; the server answers 404, ignored
        assert params["requestId"] == server.call_ids()[0]

    run(scenario)


def test_a_stop_does_not_wait_for_the_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "CANCEL_SECONDS", 0.3)

    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        server.notification_delay = 5  # a server slow to take notifications
        server.call_delay = 10
        call = asyncio.create_task(client.call_tool("echo", {"text": "x"}))
        await asyncio.sleep(0.05)
        start = asyncio.get_running_loop().time()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        assert asyncio.get_running_loop().time() - start < 0.1
        closing = asyncio.get_running_loop().time()
        await client.aclose()  # waits for the notification at most CANCEL_SECONDS
        assert asyncio.get_running_loop().time() - closing < 0.3 + 0.2

    run(scenario)


def test_a_failed_notification_changes_nothing() -> None:
    server = FakeMcpServer()
    refused: list[bytes] = []
    original = server.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if b"notifications/cancelled" in request.content:
            refused.append(request.content)  # the notification cannot be delivered
            raise httpx.ConnectError("refused", request=request)
        return await original(request)

    server.handle = handle  # type: ignore[method-assign]

    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        server.next_error = httpx.ReadTimeout("slow")
        with pytest.raises(McpUncertain, match="no complete answer"):  # the same verdict as with no notice at all
            await client.call_tool("echo", {"text": "x"})
        await asyncio.sleep(0.05)
        assert len(refused) == 1

    run(scenario, server)


def test_initialize_is_never_cancelled() -> None:
    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        server.initialize_delay = 10
        start = asyncio.create_task(client.initialize())
        await asyncio.sleep(0.05)
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert server.cancellations() == []

    run(scenario)


def test_a_request_that_could_not_connect_is_not_cancelled() -> None:
    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        server.down = True
        with pytest.raises(McpRefused, match="cannot connect"):
            await client.call_tool("echo", {"text": "x"})
        server.down = False
        await asyncio.sleep(0.05)
        assert server.cancellations() == []

    run(scenario)


# --- a real HTTP transport: what was not sent is not cancelled (PR #37 review B1, C1) -------


class LiveServer:
    """A minimal MCP endpoint (JSON answers) on a real socket, in a thread."""

    def __init__(self) -> None:
        import socket
        import threading

        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        self.cancelled: list[dict[str, object]] = []

        async def mcp(request: Request) -> Response:
            message = await request.json()
            if "id" not in message:
                if message.get("method") == "notifications/cancelled":
                    self.cancelled.append(message["params"])
                return Response(status_code=202)
            if message["method"] == "initialize":
                result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "live", "version": "1"}}
                return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result},
                                    headers={"mcp-session-id": "live-1"})
            await asyncio.sleep(float(message["params"]["arguments"].get("seconds", 0)))
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"],
                                 "result": {"content": [{"type": "text", "text": "done"}]}})

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])]), host="127.0.0.1",
                                port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> LiveServer:
        import time

        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("the live server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(5)


def test_a_stop_while_waiting_for_a_connection_sends_nothing_and_a_sent_one_is_cancelled() -> None:
    with LiveServer() as live:
        async def go() -> None:
            transport = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1))
            client = http_client(f"http://127.0.0.1:{live.port}/mcp", None, timeout_seconds=30, transport=transport)
            try:
                await client.initialize()
                first = asyncio.create_task(client.call_tool("slow", {"seconds": 5}))  # holds the one connection
                await asyncio.sleep(0.3)
                waiting = asyncio.create_task(client.call_tool("slow", {"seconds": 0}))  # waits for the pool
                await asyncio.sleep(0.3)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                await asyncio.sleep(0.3)
                assert live.cancelled == []  # never sent: nothing to cancel
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                for _ in range(40):
                    if live.cancelled:
                        break
                    await asyncio.sleep(0.05)
                assert live.cancelled == [{"requestId": 2, "reason": "cancelled by the user"}]  # 1 was initialize
            finally:
                await client.aclose()

        asyncio.run(asyncio.wait_for(go(), 30))


def test_a_pool_timeout_was_never_sent() -> None:
    server = FakeMcpServer()
    original = server.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if b"tools/call" in request.content:
            raise httpx.PoolTimeout("no free connection")
        return await original(request)

    server.handle = handle  # type: ignore[method-assign]

    async def scenario(server: FakeMcpServer, client: McpClient) -> None:
        await client.initialize()
        with pytest.raises(McpRefused, match="no free connection"):  # not uncertain: it never left
            await client.call_tool("echo", {"text": "x"})
        await asyncio.sleep(0.05)
        assert server.cancellations() == []

    run(scenario, server)
