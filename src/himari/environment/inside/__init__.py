"""Inside Environment: the HIMARI-managed default Workspace (V3-030, V3-033, V3-035).

Host layout (never shown to the model)::

    <workspaces_root>/<workspace_id>/          one per principal
        conversations/<conversation_id>/       each Conversation's default cwd

Sandbox/model view: ``/workspace/...``. Conversation folders are an
organizing convention, not a security boundary: a principal's Conversations
may read and write each other's folders. The boundary is the principal's
Workspace root, enforced on every access by :mod:`.fs`.

This package is an EnvironmentPlugin (ENV-06): it provides its own Tools
(V3-041). ``run_command`` and the Sandbox come in a later packet.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from himari.common import EnvironmentKind, TrustedScope, WorkspaceId
from himari.data.api import BindingAuthority
from himari.environment.api import BindingObservation, ConnectionStatus
from himari.environment.inside.tools import file_tools
from himari.tool.api import ToolHandler

INSIDE = EnvironmentKind("inside")
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InsideEnvironment:
    def __init__(self, workspaces_root: str | Path) -> None:
        root = Path(workspaces_root)
        if not root.is_absolute():
            raise ValueError("workspaces_root must be an absolute, trusted path")
        self._root = root
        self._tools: tuple[ToolHandler, ...] = tuple(file_tools(self))

    @property
    def kind(self) -> EnvironmentKind:
        return INSIDE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    def host_root(self, workspace_id: WorkspaceId) -> str:
        """Trusted mapping workspace_id → host directory. The id comes from
        Data (an authorized Binding), never from Tool arguments."""
        if not _WORKSPACE_ID.match(workspace_id):
            raise ValueError("invalid workspace id")
        return str(self._root / workspace_id)

    def provision(self, workspace_id: WorkspaceId) -> None:
        """Administrative setup of a principal's Workspace (not model-reachable)."""
        root = Path(self.host_root(workspace_id))
        (root / "conversations").mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)

    async def observe(
        self, scope: TrustedScope, bindings: Sequence[BindingAuthority]
    ) -> Sequence[BindingObservation]:
        now = datetime.now(UTC)
        observations = []
        for binding in bindings:
            try:
                status = ConnectionStatus.ONLINE if Path(self.host_root(binding.workspace_id)).is_dir() else ConnectionStatus.OFFLINE
            except ValueError:
                status = ConnectionStatus.OFFLINE
            observations.append(BindingObservation(binding.binding_id, status, now))
        return observations
