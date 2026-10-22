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

stdio servers (V3-P59 §5) run only through a launcher Bootstrap gives for
each one (the server sandbox); without it the server is left out with the
reason. Their Tools and views are read by a listing session of their own
at connect; calls then go to the server's processes (``stdio``), keyed by
the calling Conversation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

import httpx

from himari.common import JsonValue, ModelToolResult, TextBlock
from himari.mcp.client import UI_MIME_TYPE, McpClient, McpError, McpRefused, http_client
from himari.mcp.client import capabilities
from himari.mcp.config import McpConfig, McpServerConfig, McpStdioConfig, McpToolConfig, McpUiPolicy
from himari.mcp.resources import ResourceTool, resource_tools
from himari.mcp.stdio import RESTART_NOTICE, ListingChannel, ProcessLauncher, StdioChannel, StdioServer
from himari.tool.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_IMAGE_BASE64_CHARS,
    MAX_UI_HTML_BYTES,
    AppView,
    ResultImage,
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
        view: AppView | None = None
        if self.ui is not None:
            payload = {k: result[k] for k in ("content", "structuredContent", "_meta", "isError") if k in result}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
                raw["ui_problem"] = f"the result is larger than {MAX_APP_PAYLOAD_BYTES} bytes"
            else:
                view = AppView(self.ui, payload)
        images = result_images(result)
        text = render_result(result, shown_images=len(images))
        if self._client.take_restart(key):  # V3-P59 §5.3: the server's earlier identifiers may not hold
            text = f"{RESTART_NOTICE}\n\n{text}"
            raw["restarted"] = True
        return ToolOutcome(
            ToolStatus.FAILED if failed else ToolStatus.SUCCEEDED,
            ModelToolResult((TextBlock(text),), is_error=failed),
            raw_result=raw,
            app_view=view,
            result_images=images,
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


def result_images(result: Mapping[str, Any]) -> tuple[ResultImage, ...]:
    """The usable images of a result, in order: the model sees them (as the
    Dispatcher keeps them) instead of a placeholder."""
    content = result.get("content")
    found = [_image(item) for item in (content if isinstance(content, list) else ())
             if isinstance(item, dict) and item.get("type") == "image"]
    return tuple(image for image in found if image is not None)


def render_result(result: Mapping[str, Any], shown_images: int = 0) -> str:
    """The model's view of a ``tools/call`` result: its text content, a
    placeholder for content it cannot read here, else the structured
    content as JSON; cut to MAX_RESULT_CHARS. The first ``shown_images``
    usable images are left out: they reach the model as images."""
    parts: list[str] = []
    content = result.get("content")
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "image" and shown_images and _image(item) is not None:
            shown_images -= 1
            continue
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
        client = http_client(server.url, server.token, timeout_seconds=server.timeout_seconds, transport=transport,
                             ui=server.ui is not None)
        return client, None
    if launcher_for is None:
        raise McpRefused("stdio servers run only in the server sandbox, which is not available")
    launcher = launcher_for(server.name, server.stdio)
    stdio = StdioServer(server.name, server.stdio, launcher, capabilities=capabilities(server.ui is not None),
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
        problems.extend(await _read_views(lister or client, server.name, wanted))
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
        ui_policies={server.name: server.ui for server in config.servers},
        notes={name: s.note for name, s in zip(names, started, strict=True) if s.note is not None},
        prompts={name: s.prompts for name, s in zip(names, started, strict=True) if s.prompts},
        capabilities={name: s.capabilities for name, s in zip(names, started, strict=True)},
        by_source={name: s.client for name, s in zip(names, started, strict=True)},
        resource_tools=[t for s in started for t in s.resource_tools],
    )
