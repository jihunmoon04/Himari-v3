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
    ended = asyncio.run(data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.ANSWERED, asked=True))
    assert ended.status is WakeStatus.ANSWERED and ended.asked and ended.ended_at is not None
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


def test_a_purge_takes_its_wakes() -> None:
    store = new_store()
    data, _ = opened(store)
    asyncio.run(data.begin_wake(SCOPE, TRY, "d1", keys=("k",)))
    asyncio.run(data.trash_conversation(SCOPE))
    asyncio.run(data.begin_purge(SCOPE))
    asyncio.run(data.purge_conversation(SCOPE))
    assert asyncio.run(data.opened_wakes()) == ()
