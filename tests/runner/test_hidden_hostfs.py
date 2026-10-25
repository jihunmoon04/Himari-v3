"""The runner's hidden list and file operations (V3-P74 §7, §8), on a temporary home."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from himari.runner import api
from himari.runner.hidden import HiddenRules
from himari.runner import hostfs
from himari.runner.hostfs import Host


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for folder in (".ssh", ".config/gh", ".local/bin", ".local/share/code", ".cache/pip", ".cache/himari-runner",
                   "projects/a", "HIMARI/conversations/c1", "outside"):
        (home / folder).mkdir(parents=True)
    (home / ".ssh/id").write_text("secret")
    (home / ".gitconfig").write_text("[user]\n")
    (home / ".bashrc").write_text("export TOKEN=x\n")
    (home / ".local/bin/tool").write_text("#!/bin/sh\n")
    (home / ".local/share/code/ipc").write_text("x")
    (home / "projects/a/main.c").write_text("int main(){}\n")
    return home


@pytest.fixture
def rules(home: Path, tmp_path: Path) -> HiddenRules:
    (tmp_path / "srv").mkdir()
    return HiddenRules(str(home), (".gitconfig", ".local/bin", ".local/lib", ".cache/himari-runner"),
                       (str(tmp_path / "srv"),))


@pytest.fixture
def host(rules: HiddenRules, home: Path) -> Host:
    return Host(rules, str(home / "HIMARI"))


def test_dot_entries_are_hidden_except_the_visible_ones(rules: HiddenRules, home: Path) -> None:
    hidden = rules.is_hidden
    assert hidden(f"{home}/.ssh/id") and hidden(f"{home}/.bashrc") and hidden(f"{home}/.config/gh")
    assert hidden(f"{home}/.local/share/code/ipc") and hidden(f"{home}/.cache/pip")
    assert not hidden(f"{home}/.gitconfig") and not hidden(f"{home}/.local/bin/tool")
    assert not hidden(f"{home}/.local") and not hidden(f"{home}/.cache")  # they hold visible entries
    assert not hidden(f"{home}/.cache/himari-runner") and not hidden(f"{home}/projects/a/.git")  # only home level
    assert not hidden(str(home)) and not hidden("/usr/bin")
    masks = {m.path: m.kind for m in rules.masks()}
    assert masks[f"{home}/.ssh"] == "dir" and masks[f"{home}/.bashrc"] == "file"
    assert f"{home}/.local/share" in masks and f"{home}/.cache/pip" in masks
    assert not any(p.startswith(f"{home}/.local/bin") or p == f"{home}/.local" or p == f"{home}/.gitconfig"
                   or p.startswith(f"{home}/.cache/himari-runner") for p in masks)


def test_a_hidden_link_is_masked_at_its_target(rules: HiddenRules, home: Path, tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    (home / ".aws").symlink_to(keys)
    masks = {m.path for m in rules.masks()}
    assert str(keys) in masks
    assert rules.is_hidden(str(keys / "credentials"))


def test_extra_paths(rules: HiddenRules, tmp_path: Path) -> None:
    assert rules.is_hidden(str(tmp_path / "srv" / "db"))
    assert str(tmp_path / "srv") in {m.path for m in rules.masks()}


def read(host: Host, path: str, cwd: str) -> bytes:
    fd, _, _ = host.open_read(path, cwd)
    try:
        return os.read(fd, 1000)
    finally:
        os.close(fd)


def category(call: object) -> str:
    with pytest.raises(api.RunnerError) as caught:
        call()  # type: ignore[operator]
    return caught.value.category


def test_reading_is_broad_but_not_hidden_or_special(host: Host, home: Path) -> None:
    cwd = str(home / "HIMARI/conversations/c1")
    assert read(host, "~/projects/a/main.c", cwd) == b"int main(){}\n"
    assert read(host, "../../../projects/a/main.c", cwd) == b"int main(){}\n"
    assert category(lambda: host.open_read("~/.ssh/id", cwd)) == api.NOT_FOUND
    assert category(lambda: host.open_read("/proc/self/environ", cwd)) == api.NOT_REGULAR
    assert category(lambda: host.open_read("/sys/kernel/mm/transparent_hugepage/enabled", cwd)) == api.NOT_REGULAR
    (home / "projects/to-proc").symlink_to("/proc/self/environ")
    assert category(lambda: host.open_read("~/projects/to-proc", cwd)) == api.NOT_REGULAR
    (home / "projects/to-ssh").symlink_to(home / ".ssh")
    assert category(lambda: host.open_read("~/projects/to-ssh/id", cwd)) == api.NOT_FOUND
    assert category(lambda: host.open_read("~/projects", cwd)) == api.IS_DIRECTORY
    real, entries, _ = host.listing("~", cwd, 100)
    names = {e["name"] for e in entries}
    assert real == str(home) and ".ssh" not in names and ".bashrc" not in names and ".gitconfig" in names


def test_hard_links_can_be_read(host: Host, home: Path) -> None:
    os.link(home / "projects/a/main.c", home / "projects/a/linked.c")
    assert read(host, "~/projects/a/linked.c", "/") == b"int main(){}\n"


def write(host: Host, path: str, roots: list[str], data: bytes = b"x", **kw: object) -> str:
    fd, parent, name = host.target(path, roots[0], roots, follow=True, **kw)  # type: ignore[arg-type]
    try:
        host.write(fd, name, data, None)
    finally:
        os.close(fd)
    return os.path.join(parent, name)


def test_writing_only_inside_the_workspaces(host: Host, home: Path, tmp_path: Path) -> None:
    root = str(home / "HIMARI/conversations/c1")
    assert write(host, "notes/a.txt", [root], create_parents=True) == f"{root}/notes/a.txt"
    assert category(lambda: write(host, "~/projects/a/x.c", [root])) == api.OUTSIDE_WORKSPACES
    assert category(lambda: write(host, "../c2/x", [root], create_parents=True)) == api.OUTSIDE_WORKSPACES
    assert not (home / "HIMARI/conversations/c2").exists()
    # links: inside to inside is fine (absolute too); inside to outside, or to a hidden path, is refused
    (Path(root) / "abs-in").symlink_to(f"{root}/notes/a.txt")
    assert write(host, "abs-in", [root], b"y") == f"{root}/notes/a.txt"
    assert (Path(root) / "notes/a.txt").read_bytes() == b"y"
    (Path(root) / "out").symlink_to(home / "projects/a/main.c")
    assert category(lambda: write(host, "out", [root])) == api.OUTSIDE_WORKSPACES
    (Path(root) / "dir-out").symlink_to(home / "projects")
    assert category(lambda: write(host, "dir-out/new.c", [root])) == api.OUTSIDE_WORKSPACES
    (Path(root) / "loop").symlink_to(Path(root) / "loop")
    assert category(lambda: write(host, "loop", [root])) in (api.LINKS, api.OUTSIDE_WORKSPACES)
    # a Workspace that holds hidden paths: those stay out of reach
    assert category(lambda: write(host, "~/.ssh/authorized_keys", [str(home)])) == api.NOT_FOUND
    assert category(lambda: write(host, "~/.bashrc", [str(home)])) == api.NOT_FOUND
    assert write(host, "~/projects/new.c", [str(home)]) == f"{home}/projects/new.c"


def test_a_write_replaces_a_hard_link_not_its_content(host: Host, home: Path) -> None:
    root = str(home / "HIMARI/conversations/c1")
    os.link(home / "projects/a/main.c", Path(root) / "shared.c")
    write(host, "shared.c", [root], b"changed")
    assert (home / "projects/a/main.c").read_text() == "int main(){}\n"


def test_identity_check_and_delete(host: Host, home: Path) -> None:
    root = str(home / "HIMARI/conversations/c1")
    write(host, "f.txt", [root], b"one")
    fd, parent, name = host.target("f.txt", root, [root], follow=True)
    try:
        info = os.stat(name, dir_fd=fd)
        write(host, "f.txt", [root], b"two")  # someone else wrote meanwhile
        assert category(lambda: host.write(fd, name, b"three", host.identity(info))) == api.CHANGED
    finally:
        os.close(fd)
    (Path(root) / "link").symlink_to(home / "projects/a/main.c")
    fd, _, name = host.target("link", root, [root], follow=False)
    try:
        host.delete(fd, name)
    finally:
        os.close(fd)
    assert (home / "projects/a/main.c").exists() and not (Path(root) / "link").is_symlink()


def test_roots_are_checked(host: Host, home: Path) -> None:
    assert host.roots([str(home / "projects")], ["/"]) == [str(home / "projects")]
    assert category(lambda: host.roots([str(home / ".ssh")], ["/"])) == api.ACCESS
    assert category(lambda: host.roots([str(home / "projects")], [str(home / "HIMARI")])) == api.ACCESS
    assert category(lambda: host.roots([str(home / "projects/../projects")], ["/"])) == api.INVALID
    (home / "alias").symlink_to(home / "projects")
    assert category(lambda: host.roots([str(home / "alias")], ["/"])) == api.INVALID
    assert category(lambda: host.roots([], ["/"])) == api.INVALID


def test_conversation_folders(host: Host, home: Path) -> None:
    folder = host.conversation_folder("c9")
    assert folder == f"{home}/HIMARI/conversations/c9" and os.path.isdir(folder)
    assert category(lambda: host.conversation_folder("../x")) == api.INVALID
    (Path(folder) / "f").write_text("x")
    assert host.remove_conversation_folder("c9") and not os.path.exists(folder)
    assert not host.remove_conversation_folder("c9")


def test_missing_parents_are_judged_before_they_are_made(home: Path, tmp_path: Path) -> None:
    """PR #87 review B2: a hidden folder that does not exist yet is never created."""
    root = home / "projects"
    rules = HiddenRules(str(home), (".gitconfig",), (str(root / "secret"),))
    host = Host(rules, str(home / "HIMARI"))
    assert category(lambda: write(host, f"{root}/secret/deep/a.txt", [str(root)], create_parents=True)) == api.NOT_FOUND
    assert not (root / "secret").exists()
    assert category(lambda: write(host, f"{home}/.newdot/a", [str(home)], create_parents=True)) == api.NOT_FOUND
    assert not (home / ".newdot").exists()
    assert write(host, f"{root}/fine/deep/a", [str(root)], create_parents=True) == f"{root}/fine/deep/a"



def test_check_workspace(host: Host, home: Path, tmp_path: Path) -> None:
    """V3-P74 §10.1: a folder the user registers."""
    checked = host.check_workspace("~/projects/a", [], ["/"])
    assert checked["path"] == f"{home}/projects/a" and checked["same_as"] is None
    assert isinstance(checked["filesystem"], str)  # whatever the test's disk is
    (home / "alias").symlink_to(home / "projects/a")
    assert host.check_workspace("~/alias", [f"{home}/projects/a"], ["/"])["path"] == f"{home}/projects/a"
    os.mkdir(home / "projects/b")
    assert host.check_workspace(f"{home}/projects/b", [f"{home}/projects/a", f"{home}/nowhere"], ["/"])["same_as"] \
        is None
    assert category(lambda: host.check_workspace("~/.ssh", [], ["/"])) == api.NOT_FOUND
    assert category(lambda: host.check_workspace("~/projects/a/main.c", [], ["/"])) == api.NOT_DIRECTORY
    assert category(lambda: host.check_workspace("~/missing", [], ["/"])) == api.NOT_FOUND
    assert category(lambda: host.check_workspace("/proc", [], ["/"])) == api.NOT_REGULAR
    assert category(lambda: host.check_workspace("~/projects/a", [], [f"{home}/HIMARI"])) == api.ACCESS
    assert category(lambda: host.check_workspace("~/projects/a", "x", ["/"])) == api.INVALID
    locked = home / "projects/locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        if not os.access(locked, os.W_OK):  # not as root
            assert category(lambda: host.check_workspace(str(locked), [], ["/"])) == api.ACCESS
    finally:
        locked.chmod(0o755)


def test_the_same_folder_at_another_path_is_found(host: Host, home: Path) -> None:
    """§9.1: a bind mount needs root, so a second name for the folder stands in: the (device, inode) test."""
    other = home / "projects/a-again"
    other.symlink_to(home / "projects/a")
    checked = host.check_workspace(f"{home}/projects/a", [str(other)], ["/"])
    assert checked["same_as"] == str(other)


def test_mount_type_reads_the_deepest_mount(tmp_path: Path) -> None:
    from himari.runner.hostfs import mount_type

    table = tmp_path / "mountinfo"
    table.write_text(
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        "40 22 0:50 / /home/ubuntu/cloud\\040storage/drive rw,nosuid - fuse.rclone gdrive: rw,user_id=0\n"
        "41 22 0:51 / /run rw - tmpfs tmpfs rw\n")
    assert mount_type("/home/ubuntu/cloud storage/drive/대학", str(table)) == "fuse.rclone"
    assert mount_type("/home/ubuntu/cloud storage", str(table)) == "ext4"
    assert mount_type("/run/x", str(table)) == "tmpfs"
    assert mount_type("/x", str(tmp_path / "missing")) is None


# --- the explorer's listing and stat (V3-P75 §5.1, §5.3) ---------------------------------------------

def pages(host: Host, path: str, size: int) -> list[str]:
    names: list[str] = []
    after = None
    while True:
        _, entries, after = host.listing(path, "/", size, after)
        names += [str(e["name"]) for e in entries]
        if after is None:
            return names


def test_a_folder_is_sorted_whole_then_paged(host: Host, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review B3: the first page holds the first names, whatever order the folder lists them in."""
    big = home / "projects/big"
    big.mkdir()
    names = [f"n{i:04d}" for i in range(1200)]
    for name in reversed(names):
        (big / name).write_text("x")
    (big / "zdir").mkdir()
    (big / "adir").mkdir()
    real, first, after = host.listing(str(big), "/", 500)
    assert real == str(big) and [e["name"] for e in first][:3] == ["adir", "zdir", "n0000"]
    assert first[0]["kind"] == "directory" and first[2]["kind"] == "file" and first[2]["size"] == 1
    assert isinstance(first[2]["modified"], int) and after == "f/n0497"
    assert pages(host, str(big), 500) == ["adir", "zdir", *names]
    # Between pages: an entry added before the cursor waits for a refresh, a removed one is gone; no repeats.
    _, page, after = host.listing(str(big), "/", 500)
    (big / "a-new").write_text("x")
    (big / "n0600").unlink()
    rest: list[str] = []
    while after is not None:
        _, entries, after = host.listing(str(big), "/", 500, after)
        rest += [str(e["name"]) for e in entries]
    seen = [str(e["name"]) for e in page] + rest
    assert len(seen) == len(set(seen)) and "n0600" not in seen and "a-new" not in seen
    monkeypatch.setattr(hostfs, "MAX_ENUMERATED", 100)
    with pytest.raises(api.RunnerError, match="more than 100 entries") as refused:
        host.listing(str(big), "/", 500)
    assert refused.value.category == api.TOO_LARGE


def test_listing_hides_and_checks_its_cursor(host: Host, home: Path) -> None:
    names = pages(host, "~", 2)
    assert ".ssh" not in names and ".bashrc" not in names and ".gitconfig" in names and "projects" in names
    assert names == sorted(names, key=lambda n: (not (home / n).is_dir(), n.encode()))
    for bad in ("x/n", "f", 3):
        with pytest.raises(api.RunnerError) as refused:
            host.listing("~", "/", 2, bad)
        assert refused.value.category == api.INVALID


def test_stat_checks_as_open_does(host: Host, home: Path) -> None:
    found = host.stat_path("~/projects/a/main.c", "/")
    info = os.stat(home / "projects/a/main.c")
    assert found["path"] == str(home / "projects/a/main.c") and found["kind"] == "file" and found["size"] == 13
    assert found["identity"] == [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    assert host.stat_path("~/projects", "/")["kind"] == "directory"
    (home / "projects/key").symlink_to(home / ".ssh/id")
    for hidden in ("~/.ssh/id", "~/.bashrc", "~/projects/key", "~/missing"):
        with pytest.raises(api.RunnerError) as refused:
            host.stat_path(hidden, "/")
        assert refused.value.category == api.NOT_FOUND


# --- the web editor's writes (V3-P75 §7) --------------------------------------------------------------

def parent(host: Host, home: Path, path: str) -> tuple[int, str]:
    fd, _, name = host.target(str(home / path), "/", [str(home)], follow=False)
    return fd, name


def identity(path: Path) -> list[int]:
    info = os.stat(path)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def read_all(fd: int) -> bytes:
    try:
        return os.read(fd, 1 << 21)
    finally:
        os.close(fd)


def test_replace_keeps_a_copy_taken_before_the_rename(host: Host, home: Path, tmp_path: Path) -> None:
    target = home / "projects/a/main.c"
    before = identity(target)
    writer = open(target, "r+b")  # a process holding the old file open, to write into it later
    fd, name = parent(host, home, "projects/a/main.c")
    try:
        now, copy = host.replace(fd, name, b"int main(){return 0;}\n", before)
    finally:
        os.close(fd)
    assert target.read_bytes() == b"int main(){return 0;}\n" and now == identity(target) and now != before
    writer.write(b"LATE")  # into the replaced (unnamed) file: the copy does not change (review C1)
    writer.flush()
    writer.close()
    assert copy is not None and read_all(copy) == b"int main(){}\n"
    assert not [n for n in os.listdir(home / "projects/a") if n.startswith(".himari-")]  # no temporary left


def test_replace_refuses_a_changed_file_and_writes_nothing(host: Host, home: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    target = home / "projects/a/main.c"
    stale = identity(target)
    target.write_text("changed elsewhere\n")
    fd, name = parent(host, home, "projects/a/main.c")
    try:
        with pytest.raises(api.RunnerError) as refused:
            host.replace(fd, name, b"mine", stale)
        assert refused.value.category == api.CHANGED and target.read_text() == "changed elsewhere\n"
        # Written in place while the copy was taken: refused at the last check, nothing renamed.
        fresh = identity(target)
        copy_of = Host._copy_of

        def copy_then_write(self: Host, held: int, size: int) -> int | None:
            made = copy_of(self, held, size)
            with open(target, "ab") as late:
                late.write(b"more")
            return made

        monkeypatch.setattr(Host, "_copy_of", copy_then_write)
        with pytest.raises(api.RunnerError) as refused:
            host.replace(fd, name, b"mine", fresh)
        assert refused.value.category == api.CHANGED and target.read_text() == "changed elsewhere\nmore"
    finally:
        os.close(fd)
    assert not [n for n in os.listdir(home / "projects/a") if n.startswith(".himari-")]


def test_create_file_never_takes_a_name_and_make_folder_one_level(host: Host, home: Path) -> None:
    fd, _, _ = host.target(str(home / "projects/a/new.txt"), "/", [str(home)], follow=False)
    try:
        made = host.create_file(fd, "new.txt")
        assert (home / "projects/a/new.txt").read_bytes() == b"" and made == identity(home / "projects/a/new.txt")
        (home / "projects/a/link").symlink_to(home / "projects/a/elsewhere")
        for taken in ("new.txt", "main.c", "link"):
            with pytest.raises(api.RunnerError) as refused:
                host.create_file(fd, taken)
            assert refused.value.category == api.EXISTS
        assert not (home / "projects/a/elsewhere").exists()  # a dangling link is not followed to make its target
        host.make_folder(fd, "sub")
        assert (home / "projects/a/sub").is_dir() and os.stat(home / "projects/a/sub").st_mode & 0o777 == 0o755
        with pytest.raises(api.RunnerError) as refused:
            host.make_folder(fd, "sub")
        assert refused.value.category == api.EXISTS
    finally:
        os.close(fd)


def test_delete_checked_keeps_a_copy_and_refuses_a_changed_file(host: Host, home: Path) -> None:
    target = home / "projects/a/main.c"
    fd, name = parent(host, home, "projects/a/main.c")
    try:
        with pytest.raises(api.RunnerError) as refused:
            host.delete_checked(fd, name, [0, 0, 0, 0])
        assert refused.value.category == api.CHANGED and target.exists()
        copy = host.delete_checked(fd, name, identity(target))
    finally:
        os.close(fd)
    assert not target.exists() and copy is not None and read_all(copy) == b"int main(){}\n"


def test_a_file_over_the_copy_limit_is_not_replaced_and_deleted_without_a_copy(host: Host, home: Path) -> None:
    """PR #94 review B1: no previous version could be kept, so the editor's replace refuses it; a delete
    (after the editor's own warning) goes without a copy."""
    big = home / "projects/a/big.bin"
    big.write_bytes(b"x" * (hostfs.MAX_SNAPSHOT_BYTES + 1))
    fd, name = parent(host, home, "projects/a/big.bin")
    try:
        with pytest.raises(api.RunnerError) as refused:
            host.replace(fd, name, b"small", identity(big))
        assert refused.value.category == api.TOO_LARGE and big.stat().st_size == hostfs.MAX_SNAPSHOT_BYTES + 1
        assert host.delete_checked(fd, name, identity(big)) is None and not big.exists()
    finally:
        os.close(fd)


def test_visible_path_resolves_the_folder_and_hides_as_open_does(host: Host, home: Path) -> None:
    """PR #94 review B3: what a previous version is looked up by; the file itself may be gone."""
    assert host.visible_path("~/projects/a/gone.txt", "/") == str(home / "projects/a/gone.txt")
    (home / "projects/link").symlink_to(home / "projects/a")
    assert host.visible_path("~/projects/link/main.c", "/") == str(home / "projects/a/main.c")
    (home / "projects/to-ssh").symlink_to(home / ".ssh")
    for hidden in ("~/.ssh/id", "~/.bashrc", "~/projects/to-ssh/id", "~/missing/x"):
        with pytest.raises(api.RunnerError) as refused:
            host.visible_path(hidden, "/")
        assert refused.value.category == api.NOT_FOUND
