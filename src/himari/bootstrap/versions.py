"""Previous versions of the files the web editor saved or deleted (V3-P75 §7.4).

Kept in HIMARI's data folder (``himari-v3``'s own, hidden from commands),
one folder per file path, each version a file beside an index. What is kept
is the copy the runner took right before the rename or unlink, so a version
is what the file held when it was replaced. Versions older than
KEEP_DAYS go, and the oldest go first while all of them pass the store's
budget (MAX_BYTES; PDFs have a store and budget of their own, V3-P77 §6.2).

Files only; no Data table, so no database version changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KEEP_DAYS = 30
MAX_BYTES = 200 * 1024 * 1024
HEADROOM_BYTES = 16 * 1024 * 1024  # free space a save must leave besides its copy (§7.4 step 1)
MAX_VERSION_BYTES = 1024 * 1024  # the runner copies no more, unless asked (V3-P77 §6.1)


@dataclass(frozen=True)
class Version:
    version_id: str
    saved_at: datetime
    size: int
    reason: str  # the owner's "save" | "delete" | "revert"; "agent" | "agent_delete" (V3-P76 §4.2: before an agent's
    # first change of a file since review, the change a write or a delete); "ink" | "restore" (V3-P77 §6.2: before
    # ink was saved into a PDF, before a previous version was put back over it)


class VersionStore:
    def __init__(self, root: Path, max_bytes: int = MAX_BYTES, max_version_bytes: int = MAX_VERSION_BYTES) -> None:
        """One store per budget: the agent's versions have a root of their own (V3-P76 Q5), and so have
        PDFs' (V3-P77 §6.2: 1 GiB, each version up to 64 MiB)."""
        self.root = root
        self.max_bytes = max_bytes
        self.max_version_bytes = max_version_bytes
        self._lock = threading.Lock()  # the index files; called from threads

    def _folder(self, path: str) -> Path:
        return self.root / hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:32]

    def ready_for(self, size: int) -> bool:
        """§7.4 step 1: a copy of ``size`` bytes can be kept, with room to spare."""
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            return os.access(self.root, os.W_OK) and shutil.disk_usage(self.root).free >= size + HEADROOM_BYTES
        except OSError:
            return False

    def keep(self, path: str, fd: int, reason: str, now: datetime | None = None) -> Version:
        """Keep what ``fd`` (the runner's copy, read from its start) holds as a
        version of ``path``; ``fd`` is closed. OSError: not kept."""
        now = now or datetime.now(UTC)
        version_id = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(3)}"
        folder = self._folder(path)
        try:
            with self._lock:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                folder.mkdir(mode=0o700, exist_ok=True)
            # Copied as it is read, never whole in memory (a PDF's is up to 64 MiB); named only once complete.
            temp = folder / f"{version_id}.part"
            size = 0
            try:
                with open(temp, "xb") as out:
                    while chunk := os.read(fd, 1 << 20):
                        size += len(chunk)
                        if size > self.max_version_bytes:
                            raise OSError("a copy larger than the runner makes")
                        out.write(chunk)
                temp.replace(folder / f"{version_id}.bin")
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        finally:
            os.close(fd)
        version = Version(version_id, now, size, reason)
        with self._lock:
            kept = folder / f"{version_id}.bin"
            try:
                if not kept.exists():  # its folder pruned meanwhile, with its last version
                    raise OSError("the version went while it was being kept")
                index = self._index(folder)
                index["path"] = path
                index["versions"].append({"id": version.version_id, "saved_at": now.isoformat(), "size": size,
                                          "reason": reason})
                self._write_index(folder, index)
            except BaseException:
                # Not in the index, so nothing would ever prune it: this version's file goes now (PR #103
                # review P2); the versions already kept are not touched.
                kept.unlink(missing_ok=True)
                raise
            self._prune(now)
        return version

    def of(self, path: str) -> tuple[Version, ...]:
        """Newest first."""
        with self._lock:
            index = self._index(self._folder(path))
        return tuple(sorted((_version(v) for v in index["versions"]), key=lambda v: v.saved_at, reverse=True))

    def deleted_in(self, folder: str, limit: int = 100) -> tuple[tuple[str, Version], ...]:
        """Files of ``folder`` (directly in it) last kept on a delete: each
        path with that version, the most recently deleted first (PR #95
        review B3: how a deleted file is found again)."""
        found: list[tuple[str, Version]] = []
        with self._lock:
            if not self.root.is_dir():
                return ()
            for entry in (p for p in self.root.iterdir() if p.is_dir()):
                index = self._index(entry)
                path = index.get("path")
                if not isinstance(path, str) or os.path.dirname(path) != folder or not index["versions"]:
                    continue
                latest = max((_version(v) for v in index["versions"]), key=lambda v: v.saved_at)
                if latest.reason in ("delete", "agent_delete"):
                    found.append((path, latest))
        found.sort(key=lambda item: item[1].saved_at, reverse=True)
        return tuple(found[:limit])

    def latest(self, path: str) -> Version | None:
        """The newest version of ``path`` (V3-P77 §6.2: ink saves keep one per 30 minutes)."""
        versions = self.of(path)
        return versions[0] if versions else None

    def open(self, path: str, version_id: str) -> int | None:
        """A version opened to read (an fd the caller closes), never read whole: a PDF's goes to the runner
        as it is (V3-P77 §6.3)."""
        if not all(c.isalnum() or c in "-T" for c in version_id):
            return None
        with self._lock:
            folder = self._folder(path)
            if not any(v["id"] == version_id for v in self._index(folder)["versions"]):
                return None
            try:
                return os.open(folder / f"{version_id}.bin", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError:
                return None

    def has(self, path: str, version_id: str) -> bool:
        with self._lock:
            return any(v["id"] == version_id for v in self._index(self._folder(path))["versions"])

    def read(self, path: str, version_id: str) -> bytes | None:
        if not all(c.isalnum() or c in "-T" for c in version_id):
            return None
        with self._lock:
            folder = self._folder(path)
            if not any(v["id"] == version_id for v in self._index(folder)["versions"]):
                return None
            try:
                return (folder / f"{version_id}.bin").read_bytes()
            except OSError:
                return None

    @staticmethod
    def _index(folder: Path) -> dict[str, Any]:
        try:
            loaded = json.loads((folder / "index.json").read_text("utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("versions"), list):
                return loaded
        except (OSError, ValueError):
            pass
        return {"path": "", "versions": []}

    @staticmethod
    def _write_index(folder: Path, index: dict[str, Any]) -> None:
        temp = folder / f"index.{secrets.token_hex(4)}.tmp"
        try:
            temp.write_text(json.dumps(index), "utf-8")
            temp.replace(folder / "index.json")
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def _prune(self, now: datetime) -> None:
        """Older than KEEP_DAYS go; then the oldest, while all pass the budget."""
        everything: list[tuple[datetime, Path, str, int]] = []
        for folder in (p for p in self.root.iterdir() if p.is_dir()):
            for entry in self._index(folder)["versions"]:
                if isinstance(entry, dict):
                    everything.append((_version(entry).saved_at, folder, str(entry["id"]), int(entry["size"])))
        everything.sort()
        total = sum(size for *_, size in everything)
        gone: dict[Path, set[str]] = {}
        for saved_at, folder, version_id, size in everything:
            if saved_at >= now - timedelta(days=KEEP_DAYS) and total <= self.max_bytes:
                break
            gone.setdefault(folder, set()).add(version_id)
            total -= size
        for folder, ids in gone.items():
            index = self._index(folder)
            index["versions"] = [v for v in index["versions"] if isinstance(v, dict) and v["id"] not in ids]
            for version_id in ids:
                (folder / f"{version_id}.bin").unlink(missing_ok=True)
            if index["versions"]:
                self._write_index(folder, index)
            else:
                shutil.rmtree(folder, ignore_errors=True)


def _version(entry: object) -> Version:
    assert isinstance(entry, dict)
    return Version(str(entry["id"]), datetime.fromisoformat(str(entry["saved_at"])), int(entry["size"]),
                   str(entry["reason"]))
