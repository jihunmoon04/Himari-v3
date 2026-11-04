"""V3-P93: UI support resolved by the composition root, independently of model capabilities."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from himari.common import PrincipalId, TrustedScope
from himari.data.api import (
    InterfaceCapabilities,
    InterfaceKind,
    InterfaceSnapshot,
    MessageOrigin,
    OriginKind,
    interface_kind,
)
from himari.presence.api import Presence


class StaleConnectionGeneration(Exception):
    """V3-P93 §6.1.2: the device stream generation was replaced, closed, or does not match."""


class ConnectionGenerationRequired(Exception):
    """V3-P93 §6.1.2: new client feature declarations require X-Himari-Connection-Generation."""


@dataclass(frozen=True)
class DeviceInputContext:
    connection_generation: str | None = None
    client_file_download: bool | None = None


_device_input_context: ContextVar[DeviceInputContext | None] = ContextVar("device_input_context", default=None)


@contextmanager
def device_input_negotiation(connection_generation: str | None = None,
                             client_file_download: bool | None = None) -> Iterator[None]:
    if connection_generation is None and client_file_download is None:
        yield
        return
    token = _device_input_context.set(DeviceInputContext(connection_generation, client_file_download))
    try:
        yield
    finally:
        _device_input_context.reset(token)


@dataclass(frozen=True)
class ConfiguredInterfaces:
    file_download: bool = False
    workspace_browser: bool = False
    workspace_principals: frozenset[PrincipalId] = frozenset()
    hub: Presence | None = None
    companion_enabled: bool = True

    async def read(self, scope: TrustedScope, origin: MessageOrigin | None) -> InterfaceSnapshot:
        kind = interface_kind(origin)
        if kind in (InterfaceKind.WEB, InterfaceKind.CLI):
            file_download = self.file_download
        elif origin is not None and origin.kind is OriginKind.WAKE and origin.device_id:
            file_download = bool(self.file_download and self.companion_enabled and self.hub is not None
                                 and self.hub.device_file_download(scope.principal_id, origin.device_id))
        elif origin is not None and origin.kind is OriginKind.DEVICE and origin.device_id:
            ctx = _device_input_context.get()
            if ctx is not None:
                if ctx.connection_generation is None and ctx.client_file_download is not None:
                    raise ConnectionGenerationRequired
                if ctx.connection_generation is not None:
                    if self.hub is None or not self.hub.valid_generation(
                            scope.principal_id, origin.device_id, ctx.connection_generation):
                        raise StaleConnectionGeneration
                    file_download = bool(self.file_download and self.companion_enabled and ctx.client_file_download)
                else:
                    file_download = False
            else:
                file_download = False
        else:
            file_download = False
        return InterfaceSnapshot(kind, InterfaceCapabilities(
            file_download=file_download,
            workspace_browser=(self.workspace_browser and kind is InterfaceKind.WEB
                               and scope.principal_id in self.workspace_principals)))
