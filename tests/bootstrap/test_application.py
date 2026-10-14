"""Application: the durable composition Web/CLI are built on (sandbox off: CI-safe)."""

from __future__ import annotations

import asyncio
import stat
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from testkit import calls, final, user

from himari.bootstrap.application import Application, workspace_for
from himari.bootstrap.operator import OperatorSecretError
from himari.common import PrincipalId, ToolId, TrustedScope
from himari.control.api import DriveState
from himari.data.sqlite import StoreLockedError
from himari.environment.inside import INSIDE
from himari.environment.inside.sandbox import SandboxState
from himari.provider.scripted import ScriptedAdapter, ScriptStep

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


async def open_app(root: Path, script: Sequence[ScriptStep] = ()) -> tuple[Application, ScriptedAdapter]:
    provider = ScriptedAdapter(script)
    return await Application.open(root, provider, model_id="scripted-model", sandbox=False), provider


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_layout_is_private_and_reused(tmp_path: Path) -> None:
    root = tmp_path / "data"

    async def scenario() -> None:
        app, _ = await open_app(root)
        secret = app.operator_secret
        assert app.sandbox_state is SandboxState.UNAVAILABLE
        tools = {d.tool_id for d in app.runtime.registry.catalog(frozenset({INSIDE}))}
        assert ToolId("inside/write_file") in tools and ToolId("inside/run_command") not in tools
        with pytest.raises(StoreLockedError):  # one server per data root
            await open_app(root)
        await app.close()
        assert [mode(root / p) for p in ("", "workspaces", "control")] == [0o700] * 3
        assert mode(root / "himari.db") == 0o600 and mode(root / "control/operator.secret") == 0o600
        again, _ = await open_app(root)
        assert again.operator_secret == secret
        again.check_layout()
        await again.close()

    asyncio.run(scenario())


def test_a_data_root_others_can_access_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    with pytest.raises(OperatorSecretError):
        asyncio.run(open_app(root))
    assert list(root.iterdir()) == []


def test_conversation_flow_survives_a_restart(tmp_path: Path) -> None:
    root = tmp_path / "data"
    script: list[ScriptStep] = [calls(("inside/write_file", {"path": "notes.txt", "content": "remember me"})), final("saved")]

    async def first() -> str:
        app, provider = await open_app(root, script)
        cid = await app.create_conversation(ALICE, title="Notes")
        scope = TrustedScope(ALICE, cid)
        await app.runner.submit(scope, user("save a note"))
        status = await app.runner.wait(scope)
        assert status.result is not None and status.result.state is DriveState.COMPLETED
        host = root / "workspaces" / workspace_for(ALICE) / "conversations" / cid / "notes.txt"
        assert host.read_text() == "remember me" and mode(root / "workspaces" / workspace_for(ALICE)) == 0o700
        assert len(provider.sent) == 2
        await app.close()
        return cid

    async def second(cid: str) -> None:
        app, _ = await open_app(root)
        (summary,) = await app.data.list_conversations(ALICE)
        assert summary.conversation_id == cid and summary.title == "Notes" and summary.preview == "save a note"
        assert summary.last_sequence == 4  # user, tool calls, tool result, final
        assert await app.data.list_conversations(BOB) == ()
        bob = await app.create_conversation(BOB)
        assert workspace_for(BOB) != workspace_for(ALICE)
        assert (await app.data.resolve_conversation_workspace(TrustedScope(BOB, bob))).workspace_id == workspace_for(BOB)
        await app.close()

    asyncio.run(second(asyncio.run(first())))


def test_workspace_ids_are_safe_for_any_principal() -> None:
    for principal in ("operator", "../../etc", "ünïcødé", "a" * 500):
        ws = workspace_for(PrincipalId(principal))
        assert ws.startswith("ws_") and len(ws) == 27 and ws[3:].isalnum()
    assert workspace_for(PrincipalId("a")) != workspace_for(PrincipalId("b"))


# --- PR #11 review -------------------------------------------------------------------------


def test_the_operator_secret_is_not_in_the_repr(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _ = await open_app(tmp_path / "data")
        try:
            assert app.operator_secret not in repr(app)
        finally:
            await app.close()

    asyncio.run(scenario())


def test_close_waits_for_a_started_file_operation_past_the_grace_period(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.environment.inside import tools as inside_tools

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_act = inside_tools.WriteFile.act

    def slow_act(self: Any, root_fd: int, relative: str, args: Any) -> Any:
        entered.set()
        release.wait(10)
        try:
            return real_act(self, root_fd, relative, args)
        finally:
            finished.set()

    monkeypatch.setattr(inside_tools.WriteFile, "act", slow_act)
    script: list[ScriptStep] = [calls(("inside/write_file", {"path": "slow.txt", "content": "late"})), final("x")]

    async def scenario() -> None:
        app, _ = await open_app(tmp_path / "data", script)
        cid = await app.create_conversation(ALICE)
        scope = TrustedScope(ALICE, cid)
        await app.runner.submit(scope, user("write slowly"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        closing = asyncio.create_task(app.close(grace_seconds=0.05))
        await asyncio.sleep(0.3)  # well past the grace period: the job was cancelled
        assert not closing.done(), "close() returned while a file write was still running"
        assert app.inside.write_lock(workspace_for(ALICE)).locked()
        release.set()
        await closing
        assert finished.is_set() and app.runner.status(scope).error == "cancelled"
        assert not app.inside.write_lock(workspace_for(ALICE)).locked()

    asyncio.run(scenario())
