"""MCP protocol revisions (V3-P59 §5.2): how a connection starts and what
it may answer. One revision is implemented, 2025-11-25, which also accepts
servers answering 2025-06-18 or 2025-03-26 (the initialize-handshake
revisions). 2026-07-28 (``server/discover``, no handshake) is added as a
revision of its own (V3-P61) without touching the transports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from himari.mcp.jsonrpc import McpRefused

CLIENT_INFO = {"name": "himari", "version": "3"}


class Revision2025_11_25:
    version = "2025-11-25"  # the newest revision with the initialize handshake
    supported = frozenset({"2025-11-25", "2025-06-18", "2025-03-26"})
    initialize = "initialize"
    initialized = "notifications/initialized"

    def initialize_params(self, capabilities: Mapping[str, Any]) -> dict[str, Any]:
        return {"protocolVersion": self.version, "capabilities": dict(capabilities), "clientInfo": CLIENT_INFO}

    def accept(self, result: Mapping[str, Any]) -> str:
        """The revision the server answered with; McpRefused if unsupported."""
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in self.supported:
            raise McpRefused(f"unsupported MCP protocol version {version!r}")
        return version


REVISION = Revision2025_11_25()
