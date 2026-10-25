"""Folders on this server as Workspaces over the Web API (V3-P74 §10.3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from runnerkit import RunnerKit, start_runner  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Server:
    def __init__(self, tmp_path: Path, socket: str | None) -> None:
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", ScriptedAdapter([]),
                                                      model_id="scripted-model", sandbox=False, cleanup_seconds=None,
                                                      machine_socket=socket)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                           data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                           sandbox=False)
        self.client = TestClient(web.create_app(config, opener))

    def sign_in(self) -> None:
        assert self.application is not None
        response = self.client.post("/api/session", json={"token": self.application.operator_secret}, headers=SEND)
        assert response.status_code == 204, response.text

    def new(self) -> str:
        return str(self.client.post("/api/conversations", json={}, headers=SEND).json()["id"])

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(path, json=body, headers=SEND)


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path / "runner")
    yield started
    started.close()


@pytest.fixture
def server(tmp_path: Path, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, kit.socket)
    with s.client:
        s.sign_in()
        yield s


def test_register_edit_and_unregister(server: Server, kit: RunnerKit) -> None:
    assert server.client.get("/api/workspaces").json() == {"available": True, "items": []}
    made = server.post("/api/workspaces", {"path": "~/projects/a", "note": "C"})
    assert made.status_code == 201, made.text
    item = made.json()
    assert (item["path"], item["label"], item["note"], item["attached"]) == (str(kit.home / "projects/a"), "a", "C", 0)
    again = server.post("/api/workspaces", {"path": str(kit.home / "projects/a")})
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate"
    hidden = server.post("/api/workspaces", {"path": "~/.ssh"})
    assert hidden.status_code == 422 and hidden.json()["error"]["code"] == "not_usable"
    edited = server.client.patch(f"/api/workspaces/{item['id']}", json={"label": "A", "note": None}, headers=SEND)
    assert edited.status_code == 200 and edited.json()["label"] == "A" and edited.json()["note"] is None
    bad = server.client.patch(f"/api/workspaces/{item['id']}", json={"label": "", "note": None}, headers=SEND)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_workspace"
    assert server.client.delete("/api/workspaces/wsp_none", headers=SEND).status_code == 404
    assert server.client.delete(f"/api/workspaces/{item['id']}", headers=SEND).status_code == 204
    assert server.client.get("/api/workspaces").json()["items"] == []


def test_attach_working_and_detach(server: Server, kit: RunnerKit) -> None:
    cid = server.new()
    folders = server.client.get(f"/api/conversations/{cid}/workspaces").json()["items"]
    assert [(f["label"], f["own"], f["working"]) for f in folders] == [("this conversation", True, True)]
    assert folders[0]["path"] == str(kit.himari_home / "conversations" / cid)
    attached = server.post(f"/api/conversations/{cid}/workspaces", {"path": "~/projects/a", "label": "proj"})
    assert attached.status_code == 200, attached.text
    items = attached.json()["items"]
    assert [(f["label"], f["working"]) for f in items] == [("this conversation", False), ("proj", True)]
    both = server.post(f"/api/conversations/{cid}/workspaces", {"path": "x", "workspace_id": "wsp_1"})
    assert both.status_code == 400
    twice = server.post(f"/api/conversations/{cid}/workspaces", {"workspace_id": items[1]["workspace_id"]})
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "duplicate"
    working = server.client.put(f"/api/conversations/{cid}/workspaces/working",
                                json={"binding_id": items[0]["binding_id"]}, headers=SEND)
    assert [f["working"] for f in working.json()["items"]] == [True, False]
    own = server.client.delete(f"/api/conversations/{cid}/workspaces/{items[0]['binding_id']}", headers=SEND)
    assert own.status_code == 409 and own.json()["error"]["code"] == "own_folder"
    attached_still = server.client.delete(f"/api/workspaces/{items[1]['workspace_id']}", headers=SEND)
    assert attached_still.status_code == 409 and attached_still.json()["error"]["code"] == "workspace_attached"
    gone = server.client.delete(f"/api/conversations/{cid}/workspaces/{items[1]['binding_id']}", headers=SEND)
    assert [f["own"] for f in gone.json()["items"]] == [True]
    missing = server.client.delete(f"/api/conversations/{cid}/workspaces/bind_none", headers=SEND)
    assert missing.status_code == 404
    assert server.client.get("/api/conversations/conv_none/workspaces").status_code == 404


def test_without_the_machine_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, None)
    with s.client:
        s.sign_in()
        assert s.client.get("/api/workspaces").json() == {"available": False, "items": []}
        refused = s.post("/api/workspaces", {"path": "/tmp"})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "unavailable"
        cid = s.new()
        assert s.client.get(f"/api/conversations/{cid}/workspaces").json()["items"] == []  # Inside
