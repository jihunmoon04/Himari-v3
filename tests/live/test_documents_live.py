"""Live check of read_document with a real model (V3-P70 §15): opt-in,
``HIMARI_LIVE=1`` plus the key, and like the host gate ``HIMARI_SANDBOX_HOST=1``
with ``HIMARI_DOCUMENTS_TOOLKIT`` (an installed toolkit's ``current`` this user
owns). A PDF with a text page, a vector chart page and a scanned page whose
only text is a word in a picture: the model must read the document, view the
page without text, and say the word."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from himari.bootstrap import read_env_file
from himari.bootstrap.application import Application
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import AssistantFinal
from himari.provider.openrouter import OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
TOOLKIT = os.environ.get("HIMARI_DOCUMENTS_TOOLKIT", "")
MODEL = os.environ.get("HIMARI_LIVE_DOCUMENT_MODEL", "openai/gpt-6-luna")

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY or os.environ.get("HIMARI_SANDBOX_HOST") != "1" or not TOOLKIT,
    reason="live documents check is opt-in (HIMARI_LIVE=1, key, HIMARI_SANDBOX_HOST=1, HIMARI_DOCUMENTS_TOOLKIT)")

PDF = """
import sys
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
c = canvas.Canvas(sys.argv[1])
c.drawString(72, 760, "Quarterly report. Page 2 has the sales chart; page 3 is a scanned note.")
c.showPage()
for i, height in enumerate((120, 260, 180, 320)):
    c.rect(100 + i * 90, 200, 60, height, fill=1)
for i in range(30):
    c.line(90, 200 + i * 12, 480, 200 + i * 12)
c.showPage()
note = Image.new("RGB", (900, 300), "white")
ImageDraw.Draw(note).text((60, 90), "PELICAN", fill="black", font=ImageFont.load_default(size=120))
c.drawImage(ImageReader(note), 72, 400, 450, 150)
c.showPage()
c.save()
"""


def test_a_model_reads_a_document_and_views_its_scanned_page(tmp_path: Path) -> None:
    async def go() -> str:
        adapter = OpenRouterAdapter(KEY)
        app = await Application.open(tmp_path / "data", adapter, model_id=MODEL, cleanup_seconds=None,
                                     documents_dir=os.path.dirname(os.path.abspath(TOOLKIT)),
                                     documents_owners=frozenset({os.getuid()}))
        try:
            alice = PrincipalId("alice")
            scope = TrustedScope(alice, await app.create_conversation(alice))
            workspace, cwd = await app._workspace(scope)
            site = os.path.join(os.path.abspath(TOOLKIT), "site-packages")
            target = Path(app.inside.host_root(workspace)) / cwd / "report.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["/usr/bin/python3", "-I", "-S", "-c",
                            f"import sys; sys.path.insert(0, {site!r})\n" + PDF, str(target)], check=True)
            await app.send(scope, "Read report.pdf in this conversation's folder. What single word is written on "
                                  "the scanned page? Answer with that word in capitals.")
            await asyncio.wait_for(app.runner.wait(scope), 300)
            events = await app.data.read_events(scope)
            finals = [e.payload for e in events if isinstance(e.payload, AssistantFinal)]
            return " ".join(str(b.text) for b in finals[-1].blocks if isinstance(b, TextBlock))
        finally:
            await app.close()

    assert "PELICAN" in asyncio.run(go())
