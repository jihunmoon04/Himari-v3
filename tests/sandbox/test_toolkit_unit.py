"""The documents toolkit in the sandbox layouts (V3-P70 §5, §6.3; V3-050
amended): what bwrap is asked for, and what the self-test expects."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from himari.sandbox import backend as sb
from himari.sandbox import self_test
from himari.sandbox.api import TOOLKIT_MOUNT, SandboxProfileError, ServerProfile
from himari.sandbox.server import check_profile

CAP = sb.HostCapability(bwrap="/usr/bin/bwrap", landlock_abi=8, helper="/srv/himari/landlock_helper.py")


def options(args: list[str]) -> list[str]:
    return args[: args.index("--")]


def test_the_toolkit_is_bound_read_only_with_its_two_variables() -> None:
    args = sb.bwrap_arguments(CAP, 9, "/workspace/conversations/c1", 11, ["python3", "s.py"], toolkit_fd=12)
    opts = options(args)
    at = opts.index("--ro-bind-fd")
    assert opts[at + 1 : at + 3] == ["12", TOOLKIT_MOUNT] == ["12", "/run/himari/documents"]
    assert opts.index("--ro-bind-fd") > opts.index("--bind-fd")  # after the helper's /run/himari and the Workspace
    env = {args[i + 1]: args[i + 2] for i, a in enumerate(args) if a == "--setenv"}
    assert env == {**sb.SANDBOX_ENV, "PYTHONPATH": "/run/himari/documents/site-packages",
                   "HIMARI_FONTS": "/run/himari/documents/fonts"}
    assert "PYTHONPATH" not in sb.SANDBOX_ENV  # server profiles may still set it (Calliope does, review N1)
    import json

    policy = json.loads(args[args.index("--") + 5])
    assert policy["write_roots"] == ["/workspace", "/tmp"]  # nothing writable added


def test_without_a_toolkit_nothing_changes() -> None:
    args = sb.bwrap_arguments(CAP, 9, "/workspace/conversations/c1", 11, ["true"])
    assert "--ro-bind-fd" not in options(args) and TOOLKIT_MOUNT not in args
    assert sb.bwrap_arguments(CAP, 9, "/workspace/conversations/c1", 11, ["true"], None) == args


def test_tmp_size_comes_right_before_the_tmpfs() -> None:
    args = sb.sandbox_arguments(CAP, env={}, binds=[], write_roots=["/tmp"], cwd="/tmp", sync_fd=3,
                                argv=["true"], tmp_bytes=64 << 20)
    at = args.index("--tmpfs")
    assert args[at - 2 : at + 2] == ["--size", str(64 << 20), "--tmpfs", "/tmp"]
    plain = sb.sandbox_arguments(CAP, env={}, binds=[], write_roots=["/tmp"], cwd="/tmp", sync_fd=3, argv=["true"])
    assert "--size" not in plain


def test_tmp_bytes_in_a_profile(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    check_profile(ServerProfile(read_only=(str(folder),), tmp_bytes=64 << 20), protected=[str(tmp_path / "data")])
    with pytest.raises(SandboxProfileError):
        check_profile(ServerProfile(read_only=(str(folder),), tmp_bytes=1000), protected=[str(tmp_path / "data")])


def test_supports_tmp_size_reads_bwraps_help(tmp_path: Path) -> None:
    for text, expected in (("--size BYTES  Set size", True), ("--tmpfs DEST", False)):
        fake = tmp_path / f"bwrap{expected}"
        fake.write_text(f"#!/bin/sh\necho '{text}'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        assert sb.supports_tmp_size(sb.HostCapability(str(fake), 8, "/x")) is expected
    assert sb.supports_tmp_size(sb.HostCapability(str(tmp_path / "absent"), 8, "/x")) is False


def test_the_self_test_expects_the_toolkit_only_when_bound(tmp_path: Path) -> None:
    assert "write_toolkit" not in self_test.expected(False)  # a key it does not observe would fail the test
    assert self_test.expected(True)["write_toolkit"] is False
    assert self_test.expected(True)["extra_fds"] == [] == self_test.expected(False)["extra_fds"]
    workspace = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    toolkit_dir = tmp_path / "tk"
    toolkit_dir.mkdir()
    toolkit = os.open(toolkit_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        plain = self_test.expected_sources(workspace)
        bound = self_test.expected_sources(workspace, toolkit)
        assert TOOLKIT_MOUNT not in plain.binds
        info = os.fstat(toolkit)
        assert bound.binds[TOOLKIT_MOUNT] == (info.st_dev, info.st_ino)
        # The mount is accepted with its own source only.
        entry = [TOOLKIT_MOUNT, "ext4", "/", False, [info.st_dev, info.st_ino, 0]]
        assert self_test._unexpected_mounts([entry], bound) == []
        assert self_test._unexpected_mounts([entry], plain) == [TOOLKIT_MOUNT]
        alias = [TOOLKIT_MOUNT, "ext4", "/", False, [info.st_dev, info.st_ino + 1, 0]]
        assert self_test._unexpected_mounts([alias], bound) == [TOOLKIT_MOUNT]
    finally:
        os.close(workspace)
        os.close(toolkit)


def test_the_backend_passes_the_toolkit_to_every_launch() -> None:
    backend = sb.SandboxBackend(CAP, toolkit_fd=12)
    assert backend.toolkit_fd == 12 and sb.SandboxBackend(CAP).toolkit_fd is None
