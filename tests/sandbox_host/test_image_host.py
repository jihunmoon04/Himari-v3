"""Real-host gate for the image profile (V3-P67 §3.1, review B1; opt-in:
HIMARI_SANDBOX_HOST=1): what runs there can read the Python runtime and
Pillow, and nothing else of the host; it cannot write a bound path or reach
the network; and the worker makes a copy there."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from himari.data.api import PreparedView
from himari.imaging import ImageViews, runtime_roots
from himari.sandbox.api import SandboxState, ServerProfile
from himari.sandbox.server import ServerSandbox

pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1", reason="host gate is opt-in")

PROBE = """
import json, os, socket, sys
found = {}
for root in sys.argv[1:-1]:
    try:
        open(os.path.join(root, "planted.txt"), "w").close(); found[root] = "wrote"
    except OSError: found[root] = "refused"
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3); found["net"] = "connected"
except OSError: found["net"] = "refused"
found["data"] = os.path.exists(sys.argv[-1])
import PIL; found["pil"] = PIL.__version__
print(json.dumps(found), flush=True)
"""


def test_the_image_profile_isolates(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "himari.db").write_text("secret")

    async def go() -> dict[str, object]:
        sandbox = ServerSandbox.discover([str(data)])
        assert await sandbox.start() is SandboxState.ENFORCED
        launcher = sandbox.launcher(ServerProfile(read_only=runtime_roots()))
        process = await launcher.launch([sys.executable, "-I", "-c", PROBE, *runtime_roots(),
                                         str(data / "himari.db")], {}, "/tmp")
        try:
            found: dict[str, object] = json.loads(await asyncio.wait_for(process.read_line(), 60))
            return found
        finally:
            await process.shutdown(1.0)

    found = asyncio.run(go())
    assert all(found[root] == "refused" for root in runtime_roots()), found
    assert found["net"] == "refused" and found["data"] is False and found["pil"]
    assert not any((Path(root) / "planted.txt").exists() for root in runtime_roots())


def test_the_worker_makes_a_copy_in_the_sandbox(tmp_path: Path) -> None:
    async def go() -> PreparedView | str:
        sandbox = ServerSandbox.discover([str(tmp_path)])
        assert await sandbox.start() is SandboxState.ENFORCED
        views = ImageViews(sandbox.launcher(ServerProfile(read_only=runtime_roots())))
        assert await views.check()
        out = io.BytesIO()
        Image.new("RGB", (4032, 3024), (1, 2, 3)).save(out, "JPEG")
        return await views.make(out.getvalue())

    made = asyncio.run(go())
    assert isinstance(made, PreparedView) and (made.width, made.height) == (1568, 1176)
