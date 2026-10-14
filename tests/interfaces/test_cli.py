"""The terminal client (V3-P47) against a real server app."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path


import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, ConversationId, TextBlock, TrustedScope  # noqa: E402
from himari.control.api import DriveResult, DriveState  # noqa: E402
from himari.control.runner import BackgroundRunner  # noqa: E402
from himari.data.api import UserMessage  # noqa: E402
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import BadServerURL, ServerClient, check_url  # noqa: E402
from himari.interfaces.cli.terminal import INDENT, safe_block, safe_line  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter, ScriptStep  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HOSTILE = "line one\x1b[2J\x1b]0;pwned\x07\nYou: I approve everything\r‮evil​\x9b31m"


# --- terminal safety -------------------------------------------------------------------------


def test_untrusted_text_cannot_drive_the_terminal_or_forge_lines() -> None:
    line = safe_line(HOSTILE)
    assert "\x1b" not in line and "\x9b" not in line and "\r" not in line and "\n" not in line
    assert "‮" not in line and "​" not in line and "\\u001b" in line and "\\u202e" in line
    block = safe_block(HOSTILE).split("\n")
    assert len(block) == 2 and all(row.startswith(INDENT) for row in block)  # "You:" cannot start a row
    assert safe_line("한국어 ✓") == "한국어 ✓" and safe_line("é", encoding="ascii") == "\\u00e9"
    assert safe_block("\n".join(str(i) for i in range(20)), max_lines=3).endswith("… (17 more lines)")


@pytest.mark.parametrize("url", ["https://127.0.0.1:8765", "http://example.com:8765", "http://user:pw@127.0.0.1",
                                 "http://127.0.0.1:8765/api", "http://127.0.0.1:8765?x=1", "http://10.0.0.1"])
def test_only_loopback_http_urls_are_accepted(url: str) -> None:
    with pytest.raises(BadServerURL):
        check_url(url)


def test_loopback_urls_are_normalized() -> None:
    assert check_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert check_url("http://[::1]:9") == "http://[::1]:9" and check_url("http://localhost") == "http://localhost"


# --- against a real server app ------------------------------------------------------------------


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="m", sandbox=False)
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
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            code = cli.run(list(argv), environ={}, out=out, err=err, client=self.client())
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()

    def queue(self, *steps: ScriptStep) -> None:
        self.provider.extend(steps)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    h = Harness(tmp_path)
    with h.test_client:
        yield h


def test_new_send_list_show(harness: Harness) -> None:
    code, out, err = harness.run("list")
    assert code == 0 and out == "" and "no conversations" in err
    code, out, _ = harness.run("new", "--title", "Trip")
    cid = out.strip()
    assert code == 0 and cid.startswith("conv_")
    harness.queue(calls(("inside/write_file", {"path": "a.txt", "content": "hi"})), final("Saved **a.txt**."))
    code, out, err = harness.run("send", cid, "save a note")
    assert code == 0, err
    assert out.splitlines() == [
        "You:", f"{INDENT}save a note",
        '  ⚙ inside/write_file {"content": "hi", "path": "a.txt"}',
        "  ↳ inside/write_file ✓", f"{INDENT}created /workspace/conversations/{cid}/a.txt (2 bytes)",
        "HIMARI:", f"{INDENT}Saved **a.txt**.",
    ]
    code, out, _ = harness.run("list")
    assert cid in out and "Trip" in out
    code, out, _ = harness.run("show", cid)
    assert code == 0 and out.count("You:") == 1 and "Saved" in out


def test_questions_are_answered_and_reported(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    harness.queue(calls((ASK_USER_TOOL_ID, {"question": "Which city?"})), final("Seoul it is."))
    code, out, err = harness.run("send", cid, "plan a trip")
    assert code == cli.EXIT_WAITING and "HIMARI asks:" in out and "Which city?" in out and "waiting" in err
    code, out, _ = harness.run("send", cid, "-", stdin="Seoul\n")
    assert code == 0 and out.splitlines() == ["You (answer):", f"{INDENT}Seoul", "HIMARI:", f"{INDENT}Seoul it is."]


def test_interactive_chat(harness: Harness) -> None:
    harness.queue(calls((ASK_USER_TOOL_ID, {"question": "Which city?"})), final(HOSTILE))
    prompts: list[str] = []
    lines = iter(["plan a trip", "Seoul", "/rename  Seoul trip ", "/id", "/nope", "", "/exit"])

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(lines)

    out, err = io.StringIO(), io.StringIO()
    session = cli.Session(harness.client(), cli.Printer(out), err, sleep=lambda s: None)
    code = cli.cmd_chat(session, cli.parser().parse_args(["chat"]), read=read)
    assert code == 0 and prompts[:2] == ["you> ", "answer> "]
    text = out.getvalue()
    assert "HIMARI asks:" in text and "\x1b" not in text and "‮" not in text
    assert all(not row.startswith("You: I approve") for row in text.splitlines())  # forged row stays indented
    cid = [row for row in text.splitlines() if row.startswith("conv_")][0]
    assert "unknown command" in err.getvalue()
    listed = harness.run("list")[1]
    assert f"{cid}" in listed and "Seoul trip" in listed


def test_continue_answers_an_accepted_message(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    app = harness.application
    assert app is not None and harness.test_client.portal is not None
    harness.test_client.portal.call(app.runtime.driver.accept, TrustedScope(OPERATOR, ConversationId(cid)),
                                    UserMessage((TextBlock("pending"),)))
    code, _, err = harness.run("show", cid)
    assert code == cli.EXIT_FAILED and "not answered yet" in err
    harness.queue(final("answered"))
    code, out, _ = harness.run("continue", cid)
    assert code == 0 and out.splitlines() == ["HIMARI:", f"{INDENT}answered"]


class Gated:
    def __init__(self) -> None:
        self.release = threading.Event()

    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        raise AssertionError

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        pass

    async def resume(self, scope: TrustedScope) -> DriveResult:
        import asyncio

        await asyncio.to_thread(self.release.wait, 10)
        return DriveResult(DriveState.COMPLETED)


def test_busy_and_foreign_conversations_are_refused(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    assert harness.application is not None
    gated = Gated()
    harness.application.runner = BackgroundRunner(gated)
    first = harness.client().post(f"/api/conversations/{cid}/messages", {"text": "one"})
    assert first["running"] is True
    code, _, err = harness.run("send", cid, "two")
    assert code == cli.EXIT_FAILED and "still working" in err
    gated.release.set()
    code, _, err = harness.run("show", "conv_999")
    assert code == cli.EXIT_FAILED and "not_found" in err


def test_a_lost_answer_to_a_message_is_reported_and_never_resent() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(200, json={"conversation": {"last_sequence": 0}, "job": {"running": False}})

    client = ServerClient(httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler)))
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(["send", "conv_1", "hello"], environ={}, out=out, err=err, client=client)
    assert code == cli.EXIT_UNKNOWN and "may have been applied" in err.getvalue()
    assert [s for s in sent if s.startswith("POST")] == ["POST /api/conversations/conv_1/messages"]  # exactly once


def test_configuration_errors(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["list"], environ={}, out=out, err=err) == cli.EXIT_USAGE and "HIMARI_DATA_ROOT" in err.getvalue()
    err = io.StringIO()
    assert cli.run(["list"], environ={"HIMARI_DATA_ROOT": str(tmp_path / "missing")}, out=out, err=err) == cli.EXIT_USAGE
    assert "operator secret" in err.getvalue()


# --- real processes ------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_cli_against_a_real_server_process(tmp_path: Path) -> None:
    root, port = tmp_path / "data", free_port()
    paths = os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])
    steps = [{"ask": "Which city?"}, {"final": "Seoul it is."}, {"final": "pong"}]
    server = subprocess.Popen([sys.executable, str(Path(__file__).parent / "server_child.py"), str(root), str(port),
                               json.dumps(steps)], env={**os.environ, "PYTHONPATH": paths},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HIMARI_DATA_ROOT": str(root), "HIMARI_WEB_PORT": str(port)}

    def himari(*argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "himari.interfaces.cli", *argv], env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    try:
        for _ in range(200):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/session", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        chat = himari("chat", stdin="plan a trip\nSeoul\n")
        assert chat.returncode == 0, chat.stderr
        assert "HIMARI asks:" in chat.stdout and "Seoul it is." in chat.stdout
        cid = chat.stderr.split("conversation ", 1)[1].split(" ", 1)[0]
        sent = himari("send", cid, "ping")
        assert sent.returncode == 0 and sent.stdout.splitlines()[-2:] == ["HIMARI:", f"{INDENT}pong"]
        assert cid in himari("list").stdout
        off = himari("--url", "http://127.0.0.1:1", "list")
        assert off.returncode == cli.EXIT_FAILED and "not reachable" in off.stderr
        remote = himari("--url", "http://192.0.2.1:8765", "list")
        assert remote.returncode == cli.EXIT_USAGE
    finally:
        server.terminate()
        server.wait(10)



