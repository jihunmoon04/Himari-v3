"""Removing a Conversation's own folder (V3-P65 §5): only that folder, never
through a link, never across a mount, under the Workspace's write lock."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from himari.common import ConversationId, PrincipalId, TrustedScope, WorkspaceId
from himari.environment.inside import InsideEnvironment, fs

ALICE = WorkspaceId("ws_alice")
SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("c1"))
OWN = "conversations/c1"


@pytest.fixture
def env(tmp_path: Path) -> InsideEnvironment:
    environment = InsideEnvironment(tmp_path / "workspaces")
    environment.provision(ALICE)
    return environment


@pytest.fixture
def root(env: InsideEnvironment) -> Path:
    return Path(env.host_root(ALICE))


def remove(env: InsideEnvironment, cwd: str = OWN) -> bool:
    return asyncio.run(env.remove_conversation_folder(ALICE, SCOPE, cwd))


def test_the_folder_and_everything_in_it_go_and_nothing_else(env: InsideEnvironment, root: Path) -> None:
    own = root / OWN
    (own / "a" / "b").mkdir(parents=True)
    (own / "a" / "b" / "deep.txt").write_text("x")
    (own / "top.txt").write_text("y")
    (own / ".hidden").write_text("z")
    os.mkfifo(own / "pipe")
    (root / "conversations" / "c2").mkdir()
    (root / "conversations" / "c2" / "keep.txt").write_text("other conversation")
    (root / "shared.txt").write_text("elsewhere in the root")

    assert remove(env) is True
    assert not own.exists()
    assert (root / "conversations" / "c2" / "keep.txt").read_text() == "other conversation"
    assert (root / "shared.txt").exists() and (root / "conversations").is_dir()
    assert remove(env) is False  # already gone: nothing to do


def test_links_are_removed_and_their_targets_stay(env: InsideEnvironment, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host file")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "inner.txt").write_text("host dir")
    other = root / "conversations" / "c2"
    other.mkdir()
    (other / "keep.txt").write_text("keep")
    own = root / OWN
    own.mkdir()
    (own / "to_file").symlink_to(outside)
    (own / "to_dir").symlink_to(outside_dir)
    (own / "to_other").symlink_to(other)
    (own / "hard").hardlink_to(other / "keep.txt")

    assert remove(env) is True
    assert not own.exists()
    assert outside.read_text() == "host file" and (outside_dir / "inner.txt").read_text() == "host dir"
    assert (other / "keep.txt").read_text() == "keep" and os.stat(other / "keep.txt").st_nlink == 1


def test_a_folder_that_is_itself_a_link_loses_only_the_link(env: InsideEnvironment, root: Path,
                                                           tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "file").write_text("stays")
    (root / OWN).symlink_to(target)
    assert remove(env) is True
    assert not os.path.lexists(root / OWN) and (target / "file").read_text() == "stays"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_directories_a_command_locked_are_made_writable_first(env: InsideEnvironment, root: Path) -> None:
    own = root / OWN
    (own / "ro" / "sealed").mkdir(parents=True)
    (own / "ro" / "file").write_text("x")
    (own / "ro" / "sealed" / "file").write_text("y")
    (own / "ro" / "sealed" / "file").chmod(0o000)
    (own / "ro" / "sealed").chmod(0o000)
    (own / "ro").chmod(0o500)
    assert remove(env) is True
    assert not own.exists()


def test_a_tree_deeper_than_the_descent_limit_is_removed(env: InsideEnvironment, root: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_MAX_DEPTH", 3)
    deep = root / OWN
    for level in range(20):
        deep = deep / f"d{level}"
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_text("x")
    depths: list[int] = []
    real_empty = fs._empty

    def recording(dir_fd: int, top_fd: int, depth: int) -> bool:
        depths.append(depth)
        return real_empty(dir_fd, top_fd, depth)

    monkeypatch.setattr(fs, "_empty", recording)
    assert remove(env) is True
    assert max(depths) == 3  # never descended past the limit: deeper parts were moved up
    assert not (root / OWN).exists()
    assert not [p for p in (root / "conversations").iterdir()]  # no moved-up directory is left behind


def test_a_mount_inside_the_folder_stops_the_removal(env: InsideEnvironment, root: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    own = root / OWN
    (own / "mnt").mkdir(parents=True)
    (own / "mnt" / "on_the_mount").write_text("not ours to remove")
    real = fs.openat2

    def crossing(dirfd: int, relative: str, flags: int, *, mode: int = 0) -> int:
        if relative == "mnt":
            raise fs.FsError(fs.FsCategory.CROSS_DEVICE)  # what RESOLVE_NO_XDEV reports at a mount point
        return real(dirfd, relative, flags, mode=mode)

    monkeypatch.setattr(fs, "openat2", crossing)
    with pytest.raises(fs.FsError) as exc:
        remove(env)
    assert exc.value.category is fs.FsCategory.CROSS_DEVICE
    assert (own / "mnt" / "on_the_mount").read_text() == "not ours to remove"


def test_only_the_conversations_default_folder_is_removed(env: InsideEnvironment, root: Path) -> None:
    (root / "projects" / "site").mkdir(parents=True)
    (root / "conversations" / "c2").mkdir()
    for cwd in ("projects/site", "conversations/c2", ".", "conversations"):
        assert remove(env, cwd) is False
    assert (root / "projects" / "site").is_dir() and (root / "conversations" / "c2").is_dir()


def test_a_missing_workspace_root_is_nothing_to_remove(tmp_path: Path) -> None:
    env = InsideEnvironment(tmp_path / "workspaces")
    assert remove(env) is False


def test_removal_waits_for_the_workspace_lock(env: InsideEnvironment, root: Path) -> None:
    (root / OWN).mkdir()
    (root / OWN / "file").write_text("x")

    async def scenario() -> None:
        lock = env.write_lock(ALICE)
        await lock.acquire()  # a command runs in this root
        removal = asyncio.create_task(env.remove_conversation_folder(ALICE, SCOPE, OWN))
        await asyncio.sleep(0.05)
        assert not removal.done() and (root / OWN / "file").exists()
        lock.release()
        assert await removal is True

    asyncio.run(scenario())
    assert not (root / OWN).exists()
