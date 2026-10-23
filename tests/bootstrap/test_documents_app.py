"""Documents in the Application (V3-P70 §6.4, §7): offered only with a
checked toolkit and reader; each sentence only while its tool can do it."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from himari.bootstrap.application import Application, _documents_notes
from himari.prompt.api import DOCUMENTS_READ_NOTE
from himari.provider.scripted import ScriptedAdapter


def open_app(tmp_path: Path, **options: Any) -> Application:
    return asyncio.run(Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m", sandbox=False,
                                        cleanup_seconds=None, **options))


def instructions(app: Application) -> tuple[str, ...]:
    return tuple(app.runtime.driver._loader._instructions)  # type: ignore[attr-defined]


@pytest.mark.parametrize("documents_dir", [None, "/nonexistent/documents"])
def test_without_a_working_toolkit_nothing_is_offered(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                                      documents_dir: str | None) -> None:
    caplog.set_level(logging.INFO, "himari.bootstrap.application")
    app = open_app(tmp_path, documents_dir=documents_dir)
    try:
        assert app.documents is None and app.toolkit is None
        assert "inside/read_document" not in {t.definition.tool_id for t in app.inside.tools()}
        assert not any("read_document" in n for n in instructions(app))
    finally:
        asyncio.run(app.close())
    why = "HIMARI_DOCUMENTS_DIR is not set" if documents_dir is None else "the sandbox is off"
    assert f"documents: read_document {why}; run_command toolkit off" in caplog.text


class Reader:
    versions = {"pypdfium2": "5", "pillow": "13", "python-docx": "1", "reportlab": "5", "unknown": "1"}


class Kit:
    def __init__(self, fonts: object) -> None:
        self.manifest = {"fonts": fonts}


def test_the_sentences() -> None:
    assert _documents_notes(None, None, True) == ()
    kit = Kit(["NanumGothic-Regular.ttf", "NanumGothic-Bold.ttf", "../evil.ttf", "x" * 70 + ".ttf", 3])
    read_only = _documents_notes(Reader(), kit, False)  # type: ignore[arg-type]
    assert read_only == (DOCUMENTS_READ_NOTE,)
    read, script = _documents_notes(Reader(), kit, True)  # type: ignore[arg-type]
    assert read == DOCUMENTS_READ_NOTE
    assert "it can import pypdfium2, docx, reportlab, PIL." in script  # the toolkit's order, known names only
    assert "register NanumGothic-Regular.ttf and NanumGothic-Bold.ttf from the folder in $HIMARI_FONTS" in script
    assert "evil" not in script and "not with -I or -E" in script and script.endswith("with present_file.")
    _, no_fonts = _documents_notes(Reader(), Kit(None), True)  # type: ignore[arg-type]
    assert "HIMARI_FONTS" not in no_fonts
