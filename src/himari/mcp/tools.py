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

Views (MCP Apps, V3-P58), only for a server whose ``ui`` the operator set:
each registered Tool's ``_meta.ui.resourceUri`` is read with
``resources/read`` once, at connect, and checked; a view that cannot be
used is left out (the Tool is not). When the Tool runs, its result carries
that same view (``ToolOutcome.app_view``), so no request is added after
``tools/call``. A Tool whose ``visibility`` leaves out ``"model"`` is not
registered at all, whatever the operator listed: nothing can call it yet.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from himari.common import JsonValue, ModelToolResult, TextBlock
from himari.mcp.client import UI_MIME_TYPE, McpClient, McpError, McpRefused, http_client
from himari.mcp.config import McpConfig, McpServerConfig, McpToolConfig, McpUiPolicy
from himari.tool.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_UI_HTML_BYTES,
    AppView,
    InputSchemaCheck,
    SchemaError,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    UiCsp,
    UiResource,
    WorkspaceRequirement,
)

MAX_DESCRIPTION_CHARS = 2000
MAX_RESULT_CHARS = 64 * 1024  # what the model sees of one result
MAX_RAW_RESULT_CHARS = 256 * 1024  # what the execution record keeps
STARTUP_SECONDS = 15.0
UI_STARTUP_SECONDS = 15.0  # reading one server's views, after its Tools
MAX_UI_BYTES_PER_SERVER = 8 * 1024 * 1024
MAX_RESOURCE_URI_CHARS = 2048
MAX_DECLARED_DOMAINS = 64
MAX_DECLARED_DOMAIN_CHARS = 512
_VISIBILITY = frozenset({"model", "app"})


@dataclass(frozen=True)
class SourceStatus:
    """For display: one configured server after startup."""

    source: str
    enabled_by_default: bool
    available: bool
    tool_ids: tuple[str, ...]  # the registered ones
    problems: tuple[str, ...] = ()


class McpTool:
    def __init__(self, client: McpClient, source: str, config: McpToolConfig, listed: Mapping[str, Any],
                 check_schema: InputSchemaCheck) -> None:
        description = listed.get("description")
        description = description if isinstance(description, str) and description.strip() else config.name
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[: MAX_DESCRIPTION_CHARS - 1] + "…"
        schema = listed.get("inputSchema")
        if not isinstance(schema, dict):
            raise SchemaError("$: the server gave no input schema")
        check_schema(schema)
        self._client = client
        self._name = config.name
        # For a view's ui/initialize (hostContext.toolInfo.tool): the Tool as
        # the server listed it, under its own name.
        self.info: Mapping[str, Any] = {"name": config.name, "inputSchema": schema,
                                        **({"description": listed["description"]}
                                           if isinstance(listed.get("description"), str) else {})}
        self.ui: UiResource | None = None  # set by connect when this Tool's view could be read
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
        raw: dict[str, JsonValue] = {"server": self.definition.external_source, "tool": self._name,
                                     "result": _bounded(result)}
        view: AppView | None = None
        if self.ui is not None:
            payload = {k: result[k] for k in ("content", "structuredContent", "_meta", "isError") if k in result}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
                raw["ui_problem"] = f"the result is larger than {MAX_APP_PAYLOAD_BYTES} bytes"
            else:
                view = AppView(self.ui, payload)
        return ToolOutcome(
            ToolStatus.FAILED if failed else ToolStatus.SUCCEEDED,
            ModelToolResult((TextBlock(render_result(result)),), is_error=failed),
            raw_result=raw,
            app_view=view,
        )


def ui_meta(listed: Mapping[str, Any]) -> tuple[bool, object]:
    """(model may call it, resourceUri as given) from a ``tools/list`` entry.
    ValueError when ``_meta.ui`` cannot be read: then whether the model may
    call the Tool is not known, so the caller leaves it out."""
    meta = listed.get("_meta")
    ui = meta.get("ui") if isinstance(meta, dict) else None
    if ui is None:
        return True, None
    if not isinstance(ui, dict):
        raise ValueError("_meta.ui is not an object")
    visibility = ui.get("visibility", ["model", "app"])
    if not isinstance(visibility, list) or not all(v in _VISIBILITY for v in visibility):
        raise ValueError('_meta.ui.visibility is not a list of "model" and "app"')
    return "model" in visibility, ui.get("resourceUri")


def ui_resource(source: str, uri: str, answer: Mapping[str, Any]) -> UiResource:
    """A ``resources/read`` answer as a checked view (MCP Apps 2026-01-26:
    one entry for the URI, ``text/html;profile=mcp-app``, ``text`` or base64
    ``blob``). ValueError says why it cannot be used. ``permissions`` and
    ``domain`` are not kept: phase 1 grants neither."""
    contents = answer.get("contents")
    entries = [c for c in contents if isinstance(c, dict) and c.get("uri") == uri] if isinstance(contents, list) else []
    if len(entries) != 1:
        raise ValueError("the answer has no single entry for this URI")
    entry = entries[0]
    mime = entry.get("mimeType")
    if not isinstance(mime, str) or "".join(mime.split()).lower() != UI_MIME_TYPE:
        raise ValueError(f"its mimeType is not {UI_MIME_TYPE}")
    text, blob = entry.get("text"), entry.get("blob")
    if isinstance(text, str) and blob is None:
        html = text
    elif isinstance(blob, str) and text is None:
        try:
            html = base64.b64decode(blob, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("its blob is not base64 UTF-8") from None
    else:
        raise ValueError("it needs exactly one of text or blob")
    if len(html.encode("utf-8")) > MAX_UI_HTML_BYTES:
        raise ValueError(f"its HTML is larger than {MAX_UI_HTML_BYTES} bytes")
    meta = entry.get("_meta")
    ui = meta.get("ui", {}) if isinstance(meta, dict) else {}
    csp = ui.get("csp", {}) if isinstance(ui, dict) else None
    if not isinstance(csp, dict):
        raise ValueError("its _meta.ui.csp is not an object")

    def domains(key: str) -> tuple[str, ...]:
        value = csp.get(key, [])
        if (not isinstance(value, list) or len(value) > MAX_DECLARED_DOMAINS
                or not all(isinstance(d, str) and len(d) <= MAX_DECLARED_DOMAIN_CHARS for d in value)):
            raise ValueError(f"its _meta.ui.csp.{key} is not a list of at most {MAX_DECLARED_DOMAINS} origins")
        return tuple(value)

    border = ui.get("prefersBorder") if isinstance(ui, dict) else None
    return UiResource.create(
        source, uri, html,
        UiCsp(domains("connectDomains"), domains("resourceDomains"), domains("frameDomains"),
              domains("baseUriDomains")),
        border if isinstance(border, bool) else None,
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
    # V3-P58: what each configured server's views may reach (None: not shown).
    ui_policies: dict[str, McpUiPolicy | None] = field(default_factory=dict)

    @property
    def defaults(self) -> dict[str, bool]:
        return {s.source: s.enabled_by_default for s in self.statuses}

    def _handler(self, tool_id: str) -> McpTool | None:
        return next((h for h in self.handlers if h.definition.tool_id == tool_id), None)

    def ui_for(self, tool_id: str) -> UiResource | None:
        """The view read at connect for this Tool: the one its results carry."""
        handler = self._handler(tool_id)
        return None if handler is None else handler.ui

    def tool_info(self, tool_id: str) -> Mapping[str, Any] | None:
        """The Tool as its server listed it (name, description, inputSchema)."""
        handler = self._handler(tool_id)
        return None if handler is None else handler.info

    def ui_policy(self, source: str) -> McpUiPolicy | None:
        return self.ui_policies.get(source)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


async def _start(server: McpServerConfig, check_schema: InputSchemaCheck,
                 transport: httpx.AsyncBaseTransport | None) -> tuple[McpClient, list[McpTool], SourceStatus]:
    client = http_client(server.url, server.token, timeout_seconds=server.timeout_seconds, transport=transport,
                       ui=server.ui is not None)
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
    wanted: dict[str, list[McpTool]] = {}  # view URI → the Tools showing it
    for tool in server.tools:
        if tool.name in duplicated:
            problems.append(f"{tool.name}: the server offers it more than once")
            continue
        if tool.name not in listed:
            problems.append(f"{tool.name}: the server does not offer it")
            continue
        try:
            model_may_call, uri = ui_meta(listed[tool.name])
        except ValueError as error:
            problems.append(f"{tool.name}: left out, {error}")
            continue
        if not model_may_call:
            problems.append(f"{tool.name}: app-only Tool: not usable until app-initiated calls are supported")
            continue
        try:
            handler = McpTool(client, server.name, tool, listed[tool.name], check_schema)
        except SchemaError as error:
            problems.append(f"{tool.name}: input schema refused ({error})")
            continue
        except Exception as error:  # noqa: BLE001 — a malformed description leaves out this Tool only
            problems.append(f"{tool.name}: its description could not be used ({type(error).__name__})")
            continue
        handlers.append(handler)
        if server.ui is None or uri is None:
            continue
        if not isinstance(uri, str) or not uri.startswith("ui://") or len(uri) > MAX_RESOURCE_URI_CHARS:
            problems.append(f"{tool.name}: view left out: resourceUri is not a ui:// URI "
                            f"of at most {MAX_RESOURCE_URI_CHARS} characters")
            continue
        wanted.setdefault(uri, []).append(handler)
    if wanted:
        problems.extend(await _read_views(client, server.name, wanted))
    status = SourceStatus(server.name, server.enabled_by_default, True,
                          tuple(h.definition.tool_id for h in handlers), tuple(problems))
    return client, handlers, status


async def _read_views(client: McpClient, source: str, wanted: dict[str, list[McpTool]]) -> list[str]:
    """Reads each wanted view once and gives it to its Tools; returns the
    problems. A view that cannot be used is left out, its Tools stay."""
    problems: list[str] = []
    total = 0
    pending = list(wanted.items())
    try:
        async with asyncio.timeout(UI_STARTUP_SECONDS):
            while pending:
                uri, tools = pending[0]
                names = ", ".join(t.info["name"] for t in tools)
                try:
                    resource = ui_resource(source, uri, await client.read_resource(uri))
                except McpError as error:
                    problems.append(f"{names}: view left out: {error}")
                except Exception as error:  # noqa: BLE001 — whatever the answer was, only this view is left out
                    problems.append(f"{names}: view left out: "
                                    + (str(error) if isinstance(error, ValueError) else type(error).__name__))
                else:
                    total += len(resource.html.encode("utf-8"))
                    if total > MAX_UI_BYTES_PER_SERVER:
                        problems.append(f"{names}: view left out: the server's views are larger than "
                                        f"{MAX_UI_BYTES_PER_SERVER} bytes")
                    else:
                        for tool in tools:
                            tool.ui = resource
                pending.pop(0)
    except TimeoutError:
        for _, tools in pending:
            problems.append(", ".join(t.info["name"] for t in tools) + ": view left out: not read in time")
    return problems


async def connect(config: McpConfig, *, check_schema: InputSchemaCheck,
                  transport: httpx.AsyncBaseTransport | None = None) -> McpConnection:
    """Every server at once; never raises for an unreachable server.
    ``check_schema`` is the Registry's own check, so a Tool it would refuse
    is left out here, with the reason, instead of failing the startup."""
    started: Sequence[tuple[McpClient, list[McpTool], SourceStatus]] = await asyncio.gather(
        *(_start(server, check_schema, transport) for server in config.servers))
    return McpConnection(
        handlers=[h for _, handlers, _ in started for h in handlers],
        statuses=tuple(status for _, _, status in started),
        clients=[client for client, _, _ in started],
        ui_policies={server.name: server.ui for server in config.servers},
    )
