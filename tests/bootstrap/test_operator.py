"""Operator secret (local Web/CLI authentication root)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from himari.bootstrap.operator import (
    SECRET_NAME,
    OperatorSecretError,
    ensure_operator_secret,
    read_operator_secret,
    verify_operator_token,
)


def test_created_once_private_and_stable(tmp_path: Path) -> None:
    control = tmp_path / "control"
    secret = ensure_operator_secret(str(control))
    assert len(secret) == 43 and ensure_operator_secret(str(control)) == secret == read_operator_secret(str(control))
    assert stat.S_IMODE(control.stat().st_mode) == 0o700
    assert stat.S_IMODE((control / SECRET_NAME).stat().st_mode) == 0o600
    assert (control / SECRET_NAME).read_bytes() == secret.encode() + b"\n"
    assert [p.name for p in control.iterdir()] == [SECRET_NAME]  # no temp left behind
    assert ensure_operator_secret(str(tmp_path / "other")) != secret


@pytest.mark.parametrize("damage", ["mode", "format", "short", "symlink", "hardlink", "dir_mode", "dir_symlink"])
def test_unsafe_or_damaged_secrets_are_refused_not_repaired(tmp_path: Path, damage: str) -> None:
    control = tmp_path / "control"
    ensure_operator_secret(str(control))
    path = control / SECRET_NAME
    target = control
    if damage == "mode":
        path.chmod(0o640)
    elif damage == "format":
        path.write_bytes(path.read_bytes().rstrip(b"\n") + b"\n\n")
    elif damage == "short":
        path.write_bytes(b"abc\n")
    elif damage == "symlink":
        real = tmp_path / "real.secret"
        real.write_bytes(path.read_bytes())
        real.chmod(0o600)
        path.unlink()
        path.symlink_to(real)
    elif damage == "hardlink":
        os.link(path, tmp_path / "copy")
    elif damage == "dir_mode":
        control.chmod(0o750)
    else:
        moved = tmp_path / "moved"
        control.rename(moved)
        control.symlink_to(moved)
        target = moved
    before = sorted(p.name for p in target.iterdir())
    for call in (ensure_operator_secret, read_operator_secret):
        with pytest.raises(OperatorSecretError):
            call(str(control))
    assert sorted(p.name for p in target.iterdir()) == before


def test_missing_secret_is_not_created_by_readers(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        read_operator_secret(str(control))
    assert list(control.iterdir()) == []


def test_token_verification() -> None:
    assert verify_operator_token("a" * 43, "a" * 43)
    assert not verify_operator_token("a" * 42 + "b", "a" * 43)
    assert not verify_operator_token("", "a" * 43) and not verify_operator_token(None, "a" * 43)


def test_a_concurrently_published_secret_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = tmp_path / "control"
    first = ensure_operator_secret(str(control))
    # Another process published between our existence check and our publish.
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    assert ensure_operator_secret(str(control)) == first
    assert [p.name for p in control.iterdir()] == [SECRET_NAME]
