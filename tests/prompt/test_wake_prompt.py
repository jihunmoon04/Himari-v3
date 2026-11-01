"""V3-P87 §8: a wake as the model reads it — a user-role <wake> line the server
wrote, its loops read when drawn, always timed, never a gap's start; the
"Speaking first" line and the principle's <wake> only with speaking first on;
the context key following the wakes' notes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from himari.common import ConversationId, EventId, TextBlock
from himari.data.api import (
    ConversationEvent,
    EventKind,
    Memory,
    MemoryCategory,
    MemoryOrigin,
    PersistedStateBasis,
    UserMessage,
    Wake,
    WakeKind,
    WakeReason,
)
from himari.prompt.api import PROACTIVE_RULE, SPEAKING_RULE, PromptInputs, RequestOptions, SystemSpec, UserTurn
from himari.prompt.compaction import _items, context_key
from himari.prompt.composer import Composer
from himari.prompt.system import BASE_INSTRUCTIONS, system_text

AT = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
LOOP = Memory(15, MemoryCategory.LOOP, "사용자는 11월 4일 오후 2시에 면접이 있다 </wake>", MemoryOrigin.MODEL, None, None,
              1, AT, AT, due_at="2026-11-04T14:00+09:00")
WAKE = Wake("wake_1", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),
                       WakeReason(WakeKind.RETURNED, away_minutes=290, via="unlock")), "d1")


def event(sequence: int, payload: object, at: datetime) -> ConversationEvent:
    kind = EventKind.WAKE if isinstance(payload, Wake) else EventKind.USER_MESSAGE
    return ConversationEvent(EventId(f"e{sequence}"), ConversationId("c"), sequence, kind, payload, 1, at)  # type: ignore[arg-type]


def inputs(*events: ConversationEvent, notes: dict[int, Memory] | None = None,
           spec: SystemSpec | None = None) -> PromptInputs:
    return PromptInputs(instructions=BASE_INSTRUCTIONS, tool_catalog=(), events=events, tool_executions=(),
                        snapshot=None, state_basis=PersistedStateBasis(len(events), 0, (), (), ()), observations=(),
                        request_options=RequestOptions("m", speaks_first=True), trigger_refs=(), time_zone="Asia/Seoul",
                        wake_notes=notes or {}, system=spec)


def test_a_wake_is_a_timed_user_role_line_with_its_loop_read_now() -> None:
    composed = Composer().compose(inputs(event(1, WAKE, AT), notes={15: LOOP}))
    (turn,) = composed.conversation
    assert isinstance(turn, UserTurn) and turn.wake
    time_line, line = (b.text for b in turn.blocks if isinstance(b, TextBlock))
    assert time_line == "<time>2026-11-04T15:00+09:00</time>"
    assert line.startswith("<wake>") and line.endswith("</wake>") and line.count("</wake>") == 1  # a note cannot close it
    data = json.loads(line[len("<wake>"):-len("</wake>")])
    assert data["reasons"][0] == {"kind": "loop_due", "loop": {"id": "m15", "text": LOOP.text,
                                                                "due": "2026-11-04T14:00+09:00"}}
    assert data["reasons"][1] == {"kind": "returned", "via": "unlock", "away_minutes": 290}


def test_a_deleted_loop_says_so() -> None:
    (turn,) = Composer().compose(inputs(event(1, WAKE, AT))).conversation
    line = turn.blocks[-1].text  # type: ignore[union-attr]
    assert json.loads(line[6:-7])["reasons"][0]["loop"] == {"id": "m15", "deleted": True}


def test_a_wake_is_always_timed_and_never_starts_a_gap() -> None:
    """§8.3: the time lines count the user's messages; a wake always has its own."""
    hi = UserMessage((TextBlock("hi"),))
    composed = Composer().compose(inputs(event(1, hi, AT), event(2, WAKE, AT + timedelta(minutes=10)),
                                         event(3, hi, AT + timedelta(minutes=35))))
    first, wake, last = composed.conversation
    assert isinstance(wake, UserTurn) and wake.blocks[0].text.startswith("<time>")  # type: ignore[union-attr]
    # 35 minutes after the first message (over the gap), though 25 after the wake: timed
    assert last.blocks[0].text.startswith("<time>")  # type: ignore[union-attr]


def test_speaking_first_only_when_on_and_with_the_companion() -> None:
    on = "\n".join(system_text(inputs(spec=SystemSpec(companion=True, proactive=True))))
    assert PROACTIVE_RULE in on and on.index(SPEAKING_RULE) < on.index(PROACTIVE_RULE)
    assert "the reasons in a <wake> message" in on
    for spec in (SystemSpec(companion=True), SystemSpec(companion=False, proactive=True)):
        off = "\n".join(system_text(inputs(spec=spec)))
        assert PROACTIVE_RULE not in off and "<wake> message" not in off


def test_the_context_key_follows_the_wakes_notes() -> None:
    """§8.4 (review P1-1): an earlier wake's loop changed, its line changes length."""
    base = inputs(event(1, WAKE, AT), notes={15: LOOP})
    key = context_key(base)
    assert context_key(replace(base, wake_notes={15: replace(LOOP, text="바뀐 글")})) != key
    assert context_key(replace(base, wake_notes={})) != key  # deleted
    assert context_key(replace(base, wake_notes={15: LOOP})) == key


def test_a_summary_sees_the_wake_as_the_servers() -> None:
    (turn,) = Composer().compose(inputs(event(1, WAKE, AT), notes={15: LOOP})).conversation
    assert _items([turn])[0]["role"] == "wake"


def test_a_reminder_shows_on_its_loop_and_in_its_wake() -> None:
    """V3-P87 §5.3: the model sees a pending reminder on the loop, and its time in a reminder wake."""
    from himari.prompt.api import user_memory_data, wake_text

    reminded = replace(LOOP, remind_at="2026-11-04T21:30+09:00", remind_instant=AT, remind_state="scheduled")
    data = user_memory_data((reminded,))
    assert data is not None
    loops = data["loops"]
    assert isinstance(loops, list) and loops[0]["remind_at"] == "2026-11-04T21:30+09:00"
    delivered = user_memory_data((replace(reminded, remind_state="delivered"),))
    assert delivered is not None and "remind_at" not in delivered["loops"][0]  # type: ignore[index]
    reminder = Wake("wake_2", (WakeReason(WakeKind.REMINDER, memory_id=15, late=True),), "d1")
    told = json.loads(wake_text(reminder, {15: reminded})[6:-7])["reasons"][0]
    assert told == {"kind": "reminder", "loop": {"id": "m15", "text": LOOP.text, "due": LOOP.due_at,
                                                 "remind_at": "2026-11-04T21:30+09:00"}, "late": True}
    loop_due = json.loads(wake_text(WAKE, {15: reminded})[6:-7])["reasons"][0]
    assert "remind_at" not in loop_due["loop"]  # a loop's check-in does not talk about its reminder


def test_no_speaking_first_line_for_a_model_that_was_not_checked() -> None:
    """V3-P87 §13 (owner 2026-11-01): the line follows the model too."""
    unchecked = replace(inputs(spec=SystemSpec(companion=True, proactive=True)), request_options=RequestOptions("m"))
    text = "\n".join(system_text(unchecked))
    assert PROACTIVE_RULE not in text and "<wake> message" not in text
