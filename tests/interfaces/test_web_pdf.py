"""Saving ink into a PDF over the API (V3-P77 §5, §6): a real runner (fake sandbox) on a temporary home."""

from __future__ import annotations

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


def test_ink_is_added_after_the_pdf_and_its_previous_version_kept(server: Server, kit: RunnerKit) -> None:
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    read = server.client.get("/api/machine/download", params={"path": path})
    assert read.content == original
    seen = read.headers["x-himari-identity"].split(",")
    assert seen == identity(server, path)  # the identity of the bytes sent, to save against (§5.3)
    added = increment(original)
    saved = append(server, path, added, seen)
    assert saved.status_code == 200, saved.text
    assert target.read_bytes() == original + added  # the original bytes untouched
    assert saved.json()["previous_kept"] and saved.json()["identity"] == identity(server, path)
    kept = versions(server, path)
    assert [(v["reason"], v["size"]) for v in kept] == [("ink", len(original))]
    as_kept = server.client.get(f"/api/machine/versions/{kept[0]['id']}/download", params={"path": path})
    assert as_kept.content == original and as_kept.headers["content-security-policy"].startswith("sandbox")
    refused(server.client.get(f"/api/machine/versions/{kept[0]['id']}", params={"path": path}), 415, "not_regular")
    # Within 30 minutes of that version, another save keeps none: that one holds the PDF before the ink.
    second = increment(original + added, 91)
    again = append(server, path, second, saved.json()["identity"])
    assert again.status_code == 200 and again.json()["previous_kept"]
    assert target.read_bytes() == original + added + second and len(versions(server, path)) == 1
    # Saved against what was read: changed since, nothing written (ink made on the old bytes).
    refused(append(server, path, increment(original, 92), seen), 409, "changed")
    assert target.read_bytes() == original + added + second


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


def test_a_previous_version_is_kept_or_nothing_is_saved(server: Server, kit: RunnerKit,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.3: before the replace, a store that cannot keep the version refuses the save; after it, a failure
    to keep it is told (previous_kept false). An older runner refuses before anything is sent."""
    original = FIXTURE.read_bytes()
    target, path = place(kit, original)
    explorer = explorer_of(server)
    assert explorer.pdf_versions is not None

    async def old() -> bool:
        return False

    with monkeypatch.context() as m:
        m.setattr(explorer._machine, "copies_large", old)
        refused(append(server, path, increment(original), identity(server, path)), 422, "unsupported")
    with monkeypatch.context() as m:
        m.setattr(explorer.pdf_versions, "ready_for", lambda size: False)
        refused(append(server, path, increment(original), identity(server, path)), 507, "versions_unavailable")
    assert target.read_bytes() == original

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(explorer.pdf_versions, "keep", broken)
    saved = append(server, path, increment(original), identity(server, path))
    assert saved.status_code == 200 and saved.json()["previous_kept"] is False
    assert target.read_bytes().startswith(original) and versions(server, path) == []


def test_a_large_pdf_is_saved_deleted_and_put_back_with_its_content(server: Server, kit: RunnerKit,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """§6.1, §6.3: over the editor's 1 MiB, up to 64 MiB, a PDF keeps its versions."""
    original = FIXTURE.read_bytes()
    padded = original.replace(b"%%EOF", b"%" + b"p" * (3 * MiB) + b"\n%%EOF", 1)  # a comment: still a PDF
    target, path = place(kit, padded)
    added = increment(padded)
    assert append(server, path, added, identity(server, path)).status_code == 200
    ink = versions(server, path)
    assert [(v["reason"], v["size"]) for v in ink] == [("ink", len(padded))]
    # Put back over the file: what it holds now is kept first, whatever the 30 minutes.
    stale = identity(server, path)
    back = server.post(f"/api/machine/versions/{ink[0]['id']}/put_back", {"path": path, "expected": stale})
    assert back.status_code == 200, back.text
    assert target.read_bytes() == padded and back.json()["previous_kept"]
    assert [v["reason"] for v in versions(server, path)] == ["restore", "ink"]
    refused(server.post(f"/api/machine/versions/{ink[0]['id']}/put_back", {"path": path, "expected": stale}), 409,
            "changed")
    # Deleted: its content kept in the PDFs' store, put back in one step.
    explorer = explorer_of(server)
    assert explorer.pdf_versions is not None
    now = identity(server, path)
    with monkeypatch.context() as m:
        m.setattr(explorer.pdf_versions, "ready_for", lambda size: False)
        refused(server.post("/api/machine/file/delete", {"path": path, "expected": now}), 507, "versions_unavailable")

    async def old() -> bool:
        return False

    with monkeypatch.context() as m:
        m.setattr(explorer._machine, "copies_large", old)
        refused(server.post("/api/machine/file/delete", {"path": path, "expected": now}), 422, "unsupported")
    assert target.exists()
    gone = server.post("/api/machine/file/delete", {"path": path, "expected": now})
    assert gone.status_code == 200 and gone.json()["previous_kept"] and not target.exists()
    deleted = server.client.get("/api/machine/versions/deleted", params={"path": "~/projects/a"}).json()["items"]
    assert [(d["name"], d["size"]) for d in deleted] == [("notes.pdf", len(padded))]
    put = server.post(f"/api/machine/versions/{deleted[0]['id']}/restore", {"path": path})
    assert put.status_code == 201, put.text
    assert target.read_bytes() == padded


def test_a_large_file_that_is_not_a_pdf_is_deleted_as_before(server: Server, kit: RunnerKit) -> None:
    target, path = place(kit, b"x" * (3 * MiB), "big.bin")
    gone = server.post("/api/machine/file/delete", {"path": path, "expected": identity(server, path)})
    assert gone.status_code == 200 and not gone.json()["previous_kept"] and not target.exists()
    refused(server.post("/api/machine/versions/nope/put_back", {"path": path, "expected": [1, 2, 3, 4]}), 404,
            "not_found")
