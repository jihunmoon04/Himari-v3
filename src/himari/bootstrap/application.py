"""The durable P0 application the Web server and CLI are built on.

One data root, private to the server user::

    <data_root>/                 0700
        himari.db (+ .lock)      SQLite Data (V3-P42), 0600
        workspaces/<ws>/         Inside Workspaces, one per principal (0700)
        control/operator.secret  the local operator's credential (0600)
        mcp.json                 optional: MCP servers to connect (0600, V3-P53)

``Application.open`` checks the layout, opens the store (single writer),
starts the Sandbox when asked and the host supports it, and composes the
runtime with a background runner. Interfaces talk only to this object.

MCP servers in ``mcp.json`` are connected at startup; one that cannot be
reached is left out until a restart (``tool_sources`` says so). Which
servers a Conversation uses follows each server's default unless the
Conversation chose otherwise (``set_tool_source``).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from himari.bootstrap import DEFAULT_INSTRUCTIONS, Runtime, build_runtime
from himari.bootstrap.operator import check_private_dir, ensure_operator_secret, ensure_private_dir
from himari.common import ConversationId, PrincipalId, TrustedScope, WorkspaceId
from himari.control.api import ContextStatus
from himari.control.runner import BackgroundRunner
from himari.data.api import InitialActivation
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.environment.inside.sandbox import SandboxBackend, SandboxState
from himari.prompt.api import ContextBudget
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.mcp.config import CONFIG_NAME, load_config
from himari.tool.mcp.tools import McpConnection, SourceStatus, connect

DATABASE_NAME = "himari.db"
# Default request budget: the model's window, capped so that a long
# Conversation is compacted before each request costs a whole large window.
DEFAULT_CONTEXT_TOKENS = 200_000
UNKNOWN_WINDOW_TOKENS = 128_000  # a model whose window the Provider does not declare


def context_limit(window: int | None, configured: int | None) -> int:
    """The token limit of one request: ``configured`` if given (it may not
    exceed a known window), else the window capped at the default."""
    if configured is not None:
        if configured <= 0:
            raise ValueError("the context budget must be positive")
        if window is not None and configured > window:
            raise ValueError(f"the context budget {configured} exceeds the model's window of {window} tokens")
        return configured
    return min(window, DEFAULT_CONTEXT_TOKENS) if window is not None else UNKNOWN_WINDOW_TOKENS


def workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's Inside Workspace id: stable, and safe as a folder name
    whatever characters the principal id has."""
    return WorkspaceId("ws_" + hashlib.sha256(principal.encode()).hexdigest()[:24])


def check_tool_sources(data_root: str | os.PathLike[str], environ: Mapping[str, str]) -> None:
    """Fails (ValueError) on an ``mcp.json`` that ``Application.open`` would
    refuse, so a server can report it before starting."""
    load_config(os.path.join(os.path.abspath(data_root), CONFIG_NAME), environ)


class UnknownToolSource(LookupError):
    """No configured External Tool source has this name."""


@dataclass(frozen=True)
class ToolSourceView:
    """One configured External Tool source as a Conversation sees it."""

    source: str
    enabled: bool  # in this Conversation, now
    enabled_by_default: bool
    choice: bool | None  # this Conversation's own choice, if any
    available: bool  # connected at startup
    tool_ids: tuple[str, ...]
    problems: tuple[str, ...]


@dataclass
class Application:
    data_root: Path
    store: SqliteStore
    data: SqliteData
    runtime: Runtime
    runner: BackgroundRunner
    inside: InsideEnvironment
    sandbox_state: SandboxState
    model_id: str
    context_tokens: int  # the request token budget (compaction starts at 80%)
    operator_secret: str = field(repr=False)  # never in reprs, logs or tracebacks
    mcp: McpConnection = field(default_factory=lambda: McpConnection([], (), []))

    @classmethod
    async def open(
        cls,
        data_root: str | os.PathLike[str],
        provider: ProviderAdapter,
        *,
        model_id: str,
        reasoning_effort: str | None = None,
        sandbox: bool = True,
        context_tokens: int | None = None,
        tools: Iterable[ToolHandler] = (),
        instructions: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None,  # where mcp.json's token variables are read
        mcp_transport: httpx.AsyncBaseTransport | None = None,  # tests: a fake MCP server
    ) -> Application:
        window = provider.describe_capabilities(model_id).context_tokens
        # Compaction works toward the budget; only the model's window refuses a Run.
        budget = ContextBudget(context_limit(window, context_tokens), hard_limit_tokens=window)
        root = Path(os.path.abspath(data_root))
        ensure_private_dir(str(root))
        workspaces = root / "workspaces"
        ensure_private_dir(str(workspaces))
        secret = ensure_operator_secret(str(root / "control"))
        mcp_config = load_config(str(root / CONFIG_NAME), os.environ if environ is None else environ)

        store = SqliteStore(root / DATABASE_NAME)  # first: a second server on this root fails here
        mcp: McpConnection | None = None
        try:
            mcp = await connect(mcp_config, transport=mcp_transport)
            backend: SandboxBackend | None = None
            state = SandboxState.UNAVAILABLE
            if sandbox:
                candidate = SandboxBackend.discover()
                state = await candidate.start(protected_paths=(str(root), str(workspaces)))
                if state is SandboxState.ENFORCED:
                    backend = candidate  # run_command is offered only when enforcement is proven (V3-050)
            inside = InsideEnvironment(workspaces, sandbox=backend)
            runtime = build_runtime(provider, store=store, environments=[inside], tools=[*tools, *mcp.handlers],
                                    model_id=model_id, reasoning_effort=reasoning_effort, context_budget=budget,
                                    instructions=DEFAULT_INSTRUCTIONS if instructions is None else instructions,
                                    external_defaults=mcp.defaults)
        except BaseException:
            try:
                if mcp is not None:
                    await mcp.aclose()
            finally:
                store.close()
            raise
        assert isinstance(runtime.data, SqliteData)
        return cls(root, store, runtime.data, runtime, BackgroundRunner(runtime.driver), inside, state, model_id,
                   budget.limit_tokens, secret, mcp)

    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """For display (Web/CLI): the next request's estimated size and the
        latest compaction. Authorizes like any read (NOT_AUTHORIZED)."""
        return await self.runtime.driver.context_status(scope)

    @property
    def tool_source_statuses(self) -> tuple[SourceStatus, ...]:
        """The configured MCP servers as connected at startup."""
        return self.mcp.statuses

    async def tool_sources(self, scope: TrustedScope) -> tuple[ToolSourceView, ...]:
        """Every configured External Tool source and whether this
        Conversation uses it. Authorizes like any read (NOT_AUTHORIZED)."""
        chosen = {s.source: s.enabled for s in await self.data.read_external_settings(scope)}
        return tuple(
            ToolSourceView(s.source, chosen.get(s.source, s.enabled_by_default), s.enabled_by_default,
                           chosen.get(s.source), s.available, s.tool_ids, s.problems)
            for s in self.mcp.statuses
        )

    async def set_tool_source(self, scope: TrustedScope, source: str, enabled: bool | None) -> None:
        """This Conversation's choice for a configured source; None: follow
        its default. A choice equal to the default is not kept (V3-P53: only
        differences are), so the Conversation keeps following the default if
        the operator changes it. Takes effect from the next model request,
        and calls the model already made are checked again before they run."""
        default = self.mcp.defaults.get(source)
        if default is None:
            raise UnknownToolSource(source)
        await self.data.set_external_setting(scope, source, None if enabled == default else enabled)

    async def create_conversation(self, principal: PrincipalId, *, title: str | None = None) -> ConversationId:
        """T0 with the principal's Inside Workspace activated (provisioned first)."""
        workspace = workspace_for(principal)
        self.inside.provision(workspace)
        return await self.data.create_conversation(
            principal, title=title, activations=(InitialActivation(INSIDE, workspace),)
        )

    async def close(self, grace_seconds: float = 30.0) -> None:
        try:
            await self.runner.shutdown(grace_seconds)
        finally:
            try:
                await self.mcp.aclose()
            finally:
                self.store.close()

    def check_layout(self) -> None:
        """Re-verify the private directories (e.g. before serving)."""
        for path in (self.data_root, self.data_root / "workspaces", self.data_root / "control"):
            check_private_dir(str(path))
