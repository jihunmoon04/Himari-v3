"""The durable P0 application the Web server and CLI are built on.

One data root, private to the server user::

    <data_root>/                 0700
        himari.db (+ .lock)      SQLite Data (V3-P42), 0600
        workspaces/<ws>/         Inside Workspaces, one per principal (0700)
        control/operator.secret  the local operator's credential (0600)

``Application.open`` checks the layout, opens the store (single writer),
starts the Sandbox when asked and the host supports it, and composes the
runtime with a background runner. Interfaces talk only to this object.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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
    ) -> Application:
        window = provider.describe_capabilities(model_id).context_tokens
        # Compaction works toward the budget; only the model's window refuses a Run.
        budget = ContextBudget(context_limit(window, context_tokens), hard_limit_tokens=window)
        root = Path(os.path.abspath(data_root))
        ensure_private_dir(str(root))
        workspaces = root / "workspaces"
        ensure_private_dir(str(workspaces))
        secret = ensure_operator_secret(str(root / "control"))

        store = SqliteStore(root / DATABASE_NAME)  # first: a second server on this root fails here
        try:
            backend: SandboxBackend | None = None
            state = SandboxState.UNAVAILABLE
            if sandbox:
                candidate = SandboxBackend.discover()
                state = await candidate.start(protected_paths=(str(root), str(workspaces)))
                if state is SandboxState.ENFORCED:
                    backend = candidate  # run_command is offered only when enforcement is proven (V3-050)
            inside = InsideEnvironment(workspaces, sandbox=backend)
            runtime = build_runtime(provider, store=store, environments=[inside], tools=tools, model_id=model_id,
                                    reasoning_effort=reasoning_effort, context_budget=budget,
                                    instructions=DEFAULT_INSTRUCTIONS if instructions is None else instructions)
        except BaseException:
            store.close()
            raise
        assert isinstance(runtime.data, SqliteData)
        return cls(root, store, runtime.data, runtime, BackgroundRunner(runtime.driver), inside, state, model_id,
                   budget.limit_tokens, secret)

    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """For display (Web/CLI): the next request's estimated size and the
        latest compaction. Authorizes like any read (NOT_AUTHORIZED)."""
        return await self.runtime.driver.context_status(scope)

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
            self.store.close()

    def check_layout(self) -> None:
        """Re-verify the private directories (e.g. before serving)."""
        for path in (self.data_root, self.data_root / "workspaces", self.data_root / "control"):
            check_private_dir(str(path))
