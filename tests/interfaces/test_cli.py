"""The terminal client (V3-P47), as it is since V3-P98: speaking first and the memory passes."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope  # noqa: E402
from himari.data.api import (  # noqa: E402
    EpisodePlan,
    MemoryCategory,
    PassAction,
    PassItem,
    PassKind,
    PassStart,
    UserMessage,
)
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import BadServerURL, ServerClient, check_url  # noqa: E402
from himari.interfaces.cli.terminal import safe_line  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

HOSTILE = "line one\x1b[2J\x1b]0;pwned\x07\nYou: I approve everything\r‮evil​\x9b31m"


# --- terminal safety -------------------------------------------------------------------------


def test_untrusted_text_cannot_drive_the_terminal_or_forge_lines() -> None:
    line = safe_line(HOSTILE)
    assert "\x1b" not in line and "\x9b" not in line and "\r" not in line and "\n" not in line
    assert "‮" not in line and "​" not in line and "\\u001b" in line and "\\u202e" in line
    assert safe_line("한국어 ✓") == "한국어 ✓" and safe_line("é", encoding="ascii") == "\\u00e9"


@pytest.mark.parametrize("url", ["https://127.0.0.1:8765", "http://example.com:8765", "http://user:pw@127.0.0.1",
                                 "http://127.0.0.1:8765/api", "http://127.0.0.1:8765?x=1", "http://10.0.0.1"])
def test_only_loopback_http_urls_are_accepted(url: str) -> None:
    with pytest.raises(BadServerURL):
        check_url(url)


def test_loopback_urls_are_normalized() -> None:
    assert check_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert check_url("http://[::1]:9") == "http://[::1]:9" and check_url("http://localhost") == "http://localhost"


# --- the transport ---------------------------------------------------------------------------------


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
        sent.append(request.url.path)
        return httpx.Response(status, content=body)

    for argv in (["proactive", "try"], ["memory", "backfill", "conv_1"], ["memory", "retry", "conv_1", "pass_1"]):
        sent.clear()
        out, err = io.StringIO(), io.StringIO()
        assert cli.run(argv, environ={}, out=out, err=err, client=_mock(handler)) == expected, (argv, err.getvalue())
        assert len(sent) == 1


def test_a_lost_answer_is_reported_and_never_resent() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        raise httpx.ReadTimeout("lost", request=request)

    out, err = io.StringIO(), io.StringIO()
    code = cli.run(["memory", "backfill", "conv_1"], environ={}, out=out, err=err, client=_mock(handler))
    assert code == cli.EXIT_UNKNOWN and "may have been applied" in err.getvalue()
    assert sent == ["POST /api/memory-passes/backfill"]  # exactly once


def test_a_server_that_is_not_running_is_said() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["proactive"], environ={}, out=out, err=err, client=_mock(handler)) == cli.EXIT_FAILED
    assert "not reachable" in err.getvalue()


def test_configuration_errors(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["proactive"], environ={}, out=out, err=err) == cli.EXIT_USAGE and "HIMARI_DATA_ROOT" in err.getvalue()
    err = io.StringIO()
    environ = {"HIMARI_DATA_ROOT": str(tmp_path / "missing")}
    assert cli.run(["proactive"], environ=environ, out=out, err=err) == cli.EXIT_USAGE
    assert "operator secret" in err.getvalue()


@pytest.mark.parametrize("argv", [[], ["chat"], ["list"], ["send", "conv_1", "hi"]])
def test_what_the_web_does_is_not_a_command(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.run(argv, environ={}, out=io.StringIO(), err=io.StringIO())
    assert stopped.value.code == cli.EXIT_USAGE


# --- against a real server app ------------------------------------------------------------------


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig("127.0.0.1", 8765, frozenset({"testserver"}), frozenset({"http://testserver"}), False,
                           str(tmp_path / "data"), "m", None, False)
        self.test_client = TestClient(web.create_app(config, opener))

    def authorized(self) -> TestClient:
        assert self.application is not None
        self.test_client.headers["authorization"] = f"Bearer {derive_cli_bearer(self.application.operator_secret)}"
        return self.test_client

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(list(argv), environ={}, out=out, err=err, client=ServerClient(self.authorized()))
        return code, out.getvalue(), err.getvalue()

    def new(self) -> str:
        created = self.authorized().post("/api/conversations", json={})
        assert created.status_code in (200, 201), created.text
        return str(created.json()["id"])


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path)
    with h.test_client:
        yield h


def seed_pass(harness: Harness, cid: str, items: list[Any], episode: EpisodePlan) -> str:
    app, portal = harness.application, harness.test_client.portal
    assert app is not None and portal is not None
    scope = TrustedScope(PrincipalId("operator"), ConversationId(cid))
    portal.call(app.data.append_event, scope, UserMessage((TextBlock("Things worth remembering."),)))
    found = portal.call(app.data.last_event, scope)
    assert found is not None
    last = found[0]
    cursor = portal.call(app.data.read_memory_cursor, scope).through_sequence
    pass_id: str = portal.call(app.data.begin_pass, scope, PassStart(PassKind.LIVE, cursor, last, last, "p", "m"))
    portal.call(lambda: app.data.apply_pass(pass_id, items, episode, now=datetime.now(UTC)))
    return pass_id


def test_speaking_first_from_the_command_line(harness: Harness) -> None:
    code, out, err = harness.run("proactive")
    assert code == cli.EXIT_OK and err == ""
    assert out == "Reaching out: off (HIMARI_PROACTIVE=0)\nCurrent device: none (away)\n"
    assert harness.run("proactive", "status")[1] == out
    code, out, err = harness.run("proactive", "try")
    assert (code, out) == (cli.EXIT_FAILED, "") and err.startswith("refused (proactive_off)")


def test_a_conversations_memory_passes_from_the_command_line(harness: Harness) -> None:
    cid = harness.new()
    assert harness.run("memory", "passes", cid) == (cli.EXIT_OK, "", "(no memory passes applied here)\n")
    now = datetime.now(UTC)
    pass_id = seed_pass(harness, cid, [
        PassItem(0, "{}", PassAction.ADD, category=MemoryCategory.PROJECT, text="Builds HIMARI.")],
        EpisodePlan("Talked about HIMARI.", now, now, (1,), ()))
    code, out, _ = harness.run("memory", "passes", cid)
    assert code == cli.EXIT_OK and f"{pass_id} (through event 1):" in out
    assert "  0  remembered  Builds HIMARI.  (undo in the web)" in out
    code, _, err = harness.run("memory", "backfill", cid)
    assert code == cli.EXIT_FAILED and err.startswith("refused (memory_passes_off)")
    code, _, err = harness.run("memory", "retry", cid, pass_id)
    assert code == cli.EXIT_FAILED and err.startswith("refused (")
    assert harness.run("memory", "passes", "conv_999")[0] == cli.EXIT_FAILED  # not this operator's, or missing


@pytest.mark.parametrize("argv", [["memory"], ["memory", "passes"], ["memory", "backfill", "a", "b"],
                                  ["memory", "retry", "conv_1"]])
def test_memory_usage(harness: Harness, argv: list[str]) -> None:
    code, out, err = harness.run(*argv)
    assert (code, out) == (cli.EXIT_USAGE, "") and err.startswith("usage: himari memory passes")
