"""Web and files (V3-P66 §9): upload, send with attachments, list, download,
the error table, and events with their files."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import calls, final  # noqa: E402

from himari.bootstrap.application import Application, workspace_for  # noqa: E402
from himari.bootstrap.operator import OPERATOR  # noqa: E402
from himari.data.api import DataError, DataErrorCode  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig, WebConfigError  # noqa: E402
from himari.provider.scripted import ScriptedAdapter, ScriptStep  # noqa: E402

ORIGIN = "http://testserver"


class Server:
    def __init__(self, tmp_path: Path, script: list[ScriptStep] = (), **kwargs: Any) -> None:  # type: ignore[assignment]
        self.provider = ScriptedAdapter(script)
        self.application: Application | None = None
        self.root = tmp_path / "data"

        async def opener() -> Application:
            self.application = await Application.open(self.root, self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None, **kwargs)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(self.root),
                           model_id="scripted-model", reasoning_effort=None, sandbox=False)
        self.client = TestClient(web.create_app(config, opener))

    def sign_in(self) -> None:
        assert self.application is not None
        response = self.client.post("/api/session", json={"token": self.application.operator_secret},
                                    headers={"origin": ORIGIN})
        assert response.status_code == 204, response.text

    def new(self) -> str:
        return str(self.client.post("/api/conversations", json={}, headers={"origin": ORIGIN}).json()["id"])

    def upload(self, cid: str, name: str, data: bytes, content_type: str = "text/plain") -> Any:
        return self.client.post(f"/api/conversations/{cid}/uploads", params={"name": name}, content=data,
                                headers={"origin": ORIGIN, "content-type": content_type})

    def send(self, cid: str, text: str, *attachments: str) -> Any:
        return self.client.post(f"/api/conversations/{cid}/messages", json={"text": text, "attachments": list(attachments)},
                                headers={"origin": ORIGIN})

    def wait_idle(self, cid: str) -> None:
        for _ in range(300):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("job did not finish")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        s.sign_in()
        yield s


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_upload_send_and_download(server: Server) -> None:
    cid = server.new()
    big = b"x" * 300_000  # over the general 256 KiB body limit
    up = server.upload(cid, "notes.txt", big, "text/plain; charset=utf-8")
    assert up.status_code == 201, up.text
    body = up.json()
    assert (body["name"], body["media_type"], body["size"], body["commands"]) == ("notes.txt", "text/plain", 300_000, False)
    server.provider.extend([final("got it")])
    sent = server.send(cid, "", body["file_id"])
    assert sent.status_code == 202, sent.text
    server.wait_idle(cid)
    events = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    assert events[0]["text"] == "" and [a["name"] for a in events[0]["attachments"]] == ["notes.txt"]
    assert events[0]["attachments"][0]["workspace_path"].endswith("/uploads/notes.txt")
    listed = server.client.get(f"/api/conversations/{cid}/files").json()
    assert [f["file_id"] for f in listed["items"]] == [body["file_id"]] and listed["next_offset"] is None
    got = server.client.get(f"/api/conversations/{cid}/files/{body['file_id']}/1")
    assert got.status_code == 200 and got.content == big
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"] == "attachment; filename=\"notes.txt\"; filename*=UTF-8''notes.txt"
    assert "sandbox" in got.headers.get_list("content-security-policy")
    assert got.headers["x-content-type-options"] == "nosniff"


def test_html_never_opens_on_this_origin_and_names_are_escaped(server: Server) -> None:
    cid = server.new()
    up = server.upload(cid, 'evil "x";\\.html', b"<script>alert(1)</script>", "text/html").json()
    assert up["name"] == 'evil "x";_.html'  # a backslash is never part of a name (§5.3)
    got = server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/1")
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"] == (
        "attachment; filename=\"evil _x___.html\"; filename*=UTF-8''evil%20%22x%22%3B_.html")
    korean = server.upload(cid, "보고서.pdf", b"%PDF").json()
    assert "filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C.pdf" in server.client.get(
        f"/api/conversations/{cid}/files/{korean['file_id']}/1").headers["content-disposition"]


def test_a_presented_file_comes_with_its_tool_result(server: Server) -> None:
    cid = server.new()
    assert server.application is not None
    folder = Path(server.application.inside.host_root(workspace_for(OPERATOR))) / "conversations" / cid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "out.csv").write_text("a,b\n")
    server.provider.extend([calls(("inside/present_file", {"path": "out.csv"})), final("here")])
    server.send(cid, "make it")
    server.wait_idle(cid)
    result = next(e for e in server.client.get(f"/api/conversations/{cid}/events").json()["items"]
                  if e["kind"] == "tool_result")
    given, = result["presented_files"]
    assert (given["name"], given["version"], given["origin"]) == ("out.csv", 1, "presented")
    assert server.client.get(f"/api/conversations/{cid}/files/{given['file_id']}/1").content == b"a,b\n"


def test_the_error_table(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    cid, other = server.new(), server.new()
    assert error(server.upload(cid, "", b"a")) == (400, "invalid_name")
    assert error(server.upload(cid, "x" * 1001, b"a")) == (400, "invalid_name")
    assert server.application is not None
    server.application.file_max_bytes = 10
    assert error(server.upload(cid, "big.bin", b"b" * 11)) == (413, "file_too_large")  # declared length
    server.application.file_max_bytes = 32 * 1024 * 1024
    foreign = server.upload(other, "a.txt", b"a").json()["file_id"]
    refused = server.send(cid, "x", foreign)
    assert error(refused) == (400, "attachment_refused") and refused.json()["error"]["file_id"] == foreign
    assert error(server.send(cid, "x", *[f"file_{i}" for i in range(11)])) == (400, "too_many_attachments")
    assert error(server.send(cid, "")) == (400, "invalid_argument")
    assert error(server.client.get(f"/api/conversations/{cid}/files/file_404/1")) == (404, "not_found")
    server.application.uploads_work = False
    assert error(server.upload(cid, "a.txt", b"a")) == (409, "files_unavailable")
    server.application.uploads_work = True
    server.application.data.blobs.configure(max_bytes=5, keep_days=30)
    assert error(server.upload(cid, "full.bin", b"f" * 6)) == (507, "storage_full")
    server.application.data.blobs.configure(max_bytes=1 << 30, keep_days=30)
    up = server.upload(cid, "gone.txt", b"g").json()

    async def expired(*args: object) -> object:
        raise DataError(DataErrorCode.BLOB_EXPIRED, "x")

    monkeypatch.setattr(server.application, "download", expired)
    assert error(server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/1")) == (410, "file_expired")


def test_another_users_and_trashed_conversations_files_are_missing(server: Server) -> None:
    cid = server.new()
    up = server.upload(cid, "a.txt", b"a").json()
    server.client.post(f"/api/conversations/{cid}/trash", headers={"origin": ORIGIN})
    for response in (server.client.get(f"/api/conversations/{cid}/files/{up['file_id']}/1"),
                     server.client.get(f"/api/conversations/{cid}/files"), server.upload(cid, "b.txt", b"b")):
        assert error(response) == (404, "not_found")


def test_uploads_need_the_page_origin(server: Server) -> None:
    cid = server.new()
    response = server.client.post(f"/api/conversations/{cid}/uploads", params={"name": "a.txt"}, content=b"a",
                                  headers={"origin": "http://evil.example"})
    assert response.status_code == 403


def test_the_general_body_limit_still_holds_elsewhere(server: Server) -> None:
    cid = server.new()
    response = server.client.post(f"/api/conversations/{cid}/messages", content=b"{" + b" " * 300_000 + b"}",
                                  headers={"origin": ORIGIN, "content-type": "application/json"})
    assert error(response) == (413, "request_too_large")


def test_file_settings_are_configured() -> None:
    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    config = WebConfig.from_env(base)
    assert (config.file_max_bytes, config.file_store_max_bytes, config.file_keep_days) == (
        32 * 1024 * 1024, 1024 * 1024 * 1024, 30)
    custom = WebConfig.from_env({**base, "HIMARI_FILE_MAX_BYTES": "1000", "HIMARI_FILE_STORE_MAX_BYTES": "5000",
                                 "HIMARI_FILE_KEEP_DAYS": "7"})
    assert (custom.file_max_bytes, custom.file_store_max_bytes, custom.file_keep_days) == (1000, 5000, 7)
    for env in ({"HIMARI_FILE_KEEP_DAYS": "0"}, {"HIMARI_FILE_KEEP_DAYS": "366"}, {"HIMARI_FILE_MAX_BYTES": "0"},
                {"HIMARI_FILE_MAX_BYTES": "9", "HIMARI_FILE_STORE_MAX_BYTES": "8"}, {"HIMARI_FILE_MAX_BYTES": "lots"}):
        with pytest.raises(WebConfigError):
            WebConfig.from_env({**base, **env})


def test_a_chunked_upload_is_cut_at_the_limit(server: Server) -> None:
    cid = server.new()
    assert server.application is not None
    server.application.file_max_bytes = 1000

    def body() -> Iterator[bytes]:
        for _ in range(5):
            yield b"c" * 300  # no Content-Length: the route still counts

    response = server.client.post(f"/api/conversations/{cid}/uploads", params={"name": "big.bin"}, content=body(),
                                  headers={"origin": ORIGIN, "content-type": "application/octet-stream"})
    assert error(response) == (413, "file_too_large")
    assert server.client.get(f"/api/conversations/{cid}/files").json()["items"] == []


@pytest.mark.parametrize("path, method", [("/uploads/", "POST"), ("//uploads", "POST"), ("/uploads", "PUT")])
def test_only_the_upload_route_is_outside_the_body_limit(server: Server, path: str, method: str) -> None:
    cid = server.new()
    response = server.client.request(method, f"/api/conversations/{cid}{path}", content=b"x" * 300_000,
                                     headers={"origin": ORIGIN})
    assert error(response) == (413, "request_too_large")


def test_a_huge_version_is_refused_not_a_crash(server: Server) -> None:
    cid = server.new()
    response = server.client.get(f"/api/conversations/{cid}/files/file_1/99999999999999999999999")
    assert error(response) == (400, "invalid_request")  # PR #60 review C2
    assert error(server.client.get(f"/api/conversations/{cid}/files/file_1/0")) == (400, "invalid_request")


@pytest.mark.parametrize("raised, expected", [
    ("too_many", (409, "too_many_pending")),
    ("busy", (409, "workspace_busy")),
    ("folder", (409, "uploads_folder_unusable")),
    ("missing", (410, "file_missing")),
])
def test_more_of_the_error_table(server: Server, monkeypatch: pytest.MonkeyPatch, raised: str,
                                 expected: tuple[int, str]) -> None:
    from himari.bootstrap.application import UploadsFolderUnusable, WorkspaceBusy
    cid = server.new()
    up = server.upload(cid, "a.txt", b"a").json()
    assert server.application is not None
    errors = {"too_many": DataError(DataErrorCode.LIMIT_EXCEEDED, "x"), "busy": WorkspaceBusy("ws"),
              "folder": UploadsFolderUnusable("/workspace/x/uploads"), "missing": DataError(DataErrorCode.BLOB_MISSING, "k")}

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise errors[raised]

    monkeypatch.setattr(server.application, "send", refuse)
    assert error(server.send(cid, "x", up["file_id"])) == expected


def test_an_upload_whose_bytes_went_meanwhile_says_upload_again(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = server.new()
    assert server.application is not None

    async def swept(*args: Any, **kwargs: Any) -> Any:
        raise DataError(DataErrorCode.BLOB_MISSING, "k")

    monkeypatch.setattr(server.application, "upload", swept)
    assert error(server.upload(cid, "a.txt", b"a")) == (503, "upload_interrupted")


def test_an_attachment_without_a_readable_record_shows_as_expired() -> None:
    from himari.common import AttachmentRef, ConversationId, EventId, FileId, TextBlock
    from himari.data.api import ConversationEvent, EventKind, UserMessage
    from datetime import UTC, datetime
    event = ConversationEvent(EventId("e"), ConversationId("c"), 1, EventKind.USER_MESSAGE,
                              UserMessage((TextBlock("t"), AttachmentRef(FileId("file_9"), 1))), 1, datetime.now(UTC))
    view = web.event_view(event)
    assert view["text"] == "t" and view["attachments"] == [
        {"file_id": "file_9", "version": 1, "name": None, "expired": True, "image": None}]
