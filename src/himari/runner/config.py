"""``runner.toml`` (V3-P74 §4.1): root-owned, read-only to the runner.

Neither the model nor HIMARI can change it: the hidden list and the range
of writable folders are the runner's own, checked again on every call.
"""

from __future__ import annotations

import os
import pwd
import tomllib
from dataclasses import dataclass
from typing import Any

DEFAULT_HIDDEN = ("/srv/himari-v3", "/etc/himari-v3", "/etc/himari-runner", "/var/lib/calliope",
                  "/opt/himari-v3/deployment.json")
DEFAULT_VISIBLE = (".gitconfig", ".local/bin", ".local/lib")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    client_uid: int  # the only peer allowed (SO_PEERCRED): HIMARI's service user
    home: str  # the user's home (the runner's own)
    himari_home: str  # conversation folders live in <himari_home>/conversations
    socket: str  # bound only without systemd's socket (tests, manual runs)
    visible: tuple[str, ...]
    hidden: tuple[str, ...]
    writable_under: tuple[str, ...]


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name} is a list of strings")
    return tuple(value)


def _absolute(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or os.path.normpath(value) != value:
        raise ConfigError(f"{name} is an absolute, normal path")
    return value


def load(path: str) -> RunnerConfig:
    with open(path, "rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    return parse(raw)


def parse(raw: dict[str, Any]) -> RunnerConfig:
    home = pwd.getpwuid(os.getuid()).pw_dir
    client = raw.get("client_uid", raw.get("client_user"))
    if isinstance(client, str):
        try:
            client = pwd.getpwnam(client).pw_uid
        except KeyError:
            raise ConfigError(f"no user {client}") from None
    if not isinstance(client, int) or isinstance(client, bool):
        raise ConfigError("client_uid (or client_user) is required")
    himari_home = _absolute(raw.get("himari_home", os.path.join(home, "HIMARI")), "himari_home")
    visible = _strings(raw.get("visible", list(DEFAULT_VISIBLE)), "visible")
    writable = tuple(_absolute(p, "writable_under") for p in _strings(raw.get("writable_under", ["/"]),
                                                                        "writable_under"))
    hidden = tuple(_absolute(p, "hidden") for p in _strings(raw.get("hidden", list(DEFAULT_HIDDEN)), "hidden"))
    # Every unknown key is ignored: the commands' ``cache_dir``, ``documents_toolkit``, ``state_dir``, [cgroup]
    # and [network] (removed with ``run``), and [push] (git push, removed by V3-P100).
    return RunnerConfig(
        client_uid=client, home=home, himari_home=himari_home,
        socket=_absolute(raw.get("socket", "/run/himari-runner/runner.sock"), "socket"),
        visible=visible, hidden=hidden, writable_under=writable,
    )
