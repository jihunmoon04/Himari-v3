"""Saving ink into a PDF over the API (V3-P77 §5, §6; V3-P82 §3.6: written in place at its end, no copy and
no previous version): a real runner (fake sandbox) on a temporary home."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from runnerkit import RunnerKit, start_runner  # noqa: E402
from webkit import SEND, Server  # noqa: E402

from himari.bootstrap.explorer import Explorer, increment_ok  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures/pdf/table.pdf"
MiB = 1024 * 1024


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


def refused(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def explorer_of(server: Server) -> Explorer:
    assert server.application is not None
    return server.application.explorer(next(iter(server.application.machine_principals)))


def increment(original: bytes, number: int = 90) -> bytes:
    """A small incremental update after ``original`` (a table PDF): one new object, its xref, the trailer."""
    found = re.findall(rb"startxref\s+(\d+)", original)
    prev = int(found[-1]) if found else 0
    body = f"\n{number} 0 obj\n<< /HimariTest true >>\nendobj\n".encode()
    xref_at = len(original) + len(body)
    table = (f"xref\n{number} 1\n{len(original) + 1:010d} 00000 n\r\ntrailer\n"
             f"<< /Size {number + 1} /Root 1 0 R /Prev {prev} >>\nstartxref\n{xref_at}\n%%EOF\n").encode()
    return body + table


def identity(server: Server, path: str) -> list[str]:
    return list(server.client.get("/api/machine/stat", params={"path": path}).json()["identity"])


def append(server: Server, path: str, data: bytes, expected: list[str] | str,
           content_type: str = "application/octet-stream") -> Any:
    header = expected if isinstance(expected, str) else ",".join(expected)
    return server.client.post("/api/machine/pdf/append", params={"path": path}, content=data,
                              headers={**SEND, "Content-Type": content_type, "X-Himari-Expected": header})


def versions(server: Server, path: str) -> list[dict[str, Any]]:
    return list(server.client.get("/api/machine/versions", params={"path": path}).json()["items"])


def place(kit: RunnerKit, data: bytes, name: str = "notes.pdf") -> tuple[Path, str]:
    target = kit.home / "projects/a" / name
    target.write_bytes(data)
    return target, str(target)


def test_the_increment_check() -> None:
    original = FIXTURE.read_bytes()
    added = increment(original)
    assert increment_ok(len(original), added)
    assert increment_ok(len(original), added + b"\r\n")
    assert not increment_ok(len(original), added[:-7])  # no %%EOF
    assert not increment_ok(len(original), added.replace(b"startxref", b"startxrof"))
    assert not increment_ok(len(original), b"startxref\n10\n%%EOF\n")  # into the original
    assert not increment_ok(len(original), f"startxref\n{len(original) + 999}\n%%EOF\n".encode())  # past the end


def test_ink_is_added_at_the_end_in_place_and_nothing_else_kept(server: Server, kit: RunnerKit) -> None:
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    inode = target.stat().st_ino
    read = server.client.get("/api/machine/download", params={"path": path})
    assert read.content == original
    seen = read.headers["x-himari-identity"].split(",")
    assert seen == identity(server, path)  # the identity of the bytes sent, to save against (§5.3)
    shown = server.client.get("/api/machine/file", params={"path": path}).json()
    assert shown["kind"] == "pdf" and shown["cloud_synced"] is False  # a local disk: saved as written (§3.2)
    added = increment(original)
    saved = append(server, path, added, seen)
    assert saved.status_code == 200, saved.text
    assert target.read_bytes() == original + added  # the original bytes untouched
    assert target.stat().st_ino == inode  # the same file, written in place: no temporary file renamed over it
    assert saved.json() == {"path": path, "identity": identity(server, path)}
    assert versions(server, path) == []  # no copy: the file itself holds what it was (V3-P82 §3.6)
    second = increment(original + added, 91)
    again = append(server, path, second, saved.json()["identity"])
    assert again.status_code == 200 and target.read_bytes() == original + added + second
    assert versions(server, path) == [] and not [n for n in os.listdir(target.parent) if n.startswith(".himari-")]
    # Saved against what was read: changed since, nothing written (ink made on the old bytes).
    refused(append(server, path, increment(original, 92), seen), 409, "changed")
    assert target.read_bytes() == original + added + second


def test_a_save_cut_short_is_taken_off_its_end(server: Server, kit: RunnerKit) -> None:
    """§3.6: a PDF whose last save was cut short (a power cut) is cut back to just after one of its %%EOF —
    only there, only as read."""
    original = FIXTURE.read_bytes()
    target, path = place(kit, original + b"\n12 0 obj << /Half")
    seen = identity(server, path)

    def cut(to: int, expected: list[str]) -> Any:
        return server.client.post("/api/machine/pdf/truncate", params={"path": path, "to": to},
                                  headers={**SEND, "X-Himari-Expected": ",".join(expected)})

    refused(cut(len(original) + 3, seen), 400, "invalid")  # not after a %%EOF
    refused(cut(len(original) + 100, seen), 400, "invalid")  # past its end
    refused(cut(len(original), [seen[0], "999", seen[2], seen[3]]), 409, "changed")
    done = cut(len(original), seen)
    assert done.status_code == 200, done.text
    assert target.read_bytes() == original and done.json()["identity"] == identity(server, path)


def test_a_file_on_a_cloud_mount_is_told(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.2 (Q5): the open's filesystem tells a cloud mount, for every kind of file."""
    from himari.runner import api as runner

    _, path = place(kit, FIXTURE.read_bytes())
    (kit.home / "projects/a/note.txt").write_text("hi\n")
    machine = explorer_of(server)._machine
    real_call = machine.call

    async def on_rclone(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        result, received = await real_call(op, args, fds)
        if op == runner.OPEN:
            result = {**result, "filesystem": "fuse.rclone"}
        return result, received

    monkeypatch.setattr(machine, "call", on_rclone)
    for shown in (path, str(kit.home / "projects/a/note.txt")):
        assert server.client.get("/api/machine/file", params={"path": shown}).json()["cloud_synced"] is True


def test_a_file_with_other_names_or_read_only_is_not_written(server: Server, kit: RunnerKit) -> None:
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    os.link(target, target.parent / "same.pdf")  # a hard link: in place, the other name would change too
    refused(append(server, path, increment(original), identity(server, path)), 400, "links")
    os.unlink(target.parent / "same.pdf")
    if os.geteuid() != 0:
        target.chmod(0o444)
        refused(append(server, path, increment(original), identity(server, path)), 403, "access")
        target.chmod(0o644)
    assert target.read_bytes() == original


def test_a_failed_write_tells_the_file_as_it_is(server: Server, kit: RunnerKit,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.6: the runner cut its bytes back (or could not); the answer carries the file's identity then, to save
    against next, and whether bytes were left."""
    from himari.runner import api as runner

    original = FIXTURE.read_bytes()
    _, path = place(kit, original)
    machine = explorer_of(server)._machine
    real_call = machine.call

    async def failing(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        if op == runner.APPEND:
            raise runner.RunnerError(runner.OTHER, "disk full", identity=[1, 2, 3, 4], partial=True)
        return await real_call(op, args, fds)

    monkeypatch.setattr(machine, "call", failing)
    failed = append(server, path, increment(original), identity(server, path))
    assert failed.status_code == 500
    assert failed.json()["error"] == {"code": "other", "message": "disk full", "identity": ["1", "2", "3", "4"],
                                      "partial": True}


def test_what_is_not_saved(server: Server, kit: RunnerKit) -> None:
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    seen = identity(server, path)
    added = increment(original)
    refused(append(server, path, added, "1,2,3"), 400, "invalid_argument")
    refused(append(server, path, added, seen, "text/plain"), 415, "invalid_argument")
    refused(append(server, path, b"", seen), 400, "invalid")
    refused(append(server, path, added[:-7], seen), 400, "invalid")  # not an update ending with %%EOF
    refused(append(server, path, b"startxref\n10\n%%EOF\n", seen), 400, "invalid")
    text, text_path = place(kit, b"just text, %PDF- later\n" + b"x" * 400, "plain.pdf")
    refused(append(server, text_path, increment(text.read_bytes()), identity(server, text_path)), 415, "not_regular")
    refused(append(server, "~/.ssh/id", added, seen), 404, "not_found")
    # Over 8 MiB of ink is refused at the door; over 64 MiB in all by the explorer, before the file is read.
    refused(append(server, path, b"x" * (8 * MiB + 1), seen), 413, "request_too_large")
    big = [seen[0], seen[1], str(63 * MiB), seen[3]]
    refused(append(server, path, increment(b"x" * 10) + b" " * (2 * MiB), big), 422, "too_large")
    assert target.read_bytes() == original and versions(server, path) == []


def test_an_older_runner_is_told_and_no_store_is_needed(server: Server, kit: RunnerKit,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.6: a runner older than protocol 4 refuses before anything is sent; the PDFs' store is not needed for
    a save any more (nothing is kept)."""
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    explorer = explorer_of(server)

    async def old(fresh: bool = False) -> bool:
        return False

    with monkeypatch.context() as m:
        m.setattr(explorer._machine, "appends", old)
        refused(append(server, path, increment(original), identity(server, path)), 422, "unsupported")
    assert target.read_bytes() == original
    monkeypatch.setattr(explorer, "pdf_versions", None)
    saved = append(server, path, increment(original), identity(server, path))
    assert saved.status_code == 200 and target.read_bytes().startswith(original)


def test_a_large_pdf_is_saved_deleted_and_put_back_with_its_content(server: Server, kit: RunnerKit,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """V3-P77 §6.1, §6.3: over the editor's 1 MiB, up to 64 MiB, a PDF is saved in place (V3-P82 §3.6) and
    deleted with its content kept, put back in one step, and its kept version can be put back over it."""
    original = FIXTURE.read_bytes()
    padded = original.replace(b"%%EOF", b"%" + b"p" * (3 * MiB) + b"\n%%EOF", 1)  # a comment: still a PDF
    target, path = place(kit, padded)
    added = increment(padded)
    assert append(server, path, added, identity(server, path)).status_code == 200
    assert versions(server, path) == []
    inked = padded + added
    # Deleted: its content kept in the PDFs' store, put back in one step.
    explorer = explorer_of(server)
    assert explorer.pdf_versions is not None
    now = identity(server, path)
    with monkeypatch.context() as m:
        m.setattr(explorer.pdf_versions, "ready_for", lambda size: False)
        refused(server.post("/api/machine/file/delete", {"path": path, "expected": now}), 507, "versions_unavailable")

    async def old(fresh: bool = False) -> bool:
        return False

    with monkeypatch.context() as m:
        m.setattr(explorer._machine, "copies_large", old)
        refused(server.post("/api/machine/file/delete", {"path": path, "expected": now}), 422, "unsupported")
    assert target.exists()
    gone = server.post("/api/machine/file/delete", {"path": path, "expected": now})
    assert gone.status_code == 200 and gone.json()["previous_kept"] and not target.exists()
    deleted = server.client.get("/api/machine/versions/deleted", params={"path": "~/projects/a"}).json()["items"]
    assert [(d["name"], d["size"]) for d in deleted] == [("notes.pdf", len(inked))]
    put = server.post(f"/api/machine/versions/{deleted[0]['id']}/restore", {"path": path})
    assert put.status_code == 201, put.text
    assert target.read_bytes() == inked
    # Its kept version put back over it: what it holds now is kept first.
    assert append(server, path, increment(inked, 94), identity(server, path)).status_code == 200
    stale = identity(server, path)
    back = server.post(f"/api/machine/versions/{deleted[0]['id']}/put_back", {"path": path, "expected": stale})
    assert back.status_code == 200, back.text
    assert target.read_bytes() == inked and back.json()["previous_kept"]
    assert [v["reason"] for v in versions(server, path)] == ["restore", "delete"]
    refused(server.post(f"/api/machine/versions/{deleted[0]['id']}/put_back", {"path": path, "expected": stale}),
            409, "changed")


def test_a_large_file_that_is_not_a_pdf_is_deleted_as_before(server: Server, kit: RunnerKit) -> None:
    target, path = place(kit, b"x" * (3 * MiB), "big.bin")
    gone = server.post("/api/machine/file/delete", {"path": path, "expected": identity(server, path)})
    assert gone.status_code == 200 and not gone.json()["previous_kept"] and not target.exists()
    refused(server.post("/api/machine/versions/nope/put_back", {"path": path, "expected": [1, 2, 3, 4]}), 404,
            "not_found")


def test_a_runner_rolled_back_since_it_was_asked_fails_closed(server: Server, kit: RunnerKit,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #103 review P1, V3-P82 §3.6: HIMARI was told protocol 4; the runner is an older one now. Its refusals
    — an unknown ``append`` or ``delete_copied``, a large replace with ``copy_max`` ignored — leave the PDF as
    it was, and are told."""
    from himari.runner import api as runner

    original = FIXTURE.read_bytes()
    padded = original.replace(b"%%EOF", b"%" + b"p" * (3 * MiB) + b"\n%%EOF", 1)
    target, path = place(kit, padded)
    machine = explorer_of(server)._machine
    real_call = machine.call
    assert append(server, path, increment(padded), identity(server, path)).status_code == 200  # told: 4
    assert machine._protocol == runner.PROTOCOL_VERSION
    saved = target.read_bytes()

    async def older(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        if op == runner.HELLO:
            return {"version": 2}, []
        if op in (runner.DELETE_COPIED, runner.APPEND, runner.TRUNCATE):
            raise runner.RunnerError(runner.INVALID, f"unknown operation {op}")
        return await real_call(op, {k: v for k, v in args.items() if k != "copy_max"}, fds)  # ignored

    monkeypatch.setattr(machine, "call", older)
    machine._protocol = runner.PROTOCOL_VERSION  # as told before the rollback
    refused(server.post("/api/machine/file/delete", {"path": path, "expected": identity(server, path)}), 422,
            "unsupported")
    assert target.read_bytes() == saved
    machine._protocol = runner.PROTOCOL_VERSION
    refused(append(server, path, increment(saved, 93), identity(server, path)), 422, "unsupported")
    assert target.read_bytes() == saved
