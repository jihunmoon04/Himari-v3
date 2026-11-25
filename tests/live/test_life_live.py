"""Live check of HIMARI's life (character-life v0.1 §9): opt-in, ``HIMARI_LIVE=1`` plus the key. One story writer
call and one memory pass with ``self``, on the memory passes' model (``HIMARI_LIVE_LIFE_MODEL``, default the one the
operator configuration names), to see that the JSON round-trips with a real model: the writer's answer passes every
check, and the pass keeps what HIMARI said of itself. With ``HIMARI_LIVE_LIFE_OUT`` set to a file, both answers and
their usage are appended as JSON lines. Machine checks are only "is it the shape asked for" (character-architecture
§10); whether the writing is good is the owner's to judge. The writer's input also holds a sample weather line, five
sample topic titles and one open arc (character-world v0.1), never the production search. character-work v0.1:
the writer is also told the quiet hours, the arc's so far and the day's work piece, and may name a plan's project;
one worker call makes a piece for a work plan and its answer passes every check (``-k worker``). character-mind
v0.1: the writer's call is now the day's review, a self run with a turn's system text and runtime state, whose answer
is a thought and then its JSON (``-k review``); the facts are settled from its diary by one more call (``-k settle``)."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from himari.bootstrap import read_env_file
from himari.common import ConversationId, EventId, TextBlock
from himari.data.api import (
    AssistantFinal,
    ConversationEvent,
    Episode,
    EventKind,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    MemoryOrigin,
    PassKind,
    PersistedStateBasis,
    RecentEpisode,
    UserMessage,
)
from himari.life import review, settle, work, world
from himari.memory import passes
from himari.prompt.api import PromptInputs, RequestOptions, SystemSpec
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE
from himari.provider.api import NativeReceived, NormalizedFinal, ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("HIMARI_LIVE_LIFE_MODEL", "deepseek/deepseek-v4.1-flash-1017")
OUT = os.environ.get("HIMARI_LIVE_LIFE_OUT")
SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # Wednesday 22:30 in Seoul
CONV = ConversationId("conv_live")

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")

PERSONA = (
    "이름: 루시\n"
    "나와의 관계: 오래 알고 지낸 친구\n"
    "어디서 왔고 지금 어디에 있는지: 원래 나이트 시티의 해커였고, 지금은 내 컴퓨터 화면 속 작은 방에 산다.\n"
    "나를 부르는 말: 너\n"
    "말투: 반말, 짧고 건조하지만 다정하다.\n"
    "대사 예시:\n- 또 밤새웠어? 적당히 해.\n- 그거 내가 봐 줄까.\n- 오늘은 좀 조용히 있고 싶네.\n"
    "성격: 무심한 척하지만 잘 챙긴다.\n"
    "좋아하는 것 / 싫어하는 것: 비 오는 날, 오래된 신스팝 / 시끄러운 광고\n"
    "하지 않는 것: 이모지, 지나친 칭찬"
)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None,
          origin: LifeOrigin = LifeOrigin.WRITER) -> LifeEntry:
    return LifeEntry(entry_id, 1, kind, day, text, origin, 1, NOW, NOW)


LIFE = (entry(1, LifeKind.FACT, "고양이를 무서워한다.", origin=LifeOrigin.USER),
        entry(2, LifeKind.FACT, "화면 속 방 창가에 작은 선인장을 키운다."),
        entry(3, LifeKind.DIARY, "오늘은 하루 종일 비가 왔다. 창가에 앉아 오래된 신스팝을 들었다. 선인장에 물을 줬다.",
              "2026-11-24"),
        replace(entry(4, LifeKind.PLAN, "중고 레코드 가게 목록을 정리한다.", "2026-11-25"), time_of_day="10:00"),
        # character-world §4: one open arc, moved once since it started; character-work §3: with its so far
        replace(entry(5, LifeKind.ARC, "한 달 안에 신스팝 커버곡 하나를 완성한다.", "2026-11-18"), title="커버곡 연습",
                so_far="곡은 'Blue Monday'로 정했다. 신스 베이스 라인을 따서 코드 진행(Dm-C-F-Am)을 적었고, "
                       "1절 가사는 한국어로 옮겼다. 후렴 가사와 편곡이 남았다."),
        replace(entry(6, LifeKind.DIARY, "커버곡 연습을 시작했다. 첫 소절부터 막혔다.", "2026-11-18"), arc_id=5),
        # character-work §4: today's work plan and the piece the worker made for it
        replace(entry(7, LifeKind.PLAN, "오늘 밤엔 1절 가사 마저 옮겨야지.", "2026-11-25"), time_of_day="20:00",
                arc_id=5),
        replace(entry(8, LifeKind.WORK, "1절 번역 노트 …", "2026-11-25"), time_of_day="20:00", title="1절 가사 옮기기",
                summary="1절 가사 다 옮겼어. 'How does it feel'을 어떻게 살릴지가 제일 어려웠어.", arc_id=5, plan_id=7))
QUIET = (time(23, 0), time(8, 0))
# character-world §2, §3: sample inputs (never the production search)
WEATHER = "light rain, 3 to 9°C, rain 2.1 mm"
TOPICS = ("수도권 첫눈 예보, 주말 기온 뚝", "연말 공연 예매 시작", "국회 예산안 처리 두고 공방", "새 스마트폰 출시 첫날 매진",
          "프로야구 시상식 열려")


def adapter() -> OpenRouterAdapter:
    known = KNOWN_CAPABILITIES.get(MODEL, ProviderCapabilities(MODEL))
    return OpenRouterAdapter(KEY, capabilities={MODEL: replace(known, memory_passes=True)})


def call(asked: Any) -> tuple[str, dict[str, Any]]:
    """The answer's text and the reported usage."""
    provider = adapter()
    outcome = asyncio.run(provider.invoke_prepared(provider.prepare_native_request(asked)))
    assert isinstance(outcome, NativeReceived), outcome
    output = provider.normalize(outcome)
    assert isinstance(output, NormalizedFinal)
    usage = outcome.usage
    return (passes.answer_text(output.blocks),
            {} if usage is None else {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})


def record(kind: str, text: str, usage: dict[str, Any], **more: Any) -> None:
    line = json.dumps({"kind": kind, "model": MODEL, "usage": usage, **more, "answer": text}, ensure_ascii=False)
    print(line)
    if OUT:
        with open(OUT, "a", encoding="utf-8") as out:
            out.write(line + "\n")


def effort() -> str | None:
    known = KNOWN_CAPABILITIES.get(MODEL, ProviderCapabilities(MODEL))
    return "low" if "low" in known.reasoning_efforts else None


def test_the_days_review_writes_a_day_that_passes_its_checks() -> None:
    day = date(2026, 11, 25)
    talked = Episode(7, CONV, 0, 4, datetime(2026, 11, 25, 11, 0, tzinfo=UTC),
                     datetime(2026, 11, 25, 11, 40, tzinfo=UTC),
                     "사용자가 회사 발표 자료를 마무리했고, HIMARI가 마지막 장의 문장을 다듬자고 했다.", (1,), (2,),
                     MemoryOrigin.EXTRACTOR, None, 1, NOW, NOW)
    data = review.request_data(LIFE, [RecentEpisode(talked, "발표")], day=day, now=NOW, zone=SEOUL,
                               weather=WEATHER, topics=TOPICS, quiet=QUIET)
    assert data["arcs"] == [{"id": 5, "title": "커버곡 연습", "goal": "한 달 안에 신스팝 커버곡 하나를 완성한다.",
                             "started": "2026-11-18", "so_far": LIFE[4].so_far, "moved_on": ["2026-11-18"]}]
    assert data["work"] and data["quiet_hours"] == "23:00-08:00"
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=review.REVIEW_MAX_OUTPUT_TOKENS)
    inputs = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                          state_basis=PersistedStateBasis(0, 0, ()), observations={}, request_options=options,
                          trigger_refs=(), system=SystemSpec(companion=True, life=True), companion_persona=PERSONA,
                          now=NOW.astimezone(SEOUL), time_zone="Asia/Seoul", life=LIFE)
    text, usage = call(Composer().self_input(inputs, review.REVIEW_TASK, data))
    thought, body = review.self_answer(text)  # character-mind §4.1: the thought, then the JSON
    plan = review.parse_answer(body, day, QUIET)  # every §3 check: shape, lengths, counts, plan dates
    record("review", text, usage, thought=thought, plans=len(plan.plans), diary_chars=len(plan.diary),
           times=list(plan.times), plan_arcs=list(plan.plan_arcs), mood=plan.mood, mood_reason=plan.mood_reason,
           arc=None if plan.arc is None else {"move": plan.arc.op.value, "id": plan.arc.entry_id,
                                              "title": plan.arc.title, "goal": plan.arc.goal,
                                              "ending": plan.arc.ending})
    assert plan.diary
    # character-state §2, §3: the optional mood and plan times, when given, are in the shape asked for — kept, not
    # dropped by the checks (an out-of-shape one would be left out silently)
    raw = review.json_object(body)
    given = raw.get("mood")
    if isinstance(given, dict) and str(given.get("name", "")).strip().lower() not in ("", "neutral"):
        assert plan.mood is not None, given
    times = plan.times or (None,) * len(plan.plans)
    for asked, kept in zip(raw.get("plans") or [], times):
        if isinstance(asked, dict) and asked.get("time") is not None:
            assert kept is not None, asked
    # character-world §4: an arc move, when given, is in the shape asked for (an out-of-shape one is left out)
    if raw.get("arc") is not None:
        assert plan.arc is not None, raw["arc"]
    # character-work §4: a plan's project, when given with a time outside the quiet hours, is kept
    arcs = plan.plan_arcs or (None,) * len(plan.plans)
    for asked, project, at in zip(raw.get("plans") or [], arcs, times):
        if isinstance(asked, dict) and asked.get("arc") is not None and at is not None \
                and not review.in_quiet_hours(time.fromisoformat(at), *QUIET):
            assert project is not None, asked


def test_the_facts_settled_from_a_diary_pass_their_checks() -> None:
    """character-mind §5.3: one settle call on a diary that newly holds a friend's name."""
    diary = entry(9, LifeKind.DIARY, "오후에 친구 유나가 새 앨범을 들고 찾아왔다. 같이 들으면서 선인장에 물을 줬다.",
                  "2026-11-25")
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=settle.SETTLE_MAX_OUTPUT_TOKENS)
    text, usage = call(review.model_input(settle.SETTLE_INSTRUCTIONS, settle.request_data(diary, LIFE), options,
                                          "life-settle-input"))
    facts = settle.parse_facts(text)  # every §5.3 check
    record("settle", text, usage, facts=list(facts))


def test_the_worker_makes_a_piece_that_passes_its_checks() -> None:
    """character-work §4: one worker call for the work plan of the open project, after its hour."""
    plan = replace(LIFE[6], text="오늘 밤엔 후렴 가사 붙여 봐야지.", entry_id=9)
    life = (*LIFE[:6], plan, LIFE[7])
    at = datetime(2026, 11, 25, 12, 10, tzinfo=UTC)  # 21:10 in Seoul: the 20:00 work time is over
    data = work.request_data(PERSONA, life, plan, LIFE[4], now=at, zone=SEOUL)
    told = data["project"]
    assert data["recent_pieces"] and isinstance(told, dict) and told.get("so_far")
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=work.WORK_MAX_OUTPUT_TOKENS)
    text, usage = call(work.model_input(data, options))
    piece = work.parse_piece(text)  # every §4 check, the whole answer
    record("worker", text, usage, piece_chars=len(piece.text), title=piece.title, summary=piece.summary,
           so_far=piece.so_far, done=piece.done)


def test_a_memory_pass_keeps_what_himari_said_of_itself() -> None:
    turns = (("오늘 뭐 했어?",
              "별거 없었어. 아침엔 창가 선인장 옆에서 레코드 목록 정리했고, 오후엔 예전 나이트 시티 동료 미코한테서 "
              "연락이 와서 한참 수다 떨었어. 아, 그리고 나 요즘 우쿨렐레 배우기 시작했어."),
             ("오 우쿨렐레? 나도 기타 쳐.", "알아, 네가 기타 친다고 했잖아. 나도 언젠가 같이 쳐 보고 싶네."))
    events: list[ConversationEvent] = []
    for index, (said, answered) in enumerate(turns):
        for seq, payload in ((2 * index + 1, UserMessage((TextBlock(said),))),
                             (2 * index + 2, AssistantFinal((TextBlock(answered),)))):
            kind = EventKind.USER_MESSAGE if isinstance(payload, UserMessage) else EventKind.ASSISTANT_FINAL
            events.append(ConversationEvent(EventId(f"e{seq}"), CONV, seq, kind, payload, 1, NOW))
    part = passes.next_part(events, 0)
    assert part is not None
    facts = passes.shown_facts(LIFE)
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=passes.PASS_MAX_OUTPUT_TOKENS)
    data = passes.request_data(part, [], None, now=NOW, zone="Asia/Seoul", kind=PassKind.LIVE, facts=facts)
    text, usage = call(passes.model_input(data, options, life=True))
    answer = passes.parse_answer(text)  # the notes and the episode as ever, self beside them
    changes = passes.plan_self(answer, part, facts)
    items = passes.plan_items(answer, part, [], kind=PassKind.LIVE, now=NOW, zone="Asia/Seoul")
    record("memory_pass", text, usage, self=[(c.op.value, c.text) for c in changes], notes=len(items))
    # new things HIMARI said of itself (a ukulele, a friend Miko); never the user's own facts
    assert any(c.op is LifeChangeOp.ADD and any(w in (c.text or "") for w in ("우쿨렐레", "미코")) for c in changes)


def test_the_world_decides_events_that_pass_their_checks() -> None:
    """character-mind §7: one world call for the day; 0 to 2 events, each a time outside the quiet hours."""
    data = world.request_data(PERSONA, LIFE, day=date(2026, 11, 25), weather=WEATHER, topics=TOPICS, quiet=QUIET)
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=world.WORLD_MAX_OUTPUT_TOKENS)
    text, usage = call(review.model_input(world.WORLD_INSTRUCTIONS, data, options, "life-world-input"))
    events = world.parse_events(text, QUIET)  # every §7 check
    record("world", text, usage, events=[(e.time_of_day, e.text) for e in events])
