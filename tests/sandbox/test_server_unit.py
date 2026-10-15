"""The server profile (V3-P59 §4) without bubblewrap (runs in CI): the
configuration checks, the identity checks, the bwrap layout, the state
rules and the process API on a plain child process."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from himari.sandbox import backend as sb
from himari.sandbox import server as srv
from himari.sandbox.api import (
    MAX_LINE_BYTES,
    STDERR_TAIL_BYTES,
    ProcessGone,
    SandboxAdmissionError,
    SandboxProfileError,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    ServerProfile,
)
from himari.sandbox.identity import BoundTree, check_trees

CAP = sb.HostCapability(bwrap="/usr/bin/bwrap", landlock_abi=8, helper="/srv/himari/landlock_helper.py")
NO_MOUNTS = ""


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("data", "ro", "rw", "other")}
    for path in paths.values():
        path.mkdir()
    (paths["data"] / "workspaces").mkdir()
    (paths["data"] / "himari.db").write_text("db")
    (paths["ro"] / "lib").mkdir()
    (paths["ro"] / "lib" / "code.py").write_text("x = 1")
    (paths["rw"] / "cache.json").write_text("{}")
    return paths


# --- configuration (§4.2) ----------------------------------------------------------------


def test_a_profile_of_existing_directories_apart_from_the_data_is_accepted(layout: dict[str, Path]) -> None:
    profile = ServerProfile((str(layout["ro"]),), (str(layout["rw"]),), (str(layout["data"]),))
    assert srv.check_profile(profile) == profile


@pytest.mark.parametrize("where, path, message", [
    ("read_only", "relative/dir", "absolute"),
    ("read_only", "{ro}/../ro", "normalized"),
    ("read_only", "/", "other than /"),
    ("read_only", "{tmp}/missing", "not an existing directory"),
    ("read_only", "{ro}/lib/code.py", "not an existing directory"),
    ("read_only", "/proc", "builds"),
    ("read_only", "{data}/workspaces", "HIMARI's data"),  # inside the data folder
    ("read_only", "{tmp}", "HIMARI's data"),  # holds the data folder
    ("writable", "{alias}", "HIMARI's data"),  # a symbolic link to it
    ("writable", "{ro}/lib", "another bound directory"),
])
def test_a_profile_mistake_is_refused(layout: dict[str, Path], tmp_path: Path, where: str, path: str,
                                      message: str) -> None:
    (tmp_path / "alias").symlink_to(layout["data"])
    path = path.format(tmp=tmp_path, alias=tmp_path / "alias", **layout)
    read_only = (str(layout["ro"]), path) if where == "read_only" else (str(layout["ro"]),)
    writable = (path,) if where == "writable" else (str(layout["rw"]),)
    with pytest.raises(SandboxProfileError, match=message):
        srv.check_profile(ServerProfile(read_only, writable, (str(layout["data"]),)))


def test_a_writable_directory_must_belong_to_the_server_user(layout: dict[str, Path]) -> None:
    profile = ServerProfile((), (str(layout["rw"]),), (str(layout["data"]),))
    with pytest.raises(SandboxProfileError, match="belong to the user"):
        srv.check_profile(profile, uid=os.geteuid() + 1)
    srv.check_profile(ServerProfile((str(layout["rw"]),), (), ()), uid=os.geteuid() + 1)  # read-only: anyone's


def test_at_most_16_read_only_and_4_writable(tmp_path: Path) -> None:
    dirs = [tmp_path / f"d{i}" for i in range(17)]
    for d in dirs:
        d.mkdir()
    with pytest.raises(SandboxProfileError, match="at most"):
        srv.check_profile(ServerProfile(tuple(map(str, dirs)), (), ()))
    with pytest.raises(SandboxProfileError, match="at most"):
        srv.check_profile(ServerProfile((), tuple(map(str, dirs[:5])), ()))


@pytest.mark.parametrize("env, cwd, message", [
    ({"PATH": "/x"}, "{rw}", "cannot be set"),  # the fixed environment stays
    ({"HOME": "/x"}, "{rw}", "cannot be set"),
    ({"PWD": "/x"}, "{rw}", "cannot be set"),
    ({"1BAD": "x"}, "{rw}", "cannot be set"),
    ({"OK": "a\0b"}, "{rw}", "no NUL"),
    ({}, "{other}", "not inside a bound directory"),
    ({}, "relative", "not inside a bound directory"),
    ({}, "{rw}/../other", "not inside a bound directory"),
])
def test_a_launch_request_is_checked(layout: dict[str, Path], env: dict[str, str], cwd: str, message: str) -> None:
    profile = ServerProfile((str(layout["ro"]),), (str(layout["rw"]),), (str(layout["data"]),))
    with pytest.raises(SandboxProfileError, match=message):
        srv._check_request(profile, ["server"], env, cwd.format(**layout))
    assert srv._check_request(profile, ["server", "--x"], {"PYTHONPATH": "."}, str(layout["ro"] / "lib")) == [
        "server", "--x"]
    assert srv._check_request(profile, ["server"], {}, "/tmp")


# --- identity checks (§4.2) ----------------------------------------------------------------


def _check(trees: list[tuple[Path, bool]], protected: list[Path], **kwargs: Any) -> None:
    fds = [os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path, _ in trees]
    try:
        check_trees([BoundTree(fd, writable) for fd, (_, writable) in zip(fds, trees)], [str(p) for p in protected],
                    **{"mountinfo": NO_MOUNTS, **kwargs})
    finally:
        for fd in fds:
            os.close(fd)


def test_separate_trees_pass(layout: dict[str, Path]) -> None:
    _check([(layout["ro"], False), (layout["rw"], True)], [layout["data"]])


def test_r0_a_tree_reached_through_a_symlink_into_the_data_is_refused(layout: dict[str, Path], tmp_path: Path) -> None:
    (tmp_path / "alias").symlink_to(layout["data"] / "workspaces")
    with pytest.raises(SandboxAdmissionError, match="inside HIMARI's data"):
        _check([(tmp_path / "alias", False)], [layout["data"]])


def test_r0_a_tree_holding_the_data_is_refused(layout: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(SandboxAdmissionError, match="contains HIMARI's data"):
        _check([(tmp_path, False)], [layout["data"]])


def test_r1_a_hard_link_to_a_data_file_is_refused_in_any_tree(layout: dict[str, Path]) -> None:
    os.link(layout["data"] / "himari.db", layout["ro"] / "lib" / "db-link")
    for writable in (False, True):
        with pytest.raises(SandboxAdmissionError, match="hard link to HIMARI's data"):
            _check([(layout["ro"], writable)], [layout["data"]])


def test_r2_an_outside_hard_link_is_refused_only_in_a_writable_tree(layout: dict[str, Path]) -> None:
    (layout["other"] / "shared.txt").write_text("host")
    os.link(layout["other"] / "shared.txt", layout["rw"] / "shared.txt")
    with pytest.raises(SandboxAdmissionError, match="hard-linked from outside"):
        _check([(layout["rw"], True)], [layout["data"]])
    _check([(layout["rw"], False)], [layout["data"]])  # read-only: allowed (uv's package cache, §4.2)


def test_r2_hard_links_kept_inside_a_writable_tree_are_fine(layout: dict[str, Path]) -> None:
    (layout["rw"] / "sub").mkdir()
    os.link(layout["rw"] / "cache.json", layout["rw"] / "sub" / "cache-copy.json")
    _check([(layout["rw"], True)], [layout["data"]])


def test_m0_a_mount_below_a_tree_is_refused(layout: dict[str, Path]) -> None:
    line = f"36 35 0:30 / {layout['ro']}/lib rw,relatime shared:1 - tmpfs tmpfs rw\n"
    with pytest.raises(SandboxAdmissionError, match="mount point"):
        _check([(layout["ro"], False)], [layout["data"]], mountinfo=line)
    _check([(layout["rw"], True)], [layout["data"]], mountinfo=line)  # elsewhere: not this tree's


def test_the_entry_bound_covers_the_data_and_the_trees_together(layout: dict[str, Path]) -> None:
    for i in range(30):
        (layout["data"] / "workspaces" / f"w{i}").mkdir()
    with pytest.raises(SandboxAdmissionError, match="too many entries"):
        _check([(layout["ro"], False)], [layout["data"]], limit=31)
    _check([(layout["ro"], False)], [layout["data"]], limit=40)


def test_an_unreadable_tree_is_a_refusal(layout: dict[str, Path]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    (layout["ro"] / "lib").chmod(0)
    try:
        with pytest.raises(SandboxAdmissionError, match="could not be verified"):
            _check([(layout["ro"], False)], [layout["data"]])
    finally:
        (layout["ro"] / "lib").chmod(0o755)


def test_a_missing_protected_root_protects_nothing_but_fails_nothing(layout: dict[str, Path]) -> None:
    _check([(layout["ro"], False)], [layout["data"] / "not-yet"])


# --- the bwrap layout ------------------------------------------------------------------


def test_the_server_layout_binds_descriptors_at_host_paths_without_network() -> None:
    args = sb.sandbox_arguments(CAP, env={**sb.SANDBOX_ENV, "PYTHONPATH": "."},
                                binds=[("--ro-bind-fd", 7, "/opt/server"), ("--bind-fd", 8, "/var/lib/server")],
                                write_roots=["/var/lib/server", "/tmp"], cwd="/opt/server", sync_fd=11,
                                argv=["/opt/server/run", "--bind", "/", "/x"])
    tail = args.index("--")
    head = args[:tail]
    assert "--unshare-all" in head and "--unshare-user" in head and "--share-net" not in head
    assert ["--ro-bind-fd", "7", "/opt/server"] == head[head.index("--ro-bind-fd"):head.index("--ro-bind-fd") + 3]
    assert ["--bind-fd", "8", "/var/lib/server"] == head[head.index("--bind-fd"):head.index("--bind-fd") + 3]
    assert "--bind" not in head and head.count("--ro-bind") == sb.system_view().count("--ro-bind") + 1  # + helper
    assert ["--setenv", "PYTHONPATH", "."] == head[head.index("PYTHONPATH") - 1:head.index("PYTHONPATH") + 2]
    policy = args[tail + 5]
    assert '"write_roots":["/var/lib/server","/tmp"]' in policy
    assert args[-4:] == ["/opt/server/run", "--bind", "/", "/x"]  # argv only after the helper's separator
    with pytest.raises(ValueError, match="not a descriptor bind"):
        sb.sandbox_arguments(CAP, env={}, binds=[("--bind", 7, "/x")], write_roots=[], cwd="/", sync_fd=3, argv=["x"])


# --- state --------------------------------------------------------------------------


def test_no_capability_means_unavailable_and_nothing_launches(layout: dict[str, Path]) -> None:
    sandbox = srv.ServerSandbox(None)
    assert sandbox.state is SandboxState.UNAVAILABLE
    assert asyncio.run(sandbox.start()) is SandboxState.UNAVAILABLE
    launcher = sandbox.launcher(ServerProfile((str(layout["ro"]),), (), ()))
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(launcher.launch(["x"], {}, str(layout["ro"])))


@pytest.mark.parametrize("passed, state", [(True, SandboxState.ENFORCED), (False, SandboxState.SETUP_FAILED)])
def test_only_a_passing_server_self_test_enforces(passed: bool, state: SandboxState) -> None:
    async def self_test(sandbox: srv.ServerSandbox, protected: tuple[str, ...]) -> bool:
        return passed

    sandbox = srv.ServerSandbox(CAP)
    assert asyncio.run(sandbox.start(self_test)) is state
    with pytest.raises(RuntimeError, match="once"):
        asyncio.run(sandbox.start(self_test))


def test_not_started_refuses_without_spawning(layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = srv.ServerSandbox(CAP)
    monkeypatch.setattr(sandbox, "spawn", lambda *a: pytest.fail("spawned"))
    with pytest.raises(SandboxUnavailableError, match="capable_inactive"):
        asyncio.run(sandbox.launcher(ServerProfile((str(layout["ro"]),), (), ())).launch(["x"], {}, str(layout["ro"])))


def test_a_setup_failure_at_launch_latches(layout: dict[str, Path]) -> None:
    async def passing(sandbox: srv.ServerSandbox, protected: tuple[str, ...]) -> bool:
        return True

    async def failing(*args: Any) -> srv.ServerProcess:
        raise SandboxSetupError("no sync")

    sandbox = srv.ServerSandbox(CAP)
    asyncio.run(sandbox.start(passing))
    sandbox.spawn = failing  # type: ignore[method-assign,assignment]
    launcher = sandbox.launcher(ServerProfile((str(layout["ro"]),), (), ()))
    with pytest.raises(SandboxSetupError):
        asyncio.run(launcher.launch(["x"], {}, str(layout["ro"])))
    assert sandbox.state is SandboxState.SETUP_FAILED
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(launcher.launch(["x"], {}, str(layout["ro"])))


def test_an_identity_failure_spawns_nothing(layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    os.link(layout["data"] / "himari.db", layout["ro"] / "db-link")
    sandbox = srv.ServerSandbox(CAP)
    monkeypatch.setattr(sandbox, "_spawn_checked", lambda *a: pytest.fail("spawned"))
    profile = ServerProfile((str(layout["ro"]),), (), (str(layout["data"]),))
    with pytest.raises(SandboxAdmissionError):
        asyncio.run(sandbox.spawn(profile, ["x"], {}, str(layout["ro"])))


# --- the process API on a plain child (no sandbox) -------------------------------------


def _with_process(script: str, scenario: Any) -> None:
    async def run() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=srv.STREAM_LIMIT)
        child = srv.ServerProcess(process)
        try:
            await asyncio.wait_for(scenario(child), 30)
        finally:
            await child.shutdown(0.5)

    asyncio.run(run())


ECHO = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"


def test_lines_go_in_and_come_out() -> None:
    async def scenario(child: srv.ServerProcess) -> None:
        for text in (b'{"id":1}', b"", "é".encode()):
            await child.write_line(text)
            assert await child.read_line() == text
        with pytest.raises(ValueError):
            await child.write_line(b"two\nlines")

    _with_process(ECHO, scenario)


@pytest.mark.parametrize("over", [1, 10])
def test_a_line_over_the_bound_ends_the_process(over: int) -> None:
    script = f"import sys\nsys.stdout.write('x' * {MAX_LINE_BYTES + over} + '\\n'); sys.stdout.flush()\nimport time; time.sleep(60)"

    async def scenario(child: srv.ServerProcess) -> None:
        with pytest.raises(ProcessGone, match="longer than"):
            await child.read_line()
        assert child.returncode is not None
        with pytest.raises(ProcessGone):
            await child.write_line(b"x")

    _with_process(script, scenario)


def test_a_line_at_the_bound_is_read() -> None:
    script = f"import sys\nsys.stdout.write('x' * {MAX_LINE_BYTES} + '\\n'); sys.stdout.flush()"

    async def scenario(child: srv.ServerProcess) -> None:
        assert len(await child.read_line()) == MAX_LINE_BYTES

    _with_process(script, scenario)


def test_eof_and_exit_are_process_gone() -> None:
    async def scenario(child: srv.ServerProcess) -> None:
        with pytest.raises(ProcessGone, match="closed its output"):
            await child.read_line()
        await asyncio.wait_for(child._process.wait(), 10)
        with pytest.raises(ProcessGone):
            await child.write_line(b"late")

    _with_process("pass", scenario)


def test_stderr_is_drained_and_only_its_tail_kept() -> None:
    script = ("import sys\nsys.stderr.write('a' * 300000 + 'END'); sys.stderr.flush()\n"
              "print('ready', flush=True)\nimport time; time.sleep(60)")

    async def scenario(child: srv.ServerProcess) -> None:
        assert await child.read_line() == b"ready"  # a full stderr pipe would have blocked the child
        for _ in range(100):
            if child.stderr_tail().endswith(b"END"):
                break
            await asyncio.sleep(0.05)
        tail = child.stderr_tail()
        assert len(tail) == STDERR_TAIL_BYTES and tail.endswith(b"END")

    _with_process(script, scenario)


@pytest.mark.parametrize("script, ends_by", [
    (ECHO, "eof"),  # stdin closed: exits by itself
    ("import sys, time\nsys.stdin.read()\ntime.sleep(60)", "sigterm"),
    ("import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nsys.stdin.read()\ntime.sleep(60)",
     "sigkill"),
])
def test_shutdown_escalates_from_eof_to_sigterm_to_sigkill(script: str, ends_by: str) -> None:
    async def run() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        child = srv.ServerProcess(process)
        await asyncio.sleep(0.3)  # the handlers are in place
        start = asyncio.get_running_loop().time()
        await child.shutdown(0.5)
        took = asyncio.get_running_loop().time() - start
        expected = {"eof": 0, "sigterm": -15, "sigkill": -9}[ends_by]
        assert child.returncode == expected
        assert took < {"eof": 0.5, "sigterm": 1.0, "sigkill": 1.5}[ends_by] + 0.5
        await child.shutdown(0.5)  # again: nothing to do

    asyncio.run(run())


def test_a_bound_directory_gone_since_the_check_refuses_only_that_launch(layout: dict[str, Path]) -> None:
    async def passing(sandbox: srv.ServerSandbox, protected: tuple[str, ...]) -> bool:
        return True

    sandbox = srv.ServerSandbox(CAP)
    asyncio.run(sandbox.start(passing))
    launcher = sandbox.launcher(ServerProfile((str(layout["ro"]),), (str(layout["rw"]),), ()))
    layout["ro"].rename(layout["ro"].with_name("moved"))
    with pytest.raises(SandboxAdmissionError, match="could not be opened"):
        asyncio.run(launcher.launch(["x"], {}, str(layout["rw"])))
    assert sandbox.state is SandboxState.ENFORCED  # not a setup failure: other servers still start

