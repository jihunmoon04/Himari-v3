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
    def __init__(self, launcher: FakeLauncher, process: FakeProcess) -> None:
        self.launcher = launcher
        self.process = process
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
                result: dict[str, Any] = {"protocolVersion": params["protocolVersion"],
                                          "capabilities": self.launcher.capabilities,
                                          "serverInfo": {"name": "fake-stdio", "version": "1"}}
                if self.launcher.instructions is not None:
                    result["instructions"] = self.launcher.instructions
            elif method == "prompts/list":
                result = {"prompts": self.launcher.prompts}
            elif method == "prompts/get":
                index = self.launcher.processes.index(self.process)
                result = {"messages": [{"role": "user", "content": {
                    "type": "text", "text": f"prompt {params['name']} from process {index}"}}]}
            elif method == "ping":
                if not self.launcher.answer_ping:
                    return  # hung: reads, never answers
                if self.launcher.ping_error:
                    await self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no"}})
                    return
                result = {}
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
        self.server = FakeStdioServer(launcher, self)
        self.argv, self.env, self.cwd = list(argv), dict(env), cwd
        self.returncode: int | None = None
        self.shutdowns = 0

    async def write_line(self, data: bytes) -> None:
        if self.returncode is not None:
            raise Gone("ended")
        if self.server.launcher.stop_reading and b'"tools/call"' in data:
            await asyncio.Event().wait()  # a full pipe: the server stopped reading its stdin
        gate = self.server.launcher.write_gate
        if gate is not None:
            await gate.wait()  # a slow pipe: the line goes in once the gate opens
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
        if self.server.launcher.shutdown_delay:
            await asyncio.sleep(self.server.launcher.shutdown_delay)
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
        self.answer_cancelled = False  # MCP: a cancelled request gets no answer
        self.answer_ping = True
        self.ping_error = False  # answer ping with -32601 (a server without ping)
        self.write_gate: asyncio.Event | None = None
        self.capabilities: dict[str, Any] = {"tools": {}}
        self.instructions: str | None = None
        self.prompts: list[dict[str, Any]] = []
        self.initialize_delay = 0.0
        self.stop_reading = False  # tools/call writes block forever
        self.shutdown_delay = 0.0
        self.most_alive = 0  # the most processes alive at once (the listing process aside)

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> FakeProcess:
        if self.fail_launch is not None:
            raise self.fail_launch
        process = FakeProcess(self, argv, env, cwd)
        self.processes.append(process)
        alive = sum(1 for p in self.processes[1:] if p.returncode is None)
        self.most_alive = max(self.most_alive, alive)
        return process

    def lines(self, index: int) -> list[str]:
        """What process ``index`` received, in order: the method or ``answer``."""
        return [m.get("method", "answer") for m in self.processes[index].server.received]

    def calls(self) -> list[tuple[int, str, dict[str, Any]]]:
        """(process index, tool, arguments) of every tools/call received."""
        return [(i, m["params"]["name"], m["params"].get("arguments", {}))
                for i, p in enumerate(self.processes) for m in p.server.received if m.get("method") == "tools/call"]
