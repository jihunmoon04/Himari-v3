"""Operator overrides of MCP defaults, separate from server addresses and credentials."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from himari.bootstrap.operator import check_private_dir

MAX_BYTES = 8192


class McpDefaultsConflict(ValueError):
    """Another settings page changed the defaults; read them before saving again."""


def read_defaults(path: Path) -> dict[str, bool]:
    check_private_dir(str(path.parent))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("MCP defaults must be a private regular file")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise ValueError("MCP defaults are too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or len(value) > 64 or any(
        not isinstance(key, str) or not 1 <= len(key) <= 24 or type(enabled) is not bool
        for key, enabled in value.items()
    ):
        raise ValueError("Invalid MCP defaults")
    return {key: enabled for key, enabled in value.items()}


def write_defaults(path: Path, values: dict[str, bool]) -> None:
    check_private_dir(str(path.parent))
    read_defaults(path)  # refuse symlinks, other owners, shared files or malformed old data
    raw = (json.dumps(values, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("MCP defaults are too large")
    fd, temporary = tempfile.mkstemp(prefix=".mcp-defaults-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
