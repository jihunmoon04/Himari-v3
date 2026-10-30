"""Images in the model input (V3-P67 §6): the plan, the token count, and the
reasons a line gives."""

from __future__ import annotations

from dataclasses import replace

from himari.common import AttachmentRef, CallId, EventId, FileId, ModelToolResult, RunId, TextBlock, ToolCall, ToolId
from himari.data.api import MAX_IMAGE_BYTES_PER_REQUEST, MAX_IMAGES_PER_REQUEST
from himari.prompt.api import (
    NO_IMAGE_MODEL,
    OLDER_SCREEN,
    OVER_PLAN,
    SCREENS_SHOWN,
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


# --- screens (V3-P84 §5) ---------------------------------------------------------------------

SCREEN = replace(READY, name="screen-20261030-120000.png", path=None, view_width=1280, view_height=720)


def screen(n: int) -> AttachmentRef:
    return AttachmentRef(FileId(f"file_{n}"), 1, screen=True)


def test_only_the_latest_two_screens_are_images() -> None:
    turns = [UserTurn(EventId(f"e{n}"), (screen(n),)) for n in range(4)] + [UserTurn(EventId("e9"), (ref(9),))]
    infos = {**{f"file_{n}#1": SCREEN for n in range(4)}, "file_9#1": READY}
    plan = image_plan(model_input(turns, infos, image_input=True))
    assert plan.shown == frozenset({"file_3#1", "file_2#1", "file_9#1"})  # other images are not counted as screens
    assert plan.older_screens == frozenset({"file_0#1", "file_1#1"})
    assert f'"not_shown": "{OLDER_SCREEN}"' in attachment_line(screen(0), infos, plan)
    assert SCREENS_SHOWN == 2


def test_a_screen_line_says_screen_and_its_size_only_to_a_model_that_points() -> None:
    infos = {"file_1#1": SCREEN}
    turns = [UserTurn(EventId("e1"), (TextBlock("이거 봐"), screen(1)))]
    pointing = image_plan(model_input(turns, infos, image_input=True, screen_points=True))
    line = attachment_line(screen(1), infos, pointing)
    assert '"screen": true' in line and '"width": 1280' in line and '"height": 720' in line
    assert '"path"' not in line  # never placed in the Workspace (§4.2)
    looking = image_plan(model_input(turns, infos, image_input=True))
    line = attachment_line(screen(1), infos, looking)
    assert '"screen": true' in line and '"width"' not in line
    blind = attachment_line(screen(1), infos, image_plan(model_input(turns, infos)))
    assert '"width"' not in blind and f'"not_shown": "{NO_IMAGE_MODEL}"' in blind
    assert attachment_line(ref(1), {"file_1#1": READY}, pointing) == attachment_line(
        ref(1), {"file_1#1": READY}, ImagePlan(True, frozenset({"file_1#1"})))  # other lines do not change


def test_a_newer_screen_that_cannot_be_seen_lets_no_older_one_in() -> None:
    """PR #126 review B1: screens count by their order, shown or not; only the latest gets a size."""
    turns = [UserTurn(EventId(f"e{n}"), (screen(n),)) for n in range(3)]
    for state in ("no_sandbox", "view_expired"):
        infos = {"file_0#1": SCREEN, "file_1#1": SCREEN, "file_2#1": replace(SCREEN, view_state=state)}
        plan = image_plan(model_input(turns, infos, image_input=True, screen_points=True))
        assert plan.shown == frozenset({"file_1#1"}) and plan.older_screens == frozenset({"file_0#1"})
        assert plan.latest_screen == "file_2#1"
        assert '"width"' not in attachment_line(screen(1), infos, plan)  # seen, but moves are not about it
        assert '"width"' not in attachment_line(screen(2), infos, plan)  # moves are about it, but it is not seen
        assert f'"not_shown": "{OLDER_SCREEN}"' in attachment_line(screen(0), infos, plan)
    infos = {f"file_{n}#1": SCREEN for n in range(3)}
    plan = image_plan(model_input(turns, infos, image_input=True, screen_points=True))
    assert '"width": 1280' in attachment_line(screen(2), infos, plan)
    assert '"width"' not in attachment_line(screen(1), infos, plan)
