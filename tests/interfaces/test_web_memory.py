"""The notes over the Web API (V3-P71 §7.2)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.common import CallId, ToolCall, ToolId  # noqa: E402
from himari.data.api import MAX_MEMORIES, MAX_MEMORY_CHARS  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Server:
    def __init__(self, tmp_path: Path, memory: bool = True) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None, memory=memory)
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

    def add(self, category: str, text: str) -> Any:
        return self.client.post("/api/memories", json={"category": category, "text": text}, headers=SEND)

    def listing(self) -> dict[str, Any]:
        return self.client.get("/api/memories").json()  # type: ignore[no-any-return]

    def wait(self, cid: str) -> None:
        for _ in range(200):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("still running")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        yield s


def test_add_edit_delete_and_list(server: Server) -> None:
    server.sign_in()
    assert server.listing() == {"memories": [], "max_count": MAX_MEMORIES, "max_chars": MAX_MEMORY_CHARS,
                                "enabled": True}
    added = server.add("preference", "  Short lists.\r\n")
    assert added.status_code == 201
    body = added.json()
    assert {k: body[k] for k in ("id", "category", "text", "origin", "source", "revision")} == {
        "id": 1, "category": "preference", "text": "Short lists.", "origin": "user", "source": None, "revision": 1}
    server.add("user", "Writes Go.")
    assert [m["id"] for m in server.listing()["memories"]] == [2, 1]  # user before preference

    edited = server.client.put("/api/memories/1", json={"category": "project", "text": "Builds HIMARI.",
                                                        "revision": 1}, headers=SEND)
    assert edited.status_code == 200 and (edited.json()["category"], edited.json()["revision"]) == ("project", 2)
    stale = server.client.put("/api/memories/1", json={"category": "project", "text": "lost", "revision": 1},
                              headers=SEND)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
    assert server.client.delete("/api/memories/1?revision=1", headers=SEND).status_code == 409
    assert server.client.delete("/api/memories/1?revision=2", headers=SEND).status_code == 204
    gone = server.client.delete("/api/memories/1", headers=SEND)
    assert gone.status_code == 404 and gone.json()["error"] == {"code": "not_found", "message": "No such note."}
    assert [m["text"] for m in server.listing()["memories"]] == ["Writes Go."]


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"category": "user", "text": "x" * (MAX_MEMORY_CHARS + 1)}, 422, "too_large"),
    ({"category": "user", "text": "  "}, 422, "invalid_memory"),
    ({"category": "user", "text": "esc \u001b[2J"}, 422, "invalid_memory"),
])
def test_refused_writes_say_why_and_never_echo(server: Server, body: dict[str, Any], status: int, code: str) -> None:
    server.sign_in()
    refused = server.client.post("/api/memories", json=body, headers=SEND)
    assert refused.status_code == status and refused.json()["error"]["code"] == code
    assert "\u001b" not in refused.text and "xxxx" not in refused.text
    assert server.listing()["memories"] == []


@pytest.mark.parametrize("body", [{"category": "mood", "text": "x"}, {"text": "x"}, {"category": "user"},
                                  {"category": "user", "text": 3}, {"category": "user", "text": "x", "id": 4}])
def test_malformed_bodies_are_refused(server: Server, body: dict[str, Any]) -> None:
    server.sign_in()
    assert server.client.post("/api/memories", json=body, headers=SEND).status_code in (400, 422)
    assert server.client.put("/api/memories/1", json={**body, "revision": 1}, headers=SEND).status_code in (400, 404, 422)
    assert server.listing()["memories"] == []


def test_a_full_store_says_so(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    server.sign_in()
    monkeypatch.setattr("himari.data.sqlite.MAX_MEMORIES", 1)
    assert server.add("user", "one").status_code == 201
    refused = server.add("user", "two")
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "memory_full"


def test_a_note_the_model_saved_names_its_conversation_and_the_delete_notice_counts_it(server: Server) -> None:
    server.sign_in()
    cid = server.client.post("/api/conversations", json={"title": "Trip"}, headers=SEND).json()["id"]
    server.provider.extend([NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                                          {"category": "project", "text": "Plans a trip."}),)),
                            final("Noted.")])
    assert server.client.post(f"/api/conversations/{cid}/messages", json={"text": "remember"},
                              headers=SEND).status_code == 202
    server.wait(cid)
    (note,) = server.listing()["memories"]
    assert (note["origin"], note["source"]) == ("model", {"conversation": cid, "title": "Trip", "in_trash": False})
    assert server.client.get(f"/api/conversations/{cid}/delete-notice").json()["memory_count"] == 1
    assert server.client.post(f"/api/conversations/{cid}/trash", headers=SEND).status_code == 200
    (note,) = server.listing()["memories"]
    assert note["source"] == {"conversation": cid, "title": None, "in_trash": True}
    assert server.client.delete(f"/api/conversations/{cid}", headers=SEND).status_code == 204
    assert server.listing()["memories"] == []


def test_sign_in_origin_and_the_cli_bearer(server: Server) -> None:
    assert server.client.get("/api/memories").status_code == 401
    assert server.add("user", "x").status_code == 401
    server.sign_in()
    assert server.client.post("/api/memories", json={"category": "user", "text": "x"}).status_code == 403
    assert server.client.post("/api/memories", json={"category": "user", "text": "x"},
                              headers={"origin": "http://evil.example"}).status_code == 403
    assert server.application is not None
    bearer = derive_cli_bearer(server.application.operator_secret)
    server.client.cookies.clear()
    saved = server.client.post("/api/memories", json={"category": "user", "text": "from cli"},
                               headers={"authorization": f"Bearer {bearer}"})
    assert saved.status_code == 201


def test_memory_off_still_lists_the_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    server = Server(tmp_path, memory=False)
    with server.client:
        server.sign_in()
        assert server.listing()["enabled"] is False
        assert server.client.get("/api/server").json()["memory"] is False


def test_himari_memory_turns_it_off() -> None:
    from himari.interfaces.web.config import WebConfigError

    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).memory is True  # V3-P71 §9: on by default
    assert WebConfig.from_env({**base, "HIMARI_MEMORY": "0"}).memory is False
    assert WebConfig.from_env({**base, "HIMARI_MEMORY": " off "}).memory is False
    with pytest.raises(WebConfigError):
        WebConfig.from_env({**base, "HIMARI_MEMORY": "sometimes"})
