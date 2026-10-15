"""``<data_root>/mcp.json``: the MCP servers the operator connects (V3-P53).

::

    {"servers": {"calliope": {
        "url": "http://127.0.0.1:8790/mcp",
        "token_env": "CALLIOPE_MCP_TOKEN",      optional: Bearer token from this variable
        "default": "on",                         optional: "on" (default) or "off" in new Conversations
        "timeout_seconds": 60,                   optional: one tools/call, 1..600
        "tools": {"position": {"effect": "read"}},
        "ui": {                                  optional: show this server's Tool views (MCP Apps, V3-P58)
            "connect_domains": ["https://api.example.com", "wss://live.example.com"],
            "resource_domains": ["https://cdn.example.com"]
        }
    }}}

Only the listed Tools are registered, whatever else the server offers, and
each one's ``effect`` (``read`` / ``scoped_write`` / ``unknown``) is the
operator's judgement — the server's own hints are not trusted. The file must
be private (a regular file of this user, mode 600, one name) because it
names where credentials go; the token itself stays in the environment.
Anything unexpected is refused, never guessed: unknown keys, a URL that is
not ``https://`` (or ``http://`` to a loopback address), a token variable
that is not set.

``ui`` turns on the server's views and is the upper bound of what they may
reach: a view's CSP allows only the origins its server declares that are
also listed here, exactly (V3-P58 §3.3). Without ``ui`` the server is not told
that HIMARI shows views. An origin is ``https://host[:port]`` or
``https://*.host[:port]`` (``connect_domains`` also ``wss://``; on a loopback
host also ``http://`` / ``ws://``); nothing else may appear in it, since it
goes into a CSP header.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import urlsplit

from himari.common import ToolId, tool_id_problem
from himari.tool.api import EffectProfile

CONFIG_NAME = "mcp.json"
MAX_CONFIG_BYTES = 256 * 1024
MAX_SERVERS = 16
MAX_TOOLS_PER_SERVER = 128
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_SOURCE_CHARS = 24
HASH_CHARS = 8
_EFFECTS = {"read": EffectProfile.READ, "scoped_write": EffectProfile.SCOPED_WRITE, "unknown": EffectProfile.UNKNOWN}
_SOURCE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LOOPBACK_NAMES = frozenset({"localhost"})
MAX_UI_DOMAINS = 32
# An origin as it appears in a CSP source list: scheme, an optional "*."
# label, a host of DNS labels (or an IPv4 address) and an optional port.
_ORIGIN = re.compile(r"(https|wss|http|ws)://(\*\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)*)(?::([0-9]{1,5}))?")


class McpConfigError(ValueError):
    pass


@dataclass(frozen=True)
class McpToolConfig:
    name: str  # the server's name for it
    tool_id: ToolId  # HIMARI's id: "<server>/<mapped name>"
    effect: EffectProfile


@dataclass(frozen=True)
class McpUiPolicy:
    """What the operator allows this server's views (V3-P58)."""

    connect_domains: frozenset[str] = frozenset()
    resource_domains: frozenset[str] = frozenset()


@dataclass(frozen=True)
class McpServerConfig:
    name: str  # the External source: the namespace of its Tools' ids
    url: str
    token: str | None = field(repr=False)  # never in reprs, logs or tracebacks
    enabled_by_default: bool
    timeout_seconds: float
    tools: tuple[McpToolConfig, ...]
    ui: McpUiPolicy | None = None  # None: the server's views are not shown


@dataclass(frozen=True)
class McpConfig:
    servers: tuple[McpServerConfig, ...] = ()


def map_tool_names(source: str, names: list[str]) -> dict[str, ToolId]:
    """Each server name → a Tool id ``<source>/<name>`` that every Provider
    accepts (V3-P52), deterministically. Characters the grammar does not
    allow become ``-``; a name that would be too long, or that maps onto the
    same id as another name, is cut and given a short hash of the original."""
    room = 64 - len(source) - 2  # "/" counts as two
    plain: dict[str, str] = {}
    for name in names:
        mapped = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]", "-", name)).strip("_") or "-"
        plain[name] = mapped
    counts: dict[str, int] = {}
    for mapped in plain.values():
        counts[mapped] = counts.get(mapped, 0) + 1
    out: dict[str, ToolId] = {}
    for name, mapped in plain.items():
        if len(mapped) > room or counts[mapped] > 1:
            digest = hashlib.sha256(name.encode()).hexdigest()[:HASH_CHARS]
            mapped = mapped[: room - HASH_CHARS - 1].rstrip("_") + "-" + digest
        tool_id = f"{source}/{mapped}"
        if tool_id_problem(tool_id) is not None or tool_id in out.values():
            raise McpConfigError(f"servers.{source}.tools: cannot give {name!r} a unique Tool id")
        out[name] = ToolId(tool_id)
    return out


def check_url(url: str) -> str:
    """``https://``, or ``http://`` to a loopback address; no credentials or
    fragment in it (a token goes in ``token_env``)."""
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise McpConfigError(f"not a valid URL: {url!r}") from None
    host = parts.hostname or ""
    try:
        loopback = host in _LOOPBACK_NAMES or ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password or parts.fragment:
        raise McpConfigError("a server url is https://host/path (or http:// to a loopback address)")
    if parts.scheme == "http" and not loopback:
        raise McpConfigError("plain http:// is allowed only to a loopback address (use https://)")
    return url


def check_origin(origin: str, *, websocket: bool) -> str:
    """One CSP origin (see the module doc); returned as given, lowercase."""
    match = _ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
    if match is None or origin != origin.lower():
        raise McpConfigError(f"{origin!r} is not an origin like https://host[:port] or https://*.host")
    scheme, wildcard, host, port = match.groups()
    if scheme in ("ws", "wss") and not websocket:
        raise McpConfigError(f"{origin!r}: only connect_domains may name a WebSocket origin")
    if port is not None and not 1 <= int(port) <= 65535:
        raise McpConfigError(f"{origin!r}: not a valid port")
    if scheme in ("http", "ws"):
        try:
            loopback = host in _LOOPBACK_NAMES or ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or wildcard:
            raise McpConfigError(f"{origin!r}: http:// and ws:// are allowed only to a loopback address")
    return origin


def _ui_policy(raw: object, where: str) -> McpUiPolicy:
    entry = _keys(raw, where, set(), {"connect_domains", "resource_domains"})
    lists: dict[str, frozenset[str]] = {}
    for key in ("connect_domains", "resource_domains"):
        value = entry.get(key, [])
        if not isinstance(value, list) or len(value) > MAX_UI_DOMAINS:
            raise McpConfigError(f"{where}.{key} must be a list of at most {MAX_UI_DOMAINS} origins")
        try:
            lists[key] = frozenset(check_origin(v, websocket=key == "connect_domains") for v in value)
        except McpConfigError as error:
            raise McpConfigError(f"{where}.{key}: {error}") from None
    return McpUiPolicy(lists["connect_domains"], lists["resource_domains"])


def _read_private(path: str) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise McpConfigError(f"{path} is a symbolic link") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise McpConfigError(f"{path} must be a private regular file (mode 600, one name)")
        data = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_CONFIG_BYTES:
        raise McpConfigError(f"{path} is larger than {MAX_CONFIG_BYTES} bytes")
    return data


def _keys(value: object, where: str, required: set[str], optional: set[str] = set()) -> dict[str, object]:
    if not isinstance(value, dict):
        raise McpConfigError(f"{where} must be an object")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing:
        raise McpConfigError(f"{where}: missing {sorted(missing)}")
    if unknown:
        raise McpConfigError(f"{where}: unknown keys {sorted(unknown)}")
    return value


def parse_config(text: bytes | str, environ: Mapping[str, str]) -> McpConfig:
    """Every mistake is an McpConfigError, whatever its shape."""
    try:
        return _parse_config(text, environ)
    except McpConfigError:
        raise
    except (TypeError, ValueError, AttributeError) as error:  # a shape no explicit check foresaw
        raise McpConfigError(f"the file has an unexpected shape ({type(error).__name__})") from None


def _parse_config(text: bytes | str, environ: Mapping[str, str]) -> McpConfig:
    try:
        document = json.loads(text)
    except ValueError as error:
        raise McpConfigError(f"not valid JSON: {error}") from None
    servers = _keys(document, "the file", {"servers"})["servers"]
    if not isinstance(servers, dict) or len(servers) > MAX_SERVERS:
        raise McpConfigError(f"servers must be an object of at most {MAX_SERVERS} servers")
    out: list[McpServerConfig] = []
    for name, raw in sorted(servers.items()):
        where = f"servers.{name}"
        if not _SOURCE.fullmatch(name) or len(name) > MAX_SOURCE_CHARS:
            raise McpConfigError(f"{where}: a server name is lowercase letters, digits and single '-' "
                                 f"(at most {MAX_SOURCE_CHARS})")
        entry = _keys(raw, where, {"url", "tools"}, {"token_env", "default", "timeout_seconds", "ui"})
        url = entry["url"]
        if not isinstance(url, str):
            raise McpConfigError(f"{where}.url must be a string")
        check_url(url)
        token: str | None = None
        if "token_env" in entry:
            variable = entry["token_env"]
            if not isinstance(variable, str) or not variable:
                raise McpConfigError(f"{where}.token_env must name an environment variable")
            token = environ.get(variable, "").strip()
            if not token:
                raise McpConfigError(f"{where}: the environment variable {variable} is not set")
        default = entry.get("default", "on")
        if not isinstance(default, str) or default not in ("on", "off"):
            raise McpConfigError(f"{where}.default must be \"on\" or \"off\"")
        timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise McpConfigError(f"{where}.timeout_seconds must be 1..{int(MAX_TIMEOUT_SECONDS)}")
        tools = entry["tools"]
        if not isinstance(tools, dict) or not tools or len(tools) > MAX_TOOLS_PER_SERVER:
            raise McpConfigError(f"{where}.tools must list 1..{MAX_TOOLS_PER_SERVER} Tools")
        effects: dict[str, EffectProfile] = {}
        for tool_name, tool_raw in tools.items():
            if not tool_name or len(tool_name) > 128:
                raise McpConfigError(f"{where}.tools: a Tool name is 1..128 characters")
            effect = _keys(tool_raw, f"{where}.tools.{tool_name}", {"effect"})["effect"]
            if not isinstance(effect, str) or effect not in _EFFECTS:
                raise McpConfigError(f"{where}.tools.{tool_name}.effect must be one of {sorted(_EFFECTS)}")
            effects[tool_name] = _EFFECTS[effect]
        ids = map_tool_names(name, sorted(effects))
        out.append(McpServerConfig(
            name=name, url=url, token=token, enabled_by_default=default == "on", timeout_seconds=float(timeout),
            tools=tuple(McpToolConfig(n, ids[n], effects[n]) for n in sorted(effects)),
            ui=_ui_policy(entry["ui"], f"{where}.ui") if "ui" in entry else None,
        ))
    return McpConfig(tuple(out))


def load_config(path: str, environ: Mapping[str, str]) -> McpConfig:
    """No file: no servers."""
    data = _read_private(path)
    if data is None:
        return McpConfig()
    try:
        return parse_config(data, environ)
    except McpConfigError as error:
        raise McpConfigError(f"{path}: {error}") from None
