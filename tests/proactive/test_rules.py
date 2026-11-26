"""V3-P87 §5–§6.4: the triggers, the rules and the merging, as pure functions of a clock."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from himari.common import ConversationId, PrincipalId
from himari.data.api import (Anniversary, LifeEntry, LifeKind, LifeOrigin, Memory, MemoryCategory, MemoryOrigin,
                             WakeKind, WakeReason, WakeRecord, WakeStatus, free_wake, parse_note_date)
from himari.presence.api import ActivitySignal, DeviceState, EndedActivity, TaskEnded, TaskSignal
from himari.proactive import (
    CHECK_IN_WINDOW,
    MERGE_ORDER,
    ProactiveSettings,
    Return,
    Situation,
    candidate_keys,
    candidates,
    decide,
    free_today,
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
                            for k in kinds or (WakeKind.LOOP_DUE,)), "d1", WakeStatus.ANSWERED, at, at)


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


# --- V3-P90: sleep and the return ----------------------------------------------------------

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


@pytest.mark.parametrize("sleep", ["idle", "display_off"])
def test_a_sleeping_character_is_not_woken_for_a_reminder(sleep: str) -> None:
    """V3-P107 §2: any sleep holds a reminder back; it is told once the user is back."""
    held = decide(situation(device=device(sleeps=True, sleep=sleep), loops=(to_remind(), *DUE)))
    assert held.reasons == () and held.blocked == "asleep"
    assert {c.reason.kind for c in held.held} == {WakeKind.REMINDER, WakeKind.LOOP_DUE}
    awake = decide(situation(device=SLEEPER, loops=(to_remind(),)))
    assert awake.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7),)


def test_a_reminder_held_by_sleep_is_told_with_the_greeting() -> None:
    """V3-P107 §2: the user wakes the computer — the reminder and the return go in one wake."""
    decided = decide(situation(device=SLEEPER, returned=back(at=NOW, via="wake"), loops=(to_remind(),)))
    assert decided.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7),
                               WakeReason(WakeKind.RETURNED, away_minutes=290, via="wake"))


@pytest.mark.parametrize("state", ["told", "claimed", "delivered", "missed"])
def test_a_reminder_not_scheduled_is_never_told(state: str) -> None:
    """V3-P107 §2: ``told`` is no more (v19 makes it delivered); only a scheduled one is told."""
    decided = decide(situation(device=SLEEPER, returned=back(at=NOW, via="wake"), loops=(to_remind(state=state),)))
    assert decided.reasons == (WakeReason(WakeKind.RETURNED, away_minutes=290, via="wake"),)


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


def test_a_wake_that_said_nothing_does_not_count_toward_the_gap() -> None:
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


# --- V3-P90 §6.2, §7: the free wake ------------------------------------------------------------

def working(minutes: float, group: str = "work") -> DeviceState:
    return replace(device(), task=TaskSignal(group, minutes * 60))


def free_at(minutes_ago: float, status: WakeStatus = WakeStatus.ANSWERED) -> WakeRecord:
    at = NOW - timedelta(minutes=minutes_ago)
    return WakeRecord(f"wf{minutes_ago}", PrincipalId("operator"), ConversationId("c"), 1,
                      (WakeReason(WakeKind.FREE, group="work", minutes=1, step=1),), "d1", status, at, at)


def test_a_free_wake_comes_thirty_minutes_after_the_last_word_at_the_computer() -> None:
    """character-work §7.2 (K5): the user at the computer (awake, input within ten minutes) and thirty minutes
    since the last wake or their last message, whatever they are doing; the task goes with it when there is one."""
    said = {"last_user_message_at": NOW - timedelta(minutes=29), "free_ok": True}
    assert decide(situation(**said)).reasons == ()
    decided = decide(situation(**{**said, "last_user_message_at": NOW - timedelta(minutes=31)}))
    assert decided.reasons == (WakeReason(WakeKind.FREE, step=1),)
    assert decided.keys == ("free:d1:2026-11-04T06:29Z",)
    tasked = decide(situation(device=working(5), free_ok=True))  # a task of five minutes is no matter now
    assert tasked.reasons == (WakeReason(WakeKind.FREE, group="work", minutes=5, step=1),)
    assert tasked.keys == ("free:d1:start",)  # no wake and no message read: the wait started long ago
    assert decide(situation(device=working(31))).reasons == ()  # the model was not checked for it
    assert decide(situation(free_ok=True, settings=replace(ON, free_minutes=0))).reasons == ()
    for away in (replace(device(), idle_seconds=11 * 60.0), replace(device(), idle_seconds=None),
                 replace(device(), locked=True), replace(device(), connected=False),
                 device(sleeps=True, sleep="idle")):
        assert decide(situation(device=away, free_ok=True)).reasons == (), away  # not at the computer


def test_one_interval_from_the_latest_wake() -> None:
    """V3-P107 §4: FREE_MINUTES each time, no growing steps; a pass counts; any wake restarts the wait.
    character-work §7.2: the step counts the free wakes since the user's last message."""
    one = free_at(80, WakeStatus.PASSED)
    assert decide(situation(wakes=(one,), free_ok=True)).reasons[0].step == 2  # 80 ≥ 30
    two = free_at(29)
    assert decide(situation(wakes=(one, two), free_ok=True)).reasons == ()  # 29 < 30
    later = situation(now=NOW + timedelta(minutes=1), wakes=(one, two), free_ok=True)
    assert decide(later).reasons[0].step == 3  # 30 again, not 60
    other = (one, replace(wake(NOW - timedelta(minutes=20), WakeKind.LOOP_DUE), wake_id="loop"))
    assert decide(situation(wakes=other, free_ok=True)).reasons == ()  # waits 30 from that loop
    after = situation(wakes=(one,), free_ok=True, last_user_message_at=NOW - timedelta(minutes=50))
    assert decide(after).reasons[0].step == 1  # the user spoke since that one


def test_each_wait_opens_once() -> None:
    """Its key names the moment the wait started: taken, it is not opened again until another wake or message."""
    first = decide(situation(free_ok=True, last_user_message_at=NOW - timedelta(minutes=40)))
    again = decide(situation(now=NOW + timedelta(minutes=5), free_ok=True,
                             last_user_message_at=NOW - timedelta(minutes=40)), frozenset(first.keys))
    assert first.reasons and again.reasons == ()
    later = decide(situation(now=NOW + timedelta(minutes=40), free_ok=True, wakes=(free_at(0),),
                             last_user_message_at=NOW - timedelta(minutes=40)), frozenset(first.keys))
    assert later.reasons[0].step == 2 and later.keys != first.keys


def test_a_free_wake_is_held_by_every_rule_and_its_day() -> None:
    for changes, blocked in (({"now": datetime(2026, 11, 4, 14, 30, tzinfo=UTC)}, "quiet_hours"),  # 23:30 KST
                             ({"unanswered_wake": True}, "unanswered"),
                             ({"working": True}, "working"),
                             ({"quiet_until": NOW + timedelta(hours=1)}, "quiet")):
        decided = decide(situation(**{"free_ok": True, **changes}))
        assert decided.reasons == () and decided.blocked == blocked, changes
    # V3-P107 §4: earlier today: ten opened — spoken or passed alike (each cost a request)
    spoken = tuple(replace(free_at(40 + 20 * i), wake_id=f"s{i}") for i in range(10))
    assert decide(situation(wakes=spoken, free_ok=True)).blocked == "daily"
    assert decide(situation(wakes=spoken[1:], free_ok=True)).reasons != ()
    passes = tuple(replace(free_at(40 + 20 * i, WakeStatus.PASSED), wake_id=f"p{i}") for i in range(10))
    assert decide(situation(wakes=passes, free_ok=True)).blocked == "daily"
    yesterday = tuple(replace(w, created_at=local_midnight(NOW, SEOUL) - timedelta(minutes=1 + i))
                      for i, w in enumerate(spoken))
    assert decide(situation(wakes=yesterday, free_ok=True)).reasons != ()


# --- character-work §7.3 (K6): an unanswered wake -------------------------------------------------

def test_one_unanswered_wake_holds_himaris_openings_two_hours() -> None:
    told = {"unanswered_wake": True, "last_user_message_at": NOW - timedelta(hours=5)}
    for ago, blocked in ((60, "unanswered"), (119, "unanswered"), (121, None)):
        decided = decide(situation(loops=DUE, wakes=(wake(NOW - timedelta(minutes=ago), WakeKind.RETURNED),),
                                   **told))
        assert decided.blocked == blocked and bool(decided.reasons) == (blocked is None), ago
    passed = replace(free_at(30, WakeStatus.PASSED), wake_id="passed")  # never shown: not unanswered
    assert decide(situation(loops=DUE, wakes=(wake(NOW - timedelta(hours=3)), passed), **told)).reasons


def test_two_unanswered_wakes_hold_them_until_the_user_speaks() -> None:
    two = (replace(wake(NOW - timedelta(hours=4)), wake_id="a"), replace(wake(NOW - timedelta(hours=3)), wake_id="b"))
    held = decide(situation(loops=DUE, wakes=two, unanswered_wake=True,
                            last_user_message_at=NOW - timedelta(hours=5)))
    assert held.reasons == () and held.blocked == "unanswered"
    answered = decide(situation(loops=DUE, wakes=two, unanswered_wake=True,
                                last_user_message_at=NOW - timedelta(minutes=210)))  # between the two
    assert answered.reasons  # one since the user spoke, three hours ago
    unread = decide(situation(loops=DUE, unanswered_wake=True))  # none read: as before, until the user speaks
    assert unread.blocked == "unanswered"
    # what just happened and a reminder are not held (V3-P107 §3)
    back = Return("d1", NOW - timedelta(minutes=1), "unlock", 120)
    assert decide(situation(returned=back, wakes=two, unanswered_wake=True)).reasons


# --- character-work §7.1: a piece of work just finished --------------------------------------------

def piece(entry_id: int, minutes_ago: float) -> LifeEntry:
    at = NOW - timedelta(minutes=minutes_ago)
    return LifeEntry(entry_id, 0, LifeKind.WORK, "2026-11-04", "조각", LifeOrigin.WRITER, 1, at, at,
                     title="냉각부", summary="냉각부 설계 끝냈어.", arc_id=1, plan_id=2)


def test_a_finished_piece_is_spoken_of_once_within_three_hours_and_may_pass() -> None:
    made = situation(work=(piece(9, 60),))
    decided = decide(made)
    assert decided.reasons == (WakeReason(WakeKind.WORK_DONE, entry_id=9),) and decided.keys == ("work:9",)
    assert free_wake(decided.reasons)  # hidden until it speaks, and it may pass
    assert decide(made, frozenset(decided.keys)).reasons == ()  # once
    assert decide(situation(work=(piece(9, 181),))).reasons == ()  # past three hours
    assert decide(situation(work=(piece(9, 60),), device_id=None, device=None)).reasons == ()
    assert decide(situation(work=(replace(piece(9, 60), draft=True),))).reasons == ()  # character-mind §4.4: a draft


def test_a_finished_piece_waits_as_himaris_own_openings_do_and_counts_in_the_day() -> None:
    work = {"work": (piece(9, 30),)}
    for changes, blocked in (({"unanswered_wake": True}, "unanswered"),
                             ({"last_user_message_at": NOW - timedelta(minutes=9)}, "talking"),
                             ({"wakes": (wake(NOW - timedelta(minutes=5)),)}, "gap")):
        decided = decide(situation(**{**work, **changes}))
        assert decided.reasons == () and decided.blocked == blocked, changes
    night = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)  # 23:30 KST, a piece made half an hour before
    late = situation(now=night, work=(replace(piece(9, 0), created_at=night - timedelta(minutes=30)),))
    assert decide(late).blocked == "quiet_hours"
    spoken = tuple(replace(free_at(40 + 20 * i), wake_id=f"s{i}") for i in range(10))
    assert decide(situation(**work, wakes=spoken)).blocked == "daily"
    finished = replace(free_at(40), reasons=(WakeReason(WakeKind.WORK_DONE, entry_id=9),))
    assert free_today(situation(wakes=(finished,))) == 1


def test_a_finished_piece_goes_right_after_an_anniversary_and_before_a_loop() -> None:
    assert MERGE_ORDER.index(WakeKind.WORK_DONE) == MERGE_ORDER.index(WakeKind.ANNIVERSARY) + 1
    assert MERGE_ORDER.index(WakeKind.WORK_DONE) + 1 == MERGE_ORDER.index(WakeKind.LOOP_DUE)
    merged = decide(situation(work=(piece(9, 30),), loops=DUE, anniversaries=(HUNDRED,), bond_day="2026-11-04"))
    assert [r.kind for r in merged.reasons] == [WakeKind.ANNIVERSARY, WakeKind.WORK_DONE, WakeKind.LOOP_DUE]
    alone = decide(situation(work=(piece(9, 30),), free_ok=True))
    assert [r.kind for r in alone.reasons] == [WakeKind.WORK_DONE]  # a free wake waits behind it
    assert [c.reason.kind for c in alone.held] == [WakeKind.FREE]

def test_a_free_wake_goes_alone() -> None:
    """V3-P90 §7.3: never merged — another wake due goes first, the free one waits."""
    decided = decide(situation(device=working(31), loops=DUE, free_ok=True))
    assert [r.kind for r in decided.reasons] == [WakeKind.LOOP_DUE]
    assert [c.reason.kind for c in decided.held] == [WakeKind.FREE]


# --- character-bond §6: anniversaries ---------------------------------------------------------

HUNDRED = Anniversary("100 days together", days=100)
BIRTHDAY = Anniversary("사용자 생일", entry_id=7)


def test_an_anniversary_is_spoken_of_once_that_day_and_may_pass() -> None:
    days = situation(anniversaries=(HUNDRED, BIRTHDAY), bond_day="2026-11-04")
    decided = decide(days)
    assert decided.reasons == (WakeReason(WakeKind.ANNIVERSARY, days=100),
                               WakeReason(WakeKind.ANNIVERSARY, entry_id=7))
    assert decided.keys == ("anniversary:2026-11-04:d100", "anniversary:2026-11-04:b7")
    assert free_wake(decided.reasons)  # its own path: hidden until it speaks, and it may pass
    assert decide(days, frozenset(decided.keys)).reasons == ()  # once
    assert decide(situation(anniversaries=(HUNDRED,))).reasons == ()  # no day: none
    assert decide(situation(anniversaries=(HUNDRED,), bond_day="2026-11-04", device_id=None, device=None)).reasons == ()


def test_an_anniversary_waits_as_himaris_own_openings_do_and_counts_in_the_day() -> None:
    days = {"anniversaries": (HUNDRED,), "bond_day": "2026-11-04"}
    for changes, blocked in (({"now": datetime(2026, 11, 4, 14, 30, tzinfo=UTC)}, "quiet_hours"),  # 23:30 KST
                             ({"unanswered_wake": True}, "unanswered"),
                             ({"last_user_message_at": NOW - timedelta(minutes=9)}, "talking"),
                             ({"device": device(sleeps=True, sleep="idle")}, "asleep"),
                             ({"wakes": (wake(NOW - timedelta(minutes=5)),)}, "gap")):
        decided = decide(situation(**{**days, **changes}))
        assert decided.reasons == () and decided.blocked == blocked, changes
    spoken = tuple(replace(free_at(40 + 20 * i), wake_id=f"s{i}") for i in range(10))
    assert decide(situation(**days, wakes=spoken)).blocked == "daily"  # inside the day's count (B5)
    marked = replace(free_at(40), reasons=(WakeReason(WakeKind.ANNIVERSARY, days=100),))
    assert free_today(situation(wakes=(marked,))) == 1  # and counted in it


def test_an_anniversary_goes_before_a_free_wake_and_right_after_a_reminder() -> None:
    days = {"anniversaries": (HUNDRED,), "bond_day": "2026-11-04"}
    decided = decide(situation(**days, device=working(31), free_ok=True))
    assert [r.kind for r in decided.reasons] == [WakeKind.ANNIVERSARY]
    assert [c.reason.kind for c in decided.held] == [WakeKind.FREE]
    # character-day §9: a time set together comes between the two
    assert MERGE_ORDER.index(WakeKind.ANNIVERSARY) == MERGE_ORDER.index(WakeKind.TOGETHER) + 1
    assert MERGE_ORDER.index(WakeKind.TOGETHER) == MERGE_ORDER.index(WakeKind.REMINDER) + 1
    merged = decide(situation(**days, loops=DUE))
    assert [r.kind for r in merged.reasons] == [WakeKind.ANNIVERSARY, WakeKind.LOOP_DUE]
    assert not free_wake(merged.reasons)  # with a loop it is an ordinary wake


# --- character-day v0.1 §9, §18.9: HIMARI's own sleep and a time set together ----------------

def promise(entry_id: int, start: datetime) -> tuple[LifeEntry, datetime]:
    entry = LifeEntry(entry_id, 1, LifeKind.PLAN, "2026-11-04", "방송 같이 보기", LifeOrigin.HIMARI, 1, NOW, NOW,
                      time_of_day="16:00", minutes=120)
    return entry, start


def test_himaris_sleep_holds_every_wake_but_a_reminder_which_it_speaks_half_asleep() -> None:
    days = {"anniversaries": (HUNDRED,), "bond_day": "2026-11-04", "free_ok": True}
    held = decide(situation(**days, loops=DUE, himari_asleep=True))
    assert held.reasons == () and held.blocked == "himari_asleep"
    note = reminding(7, NOW - timedelta(minutes=1))
    told = decide(situation(loops=(note, *DUE), himari_asleep=True))
    assert told.reasons == (WakeReason(WakeKind.REMINDER, memory_id=7),) and told.blocked == "himari_asleep"
    assert decide(situation(loops=DUE, himari_asleep=False)).reasons  # awake: the usual rules
    # its life on, the quiet hours no longer stand in for its sleep: awake at 03:00, it may speak
    hours = replace(ON, quiet_start=time(23, 0), quiet_end=time(8, 0))
    night = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)  # 03:00 in Seoul
    late = (loop(1, "2026-11-04T01:00+09:00"),)
    assert decide(situation(settings=hours, now=night, loops=late, himari_asleep=False)).reasons
    assert decide(situation(settings=hours, now=night, loops=late)).blocked == "quiet_hours"  # its life off


def test_a_together_slot_opens_its_wake_once_within_ten_minutes_of_its_start() -> None:
    start = NOW - timedelta(minutes=3)
    decided = decide(situation(together=(promise(9, start),)))
    assert decided.reasons == (WakeReason(WakeKind.TOGETHER, entry_id=9),) and decided.keys == ("together:9",)
    assert decide(situation(together=(promise(9, start),)), frozenset(decided.keys)).reasons == ()  # once
    assert decide(situation(together=(promise(9, NOW + timedelta(minutes=1)),))).reasons == ()  # not yet
    assert decide(situation(together=(promise(9, NOW - timedelta(minutes=10)),))).reasons == ()  # too late


def test_a_together_wake_passes_the_gap_the_day_and_an_unanswered_wake_but_not_the_users_quiet() -> None:
    start = NOW - timedelta(minutes=1)
    soft = dict(unanswered_wake=True, free_ok=True, wakes=tuple(wake(NOW - timedelta(minutes=m), WakeKind.FREE)
                                                                for m in (5, 60, 120, 180)))
    assert decide(situation(together=(promise(9, start),), **soft)).reasons == (
        WakeReason(WakeKind.TOGETHER, entry_id=9),)
    for changes, blocked in (({"quiet_until": NOW + timedelta(hours=1)}, "quiet"),
                             ({"last_user_message_at": NOW - timedelta(seconds=30)}, "talking"),
                             ({"device_id": None, "device": None}, "no_device"),
                             ({"working": True}, "working"),
                             ({"device": device(sleeps=True, sleep="idle")}, "asleep"),
                             ({"himari_asleep": True}, "himari_asleep")):
        decided = decide(situation(together=(promise(9, start),), **changes))
        assert decided.reasons == () and decided.blocked == blocked, changes
