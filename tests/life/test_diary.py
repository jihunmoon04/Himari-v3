"""HIMARI's diary and its life calls in the day (character-day v0.1 §7, §10, §18.7): the diary written in the day
session's context — the session's own next request with the task one more user turn, the day's episodes left out,
the session recorded — or, with no session for that date, over the model's limit or not composable, the self run as
before; asleep from 21:00 before 22:00, today's diary as it falls asleep; a call after the same job's last try got no
answer at all goes to the backup model."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from himari.common import ConversationId, EventId, PrincipalId, TextBlock
from himari.data.api import (
    NO_PERSONA,
    AssistantFinal,
    ConversationEvent,
    EventKind,
    FellBy,
    LifeKind,
    LifeWriteStatus,
    PersistedStateBasis,
    RecentEpisode,
    UserMessage,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life.api import LifeSettings, MindModel, SessionInput, WriteOutcome
from himari.life.review import REVIEW_MAX_OUTPUT_TOKENS, REVIEW_TASK
from himari.life.worker import LifeWriter
from himari.prompt.api import PromptInputs, RequestOptions, SystemSpec, UserTurn
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE
from himari.provider.api import NativeNoResponse, NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
CONV = ConversationId("conv_day")
SETTINGS = LifeSettings("m", time_zone="Asia/Seoul")
BACKED = replace(SETTINGS, backup_model_id="spare", backup_effort="low")
LIVED = SystemSpec(companion=True, life=True)


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def day_answer(**fields: object) -> NormalizedFinal:
    body = json.dumps({"diary": "오늘은 사용자랑 영화를 봤다. 재밌었다. 일찍 잘 거다.", "plans": [], **fields},
                      ensure_ascii=False)
    return final(f"[[think]]좋은 하루였다.[[/think]]{body}")


def event(sequence: int, payload: object, kind: EventKind) -> ConversationEvent:
    return ConversationEvent(EventId(f"e{sequence}"), CONV, sequence, kind, payload, 1,  # type: ignore[arg-type]
                             datetime(2026, 11, 25, 11, sequence, tzinfo=UTC))


def session(day: str = "2026-11-25", limit: int | None = None) -> SessionInput:
    """The day session's next turn as the Driver would compose it: its history, its model's options."""
    inputs = PromptInputs(
        instructions=COMPANION_BASE, tool_catalog=(), tool_executions=(), snapshot=None,
        events=(event(1, UserMessage((TextBlock("오늘 같이 본 영화 재밌었어"),)), EventKind.USER_MESSAGE),
                event(2, AssistantFinal((TextBlock("응, 나도 좋았어!"),)), EventKind.ASSISTANT_FINAL)),
        state_basis=PersistedStateBasis(2, 0, ()), observations={}, trigger_refs=(), system=LIVED,
        request_options=RequestOptions("deep", "high", session_key="s1", day_session=True, stream=True),
        now=NOW, time_zone="Asia/Seoul")
    return SessionInput(CONV, day, inputs, limit)


class Link:
    """§18.7: a MindLink with the day session's model and, maybe, its next turn."""

    def __init__(self, found: SessionInput | None = None, *, broken: bool = False) -> None:
        self.found = found
        self.broken = broken

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        return MindModel("deep", "high", "the day session's model")

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        return ()

    async def session_input(self, principal: PrincipalId) -> SessionInput | None:
        if self.broken:
            raise RuntimeError("cannot read it now")
        return self.found


def setup(*script: object, settings: LifeSettings = SETTINGS, link: Link | None = None,
          clock: datetime = NOW) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter, list[object]]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    writer = LifeWriter(data, provider, settings, clock=lambda: clock, composer=Composer(),
                        instructions=COMPANION_BASE, system=LIVED, link=link)
    found: list[object] = []  # the episodes the review looked for (a self run's only)
    original = data.find_episodes

    async def find_episodes(principal: PrincipalId, words: Sequence[str], **more: Any) -> tuple[RecentEpisode, ...]:
        found.append(more)
        return await original(principal, words, **more)

    data.find_episodes = find_episodes  # type: ignore[method-assign]
    return data, provider, writer, found


def told(asked: object) -> dict[str, Any]:
    last = asked.conversation[-1]  # type: ignore[attr-defined]
    parsed: dict[str, Any] = json.loads(last.blocks[-1].text)
    return parsed


def test_the_diary_is_written_in_the_day_sessions_context_with_the_task_last_and_no_episodes() -> None:
    async def scenario() -> None:
        data, provider, writer, found = setup(day_answer(), link=Link(session()))
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        history = [p for p in asked.conversation if isinstance(p, UserTurn)]
        assert len(history) == 2 and TextBlock("오늘 같이 본 영화 재밌었어") in history[0].blocks
        assert asked.conversation[-1].blocks[0] == TextBlock(REVIEW_TASK)  # type: ignore[union-attr]
        assert asked.tool_catalog == ()
        options = asked.request_options
        assert (options.model_id, options.reasoning_effort, options.max_output_tokens, options.session_key,
                options.stream, options.day_session) == ("deep", "high", REVIEW_MAX_OUTPUT_TOKENS, "s1", False, True)
        assert told(asked)["episodes"] == [] and found == []  # the conversation is there: no episodes looked for
        (record,) = await data.list_life_writes(ME)
        assert (record.conversation_id, record.status, record.day) == (CONV, LifeWriteStatus.APPLIED, "2026-11-25")
        assert record.model_reason == "the day session's model, in the day session"
        assert await data.session_writes(ME, CONV) == (record,)
        assert [e.kind for e in await data.list_life(ME, NO_PERSONA)] == [LifeKind.DIARY]

    asyncio.run(scenario())


def test_without_a_session_for_that_date_over_its_limit_or_unreadable_the_diary_is_a_self_run() -> None:
    async def scenario() -> None:
        for link in (Link(None), Link(session(limit=1_000)), Link(session(day="2026-11-26")), Link(broken=True)):
            data, provider, writer, found = setup(day_answer(), link=link)
            assert await writer.write(ME) is WriteOutcome.WRITTEN
            asked = provider.prepared_inputs[-1]
            assert len(asked.conversation) == 1 and asked.conversation[0].blocks[0] == TextBlock(REVIEW_TASK)  # type: ignore[union-attr]
            assert len(found) == 1  # the day's episodes, as before
            (record,) = await data.list_life_writes(ME)
            assert record.conversation_id is None and record.model_reason == "the day session's model"
        data, provider, writer, _ = setup(day_answer(), link=Link(session(day="2026-11-24", limit=1_000_000)))
        assert await writer.write(ME) is WriteOutcome.WRITTEN  # a session opened before (kept awake): its context
        (record,) = await data.list_life_writes(ME)
        assert record.conversation_id == CONV

    asyncio.run(scenario())


def test_asleep_from_21_00_before_the_write_time_today_is_written() -> None:
    """§7: HIMARI fell asleep at 21:30 — the diary is today's at once (the keeper wakes the writer)."""
    async def scenario() -> None:
        evening = datetime(2026, 11, 25, 12, 40, tzinfo=UTC)  # 21:40 in Seoul
        data, _, writer, _ = setup(day_answer(), day_answer(), clock=evening)
        await data.open_sleep(ME, NO_PERSONA, at=datetime(2026, 11, 25, 12, 30, tzinfo=UTC), how=FellBy.TOOL)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        (record,) = await data.list_life_writes(ME)
        assert record.day == "2026-11-25"
        assert await writer.write(ME) is WriteOutcome.NOTHING  # once
        awake, _, writer, _ = setup(day_answer(), clock=evening)  # awake at 21:40: still yesterday's
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [w.day for w in await awake.list_life_writes(ME)] == ["2026-11-24"]

    asyncio.run(scenario())


def test_after_a_try_with_no_answer_at_all_the_next_goes_to_the_backup_model() -> None:
    """§10, §18.7: the same job and date's last try ended with no answer (retryable) — this one asks the backup;
    a malformed answer or an HTTP 400 is ours to fix, not the backup's."""
    async def scenario() -> None:
        data, provider, writer, _ = setup(NativeNoResponse("http_503"), day_answer(), settings=BACKED)
        assert await writer.write(ME) is WriteOutcome.FAILED
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        options = provider.prepared_inputs[-1].request_options
        assert (options.model_id, options.reasoning_effort) == ("spare", "low")
        latest, first = await data.list_life_writes(ME)
        assert (first.model_id, first.status, first.error) == ("m", LifeWriteStatus.FAILED, "http_503")
        assert latest.model_id == "spare" and latest.model_reason is not None and "backup" in latest.model_reason
        for failure in (final("no json here"), NativeNoResponse("http_400")):
            data, provider, writer, _ = setup(failure, day_answer(), settings=BACKED)
            assert await writer.write(ME) is WriteOutcome.FAILED
            assert await writer.write(ME) is WriteOutcome.WRITTEN
            assert provider.prepared_inputs[-1].request_options.model_id == "m"
        data, provider, writer, _ = setup(NativeNoResponse("http_503"), day_answer())  # no backup: the same model
        assert await writer.write(ME) is WriteOutcome.FAILED and await writer.write(ME) is WriteOutcome.WRITTEN
        assert provider.prepared_inputs[-1].request_options.model_id == "m"

    asyncio.run(scenario())


def test_the_world_and_settling_fall_back_to_the_backup_model_the_same_way() -> None:
    async def scenario() -> None:
        noon = datetime(2026, 11, 25, 3, 30, tzinfo=UTC)  # 12:30 in Seoul
        events = json.dumps({"events": [{"time": "15:00", "text": "택배가 왔다."}]}, ensure_ascii=False)
        data, provider, writer, _ = setup(NativeNoResponse("connect_failed:timeout"), final(events),
                                          settings=BACKED, clock=noon)
        assert await writer.world(ME) is WriteOutcome.FAILED
        assert await writer.world(ME) is WriteOutcome.WRITTEN
        assert provider.prepared_inputs[-1].request_options.model_id == "spare"
        data, provider, writer, _ = setup(day_answer(), NativeNoResponse("http_429"),
                                          final('{"facts": ["친구 유나가 있다."]}'), settings=BACKED)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert provider.prepared_inputs[-1].request_options.model_id == "m"  # its own first try
        assert await writer.settle(ME) is WriteOutcome.FAILED
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert provider.prepared_inputs[-1].request_options.model_id == "spare"

    asyncio.run(scenario())
