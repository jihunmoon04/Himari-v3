"""Inside file Tools through the full stack, plus Q-39 (no exception text to the model)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from testkit import SCOPE, RecordingTool, calls, final, user

from himari.bootstrap import build_in_memory_runtime
from himari.common import CallId, TextBlock, WorkspaceId, thaw
from himari.control.api import DriveState
from himari.data.api import ToolExecutionStatus
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.prompt.api import ToolGroupTurn
from himari.provider.scripted import ScriptedAdapter


def test_model_writes_then_reads_a_file_in_its_working_directory(tmp_path: Path) -> None:
    inside = InsideEnvironment(tmp_path / "workspaces")
    inside.provision(WorkspaceId("ws_alice"))
    provider = ScriptedAdapter([
        calls(("inside/write_file", {"path": "notes/plan.md", "content": "# Plan\n"})),
        calls(("inside/read_file", {"path": "notes/plan.md"})),
        final("done"),
    ])
    rt = build_in_memory_runtime(provider, environments=[inside])
    rt.store.create_conversation(SCOPE)
    catalog_before = rt.registry.catalog(frozenset())
    rt.store.activate_environment(SCOPE, INSIDE, WorkspaceId("ws_alice"))

    result = asyncio.run(rt.driver.drive(SCOPE, user("write a plan")))

    assert result.state is DriveState.COMPLETED and len(result.run_ids) == 3
    on_disk = tmp_path / "workspaces/ws_alice/conversations" / SCOPE.conversation_id / "notes/plan.md"
    assert on_disk.read_text() == "# Plan\n"
    group = provider.prepared_inputs[2].conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    shown = group.results[CallId("c0")].blocks[0]
    assert isinstance(shown, TextBlock)
    assert shown.text == f"/workspace/conversations/{SCOPE.conversation_id}/notes/plan.md (7 bytes)\n# Plan\n"
    assert all(not d.tool_id.startswith("inside/") for d in catalog_before)  # hidden until activated
    write_record = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))[0]
    assert write_record.status is ToolExecutionStatus.SUCCEEDED
    assert str(tmp_path) not in str(provider.sent[-1].body)  # no host path reaches the model


def test_internal_exception_text_never_reaches_the_model() -> None:
    """Q-39: the model learns only that the tool failed; details stay in raw_result."""
    from testkit import runtime

    leaky = RecordingTool("leaky")
    leaky.raise_next = RuntimeError("/srv/himari/secret/path token=abc123")
    rt, provider = runtime([calls(("leaky", {})), final("ok")], tools=[leaky])
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.COMPLETED
    record = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))[0]
    assert record.status is ToolExecutionStatus.FAILED
    assert record.model_result is not None and record.model_result.is_error
    block = record.model_result.blocks[0]
    assert isinstance(block, TextBlock) and block.text == "The tool failed with an internal error."
    assert thaw(record.raw_result) == {"error_type": "RuntimeError", "error": "/srv/himari/secret/path token=abc123"}
    assert b"secret" not in provider.sent[-1].body and b"abc123" not in provider.sent[-1].body



def test_two_environments_with_different_workspaces_work_independently(tmp_path: Path) -> None:
    """PR #8 review B1: activating another Environment must not break Inside,
    and each Environment Tool gets its own Binding's working directory."""
    from testkit import FAKE, FakeEnvironment

    from himari.tool.api import WorkspaceRequirement

    for inside_first in (True, False):
        inside = InsideEnvironment(tmp_path / f"ws{inside_first}")
        inside.provision(WorkspaceId("ws_alice"))
        remote = RecordingTool("fake/ping", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
        provider = ScriptedAdapter([calls(("inside/write_file", {"path": "a.txt", "content": "hi"}), ("fake/ping", {})),
                                    final("done")])
        rt = build_in_memory_runtime(provider, environments=[inside, FakeEnvironment(remote)])
        rt.store.create_conversation(SCOPE)
        order = [(INSIDE, "ws_alice", None), (FAKE, "remote_box", "projects/x")]
        for kind, ws, cwd in (order if inside_first else order[::-1]):
            rt.store.activate_environment(SCOPE, kind, WorkspaceId(ws), cwd_relative=cwd)

        result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

        assert result.state is DriveState.COMPLETED, inside_first
        records = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
        assert [r.status for r in records] == [ToolExecutionStatus.SUCCEEDED] * 2, inside_first
        assert (tmp_path / f"ws{inside_first}/ws_alice/conversations" / SCOPE.conversation_id / "a.txt").read_text() == "hi"
        remote_ctx = remote.contexts[-1]
        assert remote_ctx.workspace is not None
        assert (remote_ctx.workspace.workspace_id, remote_ctx.workspace.cwd_relative) == ("remote_box", "projects/x")
