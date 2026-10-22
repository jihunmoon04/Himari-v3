"""Model copies (V3-P67 §3.1): what the worker makes of an image, and how
ImageViews treats its launcher. The real sandbox run is in
tests/sandbox_host/test_image_sandbox.py (opt-in)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from PIL import Image

from himari.data.api import PreparedView
from himari.imaging import WORKER, ImageViews, runtime_roots
from himari.sandbox.api import ProcessGone


def convert(data: bytes) -> dict[str, Any]:
    """The worker as a plain child process (the sandbox is not the subject here)."""
    done = subprocess.run([sys.executable, "-I", str(WORKER)], capture_output=True, timeout=60,
                          input=(json.dumps({"data": base64.b64encode(data).decode()}) + "\n").encode())
    reply: dict[str, Any] = json.loads(done.stdout)
    return reply


def encoded(image: Image.Image, fmt: str, **options: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def decode(reply: Mapping[str, Any]) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(reply["data"])))


def test_a_large_photo_is_scaled_to_the_long_edge_and_loses_its_metadata() -> None:
    photo = Image.new("RGB", (4032, 3024), (30, 90, 200))
    exif = photo.getexif()
    exif[0x8825] = {2: (37, 33, 0)}  # GPS
    exif[0x010F] = "PhoneMaker"
    reply = convert(encoded(photo, "JPEG", exif=exif))
    assert reply["ok"] and (reply["media_type"], reply["width"], reply["height"]) == ("image/jpeg", 1568, 1176)
    copy = decode(reply)
    assert copy.size == (1568, 1176) and not copy.getexif() and "exif" not in copy.info


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"], ids=["jpeg", "png"])
def test_a_photo_of_nearly_fifty_million_pixels_fits_the_limits(fmt: str) -> None:
    """Review C3: a 50 MP phone photo (8160×6120) is made small without a full-size copy."""
    photo = Image.linear_gradient("L").resize((8160, 6120)).convert("RGB")
    reply = convert(encoded(photo, fmt))
    assert reply["ok"], reply
    assert (reply["width"], reply["height"]) == (1568, 1176)


def test_the_orientation_is_applied() -> None:
    portrait = Image.new("RGB", (300, 200), (0, 0, 0))
    exif = portrait.getexif()
    exif[0x0112] = 6  # rotate 90° clockwise when shown
    reply = convert(encoded(portrait, "JPEG", exif=exif))
    assert (reply["width"], reply["height"]) == (200, 300)


def test_a_small_image_keeps_its_size_and_transparency_stays_png() -> None:
    reply = convert(encoded(Image.new("RGBA", (120, 80), (0, 0, 0, 0)), "PNG"))
    assert (reply["media_type"], reply["width"], reply["height"]) == ("image/png", 120, 80)
    assert decode(reply).mode == "RGBA"


def test_an_animation_gives_its_first_frame() -> None:
    frames = [Image.new("P", (64, 64), i) for i in (1, 2, 3)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    reply = convert(out.getvalue())
    assert reply["ok"] and decode(reply).size == (64, 64)


@pytest.mark.parametrize("data, reason", [
    (b"<svg/>", "UnidentifiedImageError"),
    (encoded(Image.new("L", (10_000, 10_000)), "PNG"), "too many pixels"),
], ids=["svg", "bomb"])  # short ids: the test's name goes into the child's environment
def test_what_cannot_be_copied_says_why(data: bytes, reason: str) -> None:
    assert convert(data) == {"ok": False, "reason": reason}


def test_the_worker_limits_itself_before_decoding() -> None:
    import resource
    source = WORKER.read_text()
    assert source.index("limit()") < source.index("from PIL import")  # in main(): first the limits
    assert resource.RLIMIT_AS and "RLIMIT_CPU" in source


# --- ImageViews ---------------------------------------------------------------------------


class Process:
    def __init__(self, reply: bytes | None, hang: bool = False) -> None:
        self.reply, self.hang, self.shut = reply, hang, False
        self.returncode = None

    async def write_line(self, data: bytes) -> None:
        self.request = data

    async def read_line(self) -> bytes:
        if self.hang:
            await asyncio.sleep(10)
        if self.reply is None:
            raise ProcessGone("closed")
        return self.reply

    def stderr_tail(self) -> bytes:
        return b""

    async def shutdown(self, grace: float) -> None:
        self.shut = True


class Launcher:
    def __init__(self, process: Process) -> None:
        self.process: Process = process
        self.argv: list[str] = []

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> Process:
        self.argv = list(argv)
        return self.process


def good(width: int = 1, height: int = 1, media_type: str = "image/png") -> bytes:
    data = encoded(Image.new("RGB", (width, height)), "PNG")
    return json.dumps({"ok": True, "media_type": media_type, "width": width, "height": height,
                       "data": base64.b64encode(data).decode()}).encode()


def test_views_need_a_passing_check() -> None:
    async def scenario() -> None:
        assert not await ImageViews(None).check()
        assert await ImageViews(None).make(b"x") == "no sandbox"
        broken = ImageViews(Launcher(Process(None)))
        assert not await broken.check() and await broken.make(b"x") == "no sandbox"
        launcher = Launcher(Process(good()))
        views = ImageViews(launcher)
        assert await views.check()
        made = await views.make(b"original")
        assert made == PreparedView(encoded(Image.new("RGB", (1, 1)), "PNG"), "image/png", 1, 1)
        assert launcher.argv[1:] == ["-I", str(WORKER)] and launcher.process.shut

    asyncio.run(scenario())


@pytest.mark.parametrize("reply", [b"not json", json.dumps({"ok": False, "reason": "bad"}).encode(),
                                   good(2000, 10), json.dumps({"ok": True, "media_type": "image/svg+xml",
                                                               "width": 1, "height": 1, "data": ""}).encode(),
                                   good(1, 1, "image/jpeg"), good(1, 1).replace(b'"width": 1', b'"width": 2')],
                         ids=["not json", "refused", "too wide", "svg", "not its type", "not its size"])
def test_a_bad_or_refusing_answer_is_no_copy(reply: bytes) -> None:
    async def scenario() -> None:
        launcher = Launcher(Process(good()))
        views = ImageViews(launcher)
        await views.check()
        launcher.process = Process(reply)
        assert isinstance(await views.make(b"x"), str)

    asyncio.run(scenario())


def test_a_worker_that_hangs_is_shut_down() -> None:
    async def scenario() -> None:
        launcher = Launcher(Process(good()))
        views = ImageViews(launcher, wall_seconds=0.05)
        await views.check()
        launcher.process = Process(None, hang=True)
        assert "TimeoutError" in str(await views.make(b"x")) and launcher.process.shut

    asyncio.run(scenario())


def test_at_most_two_workers_run_at_once() -> None:
    """Review C1: each worker may take 512 MiB, so copies wait for a free place."""
    class Counting(Launcher):
        def __init__(self) -> None:
            super().__init__(Process(good()))
            self.running = self.most = 0

        async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> Process:
            self.running += 1
            self.most = max(self.most, self.running)
            launcher = self

            class Slow(Process):
                async def read_line(self) -> bytes:
                    await asyncio.sleep(0.01)
                    return await super().read_line()

                async def shutdown(self, grace: float) -> None:
                    launcher.running -= 1

            return Slow(good())

    async def scenario() -> None:
        launcher = Counting()
        views = ImageViews(launcher)
        await views.check()
        made = await asyncio.gather(*(views.make(b"x") for _ in range(6)))
        assert all(isinstance(m, PreparedView) for m in made) and launcher.most == 2

    asyncio.run(scenario())


def test_the_runtime_roots_hold_the_worker() -> None:
    roots = runtime_roots()
    assert any(str(WORKER).startswith(root.rstrip("/") + "/") for root in roots)
    assert not any(root == "/usr" or root.startswith("/usr/") for root in roots)
