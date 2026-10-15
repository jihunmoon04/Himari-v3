"""A fake stdio MCP server, in process: what a launcher would start.

``FakeLauncher.launch`` returns a ``FakeProcess`` whose lines go to a
``FakeStdioServer`` (one per process). The server answers ``initialize``,
``tools/list``, ``resources/read`` and ``tools/call`` from its handlers, and
can be told to be slow, to ignore cancellations, to crash, or to write
something that is not an MCP message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from mcp_fake import ECHO_SCHEMA


class Gone(Exception):
    """The fake process has ended (the launcher's ProcessGone)."""


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class FakeStdioServer:
    def __init__(self, launcher: FakeLauncher) -> None:
        self.launcher = launcher
        self.received: list[dict[str, Any]] = []
        self.out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.running: dict[int, asyncio.Task[None]] = {}

    async def receive(self, line: bytes) -> None:
        message = json.loads(line)
        self.received.append(message)
        method = message.get("method")
        if "id" not in message:
            if method == "notifications/cancelled" and self.launcher.honour_cancel:
                task = self.running.get(message["params"]["requestId"])
                if task is not None:
                    task.cancel()
            return
        if method is None:
            return  # an answer to the server's own request
        self.running[message["id"]] = asyncio.create_task(self._handle(message))

    async def _handle(self, message: dict[str, Any]) -> None:
        method, params, request_id = message["method"], message.get("params", {}), message["id"]
        try:
            if method == "initialize":
                if self.launcher.initialize_delay:
                    await asyncio.sleep(self.launcher.initialize_delay)
                result: dict[str, Any] = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                                          "serverInfo": {"name": "fake-stdio", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": self.launcher.tools}
            elif method == "resources/read":
                result = self.launcher.resources[params["uri"]]
            elif method == "tools/call":
                handler = self.launcher.handlers[params["name"]]
                answer = handler(params.get("arguments", {}))
                result = await answer if asyncio.iscoroutine(answer) else answer  # type: ignore[assignment]
            else:
                await self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no"}})
                return
        except asyncio.CancelledError:
            if self.launcher.answer_cancelled:
                await self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32800, "message": "cancelled"}})
            return
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def send(self, message: Mapping[str, Any]) -> None:
        await self.out.put(json.dumps(message).encode())

    async def send_raw(self, line: bytes) -> None:
        await self.out.put(line)


class FakeProcess:
    def __init__(self, launcher: FakeLauncher, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> None:
        self.server = FakeStdioServer(launcher)
        self.argv, self.env, self.cwd = list(argv), dict(env), cwd
        self.returncode: int | None = None
        self.shutdowns = 0

    async def write_line(self, data: bytes) -> None:
        if self.returncode is not None:
            raise Gone("ended")
        await self.server.receive(data)

    async def read_line(self) -> bytes:
        line = await self.server.out.get()
        if line is None:
            raise Gone("closed its output")
        return line

    def stderr_tail(self) -> bytes:
        return b""

    def crash(self) -> None:
        self.returncode = 1
        self.server.out.put_nowait(None)

    async def shutdown(self, grace: float) -> None:
        self.shutdowns += 1
        for task in self.server.running.values():
            task.cancel()
        if self.returncode is None:
            self.returncode = 0
            self.server.out.put_nowait(None)


def echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"echo: {arguments.get('text')}"}]}


class FakeLauncher:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = [{"name": "echo", "description": "Echo.", "inputSchema": ECHO_SCHEMA}]
        self.handlers: dict[str, Handler] = {"echo": echo}
        self.resources: dict[str, Any] = {}
        self.processes: list[FakeProcess] = []
        self.fail_launch: Exception | None = None
        self.honour_cancel = True  # a cancelled call stops (and, if answer_cancelled, says so)
        self.answer_cancelled = True
        self.initialize_delay = 0.0

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> FakeProcess:
        if self.fail_launch is not None:
            raise self.fail_launch
        process = FakeProcess(self, argv, env, cwd)
        self.processes.append(process)
        return process

    def calls(self) -> list[tuple[int, str, dict[str, Any]]]:
        """(process index, tool, arguments) of every tools/call received."""
        return [(i, m["params"]["name"], m["params"].get("arguments", {}))
                for i, p in enumerate(self.processes) for m in p.server.received if m.get("method") == "tools/call"]
