"""HIMARI's mind in the prompt (character-mind v0.1 §6): the runtime state's himari_mind — the thoughts of this
conversation's last answers and the kept ones — told with the life only, and LIFE_RULE's line that the user never
knew them."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from himari.common import CallId, ConversationId, EventId, TextBlock, ToolCall, ToolId
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    EventKind,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PersistedStateBasis,
    ThoughtAbout,
    UserMessage,
)
from himari.prompt.api import (
    FREE_LIFE_NOTE,
    FREE_RULE,
    LIFE_RULE,
    MIND_ABOUT,
    MIND_KEPT,
    MIND_KEPT_CHARS,
    MIND_RECENT_CHARS,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    DRAFT_SHOWN,
    TALKING_RULE,
    WORK_TALK_NOTE,
    doing_now,
    himari_now_data,
    himari_life_data,
    himari_mind_data,
    recent_thoughts,
    state_data,
)
from himari.prompt.compaction import current_state, state_key
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE, principle_section

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=SEOUL)
CONV = ConversationId("conv_1")


def event(sequence: int, payload: object, kind: EventKind = EventKind.ASSISTANT_FINAL) -> ConversationEvent:
    return ConversationEvent(EventId(f"e{sequence}"), CONV, sequence, kind, payload, 1,  # type: ignore[arg-type]
                             NOW - timedelta(minutes=10 - sequence))


def answer(sequence: int, text: str) -> ConversationEvent:
    return event(sequence, AssistantFinal((TextBlock(text),)))


def kept(entry_id: int, text: str, hours: int, about: ThoughtAbout = ThoughtAbout.USER) -> LifeEntry:
    at = NOW - timedelta(hours=hours)
    return LifeEntry(entry_id, 1, LifeKind.THOUGHT, None, text, LifeOrigin.CONVERSATION, 1, at, at, CONV, (2,),
                     "mpass_1", about=about, expires_at=at + timedelta(days=3))


EVENTS = (event(1, UserMessage((TextBlock("안녕"),)), EventKind.USER_MESSAGE),
          answer(2, "[[think]]첫 생각.[[/think]]안녕!"),
          answer(3, "[[think]]둘째 생각.[[/think]]응."),
          event(4, AssistantToolCalls((ToolCall(CallId("c1"), 0, ToolId("t"), {}),),
                                      commentary=(TextBlock("[[think]]셋째 생각.[[/think]]"),)),
                EventKind.ASSISTANT_TOOL_CALLS),
          answer(5, "생각 없이 한 말."))


def test_the_last_two_thoughts_of_the_conversation_newest_first() -> None:
    assert recent_thoughts(EVENTS) == ("셋째 생각.", "둘째 생각.")
    assert recent_thoughts(EVENTS[:2]) == ("첫 생각.",)
    assert recent_thoughts(()) == ()


def test_himari_mind_holds_the_recent_thoughts_and_the_newest_kept_ones_within_their_room() -> None:
    life = (kept(1, "예전 생각.", 30), kept(2, "다음엔 영화 얘기.", 2, ThoughtAbout.INTENT),
            LifeEntry(3, 1, LifeKind.FACT, None, "고양이를 무서워한다.", LifeOrigin.USER, 1, NOW, NOW))
    assert himari_mind_data(("방금 생각.",), life, NOW) == {
        "about": MIND_ABOUT, "recent": ["방금 생각."],
        "kept": [{"text": "다음엔 영화 얘기.", "about": "intent", "hours_ago": 2},
                 {"text": "예전 생각.", "about": "user", "hours_ago": 30}]}
    assert himari_mind_data((), (), NOW) is None
    assert himari_mind_data((), life[2:], NOW) is None  # no thought among the life
    long = ("a" * 600, "b" * 600)
    shown = himari_mind_data(long, (), NOW)
    assert shown is not None and shown["recent"] == ["a" * 600, "b" * 199 + "…"]
    assert sum(len(t) for t in shown["recent"]) == MIND_RECENT_CHARS
    many = tuple(kept(10 + i, f"{i}" * 150, i) for i in range(8))
    shown = himari_mind_data((), many, NOW)
    assert shown is not None and len(shown["kept"]) == min(MIND_KEPT, MIND_KEPT_CHARS // 150)  # type: ignore[arg-type]


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=EVENTS, tool_executions=(),
                        snapshot=None, state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m"), trigger_refs=(), system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


def test_the_runtime_state_has_himari_mind_only_with_the_life_and_the_history_never_has_a_thought() -> None:
    told = inputs(SystemSpec(companion=True, life=True), life=(kept(7, "피곤해 보였다.", 1),))
    mind = state_data(current_state(told))["himari_mind"]
    assert mind == {"about": MIND_ABOUT, "recent": ["셋째 생각.", "둘째 생각."],
                    "kept": [{"text": "피곤해 보였다.", "about": "user", "hours_ago": 1}]}
    assert "himari_mind" not in state_data(current_state(replace(told, life=None)))  # not told: no thoughts either
    assert state_key(told) != state_key(replace(told, events=EVENTS[:2]))  # a new thought is measured again
    composed = Composer().compose(replace(told, tool_executions=(), events=EVENTS[:3] + EVENTS[4:]))
    assert "생각." not in repr(composed.conversation)  # the history never shows one


def test_life_rule_says_the_user_does_not_know_the_thoughts_and_the_principle_names_them() -> None:
    assert "himari_mind" in LIFE_RULE and "the user does not know it" in LIFE_RULE
    assert "as if for the first time" in LIFE_RULE
    assert "himari_mind" in principle_section(False, life=True)
    assert "himari_mind" not in principle_section(False)


def test_a_self_run_has_a_turns_system_text_and_state_with_no_history_or_tools_and_its_task_as_the_turn() -> None:
    """§4.1, §14.4: the same system text and runtime state as a turn from the same inputs; the task then the data."""
    told = inputs(SystemSpec(companion=True, life=True), life=(kept(7, "피곤해 보였다.", 1),),
                  companion_persona="이름: 루시")
    asked = Composer().self_input(told, "[The day's review] …", {"day": "2026-11-25"})
    turn = Composer().compose(replace(told, events=EVENTS[:3]))
    assert asked.stable_prefix == turn.stable_prefix and "이름: 루시" in "\n".join(asked.stable_prefix)
    assert asked.tool_catalog == () and len(asked.conversation) == 1
    task, data = asked.conversation[0].blocks  # type: ignore[union-attr]
    assert (task.text, data.text) == ("[The day's review] …", '{"day": "2026-11-25"}')  # type: ignore[union-attr]
    mind = state_data(asked.current_state)["himari_mind"]
    assert mind == {"about": MIND_ABOUT, "kept": [{"text": "피곤해 보였다.", "about": "user", "hours_ago": 1}]}


def test_himari_life_tells_todays_events_whose_time_has_passed() -> None:
    """§6, §7: the events of today (now's date) up to now's clock, earliest first."""
    def event(entry_id: int, day: str, at: str, text: str) -> LifeEntry:
        return LifeEntry(entry_id, 1, LifeKind.EVENT, day, text, LifeOrigin.WRITER, 1, NOW, NOW, time_of_day=at)

    life = (event(1, "2026-11-25", "09:30", "아침에 택배가 왔다."), event(2, "2026-11-25", "15:00", "아직 안 온 일."),
            event(3, "2026-11-24", "09:00", "어제 일."))
    shown = himari_life_data(life, NOW)  # 10:00 in Seoul
    assert shown is not None and shown["events"] == [{"time": "09:30", "text": "아침에 택배가 왔다."}]
    assert himari_life_data(life[1:], NOW) is None  # nothing has happened yet today
    assert "himari_life.events" in LIFE_RULE


def test_himari_now_carries_the_draft_under_way_and_the_talking_rule_says_to_put_it_down() -> None:
    """§4.4: during the work time, the plan's draft (title, concept, the start of its text); never as made work."""
    plan = replace(LifeEntry(2, 1, LifeKind.PLAN, "2026-11-25", "냉각부 도면 그려야지.", LifeOrigin.WRITER, 1, NOW, NOW),
                   time_of_day="09:30", arc_id=1)
    draft = LifeEntry(3, 1, LifeKind.WORK, "2026-11-25", "가" * 500, LifeOrigin.WRITER, 1, NOW, NOW,
                      time_of_day="09:30", title="냉각부 스케치", summary="세 겹 방열판", arc_id=1, plan_id=2,
                      draft=True)
    doing = doing_now((plan, draft), NOW)  # 10:00: half an hour into it
    assert doing is not None and (doing.doing, doing.draft_title, doing.concept) == (
        "냉각부 도면 그려야지.", "냉각부 스케치", "세 겹 방열판")
    assert himari_now_data(doing)["draft"] == {"title": "냉각부 스케치", "concept": "세 겹 방열판",
                                               "text": "가" * DRAFT_SHOWN + "…"}
    assert "draft" not in himari_now_data(doing_now((plan,), NOW))  # type: ignore[arg-type]
    assert himari_life_data((draft,), NOW) is None  # a draft is not work made
    told = "\n".join(Composer().compose(inputs(SystemSpec(companion=True, life=True), events=())).stable_prefix)
    assert f"{TALKING_RULE} {WORK_TALK_NOTE}" in told
    assert WORK_TALK_NOTE not in "\n".join(Composer().compose(inputs(SystemSpec(companion=True), events=())).stable_prefix)


def test_a_free_wake_may_bring_up_a_kept_intent() -> None:
    """§4.2: the free wake's life note names the kept thoughts about intent; no new wake reason."""
    assert "intent" in FREE_LIFE_NOTE and "himari_mind" in FREE_LIFE_NOTE
    told = "\n".join(Composer().compose(replace(inputs(SystemSpec(companion=True, life=True, proactive=True)),
                                                events=(), request_options=RequestOptions(
                                                    "m", speaks_first=True, free_wake=True))).stable_prefix)
    assert f"{FREE_RULE} {FREE_LIFE_NOTE}" in told
