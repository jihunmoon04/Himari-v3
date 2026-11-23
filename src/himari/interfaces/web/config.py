"""Web server configuration (adapted from HIMARI-v2 ``interfaces/web/config.py``).

Plain environment reads; wildcards are refused in the allowed hosts and
origins. Nothing here imports the web framework, so a configuration
defect fails before any socket is bound.

The operator token and session cookie never cross a network in clear text
(V3-P46). The server itself speaks plain HTTP only, so it binds to a
loopback address only — a declared ``https://`` Origin proves nothing
about the connection (any client can send that header, and a browser on a
plain-HTTP page would already have sent the token). Remote use goes
through a TLS-terminating reverse proxy on the same machine: configure its
public name in ``HIMARI_WEB_ALLOWED_HOSTS`` and its ``https://`` origin in
``HIMARI_WEB_ALLOWED_ORIGINS``. A plain ``http://`` origin is accepted only
for a loopback host, and any https origin makes the cookie ``Secure``.

Models (V3-P64 §3): ``HIMARI_MODEL`` is the default; ``HIMARI_MODELS``
optionally lists every offered model as ``id[=effort]`` (comma-separated;
an id may hold ``:``, so the effort follows ``=``). The default must be
listed. Unset, the default model is the only one offered.

Trash (V3-P65): ``HIMARI_TRASH_DAYS`` (1..365, default 30) is how long a
deleted Conversation stays in the trash before it is deleted for good.

Files (V3-P66 §8.3): ``HIMARI_FILE_MAX_BYTES`` (one file, default 32 MiB),
``HIMARI_FILE_STORE_MAX_BYTES`` (the file store, default 1 GiB; past it the
least recently used files expire) and ``HIMARI_FILE_KEEP_DAYS`` (1..365,
default 30: a file unused this long expires). None of them can be turned off.

Documents (V3-P70 §6.2): ``HIMARI_DOCUMENTS_DIR`` names the documents
toolkit's folder (installed by root with scripts/install_documents.py). Unset,
read_document is not offered.

Memory (V3-P71 §9): ``HIMARI_MEMORY=0`` turns the memory Tools and notes off
(the notes stay and can be seen); on by default. Automatic memory (V3-P85
§12): ``HIMARI_MEMORY_MODEL`` names the model of memory passes (one the
provider declares checked for them; unset: none run), ``HIMARI_MEMORY_AUTO=0``
turns them off, ``HIMARI_MEMORY_IDLE_MINUTES`` (1..240, default 10) is how long
a conversation stays quiet first, ``HIMARI_MEMORY_DAILY_TOKENS`` (default
1,000,000) each principal's daily estimate-based budget.

Conversation search (V3-P72 §9): ``HIMARI_CONVERSATION_SEARCH=0`` turns
conversation_search and conversation_read off; on by default.

Streaming (V3-P73 §9): ``HIMARI_STREAMING=0`` asks no model for a stream;
on by default (models that declare it stream their answers).

Companion (V3-P83 §3): ``HIMARI_COMPANION=0`` refuses pairing and every
device token (403 ``companion_off``; the tokens stay); on by default.

Time (V3-P83 §6.4): ``HIMARI_TIME_ZONE`` (an IANA name, default
``Asia/Seoul``) is the zone the model is told the time in.

Machine Workspace (V3-P74 §4.4): ``HIMARI_MACHINE_SOCKET`` names the
runner's socket (``/run/himari-runner/runner.sock``); then new Conversations
of the principals in ``HIMARI_MACHINE_PRINCIPALS`` (comma-separated, default
``operator``) work on this server as the user. Unset (or another principal):
Conversations only talk, with no files, commands or uploads (V3-P101). HIMARI's own Tools write a
Conversation's own folder only: attached Workspaces are read, and changed
by the hub's coding agents (V3-P94, V3-P100).

"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from himari.bootstrap.proactive import ProactiveSettings, parse_quiet

HOST_VAR = "HIMARI_WEB_HOST"
PORT_VAR = "HIMARI_WEB_PORT"
ALLOWED_HOSTS_VAR = "HIMARI_WEB_ALLOWED_HOSTS"
ALLOWED_ORIGINS_VAR = "HIMARI_WEB_ALLOWED_ORIGINS"
SECURE_COOKIE_VAR = "HIMARI_WEB_SECURE_COOKIE"
DATA_ROOT_VAR = "HIMARI_DATA_ROOT"
MODEL_VAR = "HIMARI_MODEL"
MODELS_VAR = "HIMARI_MODELS"  # V3-P64: the offered models
REASONING_VAR = "HIMARI_REASONING_EFFORT"
SANDBOX_VAR = "HIMARI_SANDBOX"
CONTEXT_TOKENS_VAR = "HIMARI_CONTEXT_TOKENS"
MAX_RUNS_VAR = "HIMARI_MAX_RUNS"  # V3-P106: a turn's Run limit, 1..1000
TRASH_DAYS_VAR = "HIMARI_TRASH_DAYS"  # V3-P65: days in the trash before a permanent delete
FILE_MAX_VAR = "HIMARI_FILE_MAX_BYTES"  # V3-P66: one uploaded or presented file
FILE_STORE_MAX_VAR = "HIMARI_FILE_STORE_MAX_BYTES"  # V3-P66: the whole file store
FILE_KEEP_DAYS_VAR = "HIMARI_FILE_KEEP_DAYS"  # V3-P66: unused this long, a stored file expires
DOCUMENTS_DIR_VAR = "HIMARI_DOCUMENTS_DIR"  # V3-P70: the documents toolkit; unset: no documents
MEMORY_VAR = "HIMARI_MEMORY"  # V3-P71: 0 turns memory off
MEMORY_AUTO_VAR = "HIMARI_MEMORY_AUTO"  # V3-P85: 0 turns memory passes off
MEMORY_MODEL_VAR = "HIMARI_MEMORY_MODEL"  # V3-P85: their model; unset: none run
MEMORY_IDLE_VAR = "HIMARI_MEMORY_IDLE_MINUTES"  # V3-P85: quiet before a pass
MEMORY_TOKENS_VAR = "HIMARI_MEMORY_DAILY_TOKENS"  # V3-P85: each principal's daily budget
CONVERSATION_SEARCH_VAR = "HIMARI_CONVERSATION_SEARCH"  # V3-P72: 0 turns it off
STREAMING_VAR = "HIMARI_STREAMING"  # V3-P73: 0 turns it off
COMPANION_VAR = "HIMARI_COMPANION"  # V3-P83: 0 turns devices off
COMPANION_DEFAULT_MODEL_VAR = "HIMARI_COMPANION_DEFAULT_MODEL"  # default companion model for companion devices
COMPANION_MODELS_ONLY_VAR = "HIMARI_COMPANION_MODELS_ONLY"  # 1: only companion-verified models offered
TIME_ZONE_VAR = "HIMARI_TIME_ZONE"  # V3-P83: the zone the model is told the time in
MACHINE_SOCKET_VAR = "HIMARI_MACHINE_SOCKET"  # V3-P74: the runner's socket; unset: no machine Environment
MACHINE_PRINCIPALS_VAR = "HIMARI_MACHINE_PRINCIPALS"  # V3-P74: whose new Conversations use it
# V3-P87 §11: speaking first, and its throttling
PROACTIVE_VAR = "HIMARI_PROACTIVE"
PROACTIVE_QUIET_VAR = "HIMARI_PROACTIVE_QUIET"
PROACTIVE_GAP_VAR = "HIMARI_PROACTIVE_GAP_MINUTES"
# V3-P107 §4: free wakes opened a day, passes too (HIMARI_PROACTIVE_FREE_ATTEMPTS is no more)
PROACTIVE_FREE_DAILY_VAR = "HIMARI_PROACTIVE_FREE_DAILY"
# V3-P107 §4: one interval
PROACTIVE_FREE_MINUTES_VAR = "HIMARI_PROACTIVE_FREE_MINUTES"
PROACTIVE_FREE_STEPS_VAR = "HIMARI_PROACTIVE_FREE_STEPS"  # V3-P90's steps: only its "" (off) is still heard
PROACTIVE_RETURN_VAR = "HIMARI_PROACTIVE_RETURN_HOURS"
PROACTIVE_ENDED_VAR = "HIMARI_PROACTIVE_ENDED_MINUTES"  # V3-P90 §8 (was _FOCUS_MINUTES, _GAME_MINUTES)
DEFAULT_FILE_MAX_BYTES = 32 * 1024 * 1024  # as data.api
DEFAULT_FILE_STORE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_FILE_KEEP_DAYS = 30
DEFAULT_MAX_RUNS = 12  # as control.driver.MAX_RUNS
DEFAULT_TRASH_DAYS = 30  # as bootstrap.application.DEFAULT_TRASH_DAYS
DEFAULT_TIME_ZONE = "Asia/Seoul"  # as bootstrap.application.DEFAULT_TIME_ZONE

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "openai/gpt-6-luna"

_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOOPBACK_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_ORIGIN_SCHEMES = frozenset({"http", "https"})


class WebConfigError(Exception):
    """A configuration precondition failed before anything was started."""


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]  # exact ``scheme://host[:port]`` strings
    secure_cookie: bool
    data_root: str
    model_id: str
    reasoning_effort: str | None
    sandbox: bool
    context_tokens: int | None = None  # None: the model's window, capped (Application)
    max_runs: int = DEFAULT_MAX_RUNS  # one turn's Run limit (V3-P106)
    # The offered models and their default efforts (V3-P64); empty: model_id alone.
    models: tuple[tuple[str, str | None], ...] = ()
    trash_days: int = DEFAULT_TRASH_DAYS
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES
    file_store_max_bytes: int = DEFAULT_FILE_STORE_MAX_BYTES
    file_keep_days: int = DEFAULT_FILE_KEEP_DAYS
    documents_dir: str | None = None
    memory: bool = True
    memory_auto: bool = True  # V3-P85 §12
    memory_model: str | None = None
    memory_idle_minutes: int = 10
    memory_daily_tokens: int = 1_000_000
    conversation_search: bool = True
    streaming: bool = True
    companion: bool = True  # V3-P83
    companion_default_model: str | None = None  # V3-P92: automatic preference unless explicitly configured
    companion_models_only: bool = False  # only companion-verified models offered
    time_zone: str = DEFAULT_TIME_ZONE  # V3-P83 §6.4
    machine_socket: str | None = None  # V3-P74
    machine_principals: tuple[str, ...] = ("operator",)
    proactive: ProactiveSettings = ProactiveSettings()  # V3-P87 §11

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebConfig:
        source: Mapping[str, str] = os.environ if env is None else env
        host = _non_empty(source.get(HOST_VAR), DEFAULT_HOST, HOST_VAR)
        port = _port(source.get(PORT_VAR))
        hosts_raw, origins_raw = source.get(ALLOWED_HOSTS_VAR), source.get(ALLOWED_ORIGINS_VAR)
        # Loopback names are always allowed (a DNS-rebinding attack needs a
        # name the attacker controls), so the local CLI keeps working when a
        # reverse proxy's public name is configured.
        allowed_hosts = (_hosts(hosts_raw) if hosts_raw is not None else frozenset()) | _DEFAULT_ALLOWED_HOSTS
        allowed_origins = (
            _origins(origins_raw) if origins_raw is not None
            else frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
        )
        if any("*" in h for h in allowed_hosts) or any("*" in o for o in allowed_origins):
            raise WebConfigError("allowed hosts and origins must not contain wildcards")
        if host not in _LOOPBACK_BIND_HOSTS:
            raise WebConfigError(
                f"{HOST_VAR}={host!r} is not a loopback address: HIMARI serves plain HTTP, so it binds to "
                f"127.0.0.1, ::1 or localhost only; for remote use put a TLS reverse proxy in front"
            )
        cleartext = sorted(o for o in allowed_origins
                           if o.startswith("http://") and o[len("http://"):].rsplit(":", 1)[0] not in _LOOPBACK_ORIGIN_HOSTS)
        if cleartext:
            raise WebConfigError(
                f"{ALLOWED_ORIGINS_VAR} has plain-http origins for other machines {cleartext}: the operator token "
                "would cross the network in clear text; serve them over https (e.g. a TLS reverse proxy)"
            )
        proxied = any(o.startswith("https://") for o in allowed_origins)
        secure_cookie = _flag(source.get(SECURE_COOKIE_VAR), SECURE_COOKIE_VAR, default=proxied)
        if proxied and not secure_cookie:
            raise WebConfigError(f"{SECURE_COOKIE_VAR}=0 is refused with an https origin: the cookie must stay Secure")
        data_root = source.get(DATA_ROOT_VAR, "").strip()
        if not data_root:
            raise WebConfigError(f"{DATA_ROOT_VAR} must name the private data directory")
        effort = source.get(REASONING_VAR, "").strip() or None
        max_runs = _required(source.get(MAX_RUNS_VAR), MAX_RUNS_VAR, DEFAULT_MAX_RUNS, 1, 1000)
        trash_days = _optional(source.get(TRASH_DAYS_VAR), TRASH_DAYS_VAR, DEFAULT_TRASH_DAYS, 1, 365)
        if trash_days is None:
            raise WebConfigError(f"{TRASH_DAYS_VAR} must be between 1 and 365 (there is no keeping forever)")
        file_max = _required(source.get(FILE_MAX_VAR), FILE_MAX_VAR, DEFAULT_FILE_MAX_BYTES, 1, 2**40)
        store_max = _required(source.get(FILE_STORE_MAX_VAR), FILE_STORE_MAX_VAR, DEFAULT_FILE_STORE_MAX_BYTES, 1, 2**50)
        keep_days = _required(source.get(FILE_KEEP_DAYS_VAR), FILE_KEEP_DAYS_VAR, DEFAULT_FILE_KEEP_DAYS, 1, 365)
        if file_max > store_max:
            raise WebConfigError(f"{FILE_MAX_VAR} must not be larger than {FILE_STORE_MAX_VAR}")
        model_id = source.get(MODEL_VAR, "").strip() or DEFAULT_MODEL
        models_raw = source.get(MODELS_VAR)
        models = _models(models_raw, model_id) if models_raw is not None and models_raw.strip() else ()
        documents_dir = source.get(DOCUMENTS_DIR_VAR, "").strip() or None
        if documents_dir is not None and not os.path.isabs(documents_dir):
            raise WebConfigError(f"{DOCUMENTS_DIR_VAR} must be an absolute path")
        machine_socket = source.get(MACHINE_SOCKET_VAR, "").strip() or None
        if machine_socket is not None and not os.path.isabs(machine_socket):
            raise WebConfigError(f"{MACHINE_SOCKET_VAR} must be an absolute path")
        principals = source.get(MACHINE_PRINCIPALS_VAR)
        machine_principals = (("operator",) if principals is None
                              else tuple(p.strip() for p in principals.split(",") if p.strip()))
        return cls(
            host=host,
            port=port,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            secure_cookie=secure_cookie,
            data_root=os.path.abspath(data_root),
            model_id=model_id,
            reasoning_effort=effort,
            sandbox=_flag(source.get(SANDBOX_VAR), SANDBOX_VAR, default=True),
            context_tokens=_positive(source.get(CONTEXT_TOKENS_VAR), CONTEXT_TOKENS_VAR),
            max_runs=max_runs,
            models=models,
            trash_days=trash_days,
            file_max_bytes=file_max,
            file_store_max_bytes=store_max,
            file_keep_days=keep_days,
            documents_dir=documents_dir,
            memory=_flag(source.get(MEMORY_VAR), MEMORY_VAR, default=True),
            memory_auto=_flag(source.get(MEMORY_AUTO_VAR), MEMORY_AUTO_VAR, default=True),
            memory_model=source.get(MEMORY_MODEL_VAR, "").strip() or None,
            memory_idle_minutes=_required(source.get(MEMORY_IDLE_VAR), MEMORY_IDLE_VAR, 10, 1, 240),
            memory_daily_tokens=_required(source.get(MEMORY_TOKENS_VAR), MEMORY_TOKENS_VAR, 1_000_000, 10_000, 10**9),
            conversation_search=_flag(source.get(CONVERSATION_SEARCH_VAR), CONVERSATION_SEARCH_VAR, default=True),
            streaming=_flag(source.get(STREAMING_VAR), STREAMING_VAR, default=True),
            companion=_flag(source.get(COMPANION_VAR), COMPANION_VAR, default=True),
            companion_default_model=source.get(COMPANION_DEFAULT_MODEL_VAR, "").strip() or None,
            companion_models_only=_flag(source.get(COMPANION_MODELS_ONLY_VAR), COMPANION_MODELS_ONLY_VAR, default=False),
            time_zone=_time_zone(source.get(TIME_ZONE_VAR)),
            machine_socket=machine_socket,
            machine_principals=machine_principals,
            proactive=_proactive(source),
        )


def _non_empty(value: str | None, default: str, name: str) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise WebConfigError(f"{name} must not be empty when set")
    return value


def _proactive(source: Mapping[str, str]) -> ProactiveSettings:
    """V3-P87 §11: off unless HIMARI_PROACTIVE; 0 turns a trigger off where the setting allows."""
    try:
        quiet_start, quiet_end = parse_quiet(source.get(PROACTIVE_QUIET_VAR))
    except ValueError:
        raise WebConfigError(f"{PROACTIVE_QUIET_VAR} must be HH:MM-HH:MM, or empty for no quiet hours") from None
    return ProactiveSettings(
        enabled=_flag(source.get(PROACTIVE_VAR), PROACTIVE_VAR, default=False),
        quiet_start=quiet_start, quiet_end=quiet_end,
        gap_minutes=_required(source.get(PROACTIVE_GAP_VAR), PROACTIVE_GAP_VAR, 15, 0, 24 * 60),  # V3-P90 §8
        daily=_required(source.get(PROACTIVE_FREE_DAILY_VAR), PROACTIVE_FREE_DAILY_VAR, 10, 0, 96),
        free_minutes=_free_minutes(source),
        return_hours=_required(source.get(PROACTIVE_RETURN_VAR), PROACTIVE_RETURN_VAR, 1, 0, 72),  # V3-P90 §8
        ended_minutes=_required(source.get(PROACTIVE_ENDED_VAR), PROACTIVE_ENDED_VAR, 60, 0, 24 * 60),
    )


def _free_minutes(source: Mapping[str, str]) -> int:
    """V3-P107 §4: HIMARI_PROACTIVE_FREE_MINUTES (30; 0: no free wakes). Unset, a V3-P90 setting that
    turned free wakes off (HIMARI_PROACTIVE_FREE_STEPS empty) still turns them off; other steps are
    read no more."""
    unset = not (source.get(PROACTIVE_FREE_MINUTES_VAR) or "").strip()  # as _required reads it
    if unset and source.get(PROACTIVE_FREE_STEPS_VAR, "x").strip() == "":
        return 0
    return _required(source.get(PROACTIVE_FREE_MINUTES_VAR), PROACTIVE_FREE_MINUTES_VAR, 30, 0, 24 * 60)


def _port(value: str | None, name: str = PORT_VAR, *, default: int = DEFAULT_PORT) -> int:
    if value is None:
        return default
    try:
        port = int(value.strip())
    except ValueError:
        raise WebConfigError(f"{name} must be an integer") from None
    if not 1 <= port <= 65535:
        raise WebConfigError(f"{name} must be between 1 and 65535")
    return port


def _positive(value: str | None, name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        number = int(value.strip())
    except ValueError:
        raise WebConfigError(f"{name} must be an integer") from None
    if number < 1000:
        raise WebConfigError(f"{name} must be at least 1000")
    return number


def _required(value: str | None, name: str, default: int, low: int, high: int) -> int:
    """An integer setting that cannot be turned off; unset: ``default``."""
    if value is None or not value.strip():
        return default
    try:
        number = int(value.strip())
    except ValueError:
        raise WebConfigError(f"{name} must be an integer") from None
    if not low <= number <= high:
        raise WebConfigError(f"{name} must be between {low} and {high}")
    return number


def _optional(value: str | None, name: str, default: int | None, low: int, high: int) -> int | None:
    """An integer setting where 0 turns the guard off; unset: ``default``."""
    if value is None or not value.strip():
        return default
    try:
        number = int(value.strip())
    except ValueError:
        raise WebConfigError(f"{name} must be an integer") from None
    if number == 0:
        return None
    if not low <= number <= high:
        raise WebConfigError(f"{name} must be 0 (off) or between {low} and {high}")
    return number


def _time_zone(value: str | None) -> str:
    if value is None or not value.strip():
        return DEFAULT_TIME_ZONE
    try:
        ZoneInfo(value.strip())
    except (ValueError, ZoneInfoNotFoundError):
        raise WebConfigError(f"{TIME_ZONE_VAR} must be an IANA time zone name, e.g. Asia/Seoul") from None
    return value.strip()


def _flag(value: str | None, name: str, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise WebConfigError(f"{name} must be a boolean (1/0, true/false)")


def _models(raw: str, default: str) -> tuple[tuple[str, str | None], ...]:
    """``id[=effort]`` entries. Only the shape is checked here; whether the
    Provider supports an effort is checked when the server starts."""
    models: list[tuple[str, str | None]] = []
    for entry in _split(raw):
        model, _, effort = (part.strip() for part in entry.partition("="))
        if not model or any(c.isspace() for c in model) or (effort and any(c.isspace() for c in effort)):
            raise WebConfigError(f"{MODELS_VAR} entry {entry!r} is not model[=effort]")
        if model in (m for m, _ in models):
            raise WebConfigError(f"{MODELS_VAR} lists {model} twice")
        models.append((model, effort or None))
    if default not in (m for m, _ in models):
        raise WebConfigError(f"{MODELS_VAR} must list the default model {default} ({MODEL_VAR})")
    return tuple(models)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _hosts(raw: str) -> frozenset[str]:
    values = _split(raw)
    if not values:
        raise WebConfigError(f"{ALLOWED_HOSTS_VAR} must name at least one host when set")
    return frozenset(values)


def _origins(raw: str) -> frozenset[str]:
    values = _split(raw)
    if not values:
        raise WebConfigError(f"{ALLOWED_ORIGINS_VAR} must name at least one origin when set")
    return frozenset(_normalize_origin(v) for v in values)


def _normalize_origin(value: str) -> str:
    """Exactly ``scheme://host[:port]``: no userinfo, path, query or fragment."""
    parts = urlsplit(value)
    if parts.scheme not in _ORIGIN_SCHEMES or not parts.netloc or parts.username or parts.password:
        raise WebConfigError(f"{ALLOWED_ORIGINS_VAR} entry {value!r} is not an http(s) origin")
    if parts.path not in ("", "/") or parts.query or parts.fragment or not parts.hostname:
        raise WebConfigError(f"{ALLOWED_ORIGINS_VAR} entry {value!r} must be scheme://host[:port] only")
    try:
        port = parts.port
    except ValueError:
        raise WebConfigError(f"{ALLOWED_ORIGINS_VAR} entry {value!r} has an invalid port") from None
    default = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}" if port in (None, default) else f"{parts.scheme}://{host}:{port}"
