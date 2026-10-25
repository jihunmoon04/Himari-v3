"""A Web server on the real Application for interface tests, with a machine runner socket or none."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from himari.bootstrap.application import Application
from himari.interfaces.web import app as web
from himari.interfaces.web.config import WebConfig
from himari.provider.scripted import ScriptedAdapter

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Server:
    def __init__(self, tmp_path: Path, socket: str | None) -> None:
        self.application: Application | None = None
        self.provider = ScriptedAdapter([])

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider,
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
