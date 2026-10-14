"""Tool public contract: the ToolHandler SPI plus catalog/dispatch ports.

Source documents: docs/contracts.md §9 (TOOL-01/02/07), docs/decisions.md
V3-040~042, V3-044/045, V3-P13, V3-P17, V3-P19.

- Every Tool — Internal (memory/plan/note), Environment-provided
  (``EnvironmentPlugin.tools()``), External (run by a server outside HIMARI,
  e.g. an MCP server; V3-P52) or Control's ``ask_user`` — implements
  ``ToolHandler``.
- Tool implementations never touch the execution ledger: they return a
  ``ToolOutcome`` and the Dispatcher commits it (V3-042).
- For Environment-provided and External Tools with ``effect_profile != READ``
  the Dispatcher persists ``EXECUTION_CLAIMED`` before calling ``run`` (V3-045,
  V3-P52). Internal Tools have no effects outside their ToolOutcome, so they
  are never claimed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import (
    DeepFrozen,
    CallId,
    CallRef,
    EnvironmentKind,
    JsonValue,
    ModelToolResult,
    RunId,
    ToolCall,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
)
from himari.data.api import ArtifactMutation, ArtifactStore, BindingAuthority, ConversationWorkspace


class WorkspaceRequirement(Enum):
    NONE = "none"
    REQUIRED = "required"  # call must carry a workspace_binding_id
    DEFAULT = "default"  # explicit binding or the Conversation's default for the kind


class EffectProfile(Enum):
    READ = "read"  # safe to re-run if left PENDING after a crash
    SCOPED_WRITE = "scoped_write"
    UNKNOWN = "unknown"  # e.g. run_command: serialized per Workspace


class ToolOrigin(Enum):
    """Who carries out a Tool's effects (V3-P52)."""

    INTERNAL = "internal"  # effects are only its ToolOutcome
    ENVIRONMENT = "environment"  # an Environment acting on a Workspace
    EXTERNAL = "external"  # a server outside HIMARI (e.g. MCP); its effects are not visible here


class Completion(Enum):
    IMMEDIATE = "immediate"
    SUSPENDING = "suspending"  # P0: only ask_user; ``run`` is never called


class ConversationRecord(Enum):
    FULL = "full"  # tool_result Event stores model_result exactly
    OMIT = "omit"  # only the ToolExecution record keeps it


@dataclass(frozen=True)
class ToolDefinition(DeepFrozen):
    tool_id: ToolId  # "<environment_kind>/<name>" (V3-008) or "<external_source>/<name>"
    description: str
    input_schema: Mapping[str, JsonValue]  # JSON Schema of arguments
    workspace_requirement: WorkspaceRequirement
    effect_profile: EffectProfile
    completion: Completion = Completion.IMMEDIATE
    environment_kind: EnvironmentKind | None = None  # set iff Environment-provided
    # Set iff External: the name of the server that runs the Tool (e.g. an
    # MCP server). An External Tool needs no Workspace and is always in the
    # catalog; its effect_profile is the operator's judgement, not the server's.
    external_source: str | None = None
    conversation_record: ConversationRecord = ConversationRecord.FULL
    # Use REQUIRE_USER for effects the model cannot verify with other Tools
    # (e.g. sending a message to a third party).
    unknown_outcome: UnknownOutcomePolicy = UnknownOutcomePolicy.REPORT_TO_MODEL

    @property
    def origin(self) -> ToolOrigin:
        if self.environment_kind is not None:
            return ToolOrigin.ENVIRONMENT
        if self.external_source is not None:
            return ToolOrigin.EXTERNAL
        return ToolOrigin.INTERNAL


@dataclass(frozen=True)
class ToolInvocation(DeepFrozen):
    tool_id: ToolId
    call_id: CallId  # for idempotency/logging only; not a ledger handle
    arguments: Mapping[str, JsonValue]


@dataclass(frozen=True)
class ToolContext(DeepFrozen):
    """What a handler may use. Write effects on Data go through ToolOutcome."""

    scope: TrustedScope
    artifacts: ArtifactStore  # read-only use
    binding: BindingAuthority | None = None  # resolved by Dispatcher; re-validate before effects
    # The working directory of *this* Binding (its Workspace + cwd, DATA-11),
    # so each Environment sees its own. Never a host path.
    workspace: ConversationWorkspace | None = None


class ToolStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolOutcome(DeepFrozen):
    """A truthful, definite result. A handler that cannot know whether its
    effect happened must raise instead of guessing; the record then stays
    EXECUTION_CLAIMED and becomes UNKNOWN."""

    status: ToolStatus
    model_result: ModelToolResult
    artifact_mutations: tuple[ArtifactMutation, ...] = ()
    raw_result: JsonValue = None


@runtime_checkable
class ToolHandler(Protocol):
    """TOOL-07 SPI. One instance per Tool; Environment Tools are per kind,
    never per Environment Instance (V3-P19)."""

    @property
    def definition(self) -> ToolDefinition: ...

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome: ...


# --- Ports consumed by Control / Prompt ------------------------------------


class ToolCatalog(Protocol):
    """TOOL-01."""

    def catalog(self, active_kinds: frozenset[EnvironmentKind]) -> Sequence[ToolDefinition]:
        """Internal Tools + ask_user + Tools of kinds with an active Instance.
        Stable order (prefix-cache friendly). Exposure is not authorization."""
        ...


class GroupState(Enum):
    COMPLETED = "completed"  # every call has a model-facing result → next Run allowed
    WAITING_FOR_USER = "waiting_for_user"  # single ask_user left PENDING
    BLOCKED_UNKNOWN = "blocked_unknown"  # an uncertain call needs a user (REQUIRE_USER)


@dataclass(frozen=True)
class GroupDispatchResult(DeepFrozen):
    run_id: RunId
    state: GroupState
    waiting_call: CallRef | None = None
    unknown_calls: tuple[CallRef, ...] = ()


class ToolDispatcher(Protocol):
    """TOOL-02/03: validates, schedules, claims, runs and commits a group."""

    async def dispatch_group(
        self, scope: TrustedScope, run_id: RunId, calls: Sequence[ToolCall]
    ) -> GroupDispatchResult:
        """Runs only never-claimed PENDING calls (so it is also the resume
        path, TOOL-04). Unknown tool / invalid arguments / binding failures
        become DENIED or FAILED results, not exceptions. Uncertain calls of
        REPORT_TO_MODEL Tools get an "outcome unknown" result instead of
        being re-run (V3-047)."""
        ...
