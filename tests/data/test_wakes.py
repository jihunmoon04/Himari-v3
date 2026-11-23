"""V3-P87 §3.3, §5.3: wakes in Data — one transaction for the Event, its record,
its keys and the reminders it claims; ending once; reminders settled only by
the wake that still claims them."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.bootstrap import Store
from himari.data.api import (
    REMINDER_ATTEMPTS,
    DataError,
    DataErrorCode,
    EventKind,
    MemoryCategory,
    MessageOrigin,
    OriginKind,
    UserMessage,
    Wake,
    WakeKind,
    WakeReason,
    WakeStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore

TRY = (WakeReason(WakeKind.TRY),)


def make_reminder(store: object, memory_id: int, state: str = "scheduled", wake: str | None = None,
                  attempts: int = 0) -> None:
    """Part 2 writes reminders through memory_save; here the delivery state is set directly."""
    if isinstance(store, SqliteStore):
        store.connection.execute("UPDATE memories SET remind_at='2026-11-02T09:00+09:00', remind_state=?,"
                                 " remind_wake=?, remind_attempts=? WHERE memory_id=?",
                                 (state, wake, attempts, memory_id))
        store.connection.commit()
    else:
        assert isinstance(store, InMemoryStore)
        store.reminders[(SCOPE.principal_id, memory_id)] = (state, wake, attempts)


def reminder(store: object, memory_id: int) -> tuple[str | None, str | None, int]:
    if isinstance(store, SqliteStore):
        row = store.connection.execute("SELECT remind_state, remind_wake, remind_attempts FROM memories"
                                       " WHERE memory_id=?", (memory_id,)).fetchone()
        return row[0], row[1], row[2]
    assert isinstance(store, InMemoryStore)
    return store.reminders.get((SCOPE.principal_id, memory_id), (None, None, 0))


def opened(store: Store) -> tuple[InMemoryData | SqliteData, int]:
    data = data_for(store)
    store.create_conversation(SCOPE)
    note = asyncio.run(data.save_memory(SCOPE.principal_id, MemoryCategory.LOOP, "약 먹기"))
    return data, note.memory_id


def test_a_wake_is_an_event_with_its_record() -> None:
    store = new_store()
    data, _ = opened(store)
    event = asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("try:1",)))
    assert event.kind is EventKind.WAKE and isinstance(event.payload, Wake)
    assert event.payload.reasons == TRY and event.payload.device_id == "d1"
    record = asyncio.run(data.read_wake(SCOPE.principal_id, event.payload.wake_id))
    assert (record.status, record.event_sequence, record.conversation_id) == (WakeStatus.OPENED, 1, SCOPE.conversation_id)
    assert [r.wake_id for r in asyncio.run(data.opened_wakes())] == [record.wake_id]
    again = data_for(restart(store))
    (stored,) = asyncio.run(again.read_events(SCOPE))
    assert stored.payload == event.payload  # read back as written


def test_a_taken_key_refuses_the_whole_wake() -> None:
    store = new_store()
    data, _ = opened(store)
    asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("loop:m1:2026-11-04",)))
    with pytest.raises(DataError) as refused:
        asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("other", "loop:m1:2026-11-04")))
    assert refused.value.code is DataErrorCode.DUPLICATE_IDENTITY
    assert len(asyncio.run(data.read_events(SCOPE))) == 1 and len(asyncio.run(data.opened_wakes())) == 1
    asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("other",)))  # "other" was not kept by the refused one


def test_a_wake_needs_reasons_and_a_device() -> None:
    store = new_store()
    data, _ = opened(store)
    for reasons, device in (((), "d1"), (TRY * 5, "d1"), (TRY, "")):
        with pytest.raises(DataError) as refused:
            asyncio.run(data.begin_wake(SCOPE, reasons, device))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT


def test_a_message_never_comes_from_a_wake() -> None:
    store = new_store()
    data, _ = opened(store)
    with pytest.raises(DataError):
        asyncio.run(data.append_event(SCOPE, UserMessage(user("hi").blocks, origin=MessageOrigin(OriginKind.WAKE, "d1"))))


def test_a_wake_ends_once() -> None:
    store = new_store()
    data, _ = opened(store)
    wake_id = asyncio.run(data.begin_wake(SCOPE, TRY, "d1")).payload.wake_id  # type: ignore[union-attr]
    ended = asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.ANSWERED))
    assert ended.status is WakeStatus.ANSWERED and ended.ended_at is not None
    later = asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.FAILED))
    assert later.status is WakeStatus.ANSWERED  # once
    assert asyncio.run(data.opened_wakes()) == ()
    with pytest.raises(DataError):
        asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.OPENED))


def test_a_reminder_is_claimed_then_delivered_or_given_back() -> None:
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id)
    reason = (WakeReason(WakeKind.REMINDER, memory_id=memory_id),)
    first = asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    assert reminder(store, memory_id) == ("claimed", first, 0)
    with pytest.raises(DataError) as refused:  # one wake at a time carries it
        asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,)))
    assert refused.value.code is DataErrorCode.INVALID_STATE_TRANSITION
    asyncio.run(data.end_wake(SCOPE.principal_id, first, WakeStatus.CANCELLED))  # the user won: not delivered
    assert reminder(store, memory_id) == ("scheduled", None, 1)
    second = asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    asyncio.run(data.end_wake(SCOPE.principal_id, second, WakeStatus.ANSWERED))
    assert reminder(store, memory_id)[:2] == ("delivered", None)


def test_a_reminder_given_back_too_often_is_missed() -> None:
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id, attempts=REMINDER_ATTEMPTS - 1)
    wake_id = asyncio.run(data.begin_wake(SCOPE, (WakeReason(WakeKind.REMINDER, memory_id=memory_id),), "d1",
                                          reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.FAILED))
    assert reminder(store, memory_id)[0] == "missed"


def test_only_the_claiming_wake_settles_a_reminder() -> None:
    """Re-review P2: changed meanwhile (scheduled again, by the user), the old wake's end leaves it alone."""
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id)
    wake_id = asyncio.run(data.begin_wake(SCOPE, (WakeReason(WakeKind.REMINDER, memory_id=memory_id),), "d1",
                                          reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    make_reminder(store, memory_id)  # the user moved its time: scheduled, claimed by none
    asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.ANSWERED))
    assert reminder(store, memory_id) == ("scheduled", None, 0)



# --- V3-P107 §2: no reminder is told to a sleeping device any more ------------------------------

def test_a_reminder_left_told_is_not_told_again() -> None:
    """``told`` is no more (v19 makes it delivered): only a scheduled reminder is claimed or missed."""
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id, state="told")
    reason = (WakeReason(WakeKind.REMINDER, memory_id=memory_id),)
    with pytest.raises(DataError) as refused:
        asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,)))
    assert refused.value.code is DataErrorCode.INVALID_STATE_TRANSITION
    from datetime import UTC, datetime

    assert asyncio.run(data.miss_reminders(SCOPE.principal_id, datetime(2100, 1, 1, tzinfo=UTC))) == 0
    assert reminder(store, memory_id)[0] == "told"


def test_a_failed_reminder_goes_back_to_scheduled_even_after_an_earlier_delivery() -> None:
    """V3-P107 §2: no told state to fall back to — a wake that fails gives its reminder back."""
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id)
    reason = (WakeReason(WakeKind.REMINDER, memory_id=memory_id),)
    first = asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    asyncio.run(data.end_wake(SCOPE.principal_id, first, WakeStatus.ANSWERED))
    make_reminder(store, memory_id)  # the user set it again
    second = asyncio.run(data.begin_wake(SCOPE, reason, "d1", reminders=(memory_id,))).payload.wake_id  # type: ignore[union-attr]
    asyncio.run(data.end_wake(SCOPE.principal_id, second, WakeStatus.FAILED))
    assert reminder(store, memory_id) == ("scheduled", None, 1)


def test_a_reason_stored_with_the_old_flags_reads_without_them() -> None:
    """V3-P107 §2: V3-P90 stored ``asleep`` and ``again`` on reminder reasons; they are read past."""
    from himari.data import codec

    stored = [{"kind": "reminder", "memory_id": 7, "asleep": True, "again": True, "late": True}]
    assert codec.wake_reasons_from(stored) == (WakeReason(WakeKind.REMINDER, memory_id=7, late=True),)
    assert codec.wake_reasons((WakeReason(WakeKind.REMINDER, memory_id=7),)) == [{"kind": "reminder",
                                                                                   "memory_id": 7}]

def test_a_purge_takes_its_wakes() -> None:
    store = new_store()
    data, _ = opened(store)
    asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("k",)))
    asyncio.run(data.trash_conversation(SCOPE))
    asyncio.run(data.begin_purge(SCOPE))
    asyncio.run(data.purge_conversation(SCOPE))
    assert asyncio.run(data.opened_wakes()) == ()


def test_keys_and_reminders_are_checked_alike_in_both_backends() -> None:
    """PR #153 review P2-2: a repeated key, or reminders that are not exactly the
    notes of the reminder reasons, are refused before anything is written."""
    store = new_store()
    data, memory_id = opened(store)
    make_reminder(store, memory_id)
    reminded = (WakeReason(WakeKind.REMINDER, memory_id=memory_id),)
    for reasons, keys, reminders in (
        (TRY, ("k", "k"), ()),  # the same key twice
        (TRY, ("",), ()),  # an empty key
        (TRY, (), (memory_id,)),  # a reminder without its reason
        (reminded, (), ()),  # a reminder reason that claims nothing
        (reminded, (), (memory_id, memory_id)),  # claimed twice
        (reminded, (), (memory_id + 1,)),  # another note
        (reminded * 2, (), (memory_id,)),  # the same note's reason twice
        ((WakeReason(WakeKind.REMINDER),), (), ()),  # a reminder reason with no note
    ):
        with pytest.raises(DataError) as refused:
            asyncio.run(data.begin_wake(SCOPE, reasons, "d1", keys=keys, reminders=reminders))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT, (reasons, keys, reminders)
    assert len(asyncio.run(data.read_events(SCOPE))) == 0  # nothing written
    assert reminder(store, memory_id) == ("scheduled", None, 0)
    asyncio.run(data.begin_wake(SCOPE, reminded, "d1", keys=("k",), reminders=(memory_id,)))  # the right shape
    assert reminder(store, memory_id)[0] == "claimed"


def test_part_two_reads_alike_in_both_backends() -> None:
    """V3-P87 part 2: the recent wakes, the keys taken or expired, the latest user
    message and each device's last input (never moved back)."""
    from datetime import UTC, datetime, timedelta

    store = new_store()
    data, _ = opened(store)
    before = datetime.now(UTC) - timedelta(seconds=1)
    assert asyncio.run(data.last_user_message_at(SCOPE.principal_id)) is None
    first = asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("a",))).payload.wake_id  # type: ignore[union-attr]
    assert [w.wake_id for w in asyncio.run(data.wakes_since(SCOPE.principal_id, before))] == [first]
    assert asyncio.run(data.wakes_since(SCOPE.principal_id, datetime.now(UTC) + timedelta(seconds=1))) == ()
    asyncio.run(data.expire_keys(SCOPE.principal_id, ("b", "a")))
    assert asyncio.run(data.taken_keys(SCOPE.principal_id, ("a", "b", "c"))) == frozenset({"a", "b"})
    assert asyncio.run(data.taken_keys(SCOPE.principal_id, ())) == frozenset()
    with pytest.raises(DataError):  # an expired key is taken
        asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("b",)))
    asyncio.run(data.end_wake(SCOPE.principal_id, first, WakeStatus.ANSWERED))
    asyncio.run(data.append_event(SCOPE, user("hi")))
    said = asyncio.run(data.last_user_message_at(SCOPE.principal_id))
    assert said is not None and said >= before
    device = asyncio.run(data.add_device(SCOPE.principal_id, "PC", b"p" * 32, datetime.now(UTC) + timedelta(hours=1)))
    at = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    asyncio.run(data.record_input(SCOPE.principal_id, device.device_id, at))
    asyncio.run(data.record_input(SCOPE.principal_id, device.device_id, at - timedelta(hours=1)))
    asyncio.run(data.record_input(SCOPE.principal_id, "nobody", at))
    assert asyncio.run(data.read_inputs(SCOPE.principal_id)) == {device.device_id: at}
    asyncio.run(data.revoke_device(SCOPE.principal_id, device.device_id))
    assert asyncio.run(data.read_inputs(SCOPE.principal_id)) == {}





# --- V3-P90 §7.3: a free wake's states --------------------------------------------------------

FREE = (WakeReason(WakeKind.FREE, group="work", minutes=16, step=1),)


def free_run(data: InMemoryData | SqliteData, event: object) -> object:
    from himari.common import RawRepresentation
    from himari.data.api import EventRef, PromptBasis, RawReceived, RunPreparation

    async def go() -> object:
        run_id = await data.prepare_run(SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED,
                                                              (EventRef(event.event_id),), PromptBasis(0, 0), "p", "m"))  # type: ignore[attr-defined]
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
        return run_id
    return asyncio.run(go())


def test_a_free_wake_is_hidden_until_shown_and_gone_once_passed() -> None:
    store = new_store()
    data, _ = opened(store)
    event = asyncio.run(data.begin_wake(SCOPE, FREE, "d1", keys=("free:d1:x:1",)))
    wake_id = event.payload.wake_id  # type: ignore[union-attr]
    assert asyncio.run(data.unshown_wakes(SCOPE)) == frozenset({event.sequence})
    run_id = free_run(data, event)
    assert asyncio.run(data.wake_of_run(SCOPE, run_id)).wake_id == wake_id  # type: ignore[arg-type,union-attr]
    asyncio.run(data.finalize_hidden_run(SCOPE, run_id, wake_id, WakeStatus.PASSED))  # type: ignore[arg-type]
    assert asyncio.run(data.read_wake(SCOPE.principal_id, wake_id)).status is WakeStatus.PASSED
    assert asyncio.run(data.read_run(SCOPE, run_id)).status.value == "projected"  # type: ignore[arg-type]
    assert asyncio.run(data.read_events(SCOPE)) == ()  # it never was
    assert asyncio.run(data.unshown_wakes(SCOPE)) == frozenset()
    assert asyncio.run(data.wake_of_run(SCOPE, run_id)) is not None  # type: ignore[arg-type]  # still found by its Run
    assert asyncio.run(data.opened_wakes()) == ()
    again = data_for(restart(store))
    assert asyncio.run(again.read_events(SCOPE)) == ()


@pytest.mark.parametrize("end", ["passed", "dropped", "spoke"])
def test_a_free_wakes_screen_group_is_unshown_and_goes_with_it(end: str) -> None:
    """V3-P91 §8: the Run a free wake holds (it looked at the screen) — its call and result — is unshown
    while the wake is, never was once it passed or was dropped, and is seen once it speaks."""
    from himari.common import CallId, CallRef, ModelToolResult, TextBlock, ToolCall, ToolId
    from himari.data.api import AssistantToolCalls, ToolCompletion, ToolExecutionStatus

    store = new_store()
    data, _ = opened(store)
    event = asyncio.run(data.begin_wake(SCOPE, FREE, "d1"))
    wake_id = event.payload.wake_id  # type: ignore[union-attr]
    run_id = free_run(data, event)
    other = free_run(data, event)
    assert asyncio.run(data.hold_wake_run(SCOPE, wake_id, run_id))  # type: ignore[arg-type]
    assert asyncio.run(data.hold_wake_run(SCOPE, wake_id, run_id))  # type: ignore[arg-type]  # held already
    assert not asyncio.run(data.hold_wake_run(SCOPE, wake_id, other))  # type: ignore[arg-type]  # one Run only
    call = ToolCall(CallId("c0"), 0, ToolId("look_at_screen"), {})
    asyncio.run(data.project_model_output(SCOPE, run_id, AssistantToolCalls((call,))))  # type: ignore[arg-type]
    asyncio.run(data.commit_tool_outcome(SCOPE, CallRef(run_id, call.call_id), ToolCompletion(  # type: ignore[arg-type]
        ToolExecutionStatus.SUCCEEDED, ModelToolResult((TextBlock("Screen captured."),)))))
    held = [e.sequence for e in asyncio.run(data.read_events(SCOPE)) if e.origin_run_id == run_id]
    assert len(held) == 2  # the call and its result
    assert asyncio.run(data.unshown_wakes(SCOPE)) == frozenset({event.sequence, *held})
    assert asyncio.run(data.read_wake(SCOPE.principal_id, wake_id)).held_run_id == run_id
    if end == "spoke":
        assert asyncio.run(data.reveal_wake(SCOPE.principal_id, wake_id))
        assert len(asyncio.run(data.read_events(SCOPE))) == 3 and asyncio.run(data.unshown_wakes(SCOPE)) == frozenset()
        assert not asyncio.run(data.hold_wake_run(SCOPE, wake_id, run_id))  # type: ignore[arg-type]  # not opened
        return
    if end == "passed":
        asyncio.run(data.finalize_hidden_run(SCOPE, other, wake_id, WakeStatus.PASSED))  # type: ignore[arg-type]
    else:
        asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.DROPPED))
    assert asyncio.run(data.read_events(SCOPE)) == () and asyncio.run(data.unshown_wakes(SCOPE)) == frozenset()
    assert asyncio.run(data_for(restart(store)).read_events(SCOPE)) == ()


def test_a_shown_free_wake_is_speaking_and_ends_as_any_wake() -> None:
    store = new_store()
    data, _ = opened(store)
    wake_id = asyncio.run(data.begin_wake(SCOPE, FREE, "d1")).payload.wake_id  # type: ignore[union-attr]
    assert asyncio.run(data.reveal_wake(SCOPE.principal_id, wake_id))
    assert not asyncio.run(data.reveal_wake(SCOPE.principal_id, wake_id))  # once
    assert [w.status for w in asyncio.run(data.opened_wakes())] == [WakeStatus.SPEAKING]  # still in progress
    assert len(asyncio.run(data.read_events(SCOPE))) == 1 and asyncio.run(data.unshown_wakes(SCOPE)) == frozenset()
    kept = asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.DROPPED))
    assert kept.status is WakeStatus.SPEAKING  # a shown wake never becomes hidden
    assert asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.ANSWERED)).status is WakeStatus.ANSWERED
    with pytest.raises(DataError):
        asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.SPEAKING))


def test_a_free_wake_dropped_before_it_spoke_is_gone_and_cannot_be_shown() -> None:
    store = new_store()
    data, _ = opened(store)
    wake_id = asyncio.run(data.begin_wake(SCOPE, FREE, "d1")).payload.wake_id  # type: ignore[union-attr]
    assert asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.DROPPED)).status is WakeStatus.DROPPED
    assert not asyncio.run(data.reveal_wake(SCOPE.principal_id, wake_id))
    assert asyncio.run(data.read_events(SCOPE)) == ()


def test_a_runs_wake_is_its_own_before_an_earlier_wakes_group() -> None:
    """PR #167 re-review P1-7: wake A's tool group left among wake B's first Run's triggers — that Run
    is B's; B's Run after its own tool round is B's; A's later Run is A's. Both backends."""
    from himari.common import RawRepresentation
    from himari.data.api import CompletedGroupRef, EventRef, PromptBasis, RunPreparation

    store = new_store()
    data, _ = opened(store)

    def run(*refs: object) -> object:
        async def go() -> object:
            return await data.prepare_run(SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED,
                                                                tuple(refs), PromptBasis(0, 0), "p", "m"))  # type: ignore[arg-type]
        return asyncio.run(go())

    a = asyncio.run(data.begin_wake(SCOPE, FREE, "d1"))
    a_run = run(EventRef(a.event_id))
    asyncio.run(data.end_wake(SCOPE.principal_id, a.payload.wake_id, WakeStatus.FAILED))  # type: ignore[union-attr]
    b = asyncio.run(data.begin_wake(SCOPE, FREE, "d1"))
    b_first = run(CompletedGroupRef(a_run), EventRef(b.event_id))  # type: ignore[arg-type]
    b_after_tools = run(CompletedGroupRef(b_first))  # type: ignore[arg-type]
    a_after_tools = run(CompletedGroupRef(a_run))  # type: ignore[arg-type]
    owner = {name: asyncio.run(data.wake_of_run(SCOPE, r)).wake_id  # type: ignore[arg-type,union-attr]
             for name, r in (("b_first", b_first), ("b_after_tools", b_after_tools), ("a_after_tools", a_after_tools))}
    assert owner == {"b_first": b.payload.wake_id, "b_after_tools": b.payload.wake_id,  # type: ignore[union-attr]
                     "a_after_tools": a.payload.wake_id}  # type: ignore[union-attr]


def test_a_users_run_beside_an_older_free_wakes_group_is_no_wakes() -> None:
    """PR #167 re-review P1-8: a Run that names the user's message is the user's, though an older free
    wake's tool group is among its triggers. Both backends."""
    from himari.common import RawRepresentation
    from himari.data.api import CompletedGroupRef, EventRef, PromptBasis, RunPreparation

    store = new_store()
    data, _ = opened(store)

    def run(*refs: object) -> object:
        async def go() -> object:
            return await data.prepare_run(SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED,
                                                                tuple(refs), PromptBasis(0, 0), "p", "m"))  # type: ignore[arg-type]
        return asyncio.run(go())

    a = asyncio.run(data.begin_wake(SCOPE, FREE, "d1"))
    a_run = run(EventRef(a.event_id))
    asyncio.run(data.end_wake(SCOPE.principal_id, a.payload.wake_id, WakeStatus.FAILED))  # type: ignore[union-attr]
    message = asyncio.run(data.append_event(SCOPE, user("[[pass]]가 뭐야?")))
    users = run(CompletedGroupRef(a_run), EventRef(message.event_id))  # type: ignore[arg-type]
    assert asyncio.run(data.wake_of_run(SCOPE, users)) is None  # type: ignore[arg-type]
    after_tools = run(CompletedGroupRef(users))  # type: ignore[arg-type]
    assert asyncio.run(data.wake_of_run(SCOPE, after_tools)) is None  # type: ignore[arg-type]  # still the user's
