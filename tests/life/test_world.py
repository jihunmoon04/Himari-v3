"""The world (character-mind v0.1 §7): once a day, after the day was closed (character-day §6: the sleep's closing
that date) or from noon, the memory model decides 0 to 2 events of HIMARI's day — never the user, never feelings,
never while it sleeps — stored as events of the date; the review is told them all, the work those whose time has
come."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from himari.common import PrincipalId, TextBlock
from himari.data.api import (
    MAX_EVENT_CHARS,
    NO_PERSONA,
    DayState,
    LifeEntry,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifeWriteStatus,
    PlanKind,
    PlanState,
    PlanWrite,
    WorldEvent,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life import world
from himari.life.api import ATTEMPTS_PER_DAY, LifeSettings, WriteOutcome
from himari.life.review import AnswerError
from himari.life.worker import LifeWriter
from himari.prompt.composer import Composer
from himari.provider.api import NativeNoResponse, NormalizedFinal
from himari.provider.scripted import Measured, ScriptedAdapter

ME = PrincipalId("alice")
SEOUL = ZoneInfo("Asia/Seoul")
NOON = datetime(2026, 11, 25, 3, 30, tzinfo=UTC)  # 12:30 in Seoul, a Wednesday
MORNING = datetime(2026, 11, 24, 22, 0, tzinfo=UTC)  # 07:00 the 25th in Seoul
CLOSED = datetime(2026, 11, 24, 18, 30, tzinfo=UTC)  # 03:30 the 25th in Seoul: the sleep's closing
DAY = date(2026, 11, 25)
AT = time(22, 0)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None, at: str | None = None) -> LifeEntry:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    return LifeEntry(entry_id, 1, kind, day, text, LifeOrigin.WRITER, 1, now, now, time_of_day=at)


def events(*raw: object) -> str:
    return json.dumps({"events": list(raw)}, ensure_ascii=False)


def test_the_world_is_decided_after_the_days_closing_or_from_noon_and_before_the_review() -> None:
    assert world.due_day(NOON, SEOUL, AT) == DAY
    assert world.due_day(datetime(2026, 11, 25, 2, 59, tzinfo=UTC), SEOUL, AT) is None  # 11:59, no closing
    assert world.due_day(MORNING, SEOUL, AT, CLOSED) == DAY  # 07:00, closed at 03:30
    assert world.due_day(datetime(2026, 11, 24, 18, 0, tzinfo=UTC), SEOUL, AT, CLOSED) is None  # 03:00: not yet
    the_24th = datetime(2026, 11, 23, 18, 30, tzinfo=UTC)  # another date's closing: noon then
    assert world.due_day(MORNING, SEOUL, AT, the_24th) is None
    assert world.due_day(datetime(2026, 11, 25, 13, 0, tzinfo=UTC), SEOUL, AT, CLOSED) is None  # 22:00: review time
    assert world.world_start(DAY, SEOUL) == datetime(2026, 11, 25, 12, 0, tzinfo=SEOUL)
    assert world.world_start(DAY, SEOUL, CLOSED) == CLOSED


def test_the_input_holds_who_himari_is_its_life_its_schedule_and_sleep_and_the_last_weeks_events() -> None:
    sleep = replace(entry(7, LifeKind.PLAN, "잠", "2026-11-25", "00:30"), minutes=460, plan_kind=PlanKind.SLEEP)
    tonight = replace(sleep, entry_id=8, day="2026-11-26")
    walk = replace(entry(9, LifeKind.PLAN, "산책", "2026-11-25", "16:00"), plan_kind=PlanKind.OWN)
    gone = replace(walk, entry_id=10, text="장보기", time_of_day="18:00", plan_state=PlanState.CANCELLED, reason="귀찮아")
    life = (entry(1, LifeKind.FACT, "고양이를 무서워한다."),
            replace(entry(2, LifeKind.ARC, "풍경화 한 장", "2026-11-20"), title="그림 연습"),
            entry(3, LifeKind.PLAN, "전시를 보러 간다.", "2026-11-26", "14:00"),
            entry(4, LifeKind.EVENT, "유나가 찾아왔다.", "2026-11-23", "18:00"),
            entry(5, LifeKind.EVENT, "너무 옛날 일.", "2026-11-10", "18:00"),
            entry(6, LifeKind.DIARY, "어제 일기.", "2026-11-24"), sleep, tonight, walk, gone)
    data = world.request_data("이름: 루시", life, day=DAY, makes="그림", weather="맑음", topics=("첫눈",), zone=SEOUL)
    assert data == {
        "persona": "이름: 루시", "persona_makes": "그림", "facts": ["고양이를 무서워한다."],
        "arcs": [{"id": 2, "title": "그림 연습", "goal": "풍경화 한 장", "started": "2026-11-20", "moved_on": []}],
        "plans": [{"date": "2026-11-26", "time": "14:00", "text": "전시를 보러 간다."}],  # never a sleep slot
        "recent_events": [{"date": "2026-11-23", "time": "18:00", "text": "유나가 찾아왔다."}],
        "day": "2026-11-25", "weekday": "Wednesday", "season": "autumn",
        "schedule": [{"time": "00:30-08:10", "kind": "sleep", "text": "잠"},
                     {"time": "16:00-17:00", "kind": "own", "text": "산책"}],  # what still stands
        "sleep": ["00:30-08:10"], "weather": "맑음", "topics": ["첫눈"]}
    assert "quiet_hours" not in data and "schedule" not in world.request_data("", (), day=DAY)
    for words in ("never how HIMARI felt", "The user never appears", "Never repeat recent_events",
                  "never undo one", "Most days have none or one", "outside its sleep"):
        assert words in world.WORLD_INSTRUCTIONS, words


def test_an_answer_is_zero_to_two_events_with_a_time_outside_the_sleep() -> None:
    sleeps = [(datetime(2026, 11, 25, 0, 30, tzinfo=SEOUL), datetime(2026, 11, 25, 8, 10, tzinfo=SEOUL)),
              (datetime(2026, 11, 25, 23, 30, tzinfo=SEOUL), datetime(2026, 11, 26, 7, 30, tzinfo=SEOUL))]
    assert world.parse_events(events({"time": "9:30", "text": " 창문으로 비가\n샜다. "}), DAY, sleeps, SEOUL) == (
        WorldEvent("09:30", "창문으로 비가 샜다."),)
    assert world.parse_events(events(), DAY, sleeps, SEOUL) == () and world.parse_events("{}") == ()
    assert world.parse_events(events({"time": "02:00", "text": "새벽"})) == (WorldEvent("02:00", "새벽"),)  # no sleep
    for bad in (events(*[{"time": "10:00", "text": "x"}] * 3), events({"time": "02:00", "text": "자는 중"}),
                events({"time": "23:45", "text": "다시 자는 중"}), events({"time": "08:09", "text": "아직 잠"}),
                events({"text": "시각 없음"}), events({"time": "10:00", "text": "x" * (MAX_EVENT_CHARS + 1)}),
                events("odd"), "not json"):
        with pytest.raises(AnswerError):
            world.parse_events(bad, DAY, sleeps, SEOUL)
    assert world.parse_events(events({"time": "08:10", "text": "깼다"}), DAY, sleeps, SEOUL)  # its end is awake
    assert world.day_sleeps((), DAY, SEOUL) == []


def setup(*script: object, clock: datetime = NOON) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    writer = LifeWriter(data, provider, LifeSettings("m", time_zone="Asia/Seoul"), clock=lambda: clock,
                        composer=Composer())
    return data, provider, writer


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def test_the_world_writes_the_days_events_once_a_date_on_the_memory_model() -> None:
    async def scenario() -> None:
        data, provider, writer = setup(Measured(final(events({"time": "15:00", "text": "택배가 늦게 왔다."},
                                                             {"time": "19:00", "text": "유나가 앨범을 들고 왔다."})),
                                                700, 60))
        assert await writer.world(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        assert asked.stable_prefix == (world.WORLD_INSTRUCTIONS,) and asked.request_options.model_id == "m"
        made = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.EVENT]
        assert [(e.day, e.time_of_day, e.text, e.origin) for e in made] == [
            ("2026-11-25", "19:00", "유나가 앨범을 들고 왔다.", LifeOrigin.WRITER),
            ("2026-11-25", "15:00", "택배가 늦게 왔다.", LifeOrigin.WRITER)]
        (record,) = await data.list_life_writes(ME)
        assert (record.job, record.status, made[0].source) == (LifeJob.WORLD, LifeWriteStatus.APPLIED, record.write_id)
        assert await writer.world(ME) is WriteOutcome.NOTHING  # once a date
        assert writer.next_world(NOON) == datetime(2026, 11, 26, 3, 0, tzinfo=UTC)  # tomorrow's noon in Seoul
        assert writer.next_world(MORNING) == datetime(2026, 11, 25, 3, 0, tzinfo=UTC)  # today's noon
        early = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)  # 03:00: a closing at 03:30 brings it on
        assert writer.next_world(early, CLOSED) == CLOSED

    asyncio.run(scenario())


def test_a_closing_brings_the_days_world_on_and_its_events_keep_out_of_the_sleep() -> None:
    """character-day §6 step 4, §18.7: after the sleep's closing (the keeper's last closing that date) the world
    runs before noon; an event in the sleep throws the answer away, as any out of shape."""
    async def scenario() -> None:
        data, provider, writer = setup(final(events({"time": "03:00", "text": "자는 중"})),
                                       final(events({"time": "10:00", "text": "택배가 왔다."})), clock=MORNING)
        await data.write_plan(ME, NO_PERSONA, PlanWrite("2026-11-25", "00:30", 460, PlanKind.SLEEP, "잠"),
                              origin=LifeOrigin.WRITER, zone="Asia/Seoul",
                              now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
        assert await writer.world(ME) is WriteOutcome.NOTHING and provider.sent == []  # 07:00, no closing
        await data.write_day_state(ME, DayState(NO_PERSONA, last_closing_at=CLOSED))
        assert await writer.world(ME) is WriteOutcome.FAILED  # 03:00 is in the sleep
        told = json.loads(provider.prepared_inputs[-1].conversation[0].blocks[-1].text)  # type: ignore[union-attr]
        assert told["sleep"] == ["00:30-08:10"] and told["schedule"] == [
            {"time": "00:30-08:10", "kind": "sleep", "text": "잠"}]
        (failed,) = await data.list_life_writes(ME)
        assert failed.error is not None and "while HIMARI sleeps" in failed.error
        assert await writer.world(ME) is WriteOutcome.WRITTEN
        assert [e.text for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.EVENT] == ["택배가 왔다."]

    asyncio.run(scenario())


def test_the_world_is_tried_three_times_a_date_and_not_before_noon() -> None:
    async def scenario() -> None:
        _, provider, writer = setup(clock=MORNING)
        assert await writer.world(ME) is WriteOutcome.NOTHING and provider.sent == []
        _, provider, writer = setup(final("no"), NativeNoResponse("http_500"),
                                    final(events("odd")))
        assert [await writer.world(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * 3
        assert await writer.world(ME) is WriteOutcome.GAVE_UP

    asyncio.run(scenario())


def test_the_review_is_told_all_of_the_days_events_and_the_work_those_that_have_come() -> None:
    async def scenario() -> None:
        evening = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
        review = '[[think]]비 온 하루.[[/think]]{"diary": "비가 새서 도면이 젖었다.", "plans": []}'
        data, provider, writer = setup(final(events({"time": "15:00", "text": "비가 새서 도면이 젖었다."},
                                                    {"time": "21:00", "text": "밤에 유나가 왔다."})))
        assert await writer.world(ME) is WriteOutcome.WRITTEN
        provider.extend([final(review)])
        writer2 = LifeWriter(data, provider, LifeSettings("m", time_zone="Asia/Seoul"), clock=lambda: evening,
                             composer=Composer())
        assert await writer2.write(ME) is WriteOutcome.WRITTEN
        told = json.loads(provider.prepared_inputs[-1].conversation[0].blocks[-1].text)  # type: ignore[union-attr]
        assert told["events"] == [{"time": "15:00", "text": "비가 새서 도면이 젖었다."},
                                  {"time": "21:00", "text": "밤에 유나가 왔다."}]
        entries = await data.list_life(ME, NO_PERSONA)
        afternoon = datetime(2026, 11, 25, 16, 0, tzinfo=SEOUL)
        assert [e.text for e in world.passed_events(entries, afternoon)] == ["비가 새서 도면이 젖었다."]

    asyncio.run(scenario())
