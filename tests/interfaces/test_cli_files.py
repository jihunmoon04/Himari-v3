"""CLI and files (V3-P66 §9): send --attach, /attach in a chat, files,
download, and the file lines of events."""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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



def chat(harness: Harness, *typed: str) -> tuple[str, str]:
    lines = iter([*typed, "/exit"])
    out, err = io.StringIO(), io.StringIO()
    session = cli.Session(harness.client(), cli.Printer(out), err, sleep=lambda s: None)
    assert cli.cmd_chat(session, cli.parser().parse_args(["chat"]), read=lambda prompt: next(lines)) == 0
    return out.getvalue(), err.getvalue()


def test_files_sent_with_a_turn_that_fails_are_not_attached_again(harness: Harness, tmp_path: Path) -> None:
    """PR #60 review B1: the message was taken, its turn then failed (the
    provider has nothing to say); the next message carries no file."""
    (tmp_path / "a.txt").write_text("a")
    out, err = chat(harness, "/attach a.txt", "look", "/attach", "again")
    assert "already sent" not in err and "refused" not in err
    assert out.count("[file] a.txt") == 1 and "again" in out
    assert err.count("(nothing attached)") == 1  # /attach after the failed turn: the list is empty


def test_a_refused_file_is_dropped_and_the_others_stay(harness: Harness, tmp_path: Path) -> None:
    """PR #60 review C1: an attachment the server refuses leaves the pending list."""
    other = harness.run("new")[1].strip()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    harness.queue(final("ok"))
    foreign = harness.client().upload(f"/api/conversations/{other}/uploads", "f.txt", io.BytesIO(b"f"), "text/plain")
    lines = iter(["/attach a.txt", "/attach", "send it", "/attach", "/exit"])
    out, err = io.StringIO(), io.StringIO()
    session = cli.Session(harness.client(), cli.Printer(out), err, sleep=lambda s: None)
    real = session.client.post

    def with_foreign(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if body and body.get("attachments"):
            body = {**body, "attachments": [*body["attachments"], foreign["file_id"]]}
        return real(path, body, **kwargs)

    session.client.post = with_foreign  # type: ignore[method-assign]
    assert cli.cmd_chat(session, cli.parser().parse_args(["chat"]), read=lambda prompt: next(lines)) == 0
    notes = err.getvalue()
    assert "refused" in notes and "attached: a.txt" in notes
    assert "dropped" not in notes  # the refused id was not in the list; a.txt stays for the next message
    assert notes.count("attached: a.txt") == 3


def test_a_files_only_message_prints_no_empty_box(harness: Harness, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    harness.queue(final("ok"))
    out, _ = chat(harness, "/attach a.txt", "/send")
    rows = out.splitlines()
    you = rows.index("You:")
    assert rows[you + 1].startswith("  [file] a.txt")  # PR #60 review C7


def test_an_upload_lost_in_transit_does_not_end_the_chat(harness: Harness, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.interfaces.cli.client import OutcomeUnknown
    (tmp_path / "a.txt").write_text("a")

    def lost(*args: Any, **kwargs: Any) -> Any:
        raise OutcomeUnknown("the request was sent but its answer was lost; it may have been applied")

    monkeypatch.setattr(ServerClient, "upload", lost)
    _, err = chat(harness, "/attach a.txt", "/id")
    assert "it is not attached" in err  # PR #60 review C8: the chat went on to /id and /exit


def test_only_503_and_507_uploads_are_definite(harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.interfaces.cli.client import OutcomeUnknown, ServerRefused
    cid = harness.run("new")[1].strip()
    assert harness.application is not None
    for status, code, expected in ((503, "upload_interrupted", ServerRefused), (500, "internal_error", OutcomeUnknown)):
        async def failing(*args: Any, status: int = status, code: str = code) -> Any:
            raise web.ApiError(status, code, "no")

        monkeypatch.setattr(harness.application, "upload", failing)
        with pytest.raises(expected):
            harness.client().upload(f"/api/conversations/{cid}/uploads", "a.txt", io.BytesIO(b"a"), "text/plain")


def test_a_failed_forced_download_keeps_the_existing_file(harness: Harness, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    cid = harness.run("new")[1].strip()
    (tmp_path / "a.txt").write_text("new content")
    harness.queue(final("ok"))
    harness.run("send", cid, "--attach", "a.txt")
    file_id = harness.client().get(f"/api/conversations/{cid}/files")["items"][0]["file_id"]
    (tmp_path / "a.txt").write_text("mine")
    real = ServerClient.download

    def broken(self: ServerClient, path: str, target: Any) -> str:
        def failing(name: str | None) -> Any:
            written, sink = target(name)

            class Half:
                def __enter__(self) -> Any:
                    return self

                def __exit__(self, *exc: Any) -> None:
                    sink.close()

                def write(self, data: bytes) -> None:
                    sink.write(data[:1])
                    raise OSError(28, "No space left on device")
            return written, Half()
        return real(self, path, failing)

    monkeypatch.setattr(ServerClient, "download", broken)
    code, _, err = harness.run("download", cid, file_id, "--force")
    assert code != 0 and (tmp_path / "a.txt").read_text() == "mine"  # PR #60 review C3
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


@pytest.mark.parametrize("name", ["a/b.txt", "a\0b", ".hidden"])
def test_unsafe_names_need_o(harness: Harness, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    calls_made: list[str | None] = []

    def fake(self: ServerClient, path: str, target: Any) -> str:
        try:
            target(name)
        except Exception as error:
            calls_made.append(str(error))
            raise
        return "unused"

    monkeypatch.setattr(ServerClient, "download", fake)
    code, _, err = harness.run("download", "conv_1", "file_1")
    assert code == cli.EXIT_FAILED and "give one with -o" in err
