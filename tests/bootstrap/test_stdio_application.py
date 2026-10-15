"""stdio MCP servers through the whole Application (V3-P59 §5): mcp.json,
Bootstrap's launcher, the listing, a model's call through the Dispatcher,
and shutdown — with a real child process (``tests/stdio_server``), launched
here without the sandbox; the sandboxed path is the host gate's
(tests/sandbox_host/test_stdio_host.py)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from testkit import calls, final, user

from himari.bootstrap.application import Application
from himari.bootstrap.operator import OPERATOR
from himari.common import TrustedScope
from himari.control.api import DriveState
from himari.data.api import ToolExecutionStatus
from himari.mcp.config import McpStdioConfig
from himari.provider.scripted import ScriptedAdapter
from himari.sandbox.server import STREAM_LIMIT, ServerProcess

SERVER = Path(__file__).resolve().parents[1] / "stdio_server" / "echo_server.py"


class PlainLauncher:
    """A launcher without the sandbox (tests only): the same process API."""

    def __init__(self) -> None:
        self.launched: list[ServerProcess] = []

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> ServerProcess:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=dict(env), cwd=cwd, start_new_session=True, limit=STREAM_LIMIT)
        self.launched.append(ServerProcess(process))
        return self.launched[-1]


def write_config(root: Path, **server: Any) -> None:
    root.mkdir(mode=0o700, exist_ok=True)
    entry = {"command": "/usr/bin/python3", "args": ["-I", str(SERVER)], "env": {"GIVEN": "1"},
             "tools": {"echo": {"effect": "scoped_write"}}, **server}
    (root / "mcp.json").write_text(json.dumps({"servers": {"echo": entry}}))
    (root / "mcp.json").chmod(0o600)


def test_a_model_call_reaches_a_stdio_server_through_the_application(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_config(root)
    launcher = PlainLauncher()

    async def run() -> None:
        script = ScriptedAdapter([calls(("echo/echo", {"text": "e4"})), final("done")])
        app = await Application.open(root, script, model_id="scripted-model", sandbox=False,
                                     mcp_launcher_for=lambda source, stdio: launcher)
        try:
            (status,) = app.mcp.statuses
            assert status.available and status.tool_ids == ("echo/echo",)
            assert launcher.launched[0].returncode is not None  # the listing process has ended
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            result = await app.runtime.driver.drive(scope, user("go"))
            assert result.state is DriveState.COMPLETED
            (execution,) = await app.data.read_tool_executions(scope, result.run_ids[0])
            assert execution.status is ToolExecutionStatus.SUCCEEDED
            assert execution.model_result is not None
            assert execution.model_result.blocks[0].text == "echo: e4"  # type: ignore[union-attr]
            assert len(launcher.launched) == 2 and launcher.launched[1].returncode is None
        finally:
            await app.close()
        assert all(p.returncode is not None for p in launcher.launched)  # nothing outlives the Application

    asyncio.run(run())


def test_the_launcher_gets_the_configured_command_environment_and_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_config(root, cwd="/usr", read_only=[str(tmp_path)], writable=[], env_from=["PASSED"])
    seen: list[tuple[str, McpStdioConfig]] = []
    launcher = PlainLauncher()

    def launcher_for(source: str, stdio: McpStdioConfig) -> PlainLauncher:
        seen.append((source, stdio))
        return launcher

    async def run() -> None:
        app = await Application.open(root, ScriptedAdapter([]), model_id="scripted-model", sandbox=False,
                                     environ={"PASSED": "from-himari", "NOT_PASSED": "x"},
                                     mcp_launcher_for=launcher_for)
        await app.close()

    asyncio.run(run())
    ((source, stdio),) = seen
    assert (source, stdio.cwd, stdio.read_only, stdio.writable) == ("echo", "/usr", (str(tmp_path),), ())
    assert dict(stdio.env) == {"GIVEN": "1", "PASSED": "from-himari"}  # only what mcp.json names


def test_without_the_sandbox_a_stdio_server_is_left_out(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_config(root)

    async def run() -> None:
        app = await Application.open(root, ScriptedAdapter([]), model_id="scripted-model", sandbox=False)
        try:
            (status,) = app.mcp.statuses
            assert not status.available and "server sandbox" in status.problems[0]
        finally:
            await app.close()

    asyncio.run(run())

