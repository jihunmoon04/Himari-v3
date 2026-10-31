"""MemoryPassLog (V3-P85 §4, §11) contract, on every backend."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import ConversationId, RawRepresentation, RunId, TextBlock, TokenUsage, TrustedScope
from himari.data.api import (
    MAX_MEMORIES,
    AssistantFinal,
    DataError,
    DataErrorCode,
    EpisodePlan,
    MemoryCategory,
    MemoryCursor,
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
