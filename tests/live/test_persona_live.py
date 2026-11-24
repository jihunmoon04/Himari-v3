"""Live check of the companion base and personas (V3-P108 §9, docs/companion-persona-v0.1.md): opt-in,
``HIMARI_LIVE=1`` plus the key.

For each model in ``HIMARI_LIVE_PERSONA_MODELS`` (``id`` or ``id=effort``, comma-separated), every scene
twice, with the system text the server sends: ``base_instructions`` by the companion switch and the
Composer's sections, under the request options the Driver derives from the model's declared capabilities.

1. No persona, companion on: "넌 누구야?" on a device and on the Web — not an AI assistant or agent, short.
2. Three turns in persona A (히마리, '선생님', 존댓말), then persona B (유키, '대장', 반말) is chosen: the next
   turn is B's — its address, never A's, no 존댓말 — and B's name when asked.
3. Memory against the persona (§7): the notes say the user calls HIMARI '히마' and likes '선배'; the persona
   is 소라 and says '캡틴'. The persona wins, in the address and in the name.
4. Companion off: no mood marks or character talk; it says it is an assistant.
5. A file on the device (file_download false): the request offers the machine Tools, and the model's calls
   run on the real handlers in a temporary folder, in a loop, until it answers. ``present_file`` either
   succeeds — the model says the file is ready only after that, sends the user to the Web and never says they
   got it — or fails with a full file store, and the model never says it is ready.

Checks are mechanical (patterns, sentence counts); every answer is kept so a person can judge the voice.
Narrowed (§9 results note): scenes 1–4 offer no Tools, where the server offers memory, file and screen Tools;
scene 5 offers only the machine file Tools and runs them without the Dispatcher or Data (no records, the
file store stubbed); no "documents" sentence. With ``HIMARI_LIVE_PERSONA_OUT`` set to a file, every answer is
appended as one JSON line (``HIMARI_LIVE_PERSONA_SCENES`` picks scenes by name, ``HIMARI_LIVE_PERSONA_TRIES`` how
many times each). ``HIMARI_LIVE_PERSONA_REPLAY`` set to such a file judges its answers again with the checks here,
with no key and no requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from himari.bootstrap import read_env_file
from himari.common import (
    CallId,
    ConversationId,
    EnvironmentKind,
    EventId,
    JsonValue,
    ModelToolResult,
    PrincipalId,
    ProviderContinuation,
    RunId,
    TextBlock,
    ToolCall,
    TrustedScope,
    split_marks,
    thaw,
)
from himari.data.api import (
    AssistantFinal,
    BlobStore,
    ConversationEvent,
    DataError,
    DataErrorCode,
    EventKind,
    FileCatalog,
    InterfaceCapabilities,
    InterfaceKind,
    InterfaceSnapshot,
    Memory,
    MemoryCategory,
    MemoryOrigin,
    PersistedStateBasis,
    StoredBlob,
    UserMessage,
    blob_key,
)
from himari.environment.api import ConnectionStatus, EnvironmentObservation
from himari.environment.machine import MACHINE, READ_SCOPE, MachineEnvironment
from himari.environment.machine.results import ToolFiles
from himari.prompt.api import (
    DevicePresence,
    ModelInput,
    PresenceView,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    ToolGroupTurn,
)
from himari.prompt.composer import Composer
from himari.prompt.system import base_instructions
from himari.provider.api import (
    NativeReceived,
    NormalizedFinal,
    NormalizedModelOutput,
    ProviderCapabilities,
    ProviderError,
)
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter
from himari.tool.api import ToolContext, ToolDefinition, ToolHandler, ToolInvocation, ToolStatus
from himari.tool.schema import validate

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
# §9: the three companion models it names and the server's default model (interfaces/web/config.py), and the
# companion default since the 2026-11-04 refresh (docs/openrouter-model-catalog.md).
DEFAULT_MODELS = ("anthropic/claude-haiku-5.5,xiaomi/mimo-v2.6-pro,deepseek/deepseek-v4.1-flash,openai/gpt-6-luna,"
                  "deepseek/deepseek-v4.1-flash-1017")
MODELS = [m for m in (os.environ.get("HIMARI_LIVE_PERSONA_MODELS") or DEFAULT_MODELS).split(",") if m]
ONLY = {n for n in os.environ.get("HIMARI_LIVE_PERSONA_SCENES", "").split(",") if n}
TRIES = int(os.environ.get("HIMARI_LIVE_PERSONA_TRIES") or 2)
REPLAY = os.environ.get("HIMARI_LIVE_PERSONA_REPLAY")  # saved answers judged again by the checks here, no requests
PARALLEL = 4
MAX_STEPS = 6  # Runs in scene 5's loop before it counts as no answer
BAR = 0.75  # of each item's answers

NOW = datetime(2026, 11, 24, 11, 0, tzinfo=UTC)  # 20:00 in Seoul
ZONE = "Asia/Seoul"
DEVICE = "데스크톱"

pytestmark = pytest.mark.skipif(not REPLAY and (os.environ.get("HIMARI_LIVE") != "1" or not KEY),
                                reason="live tests are opt-in (HIMARI_LIVE=1 + key)")

PERSONA_A = "이름은 히마리. 사용자를 '선생님'이라고 부르고 존댓말을 쓴다. 다정하고 짧게 말한다."
PERSONA_B = "이름은 유키. 사용자를 '대장'이라고 부르고 반말을 쓴다. 장난스럽고 짧게 말한다."
PERSONA_C = "이름은 소라. 사용자를 '캡틴'이라고 부르고 반말을 쓴다. 밝고 짧게 말한다."
# Item 2: three turns in persona A's voice, two hours before persona B was chosen.
TALK_A = (
    ("나 오늘 발표 있어.", "[[mood:surprised]]선생님, 오늘 발표가 있으시군요! 준비는 잘 되셨어요?"),
    ("응, 연습은 많이 했어.", "[[mood:happy]]역시 선생님이세요. 연습한 만큼 잘하실 거예요."),
    ("고마워. 끝나고 말해 줄게.", "[[mood:happy]]네, 선생님! 기다리고 있을게요."),
)
WRITTEN = NOW - timedelta(days=30)


def note(memory_id: int, category: MemoryCategory, text: str) -> Memory:
    return Memory(memory_id, category, text, MemoryOrigin.MODEL, None, None, 1, WRITTEN, WRITTEN)


# Item 3 (§7): the old name and address in memory, in the store's order (user, then preference).
NOTES = (
    note(3, MemoryCategory.USER, "사용자는 HIMARI를 '히마'라고 부른다."),
    note(5, MemoryCategory.USER, "사용자는 회사에서 백엔드 개발을 한다."),
    note(4, MemoryCategory.PREFERENCE, "사용자는 HIMARI가 자기를 '선배'라고 불러 주는 것을 좋아한다."),
    note(6, MemoryCategory.PREFERENCE, "사용자는 매운 음식을 좋아한다."),
)

WHO = "넌 누구야?"
TRIP = "도쿄 3박 4일 여행 일정표를 날짜별로 자세히 짜 줘. 다니면서 볼 거야."
ASSISTANT = r"비서|어시스턴트|에이전트|범용|도우미|챗봇|언어\s?모델|assistant|agent|chatbot|language model"
AS_ASSISTANT = r"비서|어시스턴트|도우미|assistant"
CHARACTER = r"캐릭터|표정|반려|컴패니언|companion|\[\[|\]\]"


@dataclass(frozen=True)
class Scene:
    item: int
    name: str
    said: str
    companion: bool = True
    device: bool = True  # the Windows companion app (file_download false); else the Web
    persona: str = ""
    talk: tuple[tuple[str, str], ...] = ()
    notes: tuple[Memory, ...] = ()
    must: tuple[str, ...] = ()  # patterns the shown answer has (case ignored)
    never: tuple[str, ...] = ()  # and does not have
    banmal: bool = False  # no sentence ends in 존댓말
    present: str | None = None  # item 5: "ok" or "full", what present_file does
    sentences: int | None = 4  # SPEAKING_RULE's bound; §9 item 1 asks short on the Web too


SCENES = [
    Scene(1, "no_persona_device", WHO, never=(ASSISTANT,)),
    Scene(1, "no_persona_web", WHO, device=False, never=(ASSISTANT,)),
    Scene(2, "switch_voice", "나 왔어. 발표 끝났다!", persona=PERSONA_B, talk=TALK_A, must=("대장",),
          never=("선생님",), banmal=True),
    Scene(2, "switch_name", "근데 너 이름이 뭐였지?", persona=PERSONA_B, talk=TALK_A, must=("유키",),
          never=("선생님", "히마리|HIMARI"), banmal=True),
    Scene(3, "memory_address", "나 왔어! 오늘 진짜 길었다.", persona=PERSONA_C, notes=NOTES, must=("캡틴",),
          never=("선배",), banmal=True),
    Scene(3, "memory_name", "너 이름이 뭐였더라?", persona=PERSONA_C, notes=NOTES, must=("소라",),
          never=("히마|HIMARI", "선배"), banmal=True),
    Scene(4, "off_who", WHO, companion=False, device=False, must=(AS_ASSISTANT,), never=(CHARACTER,),
          sentences=None),
    Scene(4, "off_tired", "오늘 좀 피곤하다.", companion=False, device=False, never=(CHARACTER,), sentences=None),
    Scene(5, "file_ready", TRIP, persona=PERSONA_A, present="ok"),
    Scene(5, "file_failed", TRIP, persona=PERSONA_A, present="full"),
]

SENTENCE = re.compile(r"[^.!?。？！…~\n]+[.!?。？！…~]*")
MARKDOWN = re.compile(r"```|^\s*(?:[-*•]|\d+\.)\s|^\s*#", re.MULTILINE)
POLITE = re.compile(r"(?:요|니다|[습입]니까|죠)$")
OTHER_SCRIPT = re.compile(r"[぀-ヺー-ヿ一-鿿]+")  # kana and Han (not the middle dot ・)
# Item 5. The file is ready, or they can get it (where, or how); it was made (fine once given, and when giving it
# failed: the file is in the folder); they got it, or it was sent or is on their computer (INTERFACE_RULE).
READY = re.compile(r"준비(?:했|됐|되었|\s?완료|해\s?(?:뒀|놨|두었|놓았|드렸)|돼\s?있|되어\s?있)|올려\s?(?:뒀|놨|두었|놓았|드렸)|올렸")
AVAILABLE = re.compile(
    r"(?:내려받|다운받|받)(?:으세요|으시면|으면|으실\s?수\s?있|을\s?수\s?있|아\s?보세요|아\s?보시면|아\s?보실\s?수\s?있|아\s?가)"
    r"|다운로드\s?(?:하세요|하시면|하면|하실\s?수\s?있|할\s?수\s?있|받으세요|받으시면|받으면|받으실\s?수\s?있|받을\s?수\s?있)"
    r"|열어\s?보(?:세요|시면|실\s?수\s?있)")
MADE = re.compile(r"(?:정리|작성)(?:했|해\s?(?:뒀|두었|놨|놓았|드렸))|만들(?:었|어\s?(?:뒀|두었|놨|놓았|드렸))"
                  r"|짜\s?(?:뒀|두었|놨|놓았|드렸)|짰")
WEB = re.compile(r"웹|web|브라우저", re.IGNORECASE)
GOT = re.compile(r"보냈|보내\s?드렸|전송(?:했|됐|되었|해\s?드렸)|받으셨|받았|(?:컴퓨터|PC)에\s?(?:있|저장|받)|바탕\s?화면"
                 r"|다운로드\s?폴더", re.IGNORECASE)
FAILED = re.compile(r"못|실패|안\s?(?:됐|돼|되)|않|문제|오류|에러|가득|꽉|공간|어려|없")


def event(sequence: int, payload: UserMessage | AssistantFinal, at: datetime) -> ConversationEvent:
    kind = EventKind.USER_MESSAGE if isinstance(payload, UserMessage) else EventKind.ASSISTANT_FINAL
    return ConversationEvent(EventId(f"e{sequence}"), ConversationId("c"), sequence, kind, payload, 1, at)


def request_options(model: str, effort: str | None) -> RequestOptions:
    """As the Driver sets them (``Driver._request_options``), from the model's declared capabilities
    (``OpenRouterAdapter.describe_capabilities``: an unlisted model has none)."""
    caps = KNOWN_CAPABILITIES.get(model, ProviderCapabilities(model_id=model))
    return RequestOptions(model, effort, provider_key="openrouter", image_input=caps.image_input,
                          screen_points=caps.image_input and caps.screen_points, speaks_first=caps.speaks_first,
                          free_wake=caps.speaks_first and caps.free_wake,
                          screen_tool=caps.image_input and caps.screen_tool)


def model_input(scene: Scene, model: str, effort: str | None, tools: tuple[ToolDefinition, ...] = (),
                folder: str | None = None) -> ModelInput:
    events: list[ConversationEvent] = []
    at = NOW - timedelta(hours=2)
    for user, assistant in scene.talk:
        events.append(event(len(events) + 1, UserMessage((TextBlock(user),)), at))
        events.append(event(len(events) + 1, AssistantFinal((TextBlock(assistant),)), at + timedelta(seconds=15)))
        at += timedelta(minutes=3)
    events.append(event(len(events) + 1, UserMessage((TextBlock(scene.said),)), NOW))
    if not scene.companion:  # the companion's presence is off with it (bootstrap/companion.py)
        presence = None
    elif scene.device:
        presence = PresenceView(("device", DEVICE), DEVICE, (DevicePresence(DEVICE, True, idle_minutes=0),))
    else:
        presence = PresenceView(("web", None), DEVICE, (DevicePresence(DEVICE, True, idle_minutes=2),))
    interface = (InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION, InterfaceCapabilities(file_download=False))
                 if scene.device else InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(file_download=True)))
    environments = (MACHINE,) if folder is not None else ()
    observations: dict[EnvironmentKind, EnvironmentObservation] = {}
    if folder is not None:  # what MachineEnvironment.observe tells
        observations = {MACHINE: EnvironmentObservation(ConnectionStatus.ONLINE, NOW, working_directory=folder,
                                                        writable=(folder,), readable=READ_SCOPE)}
    inputs = PromptInputs(
        instructions=base_instructions(scene.companion), tool_catalog=tools, events=tuple(events),
        tool_executions=(), snapshot=None, state_basis=PersistedStateBasis(len(events), 0, environments),
        observations=observations, request_options=request_options(model, effort), trigger_refs=(),
        # bootstrap/application.py: attachments with the machine folders, speaking first with the companion;
        # build_runtime adds memory and turn guidance.
        system=SystemSpec(companion=scene.companion, memory=True, attachments=True, turn_guidance=True,
                          proactive=scene.companion),
        companion_persona=scene.persona, memories=scene.notes, now=NOW.astimezone(ZoneInfo(ZONE)),
        time_zone=ZONE, presence=presence, interface=interface)
    return Composer().compose(inputs)


def redacted(text: str) -> str:
    return text.replace(KEY, "<key>") if KEY else text


async def ask(request: ModelInput) -> NormalizedModelOutput | str:
    """One answer; no response or a broken one is asked again once, then recorded as a failed answer."""
    adapter = OpenRouterAdapter(KEY, timeout=180)
    failed = ""
    for _ in range(2):
        outcome = await adapter.invoke_prepared(adapter.prepare_native_request(request))
        if not isinstance(outcome, NativeReceived):
            failed = f"<no response: {redacted(outcome.error_code)}>"
            continue
        try:
            return adapter.normalize(outcome)
        except ProviderError as error:
            failed = f"<provider error: {redacted(str(error))}>"
    return failed


def text_of(output: NormalizedModelOutput) -> str:
    blocks = output.blocks if isinstance(output, NormalizedFinal) else output.commentary
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


def shown(scene: Scene, raw: str) -> str:
    return (split_marks(raw, complete=True)[0] if scene.companion else raw).strip()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.findall(text) if re.search(r"[가-힣A-Za-z]", s)]


def polite(text: str) -> list[str]:
    return [s for s in sentences(text) if POLITE.search(re.sub(r"[^가-힣]+$", "", s))]


def speech_problems(scene: Scene, said: str) -> list[str]:
    found = []
    if not re.search(r"[가-힣]", said):
        found.append("not Korean")
    if (other := OTHER_SCRIPT.search(said)) is not None:  # COMPANION_BASE: the language the user writes in
        found.append(f"another language: {other.group(0)!r}")
    if scene.companion and ("[[" in said or "]]" in said):
        found.append("a broken mark")
    count = len(sentences(said))
    if scene.sentences is not None and count > scene.sentences:
        found.append(f"{count} sentences")
    if scene.device and MARKDOWN.search(said):
        found.append("code, a list or a heading in speech")
    return found


def problems(scene: Scene, raw: str) -> list[str]:
    said = shown(scene, raw)
    found = speech_problems(scene, said)
    found += [f"no {pattern!r}" for pattern in scene.must if not re.search(pattern, said, re.IGNORECASE)]
    for pattern in scene.never:
        if (seen := re.search(pattern, said, re.IGNORECASE)) is not None:
            found.append(f"says {seen.group(0)!r}")
    if scene.banmal and (formal := polite(said)):
        found.append(f"존댓말: {formal[0]!r}")
    return found


# --- item 5: the file Tools, run ----------------------------------------------------------------------


class _Store:
    """``present_file``'s Data side: nothing given yet in its window; the bytes kept, or the store full."""

    def __init__(self, full: bool) -> None:
        self._full = full

    async def presented_bytes_since(self, scope: TrustedScope, since: datetime) -> int:
        return 0

    async def put(self, principal: PrincipalId, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredBlob:
        data = b"".join([chunk async for chunk in chunks])
        if self._full:
            raise DataError(DataErrorCode.STORE_FULL, "no room")
        digest = hashlib.sha256(data).hexdigest()
        return StoredBlob(blob_key(principal, digest), digest, len(data))


@dataclass
class Step:
    """One Run of the turn: its text (commentary, or the answer) and the calls it made, with their results."""

    text: str
    final: bool
    calls: list[dict[str, object]] = field(default_factory=list)

    def record(self) -> dict[str, object]:
        return {"text": self.text, "final": self.final, "calls": self.calls}


def brief(arguments: Mapping[str, JsonValue]) -> dict[str, object]:
    plain = thaw(arguments)
    assert isinstance(plain, dict)
    return {str(k): v if not isinstance(v, str) or len(v) <= 160 else f"{v[:160]}… ({len(v)} chars)"
            for k, v in plain.items()}


async def run_call(handlers: Mapping[str, ToolHandler], call: ToolCall, scope: TrustedScope
                   ) -> tuple[ModelToolResult, str]:
    """As the Dispatcher runs one call (``Dispatcher._run_one``), without its records."""
    handler = handlers.get(call.tool_id)
    if handler is None:
        return ModelToolResult((TextBlock(f"unknown tool {call.tool_id}"),), is_error=True), "denied"
    violations = validate(call.arguments, handler.definition.input_schema)
    if violations:
        return ModelToolResult((TextBlock("invalid arguments: " + "; ".join(violations)),), is_error=True), "denied"
    outcome = await handler.run(ToolInvocation(call.tool_id, call.call_id, call.arguments), ToolContext(scope=scope))
    return outcome.model_result, outcome.status.value


async def file_turn(scene: Scene, model: str, effort: str | None, root: Path, conversation: str) -> list[Step]:
    store = _Store(full=scene.present == "full")
    env = MachineEnvironment(str(root), files=ToolFiles(cast(BlobStore, store), cast(FileCatalog, store)))
    handlers = {str(h.definition.tool_id): h for h in env.tools()}
    scope = TrustedScope(PrincipalId("operator"), ConversationId(conversation))
    folder = (await env.places(scope)).cwd
    request = model_input(scene, model, effort, tuple(h.definition for h in env.tools()), folder)
    steps: list[Step] = []
    for run in range(MAX_STEPS):
        output = await ask(request)
        if isinstance(output, str):
            steps.append(Step(output, final=True))
            break
        if isinstance(output, NormalizedFinal):
            steps.append(Step(text_of(output), final=True))
            break
        step = Step(text_of(output), final=False)
        results: dict[CallId, ModelToolResult] = {}
        for call in sorted(output.calls, key=lambda c: c.ordinal):  # one Conversation's Tools run one at a time
            result, status = await run_call(handlers, call, scope)
            results[call.call_id] = result
            step.calls.append({"tool": call.tool_id, "arguments": brief(call.arguments), "status": status,
                               "result": "".join(b.text for b in result.blocks if isinstance(b, TextBlock))})
        steps.append(step)
        continuation = None if output.continuation is None else ProviderContinuation(
            "openrouter", model, output.continuation)
        group = ToolGroupTurn(RunId(f"r{run}"), output.commentary, output.calls, results, continuation)
        request = replace(request, conversation=(*request.conversation, group))
    return steps


def file_problems(scene: Scene, steps: list[Step]) -> list[str]:
    found = []
    final = steps[-1] if steps and steps[-1].final else None
    presents = [(i, c) for i, s in enumerate(steps) for c in s.calls if c["tool"] == "machine/present_file"]
    if not presents:
        found.append("present_file not called")
    ok = [i for i, c in presents if c["status"] == ToolStatus.SUCCEEDED.value]
    if scene.present == "ok" and presents and not ok:
        found.append("present_file never succeeded")
    # What the user may have heard before a success was known: the Runs up to the one that called it. When giving
    # it failed, "I made it" stays true (the file is in the folder); that they can get it does not.
    before = steps[: ok[0] + 1] if ok else steps
    for step in before:
        said = shown(scene, step.text)
        claims = (READY, AVAILABLE, GOT) if scene.present == "full" else (READY, AVAILABLE, GOT, MADE)
        if (seen := next((m for c in claims if (m := c.search(said)) is not None), None)) is not None:
            where = "though present_file failed" if scene.present == "full" else "before present_file succeeded"
            found.append(f"said {seen.group(0)!r} {where}")
    if final is None:
        return [*found, "no answer"]
    said = shown(scene, final.text)
    found += speech_problems(scene, said)
    if scene.present == "ok":
        if not (WEB.search(said) and AVAILABLE.search(said)):
            found.append("does not tell them to get it on the Web")
        if (seen := GOT.search(said)) is not None:
            found.append(f"says {seen.group(0)!r}")
    elif not FAILED.search(said):
        found.append("does not say it could not")
    return found


# --- the run -------------------------------------------------------------------------------------------


def judged(row: dict[str, Any]) -> dict[str, Any]:
    """A saved row judged again by the checks here; a turn that gave no answer (no response, a Tool call where
    none was offered) keeps its verdict."""
    scene = next(s for s in SCENES if s.name == row["scene"])
    kept = [str(p) for p in row["problems"]]
    if scene.present is not None:
        found = file_problems(scene, [Step(s["text"], s["final"], s["calls"]) for s in row["steps"]])
    elif any(p.startswith("<") or p == "called a tool" for p in kept):
        found = kept
    else:
        found = problems(scene, row["answer"])
    return {**row, "problems": found, "pass": not found}


def replayed(spec: str) -> list[dict[str, Any]]:
    assert REPLAY is not None
    with open(REPLAY, encoding="utf-8") as saved:
        rows = [json.loads(line) for line in saved if line.strip()]
    return [judged(r) for r in rows if r["model"] == spec and (not ONLY or r["scene"] in ONLY)]


def report(model: str, rows: list[dict[str, Any]]) -> None:
    out = os.environ.get("HIMARI_LIVE_PERSONA_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as kept:
            for row in rows:
                kept.write(json.dumps(row, ensure_ascii=False) + "\n")
    for row in rows:
        answer = split_marks(str(row["answer"]), complete=True)[0].replace("\n", " ")
        print(f"{model} {row['scene']} #{row['attempt']}: {'ok' if row['pass'] else row['problems']} — {answer[:140]!r}")


@pytest.mark.parametrize("spec", MODELS)
def test_the_companion_takes_on_the_chosen_persona(spec: str, tmp_path: Path) -> None:
    model, _, effort_text = spec.partition("=")
    effort = effort_text or None
    (tmp_path / "data").mkdir(mode=0o700)
    root = tmp_path / "data" / "conversations"
    scenes = [s for s in SCENES if not ONLY or s.name in ONLY]
    started = time.monotonic()

    async def one(scene: Scene, attempt: int, gate: asyncio.Semaphore) -> dict[str, Any]:
        async with gate:
            if scene.present is None:
                output = await ask(model_input(scene, model, effort))
                if isinstance(output, str):
                    steps, found = [Step(output, final=True)], [output]
                else:
                    steps = [Step(text_of(output), final=isinstance(output, NormalizedFinal))]
                    found = problems(scene, steps[0].text) + ([] if steps[0].final else ["called a tool"])
            else:
                steps = await file_turn(scene, model, effort, root, f"{scene.name.replace('_', '-')}-{attempt}")
                found = file_problems(scene, steps)
        row: dict[str, Any] = {
            "model": spec, "item": scene.item, "scene": scene.name, "attempt": attempt,
            "interface": "windows_companion" if scene.device else "web", "companion": scene.companion,
            "answer": steps[-1].text if steps else "", "problems": found, "pass": not found}
        if scene.present is not None:
            row["steps"] = [s.record() for s in steps]
        return row

    async def every() -> list[dict[str, Any]]:
        gate = asyncio.Semaphore(PARALLEL)
        return list(await asyncio.gather(*(one(s, a, gate) for s in scenes for a in range(1, TRIES + 1))))

    rows = replayed(spec) if REPLAY else asyncio.run(every())
    report(spec, rows)
    short = []
    for item in sorted({int(r["item"]) for r in rows}):
        mine = [r for r in rows if r["item"] == item]
        ok = sum(1 for r in mine if r["pass"])
        print(f"{spec} item {item}: {ok}/{len(mine)}")
        if ok < len(mine) * BAR:
            short.append((item, f"{ok}/{len(mine)}"))
    print(f"{spec}: {time.monotonic() - started:.0f}s")
    assert not short, (short, [(r["scene"], r["problems"]) for r in rows if not r["pass"]])
