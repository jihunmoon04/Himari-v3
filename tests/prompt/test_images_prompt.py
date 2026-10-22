"""Images in the model input (V3-P67 §6): the plan, the token count, and the
reasons a line gives."""

from __future__ import annotations

from dataclasses import replace

from himari.common import AttachmentRef, CallId, EventId, FileId, ModelToolResult, RunId, TextBlock, ToolCall, ToolId
from himari.data.api import MAX_IMAGE_BYTES_PER_REQUEST, MAX_IMAGES_PER_REQUEST
from himari.prompt.api import (
    NO_IMAGE_MODEL,
    OVER_PLAN,
    AttachmentInfo,
    ImagePlan,
    ToolGroupTurn,
    UserTurn,
    attachment_line,
    image_plan,
    image_tokens,
)
from himari.prompt.compaction import part_tokens, tokens_of_text

from testkit import image_model_input as model_input

READY = AttachmentInfo("p.png", "/workspace/u/p.png", "image/png", 9000, view_state="ready", view_key="k" * 64,
                       view_media_type="image/jpeg", view_width=1568, view_height=1176, view_bytes=200_000)


def ref(n: int) -> AttachmentRef:
    return AttachmentRef(FileId(f"file_{n}"), 1)


def test_the_plan_keeps_the_newest_within_count_and_bytes() -> None:
    turns = [UserTurn(EventId(f"e{n}"), (ref(n),)) for n in range(MAX_IMAGES_PER_REQUEST + 5)]
    infos = {f"file_{n}#1": READY for n in range(MAX_IMAGES_PER_REQUEST + 5)}
    plan = image_plan(model_input(turns, infos, image_input=True))
    assert len(plan.shown) == MAX_IMAGES_PER_REQUEST and "file_0#1" not in plan.shown  # the oldest go
    big = replace(READY, view_bytes=MAX_IMAGE_BYTES_PER_REQUEST // 2 + 1)
    plan = image_plan(model_input(turns[:3], {f"file_{n}#1": big for n in range(3)}, image_input=True))
    assert plan.shown == frozenset({"file_2#1"})  # bytes: only the newest fits (review B3)


def test_lines_say_why_an_image_is_not_shown() -> None:
    infos = {"file_1#1": READY, "file_2#1": replace(READY, view_state="view_expired"),
             "file_3#1": replace(READY, view_state="no_sandbox"), "file_4#1": replace(READY, view_state="not_image")}
    shown = ImagePlan(True, frozenset({"file_1#1"}))
    assert '"not_shown"' not in attachment_line(ref(1), infos, shown)
    assert '"not_shown": "expired"' in attachment_line(ref(2), infos, shown)
    assert '"not_shown": "images cannot be prepared on this server"' in attachment_line(ref(3), infos, shown)
    assert '"not_shown"' not in attachment_line(ref(4), infos, shown)  # not an image: nothing to say
    assert f'"not_shown": "{NO_IMAGE_MODEL}"' in attachment_line(ref(1), infos, ImagePlan(False))
    assert f'"not_shown": "{OVER_PLAN}"' in attachment_line(ref(1), infos, ImagePlan(True))


def test_image_tokens_take_the_larger_count() -> None:
    assert image_tokens(READY) == max(-(-1568 * 1176 // 750), 85 + 170 * 2 * 3)
    small = replace(READY, view_width=100, view_height=100)
    assert image_tokens(small) == max(-(-100 * 100 // 750), 85 + 170)


def test_the_estimate_counts_shown_images_and_the_tool_image_message() -> None:
    infos = {"file_1#1": READY}
    plan = ImagePlan(True, frozenset({"file_1#1"}))
    turn = UserTurn(EventId("e"), (TextBlock("look"), ref(1)))
    assert part_tokens(turn, infos, plan) - part_tokens(turn, infos, ImagePlan(False)) >= image_tokens(READY) - 20
    group = ToolGroupTurn(RunId("r"), (), (ToolCall(CallId("c"), 0, ToolId("x/chart"), {}),),
                          {CallId("c"): ModelToolResult((TextBlock("chart"), ref(1)))})
    with_image = part_tokens(group, infos, plan)
    without = part_tokens(group, infos, ImagePlan(False))
    assert with_image - without >= image_tokens(READY) + tokens_of_text("Images from the tool results above:")
