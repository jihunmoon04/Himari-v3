"""Real-host gate for documents (V3-P70 §5, §6.3; opt-in: HIMARI_SANDBOX_HOST=1
and HIMARI_DOCUMENTS_TOOLKIT naming an installed toolkit's ``current``, which
this user owns). The worker's sandbox isolates; the Application offers
read_document."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from himari.bootstrap.application import Application
from himari.documents import TMP_BYTES, DocumentReader, open_toolkit, worker_roots
from himari.documents.api import Piece
from himari.prompt.api import DOCUMENTS_READ_NOTE
from himari.provider.scripted import ScriptedAdapter
from himari.sandbox.api import SandboxState, ServerProfile
from himari.sandbox.server import ServerSandbox

TOOLKIT = os.environ.get("HIMARI_DOCUMENTS_TOOLKIT", "")
pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1" or not TOOLKIT,
                                reason="host gate with a documents toolkit is opt-in")
BASE = os.path.dirname(os.path.abspath(TOOLKIT)) if TOOLKIT else ""
ME = frozenset({os.getuid()})

ESCAPE = r"""
import json, os, socket, sys
toolkit, data = sys.argv[1], sys.argv[2]
found = {}
for root in (toolkit, os.path.dirname(sys.argv[0]) or "."):
    try:
        open(os.path.join(root, "planted.txt"), "w").close(); found[root] = "wrote"
    except OSError: found[root] = "refused"
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3); found["net"] = "connected"
except OSError: found["net"] = "refused"
found["data"] = os.path.exists(data)
written = 0
try:
    for i in range(10):  # 10 files of 8 MiB: past the 64 MiB /tmp
        with open(f"/tmp/fill{i}", "wb") as handle:
            handle.write(b"x" * (8 << 20))
        written += 8
except OSError:
    pass
found["tmp_mib"] = written
print(json.dumps(found), flush=True)
"""


def test_the_documents_profile_isolates_and_reads(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "himari.db").write_text("secret")
    toolkit = open_toolkit(BASE, owners=ME)

    async def go() -> tuple[dict[str, object], object]:
        sandbox = ServerSandbox.discover([str(data)])
        assert await sandbox.start() is SandboxState.ENFORCED
        launcher = sandbox.launcher(ServerProfile(read_only=worker_roots(toolkit), tmp_bytes=TMP_BYTES))
        process = await launcher.launch(["/usr/bin/python3", "-I", "-c", ESCAPE, toolkit.path,
                                         str(data / "himari.db")], {}, "/tmp")
        try:
            found: dict[str, object] = json.loads(await asyncio.wait_for(process.read_line(), 120))
        finally:
            await process.shutdown(1.0)
        reader = DocumentReader(launcher, toolkit)
        assert await reader.check() is None
        return found, await reader.read(_pdf(), pages=(Piece(1, None),), pictures=None, view=True,
                                        text_budget=60000)

    try:
        found, reading = asyncio.run(go())
    finally:
        toolkit.close()
    assert found[toolkit.path] == "refused" and found["net"] == "refused" and found["data"] is False
    assert int(str(found["tmp_mib"])) <= 64  # /tmp is sized: the ninth 8 MiB file does not fit (re-review N1)
    assert reading.parts[0].text.startswith("Hello from the host test") and len(reading.images) == 1  # type: ignore[attr-defined]


def _pdf() -> bytes:
    code = (f"import sys; sys.path.insert(0, {os.path.join(os.path.abspath(TOOLKIT), 'site-packages')!r})\n"
            "import io\nfrom reportlab.pdfgen import canvas\nb = io.BytesIO(); c = canvas.Canvas(b)\n"
            "c.drawString(72, 760, 'Hello from the host test'); c.showPage(); c.save()\n"
            "sys.stdout.buffer.write(b.getvalue())")
    return subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", code], capture_output=True, check=True).stdout


def test_the_application_opens_the_toolkit_and_tells_nothing_without_folders(tmp_path: Path) -> None:
    async def go() -> None:
        app = await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m", cleanup_seconds=None,
                                     documents_dir=BASE, documents_owners=ME, machine_principals=())
        try:
            assert app.documents is not None and app.machine is None  # nobody has folders (V3-P101, V3-P109)
            # read_document is the machine Environment's: without it the model is not told of it.
            assert DOCUMENTS_READ_NOTE not in app.runtime.driver._loader._system.documents  # type: ignore[attr-defined]
        finally:
            await app.close()
        assert app.toolkit is not None and app.toolkit.fd == -1  # closed with the Application

    asyncio.run(go())
