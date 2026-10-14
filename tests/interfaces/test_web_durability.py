"""PR #12 review B1: a message the server accepted (HTTP 202) survives the
process being killed right after the response."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.operator import derive_cli_bearer, read_operator_secret  # noqa: E402
from himari.common import TrustedScope  # noqa: E402
from himari.control.api import DriveState  # noqa: E402
from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_message_accepted_with_202_survives_a_kill_and_is_answered_after_restart(tmp_path: Path) -> None:
    root, port = tmp_path / "data", free_port()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])}
    child = subprocess.Popen([sys.executable, str(Path(__file__).parent / "web_child.py"), str(root), str(port)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                if httpx.get(f"{base}/api/session", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        headers = {"authorization": f"Bearer {derive_cli_bearer(read_operator_secret(str(root / 'control')))}"}
        cid = httpx.post(f"{base}/api/conversations", json={}, headers=headers).json()["id"]
        accepted = httpx.post(f"{base}/api/conversations/{cid}/messages", json={"text": "remember this"}, headers=headers)
        assert accepted.status_code == 202
        os.kill(child.pid, signal.SIGKILL)  # right after the 202: the background job never ran
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)

    async def after_restart() -> None:
        app = await Application.open(root, ScriptedAdapter([final("got it")]), model_id="m", sandbox=False)
        try:
            scope = TrustedScope(OPERATOR, cid)
            texts = [e.payload.blocks[0].text for e in await app.data.read_events(scope)]  # type: ignore[union-attr]
            assert texts == ["remember this"]  # committed before the 202
            await app.runner.submit(scope, None)  # the operator's Continue
            status = await app.runner.wait(scope)
            assert status.result is not None and status.result.state is DriveState.COMPLETED
            texts = [e.payload.blocks[0].text for e in await app.data.read_events(scope)]  # type: ignore[union-attr]
            assert texts == ["remember this", "got it"]
        finally:
            await app.close()

    asyncio.run(after_restart())
