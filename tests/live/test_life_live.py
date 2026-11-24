"""Live check of HIMARI's life (character-life v0.1 §9): opt-in, ``HIMARI_LIVE=1`` plus the key. One story writer
call and one memory pass with ``self``, on the memory passes' model (``HIMARI_LIVE_LIFE_MODEL``, default the one the
operator configuration names), to see that the JSON round-trips with a real model: the writer's answer passes every
check, and the pass keeps what HIMARI said of itself. With ``HIMARI_LIVE_LIFE_OUT`` set to a file, both answers and
their usage are appended as JSON lines. Machine checks are only "is it the shape asked for" (character-architecture
§10); whether the writing is good is the owner's to judge."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime
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
    RecentEpisode,
    UserMessage,
)
from himari.life import writer
from himari.memory import passes
from himari.prompt.api import RequestOptions
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
        replace(entry(4, LifeKind.PLAN, "중고 레코드 가게 목록을 정리한다.", "2026-11-25"), time_of_day="10:00"))


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


def test_the_story_writer_writes_a_day_that_passes_its_checks() -> None:
    day = date(2026, 11, 25)
    talked = Episode(7, CONV, 0, 4, datetime(2026, 11, 25, 11, 0, tzinfo=UTC),
                     datetime(2026, 11, 25, 11, 40, tzinfo=UTC),
                     "사용자가 회사 발표 자료를 마무리했고, HIMARI가 마지막 장의 문장을 다듬자고 했다.", (1,), (2,),
                     MemoryOrigin.EXTRACTOR, None, 1, NOW, NOW)
    data = writer.request_data(PERSONA, LIFE, [RecentEpisode(talked, "발표")], day=day, now=NOW, zone=SEOUL)
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=writer.WRITER_MAX_OUTPUT_TOKENS)
    text, usage = call(writer.model_input(data, options))
    plan = writer.parse_answer(text, day)  # every §3 check: shape, lengths, counts, plan dates
    record("writer", text, usage, plans=len(plan.plans), facts=len(plan.facts), diary_chars=len(plan.diary),
           times=list(plan.times), mood=plan.mood, mood_reason=plan.mood_reason)
    assert plan.diary
    # character-state §2, §3: the optional mood and plan times, when given, are in the shape asked for — kept, not
    # dropped by the checks (an out-of-shape one would be left out silently)
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    raw = json.loads(body)
    given = raw.get("mood")
    if isinstance(given, dict) and str(given.get("name", "")).strip().lower() not in ("", "neutral"):
        assert plan.mood is not None, given
    times = plan.times or (None,) * len(plan.plans)
    for asked, kept in zip(raw.get("plans") or [], times):
        if isinstance(asked, dict) and asked.get("time") is not None:
            assert kept is not None, asked


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
