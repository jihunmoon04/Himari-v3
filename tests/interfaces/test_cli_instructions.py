"""The CLI and the user's instructions (V3-P69 §8): show, set, clear, and
/instructions in a chat."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.data.api import MAX_INSTRUCTIONS_CHARS  # noqa: E402
from himari.interfaces.cli import main as cli  # noqa: E402
from himari.interfaces.cli.client import ServerClient  # noqa: E402
from himari.interfaces.cli.terminal import INDENT  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="m",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig("127.0.0.1", 8765, frozenset({"testserver"}), frozenset({"http://testserver"}), False,
                           str(tmp_path / "data"), "m", None, False)
        self.test_client = TestClient(web.create_app(config, opener))

    def authorized(self) -> TestClient:
        assert self.application is not None
        self.test_client.headers["authorization"] = f"Bearer {derive_cli_bearer(self.application.operator_secret)}"
        return self.test_client

    def run(self, *argv: str, stdin: str = "", client: ServerClient | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()  # input() asks on stdout
        try:
            code = cli.run(list(argv), environ={}, out=out, err=err,
                           client=client or ServerClient(self.authorized()))
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        return code, out.getvalue(), err.getvalue()

    def text(self) -> str:
        return str(self.authorized().get("/api/instructions").json()["text"])


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.chdir(tmp_path)
    h = Harness(tmp_path)
    with h.test_client:
        yield h


def test_show_set_from_a_file_and_from_stdin(harness: Harness, tmp_path: Path) -> None:
    assert harness.run("instructions") == (cli.EXIT_OK, "", "(no instructions)\n")
    (tmp_path / "rules.md").write_text("# Rules\n\tAnswer in Korean.\n- 짧게\n", encoding="utf-8")
    code, out, err = harness.run("instructions", "set", "rules.md")
    assert (code, out, err) == (cli.EXIT_OK, "", "(saved; applies from the next message)\n")
    code, out, _ = harness.run("instructions")
    assert code == cli.EXIT_OK and out == "# Rules\n\tAnswer in Korean.\n- 짧게\n\n"  # as written: it can be set again
    assert harness.run("instructions", "set", "-", stdin="from stdin\r\n")[0] == cli.EXIT_OK
    assert harness.text() == "from stdin\n"


def test_unsafe_characters_are_escaped_when_shown(harness: Harness) -> None:
    harness.run("instructions", "set", "-", stdin="left‮right")  # a bidi override is stored, not shown raw
    out = harness.run("instructions")[1]
    assert "‮" not in out and "\\u202e" in out


def test_clear_asks_first(harness: Harness) -> None:
    assert harness.run("instructions", "clear") == (cli.EXIT_OK, "", "(no instructions)\n")  # nothing to clear
    harness.run("instructions", "set", "-", stdin="keep me")
    for answer in ("n\n", "", "maybe\n"):  # no, end of input, anything else: nothing changes
        code, _, err = harness.run("instructions", "clear", stdin=answer)
        assert code == cli.EXIT_OK and "(nothing was changed)" in err and harness.text() == "keep me"
    code, _, err = harness.run("instructions", "clear", stdin="y\n")
    assert code == cli.EXIT_OK and "(cleared; applies from the next message)" in err and harness.text() == ""
    harness.run("instructions", "set", "-", stdin="again")
    assert harness.run("instructions", "clear", "--yes")[0] == cli.EXIT_OK and harness.text() == ""


def test_usage_and_refusals(harness: Harness) -> None:
    code, _, err = harness.run("instructions", "set")
    assert code == cli.EXIT_USAGE and "himari instructions set FILE" in err
    code, _, err = harness.run("instructions", "set", "missing.md")
    assert code == cli.EXIT_USAGE and "cannot read missing.md" in err
    code, _, err = harness.run("instructions", "set", "-", stdin="x" * (MAX_INSTRUCTIONS_CHARS + 1))
    assert code == cli.EXIT_FAILED and "refused (too_large)" in err and "10,000" in err
    code, _, err = harness.run("instructions", "set", "-", stdin="bell\a")
    assert code == cli.EXIT_FAILED and "refused (invalid_instructions)" in err
    assert harness.text() == ""


def test_a_change_made_elsewhere_meanwhile_is_not_overwritten(harness: Harness) -> None:
    class Racing(ServerClient):
        """Someone saves in the Web between the CLI's read and its save."""

        def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
            found = super().get(path, params)
            if path == "/api/instructions":
                super().put(path, {"text": "from the web", "revision": found["revision"]})
            return found

    code, _, err = harness.run("instructions", "set", "-", stdin="from the cli",
                               client=Racing(harness.authorized()))
    assert code == cli.EXIT_FAILED and "refused (revision_conflict)" in err
    assert harness.text() == "from the web"


def test_slash_instructions_in_a_chat(harness: Harness) -> None:
    def chat(*commands: str) -> tuple[str, str]:
        lines = iter([*commands, "/exit"])
        out, err = io.StringIO(), io.StringIO()
        session = cli.Session(ServerClient(harness.authorized()), cli.Printer(out), err, sleep=lambda s: None)
        assert cli.cmd_chat(session, cli.parser().parse_args(["chat"]), read=lambda prompt: next(lines)) == 0
        return out.getvalue(), err.getvalue()

    _, err = chat("/instructions")
    assert "(no instructions; set them with `himari instructions set FILE`)" in err
    harness.run("instructions", "set", "-", stdin="Answer in Korean.\nBe brief.")
    out, _ = chat("/instructions")
    assert out.splitlines()[-3:] == ["Your instructions (every conversation):", f"{INDENT}Answer in Korean.",
                                     f"{INDENT}Be brief."]
