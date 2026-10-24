"""Web server (V3-P46): security envelope, sign-in, and the conversation API
over a real Application (SQLite, sandbox off, scripted provider)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from mcp_fake import URL as MCP_URL  # noqa: E402
from mcp_fake import ECHO_SCHEMA, VIEW_HTML, VIEW_URI, FakeMcpServer, view_entry  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap import application as bootstrap  # noqa: E402
from himari.bootstrap.application import (  # noqa: E402
    DEFAULT_TURN_BUDGET,
    DEFAULT_UNATTENDED_RUNS,
    Application,
    workspace_for,
)
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, ConversationId, PrincipalId, TextBlock, TrustedScope  # noqa: E402
from himari.sandbox.api import SandboxState  # noqa: E402
from himari.control.api import DriveResult, DriveState, StopSignal, TurnProgress  # noqa: E402
from himari.control.runner import BackgroundRunner  # noqa: E402
from himari.control.turn import MIN_OUTPUT_RESERVE  # noqa: E402
from himari.data.api import UserMessage  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import MIN_TURN_BUDGET, WebConfig, WebConfigError  # noqa: E402
from himari.interfaces.web.sandbox import SandboxGrant, SandboxSigner, create_sandbox_app, view_csp  # noqa: E402
from himari.provider.api import ProviderCapabilities  # noqa: E402
from himari.provider.scripted import ScriptedAdapter, ScriptStep  # noqa: E402

ORIGIN = "http://testserver"


def config(tmp_path: Path) -> WebConfig:
    return WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                     allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                     model_id="scripted-model", reasoning_effort=None, sandbox=False)


class Server:
    def __init__(self, tmp_path: Path, script: list[ScriptStep], context_tokens: int | None = None,
                 mcp: FakeMcpServer | None = None, signer: SandboxSigner | None = None,
                 turn: dict[str, Any] | None = None, provider: ScriptedAdapter | None = None,
                 models: tuple[tuple[str, str | None], ...] = ()) -> None:
        self.provider = ScriptedAdapter(script) if provider is None else provider
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, context_tokens=context_tokens,
                                                      mcp_transport=None if mcp is None else mcp.transport(),
                                                      models=models, **(turn or {}))
            return self.application

        self.client = TestClient(web.create_app(config(tmp_path), opener, signer))

    @property
    def secret(self) -> str:
        assert self.application is not None
        return self.application.operator_secret

    def sign_in(self) -> None:
        response = self.client.post("/api/session", json={"token": self.secret}, headers={"origin": ORIGIN})
        assert response.status_code == 204, response.text

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(path, json=body if body is not None else {}, headers={"origin": ORIGIN})

    def wait_idle(self, cid: str) -> dict[str, Any]:
        for _ in range(200):
            detail = self.client.get(f"/api/conversations/{cid}").json()
            if not detail["job"]["running"]:
                return detail  # type: ignore[no-any-return]
            time.sleep(0.02)
        raise AssertionError("job did not finish")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, [])
    with s.client:
        yield s


def queue(server: Server, *steps: ScriptStep) -> None:
    server.provider.extend(steps)


# --- configuration -------------------------------------------------------------------------


def test_configuration_rules() -> None:
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).allowed_origins == {"http://127.0.0.1:8765", "http://localhost:8765"}
    with pytest.raises(WebConfigError, match="wildcards"):
        WebConfig.from_env({**base, "HIMARI_WEB_ALLOWED_HOSTS": "*"})
    with pytest.raises(WebConfigError, match="scheme://host"):
        WebConfig.from_env({**base, "HIMARI_WEB_ALLOWED_ORIGINS": "https://h.example/path"})
    with pytest.raises(WebConfigError, match="HIMARI_DATA_ROOT"):
        WebConfig.from_env({})
    with pytest.raises(WebConfigError):
        WebConfig.from_env({**base, "HIMARI_SANDBOX": "maybe"})
    assert WebConfig.from_env(base).context_tokens is None
    assert WebConfig.from_env({**base, "HIMARI_CONTEXT_TOKENS": " 120000 "}).context_tokens == 120_000
    for bad in ("lots", "999", "-5"):
        with pytest.raises(WebConfigError, match="HIMARI_CONTEXT_TOKENS"):
            WebConfig.from_env({**base, "HIMARI_CONTEXT_TOKENS": bad})


def test_the_offered_models_are_configured() -> None:
    # V3-P64 §3: HIMARI_MODEL is the default; HIMARI_MODELS lists every offered model, id[=effort].
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).models == ()  # unset: the default model alone
    assert WebConfig.from_env({**base, "HIMARI_MODELS": "  "}).models == ()
    config = WebConfig.from_env({**base, "HIMARI_MODEL": "a/one", "HIMARI_REASONING_EFFORT": "high",
                                 "HIMARI_MODELS": " b/two:free = low , a/one,c/three=  "})
    assert config.models == (("b/two:free", "low"), ("a/one", None), ("c/three", None))
    assert (config.model_id, config.reasoning_effort) == ("a/one", "high")  # applied to a/one by Application
    for raw, message in [("b/two", "must list the default model"), ("a/one,a/one=low", "twice"),
                         ("a/one,=low", "not model\\[=effort\\]"), ("a/one,b two", "not model"),
                         ("a/one,b=lo w", "not model")]:
        with pytest.raises(WebConfigError, match=message):
            WebConfig.from_env({**base, "HIMARI_MODEL": "a/one", "HIMARI_MODELS": raw})


def test_the_turn_guards_are_configured() -> None:
    # V3-P62: a token budget and the unattended pause by default, no Run limit; 0 turns one off.
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    default = WebConfig.from_env(base)
    assert (default.turn_budget, default.unattended_runs, default.max_runs) == (3_000_000, 20, None)
    assert (default.turn_budget, default.unattended_runs) == (DEFAULT_TURN_BUDGET, DEFAULT_UNATTENDED_RUNS)
    tuned = WebConfig.from_env({**base, "HIMARI_TURN_TOKEN_BUDGET": " 250000 ", "HIMARI_UNATTENDED_RUNS": "0",
                                "HIMARI_MAX_RUNS": "40"})
    assert (tuned.turn_budget, tuned.unattended_runs, tuned.max_runs) == (250_000, None, 40)
    limited = WebConfig.from_env({**base, "HIMARI_TURN_TOKEN_BUDGET": "0", "HIMARI_MAX_RUNS": "12"})
    assert (limited.turn_budget, limited.max_runs) == (None, 12)
    with pytest.raises(WebConfigError, match="needs HIMARI_MAX_RUNS"):  # a turn needs one of the two
        WebConfig.from_env({**base, "HIMARI_TURN_TOKEN_BUDGET": "0"})
    # PR #48 review C2: a budget smaller than two answer reserves could never send a request.
    assert MIN_TURN_BUDGET == 2 * MIN_OUTPUT_RESERVE
    assert WebConfig.from_env({**base, "HIMARI_TURN_TOKEN_BUDGET": "16384"}).turn_budget == 16_384
    for name, bad in [("HIMARI_TURN_TOKEN_BUDGET", "lots"), ("HIMARI_TURN_TOKEN_BUDGET", "16383"),
                      ("HIMARI_TURN_TOKEN_BUDGET", "-1"), ("HIMARI_UNATTENDED_RUNS", "1001"),
                      ("HIMARI_MAX_RUNS", "1.5")]:
        with pytest.raises(WebConfigError, match=name):
            WebConfig.from_env({**base, name: bad})


@pytest.mark.parametrize("settings, message", [
    # PR #12 re-review B2: the server speaks plain HTTP, so it never listens beyond loopback,
    # whatever origin is declared (an Origin header proves nothing about the connection).
    ({"HIMARI_WEB_HOST": "0.0.0.0", "HIMARI_WEB_ALLOWED_HOSTS": "server.example.com",
      "HIMARI_WEB_ALLOWED_ORIGINS": "https://server.example.com"}, "not a loopback address"),
    ({"HIMARI_WEB_HOST": "0.0.0.0", "HIMARI_WEB_ALLOWED_HOSTS": "server.example.com",
      "HIMARI_WEB_ALLOWED_ORIGINS": "http://server.example.com:8765"}, "not a loopback address"),
    ({"HIMARI_WEB_HOST": "192.168.0.5"}, "not a loopback address"),
    ({"HIMARI_WEB_HOST": "::"}, "not a loopback address"),
    ({"HIMARI_WEB_ALLOWED_HOSTS": "server.example.com",
      "HIMARI_WEB_ALLOWED_ORIGINS": "http://server.example.com"}, "clear text"),  # a plain-http proxy
    ({"HIMARI_WEB_ALLOWED_HOSTS": "server.example.com", "HIMARI_WEB_ALLOWED_ORIGINS": "https://server.example.com",
      "HIMARI_WEB_SECURE_COOKIE": "0"}, "must stay Secure"),
])
def test_tokens_never_travel_in_clear_text(settings: dict[str, str], message: str) -> None:
    with pytest.raises(WebConfigError, match=message):
        WebConfig.from_env({"HIMARI_DATA_ROOT": "/srv/himari", **settings})


def test_remote_use_is_a_loopback_server_behind_a_tls_proxy() -> None:
    proxied = WebConfig.from_env({"HIMARI_DATA_ROOT": "/srv/himari", "HIMARI_WEB_ALLOWED_HOSTS": "server.example.com",
                                  "HIMARI_WEB_ALLOWED_ORIGINS": "https://server.example.com:443"})
    assert proxied.host == "127.0.0.1" and proxied.secure_cookie
    assert proxied.allowed_origins == {"https://server.example.com"}
    assert proxied.allowed_hosts == {"server.example.com", "127.0.0.1", "localhost"}  # the local CLI still works
    for host in ("127.0.0.1", "::1", "localhost"):
        assert WebConfig.from_env({"HIMARI_DATA_ROOT": "/srv/himari", "HIMARI_WEB_HOST": host}).host == host
    local = WebConfig.from_env({"HIMARI_DATA_ROOT": "/srv/himari", "HIMARI_WEB_ALLOWED_ORIGINS": "http://[::1]:8765"})
    assert not local.secure_cookie


def test_the_entry_point_refuses_a_non_loopback_bind_before_listening(monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn

    from himari.interfaces.web.__main__ import main

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("a socket would have been opened"))
    for name, value in {"HIMARI_DATA_ROOT": "/nonexistent", "HIMARI_WEB_HOST": "0.0.0.0", "OPENROUTER_API_KEY": "x",
                        "HIMARI_WEB_ALLOWED_HOSTS": "server.example.com",
                        "HIMARI_WEB_ALLOWED_ORIGINS": "https://server.example.com"}.items():
        monkeypatch.setenv(name, value)
    assert main([]) == 2
    assert "not a loopback address" in capsys.readouterr().err


def test_the_entry_point_refuses_a_bad_mcp_json_before_listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn

    from himari.interfaces.web.__main__ import main

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("a socket would have been opened"))
    (tmp_path / "mcp.json").write_text('{"servers": {"calliope": {"url": "http://example.com/mcp", "tools": {}}}}')
    (tmp_path / "mcp.json").chmod(0o600)
    monkeypatch.setenv("HIMARI_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert main([]) == 2
    assert "configuration error" in capsys.readouterr().err


# --- envelope ------------------------------------------------------------------------------


def test_static_ui_and_security_headers(server: Server) -> None:
    page = server.client.get("/")
    assert page.status_code == 200 and "/assets/app.js" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["x-frame-options"] == "DENY"
    for name in ("app.js", "app.css", "rich_text.js", "app_view.js", "appearance.js"):
        assert server.client.get(f"/assets/{name}").status_code == 200
    assert server.client.get("/assets/../app.py").status_code == 404
    script = server.client.get("/assets/katex/katex.min.js")
    assert script.status_code == 200 and script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert server.client.get("/assets/katex/katex.min.css").headers["content-type"] == "text/css; charset=utf-8"
    font = server.client.get("/assets/katex/fonts/KaTeX_Main-Regular.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2" and font.content[:4] == b"wOF2"
    for unlisted in ("katex/SOURCE", "katex/LICENSE", "katex/fonts/KaTeX_Main-Regular.ttf", "katex%2F..%2Fapp.js",
                     "katex/fonts%2F..%2F..%2Fapp.js"):
        assert server.client.get(f"/assets/{unlisted}").status_code == 404
    assert server.client.get("/assets/index.html").status_code == 404
    api = server.client.get("/api/session")
    assert api.headers["cache-control"] == "no-store" and api.json()["authenticated"] is False


def test_untrusted_host_and_large_bodies_are_refused(server: Server) -> None:
    assert server.client.get("/", headers={"host": "evil.example"}).status_code == 400
    big = server.client.post("/api/session", content=b'{"token": "' + b"x" * 300_000 + b'"}',
                             headers={"origin": ORIGIN, "content-type": "application/json"})
    assert big.status_code == 413 and big.json()["error"]["code"] == "request_too_large"


def test_api_requires_sign_in_and_errors_do_not_echo_input(server: Server) -> None:
    for method, path in [("GET", "/api/conversations"), ("POST", "/api/conversations"),
                         ("GET", "/api/conversations/conv_1/events")]:
        response = server.client.request(method, path, json={}, headers={"origin": ORIGIN})
        assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"
    bad = server.client.post("/api/session", json={"token": "<script>guess</script>"}, headers={"origin": ORIGIN})
    assert bad.status_code == 401 and "guess" not in bad.text
    odd = server.client.post("/api/session", json={"token": 5, "extra": "<b>x</b>"}, headers={"origin": ORIGIN})
    assert odd.status_code == 400 and "<b>" not in odd.text


def test_sign_in_cookie_and_sign_out(server: Server) -> None:
    response = server.client.post("/api/session", json={"token": server.secret}, headers={"origin": ORIGIN})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie and server.secret not in cookie
    session = server.client.get("/api/session").json()
    assert session == {"authenticated": True, "model": "scripted-model", "sandbox": "unavailable"}
    assert server.client.get("/api/conversations").status_code == 200
    assert server.client.delete("/api/session", headers={"origin": ORIGIN}).status_code == 204
    assert server.client.get("/api/conversations").status_code == 401


def test_the_server_info_is_for_the_signed_in_operator(server: Server) -> None:
    assert server.client.get("/api/server").status_code == 401
    server.sign_in()
    info = server.client.get("/api/server").json()
    assert info == {"model": "scripted-model", "sandbox": "unavailable", "context_budget": 128_000, "trash_days": 30,
                    "images": False,  # V3-P67: no image sandbox in this test
                    "documents": {"read": False, "scripts": False, "libraries": {}},  # V3-P70: no toolkit
                    "models": [{"id": "scripted-model", "default_effort": None, "efforts": [], "window_known": False,
                                "default": True, "images": False}],
                    "turn": {"token_budget": 3_000_000, "unattended_runs": 20, "max_runs": None,
                             "no_progress_runs": 3, "seen_seconds": 60.0}}  # V3-P62's defaults


def test_the_server_info_follows_a_sandbox_that_broke_after_startup(server: Server) -> None:
    # PR #24 review B1: a setup failure while running a command latches
    # SETUP_FAILED in the backend; the startup value must not be shown instead.
    server.sign_in()
    application = server.application
    assert application is not None

    class Backend:
        state = SandboxState.ENFORCED

    backend = Backend()
    application.inside.sandbox = backend  # type: ignore[assignment]
    assert server.client.get("/api/server").json()["sandbox"] == "enforced"
    backend.state = SandboxState.SETUP_FAILED
    assert server.client.get("/api/server").json()["sandbox"] == "setup_failed"
    assert server.client.get("/api/session").json()["sandbox"] == "setup_failed"
    assert application.sandbox_state is SandboxState.UNAVAILABLE  # the startup value is kept as such


def test_cookie_mutations_need_an_allowed_origin(server: Server) -> None:
    server.sign_in()
    assert server.client.post("/api/conversations", json={}).status_code == 403
    assert server.client.post("/api/conversations", json={}, headers={"origin": "http://evil.example"}).status_code == 403
    assert server.client.post("/api/conversations", json={}, headers={"origin": "null"}).status_code == 403
    assert server.post("/api/conversations").status_code == 201
    # A bearer header does not exempt a request that also carries the cookie.
    sneaky = server.client.post("/api/conversations", json={}, headers={"authorization": "Bearer x"})
    assert sneaky.status_code == 403


def test_cli_bearer_works_without_origin_and_the_secret_itself_does_not(server: Server) -> None:
    bearer = derive_cli_bearer(server.secret)
    client = server.client
    client.cookies.clear()
    created = client.post("/api/conversations", json={"title": "from cli"}, headers={"authorization": f"Bearer {bearer}"})
    assert created.status_code == 201 and created.json()["title"] == "from cli"
    assert client.get("/api/conversations", headers={"authorization": f"Bearer {server.secret}"}).status_code == 401
    assert client.get("/api/conversations", headers={"authorization": "Bearer wrong"}).status_code == 401


# --- conversations -----------------------------------------------------------------------------


def test_conversation_flow_with_a_tool_and_a_question(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations", {"title": "  Trip  "}).json()["id"]
    queue(server, calls(("inside/write_file", {"path": "plan.txt", "content": "draft"})),
          calls((ASK_USER_TOOL_ID, {"question": "Which city?"})))
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "plan a trip"}).status_code == 202
    detail = server.wait_idle(cid)
    assert detail["waiting"]["question"] == "Which city?" and detail["job"]["state"] == "waiting_for_user"
    assert detail["conversation"]["title"] == "Trip" and detail["conversation"]["preview"] == "plan a trip"

    queue(server, final("Seoul it is."))
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "Seoul"}).status_code == 202
    detail = server.wait_idle(cid)
    assert detail["waiting"] is None and detail["job"]["state"] == "completed" and detail["job"]["generation"] == 2

    events = server.client.get(f"/api/conversations/{cid}/events").json()
    kinds = [e["kind"] for e in events["items"]]
    assert kinds == ["user_message", "assistant_tool_calls", "tool_result", "assistant_tool_calls",
                     "user_message", "tool_result", "assistant_final"]
    write_result = events["items"][2]
    assert write_result["is_error"] is False and "plan.txt" in write_result["text"]
    assert write_result["run_id"] == events["items"][1]["run_id"] != events["items"][3]["run_id"]  # (run, call) identity
    assert events["items"][4]["answers_run"] == events["items"][3]["run_id"]
    assert events["items"][4]["answers_call"] == events["items"][3]["calls"][0]["call_id"]
    later = server.client.get(f"/api/conversations/{cid}/events?after=5").json()
    assert [e["sequence"] for e in later["items"]] == [6, 7] and later["last_sequence"] == 7
    listed = server.client.get("/api/conversations").json()
    assert listed["items"][0]["id"] == cid and listed["next_offset"] is None
    assert server.client.patch(f"/api/conversations/{cid}", json={"title": "Seoul"},
                               headers={"origin": ORIGIN}).json()["title"] == "Seoul"


def test_foreign_and_missing_conversations_look_the_same(server: Server) -> None:
    server.sign_in()
    application = server.application
    assert application is not None and server.client.portal is not None
    foreign = server.client.portal.call(application.create_conversation, PrincipalId("someone-else"))
    responses = []
    for cid in (foreign, "conv_404"):
        responses += [server.client.get(f"/api/conversations/{cid}"),
                      server.post(f"/api/conversations/{cid}/messages", {"text": "hi"}),
                      server.post(f"/api/conversations/{cid}/resume"),
                      server.client.patch(f"/api/conversations/{cid}", json={"title": "x"}, headers={"origin": ORIGIN}),
                      server.client.get(f"/api/conversations/{cid}/events"),
                      server.client.get(f"/api/conversations/{cid}/context"),
                      server.post(f"/api/conversations/{cid}/compact"),
                      server.post(f"/api/conversations/{cid}/pause"),
                      server.post(f"/api/conversations/{cid}/cancel"),
                      server.post(f"/api/conversations/{cid}/dismiss"),
                      server.client.get(f"/api/conversations/{cid}/model"),
                      server.client.put(f"/api/conversations/{cid}/model", json={"model": "scripted-model"},
                                        headers={"origin": ORIGIN})]
    assert {r.status_code for r in responses} == {404}
    assert {r.text for r in responses} == {'{"error":{"code":"not_found","message":"No such conversation."}}'}
    assert server.client.get("/api/conversations").json()["items"] == []
    assert application.runner.status(TrustedScope(PrincipalId("someone-else"), foreign)).generation == 0


def test_invalid_messages_and_pages_are_refused(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    for body in ({"text": "   "}, {"text": "x" * (web.MAX_MESSAGE_CHARS + 1)}, {"text": 3}, {}):
        assert server.post(f"/api/conversations/{cid}/messages", body).status_code == 400
    for query in ("limit=0", "limit=201", "offset=-1"):
        assert server.client.get(f"/api/conversations?{query}").status_code == 400
    assert server.client.get(f"/api/conversations/{cid}/events?after=-1").status_code == 400
    assert server.client.patch(f"/api/conversations/{cid}", json={"title": "t" * 201},
                               headers={"origin": ORIGIN}).status_code == 400


class GatedDriver:
    def __init__(self) -> None:
        self.release = threading.Event()

    async def drive(self, scope: TrustedScope, message: UserMessage, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        raise AssertionError("the runner accepts, then resumes")

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        pass

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        await asyncio.to_thread(self.release.wait, 10)
        return DriveResult(DriveState.COMPLETED)

    async def compact(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        await asyncio.to_thread(self.release.wait, 10)
        return DriveResult(DriveState.COMPACTED)

    async def cancel(self, scope: TrustedScope) -> DriveResult:
        return DriveResult(DriveState.IDLE)


def test_a_second_message_while_working_is_busy(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.application is not None
    gated = GatedDriver()
    server.application.runner = BackgroundRunner(gated)
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "one"}).status_code == 202
    busy = server.post(f"/api/conversations/{cid}/messages", {"text": "two"})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    assert server.client.get(f"/api/conversations/{cid}").json()["job"]["running"] is True
    gated.release.set()
    assert server.wait_idle(cid)["job"]["state"] == "completed"


def test_pause_and_cancel_reach_the_running_job_and_cancel_when_idle_starts_one(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.application is not None
    gated = GatedDriver()
    server.application.runner = BackgroundRunner(gated)
    idle = server.post(f"/api/conversations/{cid}/pause")
    assert idle.status_code == 202 and idle.json()["running"] is False  # nothing to pause
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "one"}).status_code == 202
    paused = server.post(f"/api/conversations/{cid}/pause").json()
    assert paused["running"] is True and paused["stop_requested"] == "pause"
    cancelling = server.post(f"/api/conversations/{cid}/cancel").json()
    assert cancelling["stop_requested"] == "cancel"
    assert server.client.get(f"/api/conversations/{cid}").json()["job"]["stop_requested"] == "cancel"
    gated.release.set()
    server.wait_idle(cid)
    late = server.post(f"/api/conversations/{cid}/cancel").json()  # idle: a stop request closes nothing
    assert late["running"] is False and late["state"] == "completed"
    started = server.post(f"/api/conversations/{cid}/dismiss")  # closes what was left waiting
    assert started.status_code == 202 and started.json()["running"] is True
    assert server.wait_idle(cid)["job"]["state"] == "idle"
    no_origin = server.client.post(f"/api/conversations/{cid}/dismiss", json={})
    assert no_origin.status_code == 403  # a cookie request needs the Origin check, like every POST


def test_no_response_ever_contains_the_secret_or_the_bearer(server: Server) -> None:
    server.sign_in()
    texts = [server.client.get(p).text for p in ("/", "/api/session", "/api/conversations", "/assets/app.js")]
    texts.append(server.post("/api/conversations").text)
    bearer = derive_cli_bearer(server.secret)
    assert not any(server.secret in t or bearer in t for t in texts)


def test_an_unanswered_accepted_message_is_reported_as_continuable(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.client.get(f"/api/conversations/{cid}").json()["continuable"] is False
    application = server.application
    assert application is not None and server.client.portal is not None
    scope = TrustedScope(OPERATOR, cid)  # the signed-in operator
    server.client.portal.call(application.runtime.driver.accept, scope, UserMessage((TextBlock("pending"),)))
    detail = server.client.get(f"/api/conversations/{cid}").json()
    assert detail["continuable"] is True and detail["job"]["running"] is False
    queue(server, final("answered"))
    assert server.post(f"/api/conversations/{cid}/resume").status_code == 202
    detail = server.wait_idle(cid)
    assert detail["continuable"] is False and detail["job"]["state"] == "completed"


# --- context (compaction visibility) ----------------------------------------------------------


def test_context_reports_the_estimate_and_the_budget(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, final("hello"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "hi"})
    server.wait_idle(cid)
    context = server.client.get(f"/api/conversations/{cid}/context").json()
    assert context["enabled"] is True
    assert context["estimated_tokens"] > 0 and context["measured"] is False  # the scripted model reports no usage
    assert (context["budget_tokens"], context["compact_at_tokens"]) == (128_000, 102_400)  # unknown window
    assert context["hard_limit_tokens"] == 128_000
    assert (context["compactions"], context["covered_through_sequence"], context["summary"]) == (0, None, None)


def test_context_shows_where_the_summary_takes_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    server = Server(tmp_path, [], context_tokens=2800)
    with server.client:
        server.sign_in()
        cid = server.post("/api/conversations").json()["id"]
        queue(server, *[final("SUMMARY <of> the start")] * 12)  # every answer and every summary alike
        for text in ("가" * 550, "가" * 550, "가" * 550, "next"):
            server.post(f"/api/conversations/{cid}/messages", {"text": text})
            server.wait_idle(cid)
        context = server.client.get(f"/api/conversations/{cid}/context").json()
        assert context["compactions"] >= 1 and context["summary"] == "SUMMARY <of> the start"
        covered = context["covered_through_sequence"]
        events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
        assert 0 < covered < events[-1]["sequence"]  # the exact history is all still there
        assert [e["kind"] for e in events] == ["user_message", "assistant_final"] * 4


def test_compact_now_through_the_api(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, final("a1"), final("a2"), final("S-manual <b>"))
    for text in ("q1", "q2"):
        server.post(f"/api/conversations/{cid}/messages", {"text": text})
        server.wait_idle(cid)
    response = server.post(f"/api/conversations/{cid}/compact")
    assert response.status_code == 202 and response.json()["running"] is True
    detail = server.wait_idle(cid)
    assert detail["job"]["state"] == "compacted" and detail["job"]["compactions"] == 1
    context = server.client.get(f"/api/conversations/{cid}/context").json()
    assert context["summary"] == "S-manual <b>" and context["compactions"] == 1
    assert len(server.client.get(f"/api/conversations/{cid}/events").json()["items"]) == 4  # no new answer
    assert server.client.post(f"/api/conversations/{cid}/compact").status_code == 403  # cookie mutation needs Origin


# --- MCP servers (V3-P53) ----------------------------------------------------------------------


@pytest.fixture
def mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Server, FakeMcpServer]]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "mcp.json").write_text(
        '{"servers": {"calliope": {"url": "%s", "default": "off", "tools": {"echo": {"effect": "read"}}}}}' % MCP_URL)
    (data / "mcp.json").chmod(0o600)
    fake = FakeMcpServer()
    s = Server(tmp_path, [], mcp=fake)
    with s.client:
        yield s, fake


def put_source(server: Server, cid: str, source: str, enabled: Any) -> Any:
    return server.client.put(f"/api/conversations/{cid}/tools/{source}", json={"enabled": enabled},
                             headers={"origin": ORIGIN})


def test_tool_sources_are_listed_and_chosen_per_conversation(mcp_server: tuple[Server, FakeMcpServer]) -> None:
    server, fake = mcp_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    other = server.post("/api/conversations").json()["id"]
    (source,) = server.client.get(f"/api/conversations/{cid}/tools").json()["sources"]
    assert source == {"source": "calliope", "enabled": False, "default": False, "choice": None, "available": True,
                      "tools": ["calliope/echo"], "problems": []}

    queue(server, calls(("calliope/echo", {"text": "off"})), final("one"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "try"})
    server.wait_idle(cid)
    assert fake.calls == []  # off by default: denied
    assert "calliope/echo" not in [d.tool_id for d in server.provider.prepared_inputs[0].tool_catalog]

    (source,) = put_source(server, cid, "calliope", True).json()["sources"]
    assert source["enabled"] is True and source["choice"] is True
    assert server.client.get(f"/api/conversations/{other}/tools").json()["sources"][0]["enabled"] is False
    queue(server, calls(("calliope/echo", {"text": "on"})), final("two"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "again"})
    server.wait_idle(cid)
    assert fake.calls == [("echo", {"text": "on"})]
    events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    assert any(e["kind"] == "tool_result" and e["text"] == "echo: on" for e in events)

    (source,) = put_source(server, cid, "calliope", None).json()["sources"]
    assert source["enabled"] is False and source["choice"] is None
    # re-review C1: a choice equal to the default is not kept, so the
    # conversation keeps following the default if the operator changes it
    (source,) = put_source(server, cid, "calliope", False).json()["sources"]
    assert source["enabled"] is False and source["choice"] is None
    application = server.application
    assert application is not None and server.client.portal is not None
    assert server.client.portal.call(application.data.read_external_settings,
                                     TrustedScope(OPERATOR, ConversationId(cid))) == ()


def test_tool_source_changes_are_checked(mcp_server: tuple[Server, FakeMcpServer]) -> None:
    server, _ = mcp_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    unknown = put_source(server, cid, "nope", True)
    assert unknown.status_code == 404 and unknown.json()["error"]["message"] == "No such tool source."
    assert put_source(server, cid, "calliope", "yes").status_code == 400
    assert server.client.put(f"/api/conversations/{cid}/tools/calliope", json={"enabled": True}).status_code == 403
    application = server.application
    assert application is not None and server.client.portal is not None
    foreign = server.client.portal.call(application.create_conversation, PrincipalId("someone-else"))
    for response in (server.client.get(f"/api/conversations/{foreign}/tools"), put_source(server, foreign, "calliope", True),
                     put_source(server, foreign, "nope", True)):
        assert response.status_code == 404 and response.json()["error"]["message"] == "No such conversation."


def test_without_mcp_json_there_are_no_tool_sources(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.client.get(f"/api/conversations/{cid}/tools").json() == {"sources": []}
    assert put_source(server, cid, "calliope", True).status_code == 404


class RefusingDriver(GatedDriver):
    """Ends every job with a refusal that says why."""

    def __init__(self, detail: str) -> None:
        super().__init__()
        self.detail = detail

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        return DriveResult(DriveState.REQUEST_REJECTED, detail=self.detail)


def test_a_finished_job_says_why_as_bounded_text(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.application is not None
    server.application.runner = BackgroundRunner(RefusingDriver("context_budget_exceeded: about 950 tokens > 900"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "one"})
    job = server.wait_idle(cid)["job"]
    assert job["state"] == "request_rejected" and job["detail"] == "context_budget_exceeded: about 950 tokens > 900"
    server.application.runner = BackgroundRunner(RefusingDriver("<b>" + "x" * 900))
    server.post(f"/api/conversations/{cid}/resume")
    detail = server.wait_idle(cid)["job"]["detail"]
    assert len(detail) == web.MAX_DETAIL_CHARS and detail.endswith("…")  # bounded; the page renders it as text


# --- Tool views (MCP Apps, V3-P58) --------------------------------------------------------------

VIEW_UI = {"connect_domains": ["https://api.example.com"]}


@pytest.fixture
def view_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Server, FakeMcpServer, SandboxSigner]]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "mcp.json").write_text(json.dumps({"servers": {"calliope": {
        "url": MCP_URL, "tools": {"echo": {"effect": "read"}}, "ui": VIEW_UI}}}))
    (data / "mcp.json").chmod(0o600)
    fake = FakeMcpServer([{"name": "echo", "description": "Echo with a view.", "inputSchema": ECHO_SCHEMA,
                           "_meta": {"ui": {"resourceUri": VIEW_URI}}}])
    fake.resources = {VIEW_URI: {"contents": [view_entry(_meta={"ui": {
        "csp": {"connectDomains": ["https://api.example.com", "https://not-allowed.example.com"]},
        "prefersBorder": False}})]}}
    signer = SandboxSigner(b"k" * 32)
    s = Server(tmp_path, [], mcp=fake, signer=signer)
    with s.client:
        yield s, fake, signer


def app_url(cid: str, run_id: str, call_id: str, origin: str = ORIGIN) -> str:
    return f"/api/conversations/{cid}/calls/{run_id}/{call_id}/app?origin={origin}"


def test_views_have_their_own_port_and_origin() -> None:
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    default = WebConfig.from_env(base)
    assert default.view_port == 8766
    assert default.sandbox_origins == {"http://127.0.0.1:8766", "http://localhost:8766"}
    proxied = WebConfig.from_env({**base, "HIMARI_WEB_ALLOWED_ORIGINS": "https://h.example",
                                  "HIMARI_WEB_SANDBOX_PORT": "9443"})
    assert proxied.sandbox_origin("https://h.example") == "https://h.example:9443"
    for bad in ("8765", "0", "x"):
        with pytest.raises(WebConfigError, match="HIMARI_WEB_SANDBOX_PORT"):
            WebConfig.from_env({**base, "HIMARI_WEB_SANDBOX_PORT": bad})
    with pytest.raises(WebConfigError, match="HIMARI_WEB_SANDBOX_PORT"):
        WebConfig.from_env({**base, "HIMARI_WEB_PORT": "65535"})  # no port + 1


def test_the_page_may_frame_only_the_sandbox_origin(server: Server) -> None:
    csp = server.client.get("/").headers["content-security-policy"]
    assert csp.endswith("; frame-src http://testserver:8766")
    assert "frame-ancestors 'none'" in csp  # the page itself is still never framed


def test_a_call_with_a_view_is_flagged_and_its_view_served_as_json(
        view_server: tuple[Server, FakeMcpServer, SandboxSigner]) -> None:
    server, _, signer = view_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, calls(("calliope/echo", {"text": "hi"})), final("done"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "go"})
    server.wait_idle(cid)
    events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    (call_event,) = [e for e in events if e["kind"] == "assistant_tool_calls"]
    (result_event,) = [e for e in events if e["kind"] == "tool_result"]
    assert call_event["calls"][0]["app_view"] is True and result_event["app_view"] is True
    view = server.client.get(app_url(cid, call_event["run_id"], call_event["calls"][0]["call_id"])).json()
    assert view["state"] == "done" and view["status"] == "succeeded" and view["source"] == "calliope"
    assert view["html"] == VIEW_HTML and view["prefers_border"] is False
    assert view["tool"] == {"name": "echo", "description": "Echo with a view.", "inputSchema": ECHO_SCHEMA}
    assert view["arguments"] == {"text": "hi"}
    assert view["result"] == {"content": [{"type": "text", "text": "echo: hi"}]}
    assert view["csp"] == {"connect_domains": ["https://api.example.com"], "resource_domains": []}
    assert view["declared_csp"]["connect_domains"] == ["https://api.example.com", "https://not-allowed.example.com"]
    # The signed URL is the sandbox origin's, framed by this page only, with the allowed CSP.
    prefix = "http://testserver:8766/sandbox?"
    assert view["sandbox_url"].startswith(prefix)
    query = dict(part.split("=", 1) for part in view["sandbox_url"][len(prefix):].split("&"))
    assert signer.verify(query["p"], query["sig"]) == SandboxGrant(ORIGIN, ("https://api.example.com",), ())


def test_a_view_needs_this_pages_origin_and_this_conversation(
        view_server: tuple[Server, FakeMcpServer, SandboxSigner]) -> None:
    server, _, _ = view_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, calls(("calliope/echo", {"text": "hi"})), final("done"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "go"})
    server.wait_idle(cid)
    call_event = next(e for e in server.client.get(f"/api/conversations/{cid}/events").json()["items"]
                      if e["kind"] == "assistant_tool_calls")
    run_id, call_id = call_event["run_id"], call_event["calls"][0]["call_id"]
    assert server.client.get(app_url(cid, run_id, call_id, "http://evil.example")).status_code == 400
    assert server.client.get(f"/api/conversations/{cid}/calls/{run_id}/{call_id}/app").status_code == 400
    other = server.post("/api/conversations").json()["id"]
    assert server.client.get(app_url(other, run_id, call_id)).json()["error"]["code"] == "no_app_view"
    assert server.client.get(app_url("conv_nope", run_id, call_id)).status_code == 404
    signed_out = TestClient(server.client.app)
    assert signed_out.get(app_url(cid, run_id, call_id)).status_code == 401


def test_a_source_turned_off_hides_and_refuses_its_views(
        view_server: tuple[Server, FakeMcpServer, SandboxSigner]) -> None:
    server, _, _ = view_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, calls(("calliope/echo", {"text": "hi"})), final("done"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "go"})
    server.wait_idle(cid)
    put_source(server, cid, "calliope", False)
    events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    call_event = next(e for e in events if e["kind"] == "assistant_tool_calls")
    assert call_event["calls"][0]["app_view"] is False
    assert next(e for e in events if e["kind"] == "tool_result")["app_view"] is False
    answer = server.client.get(app_url(cid, call_event["run_id"], call_event["calls"][0]["call_id"]))
    assert (answer.status_code, answer.json()["error"]["code"]) == (404, "source_disabled")


def test_a_server_without_views_adds_no_flags(mcp_server: tuple[Server, FakeMcpServer]) -> None:
    server, _ = mcp_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    put_source(server, cid, "calliope", True)
    queue(server, calls(("calliope/echo", {"text": "hi"})), final("done"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "go"})
    server.wait_idle(cid)
    events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    call_event = next(e for e in events if e["kind"] == "assistant_tool_calls")
    assert call_event["calls"][0]["app_view"] is False
    answer = server.client.get(app_url(cid, call_event["run_id"], call_event["calls"][0]["call_id"]))
    assert answer.json()["error"]["code"] == "app_view_disabled"


# --- the sandbox origin ---------------------------------------------------------------------------

GRANT = SandboxGrant(ORIGIN, ("https://api.example.com", "wss://live.example.com"), ("https://cdn.example.com",))


def sandbox(signer: SandboxSigner) -> TestClient:
    return TestClient(create_sandbox_app(["testserver"], signer))


def test_the_proxy_is_served_with_the_signed_csp() -> None:
    signer = SandboxSigner(b"k" * 32)
    page = sandbox(signer).get(f"/sandbox?{signer.query(GRANT)}")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert csp == view_csp(GRANT)
    assert "connect-src 'self' https://api.example.com wss://live.example.com;" in csp
    assert "script-src 'self' 'unsafe-inline' https://cdn.example.com;" in csp
    assert "frame-src 'none'" in csp and "object-src 'none'" in csp and csp.endswith(f"frame-ancestors {ORIGIN}")
    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
    assert f'<meta name="himari-host-origin" content="{ORIGIN}">' in page.text
    assert "x-frame-options" not in page.headers  # it must be framed (by the page only)


def test_the_proxy_refuses_what_himari_did_not_sign_now() -> None:
    now = [1_000_000.0]
    signer = SandboxSigner(b"k" * 32, clock=lambda: now[0])
    client = sandbox(signer)
    query = signer.query(GRANT)
    p, sig = (part.split("=", 1)[1] for part in query.split("&"))
    assert client.get(f"/sandbox?{query}").status_code == 200
    assert client.get(f"/sandbox?p={p}&sig={'0' * 64}").status_code == 400
    assert client.get(f"/sandbox?p={p[:-2]}xx&sig={sig}").status_code == 400  # tampered payload
    assert client.get("/sandbox").status_code == 400
    restarted = SandboxSigner(b"j" * 32)  # a new process: a new key
    assert sandbox(restarted).get(f"/sandbox?{query}").status_code == 400
    now[0] += 601  # past its 10 minutes
    assert client.get(f"/sandbox?{query}").status_code == 400


def test_a_grant_with_anything_but_origins_is_refused() -> None:
    signer = SandboxSigner(b"k" * 32)
    for grant in [SandboxGrant(ORIGIN, ("https://a.com; script-src *",)), SandboxGrant("javascript:x"),
                  SandboxGrant(ORIGIN, (), ("'unsafe-eval'",))]:
        p, sig = (part.split("=", 1)[1] for part in signer.query(grant).split("&"))
        assert signer.verify(p, sig) is None


def test_the_sandbox_serves_only_the_proxy() -> None:
    client = sandbox(SandboxSigner())
    script = client.get("/sandbox.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    for path in ("/", "/api/session", "/assets/app.js", "/sandbox.html"):
        assert client.get(path).status_code == 404
    assert client.get("/sandbox.js", headers={"host": "evil.example"}).status_code == 400


# --- one turn's guards (V3-P62) ---------------------------------------------------------------


def test_a_job_reports_what_its_turn_used(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations", {}).json()["id"]
    queue(server, calls(("inside/write_file", {"path": "a.txt", "content": "x"})), final("done"))
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "hi"}).status_code == 202
    usage = server.wait_idle(cid)["job"]["turn_usage"]
    assert usage["runs"] == 2 and usage["budget"] == 3_000_000 and usage["max_runs"] is None
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0


def test_a_turn_stopped_by_its_budget_continues_with_a_new_one(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    # The scripted model reports no usage: each Run is charged its estimate and the 8,192 reserve.
    s = Server(tmp_path, [], turn={"turn_budget": 24_000})
    with s.client:
        s.sign_in()
        cid = s.post("/api/conversations", {}).json()["id"]
        queue(s, *(calls(("inside/write_file", {"path": f"{n}.txt", "content": "x"})) for n in range(3)),
              final("done"))
        assert s.post(f"/api/conversations/{cid}/messages", {"text": "hi"}).status_code == 202
        detail = s.wait_idle(cid)
        assert detail["job"]["state"] == "budget_reached" and detail["continuable"]
        first = detail["job"]["turn_usage"]
        assert first["budget"] == 24_000 and first["runs"] == 2
        assert s.post(f"/api/conversations/{cid}/resume").status_code == 202  # a new budget: two more Runs
        detail = s.wait_idle(cid)
        assert detail["job"]["state"] == "completed" and not detail["continuable"]
        assert detail["job"]["turn_usage"]["runs"] == 2 and len(s.provider.sent) == 4


def test_only_an_authorized_read_counts_as_a_client_reading(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    server.sign_in()
    cid = server.post("/api/conversations", {}).json()["id"]
    assert server.application is not None
    seen: list[str] = []
    monkeypatch.setattr(server.application.runner, "seen", lambda scope: seen.append(scope.conversation_id))
    assert server.client.get(f"/api/conversations/{cid}").status_code == 200
    assert server.client.get(f"/api/conversations/{cid}/events").status_code == 200
    assert seen == [cid, cid]
    assert server.client.get("/api/conversations/not-mine").status_code == 404
    assert server.client.get("/api/conversations/not-mine/events").status_code == 404
    assert seen == [cid, cid]


# --- the model (V3-P64 §7) ----------------------------------------------------------------------


class TwoModels(ScriptedAdapter):
    """"big" declares its window and efforts; "small" is unknown to the Provider."""

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        if model_id == "big":
            return ProviderCapabilities(model_id, frozenset({"low", "high", "max"}), True, context_tokens=300_000,
                                        max_output_tokens=60_000)
        return super().describe_capabilities(model_id)


@pytest.fixture
def model_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, [], provider=TwoModels([]), models=(("big", "low"), ("scripted-model", None)))
    with s.client:
        yield s


def put_model(server: Server, cid: str, body: dict[str, Any]) -> Any:
    return server.client.put(f"/api/conversations/{cid}/model", json=body, headers={"origin": ORIGIN})


def test_the_offered_models_and_a_conversations_choice(model_server: Server) -> None:
    server = model_server
    server.sign_in()
    assert server.client.get("/api/server").json()["models"] == [
        {"id": "big", "default_effort": "low", "efforts": ["low", "high", "max"], "window_known": True,
         "default": False, "images": False},
        {"id": "scripted-model", "default_effort": None, "efforts": [], "window_known": False, "default": True,
         "images": False},
    ]
    cid = server.post("/api/conversations").json()["id"]
    assert server.client.get(f"/api/conversations/{cid}/model").json() == {
        "model": "scripted-model", "effort": None, "chosen": None, "fallback": None}
    assert put_model(server, cid, {"model": "big", "effort": "high"}).json() == {
        "model": "big", "effort": "high", "chosen": {"model": "big", "effort": "high"}, "fallback": None}
    # the default model, chosen: kept as made (Q2)
    assert put_model(server, cid, {"model": "scripted-model"}).json()["chosen"] == {"model": "scripted-model",
                                                                                     "effort": None}
    assert put_model(server, cid, {"model": None}).json()["chosen"] is None


@pytest.mark.parametrize("body, status, code", [
    ({"model": "gpt-unknown"}, 404, "unknown_model"),
    ({"model": "big", "effort": "minimal"}, 422, "invalid_effort"),
    ({"model": "scripted-model", "effort": "low"}, 422, "invalid_effort"),
    ({"model": None, "effort": "low"}, 400, "invalid_argument"),
    ({"effort": "low"}, 400, "invalid_request"),  # "model" must be given (null: the default)
    ({"model": "big", "extra": 1}, 400, "invalid_request"),
    ({"model": "x" * 201}, 400, "invalid_request"),
])
def test_a_refused_model_choice_changes_nothing(model_server: Server, body: dict[str, Any], status: int,
                                                code: str) -> None:
    server = model_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    put_model(server, cid, {"model": "big"})
    response = put_model(server, cid, body)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert server.client.get(f"/api/conversations/{cid}/model").json()["chosen"] == {"model": "big", "effort": None}


def test_answers_carry_their_model_and_a_change_applies_from_the_next_turn(model_server: Server) -> None:
    server = model_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    queue(server, final("one"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "first"})
    server.wait_idle(cid)
    put_model(server, cid, {"model": "big", "effort": "max"})
    queue(server, calls(("ask_user", {"question": "sure?"})))
    server.post(f"/api/conversations/{cid}/messages", {"text": "second"})
    server.wait_idle(cid)
    sent = server.provider.prepared_inputs
    assert [(i.request_options.model_id, i.request_options.reasoning_effort) for i in sent] == [
        ("scripted-model", None), ("big", "max")]
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    assert [(e["kind"], e.get("model")) for e in items if e["kind"].startswith("assistant")] == [
        ("assistant_final", "scripted-model"), ("assistant_tool_calls", "big")]
    assert all("model" not in e for e in items if not e["kind"].startswith("assistant"))
    context = server.client.get(f"/api/conversations/{cid}/context").json()
    assert context["hard_limit_tokens"] == 240_000  # the chosen model's window less its output


# --- the trash (V3-P65 §9) ---------------------------------------------------------------------


def test_trash_restore_and_delete_for_good(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations", {"title": "Trip"}).json()["id"]
    queue(server, calls(("inside/write_file", {"path": "plan.txt", "content": "draft"})), final("written"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "write it down"})
    server.wait_idle(cid)
    assert server.application is not None
    folder = Path(server.application.inside.host_root(workspace_for(OPERATOR))) / "conversations" / cid
    assert (folder / "plan.txt").read_text() == "draft"
    assert server.client.get(f"/api/conversations/{cid}/delete-notice").json() == {
        "servers": [], "unconfigured": [], "running": False, "trash_days": 30}  # inside/… is HIMARI's own

    for refused, code in [(server.client.delete(f"/api/conversations/{cid}", headers={"origin": ORIGIN}),
                           "not_in_trash"),
                          (server.post(f"/api/conversations/{cid}/restore"), "not_restorable")]:
        assert refused.status_code == 409 and refused.json()["error"]["code"] == code

    moved = server.post(f"/api/conversations/{cid}/trash")
    assert moved.status_code == 200, moved.text
    trashed_at = datetime.fromisoformat(moved.json()["trashed_at"])
    assert datetime.fromisoformat(moved.json()["purge_after"]) == trashed_at + timedelta(days=30)
    assert server.client.get("/api/conversations").json()["items"] == []
    (item,) = server.client.get("/api/conversations?trashed=1").json()["items"]
    assert (item["id"], item["title"], item["trashed_at"]) == (cid, "Trip", moved.json()["trashed_at"])
    gone = [server.client.get(f"/api/conversations/{cid}"), server.client.get(f"/api/conversations/{cid}/events"),
            server.post(f"/api/conversations/{cid}/messages", {"text": "hi"}),
            server.post(f"/api/conversations/{cid}/trash"),
            server.client.get(f"/api/conversations/{cid}/delete-notice")]
    assert {r.status_code for r in gone} == {404}  # a trashed conversation is a missing one

    assert server.post(f"/api/conversations/{cid}/restore").status_code == 204
    assert [e["kind"] for e in server.client.get(f"/api/conversations/{cid}/events").json()["items"]][-1] == \
        "assistant_final"
    queue(server, final("back"))
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "again"}).status_code == 202
    assert server.wait_idle(cid)["job"]["state"] == "completed"

    assert server.post(f"/api/conversations/{cid}/trash").status_code == 200
    assert server.client.delete(f"/api/conversations/{cid}", headers={"origin": ORIGIN}).status_code == 204
    assert not folder.exists()
    assert server.client.get("/api/conversations?trashed=1").json()["items"] == []
    after = [server.client.get(f"/api/conversations/{cid}"), server.post(f"/api/conversations/{cid}/restore"),
             server.client.delete(f"/api/conversations/{cid}", headers={"origin": ORIGIN})]
    assert {r.status_code for r in after} == {404}
    assert server.client.delete(f"/api/conversations/{cid}").status_code == 403  # the Origin check, like every mutation


def test_someone_elses_conversation_cannot_be_trashed_or_deleted(server: Server) -> None:
    server.sign_in()
    application = server.application
    assert application is not None and server.client.portal is not None
    foreign = server.client.portal.call(application.create_conversation, PrincipalId("someone-else"))
    responses = []
    for cid in (foreign, "conv_404"):
        responses += [server.client.get(f"/api/conversations/{cid}/delete-notice"),
                      server.post(f"/api/conversations/{cid}/trash"),
                      server.post(f"/api/conversations/{cid}/restore"),
                      server.client.delete(f"/api/conversations/{cid}", headers={"origin": ORIGIN})]
    assert {r.status_code for r in responses} == {404}
    assert {r.text for r in responses} == {'{"error":{"code":"not_found","message":"No such conversation."}}'}
    owner = TrustedScope(PrincipalId("someone-else"), foreign)
    assert server.client.portal.call(application.data.read_conversation, owner).trashed_at is None


def test_trashing_a_running_conversation_stops_its_job_first(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.application is not None
    gated = GatedDriver()
    server.application.runner = BackgroundRunner(gated)
    try:
        assert server.post(f"/api/conversations/{cid}/messages", {"text": "one"}).status_code == 202
        assert server.client.get(f"/api/conversations/{cid}/delete-notice").json()["running"] is True
        assert server.post(f"/api/conversations/{cid}/trash").status_code == 200
        assert server.application.runner.status(TrustedScope(OPERATOR, ConversationId(cid))).running is False
        assert server.post(f"/api/conversations/{cid}/restore").status_code == 204
        gated.release.set()
        assert server.post(f"/api/conversations/{cid}/resume").status_code == 202
        assert server.wait_idle(cid)["job"]["state"] == "completed"
    finally:
        gated.release.set()


class StubbornDriver(GatedDriver):
    """A job that ignores being stopped until released (Q-40: a stuck command)."""

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        while not self.release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass
        return DriveResult(DriveState.COMPLETED)


def test_a_job_that_does_not_stop_in_time_leaves_the_conversation_where_it_was(
        server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "TRASH_STOP_SECONDS", 0.1)
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.application is not None
    stubborn = StubbornDriver()
    server.application.runner = BackgroundRunner(stubborn)
    try:
        assert server.post(f"/api/conversations/{cid}/messages", {"text": "one"}).status_code == 202
        refused = server.post(f"/api/conversations/{cid}/trash")
        assert refused.status_code == 409 and refused.json() == {"error": {
            "code": "still_stopping", "message": "The current work is still stopping; try again shortly."}}
        assert [i["id"] for i in server.client.get("/api/conversations").json()["items"]] == [cid]
    finally:
        stubborn.release.set()
    assert server.wait_idle(cid)["job"]["state"] == "completed"
    assert server.post(f"/api/conversations/{cid}/trash").status_code == 200  # stopped now: it moves


def test_a_conversation_being_trashed_refuses_new_work(server: Server) -> None:
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    application = server.application
    assert application is not None and server.client.portal is not None
    scope = TrustedScope(OPERATOR, ConversationId(cid))
    server.client.portal.call(application.runner.close, scope, 1.0)  # what trash does first (§7.1)
    refused = server.post(f"/api/conversations/{cid}/messages", {"text": "late"})
    assert refused.status_code == 409 and refused.json() == {"error": {
        "code": "conversation_closed", "message": "This conversation is being deleted."}}
    application.runner.reopen(scope)


def test_the_delete_notice_names_the_external_servers_used(mcp_server: tuple[Server, FakeMcpServer]) -> None:
    server, fake = mcp_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    put_source(server, cid, "calliope", True)
    queue(server, calls(("calliope/echo", {"text": "hi"})), final("done"))
    server.post(f"/api/conversations/{cid}/messages", {"text": "echo"})
    server.wait_idle(cid)
    assert fake.calls
    notice = server.client.get(f"/api/conversations/{cid}/delete-notice").json()
    assert (notice["servers"], notice["unconfigured"]) == (["calliope"], [])
