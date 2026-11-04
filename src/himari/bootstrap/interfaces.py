"""V3-P93: UI support resolved by the composition root, independently of model capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from himari.common import PrincipalId, TrustedScope
from himari.data.api import InterfaceCapabilities, InterfaceKind, InterfaceSnapshot, MessageOrigin, interface_kind


@dataclass(frozen=True)
class ConfiguredInterfaces:
    file_download: bool = False
    workspace_browser: bool = False
    workspace_principals: frozenset[PrincipalId] = frozenset()

    async def read(self, scope: TrustedScope, origin: MessageOrigin | None) -> InterfaceSnapshot:
        kind = interface_kind(origin)
        # Device file support stays false until the connection-generation/file protocol exists.
        return InterfaceSnapshot(kind, InterfaceCapabilities(
            file_download=self.file_download and kind in (InterfaceKind.WEB, InterfaceKind.CLI),
            workspace_browser=(self.workspace_browser and kind is InterfaceKind.WEB
                               and scope.principal_id in self.workspace_principals)))
