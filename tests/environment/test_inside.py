"""Inside Environment file Tools: boundary and behavior (FILE-01/03, V3-033/035/051)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from himari.common import (
    BindingId,
    CallId,
    ConversationId,
    EnvironmentInstanceId,
    PrincipalId,
    ToolId,
    TrustedScope,
    WorkspaceId,
    thaw,
)
from himari.data.api import BindingAuthority, ConversationWorkspace
from himari.environment.api import ConnectionStatus, EnvironmentPlugin
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.environment.inside.tools import MAX_OBSERVATION_BYTES
from himari.tool.api import EffectProfile, ToolContext, ToolHandler, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.registry import ToolRegistry

ALICE, BOB = WorkspaceId("ws_alice"), WorkspaceId("ws_bob")
SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("c1"))


class _NoArtifacts:
    async def read_artifact(self, scope: Any, artifact_id: Any) -> Any:
        raise KeyError(artifact_id)

    async def list_artifacts(self, scope: Any, *, kind: Any = None) -> Any:
        return ()


@pytest.fixture
def env(tmp_path: Path) -> InsideEnvironment:
    environment = InsideEnvironment(tmp_path / "workspaces")
    environment.provision(ALICE)
    environment.provision(BOB)
    return environment


@pytest.fixture
def alice_root(env: InsideEnvironment) -> Path:
    return Path(env.host_root(ALICE))


SEEN: list[ToolOutcome] = []  # every outcome, for the host-path leak check


def call(env: InsideEnvironment, tool: str, cwd: str = "conversations/c1", workspace: str = ALICE, **args: Any) -> ToolOutcome:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"inside/{tool}")
    ws = WorkspaceId(workspace)
    binding = BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), INSIDE, ws, 1)
    ctx = ToolContext(scope=SCOPE, artifacts=_NoArtifacts(), binding=binding, workspace=ConversationWorkspace(ws, cwd))
    outcome = asyncio.run(handler.run(ToolInvocation(ToolId(f"inside/{tool}"), CallId("c"), args), ctx))
    SEEN.append(outcome)
    return outcome


def text(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert hasattr(block, "text")
    return str(block.text)


# --- plugin shape ---------------------------------------------------------------------


def test_plugin_conforms_and_registers(env: InsideEnvironment) -> None:
    assert isinstance(env, EnvironmentPlugin)
    tools = list(env.tools())
    assert all(isinstance(t, ToolHandler) and t.definition.tool_id.startswith("inside/") for t in tools)
    effects = {str(t.definition.tool_id): t.definition.effect_profile for t in tools}
    assert effects["inside/read_file"] is EffectProfile.READ and effects["inside/write_file"] is EffectProfile.SCOPED_WRITE
    ToolRegistry(tools)  # schemas pass the fail-closed registration checks


def test_observe_reports_provisioned_workspaces(env: InsideEnvironment) -> None:
    bindings = [BindingAuthority(BindingId(b), EnvironmentInstanceId("e"), INSIDE, WorkspaceId(w), 1)
                for b, w in (("b1", "ws_alice"), ("b2", "ws_missing"), ("b3", "../evil"))]
    statuses = [o.status for o in asyncio.run(env.observe(SCOPE, bindings))]
    assert statuses == [ConnectionStatus.ONLINE, ConnectionStatus.OFFLINE, ConnectionStatus.OFFLINE]


def test_host_root_rejects_unsafe_workspace_ids(env: InsideEnvironment) -> None:
    for bad in ("../ws_bob", "a/b", "", ".hidden", "x" * 200):
        with pytest.raises(ValueError):
            env.host_root(WorkspaceId(bad))


# --- basic behavior ------------------------------------------------------------------


def test_write_read_edit_list_delete_round_trip(env: InsideEnvironment, alice_root: Path) -> None:
    assert call(env, "write_file", path="src/main.py", content="print('hi')\n").status is ToolStatus.SUCCEEDED
    assert (alice_root / "conversations/c1/src/main.py").read_text() == "print('hi')\n"
    read = call(env, "read_file", path="src/main.py")
    assert text(read) == "/workspace/conversations/c1/src/main.py (12 bytes)\nprint('hi')\n"
    assert call(env, "edit_file", path="src/main.py", old_text="hi", new_text="hello").status is ToolStatus.SUCCEEDED
    assert (alice_root / "conversations/c1/src/main.py").read_text() == "print('hello')\n"
    listing = text(call(env, "list_files"))
    assert listing.splitlines()[0].startswith("/workspace/conversations/c1 (1 entries")
    assert "src/" in listing
    assert call(env, "delete_file", path="/workspace/conversations/c1/src/main.py").status is ToolStatus.SUCCEEDED
    assert not (alice_root / "conversations/c1/src/main.py").exists()


def test_working_directory_is_created_on_first_use(env: InsideEnvironment, alice_root: Path) -> None:
    assert call(env, "list_files", cwd="conversations/new").status is ToolStatus.SUCCEEDED
    assert (alice_root / "conversations/new").is_dir()


def test_same_principal_can_use_other_conversation_folders(env: InsideEnvironment, alice_root: Path) -> None:
    """V3-033: Conversation folders organize work; they are not a boundary."""
    assert call(env, "write_file", path="/workspace/conversations/c2/shared.txt", content="from c1").status is ToolStatus.SUCCEEDED
    assert text(call(env, "read_file", cwd="conversations/c2", path="shared.txt")).endswith("from c1")
    assert call(env, "read_file", path="../c2/shared.txt").status is ToolStatus.SUCCEEDED


def test_writes_are_atomic_and_leave_no_temporaries(env: InsideEnvironment, alice_root: Path) -> None:
    call(env, "write_file", path="a.txt", content="one")
    call(env, "write_file", path="a.txt", content="two")
    assert call(env, "write_file", path="a.txt/inner", content="x").status is ToolStatus.FAILED  # a.txt is a file
    leftovers = [p.name for p in (alice_root / "conversations/c1").iterdir() if p.name.startswith(".himari-")]
    assert leftovers == [] and (alice_root / "conversations/c1/a.txt").read_text() == "two"


def test_large_and_binary_reads(env: InsideEnvironment, alice_root: Path) -> None:
    folder = alice_root / "conversations/c1"
    folder.mkdir(parents=True)
    (folder / "big.txt").write_text("a" * (MAX_OBSERVATION_BYTES + 10))
    (folder / "blob.bin").write_bytes(b"\x00\x01\x02")
    big = call(env, "read_file", path="big.txt")
    assert big.raw_result is not None and thaw(big.raw_result)["truncated"] is True  # type: ignore[index]
    assert text(big).endswith(f"[truncated: showing the first {MAX_OBSERVATION_BYTES} of {MAX_OBSERVATION_BYTES + 10} bytes]")
    assert "binary file" in text(call(env, "read_file", path="blob.bin"))


def test_edit_requires_exact_occurrence_count(env: InsideEnvironment, alice_root: Path) -> None:
    call(env, "write_file", path="t.txt", content="x x")
    miss = call(env, "edit_file", path="t.txt", old_text="x", new_text="y")
    assert miss.status is ToolStatus.FAILED and "found 2" in text(miss)
    assert (alice_root / "conversations/c1/t.txt").read_text() == "x x"
    assert call(env, "edit_file", path="t.txt", old_text="x", new_text="y", expected_replacements=2).status is ToolStatus.SUCCEEDED
    assert (alice_root / "conversations/c1/t.txt").read_text() == "y y"


def test_oversized_write_is_refused(env: InsideEnvironment) -> None:
    outcome = call(env, "write_file", path="huge.txt", content="a" * (256 * 1024 + 1))
    assert outcome.status is ToolStatus.FAILED and "too large" in text(outcome)


def test_delete_refuses_directories_and_reports_missing(env: InsideEnvironment, alice_root: Path) -> None:
    (alice_root / "conversations/c1/dir").mkdir(parents=True)
    assert "is a directory" in text(call(env, "delete_file", path="dir"))
    assert "no such file" in text(call(env, "delete_file", path="nope.txt"))


# --- boundary --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../../ws_bob/conversations/x.txt",
        "/workspace/../ws_bob/secret.txt",
        "/etc/passwd",
        "/workspace/../../etc/passwd",
        "../../..",
    ],
)
def test_paths_outside_the_workspace_are_denied(env: InsideEnvironment, path: str) -> None:
    outcome = call(env, "read_file", path=path)
    assert outcome.status is ToolStatus.DENIED and "outside the workspace" in text(outcome)


def test_other_principals_workspace_is_unreachable(env: InsideEnvironment) -> None:
    bob_file = Path(env.host_root(BOB)) / "conversations" / "secret.txt"
    bob_file.write_text("bob only")
    listing = text(call(env, "list_files", path="/workspace"))
    assert "ws_bob" not in listing and "secret" not in listing


@pytest.mark.parametrize("target_kind", ["host-file", "host-dir", "other-principal"])
def test_symlinks_are_never_followed(env: InsideEnvironment, alice_root: Path, tmp_path: Path, target_kind: str) -> None:
    folder = alice_root / "conversations/c1"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    targets = {"host-file": outside, "host-dir": tmp_path, "other-principal": Path(env.host_root(BOB))}
    (folder / "link").symlink_to(targets[target_kind])
    for tool, args in (("read_file", {"path": "link"}), ("read_file", {"path": "link/outside.txt"}),
                       ("list_files", {"path": "link"}), ("write_file", {"path": "link", "content": "x"}),
                       ("write_file", {"path": "link/new.txt", "content": "x"}), ("delete_file", {"path": "link"})):
        outcome = call(env, tool, **args)
        assert outcome.status in (ToolStatus.DENIED, ToolStatus.FAILED), (tool, args)
        assert "host secret" not in text(outcome)
    assert outside.read_text() == "host secret" and (folder / "link").is_symlink()
    assert not (tmp_path / "new.txt").exists()


def test_symlinked_working_directory_is_refused(env: InsideEnvironment, alice_root: Path, tmp_path: Path) -> None:
    (alice_root / "conversations/evil").symlink_to(tmp_path)
    outcome = call(env, "list_files", cwd="conversations/evil")
    assert outcome.status is ToolStatus.DENIED


def test_fifo_is_refused_without_blocking(env: InsideEnvironment, alice_root: Path) -> None:
    folder = alice_root / "conversations/c1"
    folder.mkdir(parents=True)
    os.mkfifo(folder / "pipe")

    async def bounded() -> ToolOutcome:
        return await asyncio.wait_for(asyncio.to_thread(call, env, "read_file", path="pipe"), timeout=5)

    outcome = asyncio.run(bounded())
    assert outcome.status is ToolStatus.FAILED and "not a regular file" in text(outcome)


def test_hard_linked_files_are_refused(env: InsideEnvironment, alice_root: Path) -> None:
    folder = alice_root / "conversations/c1"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("shared inode")
    os.link(folder / "a.txt", folder / "b.txt")
    for tool, args in (("read_file", {"path": "b.txt"}), ("write_file", {"path": "b.txt", "content": "x"}),
                       ("delete_file", {"path": "b.txt"})):
        assert call(env, tool, **args).status is ToolStatus.DENIED
    assert (folder / "a.txt").read_text() == "shared inode"


def test_symlinks_inside_the_workspace_are_not_followed_either(env: InsideEnvironment, alice_root: Path) -> None:
    """The rule is 'never follow', not just 'never escape' (RESOLVE_NO_SYMLINKS)."""
    folder = alice_root / "conversations/c1"
    (folder / "real").mkdir(parents=True)
    (folder / "real/file.txt").write_text("inside")
    (folder / "alias").symlink_to("real")
    (folder / "file-alias.txt").symlink_to("real/file.txt")
    for args in ({"path": "alias/file.txt"}, {"path": "file-alias.txt"}):
        assert call(env, "read_file", **args).status is ToolStatus.DENIED, args
    assert call(env, "list_files", path="alias").status is ToolStatus.DENIED


def test_writing_or_deleting_a_symlink_entry_is_a_link_refusal(env: InsideEnvironment, alice_root: Path) -> None:
    folder = alice_root / "conversations/c1"
    folder.mkdir(parents=True)
    (folder / "target.txt").write_text("keep")
    (folder / "entry").symlink_to("target.txt")
    for tool, args in (("write_file", {"path": "entry", "content": "x"}), ("delete_file", {"path": "entry"})):
        outcome = call(env, tool, **args)
        assert outcome.status is ToolStatus.DENIED and "symbolic links" in text(outcome), tool
    assert (folder / "entry").is_symlink() and (folder / "target.txt").read_text() == "keep"


def test_no_result_ever_contains_a_host_path(tmp_path: Path) -> None:
    """Runs last in this module: scans every outcome produced above."""
    assert SEEN
    host = str(tmp_path.parent)
    for outcome in SEEN:
        blob = text(outcome) + json.dumps(thaw(outcome.raw_result))
        assert host not in blob and "/tmp/" not in blob, blob
