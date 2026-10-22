"""CLI and files (V3-P66 §9): send --attach, /attach in a chat, files,
download, and the file lines of events."""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap.application import Application, workspace_for  # noqa: E402
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import ServerClient, disposition_name  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter, ScriptStep  # noqa: E402


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="m", sandbox=False,
                                                      cleanup_seconds=None)
            return self.application

        config = WebConfig("127.0.0.1", 8765, frozenset({"testserver"}), frozenset({"http://testserver"}), False,
                           str(tmp_path / "data"), "m", None, False)
        self.test_client = TestClient(web.create_app(config, opener))

    def client(self) -> ServerClient:
        assert self.application is not None
        self.test_client.headers["authorization"] = f"Bearer {derive_cli_bearer(self.application.operator_secret)}"
        return ServerClient(self.test_client)

    def run(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            code = cli.run(list(argv), environ={}, out=out, err=err, client=self.client())
        finally:
            sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def queue(self, *steps: ScriptStep) -> None:
        self.provider.extend(steps)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    monkeypatch.chdir(tmp_path)
    h = Harness(tmp_path)
    with h.test_client:
        yield h


def test_send_with_attachments_then_list_and_download(harness: Harness, tmp_path: Path) -> None:
    cid = harness.run("new")[1].strip()
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "data.csv").write_text("a,b\n")
    harness.queue(final("read them"))
    code, out, err = harness.run("send", cid, "--attach", "notes.txt", "--attach", "data.csv")
    assert code == 0, err
    assert f"  [file] notes.txt (5 bytes) — himari download {cid} " in out
    assert "commands cannot run on this server" in err  # sandbox off here
    code, out, _ = harness.run("files", cid)
    rows = out.splitlines()
    assert code == 0 and len(rows) == 2 and rows[0].endswith("data.csv") and " upload " in rows[0]
    file_id = rows[1].split()[0]
    os.mkdir(tmp_path / "out")
    os.chdir(tmp_path / "out")
    code, _, err = harness.run("download", cid, file_id)
    assert code == 0 and (tmp_path / "out" / "notes.txt").read_text() == "hello" and "saved notes.txt" in err
    code, _, err = harness.run("download", cid, file_id)
    assert code == cli.EXIT_FAILED and "exists" in err  # never overwritten
    assert harness.run("download", cid, file_id, "--force")[0] == 0
    assert harness.run("download", cid, file_id, "-o", "copy.txt")[0] == 0
    assert (tmp_path / "out" / "copy.txt").read_text() == "hello"
    code, _, err = harness.run("download", cid, "file_404")
    assert code == cli.EXIT_FAILED and "not_found" in err


def test_send_needs_text_or_files_and_sends_files_alone(harness: Harness, tmp_path: Path) -> None:
    cid = harness.run("new")[1].strip()
    assert harness.run("send", cid)[0] == cli.EXIT_USAGE
    (tmp_path / "a.txt").write_text("a")
    harness.queue(final("ok"))
    code, out, err = harness.run("send", cid, "--attach", "a.txt")
    assert code == 0, err
    events = harness.client().get(f"/api/conversations/{cid}/events")["items"]
    assert events[0]["kind"] == "user_message" and events[0]["text"] == ""  # a message, not /resume
    code, _, err = harness.run("send", cid, "x", "--attach", "missing.txt")
    assert code == cli.EXIT_FAILED and "cannot read missing.txt" in err and "not sent" in err


def test_a_model_chosen_dot_name_needs_o(harness: Harness, tmp_path: Path) -> None:
    cid = harness.run("new")[1].strip()
    assert harness.application is not None
    folder = Path(harness.application.inside.host_root(workspace_for(OPERATOR))) / "conversations" / cid
    folder.mkdir(parents=True)
    (folder / ".npmrc").write_text("registry=evil")
    harness.queue(calls(("inside/present_file", {"path": ".npmrc"})), final("here"))
    code, out, _ = harness.run("send", cid, "give it")
    assert code == 0 and "[file] .npmrc" in out
    file_id = harness.client().get(f"/api/conversations/{cid}/files")["items"][0]["file_id"]
    code, _, err = harness.run("download", cid, file_id)
    assert code == cli.EXIT_FAILED and "give one with -o" in err and not (tmp_path / ".npmrc").exists()
    assert harness.run("download", cid, file_id, "-o", "npmrc.txt")[0] == 0


def test_attach_in_a_chat(harness: Harness, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("bb")
    harness.queue(final("got a and b"), final("got b"))
    lines = iter(["/attach", "/attach a.txt b.txt", "/attach", "look", "/attach b.txt", "/detach", "/send",
                  "/attach b.txt", "/send", "/files", "/exit"])
    out, err = io.StringIO(), io.StringIO()
    session = cli.Session(harness.client(), cli.Printer(out), err, sleep=lambda s: None)
    assert cli.cmd_chat(session, cli.parser().parse_args(["chat"]), read=lambda prompt: next(lines)) == 0
    notes = err.getvalue()
    assert notes.count("(nothing attached)") == 3  # empty list, after /detach, and /send with nothing
    assert "attached: a.txt (1 bytes)" in notes and "attached: b.txt (2 bytes)" in notes
    printed = out.getvalue()
    assert printed.count("[file] a.txt") == 1 and printed.count("[file] b.txt") == 2


def test_the_download_name_comes_from_filename_star() -> None:
    assert disposition_name("attachment; filename=\"r_.pdf\"; filename*=UTF-8''%EB%B3%B4%EA%B3%A0.pdf") == "보고.pdf"
    assert disposition_name("attachment; filename=\"x\"") is None


def test_an_upload_refusal_is_definite(harness: Harness, tmp_path: Path) -> None:
    cid = harness.run("new")[1].strip()
    assert harness.application is not None
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    harness.application.data.blobs.configure(max_bytes=10, keep_days=30)
    code, _, err = harness.run("send", cid, "--attach", "big.bin")
    assert code == cli.EXIT_FAILED and "storage_full" in err and "may have been applied" not in err

