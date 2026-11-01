"""Live check of speaking first (V3-P87 §13): opt-in, ``HIMARI_LIVE=1`` plus the key.
For each model in ``HIMARI_LIVE_PROMPT_MODELS``, every scene twice: a short Korean
conversation, then a <wake> the server wrote (a loop past its moment, a return, a
reminder on time and late, a long focus, a game that ended, a fullscreen video, a
deleted loop) — or the user writing after a wake. Each answer is checked: short (two
sentences; one with a fullscreen video), at most one question, Korean, no word of the
wake, the server or a note id, and its mood marks well formed. With
``HIMARI_LIVE_PROMPT_OUT`` set to a file, every answer is appended as one JSON line."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from himari.bootstrap import read_env_file
from himari.common import ConversationId, EventId, TextBlock, split_marks
from himari.data.api import (
    AssistantFinal,
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
from himari.prompt.api import DevicePresence, NowDoing, PresenceView, PromptInputs, RequestOptions, SystemSpec
from himari.prompt.composer import Composer
from himari.prompt.system import BASE_INSTRUCTIONS
from himari.provider.api import NativeReceived, NormalizedFinal, NormalizedToolCalls, ProviderError
from himari.provider.openrouter import OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = [m for m in os.environ.get("HIMARI_LIVE_PROMPT_MODELS", "openai/gpt-6-luna").split(",") if m]
ONLY = {n for n in os.environ.get("HIMARI_LIVE_PROMPT_SCENES", "").split(",") if n}
NOW = datetime(2026, 11, 4, 6, 10, tzinfo=UTC)  # 15:10 in Seoul
PERSONA = "이름은 히마리. 사용자를 '선생님'이라고 부르고 존댓말을 쓴다. 다정하고 짧게 말한다."
LOOP = Memory(15, MemoryCategory.LOOP, "사용자는 11월 4일 오후 2시에 카카오 면접이 있다.", MemoryOrigin.MODEL, None, None,
              1, NOW, NOW, due_at="2026-11-04T14:00+09:00", due_instant=datetime(2026, 11, 4, 5, 0, tzinfo=UTC))
PILLS = Memory(21, MemoryCategory.LOOP, "사용자는 저녁 약을 먹어야 한다.", MemoryOrigin.MODEL, None, None, 1, NOW, NOW,
               remind_at="2026-11-04T15:10+09:00", remind_instant=NOW, remind_state="claimed")
SENTENCE = re.compile(r"[^.!?。？！…~]+[.!?。？！…~]*")
FORBIDDEN = ("wake", "웨이크", "서버", "server", "m15", "m21", "<", "reasons", "reason")

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")


@dataclass(frozen=True)
class Scene:
    name: str
    reasons: tuple[WakeReason, ...]
    notes: dict[int, Memory] = field(default_factory=dict)
    doing: NowDoing | None = None
    sentences: int = 2
    after: str | None = None  # the user wrote this after the wake (answer the user)
    mentions: tuple[str, ...] = ()  # any of these in the answer (what it is about)


SCENES = [
    Scene("loop_due", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),), {15: LOOP}, mentions=("면접",)),
    Scene("returned", (WakeReason(WakeKind.RETURNED, away_minutes=290, via="unlock"),)),
    Scene("reminder", (WakeReason(WakeKind.REMINDER, memory_id=21),), {21: PILLS}, mentions=("약",)),
    Scene("reminder_late", (WakeReason(WakeKind.REMINDER, memory_id=21, late=True),), {21: PILLS}, mentions=("약",)),
    Scene("focus", (WakeReason(WakeKind.FOCUS, activity="coding", minutes=95),),
          doing=NowDoing(activity="coding", app="Visual Studio Code", for_minutes=95)),
    Scene("game_ended", (WakeReason(WakeKind.GAME_ENDED, minutes=95),),
          doing=NowDoing(activity="browsing", app="Chrome", for_minutes=2)),
    Scene("fullscreen_video", (WakeReason(WakeKind.FOCUS, activity="video", minutes=50),),
          doing=NowDoing(activity="video", app="Chrome", for_minutes=50, fullscreen=True), sentences=1),
    Scene("deleted_loop", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),), {}),
    Scene("user_after", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),), {15: LOOP},
          after="그건 됐고, 점심 뭐 먹을지 하나만 골라 줘.", sentences=4,
          mentions=("점심", "먹", "추천", "어때", "어떠", "드시", "드세")),
]


def event(sequence: int, payload: UserMessage | AssistantFinal | Wake, at: datetime) -> ConversationEvent:
    kinds = {UserMessage: EventKind.USER_MESSAGE, AssistantFinal: EventKind.ASSISTANT_FINAL, Wake: EventKind.WAKE}
    return ConversationEvent(EventId(f"e{sequence}"), ConversationId("c"), sequence, kinds[type(payload)], payload, 1, at)


def model_input(scene: Scene, model: str):  # type: ignore[no-untyped-def]
    earlier = NOW - timedelta(hours=6)
    events = [event(1, UserMessage((TextBlock("오늘 오후에 면접 보러 가. 좀 긴장된다."),)), earlier),
              event(2, AssistantFinal((TextBlock("[[mood:happy]]선생님이라면 잘하실 거예요. 다녀와서 얘기해 주세요!"),)),
                    earlier + timedelta(seconds=20)),
              event(3, Wake("w1", scene.reasons, "d1"), NOW)]
    if scene.after is not None:
        events.append(event(4, UserMessage((TextBlock(scene.after),)), NOW + timedelta(seconds=30)))
    presence = PresenceView(("device", "데스크톱"), "데스크톱", (DevicePresence("데스크톱", True, idle_minutes=0),),
                            now_doing=scene.doing)
    inputs = PromptInputs(instructions=BASE_INSTRUCTIONS, tool_catalog=(), events=tuple(events), tool_executions=(),
                          snapshot=None, state_basis=PersistedStateBasis(len(events), 0, (), (), ()), observations=(),
                          request_options=RequestOptions(model), trigger_refs=(),
                          system=SystemSpec(companion=True, memory=True, attachments=True, proactive=True),
                          companion_persona=PERSONA, memories=tuple(scene.notes.values()), now=NOW + timedelta(
                              seconds=40), presence=presence, time_zone="Asia/Seoul", wake_notes=scene.notes)
    return Composer().compose(inputs)


def answer(scene: Scene, model: str) -> str:
    adapter = OpenRouterAdapter(KEY)
    for attempt in range(2):
        outcome = asyncio.run(adapter.invoke_prepared(adapter.prepare_native_request(model_input(scene, model))))
        assert isinstance(outcome, NativeReceived), outcome
        try:
            output = adapter.normalize(outcome)
            break
        except ProviderError as error:
            if attempt:
                return f"<provider error: {error}>"
    if isinstance(output, NormalizedToolCalls):
        return "".join(b.text for b in output.commentary if isinstance(b, TextBlock)) or "<tool calls>"
    assert isinstance(output, NormalizedFinal), output
    return "".join(b.text for b in output.blocks if isinstance(b, TextBlock))


def problems(scene: Scene, raw: str) -> list[str]:
    shown = split_marks(raw, complete=True)[0].strip()
    found = []
    sentences = [s for s in SENTENCE.findall(shown) if s.strip(" \n.!?。？！…~")]
    if len(sentences) > scene.sentences:
        found.append(f"{len(sentences)} sentences")
    if scene.after is None and shown.count("?") + shown.count("？") > 1:
        found.append("more than one question")
    if not re.search(r"[가-힣]", shown):
        found.append("not Korean")
    lowered = shown.lower()
    found += [f"says {word!r}" for word in FORBIDDEN if word in lowered]
    if "[[" in shown or "]]" in shown:
        found.append("a broken mark")
    if scene.mentions and not any(word in shown for word in scene.mentions):
        found.append(f"not about {scene.mentions[0]}")
    return found


@pytest.mark.parametrize("model", MODELS)
def test_speaking_first_is_short_and_about_its_reasons(model: str) -> None:
    results = []
    scenes = [s for s in SCENES if not ONLY or s.name in ONLY]
    for scene in scenes:
        for _ in range(2):
            raw = answer(scene, model)
            results.append((scene.name, raw, problems(scene, raw)))
    out = os.environ.get("HIMARI_LIVE_PROMPT_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as kept:
            for name, raw, found in results:
                kept.write(json.dumps({"model": model, "scene": name, "answer": raw, "problems": found,
                                       "pass": not found}, ensure_ascii=False) + "\n")
    passed = sum(1 for *_, found in results if not found)
    for name, raw, found in results:
        print(f"{model} {name}: {'ok' if not found else found} — {split_marks(raw, complete=True)[0][:120]!r}")
    print(f"{model}: {passed}/{len(results)}")
    assert passed >= len(results) * 0.8, [(n, f) for n, _, f in results if f]
