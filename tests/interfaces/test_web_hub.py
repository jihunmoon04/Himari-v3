"""The hub's waiting requests over the Web API (V3-P95), against a stand-in hub server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from webkit import ORIGIN, SEND  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig, WebConfigError  # noqa: E402
from himari.interfaces.web.hub import request_view  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

TOKEN = "user-secret"
PERMISSION = {"id": 7, "task_id": 3, "kind": "permission", "asks": "Write a.txt",
              "options": [{"option_id": "allow-once", "name": "Yes", "kind": "allow_once"}],
              "task": {"project": "demo", "agent": "claude", "runner": "linux", "prompt": "write it"}}


class Hub:
    """``/user/<op>`` as the hub serves it: its user token only, a refusal as 400 with its reason."""

    def __init__(self) -> None:
        self.pending: list[Any] = [PERMISSION]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                op = self.path.removeprefix("/user/")
                status: int = 200
                reply: dict[str, Any] = {}
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    status, reply = 401, {"error": "token"}
                else:
                    hub.calls.append((op, body))
                    if op == "pending":
                        reply = {"pending": hub.pending}
                    elif op == "answer" and body["id"] != 7:
                        status, reply = 400, {"error": "not waiting: 9"}
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hub() -> Iterator[Hub]:
    started = Hub()
    yield started
    started.close()


def client_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **hub_settings: Any) -> TestClient:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    opened: list[Application] = []

    async def opener() -> Application:
        opened.append(await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="scripted-model",
                                             sandbox=False, cleanup_seconds=None))
        return opened[0]

    config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                       allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                       model_id="scripted-model", reasoning_effort=None, sandbox=False, **hub_settings)
    client = TestClient(web.create_app(config, opener))
    client.__enter__()
    assert client.post("/api/session", json={"token": opened[0].operator_secret}, headers=SEND).status_code == 204
    return client


@pytest.fixture
def client(tmp_path: Path, hub: Hub, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    token = tmp_path / "user.token"
    token.write_text(TOKEN + "\n", encoding="ascii")
    started = client_for(tmp_path, monkeypatch, hub_url=hub.url, hub_user_token_file=str(token))
    yield started
    started.__exit__(None, None, None)


def test_the_waiting_requests_are_listed_and_answered(client: TestClient, hub: Hub) -> None:
    listed = client.get("/api/hub/pending").json()
    assert listed == {"enabled": True, "reachable": True, "pending": [PERMISSION]}
    assert TOKEN not in json.dumps(listed)
    assert client.post("/api/hub/pending/7/answer", json={"option_id": "allow-once"}, headers=SEND).status_code == 204
    assert client.post("/api/hub/pending/7/answer", json={"refuse": True}, headers=SEND).status_code == 204
    assert client.post("/api/hub/pending/7/answer", json={"content": {"q1": "2"}}, headers=SEND).status_code == 204
    assert client.post("/api/hub/tasks/3/cancel", headers=SEND).status_code == 204
    assert hub.calls[1:] == [("answer", {"id": 7, "option_id": "allow-once"}), ("answer", {"id": 7, "refuse": True}),
                             ("answer", {"id": 7, "content": {"q1": "2"}}), ("cancel", {"id": 3})]


def test_an_answer_is_exactly_one_thing_and_a_refusal_is_told(client: TestClient, hub: Hub) -> None:
    for body in ({}, {"option_id": "allow-once", "refuse": True}, {"option_id": "x", "content": {}},
                 {"refuse": False}, {"option_id": ""}, {"other": 1}):
        refused = client.post("/api/hub/pending/7/answer", json=body, headers=SEND)
        assert refused.status_code == 400, body
    assert hub.calls == []  # nothing reached the hub
    gone = client.post("/api/hub/pending/9/answer", json={"refuse": True}, headers=SEND)
    assert gone.status_code == 409 and gone.json()["error"] == {"code": "hub_refused", "message": "not waiting: 9"}
    assert client.post("/api/hub/pending/0/answer", json={"refuse": True}, headers=SEND).status_code == 400


def test_a_hub_that_does_not_answer_is_said_so(client: TestClient, hub: Hub, tmp_path: Path) -> None:
    (tmp_path / "user.token").write_text("wrong", encoding="ascii")  # the hub answers 401
    assert client.get("/api/hub/pending").json() == {"enabled": True, "reachable": False, "pending": []}
    refused = client.post("/api/hub/pending/7/answer", json={"refuse": True}, headers=SEND)
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "hub_misconfigured"  # V3-P102 §6
    (tmp_path / "user.token").unlink()  # nothing is sent without the token
    calls = len(hub.calls)
    assert client.get("/api/hub/pending").json()["reachable"] is False and len(hub.calls) == calls
    hub.close()  # and a hub that is gone
    (tmp_path / "user.token").write_text(TOKEN, encoding="ascii")
    assert client.get("/api/hub/pending").json() == {"enabled": True, "reachable": False, "pending": []}


def test_without_a_hub_and_signed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(tmp_path, monkeypatch)
    try:
        assert client.get("/api/hub/pending").json() == {"enabled": False, "reachable": False, "pending": []}
        assert client.post("/api/hub/pending/7/answer", json={"refuse": True}, headers=SEND).status_code == 404
        assert client.post("/api/hub/tasks/3/cancel", headers=SEND).status_code == 404
        assert client.get("/assets/hub.js").status_code == 200
        client.delete("/api/session", headers=SEND)
        assert client.get("/api/hub/pending").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_only_what_the_page_can_show_is_passed_on() -> None:
    question = request_view({
        "id": 2, "task_id": 1, "kind": "question", "asks": "Which?", "secret": "x",
        "fields": {"q1": {"title": "Pick", "description": None, "multiple": False, "extra": 1,
                          "choices": [{"value": "1", "title": "One", "description": "first"}, {"value": {"a": 1}}]}}})
    assert question == {"id": 2, "task_id": 1, "kind": "question", "asks": "Which?", "fields": [
        {"name": "q1", "title": "Pick", "description": None, "multiple": False,
         "choices": [{"value": "1", "title": "One", "description": "first"}]}]}
    assert request_view({"id": "2", "task_id": 1, "kind": "question"}) is None
    assert request_view({"id": 2, "task_id": 1, "kind": "other"}) is None
    assert request_view("x") is None
    long = request_view({"id": 2, "task_id": 1, "kind": "permission", "asks": "a" * 9000, "options": [{"name": "n"}]})
    assert long is not None and len(long["asks"]) == 4000 and long["options"] == []


def test_the_hub_settings_come_together() -> None:
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).hub_url is None
    config = WebConfig.from_env({**base, "HIMARI_HUB_URL": " http://127.0.0.1:8791 ",
                                 "HIMARI_HUB_USER_TOKEN_FILE": "/etc/himari-v3/hub-user.token"})
    assert (config.hub_url, config.hub_user_token_file) == ("http://127.0.0.1:8791", "/etc/himari-v3/hub-user.token")
    for bad, message in (({"HIMARI_HUB_URL": "http://127.0.0.1:8791"}, "set together"),
                         ({"HIMARI_HUB_USER_TOKEN_FILE": "/t"}, "set together"),
                         ({"HIMARI_HUB_URL": "127.0.0.1:8791", "HIMARI_HUB_USER_TOKEN_FILE": "/t"}, "http"),
                         ({"HIMARI_HUB_URL": "http://h", "HIMARI_HUB_USER_TOKEN_FILE": "t"}, "absolute")):
        with pytest.raises(WebConfigError, match=message):
            WebConfig.from_env({**base, **bad})
    assert not hasattr(WebConfig.from_env({**base, "HIMARI_MACHINE_ATTACHED_WRITE": "1"}), "machine_attached_write")
    assert config.hub_mcp_source == "hub"  # V3-P102 §5.1: the mcp.json server that is the hub
    assert WebConfig.from_env({**base, "HIMARI_HUB_MCP_SOURCE": " hub-2 "}).hub_mcp_source == "hub-2"
    for name in ("hub/x", "Hub", "hub_2", "hub--2", "h" * 25):  # what mcp.json would refuse as a server name
        with pytest.raises(WebConfigError, match="mcp.json server name"):
            WebConfig.from_env({**base, "HIMARI_HUB_MCP_SOURCE": name})
