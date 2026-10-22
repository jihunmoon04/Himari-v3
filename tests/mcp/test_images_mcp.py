"""MCP image content (V3-P67 §5): handed to the Dispatcher, not a placeholder."""

from __future__ import annotations

import base64

from himari.mcp.tools import render_result, result_images
from himari.tool.api import MAX_IMAGE_BASE64_CHARS


def test_images_go_out_as_images_and_leave_the_text() -> None:
    png = base64.b64encode(b"\x89PNG fake").decode()
    result = {"content": [{"type": "text", "text": "a chart"}, {"type": "image", "data": png, "mimeType": "image/png"},
                          {"type": "audio", "data": "AA==", "mimeType": "audio/wav"}]}
    images = result_images(result)
    assert [(i.data, i.media_type) for i in images] == [(b"\x89PNG fake", "image/png")]
    assert render_result(result, shown_images=len(images)) == "a chart\n[audio content omitted (audio/wav)]"
    assert "[image content omitted" in render_result(result)  # without images shown: as before


def test_an_unusable_image_stays_a_placeholder() -> None:
    too_long = {"type": "image", "data": "A" * (MAX_IMAGE_BASE64_CHARS + 4), "mimeType": "image/png"}
    broken = {"type": "image", "data": "not base64!", "mimeType": "image/png"}
    result = {"content": [too_long, broken]}
    assert result_images(result) == ()  # the length is checked before decoding (review B3)
    assert render_result(result).count("[image content omitted") == 2
