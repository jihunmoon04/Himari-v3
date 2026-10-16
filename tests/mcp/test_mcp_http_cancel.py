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
