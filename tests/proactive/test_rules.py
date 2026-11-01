"""V3-P87 §5–§6.4: the triggers, the rules and the merging, as pure functions of a clock."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from himari.common import ConversationId, PrincipalId
from himari.data.api import Memory, MemoryCategory, MemoryOrigin, WakeKind, WakeReason, WakeRecord, WakeStatus, parse_note_date
from himari.presence.api import ActivitySignal, DeviceState, EndedActivity
from himari.proactive import (
    CHECK_IN_WINDOW,
    ProactiveSettings,
    Return,
    Situation,
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


def device(activity: ActivitySignal | None = None, before: EndedActivity | None = None) -> DeviceState:
    return DeviceState("d1", True, 0.0, SEOUL, activity=activity, before=before)


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
    (back(minutes=239), {}),  # under four hours
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


# --- §5.4 activity ------------------------------------------------------------------------

def coding(minutes: float, category: str = "coding") -> ActivitySignal:
    return ActivitySignal(category, "Code", minutes * 60)


def test_a_long_focus_once_per_stretch() -> None:
    assert decide(situation(device=device(coding(29)))).reasons == ()
    first = decide(situation(device=device(coding(31))))
    assert first.reasons == (WakeReason(WakeKind.FOCUS, activity="coding", minutes=31),)
    # the same stretch a little later, its start counted back across a minute: still taken
    later = situation(now=NOW + timedelta(minutes=20, seconds=40), device=device(coding(51 + 40 / 60 + 0.01)))
    assert decide(later, frozenset(first.keys)).reasons == ()
    found, expired, _ = candidates(later)
    assert set(first.keys) & set(candidate_keys(found, expired))
    # a new stretch after a break
    again = situation(now=NOW + timedelta(hours=2), device=device(coding(31)))
    assert decide(again, frozenset(first.keys)).reasons != ()


@pytest.mark.parametrize("category", ["document", "design", "video"])
def test_focus_counts_documents_designs_and_videos(category: str) -> None:
    assert decide(situation(device=device(coding(30, category)))).reasons[0].activity == category


@pytest.mark.parametrize("category", ["game", "chat", "browsing", "music"])
def test_other_activities_are_no_focus(category: str) -> None:
    assert decide(situation(device=device(coding(90, category)))).reasons == ()


def test_a_game_that_just_ended() -> None:
    ended = EndedActivity("game", "Game", 95 * 60, 120)
    assert decide(situation(device=device(before=ended))).reasons == (
        WakeReason(WakeKind.GAME_ENDED, minutes=95),)
    for before in (replace(ended, lasted_seconds=29 * 60), replace(ended, ended_seconds_ago=11 * 60),
                   replace(ended, category="video")):
        assert decide(situation(device=device(before=before))).reasons == ()
    assert decide(situation(device=device(before=ended), settings=replace(ON, game_minutes=0))).reasons == ()


def test_no_signals_no_activity_trigger() -> None:
    assert decide(situation(device=DeviceState("d1", True, 0.0, None))).reasons == ()


# --- §6 rules -----------------------------------------------------------------------------

DUE = (loop(1, "2026-11-04T14:00+09:00"),)


@pytest.mark.parametrize(("changes", "blocked"), [
    ({"device_id": None, "device": None}, "no_device"),
    ({"working": True}, "working"),
    ({"quiet_until": NOW + timedelta(minutes=5)}, "quiet"),
    ({"last_user_message_at": NOW - timedelta(minutes=9)}, "talking"),
    ({"unanswered_wake": True}, "unanswered"),
    ({"now": datetime(2026, 11, 4, 14, 30, tzinfo=UTC)}, "quiet_hours"),  # 23:30 KST
    ({"wakes": (wake(NOW - timedelta(minutes=29)),)}, "gap"),
    ({"wakes": tuple(wake(NOW - timedelta(hours=h)) for h in (1, 2, 3, 4))}, "daily"),
])
def test_what_holds_a_trigger_back(changes: dict[str, object], blocked: str) -> None:
    decided = decide(situation(loops=DUE, **changes))
    if blocked == "quiet_hours":  # the loop's day is past its window then: use one due that evening
        decided = decide(situation(loops=(loop(1, "2026-11-04T22:00+09:00"),), **changes))
    assert decided.reasons == () and decided.blocked == blocked and decided.held


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
    decided = decide(situation(loops=loops, returned=back(), device=device(coding(40))))
    kinds = [r.kind for r in decided.reasons]
    assert kinds == [WakeKind.LOOP_DUE] * 4  # loops first, in their moments' order
    assert [r.memory_id for r in decided.reasons] == [1, 2, 3, 4]
    assert {c.reason.kind for c in decided.held} == {WakeKind.FOCUS, WakeKind.RETURNED}  # the next look
    merged = decide(situation(loops=loops[:1], returned=back(), device=device(coding(40))))
    assert [r.kind for r in merged.reasons] == [WakeKind.LOOP_DUE, WakeKind.FOCUS, WakeKind.RETURNED]
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
