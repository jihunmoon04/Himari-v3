"""The CLI and the notes (V3-P71 §8): list, add, edit, rm, the line a saved
note gets in a conversation, the delete question and the status line."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.common import CallId, ToolCall, ToolId  # noqa: E402
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import ServerClient  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.application: Application | None = None
        self.provider = ScriptedAdapter([])

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="m",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig("127.0.0.1", 8765, frozenset({"testserver"}), frozenset({"http://testserver"}), False,
                           str(tmp_path / "data"), "m", None, False)
        self.test_client = TestClient(web.create_app(config, opener))

    def authorized(self) -> TestClient:
        assert self.application is not None
        self.test_client.headers["authorization"] = f"Bearer {derive_cli_bearer(self.application.operator_secret)}"
        return self.test_client

    def run(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()  # input() asks on stdout
        try:
            code = cli.run(list(argv), environ={}, out=out, err=err, client=ServerClient(self.authorized()))
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    h = Harness(tmp_path)
    with h.test_client:
        yield h


def test_add_list_edit_and_rm(harness: Harness) -> None:
    assert harness.run("memory") == (cli.EXIT_OK, "", "(no notes)\n")
    assert harness.run("memory", "add", "preference", "Short lists.") == (
        cli.EXIT_OK, "", "(saved as m1; used from the next message)\n")
    assert harness.run("memory", "add", "user", "-", stdin="Writes Go.\n")[0] == cli.EXIT_OK
    code, out, err = harness.run("memory")
    today = out.split("· ")[-1].strip()[:10]
    assert code == cli.EXIT_OK and out == (f"user:\n  m2  Writes Go.  (added by you · {today})\n"
                                           f"preference:\n  m1  Short lists.  (added by you · {today})\n")
    assert err == "(2 of 1000 notes)\n"
    assert harness.run("memory", "edit", "m1", "Short lists, no tables.", "--category", "project")[2] == (
        "(saved; used from the next message)\n")
    assert "project:\n  m1  Short lists, no tables." in harness.run("memory")[1]
    assert harness.run("memory", "rm", "1", stdin="n\n")[2].endswith("(nothing was deleted)\n")
    assert harness.run("memory", "rm", "m1", "--yes")[2] == "(deleted)\n"
    assert harness.run("memory", "rm", "m1", "--yes") == (cli.EXIT_FAILED, "", "no note m1\n")
    assert "m1" not in harness.run("memory")[1]


@pytest.mark.parametrize("argv", [("memory", "x"), ("memory", "add", "mood", "x"), ("memory", "add", "user"),
                                  ("memory", "edit", "m1"), ("memory", "rm"), ("memory", "rm", "m1", "--category", "user"),
                                  ("memory", "--yes"), ("memory", "add", "user", "x", "--yes")])
def test_usage(harness: Harness, argv: tuple[str, ...]) -> None:
    try:
        code, _, err = harness.run(*argv)
    except SystemExit as stop:  # argparse refuses an unknown action itself
        assert stop.code == 2
        return
    assert code == cli.EXIT_USAGE and err.startswith("usage: himari memory")


def test_refusals_are_reported(harness: Harness) -> None:
    code, _, err = harness.run("memory", "add", "user", "x" * 501)
    assert code == cli.EXIT_FAILED and err.startswith("refused (too_large)")
    assert harness.run("memory", "edit", "nine", "x") == (cli.EXIT_USAGE, "", "not a note id: nine (as m12)\n")


def test_a_saved_note_is_one_line_in_the_conversation(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    harness.provider.extend([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                      {"category": "user", "text": "Uses\nGo."}),)),
        final("Noted."),
    ])
    code, out, _ = harness.run("send", cid, "remember: I use Go")
    assert code == cli.EXIT_OK
    assert "  ◆ remembered m1: Uses Go.\n" in out and "memory_save {" not in out
    code, out, err = harness.run("delete", cid, "--yes")
    assert "1 note HIMARI saved in it is deleted with it for good." in err
    assert f"m1  Uses Go.  (from {cid} (in trash) · HIMARI · " in harness.run("memory")[1]


def test_status_says_whether_memory_is_on(harness: Harness) -> None:
    assert "memory: on" in harness.run("status")[1].splitlines()
