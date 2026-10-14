"""MCP Tools as HIMARI External Tools (V3-P52/P53).

``connect`` starts every configured server, lists its Tools and registers
only the ones the operator listed, under the operator's effect profile. A
server that cannot be reached at startup, or whose answers cannot be used,
is left out (its Tools are not offered until a restart) and reported in its
``SourceStatus``; so is a listed Tool the server does not have, offers
twice, or describes in a way HIMARI refuses. Nothing a server sends can
make ``connect`` — or another server's connection — fail.

What a server says is data, not instructions or trust: its description is
cut to ``MAX_DESCRIPTION_CHARS``, its schema is checked like any other and
validated in a child process (V3-P52), and its results reach the model only
as Tool results.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from himari.common import JsonValue, ModelToolResult, TextBlock
from himari.tool.api import (
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)
from himari.tool.mcp.client import McpClient, McpError, McpRefused
from himari.tool.mcp.config import McpConfig, McpServerConfig, McpToolConfig
from himari.tool.schema import SchemaError, check_tool_input_schema

MAX_DESCRIPTION_CHARS = 2000
MAX_RESULT_CHARS = 64 * 1024  # what the model sees of one result
MAX_RAW_RESULT_CHARS = 256 * 1024  # what the execution record keeps
STARTUP_SECONDS = 15.0


@dataclass(frozen=True)
class SourceStatus:
    """For display: one configured server after startup."""

    source: str
    enabled_by_default: bool
    available: bool
    tool_ids: tuple[str, ...]  # the registered ones
    problems: tuple[str, ...] = ()


class McpTool:
    def __init__(self, client: McpClient, source: str, config: McpToolConfig, listed: Mapping[str, Any]) -> None:
        description = listed.get("description")
        description = description if isinstance(description, str) and description.strip() else config.name
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[: MAX_DESCRIPTION_CHARS - 1] + "…"
        schema = listed.get("inputSchema")
        if not isinstance(schema, dict):
            raise SchemaError("$: the server gave no input schema")
        check_tool_input_schema(schema)
        self._client = client
        self._name = config.name
        self.definition = ToolDefinition(
            tool_id=config.tool_id,
            description=description,
            input_schema=schema,  # deep-frozen by the definition
            workspace_requirement=WorkspaceRequirement.NONE,
            effect_profile=config.effect,
            external_source=source,
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        """A definite answer, or ``McpRefused`` turned into FAILED (the Tool
        did not run). Anything uncertain raises, so a claimed call is never
        reported as failed when it may have taken effect (V3-045/047)."""
        try:
            result = await self._client.call_tool(self._name, invocation.arguments)
        except McpRefused as refused:
            return ToolOutcome(
                ToolStatus.FAILED,
                ModelToolResult((TextBlock("The tool's server did not run this call."),), is_error=True),
                raw_result={"server": self.definition.external_source, "tool": self._name, "refused": str(refused)},
            )
        failed = result.get("isError") is True
        return ToolOutcome(
            ToolStatus.FAILED if failed else ToolStatus.SUCCEEDED,
            ModelToolResult((TextBlock(render_result(result)),), is_error=failed),
            raw_result={"server": self.definition.external_source, "tool": self._name, "result": _bounded(result)},
        )


def render_result(result: Mapping[str, Any]) -> str:
    """The model's view of a ``tools/call`` result: its text content, a
    placeholder for content it cannot read here, else the structured
    content as JSON; cut to MAX_RESULT_CHARS."""
    parts: list[str] = []
    content = result.get("content")
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif kind == "resource" and isinstance(item.get("resource"), dict):
            resource = item["resource"]
            text = resource.get("text")
            parts.append(text if isinstance(text, str) else f"[resource {resource.get('uri', '')} omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {item.get('name', '')} {item.get('uri', '')}]")
        else:
            parts.append(f"[{kind} content omitted ({item.get('mimeType', 'unknown type')})]")
    if not parts and "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts) if parts else "(no content)"
    if len(text) > MAX_RESULT_CHARS:
        text = text[: MAX_RESULT_CHARS - 40] + "\n[… the rest of the result was cut]"
    return text


def _bounded(result: Mapping[str, Any]) -> JsonValue:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_RAW_RESULT_CHARS:
        return {"truncated": True, "chars": len(text)}
    value: JsonValue = json.loads(text)
    return value


@dataclass
class McpConnection:
    handlers: list[McpTool]
    statuses: tuple[SourceStatus, ...]
    clients: list[McpClient]

    @property
    def defaults(self) -> dict[str, bool]:
        return {s.source: s.enabled_by_default for s in self.statuses}

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


async def _start(server: McpServerConfig, transport: httpx.AsyncBaseTransport | None) -> tuple[
        McpClient, list[McpTool], SourceStatus]:
    client = McpClient(server.url, server.token, timeout_seconds=server.timeout_seconds, transport=transport)
    try:
        async with asyncio.timeout(STARTUP_SECONDS):
            await client.initialize()
            listing = await client.list_tools()
    except Exception as error:  # noqa: BLE001 — whatever the server did, only this server is left out
        if isinstance(error, TimeoutError):
            reason = "no answer in time"
        elif isinstance(error, McpError):
            reason = str(error)
        else:
            reason = f"its answer could not be used ({type(error).__name__})"
        return client, [], SourceStatus(server.name, server.enabled_by_default, False, (), (f"unavailable: {reason}",))
    listed: dict[str, Mapping[str, Any]] = {}
    duplicated: set[str] = set()
    for entry in listing:
        name = entry.get("name")
        if isinstance(name, str):
            if name in listed:
                duplicated.add(name)
            listed[name] = entry
    handlers: list[McpTool] = []
    problems: list[str] = []
    for tool in server.tools:
        if tool.name in duplicated:
            problems.append(f"{tool.name}: the server offers it more than once")
            continue
        if tool.name not in listed:
            problems.append(f"{tool.name}: the server does not offer it")
            continue
        try:
            handlers.append(McpTool(client, server.name, tool, listed[tool.name]))
        except SchemaError as error:
            problems.append(f"{tool.name}: input schema refused ({error})")
        except Exception as error:  # noqa: BLE001 — a malformed description leaves out this Tool only
            problems.append(f"{tool.name}: its description could not be used ({type(error).__name__})")
    status = SourceStatus(server.name, server.enabled_by_default, True,
                          tuple(h.definition.tool_id for h in handlers), tuple(problems))
    return client, handlers, status


async def connect(config: McpConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> McpConnection:
    """Every server at once; never raises for an unreachable server."""
    started: Sequence[tuple[McpClient, list[McpTool], SourceStatus]] = await asyncio.gather(
        *(_start(server, transport) for server in config.servers))
    return McpConnection(
        handlers=[h for _, handlers, _ in started for h in handlers],
        statuses=tuple(status for _, _, status in started),
        clients=[client for client, _, _ in started],
    )
