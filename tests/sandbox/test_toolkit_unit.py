"""The server profile's /tmp size and bwrap's support for it (V3-P70 §5, §6.3)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from himari.sandbox import backend as sb
from himari.sandbox.api import SandboxProfileError, ServerProfile
from himari.sandbox.server import check_profile

CAP = sb.HostCapability(bwrap="/usr/bin/bwrap", landlock_abi=8, helper="/srv/himari/landlock_helper.py")


def options(args: list[str]) -> list[str]:
    return args[: args.index("--")]


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
