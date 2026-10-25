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
    monkeypatch.setattr(versions, "MAX_BYTES", 9)
    store.keep("/b", copy(b"12345"), "save", now=now + timedelta(seconds=1))
    newest = store.keep("/b", copy(b"67890"), "save", now=now + timedelta(seconds=2))
    # 6 + 5 bytes, then 5 + 5, over 9: the oldest go first, until it fits.
    assert store.of("/a") == () and [v.version_id for v in store.of("/b")] == [newest.version_id]


def test_ready_only_with_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = VersionStore(tmp_path / "v")
    assert store.ready_for(1024)

    class Usage:
        free = versions.HEADROOM_BYTES

    monkeypatch.setattr("himari.bootstrap.versions.shutil.disk_usage", lambda path: Usage())
    assert not store.ready_for(1)
