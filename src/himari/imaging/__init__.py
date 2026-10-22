"""Model copies of images (V3-P67 §3.1), made only inside the image sandbox.

``ImageViews`` runs ``worker.py`` — the one place Pillow is used — in a
server-profile sandbox that binds the Python runtime and that file read-only
and nothing writable: no network, HIMARI's data root invisible (the server
sandbox's own checks, V3-P59). One process per image; a wall-clock bound
on top of the worker's own memory and CPU limits, and at most
``MAX_WORKERS`` at once. What a worker answers is judged again here from
its bytes (type and size), never taken on its word.

When the sandbox is not ENFORCED, or the startup check (a tiny image
through the worker) fails, nothing is decoded at all: ``available`` is
False and every image stays an attachment line (``NO_SANDBOX``, B1).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

from himari.data.api import IMAGE_HEAD_BYTES, PreparedView, sniff_image
from himari.sandbox.api import ProcessLauncher

log = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("worker.py")
WALL_SECONDS = 30.0
MAX_WORKERS = 2  # each may take 512 MiB (review C1)
# The smallest PNG (1×1, opaque): what the startup check sends through.
_PROBE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def runtime_roots() -> tuple[str, ...]:
    """What the worker needs to read: the Python environment (unless it is
    under /usr, which the sandbox shows anyway) and the worker's folder
    (unless it is inside that environment)."""
    roots: list[str] = []
    for prefix in dict.fromkeys(os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)):
        if not (prefix == "/usr" or prefix.startswith("/usr/")):
            roots.append(prefix)  # a virtual environment, and a Python installed outside /usr (review N4)
    folder = str(WORKER.parent.resolve())
    if not any(folder == r or folder.startswith(r.rstrip("/") + "/") for r in roots):
        roots.append(folder)
    return tuple(roots)


class ImageViews:
    """A ``ViewMaker`` over a launcher of the image profile (or none)."""

    def __init__(self, launcher: ProcessLauncher | None, *, python: str | None = None,
                 wall_seconds: float = WALL_SECONDS) -> None:
        self._launcher = launcher
        self._python = python or sys.executable
        self._wall = wall_seconds
        self._available = False
        self._places = asyncio.Semaphore(MAX_WORKERS)

    @property
    def available(self) -> bool:
        return self._available

    async def check(self) -> bool:
        """At startup: the worker makes a copy of a tiny image in the sandbox."""
        if self._launcher is None:
            return False
        self._available = True
        made = await self.make(_PROBE)
        self._available = isinstance(made, PreparedView)
        if not self._available:
            log.warning("images cannot be prepared on this server (the image sandbox check failed: %s)", made)
        return self._available

    async def make(self, original: bytes) -> PreparedView | str:
        """The copy, or why there is none (a short reason)."""
        if not self._available or self._launcher is None:
            return "no sandbox"
        async with self._places:
            line = await self._run(self._launcher, original)
        if isinstance(line, str):
            return line
        try:
            reply = json.loads(line)
            if not reply.get("ok"):
                return str(reply.get("reason") or "failed")[:80]
            data = base64.b64decode(reply["data"], validate=True)
            media_type, width, height = reply["media_type"], int(reply["width"]), int(reply["height"])
        except (ValueError, KeyError, TypeError):
            return "a bad answer"
        made = sniff_image(data[:IMAGE_HEAD_BYTES])
        if (media_type not in ("image/png", "image/jpeg") or not (0 < width <= 1568 and 0 < height <= 1568)
                or made is None or (made.media_type, made.width, made.height) != (media_type, width, height)):
            return "a bad answer"
        return PreparedView(data, media_type, width, height)

    async def _run(self, launcher: ProcessLauncher, original: bytes) -> bytes | str:
        """The worker's one line, or why there is none."""
        try:
            process = await launcher.launch([self._python, "-I", str(WORKER)], {}, "/tmp")
        except Exception as error:  # noqa: BLE001 — unavailable, refused, setup failed: no copy
            return f"the sandbox did not start ({type(error).__name__})"
        try:
            request = json.dumps({"data": base64.b64encode(original).decode("ascii")}).encode("ascii")
            async with asyncio.timeout(self._wall):  # one bound for the whole run (review N3)
                await process.write_line(request)
                return await process.read_line()
        except (TimeoutError, Exception) as error:  # noqa: BLE001 — a timeout, a crash, a line too long
            return f"the worker failed ({type(error).__name__})"
        finally:
            await process.shutdown(0.5)
