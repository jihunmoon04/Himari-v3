"""Child process for tests/data/test_sqlite.py: sets up a scenario on a
SQLite store, then dies by SIGKILL at a chosen point. Not a test module.

Usage: python crash_child.py <db path> <scenario> [<op> <mode> <n> <dump path>]
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import sys
from typing import Any

from testkit import FAKE, SCOPE, FakeEnvironment, RecordingTool, calls, final, text_result, user

from himari.bootstrap import build_runtime
from himari.common import (
    ASK_USER_TOOL_ID,
    CallId,
    CallRef,
    RawRepresentation,
    ToolCall,
    ToolId,
    UnknownOutcomePolicy,
    WorkspaceId,
)
from himari.data.api import (
    ArtifactMutation,
    AssistantToolCalls,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.sqlite import SqliteData, SqliteStore
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import EffectProfile, ToolOutcome, WorkspaceRequirement

WRITES = ("INSERT", "UPDATE", "DELETE")


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


class Trap:
    """Connection stand-in: counts write statements of the armed operation
    and dies after the n-th one, or right after its COMMIT."""

    def __init__(self, conn: sqlite3.Connection, mode: str, n: int) -> None:
        self.conn, self.mode, self.n, self.writes, self.armed = conn, mode, n, 0, False

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, params)
        if self.armed:
            word = sql.lstrip().split()[0].upper()
            if word in WRITES:
                self.writes += 1
                if self.mode == "mid" and self.writes == self.n:
                    die()  # n writes are in the open transaction, nothing committed
            elif word == "COMMIT" and self.mode == "after":
                die()  # committed: must be durable
        return cursor


def prepared(data: SqliteData) -> Any:
    async def run() -> Any:
        run_id = await data.prepare_run(SCOPE, RunPreparation(b"\xff\x00req", "application/json", RawRepresentation.WIRE_EXACT,
                                                              (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"resp", "application/json"))
        return run_id
    return run


async def setup_op(store: SqliteStore, data: SqliteData, op: str) -> Any:
    """Prepares the state just before ``op`` and returns a coroutine factory for it."""
    await data.append_event(SCOPE, user("hi"))
    new_run = prepared(data)
    group = tuple(ToolCall(CallId(f"c{i}"), i, ToolId(t), {"i": i}) for i, t in enumerate(["note", "fake/a", "fake/b"]))
    if op == "project":
        run_id = await new_run()
        return lambda: data.project_model_output(SCOPE, run_id, AssistantToolCalls(group), continuation={"r": [1, 2]})
    if op == "commit":
        run_id = await new_run()
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
        done = ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"), raw_result={"x": 1},
                              artifact_mutations=(ArtifactMutation("note", "a"), ArtifactMutation("plan", "b")))
        return lambda: data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c0")), done)
    if op == "answer":
        run_id = await new_run()
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls((ToolCall(CallId("q"), 0, ASK_USER_TOOL_ID, {}),)))
        return lambda: data.answer_pending_call(SCOPE, CallRef(run_id, CallId("q")), user("yes"))
    if op == "recover":
        for _ in range(2):
            run_id = await new_run()
            await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
            for call in ("c1", "c2"):
                await data.claim_tool_execution(SCOPE, CallRef(run_id, CallId(call)),
                                                unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=True)
        await data.prepare_run(SCOPE, RunPreparation(b"x", "t", RawRepresentation.WIRE_EXACT, (), PromptBasis(0, 0), "p", "m"))
        return lambda: data.recover_interrupted(SCOPE)
    raise SystemExit(f"unknown op {op}")


def transaction_scenario(path: str, op: str, mode: str, n: int, dump: str) -> None:
    store = SqliteStore(path)
    store.create_conversation(SCOPE)
    data = SqliteData(store)
    operation = asyncio.run(setup_op(store, data, op))
    with open(dump, "w") as handle:
        handle.write("\n".join(store.connection.iterdump()))
    trap = Trap(store.connection, mode, n)
    store.connection = trap  # type: ignore[assignment]
    trap.armed = True
    asyncio.run(operation())
    print(f"WRITES {trap.writes}", flush=True)  # "count" mode: the op completed


def provider_scenario(path: str) -> None:
    store = SqliteStore(path)
    store.create_conversation(SCOPE)
    provider = ScriptedAdapter([final("never seen")])

    async def killed_while_sending(request: Any, *_: Any) -> Any:
        die()

    provider.invoke_prepared = killed_while_sending  # type: ignore[method-assign, assignment]
    rt = build_runtime(provider, store=store)
    asyncio.run(rt.driver.drive(SCOPE, user("hi")))


def tool_scenario(path: str) -> None:
    store = SqliteStore(path)
    store.create_conversation(SCOPE)

    def killed_mid_effect(invocation: Any, ctx: Any) -> ToolOutcome:
        die()
        raise AssertionError("unreachable")

    write = RecordingTool("fake/write", effect=EffectProfile.UNKNOWN, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT, behavior=killed_mid_effect)
    rt = build_runtime(ScriptedAdapter([calls(("fake/write", {"v": 1}))]), store=store,
                       environments=[FakeEnvironment(write)])
    store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    asyncio.run(rt.driver.drive(SCOPE, user("write")))


if __name__ == "__main__":
    db, scenario = sys.argv[1], sys.argv[2]
    if scenario == "transaction":
        transaction_scenario(db, sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6])
    elif scenario == "provider":
        provider_scenario(db)
    elif scenario == "tool":
        tool_scenario(db)
