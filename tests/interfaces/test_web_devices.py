"""Devices over the Web API (V3-P83 §4): pairing, the device token's narrow
permission, and where messages come from."""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, MOODS, CallId, ConversationId, ToolCall, TrustedScope  # noqa: E402
from himari.data.api import MessageOrigin, OriginKind, UserMessage  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.presence import devices as presence_devices  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}

# §4.3: what a device token may call; everything else is refused by default.
ALLOWED = {
    ("GET", "/api/device"),
    ("GET", "/api/conversations"),
    ("GET", "/api/conversations/activity"),
    ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}"),
    ("GET", "/api/conversations/{conversation_id}/events"),
    ("GET", "/api/conversations/{conversation_id}/live"),
    ("POST", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/conversations/{conversation_id}/pause"),
    ("POST", "/api/conversations/{conversation_id}/resume"),
    ("POST", "/api/conversations/{conversation_id}/cancel"),
    ("POST", "/api/conversations/{conversation_id}/dismiss"),
}
# Routes without any sign-in: the session itself, pairing, and the page's files.
PUBLIC = {("GET", "/api/session"), ("POST", "/api/session"), ("DELETE", "/api/session"),
          ("POST", "/api/devices/pair"), ("GET", "/assets/{name:path}"), ("GET", "/"),
          ("GET", "/c/{conversation_id}"), *(("GET", p) for p in web.SURFACE_PATHS)}


def new_token() -> str:
    return "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


class Bare:
    """The same client without the Web session's cookie (one event loop: SQLite stays on its thread)."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        saved = dict(self._client.cookies)
        self._client.cookies.clear()
        try:
            return self._client.request(method, url, **kwargs)
        finally:
            for name, value in saved.items():
                self._client.cookies.set(name, value)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


class Server:
    def __init__(self, tmp_path: Path, companion: bool = True) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None, companion=companion)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                           data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                           sandbox=False)
        self.client = TestClient(web.create_app(config, opener))
        self.device = Bare(self.client)  # no cookies: what a native app sends

    @property
    def app(self) -> Application:
        assert self.application is not None
        return self.application

    def sign_in(self) -> None:
        response = self.client.post("/api/session", json={"token": self.app.operator_secret}, headers=SEND)
        assert response.status_code == 204, response.text

    def add_device(self, name: str = "데스크톱") -> Any:
        response = self.client.post("/api/devices", json={"name": name}, headers=SEND)
        assert response.status_code == 201, response.text
        return response.json()

    def pair(self, code: str, token: str) -> Any:
        return self.device.post("/api/devices/pair", json={"token": token}, headers={"authorization": f"Pairing {code}"})

    def paired(self, name: str = "데스크톱") -> str:
        token = new_token()
        response = self.pair(self.add_device(name)["code"], token)
        assert response.status_code == 200, response.text
        return token

    def as_device(self, token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {token}"}

    def wait(self, cid: str) -> None:
        for _ in range(200):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("still running")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(presence_devices, "FAILED_PAIRING_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        s.app.devices._failure_delay = 0  # type: ignore[attr-defined]
        s.sign_in()
        yield s


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]  # type: ignore[no-any-return]


def test_a_device_pairs_without_an_origin_and_talks_with_its_token(server: Server) -> None:
    made = server.add_device()
    assert made["code"][4] == "-" and made["paired"] is False and made["pairing_expires_at"]
    token = new_token()
    response = server.pair(made["code"], token)  # no Origin, no cookie: OriginGuard lets it through (B2)
    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": "d1", "name": "데스크톱"}
    assert server.pair(made["code"], token).json() == {"device_id": "d1", "name": "데스크톱"}  # a lost answer

    me = server.as_device(token)
    started = server.device.get("/api/device", headers=me).json()
    assert started["device"] == {"device_id": "d1", "name": "데스크톱"} and started["moods"] == list(MOODS)
    assert started["recent_conversation"] is None and started["limits"]["message_chars"] == 32_000

    cid = server.device.post("/api/conversations", json={}, headers=me).json()["id"]
    server.provider.extend([final("안녕!")])
    assert server.device.post(f"/api/conversations/{cid}/messages", json={"text": "hi"}, headers=me).status_code == 202
    server.wait(cid)
    items = server.device.get(f"/api/conversations/{cid}/events", headers=me).json()["items"]
    assert [i["text"] for i in items] == ["hi", "안녕!"]
    assert server.device.get("/api/device", headers=me).json()["recent_conversation"]["id"] == cid
    listed = server.client.get("/api/devices").json()["items"]
    assert [(d["device_id"], d["paired"]) for d in listed] == [("d1", True)] and listed[0]["last_seen_at"]


def test_each_message_keeps_where_it_came_from(server: Server) -> None:
    token = server.paired()
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]
    server.provider.extend([
        NormalizedToolCalls((ToolCall(CallId("q1"), 0, ASK_USER_TOOL_ID, {"question": "어느 쪽?"}),)),
        final("알았어"), final("web"), final("cli"),
    ])
    server.device.post(f"/api/conversations/{cid}/messages", json={"text": "골라줘"}, headers=server.as_device(token))
    server.wait(cid)
    server.device.post(f"/api/conversations/{cid}/messages", json={"text": "왼쪽"},  # the answer (T7a)
                       headers=server.as_device(token))
    server.wait(cid)
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "from web"}, headers=SEND)
    server.wait(cid)
    bearer = {"authorization": f"Bearer {derive_cli_bearer(server.app.operator_secret)}"}
    assert server.device.post(f"/api/conversations/{cid}/messages", json={"text": "from cli"}, headers=bearer).status_code == 202
    server.wait(cid)

    async def origins() -> list[MessageOrigin | None]:
        events = await server.app.data.read_events(TrustedScope(OPERATOR, ConversationId(cid)))
        return [e.payload.origin for e in events if isinstance(e.payload, UserMessage)]

    found = server.client.portal.call(origins)  # type: ignore[union-attr]
    device = MessageOrigin(OriginKind.DEVICE, "d1")
    assert found == [device, device, MessageOrigin(OriginKind.WEB), MessageOrigin(OriginKind.CLI)]


def test_a_device_sends_text_only_and_sees_no_trash(server: Server) -> None:
    me = server.as_device(server.paired())
    cid = server.device.post("/api/conversations", json={}, headers=me).json()["id"]
    for body in ({"text": "x", "references": [{"path": "/etc/hostname", "identity": [1, 2, 3, 4], "start": 1, "end": 1,
                                                "sha256": "0" * 64}]},
                 {"text": "x", "attachments": ["f_1"]}):
        response = server.device.post(f"/api/conversations/{cid}/messages", json=body, headers=me)
        assert (response.status_code, error_code(response)) == (403, "device_scope"), body  # B3
    response = server.device.get("/api/conversations?trashed=true", headers=me)
    assert (response.status_code, error_code(response)) == (403, "device_scope")
    assert server.device.get("/api/conversations", headers=me).status_code == 200


def test_every_route_but_the_allowed_ones_refuses_a_device(server: Server) -> None:
    me = server.as_device(server.paired())
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]
    routes = {(method, route.path) for route in server.client.app.routes  # type: ignore[attr-defined]
              if isinstance(route, APIRoute) for method in route.methods or ()}
    assert ALLOWED <= routes and PUBLIC <= routes
    for method, path in sorted(routes - ALLOWED - PUBLIC):
        url = path.replace("{conversation_id}", cid)
        for name in ("{device_id}", "{run_id}", "{call_id}", "{file_id}", "{version}", "{binding_id}",
                     "{workspace_id}", "{version_id}", "{name}"):
            url = url.replace(name, "x1")
        response = server.device.request(method, url, headers=me, json={})
        assert (response.status_code, error_code(response)) == (403, "device_scope"), (method, path)


def test_pairing_refusals_and_revocation(server: Server) -> None:
    made = server.add_device()
    response = server.device.post("/api/devices/pair", json={"token": new_token()})  # no code: like any POST
    assert (response.status_code, error_code(response)) == (403, "origin_forbidden")
    for header in ({"authorization": "Bearer nothing"}, {"authorization": "Pairing ZZZZ-ZZZZ"}):
        response = server.device.post("/api/devices/pair", json={"token": new_token()}, headers=header)
        assert (response.status_code, error_code(response)) == (401, "pairing_invalid"), header
    response = server.pair(made["code"], "hd_" + "A" * 43)
    assert (response.status_code, error_code(response)) == (400, "invalid_token")
    pairing = {"authorization": f"Pairing {made['code']}"}
    bodies: list[Any] = [{}, {"token": 1}, {"token": "hd_" + "x" * 500}, {"token": new_token(), "name": "x"}, []]
    for body in bodies:
        response = server.device.post("/api/devices/pair", json=body, headers=pairing)  # review C3: one contract
        assert (response.status_code, error_code(response)) == (400, "invalid_token"), body
    response = server.device.post("/api/devices/pair", content=b"not json", headers=pairing)
    assert (response.status_code, error_code(response)) == (400, "invalid_token")
    token = new_token()
    assert server.pair(made["code"], token).status_code == 200
    response = server.pair(made["code"], new_token())
    assert (response.status_code, error_code(response)) == (409, "pairing_used")

    assert server.client.delete("/api/devices/d1", headers=SEND).status_code == 204
    response = server.device.get("/api/device", headers=server.as_device(token))
    assert (response.status_code, error_code(response)) == (401, "unauthenticated")
    response = server.pair(made["code"], new_token())  # within its ten minutes: not again (review r2 B1)
    assert (response.status_code, error_code(response)) == (401, "pairing_invalid")
    response = server.client.delete("/api/devices/d1", headers=SEND)
    assert (response.status_code, error_code(response)) == (404, "not_found")


def test_another_devices_token_is_refused_as_in_use(server: Server) -> None:
    token = server.paired("Desk")
    second = server.add_device("Laptop")
    response = server.pair(second["code"], token)  # review C1
    assert (response.status_code, error_code(response)) == (409, "token_in_use")
    assert server.pair(second["code"], new_token()).status_code == 200


def test_device_names_are_checked(server: Server) -> None:
    server.paired("Desk")
    for name, expected in (("desk", (409, "duplicate_name")), ("web", (422, "invalid_name")),
                           ("", (422, "invalid_name")), ("x" * 41, (422, "invalid_name"))):
        response = server.client.post("/api/devices", json={"name": name}, headers=SEND)
        assert (response.status_code, error_code(response)) == expected, name
    renamed = server.client.patch("/api/devices/d1", json={"name": "Big desk"}, headers=SEND)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Big desk"


def test_with_companion_off_pairing_and_device_tokens_are_refused(tmp_path: Path) -> None:
    on = Server(tmp_path)
    with on.client:
        on.sign_in()
        token = on.paired()
    off = Server(tmp_path, companion=False)
    with off.client:
        off.sign_in()
        response = off.device.get("/api/device", headers=off.as_device(token))
        assert (response.status_code, error_code(response)) == (403, "companion_off")
        response = off.pair("ABCD-EFGH", new_token())
        assert (response.status_code, error_code(response)) == (403, "companion_off")
        response = off.client.post("/api/devices", json={"name": "Laptop"}, headers=SEND)
        assert (response.status_code, error_code(response)) == (403, "companion_off")
        listed = off.client.get("/api/devices").json()
        assert listed["enabled"] is False and [d["device_id"] for d in listed["items"]] == ["d1"]  # the tokens stay
