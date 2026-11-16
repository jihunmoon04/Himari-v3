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
        assert app.machine is None  # no runner: no Environment, no read_document (V3-P101)
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
    assert "it can import pypdfium2, docx, reportlab, PIL, which PYTHONPATH" in script  # known names, in order
    assert "register NanumGothic-Regular.ttf and NanumGothic-Bold.ttf from the folder in $HIMARI_FONTS" in script
    assert "evil" not in script and "not with -I or -E" in script
    assert "Prepare finished files in the conversation with present_file" in script
    assert script.endswith("interface capabilities govern delivery.")
    _, no_fonts = _documents_notes(Reader(), Kit(None), True)  # type: ignore[arg-type]
    assert "HIMARI_FONTS" not in no_fonts


@pytest.mark.parametrize("bound", [True, False])
def test_the_scripts_sentence_follows_the_runners_toolkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          bound: bool) -> None:
    # V3-P101: run_command is the machine Environment's; the scripts are told only when its runner binds the toolkit.
    from himari.bootstrap import application as boot
    from himari.environment.machine import MachineEnvironment
    from himari.sandbox.api import SandboxState

    class Kit:
        fd = 99
        manifest: dict[str, Any] = {}

        def close(self) -> None:
            self.fd = -1

    class Reading:
        versions = {"python-docx": "1"}
        scripts = False

    async def hello(self: MachineEnvironment) -> dict[str, Any]:
        self.sandbox_state = SandboxState.ENFORCED
        return {"state": "enforced", "himari_home": "/home/u/HIMARI", "toolkit": bound}

    kit, reader = Kit(), Reading()

    async def documents(*_: Any) -> tuple[Any, Any, str]:
        return kit, reader, "on"

    async def no_views(*_: Any) -> Any:
        from himari.imaging import ImageViews

        return ImageViews(None)

    async def no_servers(*_: Any) -> None:
        return None

    monkeypatch.setattr(boot, "_documents", documents)
    monkeypatch.setattr(MachineEnvironment, "hello", hello)
    monkeypatch.setattr(boot, "_image_views", no_views)
    monkeypatch.setattr(boot, "_server_launchers", no_servers)
    app = asyncio.run(Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m", sandbox=True,
                                       cleanup_seconds=None, machine_socket=str(tmp_path / "runner.sock")))
    try:
        assert app.machine is not None
        tools = {t.definition.tool_id for t in app.machine.tools()}
        assert {"machine/run_command", "machine/read_document"} <= tools
        assert reader.scripts is bound and app.current_sandbox_state is SandboxState.ENFORCED
        notes = instructions(app)
        assert DOCUMENTS_READ_NOTE in notes
        assert any("run_command as `python3" in n for n in notes) is bound
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
    # V3-P70 §9 (review N9): scripts only while run_command's sandbox (the runner's: V3-P101) is enforced.
    from himari.environment.machine import MachineEnvironment
    from himari.sandbox.api import SandboxState

    class On:
        available = True
        scripts = True
        versions = {"openpyxl": "3.1.5"}

    app = open_app(tmp_path)
    try:
        assert app.documents_status == {"read": False, "scripts": False, "libraries": {}}
        app.documents = On()  # type: ignore[assignment]
        assert app.documents_status == {"read": True, "scripts": False, "libraries": {"openpyxl": "3.1.5"}}
        app.machine = MachineEnvironment(str(tmp_path / "runner.sock"))
        app.machine.sandbox_state = SandboxState.ENFORCED
        assert app.current_sandbox_state is SandboxState.ENFORCED and app.documents_status["scripts"] is True
    finally:
        app.documents = None
        asyncio.run(app.close())
