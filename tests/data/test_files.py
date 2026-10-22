"""Files (V3-P66): staged uploads attached by a message, presented files
recorded with a Tool result, and the file store's put, expiry and cleanup —
on every backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Any
from datetime import UTC, datetime, timedelta

import pytest
from testkit import FAKE, data_for, new_store, restart, text_result, user

from himari.common import (
    AttachmentRef,
    CallId,
    CallRef,
    FileId,
    PrincipalId,
    RawRepresentation,
    TextBlock,
    ToolCall,
    ToolId,
    TrustedScope,
    WorkspaceId,
)
from himari.data.api import (
    BLOB_GRACE,
    MAX_STAGED_UPLOADS,
    PRESENT_LIMIT_BYTES,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    FileOrigin,
    InitialActivation,
    PresentedFile,
    PromptBasis,
    RawReceived,
    RunPreparation,
    StoredBlob,
    ToolCompletion,
    ToolExecutionStatus,
    UserMessage,
)
from himari.data.blobs import BlobRow
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
Data = InMemoryData | SqliteData


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def conversation(data: Data, owner: PrincipalId = ALICE) -> TrustedScope:
    cid = await data.create_conversation(owner, title="t", activations=(InitialActivation(FAKE, WorkspaceId("ws")),))
    return TrustedScope(owner, cid)


async def upload(data: Data, scope: TrustedScope, content: bytes = b"hello", name: str = "a.txt") -> FileId:
    blob = await data.blobs.put(scope.principal_id, chunks(content), 1 << 20)
    return (await data.stage_upload(scope, blob, name, "text/plain")).file_id


def message(*file_ids: FileId, text: str = "see") -> UserMessage:
    return UserMessage((TextBlock(text), *(AttachmentRef(f, 1) for f in file_ids)))


async def call(data: Data, scope: TrustedScope, tool: str = "inside/present_file") -> CallRef:
    run_id = await data.prepare_run(scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                                                          (), PromptBasis(0, 0), "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c0"), 0, ToolId(tool), {}),)))
    return CallRef(run_id, CallId("c0"))


async def present(data: Data, scope: TrustedScope, content: bytes, path: str = "/workspace/out.txt",
                  status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED) -> CallRef:
    ref = await call(data, scope)
    blob = await data.blobs.put(scope.principal_id, chunks(content), 1 << 20)
    await data.commit_tool_outcome(scope, ref, ToolCompletion(
        status, text_result("given"), presented_files=(PresentedFile(blob, "out.txt", "text/plain", path),)))
    return ref


def run(coro: Coroutine[Any, Any, None]) -> None:
    asyncio.run(coro)


# --- staged → attached (§4.2) ------------------------------------------------------------


def test_a_message_attaches_its_staged_uploads() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        first, second = await upload(data, scope, b"1"), await upload(data, scope, b"2", "b.txt")
        assert await data.count_staged(scope) == 2
        assert await data.read_files(scope) == ()  # staged ones are not listed
        await data.append_event(scope, message(first, second))
        assert await data.count_staged(scope) == 0
        listed = await data.read_files(scope)
        assert [(f.file_id, f.origin, f.attached, f.version) for f in listed] == [
            (second, FileOrigin.UPLOAD, True, 1), (first, FileOrigin.UPLOAD, True, 1)]
        assert listed[0].size == 1 and not listed[0].expired

    run(scenario())


@pytest.mark.parametrize("case", ["other conversation", "sent already", "twice", "unknown", "presented", "eleven"])
def test_a_message_refuses_an_attachment_it_may_not_carry_and_writes_nothing(case: str) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope, other = await conversation(data), await conversation(data)
        ids = [await upload(data, scope)]
        if case == "other conversation":
            ids = [await upload(data, other)]
        elif case == "sent already":
            await data.append_event(scope, message(*ids))
        elif case == "twice":
            ids = ids * 2
        elif case == "unknown":
            ids = [FileId("file_999")]
        elif case == "presented":
            ref = await present(data, scope, b"made")
            ids = [(await data.read_call_files(scope, [ref]))[ref][0].file_id]
        elif case == "eleven":
            ids = [await upload(data, scope, bytes([i])) for i in range(11)]
        before = await data.read_events(scope)
        with pytest.raises(DataError) as refused:
            await data.append_event(scope, message(*ids))
        assert refused.value.code is DataErrorCode.ATTACHMENT_REFUSED
        with pytest.raises(DataError):
            await data.check_attachments(scope, ids)
        assert await data.read_events(scope) == before

    run(scenario())


def test_the_send_check_refuses_old_staged_uploads_but_the_commit_does_not() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope)
        later = datetime.now(UTC) + timedelta(hours=23, minutes=1)
        with pytest.raises(DataError) as refused:
            await data.check_attachments(scope, [file_id], now=later)
        assert refused.value.file_id == file_id
        assert [r.file_id for r in await data.check_attachments(scope, [file_id])] == [file_id]
        await data.append_event(scope, message(file_id))  # T1 has no cutoff (review N3)

    run(scenario())


def test_an_answer_to_a_question_attaches_too() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope)
        waiting = await call(data, scope, "ask_user")
        commit = await data.answer_pending_call(scope, waiting, message(file_id))
        assert commit.tool_result_event is not None
        assert (await data.read_files(scope))[0].file_id == file_id

    run(scenario())


def test_staged_uploads_are_limited_and_counted_before_bytes() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        for i in range(MAX_STAGED_UPLOADS):
            await upload(data, scope, str(i).encode())
        with pytest.raises(DataError) as refused:
            await upload(data, scope, b"one more")
        assert refused.value.code is DataErrorCode.LIMIT_EXCEEDED
        assert await data.count_staged(scope) == MAX_STAGED_UPLOADS

    run(scenario())


def test_placement_is_recorded_until_the_upload_is_sent() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope)
        await data.set_placement(scope, file_id, "/workspace/conversations/c/uploads/a.txt")
        record, = await data.read_file_records(scope, [(file_id, 1)])
        assert record.workspace_path == "/workspace/conversations/c/uploads/a.txt" and not record.attached
        await data.append_event(scope, message(file_id))
        with pytest.raises(DataError) as refused:
            await data.set_placement(scope, file_id, "/elsewhere")
        assert refused.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    run(scenario())


def test_file_records_are_the_owners_only() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope)
        foreign = TrustedScope(BOB, scope.conversation_id)
        for port in (data.read_files(foreign), data.read_file_records(foreign, [(file_id, 1)]),
                     data.count_staged(foreign), data.set_placement(foreign, file_id, "/x")):
            with pytest.raises(DataError) as refused:
                await port
            assert refused.value.code is DataErrorCode.NOT_AUTHORIZED

    run(scenario())


# --- presented files (§7.1) ------------------------------------------------------------


def test_presenting_the_same_path_again_is_the_next_version() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        first = await present(data, scope, b"v1")
        second = await present(data, scope, b"v2")
        elsewhere = await present(data, scope, b"v3", "/workspace/other.txt")
        files = await data.read_call_files(scope, [first, second, elsewhere])
        assert files[first][0].file_id == files[second][0].file_id != files[elsewhere][0].file_id
        assert [files[r][0].version for r in (first, second, elsewhere)] == [1, 2, 1]
        assert files[second][0].call == second and files[second][0].origin is FileOrigin.PRESENTED

    run(scenario())


def test_presenting_an_upload_path_does_not_continue_the_upload() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope)
        await data.set_placement(scope, file_id, "/workspace/u/a.txt")
        await data.append_event(scope, message(file_id))
        ref = await present(data, scope, b"edited", "/workspace/u/a.txt")
        given = (await data.read_call_files(scope, [ref]))[ref][0]
        assert given.file_id != file_id and given.version == 1  # review C8

    run(scenario())


def test_only_a_succeeded_call_presents_files() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        with pytest.raises(DataError) as refused:
            await present(data, scope, b"x", status=ToolExecutionStatus.FAILED)
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT

    run(scenario())


def test_presenting_is_limited_per_hour_and_refused_whole() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        data.blobs.configure(max_bytes=PRESENT_LIMIT_BYTES * 2, keep_days=30)
        scope = await conversation(data)
        ref = await call(data, scope)
        big = await data.blobs.put(ALICE, chunks(b"a" * (PRESENT_LIMIT_BYTES // 2)), PRESENT_LIMIT_BYTES)
        other = await data.blobs.put(ALICE, chunks(b"b" * (PRESENT_LIMIT_BYTES // 2 + 1)), PRESENT_LIMIT_BYTES)
        with pytest.raises(DataError) as refused:
            await data.commit_tool_outcome(scope, ref, ToolCompletion(
                ToolExecutionStatus.SUCCEEDED, text_result("x"),
                presented_files=(PresentedFile(big, "a", "text/plain", "/workspace/a"),
                                 PresentedFile(other, "b", "text/plain", "/workspace/b"))))
        assert refused.value.code is DataErrorCode.LIMIT_EXCEEDED
        assert await data.presented_bytes_since(scope, datetime.now(UTC) - timedelta(hours=1)) == 0
        assert await data.read_tool_executions(scope, ref.run_id)  # the call is still open: nothing committed

    run(scenario())


def test_a_presented_blob_that_is_gone_is_refused() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        ref = await call(data, scope)
        ghost = StoredBlob("0" * 64, "0" * 64, 3)
        with pytest.raises(DataError) as refused:
            await data.commit_tool_outcome(scope, ref, ToolCompletion(
                ToolExecutionStatus.SUCCEEDED, text_result("x"),
                presented_files=(PresentedFile(ghost, "g", "text/plain", "/workspace/g"),)))
        assert refused.value.code is DataErrorCode.BLOB_MISSING

    run(scenario())


# --- the file store (§4.5, §8) ------------------------------------------------------------


def read(data: Data, blob: StoredBlob) -> bytes:
    with data.blobs.open(blob) as handle:
        return handle.read()


def test_put_dedupes_within_a_principal_only() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        one = await data.blobs.put(ALICE, chunks(b"same", b" bytes"), 100)
        two = await data.blobs.put(ALICE, chunks(b"same bytes"), 100)
        three = await data.blobs.put(BOB, chunks(b"same bytes"), 100)
        assert one == two and one.key != three.key and one.sha256 == three.sha256
        assert data.blobs.rows.live_total() == 20
        assert read(data, one) == b"same bytes"

    run(scenario())


def test_put_stops_reading_past_the_limit_and_keeps_nothing() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with pytest.raises(DataError) as refused:
            await data.blobs.put(ALICE, chunks(b"1234", b"5678"), 6)
        assert refused.value.code is DataErrorCode.TOO_LARGE
        assert data.blobs.rows.known_keys() == frozenset() and not list(data.blobs.files.listing())

    run(scenario())


def test_a_lost_file_is_put_back_by_the_next_put_of_its_content() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        blob = await data.blobs.put(ALICE, chunks(b"keep"), 100)
        data.blobs.files.remove(blob.key)  # e.g. the database was restored from a backup (review B1)
        with pytest.raises(DataError) as missing:
            data.blobs.open(blob)
        assert missing.value.code is DataErrorCode.BLOB_MISSING
        assert await data.blobs.put(ALICE, chunks(b"keep"), 100) == blob
        assert read(data, blob) == b"keep"
        assert data.blobs.rows.live_total() == 4  # counted once

    run(scenario())


def test_a_leftover_file_without_a_live_row_is_replaced() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        blob = await data.blobs.put(ALICE, chunks(b"left"), 100)
        data.blobs.rows.expire_blobs([blob.key], datetime.now(UTC))  # expired, file not yet removed (§8.5)
        assert await data.blobs.put(ALICE, chunks(b"left"), 100) == blob  # no EEXIST (review B1)
        assert read(data, blob) == b"left"

    run(scenario())


def aged(data: Data, blob: StoredBlob, hours: float) -> None:
    row = data.blobs.rows.blob_row(blob.key)
    assert row is not None
    data.blobs.rows.touch_blob(BlobRow(row.key, row.owner, row.sha256, row.size,
                                       datetime.now(UTC) - timedelta(hours=hours)))


def test_a_full_store_expires_the_oldest_just_enough() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        data.blobs.configure(max_bytes=30, keep_days=30)
        scope = await conversation(data)
        sent = [await upload(data, scope, bytes([i]) * 10, f"{i}.bin") for i in range(3)]
        await data.append_event(scope, message(*sent))  # attached: they may expire
        old = [r.blob for r in await data.read_file_records(scope, [(f, 1) for f in sent])]
        for i, blob in enumerate(old):
            aged(data, blob, 10 - i)  # old[0] is the oldest
        new = await data.blobs.put(ALICE, chunks(b"n" * 15), 100)
        expired = [data.blobs.rows.blob_row(b.key).expired_at is not None for b in old]  # type: ignore[union-attr]
        assert expired == [True, True, False]
        assert read(data, new) == b"n" * 15
        with pytest.raises(DataError) as gone:
            data.blobs.open(old[0])
        assert gone.value.code is DataErrorCode.BLOB_EXPIRED
        listed = {f.blob.key: f.expired for f in await data.read_files(scope)}
        assert listed[old[0].key] and not listed[old[2].key]  # the record stays, marked expired

    run(scenario())


def test_when_room_cannot_be_made_nothing_expires() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        data.blobs.configure(max_bytes=30, keep_days=30)
        recent = await data.blobs.put(ALICE, chunks(b"r" * 25), 100)  # within the grace
        old = await data.blobs.put(ALICE, chunks(b"o" * 5), 100)
        aged(data, old, 5)
        with pytest.raises(DataError) as full:
            await data.blobs.put(ALICE, chunks(b"x" * 12), 100)  # needs 7, only 5 may expire (review B2)
        assert full.value.code is DataErrorCode.STORE_FULL
        assert data.blobs.rows.blob_row(old.key).expired_at is None  # type: ignore[union-attr]
        assert read(data, old) == b"o" * 5 and read(data, recent) == b"r" * 25

    run(scenario())


def test_a_staged_uploads_blob_never_expires() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        data.blobs.configure(max_bytes=20, keep_days=1)
        scope = await conversation(data)
        file_id = await upload(data, scope, b"s" * 15)
        blob = (await data.read_file_records(scope, [(file_id, 1)]))[0].blob
        aged(data, blob, 48)
        await data.sweep_blobs(datetime.now(UTC))  # past the keep days, but staged
        with pytest.raises(DataError) as full:
            await data.blobs.put(ALICE, chunks(b"y" * 10), 100)
        assert full.value.code is DataErrorCode.STORE_FULL
        await data.append_event(scope, message(file_id))  # still sendable

    run(scenario())


def test_the_sweep_expires_past_the_keep_days_and_drops_what_nothing_refers_to() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        data.blobs.configure(max_bytes=1000, keep_days=30)
        scope = await conversation(data)
        file_id = await upload(data, scope, b"kept record")
        await data.append_event(scope, message(file_id))
        kept = (await data.read_file_records(scope, [(file_id, 1)]))[0].blob
        loose_old = await data.blobs.put(ALICE, chunks(b"loose old"), 100)
        loose_new = await data.blobs.put(ALICE, chunks(b"loose new"), 100)
        aged(data, kept, 24 * 31)
        aged(data, loose_old, 2)
        await data.sweep_blobs(datetime.now(UTC))
        assert data.blobs.rows.blob_row(kept.key).expired_at is not None  # type: ignore[union-attr]
        assert data.blobs.files.inspect(kept.key) is None
        assert (await data.read_files(scope))[0].expired  # the record stays
        assert data.blobs.rows.blob_row(loose_old.key) is None and data.blobs.files.inspect(loose_old.key) is None
        assert read(data, loose_new) == b"loose new"  # within the grace

    run(scenario())


def test_stale_staged_records_are_dropped_and_their_blobs_follow() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope, b"never sent")
        blob = (await data.read_file_records(scope, [(file_id, 1)]))[0].blob
        assert await data.drop_stale_staged(datetime.now(UTC) - timedelta(hours=24)) == 0
        assert await data.drop_stale_staged(datetime.now(UTC) + timedelta(seconds=1)) == 1
        aged(data, blob, 2)
        await data.sweep_blobs(datetime.now(UTC))
        assert data.blobs.rows.blob_row(blob.key) is None

    run(scenario())


def test_a_purge_releases_blobs_no_other_conversation_uses() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope, other = await conversation(data), await conversation(data)
        shared_a, shared_b = await upload(data, scope, b"shared"), await upload(data, other, b"shared")
        own = await upload(data, scope, b"own")
        await data.append_event(scope, message(shared_a, own))
        await data.append_event(other, message(shared_b))
        records = await data.read_file_records(scope, [(shared_a, 1), (own, 1)])
        for record in records:
            aged(data, record.blob, 2)
        await data.trash_conversation(scope)
        await data.begin_purge(scope)
        await data.purge_conversation(scope)
        shared, mine = records[0].blob, records[1].blob
        assert read(data, shared) == b"shared"
        assert data.blobs.rows.blob_row(mine.key) is None and data.blobs.files.inspect(mine.key) is None

    run(scenario())


def test_a_purge_keeps_a_blob_put_again_within_the_grace() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        file_id = await upload(data, scope, b"again")
        await data.append_event(scope, message(file_id))
        blob = (await data.read_file_records(scope, [(file_id, 1)]))[0].blob
        await data.trash_conversation(scope)
        await data.begin_purge(scope)
        await data.blobs.put(ALICE, chunks(b"again"), 100)  # a new upload, not yet recorded
        await data.purge_conversation(scope)
        assert read(data, blob) == b"again"
        assert data.blobs.rows.blob_row(blob.key) is not None

    run(scenario())


def test_records_survive_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        scope = await conversation(data)
        file_id = await upload(data, scope, b"durable")
        await data.append_event(scope, message(file_id))
        again = data_for(restart(store))
        record, = await again.read_files(scope)
        assert record.file_id == file_id and read(again, record.blob) == b"durable"

    run(scenario())


def test_the_grace_is_an_hour() -> None:
    assert BLOB_GRACE == timedelta(hours=1)
