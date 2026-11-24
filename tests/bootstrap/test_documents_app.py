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
    """The base and the documents sentences the system text is built with (V3-P89 §6)."""
    loader: Any = app.runtime.driver._loader
    return (*loader._instructions, *loader._system.documents)


@pytest.mark.parametrize("documents_dir", [None, "/nonexistent/documents"])
def test_without_a_working_toolkit_nothing_is_offered(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                                      documents_dir: str | None) -> None:
    caplog.set_level(logging.INFO, "himari.bootstrap.application")
    app = open_app(tmp_path, documents_dir=documents_dir)
    try:
        assert app.documents is None and app.toolkit is None
        assert app.machine is not None  # the folders are there (V3-P109), but no reader: no read_document
        assert "machine/read_document" not in {t.definition.tool_id for t in app.machine.tools()}
        assert not any("read_document" in n for n in instructions(app))
    finally:
        asyncio.run(app.close())
    why = "HIMARI_DOCUMENTS_DIR is not set" if documents_dir is None else "the sandbox is off"
    assert f"documents: read_document {why}" in caplog.text and "run_command" not in caplog.text


def test_the_sentences() -> None:
    assert _documents_notes(None, None) == ()
    reader, kit = object(), object()
    assert _documents_notes(reader, kit) == (DOCUMENTS_READ_NOTE,)  # type: ignore[arg-type]


def test_read_document_is_told_with_conversation_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # V3-P101, V3-P109: read_document is the machine Environment's; told when Conversations have folders.
    from himari.bootstrap import application as boot

    class Kit:
        fd = 99
        manifest: dict[str, Any] = {}

        def close(self) -> None:
            self.fd = -1

    class Reading:
        versions = {"python-docx": "1"}

    kit, reader = Kit(), Reading()

    async def documents(*_: Any) -> tuple[Any, Any, str]:
        return kit, reader, "on"

    async def no_views(*_: Any) -> Any:
        from himari.imaging import ImageViews

        return ImageViews(None)

    monkeypatch.setattr(boot, "_documents", documents)
    monkeypatch.setattr(boot, "_image_views", no_views)
    app = asyncio.run(Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m", sandbox=True,
                                       cleanup_seconds=None))
    try:
        assert app.machine is not None
        tools = {t.definition.tool_id for t in app.machine.tools()}
        assert "machine/read_document" in tools and "machine/run_command" not in tools
        notes = instructions(app)
        assert DOCUMENTS_READ_NOTE in notes
        assert not any("run_command" in n for n in notes)
    finally:
        asyncio.run(app.close())


def test_a_working_toolkit_without_folders_tells_the_model_nothing(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    # V3-P101: read_document is the machine Environment's; with no folders there is no Tool to tell of.
    from himari.bootstrap import application as boot

    class Kit:
        fd = 99
        manifest: dict[str, Any] = {}

        def close(self) -> None:
            self.fd = -1

    class Reading:
        versions = {"python-docx": "1"}

    async def documents(*_: Any) -> tuple[Any, Any, str]:
        return Kit(), Reading(), "on"

    monkeypatch.setattr(boot, "_documents", documents)
    app = open_app(tmp_path, machine_principals=())
    try:
        assert app.documents is not None and app.machine is None
        assert not any("read_document" in n for n in instructions(app))
    finally:
        asyncio.run(app.close())


def test_any_startup_failure_closes_the_toolkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-review N1 (P8): whatever stops the documents startup, the descriptor is closed.
    from himari.bootstrap import application as boot

    class Kit:
        fd = 7

        def close(self) -> None:
            self.fd = -1

    class Stop(BaseException):
        pass

    kit = Kit()

    def stop() -> None:
        raise Stop

    monkeypatch.setattr(boot, "open_toolkit", lambda *a, **k: kit)
    from himari.sandbox import identity

    monkeypatch.setattr(identity, "check_trees", lambda *a, **k: None)
    monkeypatch.setattr(boot, "discover_capability", stop)
    with pytest.raises(Stop):
        asyncio.run(boot._documents(tmp_path, True, "/opt/x", frozenset()))
    assert kit.fd == -1


def test_the_documents_status(tmp_path: Path) -> None:
    # V3-P70 §9: read_document offered, and the toolkit's libraries.
    class On:
        available = True
        versions = {"openpyxl": "3.1.5"}

    app = open_app(tmp_path)
    try:
        assert app.documents_status == {"read": False, "libraries": {}}
        app.documents = On()  # type: ignore[assignment]
        assert app.documents_status == {"read": True, "libraries": {"openpyxl": "3.1.5"}}
    finally:
        app.documents = None
        asyncio.run(app.close())
