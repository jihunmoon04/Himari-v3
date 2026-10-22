"""Web and images (V3-P67 §8): the preview route and its headers, ``image``
in file views, and the images a tool result carries."""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap import application as application_module  # noqa: E402
from himari.bootstrap.application import Application, workspace_for  # noqa: E402
from himari.bootstrap.operator import OPERATOR  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, ConversationId, FileId, TrustedScope  # noqa: E402
from himari.data.api import PreparedView  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"


def encoded(size: tuple[int, int], fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, fmt)
    return out.getvalue()


COPY = encoded((20, 15), "JPEG")


class Views:
    """The image sandbox is off in these tests; the copy itself is tested on the host gate."""

    available = True

    async def make(self, original: bytes) -> PreparedView | str:
        return PreparedView(COPY, "image/jpeg", 20, 15)


class Server:
    def __init__(self, tmp_path: Path) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                           model_id="scripted-model", reasoning_effort=None, sandbox=False)
        self.client = TestClient(web.create_app(config, opener))

    def new(self) -> str:
        return str(self.client.post("/api/conversations", json={}, headers={"origin": ORIGIN}).json()["id"])

    def upload(self, cid: str, name: str, data: bytes, content_type: str) -> Any:
        return self.client.post(f"/api/conversations/{cid}/uploads", params={"name": name}, content=data,
                                headers={"origin": ORIGIN, "content-type": content_type}).json()

    def events(self, cid: str) -> list[dict[str, Any]]:
        for _ in range(300):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                items: list[dict[str, Any]] = self.client.get(f"/api/conversations/{cid}/events").json()["items"]
                return items
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def preview(self, cid: str, file: dict[str, Any]) -> Any:
        return self.client.get(f"/api/conversations/{cid}/files/{file['file_id']}/{file['version']}/view")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    async def views(root: Path, sandbox: bool) -> Views:
        return Views()

    monkeypatch.setattr(application_module, "_image_views", views)
    s = Server(tmp_path)
    with s.client:
        assert s.application is not None
        assert s.client.post("/api/session", json={"token": s.application.operator_secret},
                             headers={"origin": ORIGIN}).status_code == 204
        yield s


def test_an_uploaded_image_has_a_preview_of_its_copy(server: Server) -> None:
    cid = server.new()
    up = server.upload(cid, "photo.png", encoded((40, 30), "PNG"), "image/png")
    assert up["image"] == {"state": "ready", "media_type": "image/png", "width": 40, "height": 30,
                           "preview": {"width": 20, "height": 15}}
    shown = server.preview(cid, up)
    assert shown.status_code == 200 and shown.content == COPY  # the copy, never the uploaded bytes
    assert shown.headers["content-type"] == "image/jpeg"
    assert shown.headers["content-disposition"] == "inline"
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'; sandbox" in shown.headers.get_list("content-security-policy")
    assert server.client.get("/api/server").json()["images"] is True
    server.provider.extend([final("nice")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "look", "attachments": [up["file_id"]]},
                       headers={"origin": ORIGIN})
    attachment, = server.events(cid)[0]["attachments"]
    assert attachment["image"]["state"] == "ready"


@pytest.mark.parametrize("name, data, content_type", [
    ("evil.png", b"<html><script>alert(1)</script></html>", "image/png"),  # a type and a name are not bytes
    ("notes.txt", b"plain", "text/plain"),
    ("drawing.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
], ids=["html as png", "text", "svg"])
def test_a_file_that_is_not_an_image_has_no_preview(server: Server, name: str, data: bytes,
                                                    content_type: str) -> None:
    cid = server.new()
    up = server.upload(cid, name, data, content_type)
    assert up["image"] is None
    assert server.preview(cid, up).status_code == 404


def test_an_expired_copy_is_gone_and_says_so(server: Server) -> None:
    """Review B2: the copy expires apart from the original, which may still be downloaded."""
    cid = server.new()
    up = server.upload(cid, "photo.png", encoded((40, 30), "PNG"), "image/png")
    server.provider.extend([final("ok")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "", "attachments": [up["file_id"]]},
                       headers={"origin": ORIGIN})
    server.events(cid)
    assert server.application is not None
    scope = TrustedScope(OPERATOR, ConversationId(cid))
    application = server.application

    async def expire_copy() -> None:  # on the server's own thread: the database is used there only
        record, = await application.attachment_records(scope, [(FileId(up["file_id"]), 1)])
        assert record.view is not None
        application.data.blobs.rows.expire_blobs([record.view.blob.key], datetime.now(UTC))

    assert server.client.portal is not None
    server.client.portal.call(expire_copy)
    gone = server.preview(cid, up)
    assert (gone.status_code, gone.json()["error"]["code"]) == (410, "file_expired")
    attachment, = server.events(cid)[0]["attachments"]
    assert attachment["image"]["state"] == "view_expired" and not attachment["expired"]
    assert server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/1").status_code == 200


def test_a_tool_results_images_come_with_it(server: Server) -> None:
    """§8: ``images`` on the tool result; they are not files given to the user."""
    cid = server.new()
    assert server.application is not None
    folder = Path(server.application.inside.host_root(workspace_for(OPERATOR))) / "conversations" / cid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "chart.png").write_bytes(encoded((64, 48), "PNG"))
    server.provider.extend([calls(("inside/read_file", {"path": "chart.png"})), final("a chart")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "look at it"}, headers={"origin": ORIGIN})
    result = next(e for e in server.events(cid) if e["kind"] == "tool_result")
    image, = result["images"]
    assert (image["origin"], image["image"]) == (
        "tool", {"state": "ready", "media_type": "image/png", "width": 64, "height": 48,
                 "preview": {"width": 20, "height": 15}})
    assert result["presented_files"] == []
    assert server.preview(cid, image).content == COPY


def on_server(server: Server, step: Any) -> Any:
    """Runs ``step(application)`` on the server's own thread: the database is used there only."""
    assert server.application is not None and server.client.portal is not None
    application = server.application

    async def go() -> Any:
        return await step(application)

    return server.client.portal.call(go)


def sent_photo(server: Server) -> tuple[str, dict[str, Any], Any]:
    cid = server.new()
    up = server.upload(cid, "photo.png", encoded((40, 30), "PNG"), "image/png")
    server.provider.extend([final("ok")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "", "attachments": [up["file_id"]]},
                       headers={"origin": ORIGIN})
    server.events(cid)

    async def record(application: Application) -> Any:
        found, = await application.attachment_records(TrustedScope(OPERATOR, ConversationId(cid)),
                                                      [(FileId(up["file_id"]), 1)])
        return found

    return cid, up, on_server(server, record)


def test_a_preview_is_only_for_its_own_conversation_and_signed_in_user(server: Server) -> None:
    cid, up, _ = sent_photo(server)
    other = server.new()
    assert server.client.get(f"/api/conversations/{other}/files/{up['file_id']}/1/view").status_code == 404
    assert server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/2/view").status_code == 404
    server.client.cookies.clear()
    assert server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/1/view").status_code == 401


def test_a_copy_whose_file_is_gone_is_410_missing(server: Server) -> None:
    cid, up, record = sent_photo(server)
    on_server(server, lambda application: _remove(application, record.view.blob.key))
    gone = server.preview(cid, up)
    assert (gone.status_code, gone.json()["error"]["code"]) == (410, "file_missing")


async def _remove(application: Application, key: str) -> None:
    application.data.blobs.files.remove(key)


def test_with_the_original_expired_the_copy_is_still_shown(server: Server) -> None:
    """Review B2 (part 1): the original may expire first; the model and the preview keep the copy."""
    cid, up, record = sent_photo(server)

    async def expire(application: Application) -> None:
        application.data.blobs.rows.expire_blobs([record.blob.key], datetime.now(UTC))

    on_server(server, expire)
    assert server.preview(cid, up).status_code == 200
    attachment, = server.events(cid)[0]["attachments"]
    assert attachment["expired"] and attachment["image"]["state"] == "ready"
    listed, = server.client.get(f"/api/conversations/{cid}/files").json()["items"]
    assert listed["image"]["state"] == "ready"


@pytest.mark.parametrize("views, state", [(None, "no_sandbox"), ("refuses", "unprepared")],
                         ids=["no sandbox", "unprepared"])
def test_an_image_without_a_copy_says_why_and_has_no_preview(server: Server, views: str | None,
                                                             state: str) -> None:
    class Refusing:
        available = True

        async def make(self, original: bytes) -> str:
            return "DecompressionBombError"

    assert server.application is not None
    server.application.views = Refusing() if views else None
    cid = server.new()
    up = server.upload(cid, "photo.png", encoded((40, 30), "PNG"), "image/png")
    assert up["image"] == {"state": state, "media_type": "image/png", "width": 40, "height": 30, "preview": None}
    assert server.preview(cid, up).status_code == 404


def test_too_many_pixels_have_no_preview(server: Server) -> None:
    huge = bytearray(encoded((10, 10), "PNG"))
    huge[16:24] = (10_000).to_bytes(4, "big") + (6_000).to_bytes(4, "big")  # the header claims 60M pixels
    cid = server.new()
    up = server.upload(cid, "huge.png", bytes(huge), "image/png")
    assert up["image"]["state"] == "too_large" and server.preview(cid, up).status_code == 404


def test_an_answers_attachments_are_not_tool_images(server: Server) -> None:
    """PR #63 review C2: an ask_user answer is stored as that call's result, with its attachments."""
    cid = server.new()
    server.provider.extend([calls((ASK_USER_TOOL_ID, {"question": "Which file?"})), final("thanks")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "help"}, headers={"origin": ORIGIN})
    server.events(cid)
    up = server.upload(cid, "photo.png", encoded((40, 30), "PNG"), "image/png")
    answered = server.client.post(f"/api/conversations/{cid}/messages",  # a message while asked is the answer
                                  json={"text": "this one", "attachments": [up["file_id"]]}, headers={"origin": ORIGIN})
    assert answered.status_code == 202, answered.text
    result = next(e for e in server.events(cid) if e["kind"] == "tool_result")
    assert result["images"] == []
