"""The web's file explorer over the API (V3-P75 §5): a real runner (fake sandbox) on a temporary home."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from runnerkit import RunnerKit, start_runner  # noqa: E402
from webkit import SEND, Server  # noqa: E402

from himari.bootstrap import explorer  # noqa: E402
from himari.data.api import PreparedView, sniff_image  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402


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


def refused(response: object, status: int, code: str) -> None:
    assert getattr(response, "status_code") == status, getattr(response, "text")
    assert getattr(response, "json")()["error"]["code"] == code


def test_a_folder_page_and_what_it_is(server: Server, kit: RunnerKit) -> None:
    listing = server.client.get("/api/machine/list").json()  # the home by default
    names = [e["name"] for e in listing["entries"]]
    assert listing["path"] == str(kit.home) and listing["parent"] == str(kit.home.parent) and listing["next"] is None
    assert ".ssh" not in names and ".gitconfig" in names and names.index("projects") < names.index(".gitconfig")
    assert listing["workspace"] is None
    server.post("/api/workspaces", {"path": "~/projects/a"})
    inside = server.client.get("/api/machine/list", params={"path": "~/projects/a"}).json()
    assert inside["workspace"]["label"] == "a" and inside["entries"][0]["name"] == "main.c"
    for i in range(600):
        (kit.home / "projects" / f"f{i:03d}").write_text("")
    first = server.client.get("/api/machine/list", params={"path": "~/projects"}).json()
    second = server.client.get("/api/machine/list", params={"path": "~/projects", "after": first["next"]}).json()
    assert len(first["entries"]) == 500 and second["next"] is None
    assert [e["name"] for e in first["entries"] + second["entries"]] == ["a", *[f"f{i:03d}" for i in range(600)]]
    assert server.client.get("/api/machine/list", params={"path": "/"}).json()["parent"] is None


def test_refusals(server: Server, kit: RunnerKit) -> None:
    refused(server.client.get("/api/machine/list", params={"path": "~/.ssh"}), 404, "not_found")
    refused(server.client.get("/api/machine/file", params={"path": "~/.ssh/id"}), 404, "not_found")
    refused(server.client.get("/api/machine/download", params={"path": "~/.ssh/id"}), 404, "not_found")
    refused(server.client.get("/api/machine/list", params={"path": "projects"}), 400, "invalid")
    refused(server.client.get("/api/machine/file", params={"path": "~/projects"}), 400, "is_directory")
    refused(server.client.get("/api/machine/list", params={"path": "~/projects/a/main.c"}), 400, "not_directory")
    refused(server.client.get("/api/machine/list", params={"path": "~", "after": "x"}), 400, "invalid")
    refused(server.client.get("/api/machine/image", params={"path": "~/projects/a/main.c"}), 503, "unavailable")


def test_files_are_shown_by_kind(server: Server, kit: RunnerKit) -> None:
    shown = server.client.get("/api/machine/file", params={"path": "~/projects/a/main.c"}).json()
    info = (kit.home / "projects/a/main.c").stat()
    assert shown == {"path": str(kit.home / "projects/a/main.c"), "size": 13, "kind": "text",
                     "text": "int main(){}\n", "truncated": False, "editable": True,
                     "identity": [info.st_dev, info.st_ino, 13, info.st_mtime_ns], "modified": int(info.st_mtime)}
    big = kit.home / "projects/big.txt"
    big.write_text("가" * 400_000)  # 1.2 MB of three-byte characters
    shown = server.client.get("/api/machine/file", params={"path": str(big)}).json()
    assert shown["kind"] == "text" and shown["truncated"] and not shown["editable"]
    assert set(shown["text"]) == {"가"} and len(shown["text"].encode()) <= 256 * 1024
    (kit.home / "projects/blob").write_bytes(b"abc\0def")
    (kit.home / "projects/latin").write_bytes("café".encode("latin-1"))
    for name in ("blob", "latin"):
        assert server.client.get("/api/machine/file", params={"path": f"~/projects/{name}"}).json()["kind"] == "binary"
    stat = server.client.get("/api/machine/stat", params={"path": "~/projects/a/main.c"}).json()
    assert stat["identity"] == [info.st_dev, info.st_ino, 13, info.st_mtime_ns] and stat["kind"] == "file"


def test_a_download_is_an_attachment_never_rendered(server: Server, kit: RunnerKit) -> None:
    (kit.home / "projects/page.html").write_text("<script>alert(1)</script>")
    got = server.client.get("/api/machine/download", params={"path": "~/projects/page.html"})
    assert got.status_code == 200 and got.content == b"<script>alert(1)</script>"
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment") and "page.html" in got.headers["content-disposition"]
    assert got.headers["content-security-policy"].startswith("sandbox,")  # its own policy, then the page's
    assert got.headers["x-content-type-options"] == "nosniff"


class FakeViews:
    """The image sandbox's part: a copy that is a different, smaller PNG."""

    def __init__(self, copy: bytes) -> None:
        self.copy = copy
        self.made: list[int] = []

    @property
    def available(self) -> bool:
        return True

    async def make(self, original: bytes) -> PreparedView | str:
        self.made.append(len(original))
        found = sniff_image(self.copy)
        assert found is not None
        return PreparedView(self.copy, found.media_type, found.width, found.height)


def png(width: int, height: int) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 10, 10)).save(out, "PNG")
    return out.getvalue()


def test_an_image_is_shown_as_the_sandbox_copy(server: Server, kit: RunnerKit) -> None:
    pytest.importorskip("PIL")
    assert server.application is not None
    views = FakeViews(png(2, 2))
    server.application.views = views
    server.application._explorer = None  # made again with these views
    (kit.home / "projects/photo.png").write_bytes(png(40, 30))
    assert server.client.get("/api/machine/file", params={"path": "~/projects/photo.png"}).json()["kind"] == "image"
    got = server.client.get("/api/machine/image", params={"path": "~/projects/photo.png"})
    assert got.status_code == 200 and got.content == views.copy and got.headers["content-type"] == "image/png"
    assert got.headers["content-security-policy"].startswith("default-src 'none'; sandbox,")
    server.client.get("/api/machine/image", params={"path": "~/projects/photo.png"})
    assert len(views.made) == 1  # the same file again: the copy kept
    (kit.home / "projects/fake.png").write_text("<svg onload=alert(1)>")
    refused(server.client.get("/api/machine/image", params={"path": "~/projects/fake.png"}), 415, "not_regular")


def test_not_offered_to_this_principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, None)
    with s.client:
        s.sign_in()
        for path in ("/api/machine/list", "/api/machine/file?path=/etc/hostname", "/api/machine/download?path=/etc/hostname"):
            refused(s.client.get(path), 404, "not_found")
        assert s.client.get("/api/machine/list", headers=SEND).status_code == 404


def test_a_slow_read_is_given_up_and_its_fd_closed_after(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #92 review B1, N1: reading an opened file that hangs answers 504 in time; the fd stays open until
    the read itself has ended (never closed under it), and further reads are refused while it waits."""
    monkeypatch.setattr(explorer, "READ_SECONDS", 0.3)
    gate, seen = threading.Event(), []

    def stuck(fd: int, size: int) -> bytes:
        seen.append(fd)
        gate.wait(10)
        return os.pread(fd, size, 0)

    monkeypatch.setattr(explorer, "_pread", stuck)
    try:
        for _ in range(explorer.MAX_READS):
            refused(server.client.get("/api/machine/file", params={"path": "~/projects/a/main.c"}), 504, "timed_out")
        assert all(os.fstat(fd) for fd in seen)  # still open: the reads are still under way
        busy = server.client.get("/api/machine/file", params={"path": "~/projects/a/main.c"})
        refused(busy, 504, "timed_out")
        assert "Earlier slow reads" in busy.json()["error"]["message"]
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while any(_open(fd) for fd in seen) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_open(fd) for fd in seen)  # each closed once its read ended
    monkeypatch.setattr(explorer, "_pread", lambda fd, size: os.pread(fd, size, 0))
    assert server.client.get("/api/machine/file", params={"path": "~/projects/a/main.c"}).json()["text"]


def _open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


# --- a download's file is closed however the response ends (PR #92 re-review C1) ---------------------

class Handle:
    """A file whose reads can be held; it records whether it was closed under a read."""

    def __init__(self, data: bytes, gate: threading.Event | None = None) -> None:
        self.data, self.gate = data, gate
        self.reading = False
        self.closed = False
        self.closed_while_reading = False

    def read(self, size: int) -> bytes:
        self.reading = True
        try:
            if self.gate is not None:
                self.gate.wait(10)
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk
        finally:
            self.reading = False

    def close(self) -> None:
        self.closed_while_reading |= self.reading
        self.closed = True


def stream(handle: Handle, receive: Any, send: Any) -> None:
    response = web._FileStream(handle, media_type="application/octet-stream")  # type: ignore[arg-type]
    scope = {"type": "http", "asgi": {"spec_version": "2.3"}}

    async def go() -> None:
        try:
            await response(scope, receive, send)
        except Exception:  # noqa: BLE001 — a client gone: what matters is the file
            pass

    asyncio.run(go())


def test_a_whole_download_closes_its_file() -> None:
    handle, sent = Handle(b"x" * 200_000), []

    async def receive() -> dict[str, str]:
        await asyncio.Event().wait()
        return {}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    stream(handle, receive, send)
    assert b"".join(m.get("body", b"") for m in sent) == b"x" * 200_000 and handle.closed


def test_a_download_cut_before_its_first_chunk_closes_its_file() -> None:
    handle = Handle(b"x" * 10)

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}  # gone at once

    async def send(message: dict[str, Any]) -> None:
        await asyncio.sleep(1)  # the start never goes out before the disconnect is seen

    stream(handle, receive, send)
    assert handle.closed and not handle.closed_while_reading


def test_a_download_cut_during_a_read_closes_its_file_after_that_read() -> None:
    gate = threading.Event()
    handle = Handle(b"x" * 200_000, gate)

    async def receive() -> dict[str, str]:
        await asyncio.sleep(0.2)  # while the first read is held
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        pass

    releaser = threading.Timer(0.6, gate.set)
    releaser.start()
    stream(handle, receive, send)  # returns with the read still held …
    assert not handle.closed_while_reading
    releaser.join()
    deadline = time.monotonic() + 5
    while not handle.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert handle.closed and not handle.closed_while_reading  # … closed once it ended


# --- a conversation in a folder (V3-P75 §6) ----------------------------------------------------------

def folder_conversations(server: Server, path: str) -> dict[str, Any]:
    got = server.client.get("/api/machine/conversations", params={"path": path})
    assert got.status_code == 200, got.text
    return dict(got.json())


def test_open_a_folders_conversation_or_a_new_one(server: Server, kit: RunnerKit) -> None:
    project = str(kit.home / "projects/a")
    empty = folder_conversations(server, "~/projects/a")
    assert (empty["path"], empty["workspace"], empty["conversations"], empty["refused"]) == (project, None, [], None)
    made = server.post("/api/machine/conversations", {"path": "~/projects/a"})
    assert made.status_code == 201 and made.json()["created"]
    first = made.json()["conversation"]["id"]
    folders = server.client.get(f"/api/conversations/{first}/workspaces").json()["items"]
    working = next(f for f in folders if f["working"])
    assert working["path"] == project and not working["own"]  # registered, attached, the working folder
    again = server.post("/api/machine/conversations", {"path": project})
    assert again.status_code == 200 and not again.json()["created"] and again.json()["conversation"]["id"] == first
    second = server.post("/api/machine/conversations", {"path": project, "new": True}).json()["conversation"]["id"]
    listed = folder_conversations(server, project)
    assert [c["id"] for c in listed["conversations"]] == [second, first] and listed["workspace"]["label"] == "a"
    assert server.post("/api/machine/conversations", {"path": project}).json()["conversation"]["id"] == second
    assert len(server.client.get("/api/workspaces").json()["items"]) == 1  # registered once, reused
    # Below it: its own list, and a link to the folder above (Q2).
    (kit.home / "projects/a/src").mkdir()
    below = folder_conversations(server, "~/projects/a/src")
    assert below["conversations"] == [] and [(x["workspace"]["path"], x["count"]) for x in below["above"]] == [
        (project, 2)]
    # Moved to the trash: no longer one of the folder's.
    assert server.client.post(f"/api/conversations/{second}/trash", headers=SEND).status_code in (200, 202)
    assert [c["id"] for c in folder_conversations(server, project)["conversations"]] == [first]


def test_a_conversations_own_folder_opens_that_conversation(server: Server, kit: RunnerKit) -> None:
    cid = server.new()
    folders = server.client.get(f"/api/conversations/{cid}/workspaces").json()["items"]
    own = next(f for f in folders if f["own"])["path"]
    listed = folder_conversations(server, own)
    assert listed["own"] and [c["id"] for c in listed["conversations"]] == [cid]
    opened = server.post("/api/machine/conversations", {"path": own})
    assert opened.status_code == 200 and opened.json()["conversation"]["id"] == cid
    refused(server.post("/api/machine/conversations", {"path": own, "new": True}), 422, "not_usable")


def test_folders_that_cannot_hold_a_conversation(server: Server, kit: RunnerKit, tmp_path: Path) -> None:
    refused(server.client.get("/api/machine/conversations", params={"path": "~/.ssh"}), 422, "not_usable")
    refused(server.post("/api/machine/conversations", {"path": "~/projects/a/main.c"}), 422, "not_usable")
    locked = kit.home / "projects/locked"
    locked.mkdir()
    locked.chmod(0o555)  # readable, not writable: it cannot be a workspace
    try:
        listed = folder_conversations(server, str(locked))
        assert listed["refused"] and listed["conversations"] == []
        refused(server.post("/api/machine/conversations", {"path": str(locked)}), 422, "not_usable")
    finally:
        locked.chmod(0o755)
    before = server.client.get("/api/conversations").json()["items"]
    assert before == []  # nothing was made by the refusals


def test_not_offered_folder_conversations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, None)
    with s.client:
        s.sign_in()
        refused(s.client.get("/api/machine/conversations", params={"path": "/tmp"}), 404, "not_found")
        refused(s.post("/api/machine/conversations", {"path": "/tmp"}), 404, "not_found")


def test_a_conversation_made_for_a_folder_that_could_not_be_attached_is_removed(
        server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.bootstrap.application import Application, WorkspaceRefused

    async def refuse(self: Application, scope: Any, **kwargs: Any) -> Any:
        raise WorkspaceRefused("unavailable", "The machine runner does not answer; try again later.")

    monkeypatch.setattr(Application, "attach_workspace", refuse)
    refused(server.post("/api/machine/conversations", {"path": "~/projects/a"}), 409, "unavailable")
    assert server.client.get("/api/conversations").json()["items"] == []
    assert server.client.get("/api/conversations", params={"trashed": True}).json()["items"] == []
