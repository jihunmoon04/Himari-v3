"""Application and files (V3-P66 §5, §7, §8): upload, send with attachments,
the model presenting a file, download, and the cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from testkit import calls, final

from himari.bootstrap import application as application_module
from himari.bootstrap.application import (
    Application,
    FileNameRefused,
    FilesUnavailable,
    workspace_for,
)
from himari.common import AttachmentRef, PrincipalId, TextBlock, TrustedScope
from himari.control.api import ControlError, ControlErrorCode, JobStatus
from himari.data.api import MAX_STAGED_UPLOADS, DataError, DataErrorCode, UserMessage
from himari.environment.inside import WorkspaceBusy
from himari.prompt.api import ATTACHMENT_NOTE, PROGRESS_NOTE, TOOL_IMAGES_NOTE
from himari.provider.scripted import ScriptedAdapter

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def open_app(root: Path, adapter: ScriptedAdapter | None = None, **kwargs: object) -> Application:
    kwargs.setdefault("cleanup_seconds", None)
    return await Application.open(root, adapter or ScriptedAdapter(), model_id="scripted-model", sandbox=False,
                                  **kwargs)  # type: ignore[arg-type]


async def conversation(app: Application, owner: PrincipalId = ALICE) -> tuple[TrustedScope, Path]:
    scope = TrustedScope(owner, await app.create_conversation(owner))
    return scope, Path(app.inside.host_root(workspace_for(owner))) / "conversations" / scope.conversation_id


def test_a_sent_attachment_is_placed_and_the_model_is_told_where(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("inside/read_file", {"path": "uploads/notes.txt"})), final("read it")])
        app = await open_app(tmp_path / "data", adapter)
        try:
            scope, folder = await conversation(app)
            record = await app.upload(scope, "notes.txt", "text/plain; charset=utf-8", chunks(b"hello ", b"there"))
            assert (record.name, record.media_type, record.size, record.attached) == ("notes.txt", "text/plain", 11, False)
            await app.send(scope, "please read", [record.file_id])
            await app.runner.wait(scope)
            assert (folder / "uploads/notes.txt").read_bytes() == b"hello there"
            first = adapter.prepared_inputs[0]
            assert ATTACHMENT_NOTE in first.stable_prefix and TOOL_IMAGES_NOTE in first.stable_prefix
            prefix = list(first.stable_prefix)  # V3-P68 §3: always, after the operator's
            assert prefix.index(PROGRESS_NOTE) > prefix.index(TOOL_IMAGES_NOTE) > prefix.index("You are HIMARI, a helpful assistant.")
            info = first.attachments[f"{record.file_id}#1"]
            assert info.path == f"/workspace/conversations/{scope.conversation_id}/uploads/notes.txt"
            events = await app.data.read_events(scope)
            assert events[0].payload == UserMessage((TextBlock("please read"), AttachmentRef(record.file_id, 1)))
            result = (await app.data.read_tool_executions(scope, events[1].origin_run_id))[0]  # type: ignore[arg-type]
            assert "hello there" in result.model_result.blocks[0].text  # type: ignore[union-attr]
            # the model's later edit does not change the attachment
            (folder / "uploads/notes.txt").write_text("changed")
            got, handle = await app.download(scope, record.file_id, 1)
            with handle:
                assert handle.read() == b"hello there" and got.attached
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_presented_file_can_be_downloaded(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("inside/present_file", {"path": "out.csv"})), final("here")])
        app = await open_app(tmp_path / "data", adapter)
        try:
            scope, folder = await conversation(app)
            folder.mkdir(parents=True)
            (folder / "out.csv").write_text("a,b\n1,2\n")
            await app.send(scope, "make it")
            await app.runner.wait(scope)
            listed = await app.files(scope)
            assert [(f.name, f.media_type, f.version) for f in listed] == [("out.csv", "text/csv", 1)]
            assert listed[0].call is not None
            assert (await app.call_files(scope, [listed[0].call]))[listed[0].call] == (listed[0],)
            _, handle = await app.download(scope, listed[0].file_id, 1)
            with handle:
                assert handle.read() == b"a,b\n1,2\n"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_another_users_files_are_missing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            record = await app.upload(scope, "a.txt", None, chunks(b"a"))
            foreign = TrustedScope(BOB, scope.conversation_id)
            for attempt in (app.download(foreign, record.file_id, 1), app.files(foreign),
                            app.upload(foreign, "b.txt", None, chunks(b"b")), app.send(foreign, "x", [record.file_id])):
                with pytest.raises(DataError) as refused:
                    await attempt
                assert refused.value.code is DataErrorCode.NOT_AUTHORIZED
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_retry_after_busy_places_the_file_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data", ScriptedAdapter([final("ok")]))
        try:
            scope, folder = await conversation(app)
            record = await app.upload(scope, "r.pdf", None, chunks(b"%PDF"))
            real_submit = app.runner.submit

            async def busy(scope: TrustedScope, message: UserMessage | None) -> JobStatus:
                raise ControlError(ControlErrorCode.BUSY, "x")

            app.runner.submit = busy  # type: ignore[method-assign]
            with pytest.raises(ControlError):
                await app.send(scope, "", [record.file_id])
            app.runner.submit = real_submit  # type: ignore[method-assign]
            await app.send(scope, "", [record.file_id])
            await app.runner.wait(scope)
            assert sorted(p.name for p in (folder / "uploads").iterdir()) == ["r.pdf"]
            assert (await app.data.read_events(scope))[0].payload == UserMessage((AttachmentRef(record.file_id, 1),))
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_refused_attachment_changes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, folder = await conversation(app)
            other, _ = await conversation(app)
            record = await app.upload(other, "a.txt", None, chunks(b"a"))
            with pytest.raises(DataError) as refused:
                await app.send(scope, "x", [record.file_id])
            assert refused.value.code is DataErrorCode.ATTACHMENT_REFUSED and refused.value.file_id == record.file_id
            assert await app.data.read_events(scope) == () and not (folder / "uploads").exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_upload_refuses_bad_names_and_counts_staged_before_reading(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            for name in ("", "x" * 1001):
                with pytest.raises(FileNameRefused):
                    await app.upload(scope, name, None, chunks(b"a"))
            for i in range(MAX_STAGED_UPLOADS):
                await app.upload(scope, f"{i}.txt", None, chunks(str(i).encode()))
            read = []

            async def watched() -> AsyncIterator[bytes]:
                read.append(True)
                yield b"never"

            with pytest.raises(DataError) as refused:
                await app.upload(scope, "late.txt", None, watched())
            assert refused.value.code is DataErrorCode.LIMIT_EXCEEDED and read == []
        finally:
            await app.close()

    asyncio.run(scenario())


def test_without_a_workspace_files_are_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            app.uploads_work = False
            with pytest.raises(FilesUnavailable):
                await app.upload(scope, "a.txt", None, chunks(b"a"))
        finally:
            await app.close()

    asyncio.run(scenario())


def test_sending_waits_for_the_workspace_only_so_long_and_then_frees_the_trash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application_module, "PLACE_LOCK_SECONDS", 0.2)

    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            record = await app.upload(scope, "a.txt", None, chunks(b"a"))
            async with app.inside.write_lock(workspace_for(ALICE)):  # a long command elsewhere
                sending = asyncio.create_task(app.send(scope, "x", [record.file_id]))
                await asyncio.sleep(0.05)
                trashing = asyncio.create_task(app.trash(scope))
                with pytest.raises(WorkspaceBusy):
                    await sending
                await trashing  # not held up past the bound (review N4)
            assert (await app.data.list_conversations(ALICE, trashed=True))[0].conversation_id == scope.conversation_id
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_purge_waits_for_a_send_and_leaves_no_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data", ScriptedAdapter([final("ok")]))
        try:
            scope, folder = await conversation(app)
            record = await app.upload(scope, "a.txt", None, chunks(b"a"))
            lock = app.inside.write_lock(workspace_for(ALICE))
            await lock.acquire()
            sending = asyncio.create_task(app.send(scope, "x", [record.file_id]))
            await asyncio.sleep(0.05)
            deleting = asyncio.create_task(app.trash(scope))
            await asyncio.sleep(0.05)
            assert not deleting.done()  # the lifecycle lock is the send's (review C3)
            lock.release()
            await sending
            await deleting
            await app.purge(scope)
            assert not folder.exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_cleanup_drops_stale_staged_uploads(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            record = await app.upload(scope, "a.txt", None, chunks(b"a"))
            await app.clean_files()
            assert await app.data.count_staged(scope) == 1
            await app.clean_files(datetime.now(UTC) + timedelta(hours=25))
            assert await app.data.count_staged(scope) == 0
            with pytest.raises(DataError):
                await app.send(scope, "x", [record.file_id])
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_file_store_settings_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        asyncio.run(open_app(tmp_path / "data", file_max_bytes=10, file_store_max_bytes=5))


def test_files_are_kept_next_to_the_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            await app.upload(scope, "a.txt", None, chunks(b"a"))
            assert (tmp_path / "data" / "files" / "blobs").is_dir()
            assert oct((tmp_path / "data" / "files").stat().st_mode & 0o777) == "0o700"
        finally:
            await app.close()

    asyncio.run(scenario())



# --- PR #59 review --------------------------------------------------------------------------


class Crash(BaseException):
    """The process dying at that point: nothing after it runs."""


@pytest.mark.parametrize("when", ["after the rename", "before the rename"])
def test_a_crash_around_placing_never_places_a_file_twice(tmp_path: Path, when: str) -> None:
    """Owner review B1: the path is recorded before the rename, and a retry
    finds an earlier placement by its bytes — across a restart too."""
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        scope, folder = await conversation(app)
        a = await app.upload(scope, "a.txt", None, chunks(b"a"))
        b = await app.upload(scope, "b.txt", None, chunks(b"b"))
        real = app.inside.commit_copy

        async def dying(workspace_id, cwd, temp, path):  # type: ignore[no-untyped-def]
            if path.endswith("/b.txt"):
                if when == "after the rename":
                    await real(workspace_id, cwd, temp, path)
                raise Crash
            return await real(workspace_id, cwd, temp, path)

        app.inside.commit_copy = dying  # type: ignore[method-assign]
        with pytest.raises(Crash):
            await app.send(scope, "two", [a.file_id, b.file_id])
        app.runner._tasks.clear()  # the process is gone: nothing of it is waited for
        app.store.close()

        again = await open_app(tmp_path / "data", ScriptedAdapter([final("ok")]))
        try:
            await again.send(scope, "two", [a.file_id, b.file_id])
            await again.runner.wait(scope)
            placed = sorted(p.name for p in (folder / "uploads").iterdir())
            assert placed == ["a.txt", "b.txt"], placed  # no "b (2).txt", no leftover temporary file
            records = await again.attachment_records(scope, [(a.file_id, 1), (b.file_id, 1)])
            assert [r.workspace_path.rsplit("/", 1)[1] for r in records] == ["a.txt", "b.txt"]  # type: ignore[union-attr]
        finally:
            await again.close()

    asyncio.run(scenario())


def test_an_already_placed_upload_needs_no_stored_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data", ScriptedAdapter([final("ok")]))
        try:
            scope, folder = await conversation(app)
            record = await app.upload(scope, "a.txt", None, chunks(b"a"))
            real_submit = app.runner.submit

            async def busy(scope: TrustedScope, message: UserMessage | None) -> JobStatus:
                raise ControlError(ControlErrorCode.BUSY, "x")

            app.runner.submit = busy  # type: ignore[method-assign]
            with pytest.raises(ControlError):
                await app.send(scope, "x", [record.file_id])
            app.runner.submit = real_submit  # type: ignore[method-assign]
            app.data.blobs.files.remove(record.blob.key)  # e.g. a restored database (review C11)
            await app.send(scope, "x", [record.file_id])
            await app.runner.wait(scope)
            assert (folder / "uploads/a.txt").read_bytes() == b"a"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_refused_attachment_leaves_a_paused_turn_as_it_was(tmp_path: Path) -> None:
    from testkit import RecordingTool

    from himari.data.api import ToolExecutionStatus

    hold = asyncio.Event()

    class Slow(RecordingTool):
        async def run(self, invocation, ctx):  # type: ignore[no-untyped-def]
            await hold.wait()
            return await super().run(invocation, ctx)

    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("slow", {}), ("later", {}))])
        app = await open_app(tmp_path / "data", adapter, tools=[Slow("slow"), RecordingTool("later")])
        try:
            scope, _ = await conversation(app)
            other, _ = await conversation(app)
            await app.send(scope, "go")
            while not adapter.sent:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            app.runner.request_pause(scope)
            hold.set()
            await app.runner.wait(scope)
            events = await app.data.read_events(scope)
            run_id = events[1].origin_run_id
            assert run_id is not None
            before = [e.status for e in await app.data.read_tool_executions(scope, run_id)]
            assert ToolExecutionStatus.PENDING in before  # a paused turn with a call not started
            foreign = await app.upload(other, "a.txt", None, chunks(b"a"))
            with pytest.raises(DataError):
                await app.send(scope, "x", [foreign.file_id])
            after = [e.status for e in await app.data.read_tool_executions(scope, run_id)]
            assert after == before and len(await app.data.read_events(scope)) == len(events)  # review C5
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_name_with_a_lone_surrogate_is_cleaned_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data")
        try:
            scope, _ = await conversation(app)
            record = await app.upload(scope, "a\ud800b.txt", None, chunks(b"a"))
            assert record.name == "a_b.txt"  # PR #59 re-review C7
        finally:
            await app.close()

    asyncio.run(scenario())
