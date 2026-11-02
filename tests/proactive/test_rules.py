"""V3-P87 §5–§6.4: the triggers, the rules and the merging, as pure functions of a clock."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from himari.common import ConversationId, PrincipalId
from himari.data.api import Memory, MemoryCategory, MemoryOrigin, WakeKind, WakeReason, WakeRecord, WakeStatus, parse_note_date
from himari.presence.api import ActivitySignal, DeviceState, EndedActivity, TaskEnded
from himari.proactive import (
    CHECK_IN_WINDOW,
    ProactiveSettings,
    Return,
    Situation,
    Told,
    candidate_keys,
    candidates,
    decide,
    in_quiet_hours,
    local_midnight,
    loop_moment,
)

SEOUL = "Asia/Seoul"
NOW = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)  # 16:00 in Seoul
ON = ProactiveSettings(enabled=True)


def loop(memory_id: int, due: str, category: MemoryCategory = MemoryCategory.LOOP) -> Memory:
    parsed = parse_note_date(due, SEOUL)
    assert parsed is not None
    return Memory(memory_id, category, f"고리 {memory_id}", MemoryOrigin.MODEL, None, None, 1, NOW, NOW,
                  due_at=parsed[0], due_instant=parsed[1])


def device(activity: ActivitySignal | None = None, before: EndedActivity | None = None, *,
           sleeps: bool = False, sleep: str | None = None) -> DeviceState:
    return DeviceState("d1", True, 0.0, SEOUL, activity=activity, before=before, sleeps=sleeps, sleep=sleep)


def wake(at: datetime, *kinds: WakeKind) -> WakeRecord:
    return WakeRecord(f"w{at.timestamp()}", PrincipalId("operator"), ConversationId("c"), 1,
                      tuple(WakeReason(k, memory_id=1 if k in (WakeKind.REMINDER, WakeKind.LOOP_DUE) else None)
                            for k in kinds or (WakeKind.LOOP_DUE,)), "d1", WakeStatus.ANSWERED, False, at, at)


def situation(**changes: object) -> Situation:
    base = Situation(now=NOW, settings=ON, zone=SEOUL, device_id="d1", device=device(), quiet_until=None,
                     last_user_message_at=None, working=False, unanswered_wake=False, wakes=(), loops=(),
                     returned=None)
    return replace(base, **changes)  # type: ignore[arg-type]


# --- §5.1 loops ---------------------------------------------------------------------------

def test_a_loop_is_spoken_of_an_hour_after_its_time_or_at_six_on_its_day() -> None:
    assert loop_moment(loop(1, "2026-11-04T14:00+09:00")) == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # 15:00 KST
    assert loop_moment(loop(2, "2026-11-04")) == datetime(2026, 11, 4, 9, 0, tzinfo=UTC)  # 18:00 KST
    assert loop_moment(loop(3, "2026-11-04", MemoryCategory.USER)) is None  # only loops
    assert loop_moment(replace(loop(4, "2026-11-04"), due_at=None, due_instant=None)) is None


def test_a_loop_waits_for_its_moment_and_expires_a_day_after() -> None:
    timed = loop(1, "2026-11-04T14:00+09:00")
    before = decide(situation(now=datetime(2026, 11, 4, 5, 59, tzinfo=UTC), loops=(timed,)))
    assert before.reasons == () and before.next_at == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
    due = decide(situation(loops=(timed,)))
    assert due.reasons == (WakeReason(WakeKind.LOOP_DUE, memory_id=1),)
    assert due.keys == ("loop:m1:2026-11-04T14:00+09:00",)
    late = decide(situation(now=datetime(2026, 11, 4, 6, 0, tzinfo=UTC) + CHECK_IN_WINDOW, loops=(timed,)))
    assert late.reasons == () and late.expired == ("loop:m1:2026-11-04T14:00+09:00",)
    assert decide(situation(loops=(timed,)), frozenset(due.keys)).reasons == ()  # spoken once


def test_a_loop_with_a_new_date_is_a_new_key() -> None:
    first = loop(1, "2026-11-04T14:00+09:00")
    moved = loop(1, "2026-11-04T15:00+09:00")
    taken = frozenset(decide(situation(loops=(first,))).keys)
    assert decide(situation(now=NOW + timedelta(hours=1), loops=(moved,)), taken).keys == (
        "loop:m1:2026-11-04T15:00+09:00",)


# --- §5.2 returns -------------------------------------------------------------------------

RETURNED_AT = NOW - timedelta(seconds=30)


def back(minutes: int | None = 290, device_id: str = "d1", at: datetime = RETURNED_AT, via: str = "unlock") -> Return:
    return Return(device_id, at, via, minutes)


def test_a_return_after_four_hours_is_greeted_once() -> None:
    decided = decide(situation(returned=back()))
    assert decided.reasons == (WakeReason(WakeKind.RETURNED, away_minutes=290, via="unlock"),)
    assert decided.keys == ("return:d1:2026-11-04T06:59Z",)
    assert decide(situation(returned=back()), frozenset(decided.keys)).reasons == ()


@pytest.mark.parametrize(("returned", "changes"), [
    (back(minutes=59), {}),  # under an hour (V3-P90 §8)
    (back(minutes=None), {}),  # no input known before it (a fresh start)
    (back(device_id="d2"), {}),  # the user moved to another computer since
    (back(at=NOW - timedelta(seconds=10)), {}),  # the 20 seconds the user may speak first in
    (back(at=NOW - timedelta(minutes=11)), {}),  # too old
    (back(), {"last_user_message_at": NOW - timedelta(seconds=5)}),  # spoke first after the unlock
    (back(), {"last_user_message_at": NOW - timedelta(hours=2)}),  # came back on the Web meanwhile
    (back(), {"settings": replace(ON, return_hours=0)}),  # greetings off
])
def test_a_return_that_is_not_greeted(returned: Return, changes: dict[str, object]) -> None:
    assert decide(situation(returned=returned, **changes)).reasons == ()


def test_a_message_before_the_absence_does_not_stop_the_greeting() -> None:
    long_ago = RETURNED_AT - timedelta(minutes=300)
    assert decide(situation(returned=back(), last_user_message_at=long_ago)).reasons != ()


# --- V3-P90 §5.3 a game or a video that ended ----------------------------------------------

def ended(group: str = "game", lasted_minutes: float = 95, ago: float = 60) -> DeviceState:
    return replace(device(), task_ended=TaskEnded(group, lasted_minutes * 60, ago))


def test_a_game_or_a_video_that_just_ended() -> None:
    decided = decide(situation(device=ended()))
    assert decided.reasons == (WakeReason(WakeKind.TASK_ENDED, group="game", minutes=95),)
    assert decided.keys == ("ended:d1:game:2026-11-04T06:59Z",)
    assert decide(situation(device=ended("video", 61))).reasons == (
        WakeReason(WakeKind.TASK_ENDED, group="video", minutes=61),)
    # a moment later, the end counted back across a minute: still taken
    later = situation(now=NOW + timedelta(seconds=40), device=ended(ago=100.5))
    assert decide(later, frozenset(decided.keys)).reasons == ()


@pytest.mark.parametrize("state", [
    ended(lasted_minutes=59),  # under an hour
    ended(ago=181),  # past three minutes: let go, not held
    ended("work"),  # the end of work is not told (§5.3)
    device(),  # no task ended
])
def test_an_end_that_is_not_spoken_of(state: DeviceState) -> None:
    assert decide(situation(device=state)).reasons == ()


def test_a_game_and_a_video_ending_a_minute_apart_are_two_ends() -> None:
    """PR #165 review P2-1: the group is in the key; the same end told again is still one."""
    game = decide(situation(device=ended("game", ago=70)))
    video = decide(situation(device=ended("video", ago=10)), frozenset(game.keys))
    assert video.reasons == (WakeReason(WakeKind.TASK_ENDED, group="video", minutes=95),)
    again = situation(now=NOW + timedelta(seconds=50), device=ended("game", ago=120))  # the app repeats it
    assert decide(again, frozenset(game.keys)).reasons == ()


def test_an_end_is_off_at_zero_minutes() -> None:
    assert decide(situation(device=ended(), settings=replace(ON, ended_minutes=0))).reasons == ()


def test_the_activity_in_front_is_no_trigger_any_more() -> None:
    """V3-P90 §4: a long focus and a game's end by the program in front are made no more."""
    long_focus = replace(device(ActivitySignal("coding", "Code", 3 * 3600)),
                         before=EndedActivity("game", "Game", 95 * 60, 60))
    assert decide(situation(device=long_focus)).reasons == ()


@pytest.mark.parametrize(("changes", "blocked"), [
    ({"device": replace(ended(), sleeps=True, sleep="idle")}, "asleep"),
    ({"quiet_until": NOW + timedelta(minutes=5)}, "quiet"),
    ({"last_user_message_at": NOW - timedelta(minutes=9)}, "talking"),
    ({"working": True}, "working"),
])
def test_what_holds_an_end_back(changes: dict[str, object], blocked: str) -> None:
    decided = decide(situation(**{"device": ended(), **changes}))
    assert decided.reasons == () and decided.blocked == blocked


@pytest.mark.parametrize("changes", [
    {"now": datetime(2026, 11, 4, 16, 0, tzinfo=UTC)},  # 01:00 KST: the game ended, the user is up
    {"unanswered_wake": True},
    {"wakes": (wake(NOW - timedelta(minutes=2)),)},  # no gap
])
def test_an_end_passes_the_hours_the_gap_and_an_unanswered_wake(changes: dict[str, object]) -> None:
    assert decide(situation(device=ended(), **changes)).reasons != ()


# --- §6 rules -----------------------------------------------------------------------------

DUE = (loop(1, "2026-11-04T14:00+09:00"),)


@pytest.mark.parametrize(("changes", "blocked"), [
    ({"device_id": None, "device": None}, "no_device"),
    ({"working": True}, "working"),
    ({"quiet_until": NOW + timedelta(minutes=5)}, "quiet"),
    ({"last_user_message_at": NOW - timedelta(minutes=9)}, "talking"),
    ({"unanswered_wake": True}, "unanswered"),
    ({"now": datetime(2026, 11, 4, 14, 30, tzinfo=UTC)}, "quiet_hours"),  # 23:30 KST
    ({"wakes": (wake(NOW - timedelta(minutes=14)),)}, "gap"),  # V3-P90 §8: 15 minutes
    ({"device": device(sleeps=True, sleep="idle")}, "asleep"),  # V3-P90 §5: waits for the wake
])
def test_what_holds_a_trigger_back(changes: dict[str, object], blocked: str) -> None:
    decided = decide(situation(loops=DUE, **changes))
    if blocked == "quiet_hours":  # the loop's day is past its window then: use one due that evening
        decided = decide(situation(loops=(loop(1, "2026-11-04T22:00+09:00"),), **changes))
    assert decided.reasons == () and decided.blocked == blocked and decided.held


# --- V3-P90: sleep, the return, told reminders -----------------------------------------------

SLEEPER = device(sleeps=True)


def test_a_wake_from_sleep_is_greeted_at_once_and_after_an_hour() -> None:
    """§5.2: no 20 seconds to wait (the app checked the wake); an hour away is enough."""
    now_back = back(minutes=61, at=NOW, via="wake")
    assert decide(situation(device=SLEEPER, returned=now_back)).reasons == (
        WakeReason(WakeKind.RETURNED, away_minutes=61, via="wake"),)
    assert decide(situation(device=SLEEPER, returned=back(minutes=59, at=NOW, via="wake"))).reasons == ()
    # held back for three minutes at most
    assert decide(situation(device=SLEEPER, returned=back(at=NOW - timedelta(minutes=4), via="wake"))).reasons == ()


@pytest.mark.parametrize("changes", [
    {"now": datetime(2026, 11, 4, 17, 0, tzinfo=UTC)},  # 02:00 KST: the user woke the computer
    {"unanswered_wake": True},  # what was left unanswered before the absence
    {"wakes": (wake(NOW - timedelta(minutes=5)),)},  # no gap
    {"wakes": tuple(wake(NOW - timedelta(hours=h)) for h in (1, 2, 3, 4))},  # no daily limit
])
def test_a_return_passes_the_rules_of_the_other_wakes(changes: dict[str, object]) -> None:
    """§5.2: only the user's quiet holds a return back."""
    returned = back(at=changes.get("now", NOW), via="wake")  # type: ignore[arg-type]
    assert decide(situation(device=SLEEPER, returned=returned, **changes)).reasons != ()
    quiet = decide(situation(device=SLEEPER, returned=returned, quiet_until=NOW + timedelta(days=1), **changes))
    assert quiet.reasons == () and quiet.blocked == "quiet"


def to_remind(memory_id: int = 7, at: datetime = NOW - timedelta(minutes=1), state: str = "scheduled") -> Memory:
    return replace(loop(memory_id, "2026-11-05"), remind_instant=at, remind_state=state)


def test_a_sleeping_device_is_woken_for_a_reminder_only() -> None:
    """§5.1: an idle sleep: the reminder is spoken (marked asleep); a screen that is off waits."""
    idle = device(sleeps=True, sleep="idle")
    decided = decide(situation(device=idle, loops=(to_remind(), *DUE)))
    assert decided.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7, asleep=True),)
    assert [c.reason.kind for c in decided.held] == [WakeKind.LOOP_DUE] and decided.blocked == "asleep"
    off = decide(situation(device=device(sleeps=True, sleep="display_off"), loops=(to_remind(),)))
    assert off.reasons == () and off.blocked == "display_off"
    awake = decide(situation(device=SLEEPER, loops=(to_remind(),)))
    assert awake.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7),)


def test_a_told_reminder_seen_at_a_return_soon_after() -> None:
    """§5.1, §5.2: back on that device within ten minutes — seen, and no greeting."""
    told = (Told(7, "d1", NOW - timedelta(minutes=8)),)
    decided = decide(situation(device=SLEEPER, returned=back(at=NOW, via="wake"),
                               loops=(to_remind(state="told"),), told=told))
    assert decided.reasons == () and decided.seen == (7,)


@pytest.mark.parametrize("told", [
    Told(7, "d1", NOW - timedelta(minutes=11)),  # came back later
    Told(7, "d2", NOW - timedelta(minutes=2)),  # told on another computer
])
def test_a_told_reminder_is_told_again_at_a_later_return(told: Told) -> None:
    decided = decide(situation(device=SLEEPER, returned=back(at=NOW, via="wake"),
                               loops=(to_remind(state="told"),), told=(told,)))
    assert decided.seen == ()
    assert decided.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7, again=True),
                               WakeReason(WakeKind.RETURNED, away_minutes=290, via="wake"))


def test_a_told_reminder_waits_for_a_return() -> None:
    told = (Told(7, "d1", NOW - timedelta(minutes=30)),)
    assert decide(situation(device=SLEEPER, loops=(to_remind(state="told"),), told=told)).reasons == ()
    # a return from before the telling is not one after it
    earlier = back(at=NOW - timedelta(minutes=1), via="wake")
    later_told = (Told(7, "d1", NOW - timedelta(seconds=30)),)
    assert decide(situation(device=SLEEPER, returned=earlier, loops=(to_remind(state="told"),),
                            told=later_told)).reasons == (WakeReason(WakeKind.RETURNED, away_minutes=290, via="wake"),)


def test_what_does_not_hold_it_back() -> None:
    """Fullscreen and the microphone are no do-not-disturb (V3-P88 owner decision); old
    messages, a quiet that ended, wakes past the gap, yesterday's wakes and tries are fine."""
    fullscreen = ActivitySignal("video", "Player", 60.0, fullscreen=True)
    yesterday = local_midnight(NOW, SEOUL) - timedelta(minutes=1)
    decided = decide(situation(loops=DUE, device=replace(device(fullscreen), mic_or_camera=True),
                               quiet_until=NOW - timedelta(minutes=1),
                               last_user_message_at=NOW - timedelta(minutes=11),
                               wakes=(wake(NOW - timedelta(minutes=31)), wake(NOW - timedelta(minutes=1), WakeKind.TRY),
                                      *(wake(yesterday - timedelta(hours=h)) for h in range(4)))))
    assert decided.reasons and decided.blocked is None


def test_reminders_do_not_count_toward_the_daily_limit_but_do_toward_the_gap() -> None:
    reminders = tuple(wake(NOW - timedelta(hours=h), WakeKind.REMINDER) for h in (1, 2, 3, 4))
    assert decide(situation(loops=DUE, wakes=reminders)).reasons
    assert decide(situation(loops=DUE, wakes=(wake(NOW - timedelta(minutes=5), WakeKind.REMINDER),))).blocked == "gap"


def test_quiet_hours_wrap_midnight_in_the_devices_zone() -> None:
    late, early = time(23, 0), time(8, 0)
    at = datetime(2026, 11, 4, 22, 30, tzinfo=UTC)  # 07:30 KST, 22:30 London
    assert in_quiet_hours(at, SEOUL, late, early)
    assert in_quiet_hours(at, "Europe/London", time(22, 0), time(7, 0))
    assert not in_quiet_hours(at, "Europe/London", late, early)
    assert not in_quiet_hours(at, SEOUL, None, None)
    assert in_quiet_hours(datetime(2026, 11, 4, 3, 0, tzinfo=UTC), SEOUL, time(9, 0), time(18, 0))  # 12:00, no wrap


def test_the_day_is_the_devices_day() -> None:
    assert local_midnight(NOW, SEOUL) == datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    assert local_midnight(NOW, "Europe/London") == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


# --- §6.4 merging -------------------------------------------------------------------------

def test_candidates_merge_into_one_wake_in_order_up_to_four() -> None:
    loops = tuple(loop(i, f"2026-11-04T{9 + i:02d}:00+09:00") for i in range(1, 5))  # all due by 16:00 KST
    decided = decide(situation(loops=loops, returned=back(), device=ended()))
    kinds = [r.kind for r in decided.reasons]
    assert kinds == [WakeKind.LOOP_DUE] * 4  # loops first, in their moments' order
    assert [r.memory_id for r in decided.reasons] == [1, 2, 3, 4]
    assert {c.reason.kind for c in decided.held} == {WakeKind.TASK_ENDED, WakeKind.RETURNED}  # the next look
    merged = decide(situation(loops=loops[:1], returned=back(), device=ended()))
    assert [r.kind for r in merged.reasons] == [WakeKind.LOOP_DUE, WakeKind.TASK_ENDED, WakeKind.RETURNED]
    assert len(merged.keys) == 3 and merged.held == ()


def test_a_wake_that_said_nothing_counts_toward_neither_the_gap_nor_the_day() -> None:
    """PR #156 review P1: a wake cut before a word (failed) or cancelled as the user won."""
    silent = tuple(replace(wake(NOW - timedelta(minutes=m)), status=s)
                   for m, s in ((1, WakeStatus.FAILED), (2, WakeStatus.CANCELLED), (3, WakeStatus.FAILED),
                                (4, WakeStatus.CANCELLED), (5, WakeStatus.FAILED)))
    assert decide(situation(loops=DUE, wakes=silent)).reasons
    opened = (replace(wake(NOW - timedelta(minutes=1)), status=WakeStatus.OPENED),)
    assert decide(situation(loops=DUE, wakes=opened)).blocked == "gap"


def test_a_failed_wake_that_left_output_counts() -> None:
    """PR #156 re-review P2-2: failed is not always silent."""
    failed = replace(wake(NOW - timedelta(minutes=5)), status=WakeStatus.FAILED)
    assert decide(situation(loops=DUE, wakes=(failed,))).reasons  # said nothing
    assert decide(situation(loops=DUE, wakes=(failed,), said=frozenset({failed.wake_id}))).blocked == "gap"


# --- §5.3 reminders -----------------------------------------------------------------------

def reminding(memory_id: int, at: datetime, state: str = "scheduled") -> Memory:
    return replace(loop(memory_id, "2026-11-10"), remind_at=at.isoformat(), remind_instant=at, remind_state=state)


def test_a_reminder_is_told_on_time_past_the_soft_rules() -> None:
    """Q8: past the user's quiet too; the talking rule, an unanswered wake, quiet hours, the gap, the day."""
    note = reminding(7, NOW - timedelta(minutes=1))
    soft = dict(quiet_until=NOW + timedelta(hours=1), last_user_message_at=NOW - timedelta(minutes=1),
                unanswered_wake=True, now=NOW, wakes=tuple(wake(NOW - timedelta(minutes=m)) for m in (5, 60, 120, 180)))
    decided = decide(situation(loops=(note,), **soft))
    assert decided.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7),) and decided.reminders == (7,)
    assert decided.keys == ()  # its delivery state keeps it from repeating, not a key
    night = decide(situation(loops=(reminding(7, datetime(2026, 11, 4, 14, 59, tzinfo=UTC)),),
                             now=datetime(2026, 11, 4, 15, 0, tzinfo=UTC)))  # 00:00 KST
    assert night.reminders == (7,)


@pytest.mark.parametrize(("changes", "blocked"), [
    ({"device_id": None, "device": None}, "no_device"),
    ({"working": True}, "working"),
])
def test_a_reminder_waits_for_a_device_and_the_end_of_work(changes: dict[str, object], blocked: str) -> None:
    decided = decide(situation(loops=(reminding(7, NOW - timedelta(minutes=1)),), **changes))
    assert decided.reasons == () and decided.blocked == blocked


def test_only_a_scheduled_reminder_whose_moment_came() -> None:
    for note in (reminding(7, NOW + timedelta(minutes=1)), reminding(7, NOW - timedelta(minutes=1), "claimed"),
                 reminding(7, NOW - timedelta(minutes=1), "delivered"), reminding(7, NOW - timedelta(minutes=1), "missed")):
        assert decide(situation(loops=(note,))).reasons == ()


def test_a_reminder_past_its_day_is_told_late_at_the_next_return() -> None:
    """Q9: within seven days, at a return after its on-time window; the return need not be long."""
    yesterday = reminding(7, NOW - timedelta(hours=30))
    assert decide(situation(loops=(yesterday,))).reasons == ()  # no return: it waits
    short = back(minutes=20)  # not a greeting (under four hours), but a return
    decided = decide(situation(loops=(yesterday,), returned=short))
    assert decided.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7, late=True),)
    # a return (fresh) that came while it could still be told on time does not count as the later one
    just_past = reminding(7, NOW - timedelta(hours=24, minutes=5))
    inside = back(at=NOW - timedelta(minutes=8), minutes=20)  # 23 h 57 min after its moment
    assert decide(situation(loops=(just_past,), returned=inside)).reasons == ()
    assert decide(situation(loops=(just_past,), returned=back(at=NOW - timedelta(minutes=4), minutes=20))).reasons
    week = reminding(7, NOW - timedelta(days=7, minutes=1))
    assert decide(situation(loops=(week,), returned=short)).reasons == ()  # past seven days: Data marks it missed


def test_a_reminder_comes_first_in_a_merged_wake() -> None:
    decided = decide(situation(loops=(*DUE, reminding(7, NOW - timedelta(minutes=1))), returned=back()))
    assert [r.kind for r in decided.reasons] == [WakeKind.REMINDER, WakeKind.LOOP_DUE, WakeKind.RETURNED]


def test_the_day_counts_only_the_old_activity_wakes_once_each() -> None:
    """PR #163 review P2-1: what the status's day counts — a merged wake once, a loop or a return never."""
    from himari.proactive import activity_wake

    assert activity_wake(wake(NOW, WakeKind.FOCUS, WakeKind.LOOP_DUE)) and activity_wake(wake(NOW, WakeKind.GAME_ENDED))
    assert not activity_wake(wake(NOW, WakeKind.RETURNED)) and not activity_wake(wake(NOW, WakeKind.TASK_ENDED))


def test_a_told_reminder_with_no_teller_is_told_again_at_the_next_return() -> None:
    """PR #163 re-review P2: its wake is gone, so no return shows it was seen."""
    told = (Told(7, None, NOW - timedelta(minutes=2)),)
    decided = decide(situation(device=SLEEPER, returned=back(at=NOW, via="wake"),
                               loops=(to_remind(state="told"),), told=told))
    assert decided.seen == () and decided.reasons[0] == WakeReason(WakeKind.REMINDER, memory_id=7, again=True)

