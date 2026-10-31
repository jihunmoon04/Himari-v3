"""Memory passes run (V3-P85 §4.1, §4.5, §4.8): when, how often, and what a failure does."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import pytest

from himari.common import ConversationId, RawRepresentation, TextBlock, TrustedScope
from himari.data.api import (
    AssistantFinal,
    MemoryCategory,
    MemoryCursor,
    PassKind,
    PassStart,
    PassStatus,
    PromptBasis,
    RawReceived,
    RunPreparation,
    UserMessage,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.memory.api import MemoryPassSettings, PassOutcome
from himari.memory.passes import MEMORY_PASS_INSTRUCTIONS, PASS_MAX_OUTPUT_TOKENS
from himari.memory.worker import MemoryPasses
from himari.provider.api import NativeInvocationOutcome, NativeNoResponse, NativePreparedRequest, NormalizedFinal
from himari.provider.scripted import Measured, RawBody, ScriptedAdapter

SCOPE = TrustedScope("alice", ConversationId("conv_1"))  # type: ignore[arg-type]
OTHER = TrustedScope("alice", ConversationId("conv_2"))  # type: ignore[arg-type]
ME = SCOPE.principal_id
LATER = timedelta(minutes=11)  # past the default idle


class Probe:
    def __init__(self) -> None:
        self.busy: set[ConversationId] = set()

    def running(self, scope: TrustedScope) -> bool:
        return scope.conversation_id in self.busy


def reply(*notes: Mapping[str, object], episode: object = None) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(json.dumps({"notes": list(notes), "episode": episode})),))


BUILDS: dict[str, object] = {"op": "add", "category": "project", "text": "Builds HIMARI.", "evidence": [1]}
TALKED = {"op": "add", "text": "Talked about HIMARI.", "user_evidence": [1], "himari_context": [2]}


def setup(*script: object, offset: timedelta = LATER, settings: MemoryPassSettings | None = None,
          adapter: ScriptedAdapter | None = None) -> tuple[InMemoryData, ScriptedAdapter, Probe, MemoryPasses]:
    store = InMemoryStore()
    for scope in (SCOPE, OTHER):
        store.create_conversation(scope)
    data = InMemoryData(store)
    provider = adapter or ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    probe = Probe()
    passes = MemoryPasses(data, provider, settings or MemoryPassSettings("m", time_zone="Asia/Seoul"), probe,
                          clock=lambda: datetime.now(UTC) + offset)
    return data, provider, probe, passes


async def turn(data: InMemoryData, scope: TrustedScope, said: str, answered: str = "Sure.") -> None:
    await data.append_event(scope, UserMessage((TextBlock(said),)))
    run_id = await data.prepare_run(scope, RunPreparation(b"{}", "application/json",
                                                          RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0),
                                                          "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    await data.project_model_output(scope, run_id, AssistantFinal((TextBlock(answered),)))


def test_a_quiet_settled_conversation_is_read_once_into_notes_and_an_episode() -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(Measured(reply(BUILDS, episode=TALKED), 1200, 80))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.APPLIED, PassOutcome.NOTHING]
        (built,) = await data.list_memories(ME)
        assert (built.text, built.category) == ("Builds HIMARI.", MemoryCategory.PROJECT)
        (talked,) = await data.list_episodes(ME)
        assert (talked.text, talked.user_sequences, talked.himari_sequences) == ("Talked about HIMARI.", (1,), (2,))
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor(2)
        (asked,) = provider.prepared_inputs[1:]  # the first was prepared to size the reservation
        assert asked.stable_prefix == (MEMORY_PASS_INSTRUCTIONS,)
        assert asked.request_options.max_output_tokens == PASS_MAX_OUTPUT_TOKENS
        (record,) = await data.list_passes(SCOPE)
        assert (record.kind, record.status, record.calls, record.input_tokens, record.output_tokens) == (
            PassKind.LIVE, PassStatus.APPLIED, 1, 1200, 80)
        assert len(provider.sent) == 1

    asyncio.run(scenario())


def test_nothing_is_read_before_the_quiet_or_while_the_conversation_works() -> None:
    async def scenario() -> None:
        data, provider, probe, passes = setup(offset=timedelta(0))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.WAITING]  # not quiet yet
        data, provider, probe, passes = setup()
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        probe.busy.add(SCOPE.conversation_id)
        assert await passes.read(SCOPE) == [PassOutcome.BUSY]  # its job runs here
        probe.busy.clear()
        await data.prepare_run(SCOPE, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                                                     (), PromptBasis(0, 0), "p", "m"))
        assert await passes.read(SCOPE) == [PassOutcome.BUSY]  # a Run is not finished
        assert provider.sent == [] and await data.list_passes(SCOPE) == ()
        await data.trash_conversation(OTHER)
        assert await passes.read(OTHER) == [PassOutcome.NOTHING]

    asyncio.run(scenario())


def test_a_short_part_waits_unless_a_loop_is_open() -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(reply())
        await turn(data, SCOPE, "합격했어")
        assert await passes.read(SCOPE) == [PassOutcome.WAITING]
        await data.save_memory(ME, MemoryCategory.LOOP, "Interview on Nov 4.")
        assert await passes.read(SCOPE) == [PassOutcome.APPLIED, PassOutcome.NOTHING]

    asyncio.run(scenario())


def test_a_malformed_answer_is_asked_once_more_then_the_pass_fails_and_counts() -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(RawBody(b"not json"), reply(BUILDS))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert (await passes.read(SCOPE))[0] is PassOutcome.APPLIED
        (record,) = await data.list_passes(SCOPE)
        assert record.calls == 2

        data, provider, _, passes = setup(reply(episode={"op": "add"}), NormalizedFinal((TextBlock("sorry"),)))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.FAILED]
        cursor = await data.read_memory_cursor(SCOPE)
        assert (cursor.through_sequence, cursor.failures, cursor.retry_after is not None) == (0, 1, True)
        assert await passes.read(SCOPE) == [PassOutcome.WAITING]  # before its retry time

    asyncio.run(scenario())


def test_the_third_failure_skips_the_part() -> None:
    async def scenario() -> None:
        later = [LATER]
        data, provider, _, passes = setup(*(NativeNoResponse("http_500") for _ in range(3)))
        passes._clock = lambda: datetime.now(UTC) + later[0]  # noqa: SLF001 — past each retry time
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        for expected in (PassOutcome.FAILED, PassOutcome.FAILED, PassOutcome.SKIPPED):
            assert (await passes.read(SCOPE))[0] is expected
            later[0] += timedelta(hours=1)
        assert await data.read_memory_cursor(SCOPE) == MemoryCursor(2)
        assert await passes.read(SCOPE) == [PassOutcome.NOTHING]

    asyncio.run(scenario())


def test_the_daily_budget_is_checked_before_a_pass_begins() -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(reply(BUILDS), settings=MemoryPassSettings("m", daily_tokens=5_000))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.DEFERRED]  # 4,000 for the answer + the request
        assert provider.sent == [] and await data.list_passes(SCOPE) == ()

    asyncio.run(scenario())


class Interrupting(ScriptedAdapter):
    """Answers, after the user sent one more message meanwhile."""

    def __init__(self, data: Callable[[], InMemoryData]) -> None:
        super().__init__(memory_passes=True)
        self._data = data

    async def invoke_prepared(self, request: NativePreparedRequest, sink: object = None,
                              capture: object = None) -> NativeInvocationOutcome:
        await self._data().append_event(SCOPE, UserMessage((TextBlock("one more thing"),)))
        return await super().invoke_prepared(request)


def test_a_message_sent_while_the_model_is_asked_discards_the_pass_and_it_is_read_again() -> None:
    async def scenario() -> None:
        holder: list[InMemoryData] = []
        data, provider, _, passes = setup(reply(BUILDS), adapter=Interrupting(lambda: holder[0]))
        holder.append(data)
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.DISCARDED]
        assert await data.list_memories(ME) == () and await data.read_memory_cursor(SCOPE) == MemoryCursor()

    asyncio.run(scenario())


class Purging(ScriptedAdapter):
    """Answers after the user deleted the Conversation for good meanwhile."""

    def __init__(self, data: Callable[[], InMemoryData]) -> None:
        super().__init__(memory_passes=True)
        self._data = data

    async def invoke_prepared(self, request: NativePreparedRequest, sink: object = None,
                              capture: object = None) -> NativeInvocationOutcome:
        data = self._data()
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        return await super().invoke_prepared(request)


@pytest.mark.parametrize("answer", ["good", "bad"])
def test_a_conversation_deleted_for_good_while_the_model_is_asked_leaves_nothing(answer: str) -> None:
    async def scenario() -> None:
        holder: list[InMemoryData] = []
        step = reply(BUILDS) if answer == "good" else NativeNoResponse("http_500")
        data, provider, _, passes = setup(step, adapter=Purging(lambda: holder[0]))
        holder.append(data)
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        assert await passes.read(SCOPE) == [PassOutcome.NOTHING]
        assert await data.list_memories(ME) == () and await data.list_episodes(ME) == ()

    asyncio.run(scenario())


def test_a_long_conversation_is_read_in_parts_one_after_another() -> None:
    async def scenario() -> None:
        first: dict[str, object] = {"op": "add", "category": "project", "text": "Part one.", "evidence": [1]}
        second: dict[str, object] = {"op": "add", "category": "project", "text": "Part two.", "evidence": [3]}
        data, provider, _, passes = setup(reply(first), reply(second))
        await turn(data, SCOPE, "a" * 15_000)
        await turn(data, SCOPE, "b" * 15_000)
        assert await passes.read(SCOPE) == [PassOutcome.APPLIED, PassOutcome.APPLIED, PassOutcome.NOTHING]
        assert {n.text for n in await data.list_memories(ME)} == {"Part one.", "Part two."}
        assert [(p.from_sequence, p.through_sequence, p.snapshot_sequence) for p in await data.list_passes(SCOPE)] == [
            (0, 2, 4), (2, 4, 4)]

    asyncio.run(scenario())


def test_the_quiet_timer_starts_when_a_job_ends_and_a_new_job_cancels_it() -> None:
    async def scenario() -> None:
        settings = MemoryPassSettings("m", idle=timedelta(milliseconds=50))
        data, provider, _, passes = setup(reply(BUILDS), settings=settings)
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        passes.job_ended(SCOPE, None)
        passes.job_started(SCOPE, None, None)  # a new message came first
        await asyncio.sleep(0.2)
        assert provider.sent == []
        passes.job_ended(SCOPE, None)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if await data.list_memories(ME):
                break
        assert [n.text for n in await data.list_memories(ME)] == ["Builds HIMARI."]
        await passes.close()

    asyncio.run(scenario())


def test_start_counts_passes_cut_short_and_a_scan_reads_every_due_conversation() -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(reply(BUILDS), reply())
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        await turn(data, OTHER, "Another conversation with enough words.")
        stale = await data.begin_pass(SCOPE, PassStart(PassKind.LIVE, 0, 2, 2, "scripted", "m"))
        await passes.start()
        assert (await data.read_pass(stale)).status is PassStatus.INTERRUPTED
        assert (await data.read_memory_cursor(SCOPE)).failures == 1
        passes._clock = lambda: datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001 — past the retry time
        assert await passes.scan() == 2
        assert await data.read_memory_cursor(OTHER) == MemoryCursor(2)
        await passes.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("closed_first", [True, False])
def test_closing_cancels_timers_and_takes_no_new_work(closed_first: bool) -> None:
    async def scenario() -> None:
        data, provider, _, passes = setup(reply(BUILDS), settings=MemoryPassSettings("m", idle=timedelta(0)))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        if closed_first:
            await passes.close()
            passes.job_ended(SCOPE, None)
        else:
            passes.job_ended(SCOPE, None)
            await passes.close()
        await asyncio.sleep(0.05)
        assert provider.sent == []

    asyncio.run(scenario())


def test_a_pass_whose_answer_cannot_be_applied_ends_failed_and_the_next_one_may_begin(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #135 review P1: an error after the answer left the pass RUNNING and blocked the principal.
    from himari.memory import worker

    def broken(*_: object, **__: object) -> list[object]:
        raise RuntimeError("planning failed")

    async def scenario() -> None:
        data, provider, _, passes = setup(reply(BUILDS), reply(BUILDS))
        await turn(data, SCOPE, "I build HIMARI, the companion agent.")
        await turn(data, OTHER, "Another conversation with enough words.")
        monkeypatch.setattr(worker, "plan_items", broken)
        with pytest.raises(RuntimeError):
            await passes.read(SCOPE)
        (record,) = await data.list_passes(SCOPE)
        assert record.status is PassStatus.FAILED and record.error == "could not apply: RuntimeError"
        assert await data.read_pass_response(record.pass_id, 1) is not None  # the answer is kept
        monkeypatch.undo()
        assert (await passes.read(OTHER))[0] is PassOutcome.APPLIED

    asyncio.run(scenario())


def test_events_are_read_a_page_at_a_time_and_only_until_the_part_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #135 review P2-3: a long Conversation was read whole before it was cut.
    from himari.memory import worker

    monkeypatch.setattr(worker, "EVENTS_PAGE", 2)

    async def scenario() -> None:
        first: dict[str, object] = {"op": "add", "category": "project", "text": "Part one.", "evidence": [1]}
        data, provider, _, passes = setup(reply(first), reply())
        for text in ("a" * 15_000, "b" * 15_000, "c" * 30, "d" * 30):
            await turn(data, SCOPE, text)
        reads: list[tuple[int, int | None]] = []
        original = data.read_events

        async def counted(scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None) -> object:
            reads.append((after_sequence, limit))
            return await original(scope, after_sequence=after_sequence, limit=limit)

        data.read_events = counted  # type: ignore[method-assign, assignment]
        assert await passes.read(SCOPE) == [PassOutcome.APPLIED, PassOutcome.APPLIED, PassOutcome.NOTHING]
        # The first pass read Events 1–4 and stopped: 3 did not fit. The second read 3–8.
        assert reads == [(0, 2), (2, 2), (2, 2), (4, 2), (6, 2)]
        assert [(p.from_sequence, p.through_sequence, p.snapshot_sequence) for p in await data.list_passes(SCOPE)] == [
            (0, 2, 8), (2, 8, 8)]

    asyncio.run(scenario())
