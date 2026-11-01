"""MemoryPassLog (V3-P85 §4, §11) contract, on every backend."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import (
    CallId,
    CallRef,
    ConversationId,
    ModelToolResult,
    PrincipalId,
    RawRepresentation,
    RunId,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    TrustedScope,
)
from himari.data.api import (
    MAX_MEMORIES,
    AssistantFinal,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    EpisodePlan,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MemoryCursor,
    NoteDates,
    MemoryOrigin,
    PassAction,
    PassItem,
    PassKind,
    PassStart,
    PassStatus,
    PromptBasis,
    RawNoResponse,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    memory_from_record,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore

ME = SCOPE.principal_id
OTHER = TrustedScope(ME, ConversationId("conv_other"))
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
USER, PROJECT, LOOP = MemoryCategory.USER, MemoryCategory.PROJECT, MemoryCategory.LOOP

type Data = InMemoryData | SqliteData


def fresh(*scopes: TrustedScope) -> tuple[InMemoryStore | SqliteStore, Data]:
    store = new_store()
    for scope in scopes or (SCOPE,):
        store.create_conversation(scope)
    return store, data_for(store)


async def prepared_run(data: Data, scope: TrustedScope) -> RunId:
    return await data.prepare_run(scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                                                        (), PromptBasis(0, 0), "p", "m"))


async def turn(data: Data, scope: TrustedScope, said: str, answered: str = "Sure.") -> int:
    """A user message and its final answer; the answer's sequence."""
    await data.append_event(scope, user(said))
    run_id = await prepared_run(data, scope)
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    projected = await data.project_model_output(scope, run_id, AssistantFinal((TextBlock(answered),)))
    return projected.event.sequence


def add(item: int, text: str, category: MemoryCategory = USER, **fields: object) -> PassItem:
    return PassItem(item, "{}", PassAction.ADD, category=category, text=text, **fields)  # type: ignore[arg-type]


def episode(text: str = "Talked about HIMARI.", users: Sequence[int] = (1,), **fields: object) -> EpisodePlan:
    return EpisodePlan(text, NOW, NOW, tuple(users), (), **fields)  # type: ignore[arg-type]


async def live(data: Data, scope: TrustedScope, through: int, *, start: int | None = None,
               snapshot: int | None = None) -> str:
    cursor = await data.read_memory_cursor(scope)
    begin = cursor.through_sequence if start is None else start
    return await data.begin_pass(scope, PassStart(PassKind.LIVE, begin, through,
                                                  through if snapshot is None else snapshot, "p", "m"))


def code(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


def test_a_live_pass_adds_notes_and_an_episode_and_moves_the_cursor_kept_across_a_restart() -> None:
    async def scenario() -> None:
        store, data = fresh()
        last = await turn(data, SCOPE, "I build HIMARI. My interview is on Nov 4.")
        pass_id = await live(data, SCOPE, last)
        due = datetime(2026, 11, 4, 14, 59, tzinfo=UTC)
        result = await data.apply_pass(pass_id, [
            add(0, "The user builds HIMARI."),
            add(1, "Interview on Nov 4.", LOOP, due_at="2026-11-04", due_instant=due),
            PassItem(2, "{}", PassAction.DROP, "bad_evidence"),
            PassItem(3, "{}", PassAction.PROPOSE, "sensitive_proposed", category=USER, text="Sees a doctor."),
        ], episode(), now=NOW)
        assert (result.status, result.outcomes, result.episode_id) == (
            PassStatus.APPLIED, ("applied", "applied", "bad_evidence", "sensitive_proposed"), 1)
        data = data_for(restart(store))
        notes = {n.text: n for n in await data.list_memories(ME)}
        assert set(notes) == {"The user builds HIMARI.", "Interview on Nov 4."}  # the proposal is not a note
        loop = notes["Interview on Nov 4."]
        assert (loop.origin, loop.source_conversation, loop.source_pass, loop.source_item, loop.due_at,
                loop.due_instant) == (MemoryOrigin.EXTRACTOR, SCOPE.conversation_id, pass_id, 1, "2026-11-04", due)
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor(last)
        (written,) = await data.list_episodes(ME)
        assert (written.episode_id, written.from_sequence, written.through_sequence, written.text,
                written.user_sequences, written.origin) == (1, 0, last, "Talked about HIMARI.", (1,),
                                                            MemoryOrigin.EXTRACTOR)
        assert await data.last_episode(SCOPE) == written
        (record,) = await data.list_passes(SCOPE)
        assert (record.pass_id, record.status, record.finished_at is not None) == (pass_id, PassStatus.APPLIED, True)
        assert await data.read_pass_outcomes(pass_id) == result.outcomes

    asyncio.run(scenario())


def test_a_pass_is_discarded_when_its_conversation_changed_meanwhile() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        last = await turn(data, SCOPE, "I build HIMARI.")
        pass_id = await live(data, SCOPE, last)
        await data.append_event(SCOPE, user("one more thing"))  # sent while the model was asked
        result = await data.apply_pass(pass_id, [add(0, "The user builds HIMARI.")], episode(), now=NOW)
        assert (result.status, result.reason) == (PassStatus.DISCARDED, "new_events")
        assert await data.list_memories(ME) == () and await data.list_episodes(ME) == ()
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor()
        assert (await data.read_pass(pass_id)).status is PassStatus.DISCARDED

        last = await turn(data, OTHER, "I write Python.")
        pass_id = await live(data, OTHER, last)
        await data.trash_conversation(OTHER)  # trashed meanwhile
        result = await data.apply_pass(pass_id, [add(0, "Writes Python.")], None, now=NOW)
        assert (result.status, result.reason) == (PassStatus.DISCARDED, "conversation_gone")
        await data.restore_conversation(OTHER)
        assert await data.read_memory_cursor(OTHER) == MemoryCursor()  # read again later

        pass_id = await live(data, OTHER, last)
        await prepared_run(data, OTHER)  # a job started and its Run is not finished
        result = await data.apply_pass(pass_id, [add(0, "Writes Python.")], None, now=NOW)
        assert result.reason in ("new_events", "unfinished_work")
        assert await data.list_memories(ME) == ()

    asyncio.run(scenario())


def test_a_pass_whose_conversation_was_deleted_for_good_is_discarded_with_everything_it_named() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        last = await turn(data, SCOPE, "I build HIMARI.")
        first = await live(data, SCOPE, last)
        await data.apply_pass(first, [add(0, "The user builds HIMARI.")], episode(), now=NOW)
        await turn(data, SCOPE, "And I like tea.")
        running = await live(data, SCOPE, 4)
        kept = await data.save_memory(ME, PROJECT, "Kept by the user.")
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        result = await data.apply_pass(running, [add(0, "Likes tea.")], episode(), now=NOW)
        assert (result.status, result.reason) == (PassStatus.DISCARDED, "conversation_gone")
        assert await data.list_memories(ME) == (kept,)  # its notes and episodes went with it (D5)
        assert await data.list_episodes(ME) == ()
        with pytest.raises(DataError) as error:
            await data.read_pass(first)
        assert code(error) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())


def test_items_leave_the_users_notes_alone_and_check_revisions_and_the_limit() -> None:
    async def scenario() -> None:
        _, data = fresh()
        mine = await data.save_memory(ME, USER, "Lives in Seoul.")
        last = await turn(data, SCOPE, "I build HIMARI.")
        first = await live(data, SCOPE, last)
        await data.apply_pass(first, [add(0, "Builds HIMARI.", PROJECT)], None, now=NOW)
        auto = next(n for n in await data.list_memories(ME) if n.text == "Builds HIMARI.")
        last = await turn(data, SCOPE, "Now I build HIMARI v3, and I moved to Busan.")
        second = await live(data, SCOPE, last)
        result = await data.apply_pass(second, [
            PassItem(0, "{}", PassAction.UPDATE, memory_id=mine.memory_id, expected_revision=1, category=USER,
                     text="Lives in Busan."),
            PassItem(1, "{}", PassAction.UPDATE, memory_id=auto.memory_id, expected_revision=7, category=PROJECT,
                     text="Builds HIMARI v3."),
            PassItem(2, "{}", PassAction.UPDATE, memory_id=auto.memory_id, expected_revision=1, category=PROJECT,
                     text="Builds HIMARI v3."),
            PassItem(3, "{}", PassAction.FORGET, memory_id=999, expected_revision=1),
        ], None, now=NOW)
        assert result.outcomes == ("user_owned", "revision_conflict", "applied", "not_found")
        assert (await data.read_memory(ME, mine.memory_id)).text == "Lives in Seoul."  # the user's stays
        rewritten = await data.read_memory(ME, auto.memory_id)
        assert (rewritten.text, rewritten.revision, rewritten.source_pass, rewritten.source_item) == (
            "Builds HIMARI v3.", 2, second, 2)

        last = await turn(data, SCOPE, "x" * 30)
        third = await live(data, SCOPE, last)
        result = await data.apply_pass(third, [
            add(0, "A loop too long. " * 30, LOOP),
            add(1, "A loop with an end.", LOOP, valid_until="2026-11-01", valid_instant=NOW),
        ], None, now=NOW)
        assert result.outcomes == ("too_large", "invalid")

    asyncio.run(scenario())


def test_an_automatic_forget_retires_the_note_out_of_every_list_and_count() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "My interview is on Nov 4.")
        first = await live(data, SCOPE, last)
        await data.apply_pass(first, [add(0, "Interview on Nov 4.", LOOP)], None, now=NOW)
        (loop,) = await data.list_memories(ME)
        last = await turn(data, SCOPE, "The interview went well.")
        second = await live(data, SCOPE, last)
        result = await data.apply_pass(second, [
            PassItem(0, "{}", PassAction.FORGET, memory_id=loop.memory_id, expected_revision=loop.revision)],
            None, now=NOW)
        assert result.outcomes == ("applied",)
        assert await data.list_memories(ME) == ()
        with pytest.raises(DataError) as error:
            await data.read_memory(ME, loop.memory_id)
        assert code(error) is DataErrorCode.NOT_FOUND
        assert await data.count_source_memories(SCOPE) == 0
        added = await data.save_memory(ME, USER, "A new note.")
        assert added.memory_id == loop.memory_id + 1  # the retired id is never given out again

    asyncio.run(scenario())


def test_an_add_past_the_limit_is_recorded_not_applied() -> None:
    async def scenario() -> None:
        _, data = fresh()
        for n in range(MAX_MEMORIES):
            await data.save_memory(ME, USER, f"Note {n}.")
        last = await turn(data, SCOPE, "I build HIMARI.")
        pass_id = await live(data, SCOPE, last)
        result = await data.apply_pass(pass_id, [add(0, "Builds HIMARI.")], None, now=NOW)
        assert result.outcomes == ("memory_full",)
        assert len(await data.list_memories(ME)) == MAX_MEMORIES

    asyncio.run(scenario())


def test_an_episode_is_extended_only_when_it_is_still_the_passes_own_and_recent() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "Let's fix the bubbles.")
        first = await live(data, SCOPE, last)
        await data.apply_pass(first, [], episode("Fixed the bubbles."), now=NOW)
        (one,) = await data.list_episodes(ME)
        last = await turn(data, SCOPE, "And the hotkey.")
        second = await live(data, SCOPE, last)
        result = await data.apply_pass(second, [], EpisodePlan(
            "Fixed the bubbles and the hotkey.", NOW + timedelta(hours=1), NOW + timedelta(hours=1), (3,), (4,),
            extend=one.episode_id, expected_revision=1), now=NOW)
        assert result.episode_id == one.episode_id
        (extended,) = await data.list_episodes(ME)
        assert (extended.text, extended.revision, extended.from_sequence, extended.through_sequence,
                extended.user_sequences, extended.himari_sequences) == (
            "Fixed the bubbles and the hotkey.", 2, 0, last, (1, 3), (4,))
        last = await turn(data, SCOPE, "Next, the settings page.")
        third = await live(data, SCOPE, last)
        result = await data.apply_pass(third, [], EpisodePlan(  # a stale revision: a new episode instead
            "Started on the settings page.", NOW + timedelta(hours=2), NOW + timedelta(hours=2), (5,), (),
            extend=one.episode_id, expected_revision=1), now=NOW)
        assert result.episode_id == 2
        last = await turn(data, SCOPE, "Back to it a day later.")
        fourth = await live(data, SCOPE, last)
        result = await data.apply_pass(fourth, [], EpisodePlan(  # too long after it: a new one too
            "Finished the settings page.", NOW + timedelta(days=1), NOW + timedelta(days=1), (7,), (),
            extend=2, expected_revision=1), now=NOW)
        assert result.episode_id == 3
        assert [e.episode_id for e in await data.list_episodes(ME)] == [3, 2, 1]  # newest first

    asyncio.run(scenario())


def test_failures_count_from_one_cursor_and_the_third_skips_the_part() -> None:
    async def scenario() -> None:
        store, data = fresh()
        last = await turn(data, SCOPE, "I build HIMARI.")
        retry_at = NOW + timedelta(minutes=30)
        for expected in (1, 2):
            pass_id = await live(data, SCOPE, last)
            assert await data.fail_pass(pass_id, "provider_error", retry_at) is PassStatus.FAILED
            assert await data.read_memory_cursor(SCOPE) == MemoryCursor(0, expected, retry_at)
        pass_id = await live(data, SCOPE, last)
        data = data_for(restart(store))  # a stop while it ran: interrupted at the next start, counted
        assert await data.interrupt_running_passes(retry_at) == 1
        assert (await data.read_pass(pass_id)).status is PassStatus.SKIPPED
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor(last)  # past the part
        with pytest.raises(DataError) as error:
            await data.fail_pass(pass_id, "again", retry_at)
        assert code(error) is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_a_retry_reads_a_skipped_part_once_and_only_proposes() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "I live in Tokyo.")
        for _ in range(3):
            skipped = await live(data, SCOPE, last)
            status = await data.fail_pass(skipped, "provider_error", NOW)
        assert status is PassStatus.SKIPPED
        later = await turn(data, SCOPE, "I moved to Osaka.")
        newer = await live(data, SCOPE, later)
        await data.apply_pass(newer, [add(0, "Lives in Osaka.")], episode(users=(3,)), now=NOW)
        (osaka,) = await data.list_memories(ME)

        retry = await data.begin_pass(SCOPE, PassStart(PassKind.RETRY, 0, last, later, "p", "m", retry_of=skipped))
        result = await data.apply_pass(retry, [
            add(0, "Lives in Tokyo."),
            PassItem(1, "{}", PassAction.UPDATE, memory_id=osaka.memory_id, expected_revision=1, category=USER,
                     text="Lives in Tokyo."),
        ], episode("Said they live in Tokyo.", extend=1, expected_revision=1), now=NOW)
        assert (result.status, result.outcomes, result.episode_id) == (
            PassStatus.APPLIED, ("proposed", "past_range"), 2)  # an episode of its own, never extended
        assert await data.list_memories(ME) == (osaka,)
        assert (await data.read_pass(skipped)).status is PassStatus.RETRIED
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor(later)  # the live cursor stays

        again = await data.begin_pass(SCOPE, PassStart(PassKind.RETRY, 0, last, later, "p", "m", retry_of=skipped))
        result = await data.apply_pass(again, [add(0, "Lives in Tokyo.")], None, now=NOW)
        assert (result.status, result.reason) == (PassStatus.DISCARDED, "already_read")

    asyncio.run(scenario())


def test_one_pass_at_a_time_per_principal_and_only_on_a_live_conversation() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        last = await turn(data, SCOPE, "I build HIMARI.")
        other = await turn(data, OTHER, "I write Python.")
        running = await live(data, SCOPE, last)
        with pytest.raises(DataError) as error:
            await live(data, OTHER, other)
        assert code(error) is DataErrorCode.INVALID_STATE_TRANSITION
        await data.fail_pass(running, "x", NOW)
        await data.trash_conversation(OTHER)
        with pytest.raises(DataError) as error:
            await live(data, OTHER, other)
        assert code(error) is DataErrorCode.NOT_FOUND
        with pytest.raises(DataError) as error:  # a part must end after it starts, within the snapshot
            await live(data, SCOPE, last, start=last)
        assert code(error) is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_memory_due_finds_settled_quiet_conversations_with_a_user_message_after_the_cursor() -> None:
    async def scenario() -> None:
        scopes = [TrustedScope(ME, ConversationId(f"conv_{n}")) for n in range(5)]
        _, data = fresh(*scopes)
        for scope in scopes:
            await turn(data, scope, "hello there, this is a message")
        await prepared_run(data, scopes[1])  # unfinished work
        await data.trash_conversation(scopes[2])
        last = await turn(data, scopes[3], "more")
        done = await live(data, scopes[3], last)
        await data.apply_pass(done, [], None, now=NOW)  # read through its end
        later = datetime.now(UTC) + timedelta(seconds=1)
        due = await data.memory_due(quiet_before=later, now=later)
        assert [d.scope for d in due] == [scopes[0], scopes[4]]
        assert due[0].cursor == MemoryCursor() and due[0].last_event_at < later
        assert await data.memory_due(quiet_before=later - timedelta(hours=1), now=later) == ()  # not quiet yet
        page = await data.memory_due(quiet_before=later, now=later, limit=1)
        assert [d.scope for d in page] == [scopes[0]]
        rest = await data.memory_due(quiet_before=later, now=later, after=page[-1].scope.conversation_id)
        assert [d.scope for d in rest] == [scopes[4]]
        failed = await live(data, scopes[0], 2)
        await data.fail_pass(failed, "x", later + timedelta(minutes=30))
        assert [d.scope for d in await data.memory_due(quiet_before=later, now=later)] == [scopes[4]]  # waits
        assert await data.unfinished_work(scopes[1]) and not await data.unfinished_work(scopes[0])

    asyncio.run(scenario())


def test_the_daily_usage_counts_reservations_then_reported_or_unmetered_use() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "I build HIMARI.")
        since = datetime.now(UTC) - timedelta(minutes=1)
        pass_id = await live(data, SCOPE, last)
        first = await data.prepare_pass_call(pass_id, b'{"ask": 1}', "application/json", 1000)
        assert await data.pass_usage(ME, since) == 1000  # reserved while in flight
        await data.mark_pass_call_dispatched(pass_id, first)
        await data.record_pass_call(pass_id, first, RawReceived(b"{}", "application/json",
                                                                TokenUsage(input_tokens=900, output_tokens=300)))
        assert await data.pass_usage(ME, since) == 1200  # the reported use, past its reservation
        second = await data.prepare_pass_call(pass_id, b'{"ask": 2}', "application/json", 500)
        await data.record_pass_call(pass_id, second, RawNoResponse("timeout"))
        record = await data.read_pass(pass_id)
        assert (record.calls, record.input_tokens, record.output_tokens, record.unmetered_tokens,
                record.over_estimate, record.reserved_tokens, record.charged_tokens) == (2, 900, 300, 500, 1, 0, 1700)
        assert await data.read_pass_response(pass_id, first) == b"{}"
        assert await data.pass_usage(ME, datetime.now(UTC) + timedelta(minutes=1)) == 0  # begun before that
        with pytest.raises(DataError) as error:
            await data.record_pass_call(pass_id, first, RawReceived(b"{}", "application/json"))
        assert code(error) is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_the_cleanup_drops_stale_loops_expired_facts_old_retired_notes_and_proposals() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "Plans and facts.")
        pass_id = await live(data, SCOPE, last)
        now = datetime.now(UTC)
        await data.apply_pass(pass_id, [
            add(0, "Old interview.", LOOP, due_at="2026-01-01", due_instant=now - timedelta(days=15)),
            add(1, "Interview soon.", LOOP, due_at="2026-11-04", due_instant=now + timedelta(days=4)),
            add(2, "Was in Busan.", USER, valid_until="2026-01-01", valid_instant=now - timedelta(days=31)),
            add(3, "Is in Busan.", USER, valid_until="2026-11-07", valid_instant=now + timedelta(days=7)),
            add(4, "To drop.", PROJECT),
            PassItem(5, "{}", PassAction.PROPOSE, "proposed", category=USER, text="Proposed."),
        ], None, now=now)
        drop = next(n for n in await data.list_memories(ME) if n.text == "To drop.")
        last = await turn(data, SCOPE, "Drop it.")
        forget = await live(data, SCOPE, last)
        await data.apply_pass(forget, [PassItem(0, "{}", PassAction.FORGET, memory_id=drop.memory_id,
                                                expected_revision=1)], None, now=now)
        cleaned = await data.clean_memories(now + timedelta(days=1))
        assert (cleaned.stale_loops, cleaned.expired_facts, cleaned.retired, cleaned.proposals) == (1, 1, 0, 0)
        assert {n.text for n in await data.list_memories(ME)} == {"Interview soon.", "Is in Busan."}
        cleaned = await data.clean_memories(now + timedelta(days=31))  # the soon one is 27 days past its date now
        assert (cleaned.stale_loops, cleaned.retired, cleaned.proposals) == (1, 1, 1)
        assert {n.text for n in await data.list_memories(ME)} == {"Is in Busan."}  # 24 days past its end
        assert (await data.read_pass_outcomes(pass_id))[5] == "discarded"

    asyncio.run(scenario())


def test_a_call_that_may_have_been_sent_counts_its_reservation_whatever_ends_the_pass() -> None:
    # PR #135 review P1: failing, interrupting or discarding a pass never drops an unsettled reservation.
    async def scenario() -> None:
        store, data = fresh(SCOPE, OTHER)
        since = datetime.now(UTC) - timedelta(minutes=1)
        last = await turn(data, SCOPE, "I build HIMARI.")
        failed = await live(data, SCOPE, last)
        sent = await data.prepare_pass_call(failed, b"{}", "application/json", 800)
        await data.mark_pass_call_dispatched(failed, sent)  # sent, no answer recorded
        await data.fail_pass(failed, "timeout", NOW)
        assert (await data.read_pass(failed)).unmetered_tokens == 800

        never = await live(data, SCOPE, last)
        await data.prepare_pass_call(never, b"{}", "application/json", 500)  # stored, never sent
        await data.fail_pass(never, "provider refused before send", NOW)
        assert (await data.read_pass(never)).charged_tokens == 0

        other = await turn(data, OTHER, "I write Python.")
        cut = await live(data, OTHER, other)
        call = await data.prepare_pass_call(cut, b"{}", "application/json", 700)
        await data.mark_pass_call_dispatched(cut, call)
        data = data_for(restart(store))
        await data.interrupt_running_passes(NOW)  # a stop while it waited for the answer
        record = await data.read_pass(cut)
        assert (record.status, record.reserved_tokens, record.unmetered_tokens) == (PassStatus.INTERRUPTED, 0, 700)

        discarded = await live(data, OTHER, other, start=0)
        call = await data.prepare_pass_call(discarded, b"{}", "application/json", 300)
        await data.mark_pass_call_dispatched(discarded, call)
        await data.append_event(OTHER, user("one more"))
        assert (await data.apply_pass(discarded, [], None, now=NOW)).status is PassStatus.DISCARDED
        assert (await data.read_pass(discarded)).unmetered_tokens == 300
        assert await data.pass_usage(ME, since) == 800 + 700 + 300

    asyncio.run(scenario())


def test_the_daily_usage_counts_each_call_on_the_day_it_was_prepared() -> None:
    # PR #135 re-review P2: a pass begun before midnight whose second call is sent after it.
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "I build HIMARI.")
        pass_id = await live(data, SCOPE, last)
        first = await data.prepare_pass_call(pass_id, b"{}", "application/json", 1000)
        await data.mark_pass_call_dispatched(pass_id, first)
        await data.record_pass_call(pass_id, first, RawReceived(b"x", "application/json"))  # malformed, no usage
        midnight = datetime.now(UTC)
        await asyncio.sleep(0.001)
        second = await data.prepare_pass_call(pass_id, b"{}", "application/json", 600)
        assert await data.pass_usage(ME, midnight) == 600  # only the call of the new day, still reserved
        await data.mark_pass_call_dispatched(pass_id, second)
        await data.record_pass_call(pass_id, second, RawReceived(b"{}", "application/json",
                                                                 TokenUsage(input_tokens=400, output_tokens=50)))
        assert await data.pass_usage(ME, midnight) == 450
        assert await data.pass_usage(ME, midnight - timedelta(days=1)) == 1450

    asyncio.run(scenario())


# --- part 3: the Tools' dates, Undo's, and the episodes the Current State reads ------------------------------


async def committed(data: Data, scope: TrustedScope, change: MemoryChange) -> None:
    run_id = await prepared_run(data, scope)
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c0"), 0,
                                                                                ToolId("memory_save"), {}),)))
    await data.commit_tool_outcome(scope, CallRef(run_id, CallId("c0")), ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, ModelToolResult((TextBlock("Saved."),)), memory_change=change))


def test_a_tool_writes_a_loops_date_and_an_undo_puts_the_dates_back() -> None:
    async def scenario() -> None:
        _, data = fresh()
        due = NOW + timedelta(days=4)
        loop_id = await data.allocate_memory_id(ME)
        await committed(data, SCOPE, MemoryChange(MemoryChangeKind.SAVE, loop_id, LOOP, "Interview.",
                                                  due_at="2026-11-04", due_instant=due))
        before = await data.read_memory(ME, loop_id)
        assert (before.origin, before.due_at, before.due_instant) == (MemoryOrigin.MODEL, "2026-11-04", due)
        await committed(data, SCOPE, MemoryChange(MemoryChangeKind.SAVE, loop_id, LOOP, "Interview, no date.",
                                                  expected_revision=1))
        assert (await data.read_memory(ME, loop_id)).due_at is None  # the Tool decided: no date
        restored = await data.restore_memory(ME, before, expected_revision=2)
        assert (restored.text, restored.due_at, restored.due_instant) == ("Interview.", "2026-11-04", due)
        with pytest.raises(DataError) as error:  # a loop has a date, not an end
            await committed(data, SCOPE, MemoryChange(MemoryChangeKind.SAVE, await data.allocate_memory_id(ME),
                                                      LOOP, "Trip.", valid_until="2026-11-06", valid_instant=due))
        assert code(error) is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_recent_episodes_leave_out_the_asking_conversation_the_trash_and_the_old() -> None:
    async def scenario() -> None:
        third = TrustedScope(ME, ConversationId("conv_third"))
        store, data = fresh(SCOPE, OTHER, third)
        store.create_conversation(TrustedScope(PrincipalId("bob"), ConversationId("conv_bob")))
        for scope, text, ended in ((SCOPE, "Here.", NOW), (OTHER, "Over there.", NOW - timedelta(hours=1)),
                                   (third, "Long ago.", NOW - timedelta(days=9))):
            last = await turn(data, scope, f"{text} with words enough")
            pass_id = await live(data, scope, last)
            await data.apply_pass(pass_id, [], EpisodePlan(text, ended, ended, (1,), ()), now=NOW)
        await data.rename_conversation(OTHER, "Over")
        found = await data.recent_episodes(ME, since=NOW - timedelta(days=7), besides=SCOPE.conversation_id, limit=5)
        assert [(f.episode.text, f.title) for f in found] == [("Over there.", "Over")]
        assert await data.count_episodes(ME) == 3 and await data.count_episodes(PrincipalId("bob")) == 0
        await data.trash_conversation(OTHER)
        assert await data.recent_episodes(ME, since=NOW - timedelta(days=7), besides=None, limit=5) == (
            await data.recent_episodes(ME, since=NOW - timedelta(days=7), besides=OTHER.conversation_id, limit=5))
        assert [f.episode.text for f in await data.find_episodes(ME, ["LONG"])] == ["Long ago."]  # folded
        assert [f.episode.text for f in await data.find_episodes(ME, [], start=NOW - timedelta(days=10),
                                                                 end=NOW - timedelta(days=8))] == ["Long ago."]
        assert [f.episode.text for f in await data.find_episodes(ME, [])] == ["Here.", "Long ago."]  # not the trash

    asyncio.run(scenario())


def test_episodes_are_ranked_before_the_limit_is_taken() -> None:
    # PR #137 review: the newest 200 were taken first, so an older episode holding every word could be cut.
    async def scenario() -> None:
        _, data = fresh()
        for text, ended in (("Bubbles and hotkey fixed.", NOW - timedelta(days=3)),
                            ("Bubbles again.", NOW - timedelta(days=2)), ("Bubbles once more.", NOW - timedelta(days=1))):
            last = await turn(data, SCOPE, f"{text} with words enough")
            pass_id = await live(data, SCOPE, last)
            await data.apply_pass(pass_id, [], EpisodePlan(text, ended, ended, (last - 1,), ()), now=NOW)
        best = await data.find_episodes(ME, ["bubbles", "hotkey", "bubbles"], limit=1)
        assert [f.episode.text for f in best] == ["Bubbles and hotkey fixed."]  # two words beat one, though older
        assert [f.episode.text for f in await data.find_episodes(ME, ["bubbles"], limit=2)] == [
            "Bubbles once more.", "Bubbles again."]  # one word each: newest first
        first_days = await data.find_episodes(ME, [], start=NOW - timedelta(days=4), limit=2)
        assert [f.episode.text for f in first_days] == ["Bubbles and hotkey fixed.", "Bubbles again."]  # time order
        assert [f.episode.text for f in await data.find_episodes(ME, [], limit=1)] == ["Bubbles once more."]

    asyncio.run(scenario())


# --- part 4: Undo of a pass item, proposals, the Timeline and the status --------------------------------------


def test_an_automatic_forget_is_undone_once_within_its_days_and_the_limit() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "Interview on Nov 4.")
        first = await live(data, SCOPE, last)
        await data.apply_pass(first, [add(0, "Interview on Nov 4.", LOOP, due_at="2026-11-04",
                                          due_instant=NOW + timedelta(days=4))], None, now=NOW)
        (loop,) = await data.list_memories(ME)
        last = await turn(data, SCOPE, "The interview is done.")
        second = await live(data, SCOPE, last)
        retired_at = datetime.now(UTC)
        await data.apply_pass(second, [PassItem(0, "{}", PassAction.FORGET, memory_id=loop.memory_id,
                                                expected_revision=1)], None, now=retired_at)
        (record,) = await data.read_pass_items(second)
        assert (record.outcome, record.target, record.written_revision, record.planned) == (
            "applied", f"m{loop.memory_id}", 2, None)
        before = memory_from_record(record.previous or "")
        assert before is not None and (before.text, before.due_at, before.revision) == (
            "Interview on Nov 4.", "2026-11-04", 1)
        assert await data.memory_revision(ME, loop.memory_id) == (2, True)
        with pytest.raises(DataError) as error:
            await data.unretire_memory(ME, loop.memory_id, expected_revision=1, now=retired_at)
        assert code(error) is DataErrorCode.VERSION_CONFLICT
        with pytest.raises(DataError) as error:  # too late: Undo lasts RETIRED_KEEP
            await data.unretire_memory(ME, loop.memory_id, expected_revision=2, now=retired_at + timedelta(days=30))
        assert code(error) is DataErrorCode.NOT_FOUND
        back = await data.unretire_memory(ME, loop.memory_id, expected_revision=2, now=retired_at)
        assert (back.revision, back.due_at, back.source_pass) == (3, "2026-11-04", first)
        assert await data.list_memories(ME) == (await data.read_memory(ME, loop.memory_id),)
        with pytest.raises(DataError) as error:  # not retired any more
            await data.unretire_memory(ME, loop.memory_id, expected_revision=3, now=retired_at)
        assert code(error) is DataErrorCode.NOT_FOUND
        assert await data.memory_revision(ME, 999) is None

    asyncio.run(scenario())


def test_a_proposal_is_kept_as_the_users_or_discarded_once() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "Sensitive things.")
        pass_id = await live(data, SCOPE, last)
        await data.apply_pass(pass_id, [
            PassItem(0, "{}", PassAction.PROPOSE, "sensitive_proposed", category=USER, text="Takes pills."),
            PassItem(1, "{}", PassAction.PROPOSE, "sensitive_proposed", category=LOOP, text="Doctor on Nov 3.",
                     due_at="2026-11-03", due_instant=NOW + timedelta(days=3)),
            add(2, "Builds HIMARI."),
        ], None, now=NOW)
        await data.rename_conversation(SCOPE, "Health")
        proposals = await data.list_proposals(ME)
        assert [(p.record.item, p.kind, p.title, p.in_trash) for p in proposals] == [
            (0, PassKind.LIVE, "Health", False), (1, PassKind.LIVE, "Health", False)]
        kept = await data.decide_proposal(ME, pass_id, 1, keep=True, now=NOW)
        assert kept is not None and (kept.category, kept.due_at, kept.origin, kept.source_pass, kept.source_item,
                                     kept.user_approved_at) == (LOOP, "2026-11-03", MemoryOrigin.EXTRACTOR, pass_id,
                                                                1, NOW)
        assert await data.decide_proposal(ME, pass_id, 0, keep=False, now=NOW) is None
        assert await data.list_proposals(ME) == ()
        assert (await data.read_pass_outcomes(pass_id)) == ("discarded", "kept", "applied")
        for item in (0, 2):  # decided, or never a proposal
            with pytest.raises(DataError) as error:
                await data.decide_proposal(ME, pass_id, item, keep=True, now=NOW)
            assert code(error) is DataErrorCode.VERSION_CONFLICT
        with pytest.raises(DataError) as error:  # another principal's pass is none of theirs
            await data.decide_proposal(PrincipalId("bob"), pass_id, 0, keep=True, now=NOW)
        assert code(error) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())


def test_a_kept_proposal_waits_while_memory_is_full() -> None:
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "A past part.")
        pass_id = await live(data, SCOPE, last)
        await data.apply_pass(pass_id, [PassItem(0, "{}", PassAction.PROPOSE, "proposed", category=USER,
                                                 text="Lived in Tokyo.")], None, now=NOW)
        for n in range(MAX_MEMORIES):
            await data.save_memory(ME, USER, f"Note {n}.")
        with pytest.raises(DataError) as error:
            await data.decide_proposal(ME, pass_id, 0, keep=True, now=NOW)
        assert code(error) is DataErrorCode.LIMIT_EXCEEDED
        assert len(await data.list_proposals(ME)) == 1

    asyncio.run(scenario())


def test_the_timeline_pages_by_end_and_the_users_rewrite_stops_extending() -> None:
    async def scenario() -> None:
        _, data = fresh()
        for n in range(3):
            last = await turn(data, SCOPE, f"Part {n} with words enough")
            pass_id = await live(data, SCOPE, last)
            ended = NOW + timedelta(hours=n)
            await data.apply_pass(pass_id, [], EpisodePlan(f"Episode {n}.", ended, ended, (last - 1,), ()), now=NOW)
        first = await data.page_episodes(ME, before=None, limit=2)
        assert [f.episode.episode_id for f in first] == [3, 2]
        last_shown = first[-1].episode
        rest = await data.page_episodes(ME, before=(last_shown.ended_at, last_shown.episode_id), limit=2)
        assert [f.episode.episode_id for f in rest] == [1]
        newest = first[0].episode
        mine = await data.update_episode(ME, newest.episode_id, "  My own words.\r\n", expected_revision=1)
        assert (mine.text, mine.origin, mine.revision) == ("My own words.", MemoryOrigin.USER, 2)
        with pytest.raises(DataError) as error:
            await data.update_episode(ME, newest.episode_id, "Again.", expected_revision=1)
        assert code(error) is DataErrorCode.VERSION_CONFLICT
        last = await turn(data, SCOPE, "And more, continuing it")
        pass_id = await live(data, SCOPE, last)
        result = await data.apply_pass(pass_id, [], EpisodePlan(
            "Continued.", NOW + timedelta(hours=2), NOW + timedelta(hours=2), (last - 1,), (),
            extend=newest.episode_id, expected_revision=2), now=NOW)
        assert result.episode_id == 4  # the user's episode is never rewritten by a pass
        assert await data.count_source_episodes(SCOPE) == 4
        await data.delete_episode(ME, 4, expected_revision=1)
        with pytest.raises(DataError) as error:
            await data.delete_episode(ME, 4, expected_revision=1)
        assert code(error) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())


def test_the_status_counts_todays_tokens_the_last_applied_pass_and_recent_trouble() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        since = datetime.now(UTC) - timedelta(minutes=1)
        last = await turn(data, SCOPE, "I build HIMARI.")
        for _ in range(3):
            pass_id = await live(data, SCOPE, last)
            call = await data.prepare_pass_call(pass_id, b"{}", "application/json", 100)
            await data.record_pass_call(pass_id, call, RawNoResponse("http_500"))
            await data.fail_pass(pass_id, "http_500", NOW)
        other = await turn(data, OTHER, "Another one.")
        applied = await live(data, OTHER, other)
        await data.apply_pass(applied, [], None, now=NOW)
        summary = await data.pass_summary(ME, day_start=since, since=since)
        assert summary.charged_today == 300 and summary.failed == 2
        assert [p.status for p in summary.skipped] == [PassStatus.SKIPPED]
        assert summary.last_applied_at == NOW  # the applied pass's own time, not the real clock

    asyncio.run(scenario())


def test_a_proposal_kept_with_a_passed_date_is_refused_unless_its_date_is_dropped_or_moved() -> None:
    # PR #138 review: kept as it was, the hourly cleanup would take it at once.
    async def scenario() -> None:
        _, data = fresh()
        last = await turn(data, SCOPE, "Old plans.")
        pass_id = await live(data, SCOPE, last)
        long_ago, now = NOW - timedelta(days=20), datetime.now(UTC)
        await data.apply_pass(pass_id, [
            PassItem(0, "{}", PassAction.PROPOSE, "proposed", category=LOOP, text="Dentist.", due_at="2026-10-11",
                     due_instant=now - timedelta(days=20)),
            PassItem(1, "{}", PassAction.PROPOSE, "proposed", category=USER, text="Away.", valid_until="2026-10-20",
                     valid_instant=now - timedelta(days=1)),
            PassItem(2, "{}", PassAction.PROPOSE, "proposed", category=LOOP, text="Overdue a little.",
                     due_at="2026-10-28", due_instant=now - timedelta(days=3)),
        ], None, now=long_ago)
        for item in (0, 1):
            with pytest.raises(DataError) as error:
                await data.decide_proposal(ME, pass_id, item, keep=True, now=now)
            assert code(error) is DataErrorCode.STALE_DATE
        assert len(await data.list_proposals(ME)) == 3  # still waiting
        undated = await data.decide_proposal(ME, pass_id, 0, keep=True, now=now, dates=NoteDates())
        assert undated is not None and (undated.text, undated.due_at) == ("Dentist.", None)
        moved = await data.decide_proposal(ME, pass_id, 1, keep=True, now=now,
                                           dates=NoteDates(valid_until="2026-12-01",
                                                           valid_instant=now + timedelta(days=30)))
        assert moved is not None and moved.valid_until == "2026-12-01"
        recent = await data.decide_proposal(ME, pass_id, 2, keep=True, now=now)  # overdue, not yet the cleanup's
        assert recent is not None and recent.due_at == "2026-10-28"

    asyncio.run(scenario())


def test_the_lines_read_the_newest_passes_with_their_shown_items_at_once() -> None:
    async def scenario() -> None:
        _, data = fresh()
        ids = []
        for n in range(3):
            last = await turn(data, SCOPE, f"Part {n} with words enough")
            pass_id = await live(data, SCOPE, last)
            await data.apply_pass(pass_id, [add(0, f"Note {n}."), PassItem(1, "{}", PassAction.DROP, "bad_evidence")],
                                  None, now=NOW)
            ids.append(pass_id)
        last = await turn(data, SCOPE, "Nothing to remember here")
        quiet = await live(data, SCOPE, last)
        await data.apply_pass(quiet, [PassItem(0, "{}", PassAction.DROP, "no_evidence")], None, now=NOW)
        found = await data.applied_pass_items(SCOPE, limit=2)
        assert [p.pass_id for p, _ in found] == ids[1:]  # the newest two that show something, oldest first
        assert [[r.outcome for r in records] for _, records in found] == [["applied"], ["applied"]]
        notes = {n.text: n.memory_id for n in await data.list_memories(ME)}
        assert await data.memory_revisions(ME, [notes["Note 0."], notes["Note 2."], 999]) == {
            notes["Note 0."]: (1, False), notes["Note 2."]: (1, False)}
        assert await data.memory_revisions(ME, []) == {}

    asyncio.run(scenario())


# --- V3-P87 §5.3 (PR #157 review): a reminder survives the cleanup, an Undo and a retirement -------------

async def reminded_loop(data: Data, text: str, remind: datetime, due: tuple[str, datetime] | None = None) -> int:
    """A loop the model saved with a reminder (passes may rewrite or retire its notes)."""
    memory_id = await data.allocate_memory_id(ME)
    await committed(data, SCOPE, MemoryChange(MemoryChangeKind.SAVE, memory_id, LOOP, text,
                                              due_at=None if due is None else due[0],
                                              due_instant=None if due is None else due[1],
                                              remind_at=remind.isoformat(timespec="minutes"), remind_instant=remind))
    return memory_id


def test_the_cleanup_keeps_a_loop_until_its_reminder_is_told() -> None:
    """P1-1: undated and untouched for 90 days, or 14 days past its date: kept while its reminder waits."""
    from himari.data.api import WakeKind, WakeReason, WakeStatus

    async def scenario() -> None:
        _, data = fresh()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        later = now + timedelta(days=200)
        undated = await reminded_loop(data, "Renew the passport.", later)
        dated = await reminded_loop(data, "Dentist.", later, ("2026-01-01", now - timedelta(days=20)))
        cleaned = await data.clean_memories(later - timedelta(days=1))  # both stale by the old rules
        assert cleaned.stale_loops == 0
        assert {n.memory_id for n in await data.list_memories(ME)} == {undated, dated}
        wake = await data.begin_wake(SCOPE, (WakeReason(WakeKind.REMINDER, memory_id=undated),), "d1",
                                     reminders=(undated,))
        cleaned = await data.clean_memories(later - timedelta(days=1))  # claimed: still waiting to be told
        assert cleaned.stale_loops == 0
        await data.end_wake(ME, wake.payload.wake_id, WakeStatus.ANSWERED)  # type: ignore[union-attr]
        cleaned = await data.clean_memories(later + timedelta(days=1))  # told: the usual rule again
        assert cleaned.stale_loops == 1 and [n.memory_id for n in await data.list_memories(ME)] == [dated]

    asyncio.run(scenario())


def test_undoing_an_automatic_rewrite_keeps_the_reminder() -> None:
    """P1-2: the record a pass keeps for Undo holds the reminder; an older record without one reads as before."""
    async def scenario() -> None:
        _, data = fresh()
        remind = (NOW + timedelta(days=3)).replace(second=0, microsecond=0)
        memory_id = await reminded_loop(data, "Take the medicine.", remind)
        last = await turn(data, SCOPE, "It is the evening pills.")
        rewrite = await live(data, SCOPE, last)
        result = await data.apply_pass(rewrite, [PassItem(0, "{}", PassAction.UPDATE, memory_id=memory_id,
                                                          expected_revision=1, category=LOOP,
                                                          text="Take the evening pills.")], None, now=NOW)
        assert result.outcomes == ("applied",)
        rewritten = await data.read_memory(ME, memory_id)
        assert (rewritten.remind_instant, rewritten.remind_state) == (remind, "scheduled")  # a pass keeps it
        (record,) = await data.read_pass_items(rewrite)
        before = memory_from_record(record.previous or "")
        assert before is not None and (before.remind_at, before.remind_instant) == (
            remind.isoformat(timespec="minutes"), remind)
        back = await data.restore_memory(ME, before, expected_revision=rewritten.revision)  # the Undo
        assert (back.text, back.remind_instant, back.remind_state) == ("Take the medicine.", remind, "scheduled")
        old = json.loads(record.previous or "{}")
        old.pop("remind_at"), old.pop("remind_instant")
        assert memory_from_record(json.dumps(old)) is not None  # a record from before reminders

    asyncio.run(scenario())


def test_a_retired_loop_brought_back_keeps_how_its_reminder_went() -> None:
    """P2-1: delivered, retired by a pass, brought back by Undo: still delivered, in both backends."""
    from himari.data.api import WakeKind, WakeReason, WakeStatus

    async def scenario() -> None:
        _, data = fresh()
        remind = (datetime.now(UTC) - timedelta(minutes=5)).replace(second=0, microsecond=0)
        memory_id = await reminded_loop(data, "Call mom.", remind)
        wake = await data.begin_wake(SCOPE, (WakeReason(WakeKind.REMINDER, memory_id=memory_id),), "d1",
                                     reminders=(memory_id,))
        await data.end_wake(ME, wake.payload.wake_id, WakeStatus.ANSWERED)  # type: ignore[union-attr]
        last = await turn(data, SCOPE, "I called her.")
        forget = await live(data, SCOPE, last)
        retired_at = datetime.now(UTC)
        await data.apply_pass(forget, [PassItem(0, "{}", PassAction.FORGET, memory_id=memory_id,
                                                expected_revision=1)], None, now=retired_at)
        back = await data.unretire_memory(ME, memory_id, expected_revision=2, now=retired_at)
        assert (back.remind_instant, back.remind_state) == (remind, "delivered")
        assert (await data.read_memory(ME, memory_id)).remind_state == "delivered"

    asyncio.run(scenario())
