"""Devices over the Web API (V3-P83 §4): pairing, the device token's narrow
permission, and where messages come from."""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, MOODS, CallId, ConversationId, TextBlock, ToolCall, TrustedScope  # noqa: E402
from himari.data.api import MessageOrigin, OriginKind, UserMessage  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web import event_stream  # noqa: E402
from himari.interfaces.web.config import WebConfig, WebConfigError  # noqa: E402
from himari.presence import devices as presence_devices  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}

# §4.3: what a device token may call; everything else is refused by default.
ALLOWED = {
    ("GET", "/api/device"),
    ("POST", "/api/device/presence"),
    ("PUT", "/api/presence/quiet"),  # V3-P88 §6.2
    ("GET", "/api/events"),
    ("GET", "/api/conversations"),
    ("GET", "/api/conversations/activity"),
    ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}"),
    ("GET", "/api/conversations/{conversation_id}/events"),
    ("GET", "/api/conversations/{conversation_id}/live"),
    ("POST", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/conversations/{conversation_id}/uploads"),  # a screen only (V3-P84 §4.1)
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
    monkeypatch.setattr(event_stream, "MAX_SECONDS", 0.2)  # the test client reads a stream once it ends
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
    items = server.device.get(f"/api/conversations/{cid}/events", headers=server.as_device(token)).json()["items"]
    shown = [i["origin"] for i in items if i["kind"] == "user_message"]  # V3-P84 §7.8: an app finds its own
    assert shown == [{"kind": "device", "device_id": "d1"}] * 2 + [{"kind": "web"}, {"kind": "cli"}]


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
    for depth in (500, 20_000):  # review C4: the small cap answers first; under it, deep nesting is no 500
        nested = b"[" * depth + b"]" * depth
        response = server.device.post("/api/devices/pair", content=nested, headers=pairing)
        expected = (400, "invalid_token") if len(nested) <= web.PAIR_BODY_BYTES else (413, "request_too_large")
        assert (response.status_code, error_code(response)) == expected, depth
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


def test_a_pairing_body_too_deep_to_parse_is_no_token() -> None:
    """Review C4: RecursionError is not a ValueError; the parser's own failure reads as no token."""
    assert web.pairing_token(b"[" * 20_000 + b"]" * 20_000) is None
    assert web.pairing_token(b'{"token": "hd_x"}') == "hd_x"


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


def test_the_time_zone_is_an_iana_name() -> None:
    """V3-P83 §6.4: HIMARI_TIME_ZONE, Asia/Seoul when unset."""
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).time_zone == "Asia/Seoul"
    assert WebConfig.from_env({**base, "HIMARI_TIME_ZONE": " Europe/London "}).time_zone == "Europe/London"
    for bad in ("Mars/Olympus", "../etc", "KST+9"):
        with pytest.raises(WebConfigError):
            WebConfig.from_env({**base, "HIMARI_TIME_ZONE": bad})


def test_answers_and_questions_carry_their_display(server: Server) -> None:
    """V3-P83 §7.2, appendix A.6: ``text`` stays as the model wrote it."""
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]

    def events() -> list[Any]:
        return list(server.client.get(f"/api/conversations/{cid}/events").json()["items"])

    server.provider.extend([
        NormalizedToolCalls((ToolCall(CallId("q"), 0, ASK_USER_TOOL_ID, {"question": "[[mood:thinking]]어디?"}),),
                            commentary=(TextBlock("[[mood:happy]]좋아"),)),
        final("[[mood:excited]]서울! [[mood:nope]]끝"),
    ])
    assert server.client.post(f"/api/conversations/{cid}/messages", json={"text": "여행"}, headers=SEND).status_code == 202
    server.wait(cid)
    waiting = server.client.get(f"/api/conversations/{cid}").json()["waiting"]
    assert waiting["question"] == "[[mood:thinking]]어디?"
    assert waiting["display"] == {"text": "어디?", "moods": [{"at": 0, "mood": "thinking"}], "moves": []}
    asked = [e for e in events() if e["kind"] == "assistant_tool_calls"][0]
    assert asked["display"] == {"text": "좋아", "moods": [{"at": 0, "mood": "happy"}], "moves": []}
    assert asked["calls"][0]["display"]["text"] == "어디?"
    assert server.client.post(f"/api/conversations/{cid}/messages", json={"text": "서울"}, headers=SEND).status_code == 202
    server.wait(cid)
    answer = [e for e in events() if e["kind"] == "assistant_final"][-1]
    assert answer["text"] == "[[mood:excited]]서울! [[mood:nope]]끝"
    assert answer["display"] == {"text": "서울! 끝", "moods": [{"at": 0, "mood": "excited"}, {"at": 4, "mood": "neutral"}],
                                 "moves": []}


def frames_of(body: str) -> list[tuple[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        lines = block.strip().split("\n")
        if lines[0].startswith("event: "):
            out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def test_a_devices_signal_makes_it_current(server: Server) -> None:
    """§5, §6.1: with its event stream open and a signal, it is connected and current."""
    me = server.as_device(server.paired())
    server.client.portal.call(lambda: server.app.hub.subscribe(OPERATOR, "d1"))  # type: ignore[union-attr]
    assert server.device.get("/api/device", headers=me).json()["current_device"] is None
    response = server.device.post("/api/device/presence", json={"idle_seconds": 3, "time_zone": "Asia/Seoul"},
                                  headers=me)
    assert response.status_code == 204
    assert server.device.get("/api/device", headers=me).json()["current_device"] == "d1"
    (listed,) = server.client.get("/api/devices").json()["items"]
    assert listed["connected"] is True and listed["current"] is True
    for bad in ({"idle_seconds": -1}, {"idle_seconds": "x"}, {}, {"idle_seconds": 1, "time_zone": "x" * 65}):
        assert server.device.post("/api/device/presence", json=bad, headers=me).status_code == 400, bad
    response = server.client.post("/api/device/presence", json={"idle_seconds": 0}, headers=SEND)
    assert (response.status_code, error_code(response)) == (403, "not_a_device")


def test_the_event_stream_starts_with_hello(server: Server) -> None:
    me = server.as_device(server.paired())
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]
    server.provider.extend([final("hi")])
    server.device.post(f"/api/conversations/{cid}/messages", json={"text": "안녕"}, headers=me)
    server.wait(cid)
    response = server.device.get("/api/events", headers=me)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    name, hello = frames_of(response.text)[0]
    assert name == "hello" and hello == {
        "device": {"device_id": "d1", "name": "데스크톱"}, "moods": list(MOODS), "current_device": None,
        "recent_conversation": {"id": cid, "title": None}, "features": {"presence_signals": 1},
        "quiet_until": None, "activity": {"working": [], "waiting": []}}  # a device gets no signals revision
    web_hello = frames_of(server.client.get("/api/events").text)[0][1]
    assert web_hello["device"] is None and web_hello["recent_conversation"]["id"] == cid
    assert web_hello["signals"] == {"epoch": server.app.hub.signals_epoch, "revision": 0}
    assert server.client.portal.call(server.app.hub.open_streams, OPERATOR) == 0  # type: ignore[union-attr]


def test_a_principal_has_at_most_sixteen_event_streams(server: Server) -> None:
    def fill() -> None:
        for _ in range(16):
            server.app.hub.subscribe(OPERATOR)

    server.client.portal.call(fill)  # type: ignore[union-attr]
    response = server.client.get("/api/events")
    assert (response.status_code, error_code(response)) == (429, "too_many_streams")


def test_revoking_ends_the_devices_event_stream(server: Server) -> None:
    server.paired()
    stream = server.client.portal.call(lambda: server.app.hub.subscribe(OPERATOR, "d1"))  # type: ignore[union-attr]
    assert server.client.delete("/api/devices/d1", headers=SEND).status_code == 204

    async def drain() -> list[str]:
        out = []
        while (event := await stream.next(0)) is not None:
            out.append(event.name)
        return out

    assert server.client.portal.call(drain)[-1] == "revoked" and stream.finished  # type: ignore[union-attr]


def test_with_companion_off_the_web_still_has_its_event_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_stream, "MAX_SECONDS", 0.2)
    off = Server(tmp_path, companion=False)
    with off.client:
        off.sign_in()
        assert frames_of(off.client.get("/api/events").text)[0][0] == "hello"


# --- screens (V3-P84 §4) ---------------------------------------------------------------------

def png(width: int = 4, height: int = 3) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def test_a_device_sends_its_screen_once(server: Server) -> None:
    server.provider._script.extend([final("[[move:3,2]]여기!"), final("응")])
    me = server.as_device(server.paired())
    cid = server.device.post("/api/conversations", json={}, headers=me).json()["id"]
    response = server.device.post(f"/api/conversations/{cid}/uploads?name=../../evil.png", content=png(),
                                  headers={**me, "content-type": "image/png"})
    assert response.status_code == 201, response.text
    uploaded = response.json()
    assert uploaded["name"].startswith("screen-") and uploaded["name"].endswith(".png")  # named here (§4.1)
    sent = server.device.post(f"/api/conversations/{cid}/messages", json={"text": "", "screen": uploaded["file_id"]},
                              headers=me)  # a screen alone
    assert sent.status_code == 202, sent.text
    job_id = sent.json()["job_id"]
    assert job_id
    server.wait(cid)
    items = server.device.get(f"/api/conversations/{cid}/events", headers=me).json()["items"]
    (said,) = [i for i in items if i["kind"] == "user_message"]
    assert [a["screen"] for a in said["attachments"]] == [True] and said["attachments"][0]["workspace_path"] is None
    answer = [i for i in items if i["kind"] == "assistant_final"][-1]
    assert answer["display"] == {"text": "여기!", "moods": [], "moves": [{"at": 0, "x": 3, "y": 2}]}
    again = server.device.post(f"/api/conversations/{cid}/messages", json={"text": "또", "screen": uploaded["file_id"]},
                               headers=me)
    assert (again.status_code, error_code(again)) == (409, "screen_already_sent")  # §7.8: the first was recorded
    assert server.client.get(f"/api/conversations/{cid}").json()["job"]["job_id"] == job_id  # no second job


def test_what_a_screen_may_be(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    me = server.as_device(server.paired())
    cid = server.device.post("/api/conversations", json={}, headers=me).json()["id"]
    response = server.device.post(f"/api/conversations/{cid}/uploads", content=b"not an image", headers=me)
    assert (response.status_code, error_code(response)) == (400, "not_a_screen")
    monkeypatch.setattr(web, "SCREEN_MAX_BYTES", 10)
    response = server.device.post(f"/api/conversations/{cid}/uploads", content=png(), headers=me)
    assert (response.status_code, error_code(response)) == (413, "file_too_large")
    web_upload = server.client.post(f"/api/conversations/{cid}/uploads?name=a.png", content=png(), headers=SEND)
    assert web_upload.status_code == 201, web_upload.text
    response = server.client.post(f"/api/conversations/{cid}/messages",
                                  json={"text": "x", "screen": web_upload.json()["file_id"]}, headers=SEND)
    assert (response.status_code, error_code(response)) == (400, "invalid_argument")  # a device's only
    response = server.device.post(f"/api/conversations/{cid}/messages",
                                  json={"text": "x", "screen": web_upload.json()["file_id"],
                                        "attachments": [web_upload.json()["file_id"]]}, headers=me)
    assert (response.status_code, error_code(response)) == (403, "device_scope")  # still one screen only


# --- V3-P88: activity signals --------------------------------------------------------------------

SIGNAL = {"idle_seconds": 2, "time_zone": "Asia/Seoul", "session": "active",
          "activity": {"category": "video", "app": "Chrome", "for_seconds": 2700, "fullscreen": True},
          "media": {"title": "밤양갱", "artist": "비비", "app": "Spotify"}, "mic_or_camera": False}


def test_signals_are_kept_whole_and_shown_to_the_web_only(server: Server) -> None:
    """V3-P88 §2.1, §8.1: the body replaces the device's snapshot; GET /api/devices shows it, to the Web only."""
    me = server.as_device(server.paired())
    server.client.portal.call(lambda: server.app.hub.subscribe(OPERATOR, "d1"))  # type: ignore[union-attr]
    assert server.device.post("/api/device/presence", json=SIGNAL, headers=me).status_code == 204
    listing = server.client.get("/api/devices").json()
    (item,) = listing["items"]
    assert item["signals"]["session"] == "active" and item["signals"]["media"]["title"] == "밤양갱"
    assert item["signals"]["activity"]["category"] == "video" and item["signals"]["activity"]["fullscreen"] is True
    assert item["signals"]["mic_or_camera"] is False
    assert listing["signals_epoch"] == server.app.hub.signals_epoch and listing["signals_revision"] >= 1
    # media turned off in the app: absent, so the song is gone; a game that closed is the one before
    body = {k: v for k, v in SIGNAL.items() if k != "media"} | {"activity": None}
    assert server.device.post("/api/device/presence", json=body, headers=me).status_code == 204
    (item,) = server.client.get("/api/devices").json()["items"]
    assert "media" not in item["signals"] and "activity" not in item["signals"]
    assert item["signals"]["before"]["category"] == "video"
    response = server.device.get("/api/devices", headers=me)
    assert response.status_code == 403  # a device never reads the others' signals


def test_signal_text_is_cleaned_and_unknown_categories_are_other(server: Server) -> None:
    me = server.as_device(server.paired())
    server.client.portal.call(lambda: server.app.hub.subscribe(OPERATOR, "d1"))  # type: ignore[union-attr]
    body = {"idle_seconds": 0, "activity": {"category": "knitting", "app": "A\u0000pp\n" + "x" * 100,
                                            "title": "t" * 300},
            "media": {"title": "​  "}}
    assert server.device.post("/api/device/presence", json=body, headers=me).status_code == 204
    (item,) = server.client.get("/api/devices").json()["items"]
    activity = item["signals"]["activity"]
    assert activity["category"] == "other" and activity["app"].startswith("A pp x") and len(activity["app"]) == 64
    assert len(activity["title"]) == 120
    assert "media" not in item["signals"]  # nothing left of the title: no media
    for bad in ({"idle_seconds": 0, "session": "asleep"}, {"idle_seconds": 0, "after": "lunch"},
                {"idle_seconds": 0, "activity": {"app": "x"}}, {"idle_seconds": 0, "mic_or_camera": "yes"},
                {"idle_seconds": 0, "activity": {"category": "coding", "for_seconds": -1}},
                {"idle_seconds": 0, "activity": {"category": "coding", "extra": 1}}):
        assert server.device.post("/api/device/presence", json=bad, headers=me).status_code == 400, bad


def test_an_unlock_mark_makes_the_device_current_past_the_switch_delay(server: Server) -> None:
    a = server.as_device(server.paired("데스크톱"))
    b = server.as_device(server.paired("노트북"))
    for device_id in ("d1", "d2"):
        server.app.hub.subscribe(OPERATOR, device_id)  # the hub is plain: no loop needed
    server.device.post("/api/device/presence", json={"idle_seconds": 0, "session": "locked"}, headers=a)
    server.device.post("/api/device/presence", json={"idle_seconds": 0, "session": "active"}, headers=b)
    assert server.client.get("/api/device").json()["current_device"] == "d2"
    server.device.post("/api/device/presence", json={"idle_seconds": 0, "session": "active", "after": "unlock"},
                       headers=a)
    assert server.client.get("/api/device").json()["current_device"] == "d1"
    returned = server.app.hub.returned(OPERATOR)
    assert returned is not None and (returned.device_id, returned.via) == ("d1", "unlock")


def test_quiet_is_the_users_for_every_device(server: Server) -> None:
    """V3-P88 §6.2: a device or the Web sets it; every stream hears; past clears; seven days at most."""
    me = server.as_device(server.paired())
    device_stream = server.client.portal.call(lambda: server.app.hub.subscribe(OPERATOR, "d1"))  # type: ignore[union-attr]
    until = (datetime.now(UTC) + timedelta(hours=1)).astimezone(timezone(timedelta(hours=9)))
    response = server.device.request("PUT", "/api/presence/quiet", json={"until": until.isoformat()}, headers=me)
    assert response.status_code == 200 and datetime.fromisoformat(response.json()["until"]) == until
    assert datetime.fromisoformat(server.client.get("/api/devices").json()["quiet_until"]) == until
    assert datetime.fromisoformat(server.device.get("/api/device", headers=me).json()["quiet_until"]) == until

    async def names() -> list[tuple[str, Any]]:
        out = []
        while (event := await device_stream.next(0)) is not None:
            out.append((event.name, event.data))
        return out

    assert server.client.portal is not None
    assert ("quiet", {"until": until.isoformat()}) in server.client.portal.call(names)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert server.client.put("/api/presence/quiet", json={"until": past}, headers=SEND).json() == {"until": None}
    assert server.client.get("/api/devices").json()["quiet_until"] is None
    server.client.put("/api/presence/quiet", json={"until": until.isoformat()}, headers=SEND)
    assert server.client.put("/api/presence/quiet", json={"until": None}, headers=SEND).json() == {"until": None}
    for bad, code in (((datetime.now(UTC) + timedelta(days=8)).isoformat(), "quiet_too_long"),
                      ("2026-11-01T08:00:00", "invalid_time"), ("tomorrow", "invalid_time")):
        response = server.client.put("/api/presence/quiet", json={"until": bad}, headers=SEND)
        assert (response.status_code, error_code(response)) == (400, code), bad


# --- V3-P87 §9, §11: speaking first ------------------------------------------------------

def test_speaking_first_routes_are_the_operators(server: Server) -> None:
    server.sign_in()
    status = server.client.get("/api/proactive").json()
    assert status == {"enabled": False, "told": "off (HIMARI_PROACTIVE=0)", "current_device": None,
                      "quiet_hours": "23:00-08:00"}
    refused = server.client.post("/api/proactive/try", headers=SEND)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "proactive_off"
    token = server.paired()
    for method, path in (("GET", "/api/proactive"), ("POST", "/api/proactive/try")):
        answer = server.device.request(method, path, headers={**SEND, **server.as_device(token)})
        assert answer.status_code == 403 and answer.json()["error"]["code"] == "device_scope"


def test_a_wake_is_shown_as_a_line_with_its_reasons() -> None:
    from himari.common import EventId
    from himari.data.api import ConversationEvent, EventKind, Wake, WakeKind, WakeReason

    wake = Wake("wake_1", (WakeReason(WakeKind.REMINDER, memory_id=7, late=True),
                           WakeReason(WakeKind.FOCUS, activity="video", minutes=95),
                           WakeReason(WakeKind.RETURNED, away_minutes=240, via="unlock")), "d1")
    view = web.event_view(ConversationEvent(EventId("e1"), ConversationId("c"), 3, EventKind.WAKE, wake, 1,
                                            datetime.now(UTC)))
    assert view["kind"] == "wake"
    assert view["text"] == "HIMARI reached out · reminder m7 (late) · video for 1h 35m · you were away 4h"
    assert view["wake"] == {"id": "wake_1", "device_id": "d1", "reasons": [
        {"kind": "reminder", "memory_id": 7, "late": True}, {"kind": "focus", "activity": "video", "minutes": 95},
        {"kind": "returned", "away_minutes": 240, "via": "unlock"}]}


def test_speaking_first_settings_from_the_environment() -> None:
    base = {"HIMARI_WEB_ALLOWED_HOSTS": "localhost", "HIMARI_DATA_ROOT": "/tmp/x", "HIMARI_MODEL": "m"}
    off = WebConfig.from_env(base).proactive
    assert not off.enabled and (off.gap_minutes, off.daily, off.focus_minutes) == (30, 4, 30)
    on = WebConfig.from_env({**base, "HIMARI_PROACTIVE": "1", "HIMARI_PROACTIVE_QUIET": "",
                             "HIMARI_PROACTIVE_GAP_MINUTES": "45"}).proactive
    assert on.enabled and on.quiet_start is None and on.gap_minutes == 45
    for name, bad in (("HIMARI_PROACTIVE_QUIET", "late"), ("HIMARI_PROACTIVE_DAILY", "99")):
        with pytest.raises(WebConfigError):
            WebConfig.from_env({**base, name: bad})
