"""The web's file explorer over the API (V3-P75 §5): a real runner (fake sandbox) on a temporary home."""

from __future__ import annotations

import asyncio
import hashlib
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
from testkit import final  # noqa: E402
from webkit import SEND, Server  # noqa: E402

from himari.bootstrap import explorer  # noqa: E402
from himari.common import FileReference  # noqa: E402
from himari.data.api import PreparedView, sniff_image  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.prompt.api import FILE_REFERENCE_NOTE, UserTurn  # noqa: E402


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
    # The identity as decimal strings: an mtime in nanoseconds is past what a JavaScript number keeps exactly.
    identity = [str(info.st_dev), str(info.st_ino), "13", str(info.st_mtime_ns)]
    assert info.st_mtime_ns > 2 ** 53
    assert shown == {"path": str(kit.home / "projects/a/main.c"), "size": 13, "kind": "text",
                     "text": "int main(){}\n", "truncated": False, "editable": True,
                     "identity": identity, "modified": int(info.st_mtime)}
    big = kit.home / "projects/big.txt"
    big.write_text("가" * 400_000)  # 1.2 MB of three-byte characters
    shown = server.client.get("/api/machine/file", params={"path": str(big)}).json()
    assert shown["kind"] == "text" and shown["truncated"] and not shown["editable"]
    assert set(shown["text"]) == {"가"} and len(shown["text"].encode()) <= 256 * 1024
    (kit.home / "projects/blob").write_bytes(b"abc\0def")
    (kit.home / "projects/latin").write_bytes("café".encode("latin-1"))
    for name in ("blob", "latin"):
        assert server.client.get("/api/machine/file", params={"path": f"~/projects/{name}"}).json()["kind"] == "binary"
    # V3-P77 §3.1: a PDF by its header, whatever its name, all-ASCII ones included; only at its start
    (kit.home / "projects/scan").write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    (kit.home / "projects/plain.pdf").write_text("%PDF-1.4\n1 0 obj << >> endobj\n")
    (kit.home / "projects/about.txt").write_text("A PDF starts with %PDF-1.7\n")
    kinds = {name: server.client.get("/api/machine/file", params={"path": f"~/projects/{name}"}).json()
             for name in ("scan", "plain.pdf", "about.txt")}
    assert kinds["scan"]["kind"] == kinds["plain.pdf"]["kind"] == "pdf"
    assert kinds["scan"]["text"] is None and not kinds["plain.pdf"]["editable"]
    assert kinds["about.txt"]["kind"] == "text"
    stat = server.client.get("/api/machine/stat", params={"path": "~/projects/a/main.c"}).json()
    assert stat["identity"] == identity and stat["kind"] == "file"


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


# --- the editor's writes (V3-P75 §7) -----------------------------------------------------------------

def put(server: Server, body: dict[str, Any]) -> Any:
    return server.client.put("/api/machine/file", json=body, headers=SEND)


def test_save_keeps_the_previous_version_and_refuses_a_stale_one(server: Server, kit: RunnerKit) -> None:
    path = str(kit.home / "projects/a/main.c")
    shown = server.client.get("/api/machine/file", params={"path": path}).json()
    saved = put(server, {"path": path, "content": "int main(){return 1;}\n", "expected": shown["identity"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["previous_kept"] and (kit.home / "projects/a/main.c").read_text() == "int main(){return 1;}\n"
    assert saved.json()["identity"] == server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    stale = put(server, {"path": path, "content": "lost?", "expected": shown["identity"]})
    refused(stale, 409, "changed")
    assert (kit.home / "projects/a/main.c").read_text() == "int main(){return 1;}\n"
    items = server.client.get("/api/machine/versions", params={"path": path}).json()["items"]
    assert [(v["reason"], v["size"]) for v in items] == [("save", 13)]
    text = server.client.get(f"/api/machine/versions/{items[0]['id']}", params={"path": path}).json()["text"]
    assert text == "int main(){}\n"
    # Put back: a save like any other, which keeps what it replaces.
    put(server, {"path": path, "content": text, "expected": saved.json()["identity"]})
    assert (kit.home / "projects/a/main.c").read_text() == "int main(){}\n"
    assert len(server.client.get("/api/machine/versions", params={"path": path}).json()["items"]) == 2


def test_new_file_new_folder_and_delete(server: Server, kit: RunnerKit) -> None:
    made = server.post("/api/machine/file", {"path": "~/projects/a/notes.md"})
    assert made.status_code == 201 and (kit.home / "projects/a/notes.md").read_text() == ""
    refused(server.post("/api/machine/file", {"path": "~/projects/a/main.c"}), 409, "exists")
    assert (kit.home / "projects/a/main.c").read_text() == "int main(){}\n"  # never over a file
    folder = server.post("/api/machine/folder", {"path": "~/projects/a/docs"})
    assert folder.status_code == 201 and (kit.home / "projects/a/docs").is_dir()
    refused(server.post("/api/machine/folder", {"path": "~/projects/a/docs"}), 409, "exists")
    refused(server.post("/api/machine/folder", {"path": "~/projects/none/deeper"}), 404, "not_found")
    path = str(kit.home / "projects/a/main.c")
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    refused(server.post("/api/machine/file/delete", {"path": path, "expected": [0, 0, 0, 0]}), 409, "changed")
    gone = server.post("/api/machine/file/delete", {"path": path, "expected": identity})
    assert gone.status_code == 200 and gone.json()["previous_kept"] and not (kit.home / "projects/a/main.c").exists()
    items = server.client.get("/api/machine/versions", params={"path": path}).json()["items"]
    assert [v["reason"] for v in items] == ["delete"]
    # Anywhere the user can write — not only the conversations' workspaces (§7.1).
    assert server.post("/api/machine/file", {"path": "~/elsewhere.txt"}).status_code == 201


def test_hidden_paths_are_not_written(server: Server, kit: RunnerKit) -> None:
    for path in ("~/.ssh/new", "~/.bashrc2", str(kit.himari_home.parent / "secret-root" / "x")):
        refused(server.post("/api/machine/file", {"path": path}), 404, "not_found")
    identity = server.client.get("/api/machine/stat", params={"path": "~/.gitconfig"}).json()["identity"]
    assert server.client.get("/api/machine/stat", params={"path": "~/.ssh/id"}).status_code == 404
    refused(put(server, {"path": "~/.ssh/id", "content": "x", "expected": identity}), 404, "not_found")
    refused(server.post("/api/machine/file/delete", {"path": "~/.ssh/id", "expected": identity}), 404, "not_found")
    assert (kit.home / ".ssh/id").read_text() == "secret"


def test_save_limits(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(kit.home / "projects/a/main.c")
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    refused(put(server, {"path": path, "content": "x" * (1024 * 1024 + 1), "expected": identity}), 422, "too_large")
    refused(put(server, {"path": path, "content": "x" * (4 * 1024 * 1024), "expected": identity}), 413,
            "request_too_large")
    assert put(server, {"path": path, "content": "가" * 300_000, "expected": identity}).status_code == 200  # 900 KB
    assert server.application is not None
    explorer = server.application.explorer(server.application.machine_principals.__iter__().__next__())
    assert explorer.versions is not None
    monkeypatch.setattr(explorer.versions, "ready_for", lambda size: False)
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    refused(put(server, {"path": path, "content": "y", "expected": identity}), 507, "versions_unavailable")
    assert (kit.home / "projects/a/main.c").read_text() == "가" * 300_000  # nothing saved


def test_writes_not_offered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, None)
    with s.client:
        s.sign_in()
        refused(s.post("/api/machine/file", {"path": "/tmp/x"}), 404, "not_found")
        refused(s.client.put("/api/machine/file", json={"path": "/tmp/x", "content": "", "expected": [1, 2, 3, 4]},
                             headers=SEND), 404, "not_found")


def test_previous_versions_cannot_be_lost(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #94 review B1, B2: a file over 1 MiB is not overwritten (none of it could be kept); a delete waits
    for a store that can keep it, except a file over 1 MiB, which goes without (after the editor warns)."""
    big = kit.home / "projects/a/big.log"
    big.write_bytes(b"x" * (1024 * 1024 + 10))
    identity = server.client.get("/api/machine/stat", params={"path": str(big)}).json()["identity"]
    refused(put(server, {"path": str(big), "content": "short", "expected": identity}), 422, "too_large")
    assert big.stat().st_size == 1024 * 1024 + 10
    assert server.application is not None
    explorer = server.application.explorer(next(iter(server.application.machine_principals)))
    assert explorer.versions is not None
    monkeypatch.setattr(explorer.versions, "ready_for", lambda size: False)
    small = str(kit.home / "projects/a/main.c")
    small_identity = server.client.get("/api/machine/stat", params={"path": small}).json()["identity"]
    refused(server.post("/api/machine/file/delete", {"path": small, "expected": small_identity}), 507,
            "versions_unavailable")
    assert (kit.home / "projects/a/main.c").exists()
    gone = server.post("/api/machine/file/delete", {"path": str(big), "expected": identity})
    assert gone.status_code == 200 and not gone.json()["previous_kept"] and not big.exists()


def test_versions_follow_the_hidden_list_as_it_is_now(server: Server, kit: RunnerKit,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #94 review B3: a path hidden after it was saved shows no versions; a deleted file's still do."""
    notes = kit.home / "projects/notes"
    notes.mkdir()
    (notes / "a.txt").write_text("one")
    path = str(notes / "a.txt")
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    assert put(server, {"path": path, "content": "two", "expected": identity}).status_code == 200
    items = server.client.get("/api/machine/versions", params={"path": path}).json()["items"]
    assert len(items) == 1
    (notes / "a.txt").unlink()  # gone: its versions are still there to read
    assert server.client.get("/api/machine/versions", params={"path": path}).json()["items"] == items
    from himari.runner.hidden import HiddenRules

    by_rule = HiddenRules.by_rule
    monkeypatch.setattr(HiddenRules, "by_rule", lambda self, real: real.startswith(str(notes)) or by_rule(self, real))
    refused(server.client.get("/api/machine/versions", params={"path": path}), 404, "not_found")
    refused(server.client.get(f"/api/machine/versions/{items[0]['id']}", params={"path": path}), 404, "not_found")



def test_a_deleted_file_is_found_in_its_folder(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #95 review B3: listed for its folder while its last kept version is its delete; never a hidden one."""
    folder = kit.home / "projects/a"
    path = str(folder / "main.c")
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    assert server.post("/api/machine/file/delete", {"path": path, "expected": identity}).status_code == 200
    items = server.client.get("/api/machine/versions/deleted", params={"path": "~/projects/a"}).json()["items"]
    assert [(i["path"], i["name"], i["size"]) for i in items] == [(path, "main.c", 13)]
    text = server.client.get(f"/api/machine/versions/{items[0]['id']}", params={"path": path}).json()["text"]
    assert text == "int main(){}\n"
    refused(server.client.get("/api/machine/versions/deleted", params={"path": path}), 404, "not_found")  # gone file
    from himari.runner.hidden import HiddenRules

    by_rule = HiddenRules.by_rule
    monkeypatch.setattr(HiddenRules, "by_rule", lambda self, real: real == path or by_rule(self, real))
    assert server.client.get("/api/machine/versions/deleted", params={"path": str(folder)}).json()["items"] == []


# --- references: Ask HIMARI (V3-P76 §3) ---------------------------------------------------------------

NOTES = "alpha\n가나 😀 beta\ngamma\n"


def selection(text: str, part: str) -> dict[str, Any]:
    """What the browser sends for ``part`` of ``text``: UTF-16 positions and the hash of its UTF-8."""
    start = len(text[:text.index(part)].encode("utf-16-le")) // 2
    return {"start": start, "end": start + len(part.encode("utf-16-le")) // 2,
            "sha256": hashlib.sha256(part.encode()).hexdigest()}


def ask(server: Server, cid: str, text: str, *references: dict[str, Any]) -> Any:
    return server.post(f"/api/conversations/{cid}/messages", {"text": text, "references": list(references)})


def wait_idle(server: Server, cid: str) -> None:
    for _ in range(300):
        if not server.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def model_saw(server: Server) -> list[FileReference]:
    """The file references of the last request's newest message, as the model's input holds them."""
    model_input = server.provider.prepared_inputs[-1]
    blocks = [b for part in model_input.conversation if isinstance(part, UserTurn) for b in part.blocks]
    return [b for b in blocks if isinstance(b, FileReference)]


def test_a_selection_is_rebuilt_from_the_file_and_sent(server: Server, kit: RunnerKit) -> None:
    path = str(kit.home / "projects/a/notes.txt")
    (kit.home / "projects/a/notes.txt").write_text(NOTES)
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    cid = server.new()
    server.provider.extend([final("seen")])
    sent = ask(server, cid, "what is this?", {"path": path, "identity": identity, **selection(NOTES, "가나 😀 beta\nga")})
    assert sent.status_code == 202, sent.text
    wait_idle(server, cid)
    first = server.client.get(f"/api/conversations/{cid}/events").json()["items"][0]
    assert first["text"] == "what is this?"
    assert first["references"] == [{"path": path, "name": "notes.txt", "whole": False, "start_line": 2, "end_line": 3}]
    [seen] = model_saw(server)
    assert (seen.path, seen.excerpt, seen.start_line, seen.end_line, seen.truncated) == (
        path, "가나 😀 beta\nga", 2, 3, False)
    assert FILE_REFERENCE_NOTE in "\n".join(server.provider.prepared_inputs[-1].stable_prefix)
    # A whole file: named, with no excerpt; a message may be references only.
    server.provider.extend([final("seen")])
    whole = ask(server, cid, "", {"path": "~/projects/a/notes.txt", "identity": identity})
    assert whole.status_code == 202, whole.text
    wait_idle(server, cid)
    last = server.client.get(f"/api/conversations/{cid}/events").json()["items"][-2]
    assert last["text"] == "" and last["references"] == [
        {"path": path, "name": "notes.txt", "whole": True, "start_line": None, "end_line": None}]
    whole_seen = model_saw(server)[-1]
    assert (whole_seen.start, whole_seen.excerpt, whole_seen.size) == (None, "", len(NOTES.encode()))


def test_a_stale_reference_refuses_the_message(server: Server, kit: RunnerKit) -> None:
    path = str(kit.home / "projects/a/notes.txt")
    (kit.home / "projects/a/notes.txt").write_text(NOTES)
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    cid = server.new()
    wrong = {**selection(NOTES, "beta"), "sha256": hashlib.sha256(b"other").hexdigest()}
    refused(ask(server, cid, "x", {"path": path, "identity": identity, **wrong}), 409, "reference_stale")
    split = {**selection(NOTES, "😀"), "end": selection(NOTES, "😀")["start"] + 1}  # half a surrogate pair
    refused(ask(server, cid, "x", {"path": path, "identity": identity, **split}), 409, "reference_stale")
    past = {"start": len(NOTES) + 5, "end": len(NOTES) + 9, "sha256": hashlib.sha256(b"").hexdigest()}
    refused(ask(server, cid, "x", {"path": path, "identity": identity, **past}), 409, "reference_stale")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))  # changed since the selection
    refused(ask(server, cid, "x", {"path": path, "identity": identity, **selection(NOTES, "beta")}), 409,
            "reference_stale")
    assert server.client.get(f"/api/conversations/{cid}/events").json()["items"] == []  # nothing was sent
    assert server.provider.sent == []


def test_line_breaks_count_as_the_editor_counts_them(server: Server, kit: RunnerKit) -> None:
    """CodeMirror makes every \\r\\n and \\r one \\n: its positions and text are of that text."""
    path = kit.home / "projects/a/dos.txt"
    path.write_bytes(b"one\r\ntwo\rthree\nfour\r\n")
    identity = server.client.get("/api/machine/stat", params={"path": str(path)}).json()["identity"]
    cid = server.new()
    server.provider.extend([final("seen")])
    sent = ask(server, cid, "x", {"path": str(path), "identity": identity,
                                  **selection("one\ntwo\nthree\nfour\n", "two\nthree")})
    assert sent.status_code == 202, sent.text
    wait_idle(server, cid)
    reference = model_saw(server)[-1]
    assert (reference.excerpt, reference.start_line, reference.end_line) == ("two\nthree", 2, 3)


@pytest.mark.parametrize(("text", "start", "end", "lines"), [
    ("a\nb\n", 0, 4, (1, 2)),  # to the end of a file ending in a line break: line 3 has nothing selected
    ("a\nb\nc", 2, 4, (2, 2)),  # one line with its break
    ("a\n\n\nb", 0, 4, (1, 3)),  # the empty lines 2 and 3 are selected, line 4 is not
    ("a\n\nb", 2, 3, (2, 2)),  # an empty line alone
    ("a\nb", 1, 2, (1, 1)),  # only the break after line 1
    ("a\nb\n\n", 2, 5, (2, 3)),  # from line 2 to the file's end, past an empty line
])
def test_a_selection_ends_on_the_line_of_its_last_character(server: Server, kit: RunnerKit, text: str, start: int,
                                                            end: int, lines: tuple[int, int]) -> None:
    path = kit.home / "projects/a/lines.txt"
    path.write_text(text)
    identity = server.client.get("/api/machine/stat", params={"path": str(path)}).json()["identity"]
    cid = server.new()
    server.provider.extend([final("seen")])
    sent = ask(server, cid, "x", {"path": str(path), "identity": identity, "start": start, "end": end,
                                  "sha256": hashlib.sha256(text[start:end].encode()).hexdigest()})
    assert sent.status_code == 202, sent.text
    wait_idle(server, cid)
    reference = model_saw(server)[-1]
    assert (reference.start_line, reference.end_line) == lines


def test_reference_limits(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(kit.home / "projects/a/notes.txt")
    (kit.home / "projects/a/notes.txt").write_text(NOTES)
    identity = server.client.get("/api/machine/stat", params={"path": path}).json()["identity"]
    cid = server.new()
    whole = {"path": path, "identity": identity}
    refused(ask(server, cid, "x", *[whole] * 11), 400, "invalid_request")  # at most ten
    refused(ask(server, cid, "x", {**whole, "start": 3}), 400, "invalid")  # a start without its end
    refused(ask(server, cid, "x", {"path": "~/.ssh/id", "identity": identity}), 404, "not_found")  # hidden
    big = kit.home / "projects/big.txt"
    big.write_text("x" * (explorer.EDIT_MAX_BYTES + 1))
    big_identity = server.client.get("/api/machine/stat", params={"path": str(big)}).json()["identity"]
    part = {"start": 0, "end": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    refused(ask(server, cid, "x", {"path": str(big), "identity": big_identity, **part}), 422, "too_large")
    # 300 KB selected: the request stays small (no text in it); the model gets 20 KB of it.
    wide = kit.home / "projects/wide.txt"
    wide.write_text("가" * 100_000)
    wide_identity = server.client.get("/api/machine/stat", params={"path": str(wide)}).json()["identity"]
    body = {"path": str(wide), "identity": wide_identity, **selection("가" * 100_000, "가" * 100_000)}
    assert len(str(body)) < 300
    server.provider.extend([final("seen")])
    assert ask(server, cid, "x", body).status_code == 202
    wait_idle(server, cid)
    cut = model_saw(server)[-1]
    assert cut.truncated and cut.excerpt == "가" * (explorer.EXCERPT_BYTES // 3)
    # The excerpt is cut at a character boundary, and says so.
    monkeypatch.setattr(explorer, "EXCERPT_BYTES", 8)
    server.provider.extend([final("seen")])
    sent = ask(server, cid, "x", {**whole, **selection(NOTES, "가나 😀 beta")})
    assert sent.status_code == 202, sent.text
    wait_idle(server, cid)
    reference = model_saw(server)[-1]
    assert reference.truncated and reference.excerpt == "가나 " and reference.start_line == 2
