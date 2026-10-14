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
from typing import Any


import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_fake import URL as MCP_URL  # noqa: E402
from mcp_fake import FakeMcpServer  # noqa: E402
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
    def __init__(self, tmp_path: Path, context_tokens: int | None = None, mcp: FakeMcpServer | None = None) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="m", sandbox=False,
                                                      context_tokens=context_tokens,
                                                      mcp_transport=None if mcp is None else mcp.transport())
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

    async def compact(self, scope: TrustedScope) -> DriveResult:
        return await self.resume(scope)


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





# --- PR #13 review --------------------------------------------------------------------------


def test_call_ids_repeat_across_runs_without_hiding_results(harness: Harness) -> None:
    """B1: a call is (run_id, call_id); the scripted provider reuses c0 every Run."""
    cid = harness.run("new")[1].strip()
    harness.queue(calls((ASK_USER_TOOL_ID, {"question": "Which file?"})))
    assert harness.run("send", cid, "write a file")[0] == cli.EXIT_WAITING
    harness.queue(calls(("inside/write_file", {"path": "x.txt", "content": "hello"})), final("done"))
    code, out, _ = harness.run("send", cid, "x.txt")
    assert code == 0 and "  ↳ inside/write_file ✓" in out and "You (answer):" in out
    shown = harness.run("show", cid)[1]
    assert shown.count("  ↳ inside/write_file ✓") == 1 and shown.count("x.txt") >= 2
    assert shown.count(f"{INDENT}x.txt") == 1  # the answer once, not repeated as a tool result
    events = harness.test_client.get(f"/api/conversations/{cid}/events").json()["items"]
    runs = [e["run_id"] for e in events if e["kind"] == "assistant_tool_calls"]
    assert len(set(runs)) == 2 and [e["answers_run"] for e in events if e.get("answers_call")] == [runs[0]]


def _mock(handler: Any) -> ServerClient:
    return ServerClient(httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status, body, expected", [
    (201, b"not json", cli.EXIT_UNKNOWN),                                   # committed, unreadable
    (202, b'{"x":"' + b"a" * (9 * 1024 * 1024) + b'"}', cli.EXIT_UNKNOWN),  # committed, too large
    (500, b'{"error":{"code":"internal_error"}}', cli.EXIT_UNKNOWN),        # may follow a commit
    (409, b'{"error":{"code":"busy","message":"still working"}}', cli.EXIT_FAILED),  # a definite refusal
])
def test_a_mutation_without_a_readable_answer_is_unknown_and_sent_once(status: int, body: bytes, expected: int) -> None:
    """B2: only a readable answer or a 4xx settles a mutation."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(request.url.path)
            return httpx.Response(status, content=body)
        return httpx.Response(200, json={"conversation": {"last_sequence": 0}, "job": {"running": False}})

    for argv in (["send", "conv_1", "hi"], ["new"], ["continue", "conv_1"]):
        sent.clear()
        out, err = io.StringIO(), io.StringIO()
        assert cli.run(argv, environ={}, out=out, err=err, client=_mock(handler)) == expected, (argv, err.getvalue())
        assert len(sent) == 1


def test_large_event_pages_are_read_in_smaller_pages() -> None:
    """B3: a page over the response bound is read again at half the size."""
    limits: list[int] = []
    events: list[dict[str, Any]] = [{"sequence": i, "kind": "assistant_final", "text": f"m{i}"} for i in range(1, 41)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            limit, after = int(request.url.params["limit"]), int(request.url.params["after"])
            limits.append(limit)
            if limit > 4:
                return httpx.Response(200, content=b'{"pad":"' + b"a" * (9 * 1024 * 1024) + b'"}')
            items = [e for e in events if e["sequence"] > after][:limit]
            return httpx.Response(200, json={"items": items, "last_sequence": items[-1]["sequence"] if items else after})
        return httpx.Response(200, json={"conversation": {"last_sequence": 40}, "job": {"running": False},
                                         "waiting": None, "continuable": False})

    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["show", "conv_1"], environ={}, out=out, err=err, client=_mock(handler)) == 0, err.getvalue()
    assert limits[:4] == [32, 16, 8, 4] and set(limits[4:]) == {4}
    assert out.getvalue().count("HIMARI:") == 40


def test_a_single_oversized_event_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=b'{"pad":"' + b"a" * (9 * 1024 * 1024) + b'"}')
        return httpx.Response(200, json={"conversation": {"last_sequence": 1}, "job": {"running": False}})

    err = io.StringIO()
    assert cli.run(["show", "conv_1"], environ={}, out=io.StringIO(), err=err, client=_mock(handler)) == cli.EXIT_FAILED
    assert "larger than the CLI can read" in err.getvalue()


def test_show_works_with_a_small_response_bound(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.interfaces.cli import client as cli_client

    cid = harness.run("new")[1].strip()
    for i in range(6):
        harness.queue(final(f"answer {i} " + "z" * 3000))
        assert harness.run("send", cid, f"question {i}")[0] == 0
    monkeypatch.setattr(cli_client, "MAX_RESPONSE_BYTES", 8000)  # forces smaller pages on a real server
    code, out, err = harness.run("show", cid)
    assert code == 0, err
    assert out.count("You:") == 6 and out.count("HIMARI:") == 6


def test_an_ascii_terminal_gets_ascii_marks(harness: Harness) -> None:
    """C1: the CLI's own marks never raise UnicodeEncodeError."""
    cid = harness.run("new")[1].strip()
    harness.queue(calls(("inside/write_file", {"path": "é.txt", "content": "x"})), final("done ✓"))
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
    code = cli.run(["send", cid, "go"], environ={}, out=out, err=io.StringIO(), client=harness.client())
    text = raw.getvalue().decode("ascii")
    assert code == 0 and "  * inside/write_file" in text and "  -> inside/write_file ok" in text
    assert "  | done \\u2713" in text and "\\u00e9.txt" in text


# --- context (compaction visibility) ----------------------------------------------------------


def test_show_reports_the_context_size(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    harness.queue(final("hello"))
    harness.run("send", cid, "hi")
    code, out, err = harness.run("show", cid)
    assert code == 0 and "summary" not in out  # nothing compacted: no divider
    assert "context: about " in err and "of 128,000 tokens; summarizes at 102,400" in err


def test_a_compacted_conversation_shows_where_the_summary_takes_over(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    harness = Harness(tmp_path, context_tokens=2000)
    with harness.test_client:
        cid = harness.run("new")[1].strip()
        harness.queue(*[final("SUMMARY of the start")] * 12)  # every answer and every summary alike
        for text in ("가" * 550, "가" * 550, "가" * 550, "next"):
            assert harness.run("send", cid, text)[0] == 0
        code, out, err = harness.run("show", cid)
        rows = out.splitlines()
        divider = [i for i, row in enumerate(rows) if row.startswith("── earlier messages")]
        assert code == 0 and len(divider) == 1 and "use --verbose or /context" in rows[divider[0]]
        assert rows[-4:-2] == ["You:", f"{INDENT}next"] and divider[0] < len(rows) - 4
        assert "compacted" in err
        code, out, _ = harness.run("--verbose", "show", cid)
        rows = out.splitlines()
        at = next(i for i, row in enumerate(rows) if row.startswith("── earlier messages"))
        assert rows[at + 1] == f"{INDENT}SUMMARY of the start"

        out_buffer, err_buffer = io.StringIO(), io.StringIO()
        session = cli.Session(harness.client(), cli.Printer(out_buffer), err_buffer, sleep=lambda s: None)
        lines = iter(["/context", "/exit"])
        assert cli.cmd_chat(session, cli.parser().parse_args(["chat", cid]), read=lambda prompt: next(lines)) == 0
        assert "context: about " in err_buffer.getvalue()
        assert out_buffer.getvalue().splitlines()[-2:] == [
            "Summary the model sees for earlier messages:", f"{INDENT}SUMMARY of the start"]


def test_a_compacted_conversation_on_an_ascii_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #16 review B1: the divider is the CLI's own mark too; it never raises.
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    harness = Harness(tmp_path, context_tokens=2000)
    with harness.test_client:
        cid = harness.run("new")[1].strip()
        harness.queue(*[final("SUMMARY 요약 ─ done")] * 12)
        for text in ("가" * 550, "가" * 550, "가" * 550, "next"):
            assert harness.run("send", cid, text)[0] == 0
        for verbose in ([], ["--verbose"]):
            raw = io.BytesIO()
            out = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
            code = cli.run([*verbose, "show", cid], environ={}, out=out, err=io.StringIO(), client=harness.client())
            text = raw.getvalue().decode("ascii")
            assert code == 0 and "-- earlier messages reach HIMARI only as a summary" in text
            if verbose:
                assert "  | SUMMARY \\uc694\\uc57d \\u2500 done" in text


def test_chat_says_when_the_boundary_moved_after_the_history_was_printed(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #16 review C1
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    harness = Harness(tmp_path, context_tokens=2000)
    with harness.test_client:
        cid = harness.run("new")[1].strip()
        harness.queue(*[final("SUMMARY")] * 12)
        out, err = io.StringIO(), io.StringIO()
        session = cli.Session(harness.client(), cli.Printer(out), err, sleep=lambda s: None)
        lines = iter(["가" * 550, "가" * 550, "가" * 550, "next", "/context", "/context", "/exit"])
        assert cli.cmd_chat(session, cli.parser().parse_args(["chat", cid]), read=lambda prompt: next(lines)) == 0
        notes = [row for row in err.getvalue().splitlines() if row.startswith("(compacted since")]
        assert len(notes) == 1 and "events up to #" in notes[0]  # once: the second /context saw nothing new


def test_the_context_line_names_the_model_limit_when_it_differs() -> None:
    # PR #16 review C2: the budget is where compaction aims, not where a request is refused.
    base = {"estimated_tokens": 1234, "budget_tokens": 200_000, "compact_at_tokens": 160_000, "compactions": 0}
    assert cli.context_line({**base, "hard_limit_tokens": 200_000}) == (
        "context: about 1,234 of 200,000 tokens; summarizes at 160,000")
    assert cli.context_line({**base, "hard_limit_tokens": 400_000}).endswith("; model limit 400,000")


def test_compact_now_from_the_cli(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    harness.queue(final("a1"), final("a2"), final("S-manual"))
    harness.run("send", cid, "q1")
    harness.run("send", cid, "q2")
    code, out, err = harness.run("compact", cid)
    assert code == 0 and out == "" and "(compacted: earlier messages now reach HIMARI as a summary)" in err
    assert "compacted 1 time" in err
    code, _, err = harness.run("compact", "conv_404")
    assert code == cli.EXIT_FAILED and "refused" in err

    harness.queue(final("a3"), final("S-again"))
    out_buffer, err_buffer = io.StringIO(), io.StringIO()
    session = cli.Session(harness.client(), cli.Printer(out_buffer), err_buffer, sleep=lambda s: None)
    lines = iter(["/compact", "q3", "/compact", "/exit"])  # only the latest answer is left at first
    assert cli.cmd_chat(session, cli.parser().parse_args(["chat", cid]), read=lambda prompt: next(lines)) == 0
    assert "(nothing to compact yet)" in err_buffer.getvalue() and "compacted 2 times" in err_buffer.getvalue()


def test_compact_with_nothing_to_compact(harness: Harness) -> None:
    cid = harness.run("new")[1].strip()
    code, _, err = harness.run("compact", cid)
    assert code == 0 and "(nothing to compact yet)" in err


@pytest.mark.parametrize("answers", [["a1", "a2", "S"], []])
def test_an_unanswered_message_stays_visible_after_compacting(harness: Harness, answers: list[str]) -> None:
    # PR #17 review B1: compacting (or finding nothing to compact) is not answering.
    cid = harness.run("new")[1].strip()
    if answers:
        harness.queue(*[final(a) for a in answers])
        harness.run("send", cid, "q1")
        harness.run("send", cid, "q2")
    app = harness.application
    assert app is not None and harness.test_client.portal is not None
    harness.test_client.portal.call(app.runtime.driver.accept, TrustedScope(OPERATOR, ConversationId(cid)),
                                    UserMessage((TextBlock("unanswered"),)))
    code, _, err = harness.run("compact", cid)
    expected = "(compacted: earlier messages" if answers else "(nothing to compact yet)"
    assert expected in err and "not answered yet" in err and code == cli.EXIT_FAILED
    detail = harness.client().get(f"/api/conversations/{cid}")
    assert detail["continuable"] is True and detail["job"]["state"] in ("compacted", "nothing_to_compact")
    code, _, err = harness.run("show", cid)
    assert code == cli.EXIT_FAILED and "not answered yet" in err


def test_tool_sources_from_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "mcp.json").write_text(json.dumps({"servers": {
        "calliope": {"url": MCP_URL, "tools": {"echo": {"effect": "read"}, "gone": {"effect": "read"}}},
        "lab": {"url": "http://127.0.0.1:1/mcp", "default": "off", "tools": {"x": {"effect": "read"}}},
    }}))
    (data / "mcp.json").chmod(0o600)
    fake = FakeMcpServer()
    harness = Harness(tmp_path, mcp=fake)
    with harness.test_client:
        cid = harness.run("new")[1].strip()
        code, out, _ = harness.run("tools", cid)
        assert code == 0 and out.splitlines() == [
            "calliope: on (default; default on) — 1 tool",
            "  - gone: the server does not offer it",
            "lab: off (default; default off) — not connected (restart the server after fixing it)",
            "  - unavailable: initialize: cannot connect (ConnectError)",
        ]
        code, out, _ = harness.run("tools", cid, "off", "calliope")
        assert code == 0 and out.splitlines()[0] == "calliope: off (this conversation; default on) — 1 tool"
        code, _, err = harness.run("tools", cid, "on", "nope")
        assert code == cli.EXIT_FAILED and "No such tool source" in err

        lines = iter(["/tools on calliope", "/tools default", "/tools", "/exit"])
        out_buffer, err_buffer = io.StringIO(), io.StringIO()
        session = cli.Session(harness.client(), cli.Printer(out_buffer), err_buffer, sleep=lambda s: None)
        assert cli.cmd_chat(session, cli.parser().parse_args(["chat", cid]), read=lambda p: next(lines)) == 0
        shown = out_buffer.getvalue().splitlines()
        assert shown[0] == "calliope: on (default; default on) — 1 tool"  # same as the default: not kept
        assert "usage: /tools" in err_buffer.getvalue()
