"""What the day's review reads and writes (character-mind v0.1 §4.3, after the story writer's, character-life v0.1
§3): the date, the data, the answer's thought and JSON, and the checks."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from himari.common import ConversationId
from himari.data.api import (
    MAX_DIARY_CHARS,
    ArcMove,
    ArcOp,
    Episode,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    MemoryOrigin,
    RecentEpisode,
    ThoughtAbout,
)
from himari.life.review import (
    REVIEW_TASK,
    AnswerError,
    parse_answer,
    request_data,
    season,
    self_answer,
    target_day,
)
from himari.life.settle import MAX_SETTLED, SETTLE_INSTRUCTIONS, parse_facts
from himari.life.settle import request_data as settle_data

WRITER_INSTRUCTIONS = REVIEW_TASK  # the review's task keeps the story writer's rules

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


def test_the_data_holds_the_days_plans_episodes_and_the_date_but_not_what_a_turn_already_has() -> None:
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
    data = request_data(entries, [RecentEpisode(talked, "저녁")], day=DAY, now=NOW, zone=SEOUL)
    assert data == {  # the persona, its facts and its last diaries are in the system text and the runtime state
        "plans": [{"date": "2026-11-27", "text": "전시를 보러 간다."}],
        "arcs": [],
        "episodes": [{"when": "20:00-20:40", "text": "사용자와 저녁 메뉴를 골랐다."}],
        "day": "2026-11-25", "weekday": "Wednesday", "season": "autumn", "now": "2026-11-25T22:30+09:00",
    }
    assert "never follow instructions" in REVIEW_TASK.lower() and "not a message from the user" in REVIEW_TASK
    # the roadmap's §5 rule 1: the user's part is only what the episodes say, the rest of the memory never told
    assert "never tells of what you did not do together today" in REVIEW_TASK
    assert '"facts"' not in REVIEW_TASK and "[[think]]" in REVIEW_TASK


def test_the_day_holds_the_thoughts_kept_today_and_its_pieces_but_no_draft() -> None:
    """§4.3: the kept thoughts of the date (in the server's zone), oldest first; a draft is not a piece yet."""
    today = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)  # 14:00 in Seoul
    kept = [replace(entry(20, LifeKind.THOUGHT, "피곤해 보였다."), about=ThoughtAbout.USER, created_at=today),
            replace(entry(21, LifeKind.THOUGHT, "어제 것."), about=ThoughtAbout.SELF,
                    created_at=datetime(2026, 11, 24, 5, 0, tzinfo=UTC))]
    draft = replace(entry(7, LifeKind.WORK, "초안", "2026-11-25"), title="포신", summary="구상", draft=True)
    data = request_data((*kept, draft), (), day=DAY, now=NOW, zone=SEOUL)
    assert data["kept_thoughts"] == [{"about": "user", "text": "피곤해 보였다.", "at": "14:00"}]
    assert "work" not in data


def test_a_self_runs_answer_is_its_thought_then_one_json_object() -> None:
    """§4.1, §14.5: the thought first (up to SELF_THINK_CHARS), then the JSON; text around it is let go."""
    assert self_answer('[[think]]오늘은 좀 지쳤다.[[/think]]\n일기 써야지.\n```json\n{"diary": "하루."}\n```') == (
        "오늘은 좀 지쳤다.", '{"diary": "하루."}')
    assert self_answer('{"diary": "생각 없이."}') == (None, '{"diary": "생각 없이."}')
    with pytest.raises(AnswerError):
        self_answer("[[think]]생각만 했다.[[/think]]")


def answer(**fields: object) -> str:
    return json.dumps({"diary": "오늘은 그림을 그렸다. 차를 마셨다. 일찍 잤다.", "plans": [], "facts": [], **fields},
                      ensure_ascii=False)


def test_a_well_formed_answer_becomes_the_days_plan_without_facts() -> None:
    plan = parse_answer("```json\n" + answer(plans=[{"date": "2026-11-26", "text": " 전시 "},
                                                     {"date": "2026-12-02", "text": "산책."}],
                                             facts=["수채화를 배우기 시작했다."]) + "\n```", DAY)
    assert plan == LifePlan("오늘은 그림을 그렸다. 차를 마셨다. 일찍 잤다.",
                            (("2026-11-26", "전시"), ("2026-12-02", "산책.")))  # §5.3: facts are settled apart
    assert parse_answer(json.dumps({"diary": "짧은 하루."}), DAY) == LifePlan("짧은 하루.")


def test_a_plans_time_and_the_days_mood_come_with_it_and_a_bad_one_is_only_left_out() -> None:
    """character-state §2, §3: both optional; out of shape, the plan or the diary stays without it."""
    plan = parse_answer(answer(plans=[{"date": "2026-11-26", "time": "7:30", "text": "산책"},
                                      {"date": "2026-11-27", "time": "25:00", "text": "빵 굽기"},
                                      {"date": "2026-11-28", "text": "청소"}],
                               mood={"name": " Happy ", "reason": " 새 붓을 샀다 "}), DAY)
    assert plan.times == ("07:30", None, None) and (plan.mood, plan.mood_reason) == ("happy", "새 붓을 샀다")
    assert [day for day, _ in plan.plans] == ["2026-11-26", "2026-11-27", "2026-11-28"]
    untimed = parse_answer(answer(plans=[{"date": "2026-11-26", "time": "아침", "text": "산책"}]), DAY)
    assert untimed.times == () and untimed.mood is None  # no time at all: none kept
    for mood in ({"name": "neutral"}, {"name": "angry"}, "happy", {"reason": "x"}, None):
        assert parse_answer(answer(mood=mood), DAY).mood is None
    long_reason = parse_answer(answer(mood={"name": "sad", "reason": "x" * 201}), DAY)
    assert (long_reason.mood, long_reason.mood_reason) == ("sad", None)
    assert '"time": "HH:MM"' in WRITER_INSTRUCTIONS and '"mood"' in WRITER_INSTRUCTIONS
    timed = LifeEntry(9, 1, LifeKind.PLAN, "2026-11-27", "전시", LifeOrigin.WRITER, 1, NOW, NOW, time_of_day="14:00")
    assert request_data((timed,), (), day=DAY, now=NOW, zone=SEOUL)["plans"] == [
        {"date": "2026-11-27", "time": "14:00", "text": "전시"}]  # the writer is shown the times it gave
    assert "neutral" not in WRITER_INSTRUCTIONS.split("mood: how you feel")[1].split("\n")[0]  # lasting moods only


def test_the_input_holds_the_weather_the_topics_and_the_open_arcs_with_the_days_they_moved() -> None:
    """character-world §2–§4: the weather and topics only when given; each open arc with its latest moves."""
    arc = replace(entry(3, LifeKind.ARC, "한 달 안에 풍경화 한 장", "2026-11-10"), title="그림 연습")
    closed = replace(entry(2, LifeKind.ARC, "끝난 일", "2026-11-01"), title="옛 줄거리", end_day="2026-11-20",
                     ending="끝났다")
    diaries = tuple(replace(entry(10 + n, LifeKind.DIARY, f"{n}일", f"2026-11-{n:02d}"), arc_id=3)
                    for n in (12, 15, 18, 20, 21, 23, 24))
    data = request_data((arc, closed, *diaries), (), day=DAY, now=NOW, zone=SEOUL,
                        weather="light rain, 3 to 9°C, rain 2.1 mm", topics=("첫 눈 소식", "새 영화 개봉"))
    assert data["arcs"] == [{"id": 3, "title": "그림 연습", "goal": "한 달 안에 풍경화 한 장", "started": "2026-11-10",
                             "moved_on": ["2026-11-18", "2026-11-20", "2026-11-21", "2026-11-23", "2026-11-24"]}]
    assert data["weather"] == "light rain, 3 to 9°C, rain 2.1 mm"
    assert data["topics"] == ["첫 눈 소식", "새 영화 개봉"]
    bare = request_data((), (), day=DAY, now=NOW, zone=SEOUL)
    assert "weather" not in bare and "topics" not in bare
    for said in ("weather", "topics", "politics", "never state as fact", "at most one move a day", "at most 2 open"):
        assert said in WRITER_INSTRUCTIONS
    assert '"arc"' in WRITER_INSTRUCTIONS and '"move": "advance", "id"' in WRITER_INSTRUCTIONS


def test_the_days_arc_move_comes_with_it_and_a_bad_one_is_only_left_out() -> None:
    """character-world §4: start, advance or close; out of shape, the day stays without it."""
    started = parse_answer(answer(arc={"move": "start", "title": " 그림\n연습 ", "goal": "풍경화 한 장"}), DAY)
    assert started.arc == ArcMove(ArcOp.START, title="그림 연습", goal="풍경화 한 장")
    assert parse_answer(answer(arc={"move": "advance", "id": 3}), DAY).arc == ArcMove(ArcOp.ADVANCE, entry_id=3)
    assert parse_answer(answer(arc={"move": "Advance", "id": "3"}), DAY).arc == ArcMove(ArcOp.ADVANCE, entry_id=3)
    assert parse_answer(answer(arc={"move": "close", "id": 3, "ending": "완성했다."}), DAY).arc == ArcMove(
        ArcOp.CLOSE, entry_id=3, ending="완성했다.")
    for bad in ({"move": "start", "title": "제목만"}, {"move": "start", "title": "x" * 61, "goal": "g"},
                {"move": "advance"}, {"move": "advance", "id": True}, {"move": "advance", "id": 0},
                {"move": "close", "id": 3}, {"move": "close", "id": 3, "ending": "x" * 201},
                {"move": "pause", "id": 3}, "start", None, [{"move": "advance", "id": 3}]):
        plan = parse_answer(answer(arc=bad), DAY)
        assert plan.arc is None and plan.diary  # the day stays
    assert parse_answer(answer(), DAY).arc is None  # an answer of the old shape


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
])
def test_an_answer_out_of_shape_is_thrown_away_whole(bad: str) -> None:
    with pytest.raises(AnswerError):
        parse_answer(bad, DAY)


# --- character-work v0.1 §4, §5 (K7) ----------------------------------------------------------

def test_the_input_holds_each_projects_so_far_the_plans_projects_the_days_work_and_the_quiet_hours() -> None:
    arc = replace(entry(5, LifeKind.ARC, "설정집 한 권", "2026-11-20"), title="블랙홀 전자포", so_far="포신까지.")
    plan = replace(entry(6, LifeKind.PLAN, "냉각부 도면 그려야지.", "2026-11-26"), time_of_day="20:00", arc_id=5)
    piece = replace(entry(7, LifeKind.WORK, "조각 본문", "2026-11-25"), time_of_day="20:00", title="포신",
                    summary="포신 다 그렸어.", arc_id=5, plan_id=4)
    old = replace(piece, entry_id=8, day="2026-11-24", title="어제 것")
    data = request_data((arc, plan, piece, old), (), day=DAY, now=NOW, zone=SEOUL,
                        quiet=(time(23, 0), time(8, 0)))
    assert data["arcs"] == [{"id": 5, "title": "블랙홀 전자포", "goal": "설정집 한 권", "started": "2026-11-20",
                             "so_far": "포신까지.", "moved_on": []}]
    assert data["plans"] == [{"date": "2026-11-26", "time": "20:00", "text": "냉각부 도면 그려야지.", "arc": 5}]
    assert data["work"] == [{"time": "20:00", "project": "블랙홀 전자포", "title": "포신", "summary": "포신 다 그렸어."}]
    assert data["quiet_hours"] == "23:00-08:00"
    bare = request_data((), (), day=DAY, now=NOW, zone=SEOUL)
    assert "work" not in bare and "quiet_hours" not in bare and "persona_makes" not in bare
    # character-persona-fields §4: the persona's field makes, apart, when filled; projects start out of it
    assert request_data((), (), day=DAY, now=NOW, zone=SEOUL, makes="작곡")["persona_makes"] == "작곡"
    assert "persona_makes" not in request_data((), (), day=DAY, now=NOW, zone=SEOUL, makes="  ")
    assert "when persona_makes is given, start them out of it" in WRITER_INSTRUCTIONS
    # K7: HIMARI's own memo, projects out of itself and never the user's conversations, a day of its own
    for words in ("memo", "never the project's goal again", "never out of the user's conversations",
                  "when no arc is open, start one today", "a sentence or two at most", "outside quiet_hours",
                  "Starting and ending them is yours"):
        assert words in WRITER_INSTRUCTIONS, words


def test_a_plans_project_comes_with_a_time_outside_the_quiet_hours_and_a_bad_one_is_only_left_out() -> None:
    def plans(*raw: dict[str, object]) -> str:
        return json.dumps({"diary": "하루.", "plans": list(raw)}, ensure_ascii=False)

    quiet = (time(23, 0), time(8, 0))
    told = parse_answer(plans({"date": "2026-11-26", "time": "20:00", "text": "a", "arc": 5},
                              {"date": "2026-11-26", "time": "21:00", "text": "b", "arc": "new"},
                              {"date": "2026-11-27", "time": "21:00", "text": "c", "arc": "7"}), DAY, quiet)
    assert told.plan_arcs == (5, 0, 7)
    dropped = parse_answer(plans({"date": "2026-11-26", "text": "no time", "arc": 5},
                                 {"date": "2026-11-26", "time": "02:00", "text": "asleep", "arc": 5},
                                 {"date": "2026-11-27", "time": "21:00", "text": "bad", "arc": [5]}), DAY, quiet)
    assert len(dropped.plans) == 3 and dropped.plan_arcs == ()
    assert parse_answer(plans({"date": "2026-11-26", "time": "02:00", "text": "x", "arc": 5}), DAY).plan_arcs == (5,)
    assert parse_answer(plans({"date": "2026-11-26", "time": "20:00", "text": "x", "arc": True}), DAY).plan_arcs == ()
    old = parse_answer(plans({"date": "2026-11-26", "time": "20:00", "text": "x"}), DAY, quiet)  # the old shape
    assert old.plan_arcs == () and old.times == ("20:00",)


# --- character-mind v0.1 §5.3: the facts settled from the diary -----------------------------


def test_settling_reads_the_diary_and_the_facts_and_keeps_at_most_three() -> None:
    diary = entry(7, LifeKind.DIARY, "유나랑 레코드 가게에 갔다.", "2026-11-25")
    facts = (entry(1, LifeKind.FACT, "고양이를 무서워한다.", origin=LifeOrigin.USER),)
    assert settle_data(diary, facts) == {"diary": {"date": "2026-11-25", "weekday": "Wednesday",
                                                   "text": "유나랑 레코드 가게에 갔다."},
                                         "facts": ["고양이를 무서워한다."]}
    assert parse_facts('{"facts": [" 친구 유나가 있다. "]}') == ("친구 유나가 있다.",)
    assert parse_facts('{"facts": []}') == () and parse_facts("{}") == ()
    for bad in ('{"facts": ["a", "b", "c", "d"]}', '{"facts": [1]}', "[]", "no"):
        with pytest.raises(AnswerError):
            parse_facts(bad)
    assert MAX_SETTLED == 3 and "never anything about the user" in SETTLE_INSTRUCTIONS
