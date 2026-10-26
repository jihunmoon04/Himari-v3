"""The web editor's previous versions (V3-P75 §7.4)."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from himari.bootstrap import versions
from himari.bootstrap.versions import VersionStore


def copy(data: bytes) -> int:
    """An fd as the runner hands it over: an unnamed file, read from its start."""
    handle = tempfile.TemporaryFile()
    handle.write(data)
    handle.flush()
    fd = os.dup(handle.fileno())
    handle.close()
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def test_kept_listed_and_read(tmp_path: Path) -> None:
    store = VersionStore(tmp_path / "file-versions")
    first = store.keep("/home/u/a.txt", copy(b"one"), "save")
    second = store.keep("/home/u/a.txt", copy(b"two"), "delete")
    store.keep("/home/u/b.txt", copy(b"other"), "save")
    listed = store.of("/home/u/a.txt")
    assert [(v.version_id, v.size, v.reason) for v in listed] == [(second.version_id, 3, "delete"),
                                                                   (first.version_id, 3, "save")]
    assert store.read("/home/u/a.txt", first.version_id) == b"one"
    assert store.read("/home/u/b.txt", first.version_id) is None  # another file's version
    assert store.read("/home/u/a.txt", "../../etc/passwd") is None and store.of("/nowhere") == ()
    assert os.stat(tmp_path / "file-versions").st_mode & 0o777 == 0o700


def test_old_versions_go_then_the_oldest_while_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = VersionStore(tmp_path / "v")
    now = datetime.now(UTC)
    old = store.keep("/a", copy(b"old"), "save", now=now - timedelta(days=versions.KEEP_DAYS + 1))
    kept = store.keep("/a", copy(b"recent"), "save", now=now)
    assert [v.version_id for v in store.of("/a")] == [kept.version_id]
    assert store.read("/a", old.version_id) is None
    store.max_bytes = 9  # the store's own budget (V3-P77 §6.2: PDFs have theirs)
    store.keep("/b", copy(b"12345"), "save", now=now + timedelta(seconds=1))
    newest = store.keep("/b", copy(b"67890"), "save", now=now + timedelta(seconds=2))
    # 6 + 5 bytes, then 5 + 5, over 9: the oldest go first, until it fits.
    assert store.of("/a") == () and [v.version_id for v in store.of("/b")] == [newest.version_id]


def test_a_store_of_large_versions_streams_them_and_holds_its_limit(tmp_path: Path) -> None:
    """V3-P77 §6.2: the PDFs' store — versions up to its own size, copied as read, opened without reading."""
    store = VersionStore(tmp_path / "file-versions-pdf", max_bytes=1 << 30, max_version_bytes=3 << 20)
    assert store.latest("/p.pdf") is None
    data = b"%PDF-" + bytes(range(256)) * (10 * 1024)  # 2.5 MiB: past the editor's 1 MiB
    kept = store.keep("/p.pdf", copy(data), "ink")
    assert store.latest("/p.pdf") == kept and kept.size == len(data)
    fd = store.open("/p.pdf", kept.version_id)
    assert fd is not None
    with os.fdopen(fd, "rb") as opened:
        assert opened.read() == data
    assert store.open("/p.pdf", "../index") is None and store.open("/other.pdf", kept.version_id) is None
    with pytest.raises(OSError):
        store.keep("/p.pdf", copy(b"x" * ((3 << 20) + 1)), "ink")
    assert [v.version_id for v in store.of("/p.pdf")] == [kept.version_id]
    assert not list((tmp_path / "file-versions-pdf").rglob("*.part"))  # nothing half-kept is left
    with pytest.raises(OSError):  # the editor's store holds to 1 MiB, as the runner copies for it
        VersionStore(tmp_path / "v").keep("/q", copy(b"y" * (versions.MAX_VERSION_BYTES + 1)), "save")


def test_ready_only_with_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = VersionStore(tmp_path / "v")
    assert store.ready_for(1024)

    class Usage:
        free = versions.HEADROOM_BYTES

    monkeypatch.setattr("himari.bootstrap.versions.shutil.disk_usage", lambda path: Usage())
    assert not store.ready_for(1)


def test_files_last_kept_on_a_delete_are_found_in_their_folder(tmp_path: Path) -> None:
    """PR #95 review B3: how a deleted file is found again; one saved again since is not deleted."""
    store = VersionStore(tmp_path / "v")
    now = datetime.now(UTC)
    store.keep("/p/a.txt", copy(b"a"), "delete", now=now)
    store.keep("/p/b.txt", copy(b"b1"), "delete", now=now + timedelta(seconds=1))
    store.keep("/p/b.txt", copy(b"b2"), "save", now=now + timedelta(seconds=2))  # made again and saved
    later = store.keep("/p/c.txt", copy(b"c"), "delete", now=now + timedelta(seconds=3))
    store.keep("/p/sub/d.txt", copy(b"d"), "delete", now=now)  # not directly in /p
    found = store.deleted_in("/p")
    assert [(path, v.version_id) for path, v in found][0] == ("/p/c.txt", later.version_id)
    assert [path for path, _ in found] == ["/p/c.txt", "/p/a.txt"]
    assert store.deleted_in("/nowhere") == () and VersionStore(tmp_path / "none").deleted_in("/p") == ()
