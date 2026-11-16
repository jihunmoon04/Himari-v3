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

A Tool's view (MCP Apps, ``_meta.ui``) is not shown. A Tool whose
``visibility`` leaves out ``"model"`` is not registered at all, whatever the
operator listed: nothing can call it.

stdio servers (V3-P59 §5) run only through a launcher Bootstrap gives for
each one (the server sandbox); without it the server is left out with the
reason. Their Tools are read by a listing session of their own
at connect; calls then go to the server's processes (``stdio``), keyed by
the calling Conversation.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

import httpx

from himari.common import JsonValue, ModelToolResult, TextBlock
from himari.mcp.client import McpClient, McpError, McpRefused, http_client
from himari.mcp.config import McpConfig, McpServerConfig, McpStdioConfig, McpToolConfig
from himari.mcp.resources import ResourceTool, resource_tools
from himari.mcp.stdio import RESTART_NOTICE, ListingChannel, ProcessLauncher, StdioChannel, StdioServer
from himari.tool.api import (
    MAX_IMAGE_BASE64_CHARS,
    ResultImage,
    InputSchemaCheck,
    SchemaError,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

MAX_DESCRIPTION_CHARS = 2000
MAX_RESULT_CHARS = 64 * 1024  # what the model sees of one result
MAX_RAW_RESULT_CHARS = 256 * 1024  # what the execution record keeps
STARTUP_SECONDS = 15.0
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
        key = str(ctx.scope.conversation_id)
        try:
            result = await self._client.call_tool(self._name, invocation.arguments, key=key)
        except McpRefused as refused:
            return ToolOutcome(
                ToolStatus.FAILED,
                ModelToolResult((TextBlock("The tool's server did not run this call."),), is_error=True),
                raw_result={"server": self.definition.external_source, "tool": self._name, "refused": str(refused)},
            )
        failed = result.get("isError") is True
        raw: dict[str, JsonValue] = {"server": self.definition.external_source, "tool": self._name,
                                     "result": _bounded(result)}
        images = result_images(result)
        text = render_result(result, shown=images.keys())
        if self._client.take_restart(key):  # V3-P59 §5.3: the server's earlier identifiers may not hold
            text = f"{RESTART_NOTICE}\n\n{text}"
            raw["restarted"] = True
        return ToolOutcome(
            ToolStatus.FAILED if failed else ToolStatus.SUCCEEDED,
            ModelToolResult((TextBlock(text),), is_error=failed),
            raw_result=raw,
            result_images=tuple(images.values()),
        )


def model_may_call(listed: Mapping[str, Any]) -> bool:
    """Whether a ``tools/list`` entry is for the model (its ``_meta.ui.visibility``,
    MCP Apps). ValueError when ``_meta.ui`` cannot be read: then it is not
    known, so the caller leaves the Tool out."""
    meta = listed.get("_meta")
    ui = meta.get("ui") if isinstance(meta, dict) else None
    if ui is None:
        return True
    if not isinstance(ui, dict):
        raise ValueError("_meta.ui is not an object")
    visibility = ui.get("visibility", ["model", "app"])
    if not isinstance(visibility, list) or not all(v in _VISIBILITY for v in visibility):
        raise ValueError('_meta.ui.visibility is not a list of "model" and "app"')
    return "model" in visibility


def _image(item: Mapping[str, Any]) -> ResultImage | None:
    """An ``image`` content item's bytes (V3-P67 §4.2), the base64 length
    checked before it is decoded (review B3); None if it cannot be used."""
    data = item.get("data")
    if not isinstance(data, str) or len(data) > MAX_IMAGE_BASE64_CHARS:
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:
        return None
    mime = item.get("mimeType")
    return ResultImage(decoded, mime if isinstance(mime, str) else "") if decoded else None


def result_images(result: Mapping[str, Any]) -> dict[int, ResultImage]:
    """The usable images of a result by their place in its content, in
    order: the model sees them (as the Dispatcher keeps them) instead of a
    placeholder."""
    content = result.get("content")
    found = {index: _image(item) for index, item in enumerate(content if isinstance(content, list) else ())
             if isinstance(item, dict) and item.get("type") == "image"}
    return {index: image for index, image in found.items() if image is not None}


def render_result(result: Mapping[str, Any], shown: Collection[int] = ()) -> str:
    """The model's view of a ``tools/call`` result: its text content, a
    placeholder for content it cannot read here, else the structured
    content as JSON; cut to MAX_RESULT_CHARS. The items at ``shown`` are
    left out: they reach the model as images."""
    parts: list[str] = []
    content = result.get("content")
    for index, item in enumerate(content if isinstance(content, list) else ()):
        if not isinstance(item, dict) or index in shown:
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
    # V3-P60, read once at connect: each server's instructions (where they go)
    # and prompts, and the capabilities it declared.
    notes: dict[str, SourceNote] = field(default_factory=dict)
    prompts: dict[str, tuple[PromptInfo, ...]] = field(default_factory=dict)
    capabilities: dict[str, frozenset[str]] = field(default_factory=dict)
    by_source: dict[str, McpClient] = field(default_factory=dict)
    # V3-P60 §5: each server's list_resources and read_resource, when its resources are on.
    resource_tools: list[ResourceTool] = field(default_factory=list)

    async def get_prompt(self, source: str, name: str, arguments: Mapping[str, str], key: str) -> Mapping[str, Any]:
        """``prompts/get`` in that Conversation's session; McpRefused if the
        server offers no such prompt."""
        if not any(p.name == name for p in self.prompts.get(source, ())):
            raise McpRefused(f"{source} offers no prompt {name!r}")
        return await self.by_source[source].get_prompt(name, arguments, key=key)

    @property
    def defaults(self) -> dict[str, bool]:
        return {s.source: s.enabled_by_default for s in self.statuses}

    async def release(self, key: str) -> None:
        """V3-P65 §6.3: end what only this Conversation used (its own stdio
        processes); servers shared by every Conversation are untouched."""
        for client in self.clients:
            await client.release(key)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


PROMPTS_STARTUP_SECONDS = 10.0  # prompts/list, on its own: a broken prompt list never costs the Tools (B2)
MAX_NOTE_CHARS = 64 * 1024  # kept of a server's instructions; what the model gets is cut further (V3-P60 §3)


@dataclass(frozen=True)
class SourceNote:
    """A server's ``initialize`` instructions and where the operator puts them."""

    placement: str  # "system" | "data" (never "off": then there is no note)
    text: str


@dataclass(frozen=True)
class PromptArgument:
    name: str
    description: str
    required: bool


@dataclass(frozen=True)
class PromptInfo:
    """One prompt a server offers (``prompts/list``), as the user picks it."""

    name: str
    title: str
    description: str
    arguments: tuple[PromptArgument, ...]


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _prompt_name(value: object) -> TypeGuard[str]:
    """A prompt's or an argument's name as it is shown in the source line
    of the user's message: printable (no newline, tab or other control,
    no line separator) and at most 128 characters (PR #43 review B2: a
    newline would forge a second line)."""
    return isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable() and value.strip() == value


def _prompt_info(entry: object) -> PromptInfo | None:
    """One ``prompts/list`` entry, or None when it is not usable: whatever a
    server sends, this never raises (PR #40 review B1)."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    raw_arguments = entry.get("arguments", [])
    if not _prompt_name(name) or not isinstance(raw_arguments, list):
        return None
    arguments = []
    for raw in raw_arguments:
        if not isinstance(raw, dict) or not _prompt_name(raw.get("name")):
            return None  # a prompt whose arguments cannot be read cannot be filled in
        arguments.append(PromptArgument(raw["name"], _text(raw.get("description"), 500),
                                        raw.get("required") is True))
    return PromptInfo(name, _text(entry.get("title"), 200), _text(entry.get("description"), 1000), tuple(arguments))


@dataclass
class _Started:
    client: McpClient
    handlers: list[McpTool]
    status: SourceStatus
    note: SourceNote | None = None
    prompts: tuple[PromptInfo, ...] = ()
    capabilities: frozenset[str] = frozenset()
    resource_tools: list[ResourceTool] = field(default_factory=list)


# Bootstrap's launcher for a stdio server; raising says why it cannot run (the reason is shown).
LauncherFor = Callable[[str, McpStdioConfig], ProcessLauncher]


class _Unavailable:
    """The channel of a server left out at startup: nothing is sent."""

    async def start(self) -> Mapping[str, Any]:
        raise McpRefused("the server is not available")

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]:
        raise McpRefused("the server is not available")

    async def request_in(self, method: str, params: Mapping[str, Any], *, key: str | None = None,
                         session: object | None = None) -> tuple[object, Mapping[str, Any]]:
        raise McpRefused("the server is not available")

    def current_session(self, key: str | None) -> object | None:
        return None

    def take_restart(self, key: str | None) -> bool:
        return False

    async def release(self, key: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _clients(server: McpServerConfig, transport: httpx.AsyncBaseTransport | None,
             launcher_for: LauncherFor | None) -> tuple[McpClient, McpClient | None]:
    """The client for calls, and for a stdio server a listing client."""
    if server.stdio is None:
        assert server.url is not None
        client = http_client(server.url, server.token, timeout_seconds=server.timeout_seconds, transport=transport)
        return client, None
    if launcher_for is None:
        raise McpRefused("stdio servers run only in the server sandbox, which is not available")
    launcher = launcher_for(server.name, server.stdio)
    stdio = StdioServer(server.name, server.stdio, launcher, capabilities={},
                        timeout_seconds=server.timeout_seconds)
    return McpClient(StdioChannel(stdio)), McpClient(ListingChannel(stdio, server.timeout_seconds))


async def _start(server: McpServerConfig, check_schema: InputSchemaCheck, transport: httpx.AsyncBaseTransport | None,
                 launcher_for: LauncherFor | None) -> _Started:
    lister: McpClient | None = None
    try:
        client, lister = _clients(server, transport, launcher_for)
    except Exception as error:  # noqa: BLE001 — a server that cannot run is left out with the reason
        return _Started(McpClient(_Unavailable()), [], SourceStatus(server.name, server.enabled_by_default, False, (),
                                                                    (f"unavailable: {error}",)))
    problems: list[str] = []
    try:
        async with asyncio.timeout(STARTUP_SECONDS):
            init = await (lister or client).initialize()
            declared = init.get("capabilities")
            capabilities = frozenset(k for k, v in declared.items() if isinstance(v, dict)) \
                if isinstance(declared, dict) else frozenset()
            # Each list only when the server declares that capability (V3-P60 B4).
            listing: list[Mapping[str, Any]] = []
            if server.tools:
                if "tools" in capabilities:
                    listing = await (lister or client).list_tools()
                else:
                    problems.append("the server declares no tools: the listed Tools are not offered")
    except Exception as error:  # noqa: BLE001 — whatever the server did, only this server is left out
        if lister is not None:
            await lister.aclose()
        if isinstance(error, TimeoutError):
            reason = "no answer in time"
        elif isinstance(error, McpError):
            reason = str(error)
        else:
            reason = f"its answer could not be used ({type(error).__name__})"
        return _Started(client, [], SourceStatus(server.name, server.enabled_by_default, False, (),
                                                 (f"unavailable: {reason}",)))
    text = init.get("instructions")
    note = (SourceNote(server.instructions, text[:MAX_NOTE_CHARS])
            if server.instructions != "off" and isinstance(text, str) and text.strip() else None)
    listed: dict[str, Mapping[str, Any]] = {}
    duplicated: set[str] = set()
    for entry in listing:
        name = entry.get("name")
        if isinstance(name, str):
            if name in listed:
                duplicated.add(name)
            listed[name] = entry
    handlers: list[McpTool] = []
    for tool in server.tools:
        if tool.name in duplicated:
            problems.append(f"{tool.name}: the server offers it more than once")
            continue
        if tool.name not in listed:
            problems.append(f"{tool.name}: the server does not offer it")
            continue
        try:
            for_model = model_may_call(listed[tool.name])
        except ValueError as error:
            problems.append(f"{tool.name}: left out, {error}")
            continue
        if not for_model:
            problems.append(f"{tool.name}: app-only Tool: Tool views are not shown")
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
    # Prompts are optional: their failure, however it comes, leaves them out
    # and nothing else (PR #40 review B1, B2). They are listed last: a timed-out
    # prompts/list leaves a stdio listing session checking, so nothing may
    # follow it on that session (B3).
    prompts: tuple[PromptInfo, ...] = ()
    if server.prompts and "prompts" in capabilities:
        try:
            async with asyncio.timeout(PROMPTS_STARTUP_SECONDS):
                prompt_entries = await (lister or client).list_prompts()
            infos = [_prompt_info(e) for e in prompt_entries]
            prompts = tuple(info for info in infos if info is not None)
            if len(prompts) < len(infos):
                problems.append(f"{len(infos) - len(prompts)} prompt(s) left out: their description cannot be used")
        except TimeoutError:
            problems.append("prompts left out: no answer in time")
        except McpError as error:
            problems.append(f"prompts left out: {error}")
        except Exception as error:  # noqa: BLE001 — whatever the server sent, only its prompts are left out
            problems.append(f"prompts left out: the answer could not be used ({type(error).__name__})")
    if lister is not None:
        await lister.aclose()
    readers: list[ResourceTool] = []
    if server.resources is not None and "resources" in capabilities:
        # Only a server Tool that is registered takes the name (PR #42 re-review B2).
        readers, clashes = resource_tools(client, server.name, server.resources,
                                          {h.definition.tool_id for h in handlers})
        problems.extend(clashes)
    usable = bool(handlers or note or prompts or readers)  # a server may offer no Tools (V3-P60 B4)
    if not usable:
        problems.append("nothing to use: no listed Tool, instructions, prompts or resources")
    status = SourceStatus(server.name, server.enabled_by_default, usable,
                          tuple(h.definition.tool_id for h in handlers) + tuple(r.definition.tool_id for r in readers),
                          tuple(problems))
    return _Started(client, handlers, status, note if usable else None, prompts if usable else (), capabilities,
                    readers)


async def connect(config: McpConfig, *, check_schema: InputSchemaCheck,
                  transport: httpx.AsyncBaseTransport | None = None,
                  launcher_for: LauncherFor | None = None) -> McpConnection:
    """Every server at once; never raises for an unreachable server.
    ``check_schema`` is the Registry's own check, so a Tool it would refuse
    is left out here, with the reason, instead of failing the startup."""
    started: Sequence[_Started] = await asyncio.gather(
        *(_start(server, check_schema, transport, launcher_for) for server in config.servers))
    names = [server.name for server in config.servers]
    return McpConnection(
        handlers=[h for s in started for h in s.handlers],
        statuses=tuple(s.status for s in started),
        clients=[s.client for s in started],
        notes={name: s.note for name, s in zip(names, started, strict=True) if s.note is not None},
        prompts={name: s.prompts for name, s in zip(names, started, strict=True) if s.prompts},
        capabilities={name: s.capabilities for name, s in zip(names, started, strict=True)},
        by_source={name: s.client for name, s in zip(names, started, strict=True)},
        resource_tools=[t for s in started for t in s.resource_tools],
    )
