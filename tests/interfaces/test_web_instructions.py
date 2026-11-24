"""The user's instructions over the Web API (V3-P69 §7)."""

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
from himari.data.api import (  # noqa: E402
    MAX_INSTRUCTIONS_CHARS,
    MAX_PERSONA_CHARS,
    MAX_PERSONA_NAME_CHARS,
    MAX_PERSONAS,
)
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.prompt.api import USER_INSTRUCTIONS_HEADER  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"


class Server:
    def __init__(self, tmp_path: Path) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                           data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                           sandbox=False)
        self.client = TestClient(web.create_app(config, opener))

    def sign_in(self) -> None:
        assert self.application is not None
        response = self.client.post("/api/session", json={"token": self.application.operator_secret},
                                    headers={"origin": ORIGIN})
        assert response.status_code == 204, response.text

    def put(self, body: dict[str, Any] | str) -> Any:
        if isinstance(body, str):  # raw JSON, e.g. an escaped lone surrogate no encoder would write
            return self.client.put("/api/instructions", content=body,
                                   headers={"origin": ORIGIN, "content-type": "application/json"})
        return self.client.put("/api/instructions", json=body, headers={"origin": ORIGIN})


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        yield s


def test_read_save_and_the_next_message_carries_them(server: Server) -> None:
    server.sign_in()
    assert server.client.get("/api/instructions").json() == {
        "text": "", "revision": 0, "updated_at": None, "max_chars": MAX_INSTRUCTIONS_CHARS}
    saved = server.put({"text": "Answer in Korean.\r\n", "revision": 0})
    assert saved.status_code == 200
    body = saved.json()
    assert (body["text"], body["revision"]) == ("Answer in Korean.\n", 1) and body["updated_at"]
    assert server.client.get("/api/instructions").json() == body

    cid = server.client.post("/api/conversations", json={}, headers={"origin": ORIGIN}).json()["id"]
    server.provider.extend([final("네.")])
    assert server.client.post(f"/api/conversations/{cid}/messages", json={"text": "hi"},
                              headers={"origin": ORIGIN}).status_code == 202
    for _ in range(200):
        if not server.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
            break
        time.sleep(0.02)
    section = server.provider.prepared_inputs[-1].stable_prefix[-1]
    assert section.startswith(USER_INSTRUCTIONS_HEADER) and "\n<user_instructions>\nAnswer in Korean.\n\n" in section


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"text": "stale", "revision": 0}, 409, "revision_conflict"),  # saved since revision 0 was read
    ({"text": "x" * (MAX_INSTRUCTIONS_CHARS + 1), "revision": 1}, 422, "too_large"),
    ({"text": "가" * (MAX_INSTRUCTIONS_CHARS + 1), "revision": 1}, 422, "too_large"),
    ({"text": "a\u0000b", "revision": 1}, 422, "invalid_instructions"),
    ({"text": "esc \u001b[2J", "revision": 1}, 422, "invalid_instructions"),
    ('{"text": "a\\ud800b", "revision": 1}', 422, "invalid_instructions"),  # not a 500 on SQLite (review C1)
])
def test_refused_saves_say_why_and_change_nothing(server: Server, body: dict[str, Any] | str, status: int,
                                                  code: str) -> None:
    server.sign_in()
    assert server.put({"text": "kept", "revision": 0}).status_code == 200
    refused = server.put(body)
    assert refused.status_code == status and refused.json()["error"]["code"] == code
    assert "kept" not in refused.text and "stale" not in refused.text  # never echoes input
    assert server.client.get("/api/instructions").json()["text"] == "kept"


def test_the_longest_korean_text_fits_the_request_body_limit(server: Server) -> None:
    server.sign_in()  # 10,000 Hangul characters are 30 KB of UTF-8: well inside the 256 KiB body limit
    saved = server.put({"text": "가" * MAX_INSTRUCTIONS_CHARS, "revision": 0})
    assert saved.status_code == 200 and len(saved.json()["text"]) == MAX_INSTRUCTIONS_CHARS


@pytest.mark.parametrize("body", [{"text": "x"}, {"text": "x", "revision": -1}, {"text": 3, "revision": 0},
                                  {"text": "x", "revision": "0"}, {"text": "x", "revision": 0, "scope": "project"}])
def test_malformed_bodies_are_refused(server: Server, body: dict[str, Any]) -> None:
    server.sign_in()
    assert server.put(body).status_code in (400, 422)
    assert server.client.get("/api/instructions").json()["revision"] == 0


def test_sign_in_origin_and_the_cli_bearer(server: Server) -> None:
    assert server.client.get("/api/instructions").status_code == 401
    assert server.put({"text": "x", "revision": 0}).status_code == 401
    server.sign_in()
    # A cookie write needs an allowed origin.
    assert server.client.put("/api/instructions", json={"text": "x", "revision": 0}).status_code == 403
    assert server.client.put("/api/instructions", json={"text": "x", "revision": 0},
                             headers={"origin": "http://evil.example"}).status_code == 403
    assert server.client.get("/api/instructions").json()["revision"] == 0
    # The CLI's bearer needs none.
    assert server.application is not None
    bearer = derive_cli_bearer(server.application.operator_secret)
    server.client.cookies.clear()
    saved = server.client.put("/api/instructions", json={"text": "from cli", "revision": 0},
                              headers={"authorization": f"Bearer {bearer}"})
    assert saved.status_code == 200 and saved.json()["revision"] == 1


WRITE = {"origin": ORIGIN}


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def test_personas_are_made_edited_chosen_and_deleted(server: Server) -> None:
    """V3-P108 §6.1: the personas apart from the instructions, saying whether companion mode uses them."""
    server.sign_in()
    empty = server.client.get("/api/personas").json()
    assert empty == {"personas": [], "choice": {"persona_id": None, "revision": 0, "updated_at": None},
                     "companion": True, "limits": {"count": MAX_PERSONAS, "name_chars": MAX_PERSONA_NAME_CHARS,
                                                   "text_chars": MAX_PERSONA_CHARS}}
    made = server.client.post("/api/personas", json={"name": "Teacher", "text": "Call me 선생님."}, headers=WRITE)
    assert made.status_code == 201
    teacher = made.json()
    assert (teacher["id"], teacher["name"], teacher["text"], teacher["revision"]) == (1, "Teacher", "Call me 선생님.", 1)
    friend = server.client.post("/api/personas", json={"name": "Friend", "text": ""}, headers=WRITE).json()
    assert server.client.get("/api/instructions").json()["text"] == ""  # the instructions are untouched

    chosen = server.client.put("/api/personas/choice", json={"persona_id": 1, "revision": 0}, headers=WRITE)
    assert chosen.status_code == 200 and chosen.json()["persona_id"] == 1 and chosen.json()["revision"] == 1
    stale = server.client.put("/api/personas/choice", json={"persona_id": 2, "revision": 0}, headers=WRITE)
    assert (stale.status_code, error_code(stale)) == (409, "revision_conflict")
    missing = server.client.put("/api/personas/choice", json={"persona_id": 9, "revision": 1}, headers=WRITE)
    assert (missing.status_code, error_code(missing)) == (404, "not_found")

    edited = server.client.put(f"/api/personas/{friend['id']}", json={"name": "Friend", "text": "반말로.",
                                                                     "revision": 1}, headers=WRITE)
    assert edited.status_code == 200 and edited.json()["revision"] == 2 and edited.json()["text"] == "반말로."
    taken = server.client.put(f"/api/personas/{friend['id']}", json={"name": " teacher ", "text": "",
                                                                    "revision": 2}, headers=WRITE)
    assert (taken.status_code, error_code(taken)) == (409, "duplicate_name")
    old = server.client.put(f"/api/personas/{friend['id']}", json={"name": "F", "text": "", "revision": 1},
                            headers=WRITE)
    assert (old.status_code, error_code(old)) == (409, "revision_conflict")
    too_long = server.client.post("/api/personas", json={"name": "Long", "text": "가" * (MAX_PERSONA_CHARS + 1)},
                                  headers=WRITE)
    assert (too_long.status_code, error_code(too_long)) == (422, "too_large")
    nameless = server.client.post("/api/personas", json={"name": "  ", "text": "x"}, headers=WRITE)
    assert (nameless.status_code, error_code(nameless)) == (422, "invalid_persona")

    # deleting the chosen one leaves none chosen (§4.3)
    gone = server.client.delete("/api/personas/1", params={"revision": 1}, headers=WRITE)
    assert gone.status_code == 200 and gone.json()["persona_id"] is None and gone.json()["revision"] == 2
    listed = server.client.get("/api/personas").json()
    assert [p["name"] for p in listed["personas"]] == ["Friend"] and listed["choice"]["persona_id"] is None
    again = server.client.delete("/api/personas/1", params={"revision": 1}, headers=WRITE)
    assert (again.status_code, error_code(again)) == (404, "not_found")
    # an id no SQLite integer can hold is no persona, not a server error (review)
    huge = server.client.put("/api/personas/99999999999999999999", json={"name": "x", "text": "", "revision": 1},
                             headers=WRITE)
    assert (huge.status_code, error_code(huge)) == (404, "not_found")
    huge_choice = server.client.put("/api/personas/choice", json={"persona_id": 10**20, "revision": 2},
                                    headers=WRITE)
    assert (huge_choice.status_code, error_code(huge_choice)) == (404, "not_found")


def test_personas_need_a_signed_in_user_and_an_allowed_origin(server: Server) -> None:
    assert server.client.get("/api/personas").status_code == 401
    server.sign_in()
    assert server.client.post("/api/personas", json={"name": "A", "text": ""}).status_code == 403
    assert server.client.post("/api/personas", json={"name": "A", "text": ""},
                              headers={"origin": "http://evil.example"}).status_code == 403
    assert server.client.get("/api/personas").json()["personas"] == []


def test_the_persona_limit(server: Server) -> None:
    server.sign_in()
    for n in range(MAX_PERSONAS):
        assert server.client.post("/api/personas", json={"name": f"P{n}", "text": ""}, headers=WRITE).status_code == 201
    full = server.client.post("/api/personas", json={"name": "One more", "text": ""}, headers=WRITE)
    assert (full.status_code, error_code(full)) == (409, "persona_limit")


def test_the_old_persona_routes_are_gone(server: Server) -> None:
    server.sign_in()
    assert server.client.get("/api/instructions/persona").status_code in (404, 405)
