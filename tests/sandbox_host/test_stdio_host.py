"""stdio MCP servers in the real server sandbox, through the whole
Application (V3-P59 §4, §5; opt-in: HIMARI_SANDBOX_HOST=1)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from testkit import calls, final, user

from himari.bootstrap.application import Application
from himari.bootstrap.operator import OPERATOR
from himari.common import TrustedScope
from himari.control.api import DriveState
from himari.data.api import ToolExecutionStatus
from himari.provider.scripted import ScriptedAdapter
from himari.sandbox.backend import SANDBOX_ENV

pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1", reason="host gate is opt-in")

SERVER = Path(__file__).resolve().parents[1] / "stdio_server" / "echo_server.py"


def setup(tmp_path: Path, **server: Any) -> tuple[Path, Path, Path]:
    root, ro, rw = tmp_path / "root", tmp_path / "server", tmp_path / "state"
    root.mkdir(mode=0o700)
    ro.mkdir()
    rw.mkdir()
    shutil.copy(SERVER, ro / "echo_server.py")
    entry = {"command": "/usr/bin/python3", "args": ["-I", str(ro / "echo_server.py")], "cwd": str(rw),
             "env": {"GIVEN": "1"}, "read_only": [str(ro)], "writable": [str(rw)],
             "tools": {"echo": {"effect": "scoped_write"}}, **server}
    (root / "mcp.json").write_text(json.dumps({"servers": {"echo": entry}}))
    (root / "mcp.json").chmod(0o600)
    return root, ro, rw


def survivors(marker: Path) -> str:
    return subprocess.run(["pgrep", "-f", str(marker)], capture_output=True, text=True).stdout


def test_a_model_call_reaches_a_sandboxed_stdio_server(tmp_path: Path) -> None:
    root, ro, rw = setup(tmp_path)

    async def run() -> None:
        script = ScriptedAdapter([calls(("echo/echo", {"text": "where"})), final("done")])
        app = await Application.open(root, script, model_id="scripted-model", sandbox=True)
        try:
            (status,) = app.mcp.statuses
            assert status.available, status.problems
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            result = await app.runtime.driver.drive(scope, user("go"))
            assert result.state is DriveState.COMPLETED
            (execution,) = await app.data.read_tool_executions(scope, result.run_ids[0])
            assert execution.status is ToolExecutionStatus.SUCCEEDED and execution.model_result is not None
            seen = json.loads(execution.model_result.blocks[0].text.removeprefix("echo: "))  # type: ignore[union-attr]
            assert seen["cwd"] == str(rw)
            assert seen["env"] == {**SANDBOX_ENV, "GIVEN": "1", "PWD": str(rw)}  # nothing of HIMARI's own
            assert survivors(ro / "echo_server.py")
        finally:
            await app.close()

    asyncio.run(run())
    assert not survivors(ro / "echo_server.py")  # closing the Application ends the server


def test_a_stdio_server_may_not_reach_himaris_data(tmp_path: Path) -> None:
    root, _, _ = setup(tmp_path)
    (root / "workspaces").mkdir(mode=0o700, exist_ok=True)
    config = json.loads((root / "mcp.json").read_text())
    config["servers"]["echo"]["writable"] = [str(root / "workspaces")]
    (root / "mcp.json").write_text(json.dumps(config))

    async def run() -> None:
        app = await Application.open(root, ScriptedAdapter([]), model_id="scripted-model", sandbox=True)
        try:
            (status,) = app.mcp.statuses
            assert not status.available and "HIMARI's data" in status.problems[0]
        finally:
            await app.close()

    asyncio.run(run())
