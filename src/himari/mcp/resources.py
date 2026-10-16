"""MCP resources as two Tools per server (V3-P60 §5).

``<server>/list_resources`` lists what the server offers (``resources/list``
and ``resources/templates/list``); ``<server>/read_resource`` reads one URI
(``resources/read``) in pages of ``PAGE_CHARS``. Being ordinary External
Tools, they are offered only where their server is enabled, and the
Dispatcher checks that again; their effect is the operator's
(``"resources": {"effect": …}``, ``unknown`` by default).

**Which URIs.** By default only a URI the server lists, or one of its URI
templates (RFC 6570) describes; ``"uris": "any"`` lets the server decide.
Refusals win: a URI the list marks for the user only (``annotations.audience``
without ``"assistant"``), a URI any user-only template could describe, and
any view (``ui://``, ``text/html;profile=mcp-app``) is never read, whatever
else matches. A user-only template is matched loosely (its text up to the
first expression, then anything), an allowing one strictly.

**Per session (§5 B6, C6).** The list a URI is judged by is the one of the
session the read goes to: the lists are cached per session
(``CACHE_SECONDS``; an HTTP session, or a stdio process), the read is sent
only to the session that was judged (``request_in`` with that session), and
when that session is gone or replaced before the read is processed, the
URI is judged again on the new session's list, once.

**Per entry (§5 B5, C7).** A read answer's ``contents[]`` entries are each
checked again (its URI as above unless it is the one asked for, not a view,
not for the user only); one that fails is replaced by ``OMITTED``, and its
URI and the reason are kept only in the execution record (``raw_result``),
never shown to the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from himari.common import JsonValue, ModelToolResult, TextBlock, ToolId, tool_id_problem
from himari.mcp.client import UI_MIME_TYPE, McpClient, McpRefused, McpSessionChanged, McpUncertain
from himari.mcp.config import McpResourcePolicy
from himari.mcp.stdio import RESTART_NOTICE
from himari.tool.api import (
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

LIST_NAME = "list_resources"
READ_NAME = "read_resource"
CACHE_SECONDS = 60.0
MAX_CACHED_SESSIONS = 64
MAX_LIST_PAGES = 20
MAX_LIST_ENTRIES = 500  # resources and templates together
MAX_LIST_BYTES = 2 * 1024 * 1024
LIST_CHARS = 32_000  # one list_resources result
PAGE_CHARS = 32_000  # one read_resource page
MAX_URI_CHARS = 2048
VERSION_CHARS = 12
OMITTED = "[omitted: restricted resource]"
RESOURCE_NOT_FOUND = -32002

_EXPRESSION = re.compile(r"\{([^{}]*)\}")
_VARIABLE = re.compile(r"[A-Za-z0-9_%.]+(\*|:[1-9][0-9]{0,3})?")
# What one expression may expand to, by operator (RFC 6570 §3.2): a simple
# value cannot contain "/", "?" or "#" (they are percent-encoded).
_OPERATORS = {
    "": r"[^/?#]*",
    "+": r"[^#]*",
    "#": r"(?:#.*)?",
    ".": r"(?:\.[^/?#]*)*",
    "/": r"(?:/[^/?#]*)*",
    ";": r"(?:;[^/?#]*)*",
    "?": r"(?:\?[^#]*)?",
    "&": r"(?:&[^#]*)?",
}


def _clock() -> float:
    return asyncio.get_running_loop().time()


def strict_pattern(template: str) -> re.Pattern[str] | None:
    """The URIs ``template`` describes, or None when it cannot be read."""
    parts: list[str] = []
    position = 0
    for match in _EXPRESSION.finditer(template):
        literal = template[position:match.start()]
        if "{" in literal or "}" in literal:
            return None
        parts.append(re.escape(literal))
        body = match.group(1)
        operator = body[:1] if body[:1] in "+#./;?&" else ""
        names = body[len(operator):]
        if not names or not all(_VARIABLE.fullmatch(v) for v in names.split(",")):
            return None
        parts.append(_OPERATORS[operator])
        position = match.end()
    tail = template[position:]
    if "{" in tail or "}" in tail:
        return None
    parts.append(re.escape(tail))
    return re.compile("".join(parts), re.DOTALL)


def loose_pattern(template: str) -> re.Pattern[str]:
    """Every URI that could come from ``template``, generously: its text up
    to the first expression, then anything. Used where matching refuses."""
    brace = template.find("{")
    if brace < 0:
        return re.compile(re.escape(template), re.DOTALL)
    return re.compile(re.escape(template[:brace]) + ".*", re.DOTALL)


def is_view(uri: object, mime_type: object) -> bool:
    """An MCP Apps view: shown to the user, never model context (V3-P58)."""
    if isinstance(uri, str) and uri[:5].lower() == "ui://":
        return True
    return isinstance(mime_type, str) and "".join(mime_type.split()).lower() == UI_MIME_TYPE


def for_model(item: Mapping[str, Any]) -> bool:
    """Whether the server's annotations let the model see it: no audience
    means no restriction (MCP); one that is not a readable list, or leaves
    out ``"assistant"``, is for the user only."""
    annotations = item.get("annotations")
    if annotations is None:
        return True
    if not isinstance(annotations, dict):
        return False
    audience = annotations.get("audience")
    if audience is None:
        return True
    return isinstance(audience, list) and "assistant" in audience


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class Listed:
    uri: str
    name: str
    title: str
    description: str
    mime_type: str
    size: int | None
    shown: bool  # for the model, and not a view

    def entry(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {"uri": self.uri, "name": self.name}
        for key, value in (("title", self.title), ("description", self.description), ("mimeType", self.mime_type)):
            if value:
                out[key] = value
        if self.size is not None:
            out["size"] = self.size
        return out


@dataclass(frozen=True)
class Template:
    template: str
    name: str
    description: str
    mime_type: str
    shown: bool
    strict: re.Pattern[str] | None = field(compare=False)
    loose: re.Pattern[str] = field(compare=False)

    def entry(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {"uriTemplate": self.template, "name": self.name}
        for key, value in (("description", self.description), ("mimeType", self.mime_type)):
            if value:
                out[key] = value
        return out


@dataclass
class Index:
    """One session's lists, as read at ``at``."""

    resources: dict[str, Listed]
    templates: list[Template]
    complete: bool  # False: the server lists more than HIMARI reads
    at: float

    def refusal(self, uri: str, any_uri: bool) -> str | None:
        """Why the model may not read ``uri``, or None. Refusals win."""
        if len(uri) > MAX_URI_CHARS:
            return f"the URI is longer than {MAX_URI_CHARS} characters"
        if is_view(uri, None):
            return "it is a view, shown to the user rather than read"
        listed = self.resources.get(uri)
        if listed is not None and not listed.shown:
            return "the server marks it for the user, or as a view"
        if any(not t.shown and t.loose.fullmatch(uri) for t in self.templates):
            return "the server marks resources like it for the user, or as views"
        if listed is not None:
            return None
        if not self.complete:  # a refusing template or entry may be in the part not read
            return "the server lists more than HIMARI reads, so only URIs in the part read can be read"
        if any_uri:
            return None
        if any(t.shown and t.strict is not None and t.strict.fullmatch(uri) for t in self.templates):
            return None
        return "the server neither lists it nor describes it with a URI template"


def _listed(entry: object) -> Listed | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("uri"), str) or not entry["uri"]:
        return None
    uri = entry["uri"]
    if len(uri) > MAX_URI_CHARS:
        return None
    size = entry.get("size")
    mime = entry.get("mimeType")
    return Listed(uri, _text(entry.get("name"), 200), _text(entry.get("title"), 200),
                  _text(entry.get("description"), 1000), _text(mime, 200),
                  size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                  for_model(entry) and not is_view(uri, mime))


def _template(entry: object) -> Template | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("uriTemplate"), str) or not entry["uriTemplate"]:
        return None
    template = entry["uriTemplate"]
    if len(template) > MAX_URI_CHARS:
        return None
    mime = entry.get("mimeType")
    return Template(template, _text(entry.get("name"), 200), _text(entry.get("description"), 1000),
                    _text(mime, 200), for_model(entry) and not is_view(template, mime),
                    strict_pattern(template), loose_pattern(template))


class _Unusable(Exception):
    """The server answered, but not in a form HIMARI can use."""


@dataclass(frozen=True)
class _Read:
    text: str
    entries: list[dict[str, JsonValue]]  # for the record: each entry's uri, type and size, or why it was left out


def compose(contents: object, requested: str, index: Index, any_uri: bool) -> _Read:
    """The entries the model may see, in order, each under a header; every
    other entry becomes ``OMITTED`` (its URI and reason only in the record)."""
    if not isinstance(contents, list):
        raise _Unusable("resources/read answered without a list of contents")
    parts: list[str] = []
    record: list[dict[str, JsonValue]] = []
    for entry in contents:
        uri = entry.get("uri", requested) if isinstance(entry, dict) else None
        reason = _entry_refusal(entry, uri, requested, index, any_uri)
        if reason is not None:
            parts.append(OMITTED)
            record.append({"uri": uri[:MAX_URI_CHARS] if isinstance(uri, str) else None, "omitted": reason})
            continue
        assert isinstance(entry, dict) and isinstance(uri, str)
        mime = _text(entry.get("mimeType"), 200) or "unknown type"
        head = f"[resource {uri} · {mime}]"
        text = entry.get("text")
        if isinstance(text, str):
            parts.append(f"{head}\n{text}")
            record.append({"uri": uri, "mimeType": mime, "chars": len(text)})
        else:
            blob = entry["blob"]
            size = len(blob) * 3 // 4 - blob[-2:].count("=")
            parts.append(f"{head}\n[binary, {size} bytes]")
            record.append({"uri": uri, "mimeType": mime, "bytes": size})
    return _Read("\n\n".join(parts), record)


def _entry_refusal(entry: object, uri: object, requested: str, index: Index, any_uri: bool) -> str | None:
    if not isinstance(entry, dict):
        return "the entry is not an object"
    if not isinstance(uri, str) or not uri:
        return "its uri is not a string"
    if uri != requested:  # another resource came along: judged as if it had been asked for
        reason = index.refusal(uri, any_uri)
        if reason is not None:
            return reason
    if is_view(uri, entry.get("mimeType")):
        return "it is a view"
    if not for_model(entry):
        return "the server marks it for the user"
    text, blob = entry.get("text"), entry.get("blob")
    if not (isinstance(text, str) and blob is None) and not (isinstance(blob, str) and text is None):
        return "it has neither text nor blob, or both"
    return None


def page(text: str, offset: int) -> tuple[str, dict[str, JsonValue]]:
    """``PAGE_CHARS`` of ``text`` from ``offset``, with where it is."""
    if offset > len(text):
        raise ValueError(f"offset {offset} is past the end ({len(text)} characters)")
    chunk = text[offset:offset + PAGE_CHARS]
    end = offset + len(chunk)
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:VERSION_CHARS]
    where: dict[str, JsonValue] = {"offset": offset, "end": end, "total": len(text), "version": version,
                                   "next_offset": end if end < len(text) else None}
    return chunk, where


class ResourceReader:
    """One server's resources, for its two Tools."""

    def __init__(self, client: McpClient, source: str, policy: McpResourcePolicy) -> None:
        self._client = client
        self.source = source
        self._policy = policy
        self._cache: dict[object, Index] = {}

    @property
    def _any(self) -> bool:
        return self._policy.uris == "any"

    # --- the lists, per session ---------------------------------------------------------

    def _cached(self, session: object | None) -> Index | None:
        index = self._cache.get(session) if session is not None else None
        if index is not None and _clock() - index.at < CACHE_SECONDS:
            return index
        return None

    def _keep(self, session: object, index: Index) -> None:
        now = _clock()
        for old in [s for s, i in self._cache.items() if now - i.at >= CACHE_SECONDS]:
            del self._cache[old]
        self._cache[session] = index
        while len(self._cache) > MAX_CACHED_SESSIONS:
            del self._cache[min(self._cache, key=lambda s: self._cache[s].at)]

    async def index(self, key: str) -> tuple[object, Index]:
        """The lists of the session ``key``'s requests go to now: cached, or
        read there (all pages in that one session)."""
        session = self._client.current_session(key)
        cached = self._cached(session)
        if cached is not None:
            return session, cached
        if session is not None:
            self._cache.pop(session, None)
        resources: dict[str, Listed] = {}
        templates: list[Template] = []
        complete = True
        size = 0
        # Templates first: the ones that refuse must not be lost when the list is cut.
        for method, field_name in (("resources/templates/list", "resourceTemplates"), ("resources/list", "resources")):
            cursor: str | None = None
            for _ in range(MAX_LIST_PAGES):
                try:
                    session, result = await self._client.request_in(
                        method, {} if cursor is None else {"cursor": cursor}, key=key, session=session)
                except McpSessionChanged:
                    raise
                except McpRefused:
                    if method == "resources/templates/list":  # a server without templates
                        break
                    raise
                entries = result.get(field_name)
                if not isinstance(entries, list):
                    raise _Unusable(f"{method} answered without a list of {field_name}")
                size += len(json.dumps(entries))
                if size > MAX_LIST_BYTES:
                    complete = False
                    break
                for raw in entries:
                    if len(resources) + len(templates) >= MAX_LIST_ENTRIES:
                        complete = False
                        break
                    if method == "resources/list":
                        listed = _listed(raw)
                        if listed is not None:  # listed twice: the more restricted wins
                            earlier = resources.get(listed.uri)
                            resources[listed.uri] = listed if earlier is None or earlier.shown else earlier
                    elif (template := _template(raw)) is not None:
                        templates.append(template)
                if not complete:
                    break
                next_cursor = result.get("nextCursor")
                if next_cursor is None or next_cursor == "":
                    break
                if not isinstance(next_cursor, str):
                    raise _Unusable(f"{method} answered with an invalid nextCursor")
                cursor = next_cursor
            else:
                complete = False
            if not complete:
                break
        assert session is not None
        index = Index(resources, templates, complete, _clock())
        self._keep(session, index)
        return session, index

    # --- the two Tools ---------------------------------------------------------------------

    async def list_resources(self, cursor: str | None, key: str) -> tuple[str, dict[str, JsonValue]]:
        for attempt in (1, 2):
            try:
                _, index = await self.index(key)
                break
            except McpSessionChanged:
                if attempt == 2:
                    raise
        try:
            start = 0 if cursor is None else int(cursor)
        except ValueError:
            start = -1
        shown: list[dict[str, JsonValue]] = [r.entry() for r in index.resources.values() if r.shown]
        shown_templates = [t.entry() for t in index.templates if t.shown]
        items = [("resources", e) for e in shown] + [("templates", e) for e in shown_templates]
        if not 0 <= start <= len(items):
            raise ValueError("the cursor is not one this tool gave")
        out: dict[str, list[JsonValue]] = {"resources": [], "templates": []}
        position = start
        budget = LIST_CHARS - 400  # room for the cursor and the note
        used = len(json.dumps(out, ensure_ascii=False))
        while position < len(items):
            kind, item = items[position]
            cost = len(json.dumps(item, ensure_ascii=False)) + 2
            if used + cost > budget and position > start:
                break
            out[kind].append(item)
            used += cost
            position += 1
        result: dict[str, JsonValue] = {**out}
        if position < len(items):
            result["next_cursor"] = str(position)
        if not index.complete:
            result["note"] = (f"the server lists more than HIMARI reads ({MAX_LIST_ENTRIES} entries or "
                              f"{MAX_LIST_PAGES} pages); resources past that are not shown")
        text = json.dumps(result, ensure_ascii=False)  # measured as above: compact
        return text, {"listed": len(index.resources), "templates": len(index.templates), "shown": len(items),
                      "from": start, "to": position, "complete": index.complete}

    async def read_resource(self, uri: str, offset: int, key: str) -> tuple[str | None, str, dict[str, JsonValue]]:
        """(refusal, text, record). The URI is judged by the list of the
        session the read goes to, and read only there; if that session is
        replaced first, judged again on the new one, once (C6)."""
        for attempt in (1, 2):
            try:
                session, index = await self.index(key)
                reason = index.refusal(uri, self._any)
                if reason is not None:
                    return reason, "", {"uri": uri, "refused": reason}
                _, result = await self._client.request_in("resources/read", {"uri": uri}, key=key, session=session)
                break
            except McpSessionChanged:
                if attempt == 2:
                    raise
        read = compose(result.get("contents"), uri, index, self._any)
        chunk, where = page(read.text, offset)
        record: dict[str, JsonValue] = {"uri": uri, **where, "entries": list(read.entries)}
        return None, chunk, record

    def take_restart(self, key: str) -> bool:
        return self._client.take_restart(key)


LIST_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"cursor": {"type": "string", "description": "next_cursor from the previous result"}},
    "additionalProperties": False,
}
READ_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "uri": {"type": "string", "maxLength": MAX_URI_CHARS, "description": "a listed URI, or one a template describes"},
        "offset": {"type": "integer", "minimum": 0, "description": "where to continue: next_offset of the previous page"},
    },
    "required": ["uri"],
    "additionalProperties": False,
}


class ResourceTool:
    """``<server>/list_resources`` or ``<server>/read_resource``."""

    def __init__(self, reader: ResourceReader, name: str, tool_id: ToolId, policy: McpResourcePolicy) -> None:
        self._reader = reader
        self._name = name
        source = reader.source
        if name == LIST_NAME:
            description = (f"List the resources the {source} server offers to read: their URIs, and URI templates "
                           f"(RFC 6570) describing more. Read one with {source}/{READ_NAME}. When the result has "
                           "next_cursor, call again with it for the rest.")
            schema = LIST_SCHEMA
        else:
            description = (f"Read a resource of the {source} server: a URI from {source}/{LIST_NAME}, or one a "
                           f"listed URI template describes. Gives up to {PAGE_CHARS} characters from offset "
                           "(default 0) and where they are: total, next_offset (none at the end) and version. "
                           "Continue with offset = next_offset. If version differs from the previous page's, the "
                           "resource changed between reads: read it again from offset 0.")
            schema = READ_SCHEMA
        self.definition = ToolDefinition(
            tool_id=tool_id,
            description=description,
            input_schema=schema,
            workspace_requirement=WorkspaceRequirement.NONE,
            effect_profile=policy.effect,
            external_source=source,
        )

    def _failed(self, text: str, raw: dict[str, JsonValue]) -> ToolOutcome:
        return ToolOutcome(ToolStatus.FAILED, ModelToolResult((TextBlock(text),), is_error=True),
                           raw_result={"server": self._reader.source, "tool": self._name, **raw})

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        """A definite answer; anything uncertain raises (V3-045/047), as for
        the server's own Tools."""
        key = str(ctx.scope.conversation_id)
        arguments = invocation.arguments
        try:
            if self._name == LIST_NAME:
                cursor = arguments.get("cursor")
                text, raw = await self._reader.list_resources(cursor if isinstance(cursor, str) else None, key)
            else:
                uri, offset = arguments.get("uri"), arguments.get("offset", 0)
                if not isinstance(uri, str) or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                    return self._failed("Give uri as a string and offset as a whole number of at least 0.", {})
                refusal, text, raw = await self._reader.read_resource(uri, offset, key)
                if refusal is not None:
                    return self._failed(f"This resource cannot be read: {refusal}.", raw)
                text = _with_page(text, raw)
        except McpSessionChanged as changed:
            return self._failed("The server's session changed twice during this request; try again.",
                                {"refused": str(changed)})
        except McpRefused as refused:
            if refused.code == RESOURCE_NOT_FOUND:
                return self._failed("The server has no such resource.", {"refused": str(refused)})
            return self._failed("The server did not run this request.", {"refused": str(refused)})
        except McpUncertain as uncertain:
            if uncertain.code == RESOURCE_NOT_FOUND:  # MCP: -32002, the resource was not found
                return self._failed("The server has no such resource.", {"error": str(uncertain)})
            raise
        except _Unusable as unusable:
            return self._failed(f"The server's answer could not be used: {unusable}.", {"unusable": str(unusable)})
        except ValueError as wrong:  # a cursor or offset this tool did not give
            return self._failed(f"{wrong}.", {})
        if self._reader.take_restart(key):
            text = f"{RESTART_NOTICE}\n\n{text}"
            raw = {**raw, "restarted": True}
        return ToolOutcome(ToolStatus.SUCCEEDED, ModelToolResult((TextBlock(text),)),
                           raw_result={"server": self._reader.source, "tool": self._name, **raw})


def _with_page(text: str, where: Mapping[str, Any]) -> str:
    end = "end of the resource" if where["next_offset"] is None else f"next_offset {where['next_offset']}"
    return (f"[{where['uri']} · characters {where['offset']}–{where['end']} of {where['total']} · "
            f"version {where['version']} · {end}]\n\n{text}")


def resource_tools(client: McpClient, source: str, policy: McpResourcePolicy,
                   taken: set[str]) -> tuple[list[ResourceTool], list[str]]:
    """The two Tools for a server whose resources are on, and the problems:
    an id the server's own Tools already use is left to them (§5)."""
    reader = ResourceReader(client, source, policy)
    tools: list[ResourceTool] = []
    problems: list[str] = []
    for name in (LIST_NAME, READ_NAME):
        tool_id = f"{source}/{name}"
        if tool_id in taken:
            problems.append(f"{name}: the server's own Tool of that name is offered instead")
        elif tool_id_problem(tool_id) is not None:
            problems.append(f"{name}: no valid Tool id for it ({tool_id_problem(tool_id)})")
        else:
            tools.append(ResourceTool(reader, name, ToolId(tool_id), policy))
    return tools, problems
