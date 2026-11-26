"""HIMARI's day in the prompt (character-day v0.1 §8, §18.5): the day session's rules (SCHEDULE_RULE, SLEEP_RULE right
after LIFE_RULE, only there), its runtime state (himari_schedule, himari_sleep, himari_day), the time line of a message
received asleep, the schedule Tools offered only in the day session, and the diary's request in the session's
context."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from himari.common import ConversationId, EventId, TextBlock
from himari.data.api import (
    AsleepAnswer,
    AssistantFinal,
    ConversationEvent,
    DayBefore,
    DayLine,
    EventKind,
    FellBy,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PersistedStateBasis,
    PlanKind,
    PlanState,
    SleepRecord,
    SleepState,
    SleepView,
    UserMessage,
    Wake,
    WakeKind,
    WakeReason,
    WokeBy,
)
from himari.prompt.api import (
    ASLEEP_MARK,
    BOND_RULE,
    DAY_SESSION,
    LIFE_RULE,
    SCHEDULE_RULE,
    SLEEP_RULE,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    UserTurn,
    himari_day_data,
    himari_schedule_data,
    himari_sleep_data,
    state_data,
    wake_text,
)
from himari.prompt.compaction import current_state
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 26, 23, 45, tzinfo=SEOUL)
AT = datetime(2026, 11, 20, tzinfo=UTC)
CONV = ConversationId("conv_1")


def slot(entry_id: int, day: str, at: str, minutes: int, kind: PlanKind, text: str, *,
         state: PlanState | None = None, reason: str | None = None) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.PLAN, day, text, LifeOrigin.HIMARI, 1, AT, AT, time_of_day=at,
                     minutes=minutes, plan_kind=kind, plan_state=state, reason=reason)


SLEEP = slot(1, "2026-11-27", "00:15", 480, PlanKind.SLEEP, "잠")
LIVED = SystemSpec(companion=True, life=True, proactive=True)


def inputs(*, day: bool = True, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True, day_session=day),
                        trigger_refs=(), system=LIVED, now=NOW, time_zone="Asia/Seoul")
    return replace(base, **more)  # type: ignore[arg-type]


def test_the_day_sessions_rules_follow_the_life_rule_there_only() -> None:
    composed = "\n\n".join(system_text(inputs()))
    assert f"{LIFE_RULE}\n{SCHEDULE_RULE}\n{SLEEP_RULE}\n{BOND_RULE}" in composed
    other = "\n\n".join(system_text(inputs(day=False)))
    assert SCHEDULE_RULE not in other and SLEEP_RULE not in other and "himari_schedule" not in other
    assert "himari_schedule" in principle_section(False, life=True, day=True)
    assert "together" in SCHEDULE_RULE and "never drop" in SCHEDULE_RULE and "go_to_sleep" in SLEEP_RULE
    assert "half_asleep" in SLEEP_RULE and "one short line" in SLEEP_RULE
    assert RequestOptions("m", day_session=True).tool_capabilities == frozenset({DAY_SESSION})
    assert RequestOptions("m").tool_capabilities == frozenset()


def test_the_schedule_tells_today_and_tomorrow_with_what_no_longer_stands() -> None:
    slots = (SLEEP, slot(2, "2026-11-26", "21:00", 60, PlanKind.OWN, "산책", state=PlanState.CANCELLED,
                         reason="비가 와서"),
             slot(3, "2026-11-27", "15:00", 120, PlanKind.TOGETHER, "게임"),
             slot(4, "2026-11-29", "10:00", 60, PlanKind.OWN, "먼 일"))
    data = himari_schedule_data(slots, NOW)
    assert data["today"] == [{"id": 2, "kind": "own", "text": "산책", "start": "21:00", "end": "22:00", "minutes": 60,
                              "state": "cancelled", "reason": "비가 와서"}]
    assert data["tomorrow"] == [
        {"id": 1, "kind": "sleep", "text": "잠", "start": "00:15", "end": "08:15", "minutes": 480},
        {"id": 3, "kind": "together", "text": "게임", "start": "15:00", "end": "17:00", "minutes": 120}]


def test_the_sleep_tells_drowsy_minutes_the_last_sleep_and_a_fresh_waking() -> None:
    drowsy = SleepView(SleepState.DROWSY, minutes_to_sleep=30, tonight=SLEEP)
    assert himari_sleep_data(drowsy, NOW) | {"about": ""} == {
        "about": "", "state": "drowsy", "minutes_to_sleep": 30, "tonight": {"id": 1, "start": "00:15", "end": "08:15"}}
    fell = datetime(2026, 11, 27, 0, 15, tzinfo=SEOUL)
    woke = datetime(2026, 11, 27, 3, 12, tzinfo=SEOUL)
    last = SleepRecord(5, 1, fell, FellBy.SCHEDULE, woke, WokeBy.USER, messages=3, first_message_at=woke - timedelta(
        minutes=4), last_message_at=woke)
    data = himari_sleep_data(SleepView(SleepState.AWAKE, tonight=SLEEP, last=last, woken_at=woke), woke)
    assert data["last"] == {"fell_asleep": "2026-11-27T00:15+09:00", "how": "schedule",
                            "woke": "2026-11-27T03:12+09:00", "how_woke": "user", "messages_while_asleep": 3,
                            "between": "03:08-03:12"}
    assert data["just_woke"] == {"at": "03:12", "slept_minutes": 177}
    assert himari_sleep_data(SleepView(SleepState.HALF_ASLEEP, last=last), woke)["state"] == "half_asleep"


def test_the_day_before_is_told_in_the_state_only_when_given() -> None:
    before = DayBefore("2026-11-26", (DayLine("23:58", "user", "잘 자"),), missed=("23:00 통화",),
                       fell_at=datetime(2026, 11, 27, 0, 15, tzinfo=SEOUL), how_fell=FellBy.TOOL,
                       delayed="방송 보고 잘래", woken=1, woke_at=datetime(2026, 11, 27, 9, 0, tzinfo=SEOUL))
    told = himari_day_data(before, NOW)["before"]
    assert told == {"day": "2026-11-26", "last_words": [{"at": "23:58", "who": "user", "text": "잘 자"}],
                    "promises_missed": ["23:00 통화"],
                    "fell_asleep": {"at": "2026-11-27T00:15+09:00", "how": "tool", "put_off_because": "방송 보고 잘래"},
                    "woken_by_user": 1, "woke_up": "2026-11-27T09:00+09:00"}
    data = state_data(current_state(inputs(schedule=(SLEEP,), sleep=SleepView(SleepState.AWAKE), day_before=before)))
    assert {"himari_schedule", "himari_sleep", "himari_day"} <= set(data)
    bare = state_data(current_state(inputs()))
    assert not {"himari_schedule", "himari_sleep", "himari_day"} & set(bare)


def event(sequence: int, at: datetime, payload: object, kind: EventKind) -> ConversationEvent:
    return ConversationEvent(EventId(f"e{sequence}"), CONV, sequence, kind, payload, 1, at)  # type: ignore[arg-type]


def test_a_message_received_asleep_always_has_its_time_line_marked() -> None:
    first = NOW - timedelta(minutes=5)
    events = (event(1, first, UserMessage((TextBlock("안녕"),)), EventKind.USER_MESSAGE),
              event(2, first + timedelta(minutes=1), AssistantFinal((TextBlock("응"),)), EventKind.ASSISTANT_FINAL),
              event(3, first + timedelta(minutes=2), UserMessage((TextBlock("자?"),), while_asleep=AsleepAnswer.NONE),
                    EventKind.USER_MESSAGE),
              event(4, first + timedelta(minutes=3), UserMessage((TextBlock("일어나"),),
                                                                  while_asleep=AsleepAnswer.MUMBLE),
                    EventKind.USER_MESSAGE))
    composed = Composer().compose(inputs(events=events))
    turns = [p for p in composed.conversation if isinstance(p, UserTurn)]
    assert turns[0].blocks[0] == TextBlock("<time>2026-11-26T23:40+09:00</time>")
    assert turns[1].blocks[0] == TextBlock("<time>2026-11-26T23:42+09:00</time>" + ASLEEP_MARK)
    assert turns[2].blocks[0] == TextBlock("<time>2026-11-26T23:43+09:00</time>" + ASLEEP_MARK)


def test_the_diary_is_asked_at_the_end_of_the_sessions_own_request_with_no_tools() -> None:
    events = (event(1, NOW, UserMessage((TextBlock("오늘 재밌었어"),)), EventKind.USER_MESSAGE),
              event(2, NOW, AssistantFinal((TextBlock("[[think]]좋았다[[/think]]나도!"),)), EventKind.ASSISTANT_FINAL))
    composer = Composer()
    turn = composer.compose(inputs(events=events))
    diary = composer.session_input(inputs(events=events), "[The day's diary]", {"day": "2026-11-26"})
    assert diary.stable_prefix == turn.stable_prefix and diary.tool_catalog == ()
    assert diary.conversation[:-1] == turn.conversation
    last = diary.conversation[-1]
    assert isinstance(last, UserTurn) and last.blocks[0] == TextBlock("[The day's diary]")
    assert diary.current_state == turn.current_state


def test_a_together_wake_names_its_slot_as_read_now() -> None:
    promise = slot(3, "2026-11-27", "03:00", 120, PlanKind.TOGETHER, "방송 같이 보기")
    wake = Wake("w1", (WakeReason(WakeKind.TOGETHER, entry_id=3),), "d1")
    assert '"slot": {"id": 3, "minutes": 120, "start": "03:00", "text": "방송 같이 보기"}' in wake_text(
        wake, {}, life={3: promise})
    assert '"deleted": true' in wake_text(wake, {}, life={})


def test_the_schedule_and_sleep_are_told_on_the_server_zones_clock_not_the_devices() -> None:
    """§8 (review 10): at 00:30 in Seoul on a Los Angeles device, today is Seoul's (the slots' dates) — ``now`` stays
    the device's."""
    la = timezone(timedelta(hours=-8))
    seoul_now = datetime(2026, 11, 27, 0, 30, tzinfo=SEOUL)
    slots = (SLEEP, slot(3, "2026-11-27", "15:00", 120, PlanKind.TOGETHER, "게임"),
             slot(4, "2026-11-28", "10:00", 60, PlanKind.OWN, "산책"))
    last = SleepRecord(5, 1, datetime(2026, 11, 26, 15, 15, tzinfo=UTC), FellBy.SCHEDULE)
    data = state_data(current_state(inputs(now=seoul_now.astimezone(la), schedule=slots,
                                           sleep=SleepView(SleepState.ASLEEP, tonight=SLEEP, last=last))))
    assert [s["id"] for s in data["himari_schedule"]["today"]] == [1, 3]  # type: ignore[index]
    assert [s["id"] for s in data["himari_schedule"]["tomorrow"]] == [4]  # type: ignore[index]
    assert data["himari_sleep"]["last"]["fell_asleep"] == "2026-11-27T00:15+09:00"  # type: ignore[index]
    assert data["now"].endswith("-08:00")
