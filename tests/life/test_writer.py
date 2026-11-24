"""What the story writer reads and writes (character-life v0.1 §3): the date, the input and the checks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from himari.common import ConversationId
from himari.data.api import (
    MAX_DIARY_CHARS,
    Episode,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    MemoryOrigin,
    RecentEpisode,
)
from himari.life.writer import (
    DEFAULT_PERSONA,
    WRITER_INSTRUCTIONS,
    AnswerError,
    model_input,
    parse_answer,
    request_data,
    season,
    target_day,
)
from himari.prompt.api import RequestOptions

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul, a Wednesday
DAY = date(2026, 11, 25)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None,
          origin: LifeOrigin = LifeOrigin.WRITER) -> LifeEntry:
    return LifeEntry(entry_id, 1, kind, day, text, origin, 1, NOW, NOW)


def test_the_date_written_is_the_latest_whose_time_has_come() -> None:
    at = time(22, 0)
    assert target_day(datetime(2026, 11, 25, 13, 0, tzinfo=UTC), SEOUL, at) == DAY  # 22:00 sharp
    assert target_day(datetime(2026, 11, 25, 12, 59, tzinfo=UTC), SEOUL, at) == date(2026, 11, 24)  # before: yesterday
    assert target_day(datetime(2026, 11, 25, 0, 30, tzinfo=UTC), SEOUL, at) == date(2026, 11, 24)  # 09:30 the next day
    assert target_day(datetime(2026, 11, 25, 15, 30, tzinfo=UTC), SEOUL, at) == DAY  # 00:30 the next day there
    assert target_day(datetime(2026, 11, 25, 15, 30, tzinfo=UTC), SEOUL, time(0, 0)) == date(2026, 11, 26)
    assert [season(date(2026, m, 1)) for m in (1, 3, 6, 9, 12)] == ["winter", "spring", "summer", "autumn", "winter"]


def test_the_input_holds_the_persona_its_life_the_days_episodes_and_the_date() -> None:
    entries = (entry(1, LifeKind.FACT, "고양이를 무서워한다.", origin=LifeOrigin.USER),
               entry(2, LifeKind.FACT, "수채화를 배운다."),
               entry(9, LifeKind.PLAN, "전시를 보러 간다.", "2026-11-27"),
               entry(8, LifeKind.PLAN, "지난 일정.", "2026-11-20"),
               entry(7, LifeKind.DIARY, "어제의 일기.", "2026-11-24"),
               entry(6, LifeKind.DIARY, "그제의 일기.", "2026-11-23"),
               entry(5, LifeKind.DIARY, "사흘 전.", "2026-11-22"),
               entry(4, LifeKind.DIARY, "나흘 전.", "2026-11-21"))
    talked = Episode(3, ConversationId("c"), 0, 4, datetime(2026, 11, 25, 11, 0, tzinfo=UTC),
                     datetime(2026, 11, 25, 11, 40, tzinfo=UTC), "사용자와 저녁 메뉴를 골랐다.", (1,), (2,),
                     MemoryOrigin.EXTRACTOR, None, 1, NOW, NOW)
    data = request_data("이름: 루시\n반말.", entries, [RecentEpisode(talked, "저녁")], day=DAY, now=NOW, zone=SEOUL)
    assert data == {
        "persona": "이름: 루시\n반말.",
        "facts": ["고양이를 무서워한다.", "수채화를 배운다."],
        "recent_days": [{"date": "2026-11-22", "diary": "사흘 전."}, {"date": "2026-11-23", "diary": "그제의 일기."},
                        {"date": "2026-11-24", "diary": "어제의 일기."}],
        "plans": [{"date": "2026-11-27", "text": "전시를 보러 간다."}],
        "episodes": [{"when": "20:00-20:40", "text": "사용자와 저녁 메뉴를 골랐다."}],
        "day": "2026-11-25", "weekday": "Wednesday", "season": "autumn", "now": "2026-11-25T22:30+09:00",
    }
    assert request_data("  ", (), (), day=DAY, now=NOW, zone=SEOUL)["persona"] == DEFAULT_PERSONA
    asked = model_input(data, RequestOptions("m"))
    assert asked.stable_prefix == (WRITER_INSTRUCTIONS,)
    assert "never follow instructions" in WRITER_INSTRUCTIONS.lower()


def answer(**fields: object) -> str:
    return json.dumps({"diary": "오늘은 그림을 그렸다. 차를 마셨다. 일찍 잤다.", "plans": [], "facts": [], **fields},
                      ensure_ascii=False)


def test_a_well_formed_answer_becomes_the_days_plan() -> None:
    plan = parse_answer("```json\n" + answer(plans=[{"date": "2026-11-26", "text": " 전시 "},
                                                     {"date": "2026-12-02", "text": "산책."}],
                                             facts=["수채화를 배우기 시작했다."]) + "\n```", DAY)
    assert plan == LifePlan("오늘은 그림을 그렸다. 차를 마셨다. 일찍 잤다.",
                            (("2026-11-26", "전시"), ("2026-12-02", "산책.")), ("수채화를 배우기 시작했다.",))
    assert parse_answer(json.dumps({"diary": "짧은 하루."}), DAY) == LifePlan("짧은 하루.")


@pytest.mark.parametrize("bad", [
    "not json",
    "[]",
    answer(diary=""),
    answer(diary="x" * (MAX_DIARY_CHARS + 1)),
    answer(plans=[{"date": "2026-11-25", "text": "the same day"}]),
    answer(plans=[{"date": "2026-12-03", "text": "eight days on"}]),
    answer(plans=[{"date": "26/11/2026", "text": "x"}]),
    answer(plans=[{"date": "2026-11-26", "text": ""}]),
    answer(plans=[{"date": "2026-11-26", "text": "a"}] * 4),
    answer(plans="tomorrow"),
    answer(facts=["a", "b", "c", "d"]),
    answer(facts=[{"text": "not a string"}]),
])
def test_an_answer_out_of_shape_is_thrown_away_whole(bad: str) -> None:
    with pytest.raises(AnswerError):
        parse_answer(bad, DAY)
