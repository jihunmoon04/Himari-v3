"""The runner's hidden list and file operations (V3-P74 §7, §8), on a temporary home."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from himari.runner import api
from himari.runner.hidden import HiddenRules
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
