"""The world (character-mind v0.1 §7): once a day after the quiet hours, the memory model decides 0 to 2 events of
HIMARI's day — never the user, never feelings — stored as events of the date; the review is told them all, the work
those whose time has come."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from himari.common import PrincipalId, TextBlock
from himari.data.api import (
    MAX_EVENT_CHARS,
    NO_PERSONA,
    LifeEntry,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifeWriteStatus,
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
QUIET = (time(23, 0), time(8, 0))
MORNING = datetime(2026, 11, 25, 0, 30, tzinfo=UTC)  # 09:30 in Seoul, a Wednesday
DAY = date(2026, 11, 25)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None, at: str | None = None) -> LifeEntry:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    return LifeEntry(entry_id, 1, kind, day, text, LifeOrigin.WRITER, 1, now, now, time_of_day=at)


def events(*raw: object) -> str:
    return json.dumps({"events": list(raw)}, ensure_ascii=False)


def test_the_world_is_decided_after_the_quiet_hours_and_before_the_review() -> None:
    at = time(22, 0)
    assert world.due_day(MORNING, SEOUL, QUIET, at) == DAY
    assert world.due_day(datetime(2026, 11, 24, 22, 30, tzinfo=UTC), SEOUL, QUIET, at) is None  # 07:30: asleep
    assert world.due_day(datetime(2026, 11, 25, 13, 0, tzinfo=UTC), SEOUL, QUIET, at) is None  # 22:00: review time
    assert world.due_day(datetime(2026, 11, 24, 23, 0, tzinfo=UTC), SEOUL, None, at) == DAY  # 08:00 without quiet


def test_the_input_holds_who_himari_is_its_life_and_the_last_weeks_events_never_the_user() -> None:
    life = (entry(1, LifeKind.FACT, "고양이를 무서워한다."),
            replace(entry(2, LifeKind.ARC, "풍경화 한 장", "2026-11-20"), title="그림 연습"),
            entry(3, LifeKind.PLAN, "전시를 보러 간다.", "2026-11-26", "14:00"),
            entry(4, LifeKind.EVENT, "유나가 찾아왔다.", "2026-11-23", "18:00"),
            entry(5, LifeKind.EVENT, "너무 옛날 일.", "2026-11-10", "18:00"),
            entry(6, LifeKind.DIARY, "어제 일기.", "2026-11-24"))
    data = world.request_data("이름: 루시", life, day=DAY, makes="그림", weather="맑음", topics=("첫눈",), quiet=QUIET)
    assert data == {
        "persona": "이름: 루시", "persona_makes": "그림", "facts": ["고양이를 무서워한다."],
        "arcs": [{"id": 2, "title": "그림 연습", "goal": "풍경화 한 장", "started": "2026-11-20", "moved_on": []}],
        "plans": [{"date": "2026-11-26", "time": "14:00", "text": "전시를 보러 간다."}],
        "recent_events": [{"date": "2026-11-23", "time": "18:00", "text": "유나가 찾아왔다."}],
        "day": "2026-11-25", "weekday": "Wednesday", "season": "autumn", "quiet_hours": "23:00-08:00",
        "weather": "맑음", "topics": ["첫눈"]}
    for words in ("never how HIMARI felt", "The user never appears", "Never repeat recent_events",
                  "never undo one", "Most days have none or one"):
        assert words in world.WORLD_INSTRUCTIONS, words


def test_an_answer_is_zero_to_two_events_with_a_time_outside_the_quiet_hours() -> None:
    assert world.parse_events(events({"time": "9:30", "text": " 창문으로 비가\n샜다. "}), QUIET) == (
        WorldEvent("09:30", "창문으로 비가 샜다."),)
    assert world.parse_events(events(), QUIET) == () and world.parse_events("{}") == ()
    for bad in (events(*[{"time": "10:00", "text": "x"}] * 3), events({"time": "02:00", "text": "자는 중"}),
                events({"text": "시각 없음"}), events({"time": "10:00", "text": "x" * (MAX_EVENT_CHARS + 1)}),
                events("odd"), "not json"):
        with pytest.raises(AnswerError):
            world.parse_events(bad, QUIET)


def setup(*script: object, clock: datetime = MORNING) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    writer = LifeWriter(data, provider, LifeSettings("m", time_zone="Asia/Seoul"), clock=lambda: clock,
                        composer=Composer(), quiet=QUIET)
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
        assert writer.next_world(MORNING) == datetime(2026, 11, 25, 23, 0, tzinfo=UTC)  # tomorrow 08:00 in Seoul

    asyncio.run(scenario())


def test_the_world_is_tried_three_times_a_date_and_not_while_asleep() -> None:
    async def scenario() -> None:
        _, provider, writer = setup(clock=datetime(2026, 11, 24, 22, 0, tzinfo=UTC))  # 07:00 in Seoul
        assert await writer.world(ME) is WriteOutcome.NOTHING and provider.sent == []
        _, provider, writer = setup(final("no"), NativeNoResponse("http_500"),
                                    final(events({"time": "03:00", "text": "자는 중"})))
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
                             composer=Composer(), quiet=QUIET)
        assert await writer2.write(ME) is WriteOutcome.WRITTEN
        told = json.loads(provider.prepared_inputs[-1].conversation[0].blocks[-1].text)  # type: ignore[union-attr]
        assert told["events"] == [{"time": "15:00", "text": "비가 새서 도면이 젖었다."},
                                  {"time": "21:00", "text": "밤에 유나가 왔다."}]
        entries = await data.list_life(ME, NO_PERSONA)
        afternoon = datetime(2026, 11, 25, 16, 0, tzinfo=SEOUL)
        assert [e.text for e in world.passed_events(entries, afternoon)] == ["비가 새서 도면이 젖었다."]

    asyncio.run(scenario())


def test_the_world_never_runs_in_quiet_hours_that_begin_before_the_review() -> None:
    """Review P2-10: quiet 20:00-08:00 and the review at 22:00 — 21:00 is asleep: no world, first or retry."""
    quiet, at = (time(20, 0), time(8, 0)), time(22, 0)
    assert world.due_day(datetime(2026, 11, 25, 12, 0, tzinfo=UTC), SEOUL, quiet, at) is None  # 21:00 in Seoul
    assert world.due_day(datetime(2026, 11, 25, 10, 59, tzinfo=UTC), SEOUL, quiet, at) == DAY  # 19:59
    daytime = (time(13, 0), time(14, 0))  # a range that does not wrap
    assert world.due_day(datetime(2026, 11, 25, 4, 30, tzinfo=UTC), SEOUL, daytime, at) is None  # 13:30
    assert world.due_day(datetime(2026, 11, 25, 5, 30, tzinfo=UTC), SEOUL, daytime, at) == DAY  # 14:30
