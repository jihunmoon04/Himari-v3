"""Real-host gate for documents (V3-P70 §5, §6.3; opt-in: HIMARI_SANDBOX_HOST=1
and HIMARI_DOCUMENTS_TOOLKIT naming an installed toolkit's ``current``, which
this user owns). The worker's sandbox isolates; the Workspace sandbox shows
the toolkit read-only; the Application offers read_document and both notes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from himari.bootstrap.application import Application
from himari.common import (
    BindingId,
    CallId,
    ConversationId,
    EnvironmentInstanceId,
    PrincipalId,
    ToolId,
    TrustedScope,
    WorkspaceId,
)
from himari.data.api import BindingAuthority, ConversationWorkspace
from himari.documents import TMP_BYTES, DocumentReader, open_toolkit, worker_roots
from himari.documents.api import Piece
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.prompt.api import DOCUMENTS_READ_NOTE
from himari.provider.scripted import ScriptedAdapter
from himari.sandbox.api import SandboxState, ServerProfile
from himari.sandbox.backend import SandboxBackend
from himari.sandbox.server import ServerSandbox
from himari.tool.api import ToolContext, ToolInvocation, ToolStatus

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


def test_run_command_sees_the_toolkit_read_only(tmp_path: Path) -> None:
    toolkit = open_toolkit(BASE, owners=ME)
    try:
        backend = SandboxBackend.discover(toolkit_fd=toolkit.fd)
        assert asyncio.run(backend.start(protected_paths=(str(tmp_path),))) is SandboxState.ENFORCED
        inside = InsideEnvironment(tmp_path / "workspaces", sandbox=backend)
        inside.provision(WorkspaceId("ws_alice"))
        handler = next(t for t in inside.tools() if t.definition.tool_id == "inside/run_command")
        binding = BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), INSIDE, WorkspaceId("ws_alice"), 1)
        ctx = ToolContext(scope=TrustedScope(PrincipalId("alice"), ConversationId("c1")), artifacts=None,  # type: ignore[arg-type]
                          binding=binding, workspace=ConversationWorkspace(WorkspaceId("ws_alice"), "conversations/c1"))
        script = ("import os, docx, openpyxl, pptx, reportlab, pypdf, pypdfium2, PIL\n"
                  "fonts = os.environ['HIMARI_FONTS']\n"
                  "print(sorted(os.listdir(fonts)))\n"
                  "try:\n    open('/run/himari/documents/x', 'w')\nexcept OSError:\n    print('read-only')\n"
                  "d = docx.Document(); d.add_paragraph('made here'); d.save('made.docx'); print('saved')\n")
        outcome = asyncio.run(handler.run(ToolInvocation(ToolId("inside/run_command"), CallId("c"),
                                                         {"argv": ["python3", "-c", script]}), ctx))
        body = str(outcome.model_result.blocks[0].text)  # type: ignore[union-attr]
        assert outcome.status is ToolStatus.SUCCEEDED and "exit code: 0" in body, body
        assert "NanumGothic-Regular.ttf" in body and "read-only" in body and "saved" in body
        assert (tmp_path / "workspaces/ws_alice/conversations/c1/made.docx").stat().st_size > 1000
    finally:
        toolkit.close()


def test_the_application_offers_documents(tmp_path: Path) -> None:
    async def go() -> None:
        app = await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m", cleanup_seconds=None,
                                     documents_dir=BASE, documents_owners=ME)
        try:
            tools = {t.definition.tool_id for t in app.inside.tools()}
            assert {"inside/read_document", "inside/run_command"} <= tools
            assert app.documents is not None and app.documents.scripts
            instructions = app.runtime.driver._loader._instructions  # type: ignore[attr-defined]
            assert DOCUMENTS_READ_NOTE in instructions
            assert any("register NanumGothic" in n and "import pypdf" in n for n in instructions)
        finally:
            await app.close()
        assert app.toolkit is not None and app.toolkit.fd == -1  # closed with the Application

    asyncio.run(go())


def test_end_to_end_a_model_reads_edits_and_hands_back_documents(tmp_path: Path) -> None:
    # §15: a scripted model makes a Word file, reads it, changes it with a script, gives it to the user; then
    # makes a Korean PDF with NanumGothic and looks at it.
    from testkit import calls, final

    from himari.common import TextBlock
    from himari.prompt.api import ToolGroupTurn

    make_docx = ("import docx; d = docx.Document(); d.add_heading('보고서', 1); d.add_paragraph('초안 문장'); "
                 "d.save('report.docx')")
    edit_docx = ("import docx; d = docx.Document('report.docx'); d.paragraphs[1].text = '고친 문장'; "
                 "d.save('report.docx')")
    make_pdf = ("import os; from reportlab.pdfgen import canvas; from reportlab.pdfbase import pdfmetrics; "
                "from reportlab.pdfbase.ttfonts import TTFont; "
                "pdfmetrics.registerFont(TTFont('Nanum', os.path.join(os.environ['HIMARI_FONTS'], "
                "'NanumGothic-Regular.ttf'))); c = canvas.Canvas('hello.pdf'); c.setFont('Nanum', 20); "
                "c.drawString(72, 760, '안녕하세요 문서'); c.showPage(); c.save()")
    adapter = ScriptedAdapter([
        calls(("inside/run_command", {"argv": ["python3", "-c", make_docx]})),
        calls(("inside/read_document", {"path": "report.docx"})),
        calls(("inside/run_command", {"argv": ["python3", "-c", edit_docx]})),
        calls(("inside/read_document", {"path": "report.docx"}), ("inside/present_file", {"path": "report.docx"})),
        calls(("inside/run_command", {"argv": ["python3", "-c", make_pdf]})),
        calls(("inside/read_document", {"path": "hello.pdf", "view": True})),
        final("done"),
    ])

    async def go() -> list[str]:
        app = await Application.open(tmp_path / "data", adapter, model_id="m", cleanup_seconds=None,
                                     documents_dir=BASE, documents_owners=ME)
        try:
            scope = TrustedScope(PrincipalId("alice"), await app.create_conversation(PrincipalId("alice")))
            await app.send(scope, "go")
            await asyncio.wait_for(app.runner.wait(scope), 300)
            last = adapter.prepared_inputs[-1].conversation
            return [str(block.text) for part in last if isinstance(part, ToolGroupTurn)
                    for result in part.results.values() for block in result.blocks if isinstance(block, TextBlock)]
        finally:
            await app.close()

    texts = asyncio.run(go())
    reads = [t for t in texts if "(Word," in t or "(PDF," in t]
    assert "# 보고서\n초안 문장" in reads[0] and "# 보고서\n고친 문장" in reads[1], texts
    assert any(t.startswith("Presented report.docx") for t in texts), texts
    assert "안녕하세요 문서" in reads[2] and "[images: page 1]" in reads[2], texts
