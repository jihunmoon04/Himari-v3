"""HIMARI's diary and its life calls in the day (character-day v0.1 §7, §10, §18.7): the diary written in the day
session's context — the session's own next request with the task one more user turn, the day's episodes left out,
the session recorded — or, with no session for that date, over the model's limit or not composable, the self run as
before; asleep from 21:00 before 22:00, today's diary as it falls asleep; a call after the same job's last try got no
answer at all goes to the backup model. character-diary v0.1 §4–§6: the diary at its slot with the events passed and
those of last night's sleep, the amend as the day session closes (only when something new came, never the user's
diary, never a diary written without a session), a diary missing at the closing written in the closed session, and
the facts settled once the day is closed and amended."""

from __future__ import annotations

import asyncio
import json

import pytest
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from himari.common import ConversationId, EventId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    NO_PERSONA,
    AssistantFinal,
    ConversationEvent,
    DataError,
    DataErrorCode,
    DayBefore,
    EventKind,
    FellBy,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifeWriteStart,
    LifeWriteStatus,
    PlanKind,
    PlanWrite,
    RawReceived,
    WokeBy,
    WorldEvent,
    PersistedStateBasis,
    RecentEpisode,
    UserMessage,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life.api import LifeSettings, MindModel, SessionInput, WriteOutcome
from himari.life import review
from himari.life.review import AMEND_TASK, REVIEW_MAX_OUTPUT_TOKENS, REVIEW_TASK
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
KEPT = replace(SETTINGS, keeper=True)  # character-diary §6: the day keeper runs (the facts wait for the closing)
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


def session(day: str = "2026-11-25", limit: int | None = None, conv: ConversationId = CONV) -> SessionInput:
    """The day session's next turn as the Driver would compose it: its history, its model's options."""
    inputs = PromptInputs(
        instructions=COMPANION_BASE, tool_catalog=(), tool_executions=(), snapshot=None,
        events=(event(1, UserMessage((TextBlock("오늘 같이 본 영화 재밌었어"),)), EventKind.USER_MESSAGE),
                event(2, AssistantFinal((TextBlock("응, 나도 좋았어!"),)), EventKind.ASSISTANT_FINAL)),
        state_basis=PersistedStateBasis(2, 0, ()), observations={}, trigger_refs=(), system=LIVED,
        request_options=RequestOptions("deep", "high", session_key="s1", day_session=True, stream=True),
        now=NOW, time_zone="Asia/Seoul")
    return SessionInput(conv, day, inputs, limit)


class Link:
    """§18.7: a MindLink with the day session's model and, maybe, its next turn."""

    def __init__(self, found: SessionInput | None = None, *, broken: bool = False,
                 closed: SessionInput | None = None) -> None:
        self.found = found
        self.broken = broken
        self.closed = closed  # character-diary §5: a closed day session, read by its id

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        return MindModel("deep", "high", "the day session's model")

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        return ()

    async def session_input(self, principal: PrincipalId,
                            conversation_id: ConversationId | None = None) -> SessionInput | None:
        if self.broken:
            raise RuntimeError("cannot read it now")
        if conversation_id is not None:
            return self.closed if self.closed is not None and self.closed.conversation_id == conversation_id else None
        return self.found


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def setup(*script: object, settings: LifeSettings = SETTINGS, link: Link | None = None,
          clock: datetime | Clock = NOW) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter, list[object]]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    ticking = clock if isinstance(clock, Clock) else Clock(clock)
    writer = LifeWriter(data, provider, settings, clock=ticking, composer=Composer(),
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


# --- character-diary v0.1 §4–§6 ----------------------------------------------------------------


def seoul(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)


def amend_answer(diary: str | None = "오늘은 사용자랑 영화를 봤다. 밤엔 내일 면접 이야기를 들었다.") -> NormalizedFinal:
    body = json.dumps({} if diary is None else {"diary": diary}, ensure_ascii=False)
    return final(f"[[think]]면접 얘기를 적어야지.[[/think]]{body}")


async def tonight(data: InMemoryData, diary_at: str = "22:30") -> None:
    """The night of the 25th as the keeper left it: its sleep slot (00:30–08:10) and its diary slot."""
    placed = seoul(25, 9)
    await data.write_plan(ME, NO_PERSONA, PlanWrite("2026-11-26", "00:30", 460, PlanKind.SLEEP, "잠"),
                          origin=LifeOrigin.WRITER, zone="Asia/Seoul", now=placed)
    await data.write_plan(ME, NO_PERSONA, PlanWrite("2026-11-25", diary_at, 30, PlanKind.DIARY, "일기 쓰기"),
                          origin=LifeOrigin.WRITER, zone="Asia/Seoul", now=placed)


async def slept_last_night(data: InMemoryData) -> None:
    sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(25, 0, 10), how=FellBy.SCHEDULE)
    await data.end_sleep(ME, sleep.sleep_id, at=seoul(25, 8, 0), how=WokeBy.SCHEDULE)


def session_closed(data: InMemoryData, at: datetime | None) -> None:
    """The day session CONV as Data reads it: closed at ``at`` (None: still open)."""
    original = getattr(data, "_unpatched_read", data.read_conversation)
    data._unpatched_read = original  # type: ignore[attr-defined]

    async def read_conversation(scope: TrustedScope) -> Any:
        if scope.conversation_id != CONV:
            return await original(scope)
        return SimpleNamespace(conversation_id=CONV, day_session="2026-11-25", closed_at=at)

    data.read_conversation = read_conversation  # type: ignore[method-assign]


async def closed_the_day(data: InMemoryData, *, session: bool = True) -> None:
    """HIMARI fell asleep at 00:30 and the day closed at 03:30 in that sleep (the session CONV, when it had one)."""
    sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(26, 0, 30), how=FellBy.SCHEDULE)
    before = DayBefore("2026-11-25", fell_at=seoul(26, 0, 30), conversation_id=CONV if session else None)
    await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(26, 3, 30), before=before if session else None)
    if session:
        session_closed(data, seoul(26, 3, 30))


def kept(*script: object, **more: Any) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter, list[object]]:
    """``setup`` with the day keeper on (character-diary §6), its day session CONV open."""
    found = setup(*script, settings=KEPT, **more)
    session_closed(found[0], None)
    return found


def said_events(data: InMemoryData, *moments: datetime, held_until: datetime | None = None) -> None:
    """The closed session's events (their times as given): those before ``held_until`` (the diary's request, at
    22:40 unless given) within the two events its session held, the rest after them."""
    cut = held_until or seoul(25, 22, 40)
    events = [ConversationEvent(EventId(f"s{i}"), CONV, 2 if at < cut else 10 + i, EventKind.USER_MESSAGE,
                                UserMessage((TextBlock("나 내일 면접이야"),)), 1, at)
              for i, at in enumerate(moments)]

    async def read_events(scope: TrustedScope, **more: Any) -> Sequence[ConversationEvent]:
        return events if scope.conversation_id == CONV else []

    data.read_events = read_events  # type: ignore[method-assign]


def test_the_diary_is_written_when_its_slot_starts_with_the_events_passed_and_last_nights() -> None:
    """§4, §7: before its slot the latest due date is yesterday's; at the slot, today's — told the events whose time
    has passed (not those still to come) and last night's sleep's, marked while_asleep."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 20))
        data, provider, writer, _ = kept(day_answer(), day_answer(), clock=clock)
        await tonight(data)
        await slept_last_night(data)
        write = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-25", "scripted", "m", b"{}",
                                                               "application/json", 10, job=LifeJob.WORLD))
        await data.record_life_write(write, RawReceived(b"{}", "application/json"))
        await data.add_events(write, [WorldEvent("03:00", "밤새 비가 왔다."), WorldEvent("23:30", "쪽지가 왔다.")],
                              now=seoul(25, 9))
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [w.day for w in await data.list_life_writes(ME) if w.job is LifeJob.REVIEW] == ["2026-11-24"]
        clock.now = seoul(25, 22, 30)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        latest = (await data.list_life_writes(ME))[0]
        assert latest.day == "2026-11-25"
        events = told(provider.prepared_inputs[-1])["events"]
        assert events == [{"time": "03:00", "text": "밤새 비가 왔다.", "while_asleep": True}]  # 23:30 is still to come
        assert any(slot["kind"] == "diary" for slot in told(provider.prepared_inputs[-1])["schedule"])

    asyncio.run(scenario())


def test_the_diary_is_amended_at_the_closing_when_something_new_came_and_the_facts_settle_after() -> None:
    """§5, §6: written at 22:40 in the day session; the user spoke at 23:10; the day closed at 03:30 — the amend
    runs once in the closed session's context with AMEND_TASK, rewrites the diary, and only then are the facts
    settled (not before the closing, not before the amend)."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), amend_answer(), final('{"facts": ["면접을 응원했다."]}'),
                                          link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        await slept_last_night(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.NOTHING  # the day is not closed yet
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # nor amended
        said_events(data, seoul(25, 22, 0), seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        sent = len(provider.prepared_inputs)
        assert await writer.settle(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent  # amend first
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        assert asked.conversation[-1].blocks[0] == TextBlock(AMEND_TASK)  # type: ignore[union-attr]
        assert len([p for p in asked.conversation if isinstance(p, UserTurn)]) == 2  # the session's history, then it
        data_told = told(asked)
        assert (data_told["day"], data_told["written_at"], data_told["fell_asleep"]) == ("2026-11-25", "22:40",
                                                                                         "00:30")
        amend = (await data.list_life_writes(ME))[0]
        assert (amend.job, amend.conversation_id, amend.status, amend.day) == (
            LifeJob.AMEND, CONV, LifeWriteStatus.APPLIED, "2026-11-25")
        assert amend.model_reason == "the day session's model, in the day session"
        diary = next(e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY)
        assert "면접" in diary.text and diary.source == amend.write_id and diary.revision == 2
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # once
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert any(e.text == "면접을 응원했다." for e in await data.list_life(ME, NO_PERSONA))

    asyncio.run(scenario())


def test_nothing_new_after_the_diary_no_amend_is_called_and_the_facts_settle() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final('{"facts": []}'),
                                          link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        await slept_last_night(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 22, 0), seoul(26, 1, 30))  # before the diary, and in the sleep
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert await writer.settle(ME) is WriteOutcome.WRITTEN

    asyncio.run(scenario())


def test_the_users_diary_and_a_diary_with_no_session_are_never_amended() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final('{"facts": []}'),
                                          link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        await slept_last_night(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        diary = next(e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY)
        await data.edit_life(ME, diary.entry_id, "내가 고친 일기.", expected_revision=diary.revision)
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert await writer.settle(ME) is WriteOutcome.WRITTEN  # the user's diary: skipped, the facts follow
        # a diary written as a self run (the user never came): no session, so no amend
        clock.now = seoul(25, 22, 40)
        data, provider, writer, _ = kept(day_answer(), link=Link(None, closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert (await data.list_life_writes(ME))[0].conversation_id is None
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data, session=False)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == 1

    asyncio.run(scenario())


def test_a_diary_missing_at_the_closing_is_written_in_the_closed_session() -> None:
    """§5: every try failed (or the server was off) — after the closing, the closed session's context; no amend."""
    async def scenario() -> None:
        clock = Clock(seoul(26, 3, 40))
        data, provider, writer, _ = kept(day_answer(), link=Link(None, closed=session()), clock=clock)
        await tonight(data)
        await closed_the_day(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        assert asked.conversation[-1].blocks[0] == TextBlock(REVIEW_TASK)  # type: ignore[union-attr]
        assert len([p for p in asked.conversation if isinstance(p, UserTurn)]) == 2  # in the closed session
        (record,) = await data.list_life_writes(ME)
        assert (record.day, record.conversation_id) == ("2026-11-25", CONV)
        said_events(data, seoul(25, 23, 10))
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # written after the fall: nothing came after it

    asyncio.run(scenario())


def test_with_no_session_context_left_the_amend_is_a_self_run_told_what_was_said() -> None:
    """A deviation kept small: the closed session cannot be composed now — the amend goes as a self run with what
    the two said after the diary (said_after), so it is still made once."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        link = Link(session(), closed=session())
        data, provider, writer, _ = kept(day_answer(), amend_answer(None), link=link, clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        link.closed = None
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        assert len(asked.conversation) == 1 and asked.conversation[0].blocks[0] == TextBlock(AMEND_TASK)  # type: ignore[union-attr]
        assert told(asked)["said_after"] == [{"at": "23:10", "who": "user", "text": "나 내일 면접이야"}]
        diary = next(e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY)
        assert diary.revision == 1  # nothing to add: left as it was
        assert (await data.list_life_writes(ME))[0].conversation_id == CONV

    asyncio.run(scenario())


def test_after_its_tries_the_amend_gives_up_and_the_facts_settle() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final("no json"), final("no json"), final("no json"),
                                          final('{"facts": []}'), link=Link(session(), closed=session()),
                                          clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        for _ in range(3):
            assert await writer.amend(ME) is WriteOutcome.FAILED
            clock.now += timedelta(minutes=10)
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # its tries spent: skipped, kept for good
        skipped = (await data.list_life_writes(ME))[0]
        assert (skipped.job, skipped.status, skipped.error) == (LifeJob.AMEND, LifeWriteStatus.SKIPPED,
                                                                "skipped: given up after its tries")
        assert await writer.settle(ME) is WriteOutcome.WRITTEN

    asyncio.run(scenario())



# --- the review of H-86 (Codex, 2026-11-27): one regression each ------------------------------------


def jobs(records: Sequence[Any]) -> list[str]:
    return [w.job.value for w in reversed(records) if w.status is LifeWriteStatus.APPLIED]


def test_review_p1_1_on_the_first_night_with_no_sleep_yet_the_facts_wait_for_the_closing_and_amend() -> None:
    """With the day keeper on, a first night has slots and a session but no sleep row yet: the facts never settle
    right after the diary — the order is review, amend, settle."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), amend_answer(), final('{"facts": []}'),
                                  link=Link(session(), closed=session()), clock=clock)
        await tonight(data)  # no sleep recorded ever
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.NOTHING
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        assert await writer.settle(ME) is WriteOutcome.NOTHING  # the amend first
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert jobs(await data.list_life_writes(ME)) == ["review", "amend", "settle"]

    asyncio.run(scenario())


def test_review_p1_2_a_session_over_two_nights_settles_both_diaries() -> None:
    """A short sleep on the 25th ends before any closing; the same session writes the 26th's diary; the closing on
    the 27th ends both dates — each is settled, the earliest first; only the last diary is the amend's."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), day_answer(), final('{"facts": ["하나"]}'),
                                         final('{"facts": ["둘"]}'), link=Link(session(), closed=session()),
                                         clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        short = await data.open_sleep(ME, NO_PERSONA, at=seoul(25, 23, 30), how=FellBy.SCHEDULE)
        await data.end_sleep(ME, short.sleep_id, at=seoul(26, 1, 0), how=WokeBy.USER)  # woken: no closing
        clock.now = seoul(26, 22, 40)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [w.day for w in await data.list_life_writes(ME)] == ["2026-11-26", "2026-11-25"]
        sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(27, 0, 30), how=FellBy.SCHEDULE)
        await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(27, 3, 30),
                                before=DayBefore("2026-11-25", fell_at=seoul(27, 0, 30), conversation_id=CONV))
        session_closed(data, seoul(27, 3, 30))
        said_events(data, seoul(25, 22, 0), held_until=seoul(26, 23, 0))
        clock.now = seoul(27, 3, 31)
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # nothing came after the 26th's
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        settled = [w.day for w in reversed(await data.list_life_writes(ME)) if w.job is LifeJob.SETTLE]
        assert settled == ["2026-11-25", "2026-11-26"]
        assert await writer.settle(ME) is WriteOutcome.NOTHING

    asyncio.run(scenario())


def closed_session_on(model: str) -> SessionInput:
    found = session()
    return replace(found, inputs=replace(found.inputs, request_options=replace(found.inputs.request_options,
                                                                                model_id=model)))


def test_review_p1_3_after_every_evening_try_failed_the_closing_still_writes_the_diary_with_tries_of_its_own() -> None:
    """Three evening tries failed (the date's tries spent); after the closing the diary is written in the closed
    session, on that session's own model — also when looked at only the next evening (a restart); the recovery has
    its own three tries and then stops."""
    async def scenario() -> None:
        for late in (False, True):
            clock = Clock(seoul(25, 22, 40))
            data, provider, writer, _ = kept(final("no"), final("no"), final("no"), day_answer(),
                                             link=Link(session(), closed=closed_session_on("closed-model")),
                                             clock=clock)
            await tonight(data)
            for _ in range(3):
                assert await writer.write(ME) is WriteOutcome.FAILED
                clock.now += timedelta(minutes=10)
            assert await writer.write(ME) is WriteOutcome.GAVE_UP
            await closed_the_day(data)
            clock.now = seoul(26, 22, 30) if late else seoul(26, 3, 31)
            assert await writer.write(ME) is WriteOutcome.WRITTEN
            asked = provider.prepared_inputs[-1]
            assert asked.request_options.model_id == "closed-model"
            assert asked.conversation[-1].blocks[0] == TextBlock(REVIEW_TASK)  # type: ignore[union-attr]
            latest = (await data.list_life_writes(ME))[0]
            assert (latest.day, latest.conversation_id, latest.status) == ("2026-11-25", CONV,
                                                                            LifeWriteStatus.APPLIED)
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(*[final("no")] * 6, link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        for _ in range(3):
            assert await writer.write(ME) is WriteOutcome.FAILED
        await closed_the_day(data)
        clock.now = seoul(26, 3, 31)
        for _ in range(3):
            assert await writer.write(ME) is WriteOutcome.FAILED
            clock.now += timedelta(minutes=10)
        assert await writer.write(ME) is WriteOutcome.GAVE_UP and len(await data.list_life_writes(ME)) == 6

    asyncio.run(scenario())


def test_review_p1_4_two_events_in_one_minute_are_amended_and_a_request_never_composed_ends_its_tries(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), amend_answer(), link=Link(session(), closed=session()),
                                         clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        world = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-25", "p", "m", b"{}", "j", 1,
                                                               job=LifeJob.WORLD))
        await data.record_life_write(world, RawReceived(b"{}", "j"))
        await data.add_events(world, [WorldEvent("23:10", "택배가 왔다."), WorldEvent("23:10", "비가 왔다.")],
                              now=seoul(25, 9))
        said_events(data)
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        assert [e["text"] for e in told(provider.prepared_inputs[-1])["events"]] == ["택배가 왔다.", "비가 왔다."]

    asyncio.run(scenario())

    async def broken() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final('{"facts": []}'),
                                         link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)

        def cannot(*args: object, **more: object) -> dict[str, object]:
            raise ValueError("cannot compose")

        monkeypatch.setattr(review, "amend_data", cannot)
        for _ in range(3):
            assert await writer.amend(ME) is WriteOutcome.FAILED
        assert await writer.amend(ME) is WriteOutcome.NOTHING  # its tries spent: skipped for good
        failed = [w for w in await data.list_life_writes(ME) if w.job is LifeJob.AMEND
                  and w.status is LifeWriteStatus.FAILED]
        assert len(failed) == 3 and all(w.error is not None and "not composed" in w.error for w in failed)
        assert all(w.charged_tokens == 0 for w in failed)
        assert await writer.settle(ME) is WriteOutcome.WRITTEN  # never waiting for ever

    asyncio.run(broken())


def test_review_p1_6_a_message_that_came_while_the_diary_was_written_brings_the_amend() -> None:
    """The diary's request held the session's events up to sequence 2; a message recorded at its very moment (not
    after its end) came after what it held — the amend is called for it."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), amend_answer(), link=Link(session(), closed=session()),
                                  clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        (draft,) = await data.list_life_writes(ME)
        assert (draft.basis_sequence, draft.basis_at) == (2, seoul(25, 22, 40))
        said_events(data, seoul(25, 22, 40), held_until=seoul(25, 22, 40))  # sequence 10, at the request's moment
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        assert await writer.amend(ME) is WriteOutcome.WRITTEN

    asyncio.run(scenario())


def test_review_p1_7_a_closed_session_in_the_trash_skips_the_amend_and_the_facts_settle() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final('{"facts": []}'),
                                         link=Link(session(), closed=None), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)

        async def trashed(scope: TrustedScope, **more: Any) -> Sequence[ConversationEvent]:
            raise DataError(DataErrorCode.NOT_FOUND, scope.conversation_id)

        data.read_events = trashed  # type: ignore[method-assign]
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert await writer.settle(ME) is WriteOutcome.WRITTEN

    asyncio.run(scenario())


def test_review_p2_8_events_after_an_early_wake_go_to_the_next_diary_and_only_sleep_is_marked_asleep() -> None:
    """Asleep at 21:15 (the diary written then), woken at 22:00, awake till 01:00, asleep again till 08:00: the
    23:00 event comes to the 26th's diary, not marked; the 03:00 one marked while_asleep."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 21, 15))
        data, provider, writer, _ = kept(day_answer(), day_answer(), clock=clock)
        world = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-25", "p", "m", b"{}", "j", 1,
                                                               job=LifeJob.WORLD))
        await data.record_life_write(world, RawReceived(b"{}", "j"))
        await data.add_events(world, [WorldEvent("23:00", "친구가 쪽지를 두고 갔다.")], now=seoul(25, 9))
        world = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-26", "p", "m", b"{}", "j", 1,
                                                               job=LifeJob.WORLD))
        await data.record_life_write(world, RawReceived(b"{}", "j"))
        await data.add_events(world, [WorldEvent("03:00", "밤새 비가 왔다.")], now=seoul(25, 9))
        first = await data.open_sleep(ME, NO_PERSONA, at=seoul(25, 21, 15), how=FellBy.TOOL)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert "events" not in told(provider.prepared_inputs[-1])  # 23:00 is still to come
        await data.end_sleep(ME, first.sleep_id, at=seoul(25, 22, 0), how=WokeBy.USER)
        again = await data.open_sleep(ME, NO_PERSONA, at=seoul(26, 1, 0), how=FellBy.SCHEDULE)
        await data.end_sleep(ME, again.sleep_id, at=seoul(26, 8, 0), how=WokeBy.SCHEDULE)
        clock.now = seoul(26, 22, 30)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert told(provider.prepared_inputs[-1])["events"] == [
            {"date": "2026-11-25", "time": "23:00", "text": "친구가 쪽지를 두고 갔다."},
            {"time": "03:00", "text": "밤새 비가 왔다.", "while_asleep": True}]

    asyncio.run(scenario())


def test_review_p2_11_a_diary_and_an_amend_applied_in_a_session_are_told_for_the_open_timeline() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), amend_answer(), link=Link(session(), closed=session()),
                                  clock=clock)
        told_of: list[tuple[PrincipalId, ConversationId]] = []
        writer.on_diary = lambda principal, conversation: told_of.append((principal, conversation))
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        assert told_of == [(ME, CONV), (ME, CONV)]

    asyncio.run(scenario())



# --- the second review of H-86 (Codex, 2026-11-27): one regression each -----------------------------


def trashed(data: InMemoryData) -> None:
    """The day session CONV in the Trash: Data never reads it."""
    async def read_conversation(scope: TrustedScope) -> Any:
        raise DataError(DataErrorCode.NOT_FOUND, scope.conversation_id)

    data.read_conversation = read_conversation  # type: ignore[method-assign]


def test_review2_1_a_later_closing_never_forgets_a_delayed_amend() -> None:
    """The 25th's amend waits (its round did not come); the next night's closing has no session and replaces the
    handoff — the 25th's diary still gets its amend, and only then its facts."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), amend_answer(), final('{"facts": []}'),
                                  link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        await closed_the_day(data)  # the 25th's session closed at 26 03:30 — no amend round yet
        night = await data.current_sleep(ME, NO_PERSONA)
        assert night is not None
        await data.end_sleep(ME, night.sleep_id, at=seoul(26, 8, 10), how=WokeBy.SCHEDULE)
        sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(27, 0, 30), how=FellBy.SCHEDULE)
        await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(27, 3, 30), before=None)
        assert (await data.read_day_state(ME, NO_PERSONA)).before is None
        clock.now = seoul(27, 3, 31)
        assert await writer.settle(ME) is WriteOutcome.NOTHING  # its amend is not decided
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert jobs(await data.list_life_writes(ME)) == ["review", "amend", "settle"]

    asyncio.run(scenario())


def test_review2_2_a_session_restored_from_the_trash_never_reopens_its_amend() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), final('{"facts": []}'), amend_answer(),
                                         link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        trashed(data)
        assert await writer.amend(ME) is WriteOutcome.NOTHING
        skipped = (await data.list_life_writes(ME))[0]
        assert (skipped.job, skipped.status, skipped.error) == (
            LifeJob.AMEND, LifeWriteStatus.SKIPPED, "skipped: its day session is not readable")
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        session_closed(data, seoul(26, 3, 30))  # restored, closed as it was
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert jobs(await data.list_life_writes(ME)) == ["review", "settle"]

    asyncio.run(scenario())


def test_review2_3_a_diary_the_user_deleted_is_never_written_again() -> None:
    """The 25th's diary was written (in the session) and the user deleted it: neither the same evening's review nor
    the closing's recovery the next evening writes it again."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), day_answer(), link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        diary = next(e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY)
        await data.retire_life(ME, diary.entry_id, expected_revision=diary.revision)
        clock.now = seoul(25, 23, 0)
        assert await writer.write(ME) is WriteOutcome.NOTHING
        await closed_the_day(data)
        clock.now = seoul(26, 22, 30)
        assert await writer.write(ME) is WriteOutcome.WRITTEN  # the 26th's, never the 25th's again
        reviews = [w.day for w in reversed(await data.list_life_writes(ME)) if w.job is LifeJob.REVIEW]
        assert reviews == ["2026-11-25", "2026-11-26"]

    asyncio.run(scenario())


def test_review2_4_the_recovery_window_is_measured_from_the_closing() -> None:
    """Closed at 26 03:30: at 28 02:30 (47 hours on) the 25th's missing diary is still written; past 48 hours it is
    not."""
    async def scenario() -> None:
        for at, recovered in ((seoul(28, 2, 30), True), (seoul(28, 3, 31), False)):
            clock = Clock(seoul(25, 22, 40))
            data, _, writer, _ = kept(final("no"), final("no"), final("no"), day_answer(),
                                      link=Link(session(), closed=session()), clock=clock)
            await tonight(data)
            for _ in range(3):
                assert await writer.write(ME) is WriteOutcome.FAILED
            await closed_the_day(data)
            clock.now = at
            assert await writer.write(ME) is WriteOutcome.WRITTEN
            assert (await data.list_life_writes(ME))[0].day == ("2026-11-25" if recovered else "2026-11-27")

    asyncio.run(scenario())


def test_review2_5_the_recovery_tries_are_counted_by_the_store_whatever_else_was_written() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(*[final("no")] * 6, day_answer(), link=Link(session(), closed=session()),
                                  clock=clock)
        await tonight(data)
        for _ in range(3):
            assert await writer.write(ME) is WriteOutcome.FAILED
        await closed_the_day(data)
        clock.now = seoul(26, 3, 31)
        for _ in range(3):
            assert await writer.write(ME) is WriteOutcome.FAILED
        for i in range(61):  # other jobs' writes, newer than the tries
            await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-20", "p", "m", b"{}", "j", 0,
                                                           job=LifeJob.WORLD, basis_at=seoul(26, 4, 0)))
        assert await writer.write(ME) is WriteOutcome.GAVE_UP
        assert len([w for w in await data.list_life_writes(ME, limit=200) if w.job is LifeJob.REVIEW]) == 6

    asyncio.run(scenario())



# --- the third review of H-86 (Codex, 2026-11-27): one regression each ------------------------------


def told_by(data: InMemoryData, said: dict[ConversationId, Sequence[datetime]]) -> None:
    """Each session's user messages after what the diary's request held (sequences from 10), at the times given."""
    found = {conv: [ConversationEvent(EventId(f"{conv}-{i}"), conv, 10 + i, EventKind.USER_MESSAGE,
                                      UserMessage((TextBlock("늦게 한 말"),)), 1, at) for i, at in enumerate(moments)]
             for conv, moments in said.items()}

    async def read_events(scope: TrustedScope, **more: Any) -> Sequence[ConversationEvent]:
        after = int(more.get("after_sequence", 0))
        return [e for e in found.get(scope.conversation_id, []) if e.sequence > after]

    data.read_events = read_events  # type: ignore[method-assign]


def test_review3_1_a_late_closing_cuts_the_amend_at_the_fall_of_the_sleep_it_named() -> None:
    """Asleep from 00:30; the keeper, restarted, closes the day at 10:00 and records the wake at 08:10: the only
    message after the diary came at 01:10, in the sleep — nothing new, no call (the closing named its sleep)."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), clock=clock)
        real = await data.open_day_session(ME, "2026-11-25", "11월 25일 (수)")
        writer._link = Link(session(conv=real), closed=session(conv=real))  # noqa: SLF001
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert (await data.list_life_writes(ME))[0].conversation_id == real
        told_by(data, {real: [seoul(26, 1, 10)]})
        sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(26, 0, 30), how=FellBy.SCHEDULE)
        assert await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(26, 10, 0), conversation_id=real,
                                       before=DayBefore("2026-11-25", fell_at=seoul(26, 0, 30), conversation_id=real))
        await data.end_sleep(ME, sleep.sleep_id, at=seoul(26, 8, 10), how=WokeBy.SCHEDULE)
        closed = await data.current_sleep(ME, NO_PERSONA)
        assert closed is not None and closed.closed_session == real
        clock.now = seoul(26, 10, 1)
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert (await data.list_life_writes(ME))[0].error == "skipped: nothing new after it"

    asyncio.run(scenario())


def test_review3_2_a_diary_settled_before_v30_is_never_amended() -> None:
    """The v29 server settled the diary at once; after the upgrade its closed session with a late message gets a
    terminal skip before the session is read."""
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        settled = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-25", "p", "m", b"{}", "j", 1,
                                                                 job=LifeJob.SETTLE))
        await data.record_life_write(settled, RawReceived(b"{}", "j"))
        await data.apply_settle_write(settled, (), now=seoul(25, 22, 50))
        said_events(data, seoul(25, 23, 10))
        clock.now = seoul(26, 3, 31)
        await closed_the_day(data)
        read: list[object] = []
        original = data.read_conversation

        async def watched(scope: TrustedScope) -> Any:
            read.append(scope)
            return await original(scope)

        data.read_conversation = watched  # type: ignore[method-assign]
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.NOTHING and len(provider.prepared_inputs) == sent
        assert read == []  # decided before its conversation is read
        assert (await data.list_life_writes(ME))[0].error == "skipped: settled before the amend"

    asyncio.run(scenario())


def test_review3_3_a_session_closed_days_after_its_diary_is_still_amended_and_one_never_closed_is_skipped() -> None:
    async def scenario() -> None:
        clock = Clock(seoul(25, 22, 40))
        data, _, writer, _ = kept(day_answer(), amend_answer(), final('{"facts": []}'), clock=clock)
        real = await data.open_day_session(ME, "2026-11-25", "11월 25일 (수)")
        writer._link = Link(session(conv=real), closed=session(conv=real))  # noqa: SLF001
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        told_by(data, {real: [seoul(27, 23, 0)]})  # awake through the disrupted nights
        sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(29, 0, 30), how=FellBy.SCHEDULE)
        await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(29, 3, 30), conversation_id=real,
                                before=DayBefore("2026-11-25", fell_at=seoul(29, 0, 30), conversation_id=real))
        clock.now = seoul(29, 3, 31)  # the 25th is four days back: found by its session's closing
        assert await writer.amend(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        assert jobs(await data.list_life_writes(ME)) == ["review", "amend", "settle"]
        # a session that never closes: SETTLE_DAYS on, its diary's amend is skipped for good
        clock.now = seoul(25, 22, 40)
        data, provider, writer, _ = kept(day_answer(), link=Link(session(), closed=session()), clock=clock)
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        clock.now = seoul(27, 12, 0)
        assert await writer.amend(ME) is WriteOutcome.NOTHING
        assert not await data.life_done(ME, NO_PERSONA, "2026-11-25", LifeJob.AMEND)  # still waiting
        clock.now = seoul(28, 0, 10)
        assert await writer.amend(ME) is WriteOutcome.NOTHING
        assert (await data.list_life_writes(ME))[0].error == "skipped: its day session never closed in time"

    asyncio.run(scenario())


def test_review3_4_an_amend_waiting_for_the_budget_never_holds_back_the_others_and_one_too_large_ends() -> None:
    """The 25th's amend must wait for the daily budget: the 26th's (nothing new) is still skipped, so its facts
    settle; a request that could never fit the daily budget is skipped for good."""
    async def scenario() -> None:
        older, newer = ConversationId("conv_a"), ConversationId("conv_b")
        clock = Clock(seoul(25, 22, 40))
        data, provider, writer, _ = kept(day_answer(), day_answer(), final('{"facts": []}'), clock=clock)
        writer._link = Link(session(conv=older))  # noqa: SLF001
        await tonight(data)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        first = await data.open_sleep(ME, NO_PERSONA, at=seoul(26, 0, 30), how=FellBy.SCHEDULE)
        await data.note_closing(ME, NO_PERSONA, first.sleep_id, at=seoul(26, 3, 30), before=None)
        await data.end_sleep(ME, first.sleep_id, at=seoul(26, 8, 10), how=WokeBy.SCHEDULE)
        writer._link = Link(session(day="2026-11-26", conv=newer))  # noqa: SLF001
        clock.now = seoul(26, 22, 40)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        second = await data.open_sleep(ME, NO_PERSONA, at=seoul(27, 0, 30), how=FellBy.SCHEDULE)
        await data.note_closing(ME, NO_PERSONA, second.sleep_id, at=seoul(27, 3, 30), before=None)
        closed_at = {older: seoul(26, 3, 30), newer: seoul(27, 3, 30)}

        async def read_conversation(scope: TrustedScope) -> Any:
            return SimpleNamespace(conversation_id=scope.conversation_id, closed_at=closed_at[scope.conversation_id])

        data.read_conversation = read_conversation  # type: ignore[method-assign]
        told_by(data, {older: [seoul(25, 23, 10)], newer: []})

        async def spent(principal: PrincipalId, since: datetime) -> int:
            return writer.settings.daily_tokens  # the day's budget is used up

        data.life_usage = spent  # type: ignore[method-assign]
        clock.now = seoul(27, 3, 31)
        sent = len(provider.prepared_inputs)
        assert await writer.amend(ME) is WriteOutcome.DEFERRED
        assert await data.life_done(ME, NO_PERSONA, "2026-11-26", LifeJob.AMEND)  # skipped all the same
        assert not await data.life_done(ME, NO_PERSONA, "2026-11-25", LifeJob.AMEND)
        assert await writer.settle(ME) is WriteOutcome.DEFERRED  # the 26th's facts are next, budget allowing
        assert all(w.job is not LifeJob.AMEND or w.status is LifeWriteStatus.SKIPPED
                   for w in await data.list_life_writes(ME))
        writer.settings = replace(writer.settings, daily_tokens=100)  # never enough for the 25th's amend
        assert await writer.amend(ME) is WriteOutcome.NOTHING
        assert (await data.list_life_writes(ME))[0].error == "skipped: too large for the daily budget"
        assert len(provider.prepared_inputs) >= sent

    asyncio.run(scenario())
