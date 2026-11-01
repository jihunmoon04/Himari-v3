"""Live check of points on a screen (V3-P84 §5, §10): opt-in, ``HIMARI_LIVE=1``
plus the key. For each model in ``HIMARI_LIVE_SCREEN_MODELS``, ten asks on
1280×720 screens (a small button, a screen corner, a menu word, a shape, a
field), sent as HIMARI sends a screen; a ``[[move:X,Y]]`` must fall on the
target (24 pixels to spare) in at least eight. A model gets ``screen_points``
in the declared list only when it passes; the result goes to
docs/openrouter-model-catalog.md. With ``HIMARI_LIVE_SCREEN_OUT`` set to a
file, every ask's answer is appended to it as one JSON line (PR #128 review C1)."""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from testkit import image_model_input

from himari.bootstrap import read_env_file
from himari.common import AttachmentRef, EventId, FileId, TextBlock, split_marks
from himari.prompt.api import AttachmentInfo, UserTurn
from himari.prompt.system import companion_section
from himari.provider.api import NativeReceived, NormalizedFinal, ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = [m for m in os.environ.get("HIMARI_LIVE_SCREEN_MODELS", "openai/gpt-6-luna").split(",") if m]
WIDTH, HEIGHT, SPARE, NEEDED = 1280, 720, 24, 8

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")

type Box = tuple[int, int, int, int]


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # an older Pillow: the small bitmap font
        return ImageFont.load_default()


def desktop() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), (236, 238, 242))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 44), fill=(250, 250, 252))
    draw.rectangle((140, 90, 1140, 620), fill=(255, 255, 255), outline=(200, 200, 205))
    for row in range(8):
        draw.rectangle((180, 130 + row * 50, 900, 150 + row * 50), fill=(225, 227, 232))
    return image, draw


def labelled(draw: ImageDraw.ImageDraw, box: Box, text: str, fill: tuple[int, int, int]) -> Box:
    draw.rectangle(box, fill=fill, outline=(120, 120, 130))
    draw.text((box[0] + 10, box[1] + 6), text, fill=(20, 20, 20), font=font(18))
    return box


SCENES = ("button", "corner", "menu", "shape", "field")


def scenes() -> list[tuple[str, bytes, Box]]:
    """(what to ask, the PNG, the target's box), in the order of SCENES."""
    out = []
    image, draw = desktop()
    box = labelled(draw, (1000, 560, 1090, 594), "Save", (80, 140, 240))
    labelled(draw, (890, 560, 980, 594), "Cancel", (230, 230, 235))
    out.append(("Save 단추를 가리켜 줘.", image, box))
    image, draw = desktop()
    box = labelled(draw, (1170, 6, 1272, 38), "Settings", (245, 245, 248))
    out.append(("오른쪽 위의 Settings를 가리켜 줘.", image, box))
    image, draw = desktop()
    boxes = [labelled(draw, (8 + n * 80, 8, 80 + n * 80, 38), word, (250, 250, 252))
             for n, word in enumerate(("File", "Edit", "View", "Help"))]
    out.append(("메뉴의 View를 가리켜 줘.", image, boxes[2]))
    image, draw = desktop()
    for n in range(6):
        draw.rectangle((300 + n * 110, 300, 360 + n * 110, 360), fill=(160, 160, 165))
    draw.ellipse((740, 420, 790, 470), fill=(220, 30, 30))
    out.append(("빨간 동그라미를 가리켜 줘.", image, (740, 420, 790, 470)))
    image, draw = desktop()
    draw.text((420, 236), "Search", fill=(60, 60, 60), font=font(18))
    box = (500, 230, 860, 262)
    draw.rectangle(box, fill=(255, 255, 255), outline=(90, 90, 100), width=2)
    out.append(("검색 입력칸을 가리켜 줘.", image, box))
    pngs = []
    for ask, picture, target in out:
        data = io.BytesIO()
        picture.save(data, "PNG")
        pngs.append((ask, data.getvalue(), target))
    return pngs


def point(model: str, ask: str, png: bytes) -> tuple[int, int] | None:
    key = "s" * 64
    caps = replace(KNOWN_CAPABILITIES.get(model, ProviderCapabilities(model)), image_input=True, screen_points=True)
    adapter = OpenRouterAdapter(KEY, capabilities={model: caps}, blobs={key: png}.__getitem__)
    info = AttachmentInfo("screen-20261030-120000.png", None, "image/png", len(png), view_state="ready", view_key=key,
                          view_media_type="image/png", view_width=WIDTH, view_height=HEIGHT, view_bytes=len(png))
    turn = UserTurn(EventId("e"), (TextBlock(ask), AttachmentRef(FileId("f"), 1, screen=True)))
    model_input = image_model_input([turn], {"f#1": info}, image_input=True, screen_points=True, model=model)
    model_input = replace(model_input, stable_prefix=("You are HIMARI.", companion_section(True)))  # V3-P89 §3.5
    outcome = asyncio.run(adapter.invoke_prepared(adapter.prepare_native_request(model_input)))
    assert isinstance(outcome, NativeReceived), outcome
    output = adapter.normalize(outcome)
    assert isinstance(output, NormalizedFinal), output
    text = "".join(b.text for b in output.blocks if isinstance(b, TextBlock))
    moves = split_marks(text, complete=True)[2]
    return (moves[0][1], moves[0][2]) if moves else None


def inside(found: tuple[int, int] | None, box: Box) -> bool:
    return found is not None and box[0] - SPARE <= found[0] <= box[2] + SPARE and \
        box[1] - SPARE <= found[1] <= box[3] + SPARE


@pytest.mark.parametrize("model", MODELS)
def test_a_model_points_where_it_is_asked(model: str) -> None:
    results = [(scene, ask, box, found, inside(found, box))
               for scene, (ask, png, box) in list(zip(SCENES, scenes(), strict=True)) * 2
               for found in [point(model, ask, png)]]
    out = os.environ.get("HIMARI_LIVE_SCREEN_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as kept:
            for scene, ask, box, found, hit in results:
                kept.write(json.dumps({"model": model, "scene": scene, "ask": ask, "target": box,
                                       "point": found, "hit": hit}, ensure_ascii=False) + "\n")
    hits = sum(1 for *_, hit in results if hit)
    print(f"{model}: {hits}/10, missed {[scene for scene, *_, hit in results if not hit]}")
    assert hits >= NEEDED, results
