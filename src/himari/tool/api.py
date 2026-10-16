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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
# Tool views (MCP Apps, V3-P58): owned by Data, re-exported here so that a
# Tool implementation (e.g. the MCP connector, which may import only
# tool.api — V3-059) can build them.
from himari.data.api import MAX_APP_PAYLOAD_BYTES as MAX_APP_PAYLOAD_BYTES
from himari.data.api import MAX_UI_HTML_BYTES as MAX_UI_HTML_BYTES
from himari.data.api import AppView as AppView
from himari.data.api import UiCsp as UiCsp
from himari.data.api import UiResource as UiResource


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
    # MCP server). An External Tool needs no Workspace; it is in the catalog
    # and may run only where its source is enabled (ExternalAccess, V3-P53).
    # Its effect_profile is the operator's judgement, not the server's.
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


class SchemaError(ValueError):
    """A Tool input schema HIMARI refuses to register (V3-P52)."""


# Checks a Tool input schema the way the Registry will (V3-P52); raises
# SchemaError. Given to a connector (e.g. MCP) by the composition root, so it
# can leave out one Tool whose schema would be refused, before registering.
InputSchemaCheck = Callable[[Mapping[str, JsonValue]], None]


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
    # V3-P58: the call's view, stored with the result by the Dispatcher. Kept
    # only when the call ran (SUCCEEDED/FAILED); a view Data refuses is
    # dropped and the result is committed without it.
    app_view: AppView | None = None


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

    def catalog(
        self, active_kinds: frozenset[EnvironmentKind], enabled_sources: frozenset[str] = frozenset()
    ) -> Sequence[ToolDefinition]:
        """Internal Tools + ask_user + Tools of kinds with an active Instance
        + External Tools of enabled sources. Stable order (prefix-cache
        friendly). Exposure is not authorization."""
        ...


class ExternalAccess(Protocol):
    """V3-P53: the External Tool sources a Conversation may use. The catalog
    (what the model sees) and the Dispatcher (what runs, checked again right
    before each call) both ask it, so turning a source off mid-turn denies
    calls the model already made."""

    async def enabled_sources(self, scope: TrustedScope) -> frozenset[str]: ...


class GroupState(Enum):
    COMPLETED = "completed"  # every call has a model-facing result → next Run allowed
    WAITING_FOR_USER = "waiting_for_user"  # single ask_user left PENDING
    BLOCKED_UNKNOWN = "blocked_unknown"  # an uncertain call needs a user (REQUIRE_USER)
    PAUSED = "paused"  # a pause stopped it before a call started; the rest stays PENDING (V3-P55)


@dataclass(frozen=True)
class GroupDispatchResult(DeepFrozen):
    run_id: RunId
    state: GroupState
    waiting_call: CallRef | None = None
    unknown_calls: tuple[CallRef, ...] = ()
    # The calls the Dispatcher refused before running, by its own rule (V3-P62
    # §4): returned, never stored. A handler's own DENIED is not here.
    refusals: Mapping[CallId, RefusalRule] = field(default_factory=dict)


class RefusalRule(Enum):
    """Why the Dispatcher refused a call before running it (V3-P62 §4)."""

    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    SOURCE_DISABLED = "source_disabled"
    ENVIRONMENT_INACTIVE = "environment_inactive"
    BINDING = "binding"

    @property
    def by_input(self) -> bool:
        """Decided by the call alone: the same call is refused the same way, whatever the state."""
        return self in (RefusalRule.UNKNOWN_TOOL, RefusalRule.INVALID_ARGUMENTS)


class ToolDispatcher(Protocol):
    """TOOL-02/03: validates, schedules, claims, runs and commits a group."""

    async def dispatch_group(
        self,
        scope: TrustedScope,
        run_id: RunId,
        calls: Sequence[ToolCall],
        *,
        pause: Callable[[], bool] | None = None,
    ) -> GroupDispatchResult:
        """Runs only never-claimed PENDING calls (so it is also the resume
        path, TOOL-04). Unknown tool / invalid arguments / binding failures
        become DENIED or FAILED results, not exceptions. Uncertain calls of
        REPORT_TO_MODEL Tools get an "outcome unknown" result instead of
        being re-run (V3-047).

        ``pause`` is asked before each call starts; once it returns True no
        further call starts and the group is PAUSED (V3-P55). A suspending
        call (``ask_user``) is not paused: it waits for the user anyway.
        Cancelled (asyncio) during a call, the call is left as it is —
        PENDING if it was not claimed, claimed otherwise — for
        ``cancel_group`` to settle."""
        ...

    async def cancel_group(self, scope: TrustedScope, run_id: RunId, reason: str) -> GroupDispatchResult:
        """V3-P55: settle a group that will not be run further. Every
        never-claimed PENDING call (a waiting ``ask_user`` included) gets a
        CANCELLED result telling the model ``reason``; it never runs. Claimed
        calls are uncertain and are treated as in ``dispatch_group``:
        REPORT_TO_MODEL ones get "outcome unknown", REQUIRE_USER ones leave
        the group BLOCKED_UNKNOWN. Otherwise the group is COMPLETED."""
        ...
