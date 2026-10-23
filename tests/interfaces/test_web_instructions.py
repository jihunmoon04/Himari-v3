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
from himari.data.api import MAX_INSTRUCTIONS_CHARS  # noqa: E402
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
