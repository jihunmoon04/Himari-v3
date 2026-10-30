"""V3-P83 §6.2 (re-review C1): which message a turn answers, as the Driver reads it."""

from __future__ import annotations

from datetime import UTC, datetime

from testkit import text_result

from himari.common import CallId, CallRef, ConversationId, EventId, RunId, TextBlock, ToolCall, ToolId
from himari.control.driver import sent_from
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    EventKind,
    EventPayload,
    MessageOrigin,
    OriginKind,
    ToolResultRecord,
    UserMessage,
)

RUN = RunId("r1")
DESK = MessageOrigin(OriginKind.DEVICE, "d1")
WEB = MessageOrigin(OriginKind.WEB)
KINDS = {UserMessage: EventKind.USER_MESSAGE, AssistantFinal: EventKind.ASSISTANT_FINAL,
         AssistantToolCalls: EventKind.ASSISTANT_TOOL_CALLS, ToolResultRecord: EventKind.TOOL_RESULT}


def events(*payloads: EventPayload) -> list[ConversationEvent]:
    return [ConversationEvent(EventId(f"e{n}"), ConversationId("c"), n, KINDS[type(p)], p, 1,
                              datetime(2026, 10, 30, tzinfo=UTC), RUN if not isinstance(p, UserMessage) else None)
            for n, p in enumerate(payloads, start=1)]


def said(origin: MessageOrigin | None, text: str = "hi") -> UserMessage:
    return UserMessage((TextBlock(text),), origin=origin)


ASKED = AssistantToolCalls((ToolCall(CallId("q"), 0, ToolId("ask_user"), {"question": "?"}),))
ANSWERED = ToolResultRecord(CallRef(RUN, CallId("q")), text_result("yes"))


def test_the_last_message_no_answer_follows() -> None:
    assert sent_from(events(said(WEB), AssistantFinal((TextBlock("a"),)), said(DESK))) == DESK


def test_an_answer_to_a_question_counts_though_its_result_follows() -> None:
    answer = UserMessage((TextBlock("yes"),), answers_call=CallRef(RUN, CallId("q")), origin=DESK)
    assert sent_from(events(said(WEB), ASKED, answer, ANSWERED)) == DESK


def test_a_resume_after_an_answer_has_none() -> None:
    assert sent_from(events(said(DESK), AssistantFinal((TextBlock("a"),)))) is None
    assert sent_from(events(said(DESK), ASKED)) is None
    assert sent_from([]) is None


def test_a_message_from_before_origins_has_none() -> None:
    assert sent_from(events(said(None))) is None


def test_order_is_by_sequence() -> None:
    found = events(said(WEB), AssistantFinal((TextBlock("a"),)), said(DESK))
    assert sent_from(list(reversed(found))) == DESK
