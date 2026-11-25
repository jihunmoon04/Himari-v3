"""What a memory pass reads and may write (V3-P85 §4.2–§4.4, §4.9, §5.3, §6.2): pure."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from himari.common import AttachmentRef, CallId, ConversationId, EventId, TextBlock, ToolCall, ToolId
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    BondChange,
    BondTag,
    ConversationEvent,
    Episode,
    EventKind,
    LifeChange,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    Memory,
    MemoryCategory,
    MemoryOrigin,
    PassAction,
    PassKind,
    UserMessage,
)
from himari.memory.passes import (
    ASSISTANT_CHARS,
    BOND_INSTRUCTIONS,
    MEMORY_PASS_INSTRUCTIONS,
    PASS_BOND_CHARS,
    PASS_MAX_BOND,
    PASS_FACT_CHARS,
    PASS_INPUT_CHARS,
    PASS_MAX_NOTES,
    PASS_MAX_SELF,
    PASS_MEMORY_CHARS,
    SELF_INSTRUCTIONS,
    AnswerError,
    Part,
    model_input,
    next_part,
    parse_answer,
    parse_date,
    plan_bond,
    plan_episode,
    plan_items,
    plan_self,
    plan_thoughts,
    THOUGHT_INSTRUCTIONS,
    PASS_MAX_THOUGHTS,
    request_data,
    reservation,
    shown_bond,
    shown_facts,
    shown_notes,
    waits,
)
from himari.prompt.api import RequestOptions

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
CONV = ConversationId("conv_1")
SEOUL = "Asia/Seoul"


def event(sequence: int, payload: object, at: datetime = NOW) -> ConversationEvent:
    kind = EventKind.USER_MESSAGE if isinstance(payload, UserMessage) else EventKind.ASSISTANT_FINAL
    return ConversationEvent(EventId(f"e{sequence}"), CONV, sequence, kind, payload, 1, at)  # type: ignore[arg-type]


def said(sequence: int, text: str, at: datetime = NOW) -> ConversationEvent:
    return event(sequence, UserMessage((TextBlock(text),)), at)


def answered(sequence: int, text: str, at: datetime = NOW) -> ConversationEvent:
    return event(sequence, AssistantFinal((TextBlock(text),)), at)


def note(memory_id: int, text: str, category: MemoryCategory = MemoryCategory.USER,
         origin: MemoryOrigin = MemoryOrigin.EXTRACTOR, revision: int = 1, **fields: object) -> Memory:
    return Memory(memory_id, category, text, origin, CONV, None, revision, NOW, NOW, **fields)  # type: ignore[arg-type]


PART = Part(0, 4, tuple(next_part([said(1, "I build HIMARI and my interview is on 2026-11-04."),
                                   answered(2, "Good luck!"), said(3, "Please remember I take blood pressure pills."),
                                   answered(4, "Noted.")], 0).messages), NOW)  # type: ignore[union-attr]


def answer(*notes: Mapping[str, object], episode: object = None) -> dict[str, object]:
    return {"notes": list(notes), "episode": episode}


def test_a_part_keeps_user_messages_whole_cuts_answers_and_long_middles_and_leaves_tool_events_out() -> None:
    long = "a" * 20_000
    events = [said(1, "hi " + long), answered(2, "<b>" + "x" * 2000 + "</b>[[mood:happy]]"),
              event(3, UserMessage((TextBlock("see"), AttachmentRef("f1", 1), AttachmentRef("f2", 1, screen=True))))]
    part = next_part(events, 0)
    assert part is not None and (part.from_sequence, part.through_sequence) == (0, 3)
    first, second, third = part.messages
    assert first.omitted == len("hi " + long) - 16_000 and "chars omitted]" in first.text and len(first.text) < 16_100
    assert second.role == "himari" and len(second.text) == ASSISTANT_CHARS and "[[mood" not in second.text
    assert third.text == "see\n[attachment]\n[screen]"
    assert part.omitted == first.omitted
    assert next_part(events, 3) is None  # nothing said after the cursor
    assert next_part([answered(4, "alone")], 3) is None


def test_a_long_conversation_is_cut_at_turn_boundaries_and_the_rest_read_next() -> None:
    events = [said(1, "a" * 15_000), answered(2, "ok"), said(3, "b" * 15_000), answered(4, "ok"), said(5, "c")]
    first = next_part(events, 0)
    assert first is not None and (first.from_sequence, first.through_sequence) == (0, 2)
    assert sum(len(m.text) for m in first.messages) <= PASS_INPUT_CHARS
    second = next_part(events, first.through_sequence)
    assert second is not None and (second.from_sequence, second.through_sequence) == (2, 5)


def test_a_short_part_waits_unless_a_loop_is_open_or_it_stayed_quiet() -> None:
    part = next_part([said(1, "합격했어", NOW)], 0)
    assert part is not None
    assert waits(part, open_loops=False, now=NOW + timedelta(minutes=10))
    assert not waits(part, open_loops=True, now=NOW + timedelta(minutes=10))  # "I passed" may close a loop
    assert not waits(part, open_loops=False, now=NOW + timedelta(hours=24))
    assert not waits(next_part([said(1, "x" * 20)], 0), open_loops=False, now=NOW)  # type: ignore[arg-type]


def test_the_notes_shown_put_the_core_and_loops_first_within_the_limit() -> None:
    old = [note(n, "p" * 3000, MemoryCategory.PROJECT) for n in range(1, 6)]
    core = [note(10, "Lives in Seoul."), note(11, "Interview.", MemoryCategory.LOOP)]
    shown = shown_notes([*old, *core])
    assert shown[:2] == core and sum(len(n.text) for n in shown) <= PASS_MEMORY_CHARS
    assert len(shown) == 2 + (PASS_MEMORY_CHARS - 25) // 3000


def test_the_request_is_fixed_instructions_and_one_turn_of_escaped_data() -> None:
    previous = Episode(4, CONV, 0, 2, NOW, NOW, "Talked.", (1,), (), MemoryOrigin.EXTRACTOR, None, 1, NOW, NOW)
    data = request_data(PART, [note(3, "<b>Lives</b> in Seoul.", due_at=None)], previous, now=NOW, zone=SEOUL,
                        kind=PassKind.LIVE)
    assert data["now"] == "2026-10-31T21:00+09:00" and data["past_range"] is False
    assert data["previous_episode"] == {"id": "e4", "text": "Talked.", "ended": "2026-10-31T21:00+09:00"}
    messages = data["messages"]
    assert isinstance(messages, list) and [m["role"] for m in messages] == ["user", "himari", "user", "himari"]
    asked = model_input(data, RequestOptions("m", max_output_tokens=4000))
    assert asked.stable_prefix == (MEMORY_PASS_INSTRUCTIONS,) and asked.tool_catalog == ()
    (turn,) = asked.conversation
    text = turn.blocks[0].text  # type: ignore[union-attr]
    assert "<b>" not in text and "\\u003cb\\u003e" in text  # cannot pass as markup
    past = request_data(PART, [], previous, now=NOW, zone=SEOUL, kind=PassKind.RETRY)
    assert past["past_range"] is True and past["previous_episode"] is None  # a past part never extends
    assert reservation(b"x" * 3000) == 1500 + 4000


def test_an_answer_must_be_the_json_document_asked_for() -> None:
    assert parse_answer('```json\n{"notes": [], "episode": null}\n```') == {"notes": [], "episode": None}
    for bad in ("not json", '{"episode": null}', '{"notes": [{"op": "keep"}]}',
                '{"notes": [], "episode": {"op": "add"}}', '{"notes": [{"op": "add", "category": "secret"}]}'):
        with pytest.raises(AnswerError):
            parse_answer(bad)


def test_dates_become_instants_in_the_users_zone_and_need_an_offset() -> None:
    assert parse_date("2026-11-04", SEOUL) == ("2026-11-04", datetime(2026, 11, 4, 14, 59, 59, 999999, tzinfo=UTC))
    assert parse_date("2026-11-04T14:00+09:00", SEOUL) == ("2026-11-04T14:00+09:00",
                                                         datetime(2026, 11, 4, 5, 0, tzinfo=UTC))
    for bad in ("2026-11-04T14:00", "next tuesday", "", None, 3):
        assert parse_date(bad, SEOUL) is None


def test_notes_rest_on_user_messages_of_the_part_and_name_only_shown_notes() -> None:
    shown = [note(5, "Builds HIMARI.", MemoryCategory.PROJECT), note(6, "Mine.", origin=MemoryOrigin.USER),
             note(7, "Kept.", user_approved_at=NOW)]
    items = plan_items(answer(
        {"op": "add", "category": "project", "text": "Builds HIMARI.", "evidence": [1]},
        {"op": "add", "category": "user", "text": "From the answer.", "evidence": [2]},
        {"op": "add", "category": "user", "text": "No evidence."},
        {"op": "update", "id": "m5", "category": "project", "text": "Builds HIMARI v3.", "evidence": [1]},
        {"op": "update", "id": "m6", "text": "Not mine to change.", "evidence": [1]},
        {"op": "forget", "id": "m7", "evidence": [1]},
        {"op": "forget", "id": "m99", "evidence": [1]},
        {"op": "add", "category": "loop", "text": "Interview.", "evidence": [1], "due": "2026-11-04",
         "valid_until": "2026-12-01"},
    ), PART, shown, kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert [(i.action, i.reason) for i in items] == [
        (PassAction.ADD, None), (PassAction.DROP, "bad_evidence"), (PassAction.DROP, "no_evidence"),
        (PassAction.UPDATE, None), (PassAction.DROP, "user_owned"), (PassAction.DROP, "user_owned"),
        (PassAction.DROP, "unknown_note"), (PassAction.ADD, None)]
    update, loop = items[3], items[7]
    assert (update.memory_id, update.expected_revision, update.text) == (5, 1, "Builds HIMARI v3.")
    assert (loop.due_at, loop.valid_until) == ("2026-11-04", None)  # a loop has no end, only a date
    assert json.loads(items[1].proposal)["text"] == "From the answer."


def test_dates_out_of_range_drop_the_note_and_at_most_eight_decisions_are_read() -> None:
    items = plan_items(answer(
        {"op": "add", "category": "loop", "text": "Long ago.", "evidence": [1], "due": "2026-01-01"},
        {"op": "add", "category": "loop", "text": "Far ahead.", "evidence": [1], "due": "2030-01-01"},
        {"op": "add", "category": "user", "text": "Briefly.", "evidence": [1], "valid_until": "2028-01-01"},
        *({"op": "add", "category": "user", "text": f"N{n}.", "evidence": [1]} for n in range(10)),
    ), PART, [], kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert len(items) == PASS_MAX_NOTES
    assert [i.reason for i in items[:3]] == ["past_due", "bad_date", "bad_date"]


def test_a_sensitive_note_needs_a_remember_request_and_yields_to_a_refusal() -> None:
    part = next_part([said(1, "Please remember I take blood pressure pills."), said(2, "I went to the 병원 today."),
                      said(3, "이 병력은 기억해 두지 마. 병원에 다녀왔어.")], 0)
    assert part is not None
    items = plan_items(answer(
        {"op": "add", "category": "user", "text": "Takes blood pressure pills.", "evidence": [1], "sensitive": True},
        {"op": "add", "category": "user", "text": "Went to the 병원.", "evidence": [2], "sensitive": False},
        {"op": "add", "category": "user", "text": "병원에 다녀왔다.", "evidence": [3]},
    ), part, [], kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert [(i.action, i.reason) for i in items] == [
        (PassAction.ADD, None),  # asked to remember
        (PassAction.PROPOSE, "sensitive_proposed"),  # a sensitive word, whatever the model marked
        (PassAction.DROP, "sensitive_refused")]  # "기억해 두지 마" refuses, though it holds "기억해"


def test_a_past_part_only_proposes_and_never_changes_or_forgets() -> None:
    shown = [note(5, "Lives in Osaka.")]
    items = plan_items(answer(
        {"op": "add", "category": "user", "text": "Lives in Tokyo.", "evidence": [1]},
        {"op": "update", "id": "m5", "text": "Lives in Tokyo.", "evidence": [1]},
        {"op": "forget", "id": "m5", "evidence": [1]},
    ), PART, shown, kind=PassKind.BACKFILL, now=NOW, zone=SEOUL)
    assert [(i.action, i.reason) for i in items] == [
        (PassAction.PROPOSE, "proposed"), (PassAction.DROP, "past_range"), (PassAction.DROP, "past_range")]


def test_an_episode_needs_a_user_message_of_the_part_and_extends_only_a_live_previous_one() -> None:
    previous = Episode(4, CONV, 0, 0, NOW, NOW, "Earlier.", (1,), (), MemoryOrigin.EXTRACTOR, None, 3, NOW, NOW)
    good = {"op": "extend", "text": "Talked.", "user_evidence": [1, 3], "himari_context": [2]}
    plan = plan_episode(answer(episode=good), PART, previous, kind=PassKind.LIVE)
    assert plan is not None and (plan.extend, plan.expected_revision, plan.user_sequences,
                                 plan.himari_sequences) == (4, 3, (1, 3), (2,))
    past = plan_episode(answer(episode=good), PART, previous, kind=PassKind.RETRY)
    assert past is not None and past.extend is None
    for bad in ({**good, "user_evidence": []}, {**good, "user_evidence": [2]}, {**good, "himari_context": [1]}):
        assert plan_episode(answer(episode=bad), PART, previous, kind=PassKind.LIVE) is None
    assert plan_episode(answer(), PART, previous, kind=PassKind.LIVE) is None


def test_a_date_at_the_calendars_edge_or_not_a_date_drops_the_note() -> None:
    # PR #135 review P1 (9999-12-31 overflowed) and P2-2 (an unreadable date made a loop without one).
    assert parse_date("9999-12-31", SEOUL) is None and parse_date("0001-01-01T00:00+09:00", SEOUL) is None
    items = plan_items(answer(
        {"op": "add", "category": "loop", "text": "Far.", "evidence": [1], "due": "9999-12-31"},
        {"op": "add", "category": "loop", "text": "Interview.", "evidence": [1], "due": "next Friday"},
        {"op": "add", "category": "user", "text": "Away.", "evidence": [1], "valid_until": "soon"},
        {"op": "add", "category": "loop", "text": "Some day.", "evidence": [1], "due": None},
        {"op": "add", "category": "user", "text": "A fact.", "evidence": [1], "due": "next Friday"},
    ), PART, [], kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert [(i.action, i.reason) for i in items] == [
        (PassAction.DROP, "bad_date"), (PassAction.DROP, "bad_date"), (PassAction.DROP, "bad_date"),
        (PassAction.ADD, None), (PassAction.ADD, None)]  # no date given; a fact's due is not its field
    assert (items[3].due_at, items[4].due_at) == (None, None)


def test_an_update_keeps_the_dates_it_leaves_out_and_clears_one_set_to_null() -> None:
    # PR #135 review P2-1: rewording an open loop kept its date only by chance.
    overdue = NOW - timedelta(days=2)
    shown = [note(5, "Interview.", MemoryCategory.LOOP, due_at="2026-10-29", due_instant=overdue),
             note(6, "Away.", valid_until="2026-11-07", valid_instant=NOW + timedelta(days=7))]
    items = plan_items(answer(
        {"op": "update", "id": "m5", "text": "Interview at Kakao.", "evidence": [1]},
        {"op": "update", "id": "m6", "text": "Away in Busan.", "evidence": [1]},
        {"op": "update", "id": "m5", "text": "Interview, no date.", "evidence": [1], "due": None},
        {"op": "update", "id": "m6", "category": "project", "text": "Works from Busan.", "evidence": [1]},
    ), PART, shown, kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert [(i.action, i.due_at, i.due_instant, i.valid_until) for i in items] == [
        (PassAction.UPDATE, "2026-10-29", overdue, None),  # kept, though past: only given dates are ranged
        (PassAction.UPDATE, None, None, "2026-11-07"),
        (PassAction.UPDATE, None, None, None),
        (PassAction.UPDATE, None, None, "2026-11-07")]  # a project may still have an end


def test_a_wake_is_context_never_the_users_words() -> None:
    """V3-P87 §8.3: why HIMARI spoke first is shown, but it is not evidence, starts no part and counts no chars."""
    from himari.data.api import Wake, WakeKind, WakeReason

    wake = ConversationEvent(EventId("e2"), CONV, 2, EventKind.WAKE,
                             Wake("wake_1", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),
                                             WakeReason(WakeKind.FOCUS, activity="coding", minutes=40)), "d1"), 1, NOW)
    assert next_part([wake], 0) is None  # a wake alone is nothing to read
    part = next_part([said(1, "면접 준비해야 해"), wake, answered(3, "면접 준비는 잘 돼가?")], 0)
    assert part is not None
    roles = [(m.role, m.text) for m in part.messages]
    assert roles[1] == ("context", "[HIMARI spoke first: loop due m15, focus coding]")
    assert part.user_chars == len("면접 준비해야 해")


def test_a_pass_does_not_forget_a_loop_whose_reminder_waits() -> None:
    """V3-P87 (PR #157 re-review P2-2): dropped here, and refused by Data as well."""
    waiting = note(5, "Take the medicine.", MemoryCategory.LOOP, remind_state="scheduled")
    told = note(6, "Call mom.", MemoryCategory.LOOP, remind_state="delivered")
    items = plan_items(answer({"op": "forget", "id": "m5", "evidence": [1]},
                              {"op": "forget", "id": "m6", "evidence": [1]}),
                       PART, [waiting, told], kind=PassKind.LIVE, now=NOW, zone=SEOUL)
    assert [(i.action, i.reason) for i in items] == [(PassAction.DROP, "reminder_pending"), (PassAction.FORGET, None)]


# --- HIMARI's own facts (character-life v0.1 §4) ---------------------------------------


def fact(entry_id: int, text: str, origin: LifeOrigin = LifeOrigin.CONVERSATION, revision: int = 1) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.FACT, None, text, origin, revision, NOW, NOW)


def test_with_himaris_life_the_pass_is_shown_its_facts_and_told_to_keep_what_it_says_of_itself() -> None:
    facts = [fact(3, "고양이를 무서워한다.", LifeOrigin.USER), fact(5, "바다를 좋아한다.")]
    data = request_data(PART, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE, facts=facts)
    assert data["himari_facts"] == [{"id": "f3", "text": "고양이를 무서워한다.", "written_by": "user"},
                                    {"id": "f5", "text": "바다를 좋아한다.", "written_by": "conversation"}]
    assert "himari_facts" not in request_data(PART, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE)
    assert model_input(data, RequestOptions("m"), life=True).stable_prefix == (  # character-mind §5: and thoughts
        MEMORY_PASS_INSTRUCTIONS + "\n" + SELF_INSTRUCTIONS + "\n" + THOUGHT_INSTRUCTIONS,)
    assert model_input(data, RequestOptions("m"), life=True, bond=True).stable_prefix == (  # character-bond §3
        MEMORY_PASS_INSTRUCTIONS + "\n" + SELF_INSTRUCTIONS + "\n" + THOUGHT_INSTRUCTIONS + "\n" + BOND_INSTRUCTIONS,)
    assert model_input(data, RequestOptions("m")).stable_prefix == (MEMORY_PASS_INSTRUCTIONS,)  # life off: as before
    long = [fact(i, "x" * 500) for i in range(1, 20)]
    assert len(shown_facts([fact(1, "y", LifeOrigin.USER), *long])) == 1 + (PASS_FACT_CHARS - 1) // 500


def test_self_decisions_rest_on_himaris_replies_and_leave_the_users_facts_alone() -> None:
    facts = [fact(3, "고양이를 무서워한다.", LifeOrigin.USER), fact(5, "바다를 좋아한다.", revision=4)]
    raw = parse_answer(json.dumps({"notes": [], "self": [
        {"op": "add", "text": " 수채화를 배운다. ", "himari_evidence": [2]},
        {"op": "add", "text": "from the user", "himari_evidence": [1]},  # a user message is no ground
        {"op": "update", "id": "f3", "text": "user's", "himari_evidence": [2]},  # the user's fact
        {"op": "update", "id": "f5", "text": "산을 더 좋아한다.", "himari_evidence": [4]},
        {"op": "retire", "id": "5", "himari_evidence": [2, 4]},
    ]}, ensure_ascii=False))
    assert plan_self(raw, PART, facts) == [
        LifeChange(LifeChangeOp.ADD, "수채화를 배운다.", sequences=(2,)),
        LifeChange(LifeChangeOp.UPDATE, "산을 더 좋아한다.", 5, 4, (4,)),
        LifeChange(LifeChangeOp.RETIRE, None, 5, 4, (2, 4))]
    dropped = parse_answer(json.dumps({"notes": [], "self": [
        {"op": "add", "text": "no ground", "himari_evidence": []},
        {"op": "retire", "id": "f9", "himari_evidence": [4]},  # not shown
        {"op": "forget", "id": "f5", "himari_evidence": [2]},  # no such op
        {"op": "update", "id": "f5", "himari_evidence": [2]},  # no text
    ]}))
    assert plan_self(dropped, PART, facts) == []
    assert plan_self(parse_answer(json.dumps({"notes": [], "self": None})), PART, facts) == []
    assert plan_self(parse_answer(json.dumps({"notes": [], "self": ["odd", {"op": "add"}]})), PART, facts) == []
    many = [{"op": "add", "text": f"사실 {i}.", "himari_evidence": [2]} for i in range(PASS_MAX_SELF + 2)]
    assert len(plan_self(parse_answer(json.dumps({"notes": [], "self": many})), PART, facts)) == PASS_MAX_SELF


# --- the bond (character-bond v0.1 §3) ----------------------------------------------------


def bond(entry_id: int, tag: BondTag, text: str, origin: LifeOrigin = LifeOrigin.CONVERSATION, revision: int = 1,
         month_day: str | None = None) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.BOND, None, text, origin, revision, NOW, NOW, tag=tag, month_day=month_day)


def test_with_himaris_life_the_pass_is_shown_the_bond_and_told_to_keep_what_the_two_settled() -> None:
    entries = [bond(4, BondTag.NAME, "대장", LifeOrigin.USER), bond(6, BondTag.DATE, "사용자 생일", month_day="03-02")]
    data = request_data(PART, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE, facts=[], bond=entries)
    assert data["bond_entries"] == [
        {"id": "b4", "tag": "name", "text": "대장", "written_by": "user"},
        {"id": "b6", "tag": "date", "text": "사용자 생일", "month_day": "03-02", "written_by": "conversation"}]
    assert "bond_entries" not in request_data(PART, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE, facts=[])
    assert "user" in BOND_INSTRUCTIONS and "at least one" in BOND_INSTRUCTIONS  # B2
    long = [bond(i, BondTag.JOKE, "x" * 300) for i in range(1, 20)]
    assert len(shown_bond(long)) == PASS_BOND_CHARS // 300


def test_bond_decisions_rest_on_the_users_messages_and_leave_the_users_entries_alone() -> None:
    entries = [bond(4, BondTag.NAME, "대장", LifeOrigin.USER), bond(6, BondTag.PROMISE, "주말마다 영화.", revision=3),
               bond(8, BondTag.DATE, "사용자 생일", month_day="03-02")]
    raw = parse_answer(json.dumps({"notes": [], "bond": [
        {"op": "add", "tag": "joke", "text": " 감자 사건 ", "evidence": {"user": [3], "himari": [2]}},
        {"op": "add", "tag": "name", "text": "HIMARI alone", "evidence": {"user": [], "himari": [2]}},  # B2
        {"op": "add", "tag": "promise", "text": "a himari seq as the user's", "evidence": {"user": [2]}},
        {"op": "add", "tag": "first", "text": "처음으로 같이 밤샘", "evidence": {"user": [1]}},  # no episode
        {"op": "add", "tag": "date", "text": "처음 만난 날", "month_day": "11-04", "evidence": {"user": [1]}},
        {"op": "update", "id": "b4", "text": "선장", "evidence": {"user": [1]}},  # the user's
    ]}, ensure_ascii=False))
    assert plan_bond(raw, PART, entries, episode=False) == [
        BondChange(LifeChangeOp.ADD, BondTag.JOKE, "감자 사건", sequences=(2, 3)),
        BondChange(LifeChangeOp.ADD, BondTag.DATE, "처음 만난 날", "11-04", sequences=(1,))]
    with_episode = plan_bond(raw, PART, entries, episode=True)
    assert BondChange(LifeChangeOp.ADD, BondTag.FIRST, "처음으로 같이 밤샘", sequences=(1,)) in with_episode
    changed = parse_answer(json.dumps({"notes": [], "bond": [
        {"op": "update", "id": "b6", "text": "토요일마다 영화.", "evidence": {"user": [3]}},
        {"op": "update", "id": "b8", "text": "생일", "evidence": {"user": [1]}},  # keeps its day
        {"op": "retire", "id": "8", "evidence": {"user": [1, 3], "himari": [4]}},
        {"op": "retire", "id": "b9", "evidence": {"user": [1]}},  # not shown
        {"op": "forget", "id": "b6", "evidence": {"user": [1]}},  # no such op
        {"op": "add", "tag": "nickname", "text": "x", "evidence": {"user": [1]}},  # no such tag
        "odd",
    ]}, ensure_ascii=False))
    assert plan_bond(changed, PART, entries, episode=False) == [
        BondChange(LifeChangeOp.UPDATE, BondTag.PROMISE, "토요일마다 영화.", None, 6, 3, (3,)),
        BondChange(LifeChangeOp.UPDATE, BondTag.DATE, "생일", "03-02", 8, 1, (1,)),
        BondChange(LifeChangeOp.RETIRE, entry_id=8, expected_revision=1, sequences=(1, 3, 4))]
    assert plan_bond(parse_answer(json.dumps({"notes": [], "bond": None})), PART, entries, episode=True) == []
    assert plan_bond(parse_answer(json.dumps({"notes": []})), PART, entries, episode=True) == []
    many = [{"op": "add", "tag": "joke", "text": f"농담 {i}", "evidence": {"user": [1]}}
            for i in range(PASS_MAX_BOND + 2)]
    assert len(plan_bond(parse_answer(json.dumps({"notes": [], "bond": many})), PART, entries,
                         episode=False)) == PASS_MAX_BOND


# --- HIMARI's thoughts (character-mind v0.1 §5) ------------------------------------------


def thinking(sequence: int, thought: str, text: str, *, calls: bool = False) -> ConversationEvent:
    """An answer (or, with ``calls``, a tool call's commentary) that began with a thought."""
    blocks = (TextBlock(f"[[think]]{thought}[[/think]]{text}"),)
    if calls:
        payload = AssistantToolCalls((ToolCall(CallId("c1"), 0, ToolId("t"), {}),), commentary=blocks)
        return ConversationEvent(EventId(f"e{sequence}"), CONV, sequence, EventKind.ASSISTANT_TOOL_CALLS, payload, 1,
                                 NOW)
    return event(sequence, AssistantFinal(blocks))


THOUGHTFUL = next_part([said(1, "오늘도 새벽까지 일했어"), thinking(2, "또 밤샜네. 잔소리는 참자.", "[[mood:worried]]고생했어!"),
                        said(3, "응 조금만 더 할게"), thinking(4, "스트레칭 얘기는 다음에.", "", calls=True),
                        answered(5, "화이팅!")], 0)


def kept(entry_id: int, text: str, about: ThoughtAbout = ThoughtAbout.USER) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.THOUGHT, None, text, LifeOrigin.CONVERSATION, 1, NOW, NOW, CONV, (2,),
                     "mpass_1", about=about, expires_at=NOW + timedelta(days=3))


def test_a_part_reads_the_thoughts_apart_from_what_was_said() -> None:
    assert THOUGHTFUL is not None
    replies = [(m.sequence, m.text, m.thought) for m in THOUGHTFUL.messages if m.role == "himari"]
    assert replies == [(2, "고생했어!", "또 밤샜네. 잔소리는 참자."), (4, "", "스트레칭 얘기는 다음에."), (5, "화이팅!", None)]
    data = request_data(THOUGHTFUL, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE, facts=[],
                        thoughts=[kept(7, "요즘 지쳐 보인다.")])
    assert data["himari_thoughts"] == [{"seq": 2, "thought": "또 밤샜네. 잔소리는 참자."},
                                       {"seq": 4, "thought": "스트레칭 얘기는 다음에."}]
    assert data["kept_thoughts"] == [{"id": "t7", "about": "user", "text": "요즘 지쳐 보인다.",
                                      "kept": "2026-10-31T21:00+09:00"}]
    assert "잔소리" not in json.dumps([m["text"] for m in data["messages"]], ensure_ascii=False)  # type: ignore[index, union-attr, call-overload]
    without = request_data(THOUGHTFUL, [], None, now=NOW, zone=SEOUL, kind=PassKind.LIVE, facts=[])
    assert "himari_thoughts" not in without and "kept_thoughts" not in without  # life off: never asked


def test_thoughts_kept_rest_on_the_parts_thoughts_and_said_names_a_kept_one() -> None:
    assert THOUGHTFUL is not None
    shown = [kept(7, "요즘 지쳐 보인다."), kept(8, "다음엔 영화 얘기.", ThoughtAbout.INTENT)]
    raw = parse_answer(json.dumps({"notes": [], "thoughts": [
        {"text": " 잔소리하고 싶었지만 참았다. ", "about": "SELF", "evidence": [2]},
        {"text": "a user message is no ground", "about": "user", "evidence": [1]},
        {"text": "no about", "evidence": [4]},
        {"text": "third: past the two read", "about": "work", "evidence": [4]},
    ], "said": ["t8", "t8", "t9", 7, "odd"]}, ensure_ascii=False))
    assert plan_thoughts(raw, THOUGHTFUL, shown) == [
        ThoughtChange(ThoughtOp.KEEP, "잔소리하고 싶었지만 참았다.", ThoughtAbout.SELF, sequences=(2,)),
        ThoughtChange(ThoughtOp.SAID, entry_id=8)]
    assert plan_thoughts(parse_answer(json.dumps({"notes": [], "thoughts": None, "said": None})), THOUGHTFUL,
                         shown) == []
    many = [{"text": f"생각 {i}", "about": "intent", "evidence": [2, 4]} for i in range(PASS_MAX_THOUGHTS + 2)]
    assert len(plan_thoughts(parse_answer(json.dumps({"notes": [], "thoughts": many})), THOUGHTFUL,
                             [])) == PASS_MAX_THOUGHTS
