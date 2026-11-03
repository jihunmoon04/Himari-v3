"""Synthetic screens and verdicts for the opt-in V3-P91 model check; no credentials or I/O."""

from __future__ import annotations

import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from himari.common import (
    AttachmentRef, CallId, ConversationId, EventId, FileId, ModelToolResult, ProviderContinuation,
    RunId, TextBlock, ToolCall, ToolId, TrustedScope, split_marks,
)
from himari.data.api import (
    ConversationEvent, EventKind, EventPayload, PersistedStateBasis, UserMessage, Wake, WakeKind, WakeReason,
)
from himari.prompt.api import (
    AttachmentInfo, DevicePresence, ModelInput, NowDoing, PresenceView, PromptInputs, RequestOptions,
    SystemSpec, ToolGroupTurn,
)
from himari.prompt.composer import Composer
from himari.prompt.system import BASE_INSTRUCTIONS
from himari.provider.api import NormalizedFinal, NormalizedModelOutput, NormalizedToolCalls
from himari.provider.openrouter import KNOWN_CAPABILITIES
from himari.tool.api import ScreenRefusal
from himari.tool.screen import CAPTURED, LOOK_AT_SCREEN, NO_DEVICE, ScreenTool

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=UTC)
WIDTH, HEIGHT = 1280, 720
PERSONA = "이름은 히마리. 사용자를 선생님이라고 부르고 존댓말로 다정하고 짧게 말한다."


class NoDevice:
    async def look(self, scope: TrustedScope, device_id: str, *, free_wake: bool = False) -> bytes | ScreenRefusal:
        return ScreenRefusal.AWAY


SCREEN = ScreenTool(NoDevice()).definition


@dataclass(frozen=True)
class Picture:
    name: str
    png: bytes
    expected: str = ""
    target: tuple[int, int, int, int] | None = None

    @property
    def info(self) -> AttachmentInfo:
        return AttachmentInfo("screen-20261103-193000.png", None, "image/png", len(self.png), view_state="ready",
                              view_key=self.name, view_media_type="image/png", view_width=WIDTH,
                              view_height=HEIGHT, view_bytes=len(self.png))


def picture(name: str) -> Picture:
    """English labels avoid machine-specific Korean fonts; answers still use Korean."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (235, 238, 244))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=26)
    draw.rectangle((100, 80, 1180, 640), fill="white", outline=(130, 140, 160), width=2)

    def line(y: int, text: str) -> None:
        draw.text((150, y), text, fill=(20, 30, 50), font=font)

    target = None
    expected = ""
    if name == "theme":
        line(115, "Editor Settings")
        line(190, "Current theme: Ocean")
        line(255, "Choose a theme:")
        draw.rectangle((690, 300, 900, 350), fill=(195, 225, 255))
        draw.text((730, 308), "Light", fill="black", font=font)
        target, expected = (690, 300, 900, 350), "Ocean"
    elif name == "error":
        line(115, "Visual Studio Code - sample.py")
        line(210, "itemCount = 3")
        draw.rectangle((140, 270, 720, 325), fill=(255, 220, 220))
        line(280, "result = item_count + 1")
        line(450, "NameError: name 'item_count' is not defined")
        target, expected = (140, 270, 720, 325), "item_count"
    elif name == "work":
        line(115, "Visual Studio Code - sample.py")
        line(220, "def greeting(name):")
        line(270, "    return f'Hello, {name}'")
        line(450, "Tests: 24 passed, 0 failed")
    elif name == "writing":
        line(115, "Document Editor - Project Notes")
        line(230, "Outline complete")
        line(300, "Draft saved successfully")
        line(450, "Word count: 1800")
    elif name == "call":
        line(115, "Google Meet - Team review")
        line(220, "Meeting in progress - 4 participants")
        line(300, "Microphone: ON     Camera: ON")
        line(440, "Presenting to everyone")
    elif name == "match":
        line(115, "League of Legends")
        line(220, "RANKED MATCH IN PROGRESS")
        line(300, "Game time: 23:14     Team fight")
    else:
        raise ValueError(name)
    out = io.BytesIO()
    image.save(out, "PNG")
    return Picture(name, out.getvalue(), expected, target)


def model_input(model: str, text: str = "", *, free: bool = False, web: bool = False,
                attached: Picture | None = None, doing: NowDoing | None = None) -> ModelInput:
    caps = KNOWN_CAPABILITIES[model]
    options = RequestOptions(model, reasoning_effort="low" if "low" in caps.reasoning_efforts else None,
                             provider_key="openrouter", image_input=True, screen_points=caps.screen_points,
                             speaks_first=caps.speaks_first, free_wake=caps.free_wake, screen_tool=True,
                             max_output_tokens=4096)
    payload: EventPayload
    if free:
        payload = Wake("w1", (WakeReason(WakeKind.FREE, group="work", minutes=90, step=3),), "d1")
        kind = EventKind.WAKE
    else:
        blocks = (TextBlock(text),) if attached is None else (
            TextBlock(text), AttachmentRef(FileId("screen"), 1, screen=True))
        payload = UserMessage(blocks)
        kind = EventKind.USER_MESSAGE
    event = ConversationEvent(EventId("e1"), ConversationId("c1"), 1, kind, payload, 1, NOW)
    presence = PresenceView(("web", None) if web else ("device", "Desktop"), "Desktop",
                            (DevicePresence("Desktop", True, idle_minutes=0),), now_doing=doing)
    inputs = PromptInputs(
        instructions=BASE_INSTRUCTIONS, tool_catalog=(SCREEN,), events=(event,), tool_executions=(),
        snapshot=None, state_basis=PersistedStateBasis(1, 0, (), (), ()), observations=(),
        request_options=options, trigger_refs=(), system=SystemSpec(companion=True, attachments=True, proactive=True),
        companion_persona=PERSONA, now=NOW, presence=presence, time_zone="Asia/Seoul",
        attachments={} if attached is None else {"screen#1": attached.info},
    )
    return Composer().compose(inputs)


def after_look(before: ModelInput, output: NormalizedToolCalls | None = None,
               found: Picture | None = None) -> ModelInput:
    """Replay an actual call/continuation, or seed one to isolate the post-look verdict."""
    calls = output.calls if output is not None else (ToolCall(CallId("look1"), 0, ToolId(LOOK_AT_SCREEN), {}),)
    blocks = (TextBlock(NO_DEVICE),) if found is None else (
        TextBlock(CAPTURED), AttachmentRef(FileId("screen"), 1, screen=True))
    continuation = None if output is None or output.continuation is None else ProviderContinuation(
        "openrouter", before.request_options.model_id, output.continuation)
    group = ToolGroupTurn(RunId("screen-run"), () if output is None else output.commentary, calls,
                          {c.call_id: ModelToolResult(blocks, is_error=found is None) for c in calls}, continuation)
    return replace(before, conversation=(*before.conversation, group),
                   attachments={} if found is None else {"screen#1": found.info})


def output_text(output: NormalizedModelOutput) -> str:
    blocks = output.blocks if isinstance(output, NormalizedFinal) else output.commentary
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


def call_problems(output: NormalizedModelOutput) -> list[str]:
    if not isinstance(output, NormalizedToolCalls):
        return ["did not call screen tool"]
    return [] if len(output.calls) == 1 and output.calls[0].tool_id == LOOK_AT_SCREEN and not output.calls[0].arguments \
        else ["expected one argument-free screen call"]


def final_problems(output: NormalizedModelOutput) -> list[str]:
    if not isinstance(output, NormalizedFinal):
        return ["called a tool instead of answering"]
    plain = split_marks(output_text(output), complete=True)[0].strip()
    return [] if plain and re.search(r"[가-힣]", plain) else ["no Korean answer"]


def refusal_problems(output: NormalizedModelOutput) -> list[str]:
    problems = final_problems(output)
    plain = output_text(output)
    unavailable = (
        r"(?:볼|보여줄|확인할|접근할|가져올|불러올|캡처할|표시할) 수(?:가)? 없|"
        r"(?:확인|접근|캡처)(?:이|하기가?)? 어려|"
        r"(?:가져오|불러오|확인하|보|캡처하)지 못|"
        r"(?:보이지 않|안 (?:보|잡)|못 (?:보|봐|가져|확인))|"
        r"볼 수 있는 (?:기기|상태).{0,15}(?:아니|없)|화면.{0,20}(?:꺼져|연결되지)"
    )
    if not re.search(unavailable, plain):
        problems.append("did not acknowledge unavailable screen")
    return problems


def requirements(model: str) -> dict[str, tuple[int, int]]:
    groups = {"call": (5, 6), "no_call": (5, 6), "read": (5, 6), "refuse": (3, 3)}
    if KNOWN_CAPABILITIES[model].free_wake:
        groups.update({"choice": (3, 3), "speak": (5, 6), "pass": (6, 6)})
    return groups


def review_results(recorded: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replay saved answers without paid calls; preserve original verdicts when the grader changed.

    The first sweep counted dots in filenames as sentences and missed Korean refusal variants.
    Call/reading verdicts stay as captured: the original trace did not store call arguments.
    """
    rows = [dict(row) for row in recorded]
    for row in rows:
        if row["group"] not in ("speak", "pass", "refuse", "choice") or "answer" not in row:
            continue
        if row.get("calls"):
            continue  # Original tool-call validation includes its arguments; do not guess them.
        output = NormalizedFinal((TextBlock(row["answer"]),))
        group = row["group"]
        problems = refusal_problems(output) if group == "refuse" else choice_problems(output) if group == "choice" \
            else free_problems(output, speak=group == "speak")
        if problems != row["problems"]:
            row.setdefault("initial_problems", row["problems"])
        row.update(problems=problems, **{"pass": not problems, "grader_revision": 2})
    # A missed request is a failed read, not a smaller denominator. No paid call was made for these rows.
    for row in tuple(rows):
        if row["group"] != "call":
            continue
        if not any(r["model"] == row["model"] and r["scene"] == row["scene"] + "_result"
                   and r["attempt"] == row["attempt"] for r in rows):
            rows.append({"model": row["model"], "scene": row["scene"] + "_result", "group": "read",
                         "attempt": row["attempt"], "pass": False, "problems": ["no valid call to continue"],
                         "not_requested": True})
    return rows


def failed_groups(rows: Sequence[Mapping[str, Any]], models: Sequence[str]) -> list[tuple[str, str, int, int]]:
    failures = []
    for model in models:
        for group, (needed, total) in requirements(model).items():
            mine = [r for r in rows if r["model"] == model and r["group"] == group]
            hits = sum(bool(row["pass"]) for row in mine)
            if len(mine) != total or hits < needed:
                failures.append((model, group, hits, len(mine)))
    return failures


def choice_problems(output: NormalizedModelOutput) -> list[str]:
    if isinstance(output, NormalizedToolCalls):
        return call_problems(output)
    plain = split_marks(output_text(output), complete=True)[0].strip()
    return free_problems(output, speak=plain != "[[pass]]")


def screen_problems(output: NormalizedModelOutput, found: Picture, points: bool) -> list[str]:
    problems = final_problems(output)
    raw = output_text(output)
    if found.expected.lower() not in raw.lower():
        problems.append("missed screen-only label")
    if points and found.target is not None:
        moves = split_marks(raw, complete=True)[2]
        left, top, right, bottom = found.target
        if not moves or not (left - 24 <= moves[0][1] <= right + 24 and top - 24 <= moves[0][2] <= bottom + 24):
            problems.append("missing/off-target move")
    return problems


def free_problems(output: NormalizedModelOutput, *, speak: bool) -> list[str]:
    if not isinstance(output, NormalizedFinal):
        return ["called a tool after looking"]
    raw = output_text(output)
    plain = split_marks(raw, complete=True)[0].strip()
    if not speak:
        return [] if plain == "[[pass]]" else ["interrupted call/match"]
    if plain in ("", "[[pass]]"):
        return ["passed a speaking scene"]
    shown = split_marks(raw.replace("[[pass]]", ""), complete=True)[0]
    problems = final_problems(NormalizedFinal((TextBlock(shown),)))
    # A filename (sample.py) or decimal is not a sentence boundary.
    sentences = re.sub(r"(?<=[A-Za-z0-9_])\.(?=[A-Za-z0-9_])", "·", shown)
    if len([s for s in re.findall(r"[^.!?。？！…~]+[.!?。？！…~]*", sentences)
            if s.strip(" \n.!?。？！…~")]) > 4:
        problems.append("over four sentences")
    if shown.count("?") + shown.count("？") > 1:
        problems.append("more than one question")
    if any(word in shown.lower() for word in ("wake", "웨이크", "서버", "reasons", "[[", "]]")):
        problems.append("internal/broken mark")
    return problems
