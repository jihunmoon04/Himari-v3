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

from himari.runner.proxy import DEFAULT_ALLOW, allow_list

DEFAULT_HIDDEN = ("/srv/himari-v3", "/etc/himari-v3", "/etc/himari-runner", "/var/lib/calliope",
                  "/opt/himari-v3/deployment.json")
DEFAULT_VISIBLE = (".gitconfig", ".local/bin", ".local/lib")
CACHE_NAME = ".cache/himari-runner"
STATE_NAME = ".local/state/himari-runner"  # hidden by the home's dot rule (§7)
PROXY_SOCKET = "proxy-{}.sock"  # one per command, tagged with 16 hex digits (§22.2)
_MAX_SOCKET_PATH = 107  # sun_path, less its NUL


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    client_uid: int  # the only peer allowed (SO_PEERCRED): HIMARI's service user
    home: str  # the user's home (the runner's own)
    himari_home: str  # conversation folders live in <himari_home>/conversations
    socket: str  # bound only without systemd's socket (tests, manual runs)
    cache_dir: str  # writable in every command (§6.3), visible
    visible: tuple[str, ...]
    hidden: tuple[str, ...]
    writable_under: tuple[str, ...]
    documents_toolkit: str | None
    cgroup: str  # "none" | "delegated"
    memory_max: str
    pids_max: int
    runner_memory_low: str
    state_dir: str  # the runner's own: the proxy's sockets (§11)
    network: tuple[str, ...] | None  # the proxy's allow list (§11.3); None: commands have no network

    def proxy_socket(self, tag: str) -> str:
        return os.path.join(self.state_dir, PROXY_SOCKET.format(tag))


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
    cache_dir = _absolute(raw.get("cache_dir", os.path.join(home, CACHE_NAME)), "cache_dir")
    visible = _strings(raw.get("visible", list(DEFAULT_VISIBLE)), "visible")
    if cache_dir.startswith(home + "/"):
        visible = (*visible, cache_dir[len(home) + 1:])
    writable = tuple(_absolute(p, "writable_under") for p in _strings(raw.get("writable_under", ["/"]),
                                                                        "writable_under"))
    hidden = tuple(_absolute(p, "hidden") for p in _strings(raw.get("hidden", list(DEFAULT_HIDDEN)), "hidden"))
    cgroup = raw.get("cgroup", {})
    if not isinstance(cgroup, dict):
        raise ConfigError("[cgroup] is a table")
    mode = cgroup.get("mode", "none")
    if mode not in ("none", "delegated"):
        raise ConfigError("cgroup.mode is none or delegated")
    pids = cgroup.get("pids_max", 1024)
    if not isinstance(pids, int) or pids < 16:
        raise ConfigError("cgroup.pids_max is a number of at least 16")
    toolkit = raw.get("documents_toolkit")
    state_dir = _absolute(raw.get("state_dir", os.path.join(home, STATE_NAME)), "state_dir")
    if len(os.path.join(state_dir, PROXY_SOCKET.format("0" * 16)).encode()) > _MAX_SOCKET_PATH:
        raise ConfigError("state_dir is too long for a socket path")
    network = raw.get("network", {})
    if not isinstance(network, dict):
        raise ConfigError("[network] is a table")
    enabled = network.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("network.enabled is true or false")
    try:
        allow = allow_list(_strings(network.get("allow", list(DEFAULT_ALLOW)), "network.allow"))
    except ValueError as error:
        raise ConfigError(f"network.allow: {error}") from None
    return RunnerConfig(
        client_uid=client, home=home, himari_home=himari_home,
        socket=_absolute(raw.get("socket", "/run/himari-runner/runner.sock"), "socket"),
        cache_dir=cache_dir, visible=visible, hidden=hidden, writable_under=writable,
        documents_toolkit=None if not toolkit else _absolute(toolkit, "documents_toolkit"),
        cgroup=mode, memory_max=str(cgroup.get("memory_max", "4G")), pids_max=pids,
        runner_memory_low=str(cgroup.get("runner_memory_low", "256M")),
        state_dir=state_dir, network=allow if enabled and allow else None,
    )
