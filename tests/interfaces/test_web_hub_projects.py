"""The hub's projects, device folders and project requests over the Web (V3-P103 §4, §4.2), against a stand-in hub
server: the routes (operator only, Origin, bodies rebuilt with their limits, names never in a URL path), the error
mapping, the long ``resolve``, what the log keeps, and the two list-stream events."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from webkit import ORIGIN, SEND  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web import hub as hub_module  # noqa: E402
from himari.interfaces.web import hub_stream  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.interfaces.web.hub import HubClient, HubUnknown, project_request_view, projects_view  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

TOKEN = "user-secret"
SPECIAL = "a/b?c#d 한글 x"
SECRET_PATH = "/home/u/secret-folder"
CODE = "482-913"
REQUEST = {"id": 7, "kind": "create", "project": "site", "runner": "laptop", "origin": "model",
           "path": SECRET_PATH, "create_dir": True, "agent": "claude", "why": "for the site", "code": CODE,
           "mode": "local", "state": "waiting_device", "rev": 3, "result_path": None, "reason": None,
           "resolving": False, "created_at": "2026-11-18T10:00:00Z", "decided_at": None,
           "expires_at": "2026-11-18T10:05:00Z", "claimed_at": None, "ended_at": None, "same_name_on": ["linux"]}
PROJECT = {"name": SPECIAL, "path": "/p/x", "default_agent": "codex", "preset": None, "profile": None}


def sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class Hub:
    """``/user/<op>`` and the list stream. ``replies[op]``: (status, body); status 0 drops the connection after the
    hub read it. ``delays[op]``: seconds before it answers. The list stream sends what is put on ``list_frames``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies: dict[str, tuple[int, dict[str, Any]]] = {}
        self.delays: dict[str, float] = {}
        self.list_frames: queue.Queue[str | None] = queue.Queue()
        self.busy = False
        self.streams = 0
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _reply(self, status: int, reply: dict[str, Any]) -> None:
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                op = self.path.removeprefix("/user/")
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "token"})
                hub.calls.append((op, body))
                time.sleep(hub.delays.get(op, 0))
                status, reply = hub.replies.get(op, (200, {}))
                if status == 0:
                    self.close_connection = True
                    return
                self._reply(status, reply)

            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "token"})
                hub.streams += 1
                if hub.busy:
                    return self._reply(503, {"error": "streams"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    while (item := hub.list_frames.get()) is not None:
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.list_frames.put(None)
        self.server.shutdown()
        self.server.server_close()


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
def hub() -> Iterator[Hub]:
    started = Hub()
    yield started
    started.close()


@pytest.fixture
def client(tmp_path: Path, hub: Hub, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    token = tmp_path / "user.token"
    token.write_text(TOKEN + "\n", encoding="ascii")
    started = client_for(tmp_path, monkeypatch, hub_url=hub.url, hub_user_token_file=str(token))
    yield started
    started.__exit__(None, None, None)


# -- the views ------------------------------------------------------------------------------------

def test_a_request_keeps_the_contracts_fields_of_the_right_type() -> None:
    shown = project_request_view({**REQUEST, "nonce": "secret", "client_key": "k", "rev": 3, "why": "w" * 900,
                                  "create_dir": "yes"})
    assert shown is not None
    assert "nonce" not in shown and "client_key" not in shown and "create_dir" not in shown  # of the wrong type
    assert len(shown["why"]) == 500 and shown["code"] == CODE and shown["path"] == SECRET_PATH
    assert shown["same_name_on"] == ["linux"]
    older = {k: v for k, v in REQUEST.items() if k != "resolving"}  # a hub without H17
    assert project_request_view(older) == {**REQUEST, "resolving": False}
    for bad in ({**REQUEST, "rev": None}, {**REQUEST, "id": "7"}, {**REQUEST, "state": 1}, "x", None):
        assert project_request_view(bad) is None, bad


def test_projects_carry_each_devices_way_its_folders_and_the_held_names() -> None:
    shown = projects_view({
        "projects": [PROJECT],
        "runners": [{"name": "laptop", "agents": ["claude"], "projects": ["demo"], "connected": True,
                     "approve": {"mode": "local", "roots": ["/r", 3], "why": None, "policy_fp": "x"},
                     "folders": {"demo": "D:/code/demo", "bad": 3}, "token": "no"}],
        "held": [{"project": "orphan", "runner": "gone", "created_at": "t", "extra": 1}, "x"],
    })
    runner = shown["runners"][0]
    assert runner["approve"] == {"mode": "local", "roots": ["/r"], "why": None}
    assert runner["folders"] == {"demo": "D:/code/demo"} and "token" not in runner
    assert shown["held"] == [{"project": "orphan", "runner": "gone", "created_at": "t"}]
    assert "held" not in projects_view({"projects": []})  # a hub without H8: the page leaves adoption out


# -- the routes -----------------------------------------------------------------------------------

def test_each_route_reaches_its_op_with_the_body_rebuilt(client: TestClient, hub: Hub) -> None:
    hub.replies = {
        "project_requests": (200, {"requests": [REQUEST, {"id": "bad"}], "missing": [9, "x"]}),
        "project_request": (200, {"request": REQUEST}),
        "project_request_answer": (200, {"request": {**REQUEST, "rev": 4}}),
        "project_request_cancel": (200, {"request": {**REQUEST, "state": "cancelled", "rev": 5}}),
        "project_request_resolve": (200, {"request": {**REQUEST, "state": "done", "rev": 6}}),
        "project_folder_remove": (200, {"request": {**REQUEST, "kind": "remove_folder"}}),
        "project_set": (200, {"project": {**PROJECT, "secret": 1}}),
        "project_adopt": (200, {"project": PROJECT}),
        "runner_dirs": (200, {"path": "/r", "dirs": ["a", 2, "b"]}),
    }
    listed = client.get("/api/hub/project-requests", params={"open": "true"}).json()
    assert listed == {"requests": [REQUEST], "missing": [9]}
    client.get("/api/hub/project-requests", params={"limit": 20})
    client.get("/api/hub/project-requests", params={"ids": "7,9,7"})
    made = client.post("/api/hub/project-requests", headers=SEND, json={
        "kind": "create", "project": SPECIAL, "runner": "laptop", "create_dir": True, "agent": "claude",
        "client_key": "k1", "key_time": "2026-11-18T10:00:00Z"})
    assert made.status_code == 200 and made.json() == {"request": REQUEST}
    client.post("/api/hub/project-requests", headers=SEND, json={
        "kind": "add_folder", "project": "demo", "runner": "linux", "path": "/r/demo"})
    assert client.post("/api/hub/project-requests/7/answer", json={"approve": True}, headers=SEND).json()[
        "request"]["rev"] == 4
    client.post("/api/hub/project-requests/7/cancel", headers=SEND)
    client.post("/api/hub/project-requests/7/resolve", json={}, headers=SEND)
    client.post("/api/hub/project-requests/7/resolve", json={"force": True, "outcome": "not_applied"}, headers=SEND)
    assert client.post("/api/hub/projects/settings", headers=SEND, json={
        "project": SPECIAL, "agent": "codex", "preset": None}).json() == {"project": PROJECT}
    client.post("/api/hub/projects/remove-folder", headers=SEND, json={"project": SPECIAL, "runner": "linux"})
    client.post("/api/hub/projects/adopt", headers=SEND, json={"project": SPECIAL, "runner": "laptop"})
    dirs = client.post("/api/hub/runner-dirs", headers=SEND, json={"runner": "linux", "path": "/r"}).json()
    assert dirs == {"path": "/r", "dirs": ["a", "b"]}
    client.post("/api/hub/runner-dirs", headers=SEND, json={"runner": "linux"})
    assert hub.calls == [
        ("project_requests", {"open": True}),
        ("project_requests", {"limit": 20}),
        ("project_requests", {"ids": [7, 9]}),
        ("project_request", {"kind": "create", "project": SPECIAL, "runner": "laptop", "create_dir": True,
                             "agent": "claude", "client_key": "k1", "key_time": "2026-11-18T10:00:00Z"}),
        ("project_request", {"kind": "add_folder", "project": "demo", "runner": "linux", "path": "/r/demo"}),
        ("project_request_answer", {"id": 7, "approve": True}),
        ("project_request_cancel", {"id": 7}),
        ("project_request_resolve", {"id": 7}),
        ("project_request_resolve", {"id": 7, "force": True, "outcome": "not_applied"}),
        ("project_set", {"project": SPECIAL, "agent": "codex", "preset": None}),  # only what was given
        ("project_folder_remove", {"project": SPECIAL, "runner": "linux"}),
        ("project_adopt", {"project": SPECIAL, "runner": "laptop"}),
        ("runner_dirs", {"runner": "linux", "path": "/r"}),
        ("runner_dirs", {"runner": "linux"}),
    ]
    assert TOKEN not in json.dumps(listed)


def test_a_hub_without_ids_says_nothing_of_missing(client: TestClient, hub: Hub) -> None:
    hub.replies["project_requests"] = (200, {"requests": [REQUEST]})  # no H12: the page cannot tell gone from old
    assert client.get("/api/hub/project-requests", params={"ids": "7"}).json() == {"requests": [REQUEST],
                                                                                    "missing": None}
    for odd in ({}, {"requests": None, "missing": [7]}, {"requests": "x"}):  # not an answer: a failed read
        hub.replies["project_requests"] = (200, odd)
        failed = client.get("/api/hub/project-requests", params={"ids": "7"})
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "hub_unknown", odd


def test_names_never_go_in_a_path_and_the_routes_take_their_limits(client: TestClient, hub: Hub) -> None:
    routes = {route.path for route in client.app.routes if "/api/hub/" in getattr(route, "path", "")}  # type: ignore[attr-defined]
    new = {path for path in routes if "project" in path or "runner" in path or "client-key" in path}
    assert all("{project}" not in path or path.endswith("/preset") for path in new)  # the old PUT …/preset stays
    assert all("{runner}" not in path for path in new)
    bad: list[tuple[str, str, dict[str, Any]]] = [
        ("post", "/api/hub/project-requests", {"kind": "remove_folder", "project": "p", "runner": "r"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "", "runner": "r"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p" * 201, "runner": "r"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "r", "path": "/" * 4001}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "r", "create_dir": 1}),
        ("post", "/api/hub/project-requests", {"kind": "add_folder", "project": "p", "runner": "r", "agent": "x"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "r", "client_key": "k"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "r",
                                               "client_key": "k" * 65, "key_time": "t"}),
        ("post", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "r", "other": 1}),
        ("post", "/api/hub/project-requests/7/answer", {"approve": "yes"}),
        ("post", "/api/hub/project-requests/7/resolve", {"force": True}),
        ("post", "/api/hub/project-requests/7/resolve", {"outcome": "applied"}),
        ("post", "/api/hub/project-requests/7/resolve", {"force": True, "outcome": "maybe"}),
        ("post", "/api/hub/project-requests/0/cancel", {}),
        ("post", "/api/hub/projects/settings", {"project": "p", "color": "red"}),
        ("post", "/api/hub/projects/remove-folder", {"project": "p"}),
        ("post", "/api/hub/projects/adopt", {"runner": "r"}),
        ("post", "/api/hub/runner-dirs", {"path": "/r"}),
    ]
    for method, path, body in bad:
        assert getattr(client, method)(path, json=body, headers=SEND).status_code == 400, (path, body)
    for ids in ("x", "1,,2", ",".join(str(n) for n in range(1, 52)), "0", str(2**53 + 1), "1;2"):
        assert client.get("/api/hub/project-requests", params={"ids": ids}).status_code == 400, ids
    assert client.get("/api/hub/project-requests", params={"limit": 501}).status_code == 400
    assert hub.calls == []  # nothing reached the hub


# Every V3-P103 route, with a body it would take.
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/hub/project-requests?open=true", None),
    ("GET", "/api/hub/project-requests?ids=7", None),
    ("POST", "/api/hub/project-requests", {"kind": "create", "project": "p", "runner": "linux"}),
    ("POST", "/api/hub/project-requests/7/answer", {"approve": True}),
    ("POST", "/api/hub/project-requests/7/cancel", {}),
    ("POST", "/api/hub/project-requests/7/resolve", {}),
    ("POST", "/api/hub/projects/settings", {"project": "p", "agent": None}),
    ("POST", "/api/hub/projects/remove-folder", {"project": "p", "runner": "linux"}),
    ("POST", "/api/hub/client-key", {}),
    ("POST", "/api/hub/projects/adopt", {"project": "p", "runner": "linux"}),
    ("POST", "/api/hub/runner-dirs", {"runner": "linux"}),
]


def without_cookie(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    """The same client with no Web session (one event loop: SQLite stays on its thread)."""
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        return client.request(method, path, **kwargs)
    finally:
        for name, value in saved.items():
            client.cookies.set(name, value)


def test_only_the_operator_and_only_from_the_page(client: TestClient, hub: Hub) -> None:
    # a paired device, with its own token: refused on every route (V3-P83: a device may not use the rest)
    code = client.post("/api/devices", json={"name": "phone"}, headers=SEND).json()["code"]
    device_token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    paired = without_cookie(client, "POST", "/api/devices/pair", json={"token": device_token},
                            headers={"authorization": f"Pairing {code}"})
    assert paired.status_code == 200, paired.text
    for method, path, body in ROUTES:
        sent = {"json": body} if body is not None else {}
        as_device = without_cookie(client, method, path, headers={"authorization": f"Bearer {device_token}", **SEND},
                                   **sent)
        assert (as_device.status_code, as_device.json()["error"]["code"]) == (403, "device_scope"), path
        nobody = without_cookie(client, method, path, headers=SEND, **sent)
        assert (nobody.status_code, nobody.json()["error"]["code"]) == (401, "unauthenticated"), path
        if method == "POST":  # a change (or a path in a body) from the page only
            refused = client.post(path, json=body)  # a cookie and no Origin
            assert refused.status_code == 403 and refused.json()["error"]["code"] == "origin_forbidden", path
    client.delete("/api/session", headers=SEND)
    for method, path, body in ROUTES:  # signed out
        assert client.request(method, path, headers=SEND, **({"json": body} if body is not None else {})
                              ).status_code == 401, path
    assert hub.calls == []


def test_a_client_key_is_this_servers_and_reaches_no_hub(client: TestClient, hub: Hub) -> None:
    first, second = (client.post("/api/hub/client-key", headers=SEND).json() for _ in range(2))
    assert len(first["client_key"]) == 32 and int(first["client_key"], 16) >= 0  # 128 random bits
    assert first["client_key"] != second["client_key"]
    assert first["key_time"].endswith("Z") and first["key_time"][:4] == time.strftime("%Y", time.gmtime())
    assert hub.calls == []


def test_the_hubs_answers_are_told_apart(client: TestClient, hub: Hub, tmp_path: Path) -> None:
    hub.replies = {"project_request": (400, {"error": "client_key_expired: 키가 너무 오래됐습니다"}),
                   "project_request_resolve": (400, {"error": "resolving: 요청 #7을 이미 정리하는 중입니다"}),
                   "project_folder_remove": (0, {}), "project_adopt": (200, {"nothing": 1})}
    body = {"kind": "create", "project": "p", "runner": "r", "client_key": "k", "key_time": "t"}
    refused = client.post("/api/hub/project-requests", json=body, headers=SEND)
    assert refused.status_code == 409 and refused.json()["error"] == {
        "code": "hub_refused", "message": "client_key_expired: 키가 너무 오래됐습니다"}  # its words, for the page
    busy = client.post("/api/hub/project-requests/7/resolve", json={}, headers=SEND)
    assert busy.json()["error"]["message"].startswith("resolving:")
    lost = client.post("/api/hub/projects/remove-folder", json={"project": "p", "runner": "r"}, headers=SEND)
    assert lost.status_code == 503 and lost.json()["error"]["code"] == "hub_unknown"
    odd = client.post("/api/hub/projects/adopt", json={"project": "p", "runner": "r"}, headers=SEND)
    assert odd.status_code == 503 and odd.json()["error"]["code"] == "hub_unknown"  # done, but not readable
    hub.replies = {"project_request": (500, {}), "project_request_answer": (502, {}), "runner_dirs": (500, {})}
    for path, sent in (("/api/hub/project-requests", body), ("/api/hub/project-requests/7/answer", {"approve": True})):
        failed = client.post(path, json=sent, headers=SEND)  # a change the hub took may have been done
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "hub_unknown"
    read = client.post("/api/hub/runner-dirs", json={"runner": "r"}, headers=SEND)  # a read: nothing was done
    assert read.status_code == 503 and read.json()["error"]["code"] == "hub_unavailable"
    (tmp_path / "user.token").write_text("wrong", encoding="ascii")
    wrong = client.post("/api/hub/runner-dirs", json={"runner": "r"}, headers=SEND)
    assert wrong.status_code == 503 and wrong.json()["error"]["code"] == "hub_misconfigured"
    hub.close()
    (tmp_path / "user.token").write_text(TOKEN, encoding="ascii")
    gone = client.get("/api/hub/project-requests", params={"open": "true"})
    assert gone.status_code == 503 and gone.json()["error"]["code"] == "hub_unavailable"


def test_the_log_keeps_ids_states_and_kinds_only(client: TestClient, hub: Hub, caplog: pytest.LogCaptureFixture
                                                 ) -> None:
    caplog.set_level(logging.DEBUG)
    hub.replies = {"project_request": (200, {"request": REQUEST}),
                   "runner_dirs": (400, {"error": f"roots 아래가 아닙니다: {SECRET_PATH}"}),
                   "project_folder_remove": (400, {"error": f"폴더가 없습니다: {SECRET_PATH}"})}
    client.post("/api/hub/project-requests", headers=SEND, json={
        "kind": "create", "project": "site", "runner": "laptop", "path": SECRET_PATH})
    client.post("/api/hub/runner-dirs", headers=SEND, json={"runner": "linux", "path": SECRET_PATH})
    client.post("/api/hub/projects/remove-folder", headers=SEND, json={"project": "site", "runner": "linux"})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "request 7 create waiting_device" in logged and "runner_dirs: hub_refused" in logged
    for secret in (SECRET_PATH, CODE, TOKEN, "roots 아래", "폴더가 없습니다"):
        assert secret not in logged, secret
    assert SECRET_PATH not in "\n".join(str(getattr(r, "args", "")) for r in caplog.records)


def test_resolve_waits_longer_than_the_rest(hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "RESOLVE_SECONDS", 2.0)
    token = tmp_path / "user.token"
    token.write_text(TOKEN, encoding="ascii")
    client = HubClient(hub.url, str(token), timeout=0.3)
    hub.delays = {"project_request_resolve": 0.6, "project_request_cancel": 0.6}
    hub.replies = {"project_request_resolve": (200, {"request": {**REQUEST, "state": "done"}}),
                   "project_request_cancel": (200, {"request": REQUEST})}
    assert asyncio.run(client.project_request_resolve(7))["state"] == "done"  # the fence's answer came in time
    with pytest.raises(HubUnknown):
        asyncio.run(client.project_request_cancel(7))
    assert hub_module.DIRS_SECONDS == 15.0


# -- the list stream (§4.2) -----------------------------------------------------------------------

def frames(text: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


async def until(reader: hub_stream.ListReader, done: Callable[[list[tuple[str, Any]]], bool]
                ) -> list[tuple[str, Any]]:
    got: list[tuple[str, Any]] = []
    while not done(got):
        await reader.ready.wait()
        got += frames("".join(reader.take()))
    return got


def run_list(hub: Hub, tmp_path: Path, act: Callable[[hub_stream.ListRelay], Any]) -> Any:
    token = tmp_path / "user.token"
    token.write_text(TOKEN, encoding="ascii")

    async def scenario() -> Any:
        client = HubClient(hub.url, str(token))
        upstream = hub_stream.Upstream(client)
        relay = hub_stream.ListRelay(client, upstream)
        try:
            return await asyncio.wait_for(act(relay), 10)
        finally:
            await relay.stop()
            await upstream.aclose()

    return asyncio.run(scenario())


SESSION = {"id": 5, "project": "demo", "state": "open", "activity": "idle"}


def test_the_two_events_reach_every_browser_and_projects_follows_each_new_list(hub: Hub, tmp_path: Path) -> None:
    async def act(relay: hub_stream.ListRelay) -> None:
        first, second = relay.join(), relay.join()
        hub.list_frames.put(sse("sessions", {"sessions": [SESSION]}))
        for reader in (first, second):
            got = await until(reader, lambda g: len(g) >= 2)
            assert [name for name, _ in got] == ["sessions", "projects"]  # read the projects: events may be lost
        hub.list_frames.put(sse("project_request", {**REQUEST, "nonce": "n"}))
        hub.list_frames.put(sse("project_request", {"id": 8}))  # no rev: not a request the page can merge
        hub.list_frames.put(sse("projects", {"extra": True}))
        for reader in (first, second):
            got = await until(reader, lambda g: len(g) >= 2)
            assert got == [("project_request", REQUEST), ("projects", {})]
        hub.list_frames.put(None)  # the upstream comes back: a new whole list, and projects again
        hub.list_frames.put(sse("sessions", {"sessions": [SESSION]}))
        got = await until(first, lambda g: any(name == "projects" for name, _ in g))
        assert [name for name, _ in got][-2:] == ["sessions", "projects"]

    run_list(hub, tmp_path, act)


def test_while_polling_projects_goes_out_every_thirty_seconds(hub: Hub, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_stream, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(hub_stream, "PROJECTS_POLL_SECONDS", 0.3)
    hub.busy = True
    hub.replies["sessions"] = (200, {"sessions": [SESSION]})

    async def act(relay: hub_stream.ListRelay) -> None:
        reader = relay.join()
        start = time.monotonic()
        got = await until(reader, lambda g: sum(name == "projects" for name, _ in g) >= 3)
        told = [name for name, _ in got]
        assert told[:2] == ["sessions", "projects"]  # told at once on going down to polling
        assert told.count("sessions") >= 6  # and not with every read
        assert time.monotonic() - start >= 0.55

    run_list(hub, tmp_path, act)


def test_a_signed_out_browser_loses_its_list_while_requests_flow() -> None:
    async def scenario() -> int:
        relay = hub_stream.ListRelay(HubClient("http://127.0.0.1:1", "none"),
                                     hub_stream.Upstream(HubClient("http://127.0.0.1:1", "none")))
        reader = hub_stream.ListReader()

        async def changes() -> None:
            while True:
                reader.take()
                reader.offer(sse("project_request", REQUEST))
                await asyncio.sleep(0.002)

        feeding = asyncio.ensure_future(changes())
        try:
            sent = [text async for text in hub_stream.list_frames(relay, reader, lambda: False, asyncio.Event(),
                                                                  check=0.05)]
        finally:
            feeding.cancel()
        return len(sent)

    assert asyncio.run(asyncio.wait_for(scenario(), 5)) >= 1  # ended within the check, not by the feed


def test_the_list_stream_relays_through_the_server(client: TestClient, hub: Hub,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over HTTP: the browser's own stream gets the request rebuilt (operator view: path and code)."""
    monkeypatch.setattr(hub_stream, "LINGER_SECONDS", 0.1)
    hub.list_frames.put(sse("sessions", {"sessions": [SESSION]}))
    hub.list_frames.put(sse("project_request", REQUEST))
    seen: list[tuple[str, Any]] = []
    with client.stream("GET", "/api/hub/stream") as response:
        assert response.status_code == 200
        text = ""
        for chunk in response.iter_text():
            text += chunk
            seen = frames(text)
            if any(name == "project_request" for name, _ in seen):
                break
    assert ("project_request", REQUEST) in seen and ("projects", {}) in seen
