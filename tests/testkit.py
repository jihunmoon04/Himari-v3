"""Test doubles shared by module and integration suites."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from datetime import UTC, datetime

from himari.bootstrap import DEFAULT_INSTRUCTIONS, Runtime, Store, build_runtime
from himari.common import (
    CallId,
    CallRef,
    RunId,
    UnknownOutcomePolicy,
    ConversationId,
    EnvironmentKind,
    JsonValue,
    ModelToolResult,
    PrincipalId,
    TextBlock,
    ToolCall,
    ToolId,
    TrustedScope,
)
from himari.control.api import ControlError, ControlErrorCode, DriveResult, ModelCatalog, StopSignal, TurnProgress
from himari.data.api import ConversationEvent, UserMessage, ViewMaker, WakeReason
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.api import ConnectionStatus, EnvironmentObservation
from himari.prompt.api import ContextBudget
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter, ScriptStep
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
)

SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("conv_1"))


def _v21_undo() -> tuple[str, ...]:
    """What v21 changed, undone: the dropped tables made again and the two tables that lost a
    column rebuilt, each with the exact text v20 has (an added column would go last, so SQLite's
    ADD COLUMN cannot undo a DROP COLUMN). ``environments`` rows go back as activations."""
    from himari.data.sqlite import _UPGRADES

    ref = sqlite3.connect(":memory:")
    try:
        for statement in _UPGRADES[20][0].split(";"):
            if statement.strip():
                ref.execute(statement)
        sql = {name: text for name, text in ref.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}

        def indexes(table: str) -> list[str]:
            return [text for (text,) in ref.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]

        def columns(table: str) -> list[str]:
            return [row[1] for row in ref.execute(f"PRAGMA table_info({table})")]

        undo = ["PRAGMA legacy_alter_table=ON"]  # a rename leaves the other tables' references as they are
        for table, gone in (("conversations", {"workspace_id", "workspace_cwd"}),
                            ("tool_executions", {"workspace_binding_id"})):
            kept = ", ".join(c for c in columns(table) if c not in gone)
            undo += [f"ALTER TABLE {table} RENAME TO {table}_v21", sql[table],
                     f"INSERT INTO {table} ({kept}) SELECT {kept} FROM {table}_v21", f"DROP TABLE {table}_v21",
                     *indexes(table)]
        undo += [sql[t] for t in ("artifacts", "activations", "bindings", "workspaces")]
        undo += [*indexes("bindings"), *indexes("workspaces")]
        undo += ["INSERT INTO activations (conversation_id, environment_instance_id, environment_kind)"
                 " SELECT conversation_id, 'env_' || conversation_id || '_' || environment_kind, environment_kind"
                 " FROM environments",
                 "DROP TABLE environments", "PRAGMA legacy_alter_table=OFF"]
        return tuple(undo)
    finally:
        ref.close()


V21_UNDO = _v21_undo()  # what v21 changed (the work-agent leftovers dropped), undone
# what v22 added (character-life v0.1), undone
V22_UNDO = ("DROP INDEX life_writes_by_owner", "DROP TABLE life_writes", "DROP TABLE life_ids",
            "DROP INDEX life_by_persona", "DROP TABLE life_entries")
# what v23 added (character-bond v0.1), undone
V23_UNDO = ("DROP TABLE bond_stages", "ALTER TABLE life_entries DROP COLUMN month_day",
            "ALTER TABLE life_entries DROP COLUMN tag")
# what v24 added (character-state v0.1), undone
V24_UNDO = ("DROP TABLE himari_moods", "ALTER TABLE life_entries DROP COLUMN time_of_day")
# what v25 added (character-world v0.1), undone
V25_UNDO = tuple(f"ALTER TABLE life_entries DROP COLUMN {c}" for c in ("arc_id", "ending", "end_day", "title"))
# what v20 added (V3-P108), undone
V20_UNDO = ("DROP TABLE persona_choice", "DROP TABLE persona_ids", "DROP INDEX personas_by_name",
            "DROP TABLE personas")
V17_UNDO = ("DROP TABLE wake_keys", "DROP INDEX wakes_opened", "DROP INDEX wakes_by_owner", "DROP TABLE wakes",
            "ALTER TABLE devices DROP COLUMN last_input_at",
            *(f"ALTER TABLE memories DROP COLUMN {c}" for c in ("reminded_at", "remind_attempts", "remind_wake",
                                                                  "remind_state", "remind_instant", "remind_at")))
V15_UNDO = ("DROP INDEX memory_pass_items_by_outcome", "DROP TABLE memory_pass_items", "DROP TABLE memory_pass_calls",
            "DROP INDEX memory_passes_by_owner", "DROP INDEX memory_passes_by_conversation", "DROP TABLE memory_passes",
            "DROP TABLE memory_cursors", "DROP INDEX episodes_by_conversation", "DROP INDEX episodes_by_time",
            "DROP TABLE episodes", "ALTER TABLE memory_ids DROP COLUMN last_episode_id",
            *(f"ALTER TABLE memories DROP COLUMN {c}" for c in (
                "user_approved_at", "retired_item", "retired_pass", "retired_at", "source_item", "source_pass",
                "valid_instant", "valid_until", "due_instant", "due_at")))  # what v15 added (V3-P85), undone
FOREIGN = TrustedScope(PrincipalId("mallory"), ConversationId("conv_1"))
FAKE = EnvironmentKind("fake")


# Which Data backend new stores use; the ``data_backend`` fixture
# (tests/data, tests/integration) switches it to SQLite for one run.
STORE_FACTORY: Callable[[], Store] = InMemoryStore
OPEN_STORES: list[SqliteStore] = []


def new_store() -> Store:
    store = STORE_FACTORY()
    if isinstance(store, SqliteStore):
        OPEN_STORES.append(store)
    return store


def restart(store: Store) -> Store:
    """Model a process restart over persisted data: SQLite closes and reopens
    the database file (nothing survives but what was committed); the
    in-memory store is shared as is."""
    if isinstance(store, SqliteStore):
        store.close()
        reopened = SqliteStore(store.path)
        OPEN_STORES.append(reopened)
        return reopened
    return store


def data_for(store: Store) -> InMemoryData | SqliteData:
    return SqliteData(store) if isinstance(store, SqliteStore) else InMemoryData(store)


def forget_unknown_policy(store: Store) -> list[CallRef]:
    """Simulate records written before V3-047 (no claim-time policy) for
    every execution of SCOPE; returns their refs."""
    if isinstance(store, SqliteStore):
        rows = store.connection.execute(
            "SELECT run_id, call_id FROM tool_executions WHERE conversation_id=? ORDER BY seq", (SCOPE.conversation_id,)
        ).fetchall()
        store.connection.execute("UPDATE tool_executions SET unknown_outcome=NULL WHERE conversation_id=?",
                                 (SCOPE.conversation_id,))
        return [CallRef(RunId(r["run_id"]), CallId(r["call_id"])) for r in rows]
    conv = store._conversations[SCOPE.conversation_id]
    for ref in conv.executions:
        conv.executions[ref] = dataclasses.replace(conv.executions[ref], unknown_outcome=None)
    return list(conv.executions)


class SimulatedCrash(BaseException):
    """Escapes every ``except Exception``: models the process dying here."""


class NoWakes:
    """For a fake ConversationDriver: a server keeping no wakes (V3-P87)."""

    async def accept_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                          keys: Sequence[str] = (), reminders: Sequence[int] = ()) -> ConversationEvent:
        raise ControlError(ControlErrorCode.NOT_SETTLED, "this fake keeps no wakes")

    async def close_cut_group(self, scope: TrustedScope, reason: str) -> bool:
        return False

    async def withdraw_wake(self, scope: TrustedScope, wake_id: str) -> None:
        pass

    async def resume_wake(self, scope: TrustedScope, wake_id: str, *, stop: StopSignal | None = None,
                          progress: TurnProgress | None = None, max_runs: int | None = None,
                          free: bool = False) -> DriveResult:
        resume = getattr(self, "resume")  # the fake's own
        result: DriveResult = await resume(scope, stop=stop, progress=progress, max_runs=max_runs)
        return result


def user(text: str) -> UserMessage:
    return UserMessage(blocks=(TextBlock(text),))


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal(blocks=(TextBlock(text),))


def calls(*specs: tuple[str, dict[str, JsonValue]], commentary: str = "") -> NormalizedToolCalls:
    return NormalizedToolCalls(
        calls=tuple(
            ToolCall(CallId(f"c{i}"), i, ToolId(tool_id), args) for i, (tool_id, args) in enumerate(specs)
        ),
        commentary=(TextBlock(commentary),) if commentary else (),
    )


def text_result(text: str, *, is_error: bool = False) -> ModelToolResult:
    return ModelToolResult(blocks=(TextBlock(text),), is_error=is_error)


class RecordingTool:
    """Configurable tool that counts invocations and may fail or crash."""

    def __init__(
        self,
        tool_id: str,
        *,
        effect: EffectProfile = EffectProfile.READ,
        environment_kind: EnvironmentKind | None = None,
        external_source: str | None = None,
        behavior: Callable[[ToolInvocation, ToolContext], ToolOutcome] | None = None,
    ) -> None:
        self.definition = ToolDefinition(
            tool_id=ToolId(tool_id),
            description=f"test tool {tool_id}",
            input_schema={"type": "object"},
            effect_profile=effect,
            environment_kind=environment_kind,
            external_source=external_source,
        )
        self.invocations: list[ToolInvocation] = []
        self.contexts: list[ToolContext] = []
        self.crash_next = False
        self.raise_next: Exception | None = None
        self._behavior = behavior

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        self.invocations.append(invocation)
        self.contexts.append(ctx)
        if self.crash_next:
            self.crash_next = False
            raise SimulatedCrash(self.definition.tool_id)
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self._behavior is not None:
            return self._behavior(invocation, ctx)
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result(f"{self.definition.tool_id} ok"))


class FakeEnvironment:
    def __init__(self, *tools: RecordingTool) -> None:
        self._tools = tools

    @property
    def kind(self) -> EnvironmentKind:
        return FAKE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    async def observe(self, scope: TrustedScope) -> EnvironmentObservation:
        return EnvironmentObservation(ConnectionStatus.UNKNOWN, datetime.now(UTC))


def runtime(
    script: Iterable[ScriptStep] = (),
    *,
    tools: Iterable[ToolHandler] = (),
    env_tools: Iterable[RecordingTool] = (),
    store: Store | None = None,
    provider: ScriptedAdapter | None = None,
    context_budget: ContextBudget | None = None,
    instructions: Sequence[str] | None = None,
    max_runs_per_drive: int = 16,
    turn_guidance: bool = False,
    external_defaults: Mapping[str, bool] | None = None,
    models: ModelCatalog | None = None,
    image_views: ViewMaker | None = None,
    streaming: bool = False,
) -> tuple[Runtime, ScriptedAdapter]:
    """Fresh runtime; pass ``restart(rt.store)`` to model a process restart."""
    adapter = provider if provider is not None else ScriptedAdapter(script)
    fresh = store is None
    store = new_store() if store is None else store
    env_tools = tuple(env_tools)
    tools = tuple(tools)
    rt = build_runtime(
        adapter,
        tools=tools,
        environments=[FakeEnvironment(*env_tools)] if env_tools else [],
        store=store,
        context_budget=context_budget,
        instructions=DEFAULT_INSTRUCTIONS if instructions is None else instructions,
        max_runs_per_drive=max_runs_per_drive,
        turn_guidance=turn_guidance,
        external_defaults=external_defaults,
        models=models,
        image_views=image_views,
        streaming=streaming,
        # Tests exercising REQUIRE_USER opt in explicitly by using such a Tool;
        # the production default stays off (see test_registry_schema.py).
        allow_require_user_tools=any(
            t.definition.unknown_outcome is UnknownOutcomePolicy.REQUIRE_USER for t in (*tools, *env_tools)
        ),
    )
    if fresh:
        rt.store.create_conversation(SCOPE)
    return rt, adapter


def assert_chat_structure(messages: list[dict[str, Any]]) -> None:
    """Minimal codification of the Chat Completions ordering rules."""
    assert messages and messages[0]["role"] in ("system", "user")
    non_system = [m for m in messages if m["role"] != "system"]
    assert non_system[0]["role"] == "user", "first turn must be the user's"
    i = 0
    while i < len(messages):
        m = messages[i]
        assert m["role"] in ("system", "user", "assistant", "tool")
        if m["role"] == "tool":
            raise AssertionError(f"tool message without a preceding tool_calls group at {i}")
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = messages[i + 1 : i + 1 + len(ids)]
            assert [f["role"] for f in following] == ["tool"] * len(ids), "results must follow immediately"
            assert [f["tool_call_id"] for f in following] == ids, "results in call order, ids paired"
            i += 1 + len(ids)
            continue
        i += 1


def image_model_input(parts: Sequence[Any], attachments: Mapping[str, Any], *, image_input: bool = False,
                      model: str = "m", screen_points: bool = False) -> Any:
    """A ModelInput for attachment and image tests (V3-P67, V3-P84)."""
    from himari.data.api import PersistedStateBasis, PromptBasis
    from himari.prompt.api import CurrentStateProjection, ModelInput, RequestOptions
    return ModelInput(stable_prefix=("You are HIMARI.",), tool_catalog=(), conversation=tuple(parts),
                      current_state=CurrentStateProjection(PersistedStateBasis(0, 0, ())),
                      request_options=RequestOptions(model, image_input=image_input,
                                                     screen_points=screen_points), trigger_refs=(),
                      prompt_basis=PromptBasis(0, 0), attachments=dict(attachments))
