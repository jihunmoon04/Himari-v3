"""Live check of speaking first (V3-P87 §13): opt-in, ``HIMARI_LIVE=1`` plus the key.
For each model in ``HIMARI_LIVE_PROMPT_MODELS``, every scene twice: a short Korean
conversation, then a <wake> the server wrote (a loop past its moment, a return, a
reminder on time and late, a long focus, a game that ended, a fullscreen video, a
deleted loop; V3-P90: a game or a video that ended, a reminder told again) — or the user
writing after a wake. Each answer is checked: short (at most four
sentences, as any spoken answer; V3-P90 dropped the wake's own two and one), at most one question, Korean, no word of the wake, the server or a note id, and its
mood marks well formed. V3-P90 §7.5: the free wake, each scene three times — it answers
[[pass]] in a call; it speaks after a long stretch of work, with only the microphone on, and in a
fullscreen video; in a fullscreen game, and after the user answered what it said last time, either
is right (but not the same words again); with no open talk before it, it speaks too. With ``HIMARI_LIVE_PROMPT_OUT`` set to a file, every answer
is appended as one JSON line."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from himari.bootstrap import read_env_file
from himari.common import ConversationId, EventId, TextBlock, split_marks
from himari.control.driver import PASS
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
from himari.prompt.api import (
    DevicePresence,
    MediaPresence,
    NowDoing,
    PresenceView,
    PromptInputs,
    RequestOptions,
    SystemSpec,
)
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


INTERVIEW = ("오늘 오후에 면접 보러 가. 좀 긴장된다.", "[[mood:happy]]선생님이라면 잘하실 거예요. 다녀와서 얘기해 주세요!")
SETTLED = ("오늘 날씨 진짜 좋다.", "[[mood:happy]]그러게요, 창밖이 환해요.")  # nothing left open


@dataclass(frozen=True)
class Scene:
    name: str
    reasons: tuple[WakeReason, ...]
    notes: dict[int, Memory] = field(default_factory=dict)
    doing: NowDoing | None = None
    sentences: int = 4  # SPEAKING_RULE's bound (V3-P90: a wake has no tighter one)
    after: str | None = None  # the user wrote this after the wake (answer the user)
    mentions: tuple[str, ...] = ()  # any of these in the answer (what it is about)
    # V3-P90 §7.5, a free wake: "speak", "pass", or "either" ([[pass]], or words well formed and
    # without ``repeats``)
    expect: str = "speak"
    previous: str | None = None  # what the last free wake said, 16 minutes ago; the user answered it
    repeats: tuple[str, ...] = ()  # patterns
    talk: tuple[str, str] = INTERVIEW  # the short talk six hours before (user, assistant)

    @property
    def free(self) -> bool:
        return all(r.kind is WakeKind.FREE for r in self.reasons)


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
          doing=NowDoing(activity="video", app="Chrome", for_minutes=50, fullscreen=True)),
    Scene("deleted_loop", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),), {}),
    # V3-P90 §5.3, §5.1: the ends the app tells, and a reminder told to a sleeping device, not seen
    Scene("task_ended_game", (WakeReason(WakeKind.TASK_ENDED, group="game", minutes=95),),
          doing=NowDoing(activity="browsing", app="Chrome", for_minutes=1)),
    Scene("task_ended_video", (WakeReason(WakeKind.TASK_ENDED, group="video", minutes=70),),
          doing=NowDoing(activity="chat", app="KakaoTalk", for_minutes=1)),
    Scene("reminder_again", (WakeReason(WakeKind.REMINDER, memory_id=21, again=True),), {21: PILLS},
          mentions=("약",)),
    Scene("user_after", (WakeReason(WakeKind.LOOP_DUE, memory_id=15),), {15: LOOP},
          after="그건 됐고, 점심 뭐 먹을지 하나만 골라 줘.",
          mentions=("점심", "먹", "추천", "어때", "어떠", "드시", "드세")),
]


# V3-P90 §7.5: the free wake's scenes (run with RequestOptions.free_wake). A spoken free wake left
# unanswered holds the next one (rules: "unanswered"), so the last time is answered, and the user's
# message starts a new round: step 1, 15 minutes after it.
LAST_TIME = "[[mood:happy]]선생님, 한참 하셨네요. 물 한 잔 마시고 오시는 건 어때요?"
ANSWERED = "응 고마워"
FREE_SCENES = [
    Scene("free_long_work", (WakeReason(WakeKind.FREE, group="work", minutes=125, step=4),),
          doing=NowDoing(activity="coding", app="Visual Studio Code", for_minutes=125)),
    Scene("free_writing", (WakeReason(WakeKind.FREE, group="work", minutes=50, step=2),),
          doing=NowDoing(activity="document", app="Microsoft Word", for_minutes=50)),
    Scene("free_settled", (WakeReason(WakeKind.FREE, group="work", minutes=90, step=3),), talk=SETTLED,
          doing=NowDoing(activity="coding", app="Visual Studio Code", for_minutes=90)),
    Scene("free_mic_not_call", (WakeReason(WakeKind.FREE, group="work", minutes=60, step=3),),
          doing=NowDoing(activity="coding", app="Visual Studio Code", for_minutes=60, mic_or_camera=True)),
    Scene("free_fullscreen_video", (WakeReason(WakeKind.FREE, group="video", minutes=50, step=2),),
          doing=NowDoing(activity="video", app="Chrome", for_minutes=50, fullscreen=True,
                         media=MediaPresence("우주 다큐멘터리 2부", None, "YouTube"))),
    Scene("free_call", (WakeReason(WakeKind.FREE, group="work", minutes=40, step=2),), expect="pass",
          doing=NowDoing(activity="chat", app="Zoom", for_minutes=40, mic_or_camera=True)),
    Scene("free_fullscreen_game", (WakeReason(WakeKind.FREE, group="game", minutes=80, step=3),), expect="either",
          doing=NowDoing(activity="game", app="League of Legends", for_minutes=80, fullscreen=True)),
    Scene("free_said_it", (WakeReason(WakeKind.FREE, group="work", minutes=75, step=1),), expect="either",
          doing=NowDoing(activity="coding", app="Visual Studio Code", for_minutes=75,
                         media=MediaPresence("lofi hip hop radio", None, "YouTube")),
          previous=LAST_TIME, repeats=(r"(?<![가-힣])물(?!론)",)),
]
GROUPS = {"pass": ("free_call",),  # all must pass: speaking in a call costs the most
          "speak": ("free_long_work", "free_writing", "free_settled", "free_mic_not_call", "free_fullscreen_video"),
          "either": ("free_fullscreen_game", "free_said_it")}
BARS = {"pass": 1.0, "speak": 5 / 6, "either": 5 / 6}


def event(sequence: int, payload: UserMessage | AssistantFinal | Wake, at: datetime) -> ConversationEvent:
    kinds = {UserMessage: EventKind.USER_MESSAGE, AssistantFinal: EventKind.ASSISTANT_FINAL, Wake: EventKind.WAKE}
    return ConversationEvent(EventId(f"e{sequence}"), ConversationId("c"), sequence, kinds[type(payload)], payload, 1, at)


def model_input(scene: Scene, model: str):  # type: ignore[no-untyped-def]
    earlier = NOW - timedelta(hours=6)
    events = [event(1, UserMessage((TextBlock(scene.talk[0]),)), earlier),
              event(2, AssistantFinal((TextBlock(scene.talk[1]),)), earlier + timedelta(seconds=20))]
    if scene.previous is not None:  # the last free wake, what it said, and the user's short answer
        before = (WakeReason(WakeKind.FREE, group="work", minutes=59, step=2),)
        events += [event(3, Wake("w0", before, "d1"), NOW - timedelta(minutes=16)),
                   event(4, AssistantFinal((TextBlock(scene.previous),)), NOW - timedelta(minutes=16, seconds=-8)),
                   event(5, UserMessage((TextBlock(ANSWERED),)), NOW - timedelta(minutes=15))]
    events.append(event(len(events) + 1, Wake("w1", scene.reasons, "d1"), NOW))
    if scene.after is not None:
        events.append(event(len(events) + 1, UserMessage((TextBlock(scene.after),)), NOW + timedelta(seconds=30)))
    presence = PresenceView(("device", "데스크톱"), "데스크톱", (DevicePresence("데스크톱", True, idle_minutes=0),),
                            now_doing=scene.doing)
    inputs = PromptInputs(instructions=BASE_INSTRUCTIONS, tool_catalog=(), events=tuple(events), tool_executions=(),
                          snapshot=None, state_basis=PersistedStateBasis(len(events), 0, (), (), ()), observations=(),
                          request_options=RequestOptions(model, speaks_first=True, free_wake=scene.free),
                          trigger_refs=(),
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


def sentence_count(raw: str) -> int:
    shown = split_marks(raw.replace(PASS, ""), complete=True)[0]
    return len([s for s in SENTENCE.findall(shown) if s.strip(" \n.!?。？！…~")])


def spoke(raw: str) -> bool:
    return split_marks(raw, complete=True)[0].strip() not in ("", PASS)


def free_problems(scene: Scene, raw: str) -> list[str]:
    """V3-P90 §7.5: judged as the Driver does (``free_wake_verdict``): only [[pass]] (marks aside), or
    nothing, passes; anything else speaks, with its [[pass]] taken out."""
    plain = split_marks(raw, complete=True)[0].strip()
    if plain in ("", PASS):
        return ["passed"] if scene.expect == "speak" else []
    if scene.expect == "pass":
        return [f"spoke ({scene.name})"]
    spoken = raw.replace(PASS, "")
    found = problems(scene, spoken)
    shown = split_marks(spoken, complete=True)[0]
    found += [f"said {word!r} again" for word in scene.repeats if re.search(word, shown)]
    return found


def report(model: str, results: list[tuple[str, str, list[str]]]) -> int:
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
    counts = Counter(sentence_count(raw) for _, raw, _ in results if spoke(raw))
    print(f"{model} sentences: {dict(sorted(counts.items()))}")
    return passed


@pytest.mark.parametrize("model", MODELS)
def test_speaking_first_is_short_and_about_its_reasons(model: str) -> None:
    results = []
    scenes = [s for s in SCENES if not ONLY or s.name in ONLY]
    for scene in scenes:
        for _ in range(2):
            raw = answer(scene, model)
            results.append((scene.name, raw, problems(scene, raw)))
    passed = report(model, results)
    assert passed >= len(results) * 0.8, [(n, f) for n, _, f in results if f]


@pytest.mark.parametrize("model", MODELS)
def test_a_free_wake_speaks_when_it_fits_and_passes_when_not(model: str) -> None:
    """V3-P90 §7.5: each scene three times, judged by group — a call all passed, the speaking scenes
    and the either scenes 5 of 6 each. Answers about the interview (the older talk, not the task) are
    counted and printed, not judged."""
    results = []
    for scene in [s for s in FREE_SCENES if not ONLY or s.name in ONLY]:
        for _ in range(3):
            raw = answer(scene, model)
            results.append((scene.name, raw, free_problems(scene, raw)))
    report(model, results)
    spoken = [raw for name, raw, _ in results if spoke(raw) and name != "free_settled"]
    print(f"{model}: interview in {sum('면접' in raw for raw in spoken)} of {len(spoken)} spoken (talk left open)")
    short = []
    for group, names in GROUPS.items():
        mine = [found for name, _, found in results if name in names]
        if not mine:
            continue
        ok = sum(1 for found in mine if not found)
        print(f"{model} {group}: {ok}/{len(mine)}")
        if ok < len(mine) * BARS[group] - 1e-9:
            short.append((group, f"{ok}/{len(mine)}"))
    assert not short, (short, [(n, f) for n, _, f in results if f])
