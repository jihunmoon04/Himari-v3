"""Application's trash (V3-P65 §7): the order of a trash, a restore and a
permanent delete, one at a time per Conversation, and the periodic purge."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from testkit import RecordingTool, calls, final, text_result, user

from himari.bootstrap import application as application_module
from himari.bootstrap.application import Application, ConversationStillStopping, workspace_for
from himari.common import CallId, CallRef, ConversationId, PrincipalId, RawRepresentation, ToolCall, ToolId, TrustedScope
from himari.control.api import ControlError, ControlErrorCode
from himari.data.api import (
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.provider.scripted import ScriptedAdapter, ScriptStep
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


class HoldingTool(RecordingTool):
    """Runs until ``release`` (or a cancellation, unless ``stubborn``)."""

    def __init__(self) -> None:
        super().__init__("hold")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.stubborn = False

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        self.invocations.append(invocation)
        self.entered.set()
        while True:
            try:
                await self.release.wait()
                break
            except asyncio.CancelledError:
                if not self.stubborn:
                    raise
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result("held"))


async def open_app(root: Path, script: Sequence[ScriptStep] = (), **kwargs: object) -> Application:
    kwargs.setdefault("cleanup_seconds", None)
    return await Application.open(root, ScriptedAdapter(script), model_id="scripted-model", sandbox=False,
                                  **kwargs)  # type: ignore[arg-type]


async def conversation(app: Application, owner: PrincipalId = ALICE) -> tuple[TrustedScope, Path]:
    """A Conversation and its own folder, with a file in it."""
    scope = TrustedScope(owner, await app.create_conversation(owner))
    folder = Path(app.inside.host_root(workspace_for(owner))) / "conversations" / scope.conversation_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("mine")
    return scope, folder


def code(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


def test_trash_restore_and_purge(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, folder = await conversation(app)
            other, other_folder = await conversation(app)
            moved = await app.trash(scope)
            assert folder.exists()  # a trashed Conversation keeps its files until it is purged
            assert [s.conversation_id for s in await app.data.list_conversations(ALICE)] == [other.conversation_id]
            (listed,) = await app.data.list_conversations(ALICE, trashed=True)
            assert listed.trashed_at == moved
            with pytest.raises(ControlError) as refused:
                await app.runner.submit(scope, user("late"))
            assert refused.value.code is ControlErrorCode.CLOSED

            await app.restore(scope)
            assert (await app.data.read_conversation(scope)).trashed_at is None
            assert (await app.runner.submit(scope, None)).running  # takes work again
            await app.runner.wait(scope)

            await app.trash(scope)
            await app.purge(scope)
            assert not folder.exists() and other_folder.exists()
            assert await app.data.list_conversations(ALICE, trashed=True) == ()
            with pytest.raises(DataError) as gone:
                await app.restore(scope)
            assert code(gone) is DataErrorCode.NOT_AUTHORIZED
        finally:
            await app.close()

    asyncio.run(scenario())


def test_trash_stops_a_running_job_first(tmp_path: Path) -> None:
    hold = HoldingTool()

    async def scenario() -> None:
        app = await open_app(tmp_path / "data", [calls(("hold", {})), final("never")], tools=[hold])
        try:
            scope, _ = await conversation(app)
            await app.runner.submit(scope, user("hold on"))
            await hold.entered.wait()
            notice = await app.delete_notice(scope)
            assert notice.running and notice.trash_days == 30
            await app.trash(scope)  # the cancel cut the call short; the job ended before the move
            assert not app.runner.status(scope).running
            await app.restore(scope)
            (event, *_) = [e for e in await app.data.read_events(scope) if e.origin_run_id is not None]
            records = await app.data.read_tool_executions(scope, event.origin_run_id)  # type: ignore[arg-type]
            assert [r.status for r in records] == [ToolExecutionStatus.CANCELLED]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_job_that_will_not_stop_refuses_the_trash_and_changes_nothing(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application_module, "TRASH_STOP_SECONDS", 0.1)
    hold = HoldingTool()
    hold.stubborn = True

    async def scenario() -> None:
        app = await open_app(tmp_path / "data", [calls(("hold", {})), final("done")], tools=[hold])
        try:
            scope, _ = await conversation(app)
            await app.runner.submit(scope, user("hold on"))
            await hold.entered.wait()
            with pytest.raises(ConversationStillStopping):
                await app.trash(scope)
            assert (await app.data.read_conversation(scope)).trashed_at is None
            hold.release.set()
            await app.runner.wait(scope)
            assert (await app.runner.submit(scope, None)).running  # reopened
            await app.runner.wait(scope)
        finally:
            hold.release.set()  # a stubborn job would hold close() forever
            await app.close()

    asyncio.run(scenario())


def test_pr56_b1_a_cancelled_trash_leaves_the_conversation_open(tmp_path: Path) -> None:
    hold = HoldingTool()
    hold.stubborn = True  # the job outlives the cancelled trash

    async def scenario() -> None:
        app = await open_app(tmp_path / "data", [calls(("hold", {})), final("done")], tools=[hold])
        try:
            scope, _ = await conversation(app)
            await app.runner.submit(scope, user("hold on"))
            await hold.entered.wait()
            trashing = asyncio.create_task(app.trash(scope))
            await asyncio.sleep(0.05)
            assert scope.conversation_id in app.runner._closed  # waiting in close for the job
            trashing.cancel()  # the request went away
            with pytest.raises(asyncio.CancelledError):
                await trashing
            assert (await app.data.read_conversation(scope)).trashed_at is None  # still active
            assert scope.conversation_id not in app.runner._closed  # and open again
            hold.release.set()
            await app.runner.wait(scope)
            assert (await app.runner.submit(scope, user("still here"))).running
            await app.runner.wait(scope)
            assert app._lifecycle == {}
        finally:
            hold.release.set()  # a stubborn job would hold close() forever
            await app.close()

    asyncio.run(scenario())


def test_pr56_c1_lifecycle_locks_are_kept_only_while_used(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            for n in range(20):  # ids that are not there, or not the caller's
                missing = TrustedScope(ALICE, ConversationId(f"conv_missing_{n}"))
                for attempt in (app.trash, app.restore, app.purge):
                    with pytest.raises(DataError):
                        await attempt(missing)
            await app.trash(scope)
            await app.restore(scope)
            await app.trash(scope)
            await app.purge(scope)
            assert app._lifecycle == {}
        finally:
            await app.close()

    asyncio.run(scenario())


def test_b4_another_principal_cannot_touch_the_owners_runner(tmp_path: Path) -> None:
    hold = HoldingTool()

    async def scenario() -> None:
        app = await open_app(tmp_path / "data", [calls(("hold", {})), final("done")], tools=[hold])
        try:
            scope, folder = await conversation(app)
            await app.runner.submit(scope, user("hold on"))
            await hold.entered.wait()
            intruder = TrustedScope(BOB, scope.conversation_id)
            before = app.runner.status(scope)
            for attempt in (app.trash, app.restore, app.purge, app.delete_notice):
                with pytest.raises(DataError) as refused:
                    await attempt(intruder)
                assert code(refused) is DataErrorCode.NOT_AUTHORIZED
            assert app.runner.status(scope) == before and before.running  # the owner's job runs on
            assert not app.runner.status(scope).stop_requested
            assert scope.conversation_id not in app.runner._closed
            hold.release.set()
            await app.runner.wait(scope)
            assert (await app.runner.submit(scope, user("still mine"))).running  # never closed
            await app.runner.wait(scope)
            assert folder.exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_b1_a_restore_waiting_behind_a_purge_finds_it_gone(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, folder = await conversation(app)
            await app.trash(scope)
            lock = app.inside.write_lock(workspace_for(ALICE))
            await lock.acquire()  # the folder removal waits here, PURGING already recorded
            purging = asyncio.create_task(app.purge(scope))
            await asyncio.sleep(0.05)
            restoring = asyncio.create_task(app.restore(scope))
            await asyncio.sleep(0.05)
            assert not restoring.done()  # waits for the purge to finish
            lock.release()
            await purging
            with pytest.raises(DataError):
                await restoring
            assert not folder.exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_b1_a_purge_waiting_behind_a_restore_leaves_the_files(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, folder = await conversation(app)
            await app.trash(scope)
            gate = asyncio.Event()

            async def hold() -> None:
                async with app._exclusive(scope.conversation_id):
                    await gate.wait()

            holder = asyncio.create_task(hold())
            await asyncio.sleep(0)
            restoring = asyncio.create_task(app.restore(scope))
            await asyncio.sleep(0)
            purging = asyncio.create_task(app.purge(scope))  # queued after the restore
            await asyncio.sleep(0.05)
            gate.set()
            await holder
            await restoring
            with pytest.raises(DataError) as refused:
                await purging
            assert code(refused) is DataErrorCode.INVALID_STATE_TRANSITION
            assert (folder / "notes.txt").read_text() == "mine"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_purge_cut_short_after_the_folder_is_never_restored_and_is_finished_later(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, folder = await conversation(app)
            await app.trash(scope)
            real = app.data.purge_conversation

            async def crash(scope: TrustedScope) -> None:
                raise RuntimeError("the server died here")

            monkeypatch.setattr(app.data, "purge_conversation", crash)
            with pytest.raises(RuntimeError):
                await app.purge(scope)
            assert not folder.exists()
            with pytest.raises(DataError) as refused:
                await app.restore(scope)  # its files are gone: no restore
            assert code(refused) is DataErrorCode.INVALID_STATE_TRANSITION
            monkeypatch.setattr(app.data, "purge_conversation", real)
            assert await app.purge_due() == 1  # PURGING, whatever its age
            assert await app.data.list_conversations(ALICE, trashed=True) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_purge_due_takes_what_was_trashed_more_than_the_days_ago(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data", trash_days=7)
        try:
            scope, folder = await conversation(app)
            bob, bob_folder = await conversation(app, BOB)
            await app.trash(scope)
            await app.trash(bob)
            assert await app.purge_due() == 0
            assert await app.purge_due(datetime.now(UTC) + timedelta(days=6)) == 0
            assert await app.purge_due(datetime.now(UTC) + timedelta(days=7, seconds=1)) == 2  # every principal's
            assert not folder.exists() and not bob_folder.exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_startup_purges_what_is_due(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        scope, folder = await conversation(app)
        await app.trash(scope)
        long_ago = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="microseconds")
        app.store.connection.execute("UPDATE conversations SET trashed_at=?", (long_ago,))
        await app.close()

        app = await open_app(tmp_path / "data", cleanup_seconds=3600)
        try:
            for _ in range(100):
                if not folder.exists() and await app.data.list_conversations(ALICE, trashed=True) == ():
                    break
                await asyncio.sleep(0.01)
            assert not folder.exists() and await app.data.list_conversations(ALICE, trashed=True) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_trash_ends_the_conversations_own_mcp_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        released: list[str] = []

        async def release(key: str) -> None:
            released.append(key)

        monkeypatch.setattr(app.mcp, "release", release)
        try:
            scope, _ = await conversation(app)
            await app.trash(scope)
            assert released == [scope.conversation_id]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_notice_names_external_servers_from_the_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            assert (await app.delete_notice(scope)).servers == ()
            run_id = await app.data.prepare_run(scope, RunPreparation(
                b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
            await app.data.mark_dispatch(scope, run_id)
            await app.data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
            group = tuple(ToolCall(CallId(f"c{i}"), i, ToolId(t), {})
                          for i, t in enumerate(("calliope/open_game", "inside/read_file", "weather/today")))
            await app.data.project_model_output(scope, run_id, AssistantToolCalls(group))
            await app.data.commit_tool_outcome(scope, CallRef(run_id, CallId("c0")), ToolCompletion(
                ToolExecutionStatus.SUCCEEDED, text_result("ok")))
            notice = await app.delete_notice(scope)
            # Neither server is configured now; both are still named. HIMARI's own Tools are not servers.
            assert notice.servers == ("calliope", "weather") and notice.unconfigured == ("calliope", "weather")
            assert not notice.running
        finally:
            await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("days", [0, 366])
def test_trash_days_are_bounded(tmp_path: Path, days: int) -> None:
    with pytest.raises(ValueError, match="trash_days"):
        asyncio.run(open_app(tmp_path / "data", trash_days=days))
