"""Images end to end in the Application (V3-P67): an uploaded picture gets a
model copy and reaches a model that sees images as an image; a model that
does not gets the line."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from pathlib import Path

from PIL import Image
from testkit import final

from himari.bootstrap.application import Application
from himari.common import PrincipalId, TrustedScope
from himari.data.api import ImageStatus, PreparedView
from himari.prompt.api import image_plan
from himari.provider.api import ProviderCapabilities
from himari.provider.scripted import ScriptedAdapter

ALICE = PrincipalId("alice")


class Seeing(ScriptedAdapter):
    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return ProviderCapabilities(model_id=model_id, parallel_tool_calls=True, image_input=model_id == "sees")


class Views:
    available = True

    async def make(self, original: bytes) -> PreparedView | str:
        return PreparedView(b"COPY", "image/jpeg", 20, 15)


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 30)).save(out, "PNG")
    return out.getvalue()


def scenario(tmp_path: Path, model: str) -> tuple[Seeing, Application, TrustedScope]:
    async def go() -> tuple[Seeing, Application, TrustedScope]:
        adapter = Seeing([final("I see it")])
        app = await Application.open(tmp_path / "data", adapter, model_id=model, sandbox=False, cleanup_seconds=None)
        app.views = Views()  # the sandbox is off here; the copy itself is tested on the host gate
        scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
        record = await app.upload(scope, "photo.png", "image/png", chunks(png()))
        assert record.image_status is ImageStatus.READY and record.view is not None
        await app.send(scope, "what is this?", [record.file_id])
        await app.runner.wait(scope)
        return adapter, app, scope

    return asyncio.run(go())


def test_a_model_that_sees_images_gets_the_copy(tmp_path: Path) -> None:
    adapter, app, _ = scenario(tmp_path, "sees")
    try:
        first = adapter.prepared_inputs[0]
        assert first.request_options.image_input
        info, = first.attachments.values()
        assert info.view_state == "ready" and info.view_media_type == "image/jpeg"
        assert len(image_plan(first).shown) == 1
    finally:
        asyncio.run(app.close())


def test_a_model_that_does_not_gets_the_line(tmp_path: Path) -> None:
    adapter, app, _ = scenario(tmp_path, "text-only")
    try:
        first = adapter.prepared_inputs[0]
        assert not first.request_options.image_input and image_plan(first).shown == frozenset()
        info, = first.attachments.values()
        assert info.view_state == "ready"  # the copy is there; this model is just not given it
    finally:
        asyncio.run(app.close())
