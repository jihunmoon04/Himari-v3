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
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from himari.common import ConversationId, EventId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    NO_PERSONA,
    AssistantFinal,
    ConversationEvent,
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


async def closed_the_day(data: InMemoryData, *, session: bool = True) -> None:
    """HIMARI fell asleep at 00:30 and the day closed at 03:30 in that sleep (the session CONV, when it had one)."""
    sleep = await data.open_sleep(ME, NO_PERSONA, at=seoul(26, 0, 30), how=FellBy.SCHEDULE)
    before = DayBefore("2026-11-25", fell_at=seoul(26, 0, 30), conversation_id=CONV if session else None)
    await data.note_closing(ME, NO_PERSONA, sleep.sleep_id, at=seoul(26, 3, 30), before=before if session else None)


def said_events(data: InMemoryData, *moments: datetime) -> None:
    """The closed session's events (their times as given)."""
    events = [ConversationEvent(EventId(f"s{i}"), CONV, i + 1, EventKind.USER_MESSAGE,
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
        data, provider, writer, _ = setup(day_answer(), day_answer(), clock=clock)
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
        data, provider, writer, _ = setup(day_answer(), amend_answer(), final('{"facts": ["면접을 응원했다."]}'),
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
        data, provider, writer, _ = setup(day_answer(), final('{"facts": []}'),
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
        data, provider, writer, _ = setup(day_answer(), final('{"facts": []}'),
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
        data, provider, writer, _ = setup(day_answer(), link=Link(None, closed=session()), clock=clock)
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
        data, provider, writer, _ = setup(day_answer(), link=Link(None, closed=session()), clock=clock)
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
        data, provider, writer, _ = setup(day_answer(), amend_answer(None), link=link, clock=clock)
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
        data, provider, writer, _ = setup(day_answer(), final("no json"), final("no json"), final("no json"),
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
        assert await writer.amend(ME) is WriteOutcome.GAVE_UP
        assert await writer.settle(ME) is WriteOutcome.WRITTEN

    asyncio.run(scenario())
