"""The CLI and folders on this server (V3-P74 §10.3): workspace, attach, detach, workdir."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from runnerkit import RunnerKit, start_runner  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import ServerClient  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402


class Harness:
    def __init__(self, tmp_path: Path, socket: str | None) -> None:
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m",
                                                      sandbox=False, cleanup_seconds=None, machine_socket=socket)
            return self.application

        config = WebConfig("127.0.0.1", 8765, frozenset({"testserver"}), frozenset({"http://testserver"}), False,
                           str(tmp_path / "data"), "m", None, False)
        self.test_client = TestClient(web.create_app(config, opener))

    def run(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        assert self.application is not None
        self.test_client.headers["authorization"] = f"Bearer {derive_cli_bearer(self.application.operator_secret)}"
        out, err = io.StringIO(), io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()  # input() asks on stdout
        try:
            code = cli.run(list(argv), environ={}, out=out, err=err, client=ServerClient(self.test_client))
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path / "runner")
    yield started
    started.close()


@pytest.fixture
def harness(tmp_path: Path, kit: RunnerKit) -> Iterator[Harness]:
    h = Harness(tmp_path, kit.socket)
    with h.test_client:
        yield h


def test_workspace_list_add_and_rm(harness: Harness, kit: RunnerKit) -> None:
    project = str(kit.home / "projects/a")
    assert harness.run("workspace") == (cli.EXIT_OK, "", "(no folders registered; add one with `himari workspace "
                                                         "add PATH`)\n")
    code, out, _ = harness.run("workspace", "add", "~/projects/a", "--label", "proj", "--note", "C code")
    assert code == cli.EXIT_OK and out.split()[1:3] == ["proj", project]
    code, out, _ = harness.run("workspace")
    assert code == cli.EXIT_OK and out.splitlines()[1] == "    C code"
    assert harness.run("workspace", "add", "~/.ssh")[0] == cli.EXIT_FAILED
    assert harness.run("workspace", "rm", project, stdin="n\n")[2] == "(nothing was changed)\n"
    assert harness.run("workspace", "rm", project, "--yes")[0] == cli.EXIT_OK
    assert harness.run("workspace", "rm", project, "--yes")[0] == cli.EXIT_FAILED
    assert harness.run("workspace", "add")[0] == cli.EXIT_USAGE


def test_attach_workdir_and_detach(harness: Harness, kit: RunnerKit) -> None:
    project = str(kit.home / "projects/a")
    cid = harness.run("new")[1].strip()
    own = str(kit.himari_home / "conversations" / cid)
    assert harness.run("attach", cid)[1] == f"* {own}  this conversation  (this conversation's own)\n"
    code, out, err = harness.run("attach", cid, "~/projects/a")
    assert code == cli.EXIT_OK and out.splitlines() == [f"  {own}  this conversation  (this conversation's own)",
                                                        f"* {project}  a"]
    assert err == "(the model can write there from its next step)\n"
    assert harness.run("workdir", cid, own)[1].startswith(f"* {own}")
    assert harness.run("detach", cid, own)[0] == cli.EXIT_FAILED  # the own folder stays
    assert harness.run("detach", cid, project)[1] == f"* {own}  this conversation  (this conversation's own)\n"
    assert harness.run("detach", cid, project)[0] == cli.EXIT_FAILED
    assert harness.run("attach", cid, project)[0] == cli.EXIT_OK  # registered already: attached by its id


def test_not_offered(tmp_path: Path) -> None:
    h = Harness(tmp_path, None)
    with h.test_client:
        assert h.run("workspace") == (cli.EXIT_FAILED, "", "(folders on this server are not offered to you)\n")
        cid = h.run("new")[1].strip()
        assert h.run("attach", cid) == (cli.EXIT_OK, "", "(this conversation does not work on this server's folders)\n")
