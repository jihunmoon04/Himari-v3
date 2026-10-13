"""Test doubles shared by module and integration suites."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime

from himari.bootstrap import InMemoryRuntime, build_in_memory_runtime
from himari.common import (
    CallId,
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
from himari.data.api import ArtifactMutation, BindingAuthority, UserMessage
from himari.data.memory import InMemoryStore
from himari.environment.api import BindingObservation, ConnectionStatus
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
    store: InMemoryStore | None = None,
    provider: ScriptedAdapter | None = None,
) -> tuple[InMemoryRuntime, ScriptedAdapter]:
    """Fresh runtime; pass ``store`` to model a restart over persisted data."""
    adapter = provider if provider is not None else ScriptedAdapter(script)
    env_tools = tuple(env_tools)
    rt = build_in_memory_runtime(
        adapter,
        tools=tools,
        environments=[FakeEnvironment(*env_tools)] if env_tools else [],
        store=store,
    )
    if store is None:
        rt.store.create_conversation(SCOPE)
    return rt, adapter
