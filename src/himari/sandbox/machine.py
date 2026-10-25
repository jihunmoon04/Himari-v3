"""The machine profile (V3-P74 §6): commands run as the user on this server.

Reading is broad and writing narrow: the host root is bound read-only, the
attached Workspaces are bound again writable, and the hidden paths are
masked **last** — an empty read-only folder or an empty read-only file —
so a Workspace that contains a hidden path cannot uncover it (§6.1,
review N2). The helper checks the masks in the final tree before the
command runs, then adds Landlock (writes only in the Workspaces, /tmp and
the tool cache; TCP inside the private loopback allowed, Q8) and the
seccomp filter for host Unix sockets (§6.6).

``/tmp``, ``/run``, ``/dev`` and ``/proc`` are the sandbox's own. The
network namespace has loopback only; with a proxy (§11), the runner's
proxy socket is bound at ``PROXY_IN_SANDBOX`` and the helper's bridge
listens on ``127.0.0.1:BRIDGE_PORT``, named in the proxy variables.

There is no admission walk (V3-P41's whole-tree check): the boundary is
the mount namespace, Landlock and the user's own permissions.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from himari.sandbox.api import (
    MAX_OUTPUT_BYTES,
    TOOLKIT_MOUNT,
    CommandResult,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    validate_argv,
)
from himari.sandbox.backend import (
    HELPER_IN_SANDBOX,
    INTERPRETER,
    HostCapability,
    discover_capability,
    protected_run,
    toolkit_env,
)

CACHE_VARIABLES = ("XDG_CACHE_HOME", "PIP_CACHE_DIR", "GRADLE_USER_HOME", "npm_config_cache")
PROXY_IN_SANDBOX = "/run/himari/proxy.sock"
BRIDGE_PORT = 3128
PROXY_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
PROXY_VARIABLES = {
    **{name: PROXY_URL for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                                    "all_proxy")},
    "NO_PROXY": "localhost,127.0.0.1,::1",
    "no_proxy": "localhost,127.0.0.1,::1",
    # Maven, Gradle and other Java programs (§11.3)
    "JAVA_TOOL_OPTIONS": (f"-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort={BRIDGE_PORT} "
                          f"-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort={BRIDGE_PORT} "
                          "-Dhttp.nonProxyHosts=localhost|127.0.0.1"),
}
_ENTER_CGROUP = 'echo 0 > "$1" && shift && exec "$@"'  # the shell joins the cgroup, then becomes bwrap


@dataclass(frozen=True)
class Mask:
    """A hidden path as the host has it: its real path, "dir" or "file",
    and its host device (a folder's mask must be on another one)."""

    path: str
    kind: str
    device: int


@dataclass(frozen=True)
class MachineLayout:
    """One command's view. Every fd is an open, already checked directory;
    the caller keeps them open until the command returns."""

    home: str
    cwd: str
    writable: tuple[tuple[str, int], ...]  # (real path, fd), bound in this order
    masks: tuple[Mask, ...]
    cache: tuple[str, int] | None = None  # the runner's tool cache, writable
    toolkit_fd: int | None = None  # the documents toolkit, read-only at TOOLKIT_MOUNT (V3-P70)
    cgroup_procs: str | None = None  # the commands cgroup's cgroup.procs (§6.5); None: stay put
    proxy: str | None = None  # the runner's proxy socket (§11); None: no network at all


def machine_env(layout: MachineLayout) -> dict[str, str]:
    env = {
        "PATH": f"{layout.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": layout.home,
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    if layout.cache is not None:
        cache = layout.cache[0]
        env.update({"XDG_CACHE_HOME": cache, "PIP_CACHE_DIR": f"{cache}/pip",
                    "GRADLE_USER_HOME": f"{cache}/gradle", "npm_config_cache": f"{cache}/npm",
                    "MAVEN_OPTS": f"-Dmaven.repo.local={cache}/m2"})
    if layout.toolkit_fd is not None:
        env.update(toolkit_env())
    if layout.proxy is not None:
        env.update(PROXY_VARIABLES)
    return env


def machine_policy(layout: MachineLayout, sync_fd: int) -> dict[str, object]:
    write_roots = [path for path, _ in layout.writable] + ["/tmp"]
    if layout.cache is not None:
        write_roots.append(layout.cache[0])
    policy: dict[str, object] = {
        "read_roots": ["/"],
        "write_roots": write_roots,
        "device_root": "/dev",
        "sync_fd": sync_fd,
        "tcp": "allow",
        "hidden": [[m.path, m.kind, m.device] for m in layout.masks],
        "seccomp": "unix",
    }
    if layout.proxy is not None:
        policy["proxy"] = {"socket": PROXY_IN_SANDBOX, "port": BRIDGE_PORT}
    return policy


def machine_arguments(capability: HostCapability, layout: MachineLayout, sync_fd: int,
                      argv: Sequence[str], empty_fds: Sequence[int]) -> list[str]:
    """bwrap arguments in the contract's order (§6.1): the read-only root
    and the sandbox's own /dev, /proc, /tmp, /run; the Workspaces (shallow
    first); the cache; the hidden masks last. ``empty_fds`` read as empty
    (/dev/null), one per hidden file (bwrap closes each after reading it):
    each hidden file becomes an empty read-only file of the sandbox's own —
    not the null device, which a bind mount (``nodev``) could not open."""
    args = [
        capability.bwrap,
        "--unshare-all",  # user, ipc, pid, net, uts, cgroup
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
        "--cap-drop", "ALL",
        "--clearenv",
    ]
    for key, value in machine_env(layout).items():
        args += ["--setenv", key, value]
    args += [
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--ro-bind", capability.helper, HELPER_IN_SANDBOX,
    ]
    if layout.proxy is not None:
        args += ["--ro-bind", layout.proxy, PROXY_IN_SANDBOX]
    if layout.toolkit_fd is not None:
        args += ["--ro-bind-fd", str(layout.toolkit_fd), TOOLKIT_MOUNT]
    for path, fd in sorted(layout.writable, key=lambda item: item[0].count("/")):
        args += ["--bind-fd", str(fd), path]
    if layout.cache is not None:
        args += ["--bind-fd", str(layout.cache[1]), layout.cache[0]]
    empty = iter(empty_fds)
    for mask in layout.masks:
        if mask.kind == "dir":
            args += ["--tmpfs", mask.path, "--remount-ro", mask.path]
        elif mask.kind == "file":
            args += ["--ro-bind-data", str(next(empty)), mask.path]
        else:
            raise ValueError(f"not a mask kind: {mask.kind}")
    args += [
        "--chdir", layout.cwd,
        "--",
        INTERPRETER, "-I", "-S", HELPER_IN_SANDBOX,
        json.dumps(machine_policy(layout, sync_fd), separators=(",", ":")),
        "--",
        *argv,
    ]
    return args


def layout_fds(layout: MachineLayout) -> tuple[int, ...]:
    fds = [fd for _, fd in layout.writable]
    if layout.cache is not None:
        fds.append(layout.cache[1])
    if layout.toolkit_fd is not None:
        fds.append(layout.toolkit_fd)
    return tuple(fds)


MachineSelfTest = Callable[["MachineSandbox"], Awaitable[bool]]


class MachineSandbox:
    """States as the other profiles (V3-050): ENFORCED only after the
    real-spawn self-test passed; a setup failure latches SETUP_FAILED."""

    def __init__(self, capability: HostCapability | None) -> None:
        self._capability = capability
        self._state = SandboxState.UNAVAILABLE if capability is None else SandboxState.CAPABLE_INACTIVE
        self._started = False

    @classmethod
    def discover(cls) -> MachineSandbox:
        return cls(discover_capability())

    @property
    def state(self) -> SandboxState:
        return self._state

    async def start(self, self_test: MachineSelfTest) -> SandboxState:
        if self._started:
            raise RuntimeError("the machine sandbox starts once per process")
        self._started = True
        if self._state is not SandboxState.CAPABLE_INACTIVE:
            return self._state
        try:
            passed = await self_test(self)
        except Exception:
            passed = False
        self._state = SandboxState.ENFORCED if passed else SandboxState.SETUP_FAILED
        return self._state

    async def run(self, argv: list[str], layout: MachineLayout, timeout: float, *,
                  max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
        if self._state is not SandboxState.ENFORCED:
            raise SandboxUnavailableError(self._state.value)
        try:
            return await self.launch(argv, layout, timeout, max_output=max_output)
        except SandboxSetupError:
            self._state = SandboxState.SETUP_FAILED  # latched
            raise

    async def launch(self, argv: list[str], layout: MachineLayout, timeout: float, *,
                     max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
        """The one spawn of this profile (``run`` and the self-test)."""
        capability = self._capability
        if capability is None:
            raise SandboxUnavailableError(SandboxState.UNAVAILABLE.value)
        if not os.path.isabs(layout.cwd):
            raise ValueError("cwd must be absolute")
        argv = validate_argv(argv)

        empty_fds: list[int] = []
        try:
            for _ in range(sum(1 for m in layout.masks if m.kind == "file")):
                empty_fds.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))

            def build(sync_w: int) -> list[str]:
                bwrap = machine_arguments(capability, layout, sync_w, argv, empty_fds)
                if layout.cgroup_procs is None:
                    return bwrap
                return ["/bin/sh", "-c", _ENTER_CGROUP, "sh", layout.cgroup_procs, *bwrap]

            return await protected_run(build, (*layout_fds(layout), *empty_fds), timeout, max_output=max_output)
        finally:
            for fd in empty_fds:
                os.close(fd)


def cache_layout(env: Mapping[str, str]) -> dict[str, str]:
    """The cache variables of an environment (tests)."""
    return {k: env[k] for k in CACHE_VARIABLES if k in env}
