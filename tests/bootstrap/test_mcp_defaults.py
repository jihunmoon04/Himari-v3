from __future__ import annotations

import os
from pathlib import Path

import pytest

from himari.bootstrap.mcp_defaults import read_defaults, write_defaults


def test_mcp_default_file_refuses_links_shared_modes_and_invalid_values(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "mcp-defaults.json"
    target = tmp_path / "other"
    target.write_text('{"calliope":true}')
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(OSError):
        write_defaults(path, {"calliope": False})
    assert target.read_text() == '{"calliope":true}'
    path.unlink()
    os.link(target, path)
    with pytest.raises(ValueError):
        read_defaults(path)
    path.unlink()
    path.write_text('{"calliope":true}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_defaults(path)
    path.chmod(0o600)
    path.write_text('{"calliope":"false"}')
    with pytest.raises(ValueError):
        read_defaults(path)


def test_failed_atomic_publish_keeps_previous_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "mcp-defaults.json"
    write_defaults(path, {"calliope": True})

    def fail(source: str, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_defaults(path, {"calliope": False})
    assert read_defaults(path) == {"calliope": True}
    assert sorted(item.name for item in tmp_path.iterdir()) == ["mcp-defaults.json"]
