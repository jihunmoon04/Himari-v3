"""Test doubles shared by module and integration suites."""

from __future__ import annotations

import dataclasses
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
from himari.control.api import ModelCatalog
from himari.data.api import ArtifactMutation, BindingAuthority, UserMessage, ViewMaker
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.api import BindingObservation, ConnectionStatus
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
    WorkspaceRequirement,
)

SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("conv_1"))
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
        workspace: WorkspaceRequirement = WorkspaceRequirement.NONE,
        behavior: Callable[[ToolInvocation, ToolContext], ToolOutcome] | None = None,
    ) -> None:
        self.definition = ToolDefinition(
            tool_id=ToolId(tool_id),
            description=f"test tool {tool_id}",
            input_schema={"type": "object"},
            workspace_requirement=workspace,
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


def note_writer(invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome(
        ToolStatus.SUCCEEDED,
        text_result("complete"),
        artifact_mutations=(ArtifactMutation(kind="note", content=invocation.arguments.get("text")),),
    )


class FakeEnvironment:
    def __init__(self, *tools: RecordingTool) -> None:
        self._tools = tools

    @property
    def kind(self) -> EnvironmentKind:
        return FAKE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    async def observe(
        self, scope: TrustedScope, bindings: Sequence[BindingAuthority]
    ) -> Sequence[BindingObservation]:
        return [BindingObservation(b.binding_id, ConnectionStatus.UNKNOWN, datetime.now(UTC)) for b in bindings]


def runtime(
    script: Iterable[ScriptStep] = (),
    *,
    tools: Iterable[ToolHandler] = (),
    env_tools: Iterable[RecordingTool] = (),
    store: Store | None = None,
    provider: ScriptedAdapter | None = None,
    context_budget: ContextBudget | None = None,
    instructions: Sequence[str] | None = None,
    max_runs_per_drive: int | None = 16,
    turn_budget: int | None = None,
    no_progress_runs: int | None = None,
    external_defaults: Mapping[str, bool] | None = None,
    models: ModelCatalog | None = None,
    image_views: ViewMaker | None = None,
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
        turn_budget=turn_budget,
        no_progress_runs=no_progress_runs,
        external_defaults=external_defaults,
        models=models,
        image_views=image_views,
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
                      model: str = "m") -> Any:
    """A ModelInput for attachment and image tests (V3-P67)."""
    from himari.data.api import PersistedStateBasis, PromptBasis
    from himari.prompt.api import CurrentStateProjection, ModelInput, RequestOptions
    return ModelInput(stable_prefix=("You are HIMARI.",), tool_catalog=(), conversation=tuple(parts),
                      current_state=CurrentStateProjection(PersistedStateBasis(0, 0, (), (), ())),
                      request_options=RequestOptions(model, image_input=image_input), trigger_refs=(),
                      prompt_basis=PromptBasis(0, 0), attachments=dict(attachments))
