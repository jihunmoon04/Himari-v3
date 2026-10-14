"""Web server (V3-P46): security envelope, sign-in, and the conversation API
over a real Application (SQLite, sandbox off, scripted provider)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, PrincipalId, TextBlock, TrustedScope  # noqa: E402
from himari.control.api import DriveResult, DriveState  # noqa: E402
from himari.control.runner import BackgroundRunner  # noqa: E402
from himari.data.api import UserMessage  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig, WebConfigError  # noqa: E402
from himari.provider.scripted import ScriptedAdapter, ScriptStep  # noqa: E402

ORIGIN = "http://testserver"


def config(tmp_path: Path) -> WebConfig:
    return WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                     allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                     model_id="scripted-model", reasoning_effort=None, sandbox=False)


class Server:
    def __init__(self, tmp_path: Path, script: list[ScriptStep]) -> None:
        self.provider = ScriptedAdapter(script)
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False)
            return self.application

        self.client = TestClient(web.create_app(config(tmp_path), opener))

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


# --- envelope ------------------------------------------------------------------------------


def test_static_ui_and_security_headers(server: Server) -> None:
    page = server.client.get("/")
    assert page.status_code == 200 and "/assets/app.js" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["x-frame-options"] == "DENY"
    for name in ("app.js", "app.css", "rich_text.js"):
        assert server.client.get(f"/assets/{name}").status_code == 200
    assert server.client.get("/assets/../app.py").status_code == 404
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
                      server.client.get(f"/api/conversations/{cid}/events")]
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

    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        raise AssertionError("the runner accepts, then resumes")

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        pass

    async def resume(self, scope: TrustedScope) -> DriveResult:
        await asyncio.to_thread(self.release.wait, 10)
        return DriveResult(DriveState.COMPLETED)


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
