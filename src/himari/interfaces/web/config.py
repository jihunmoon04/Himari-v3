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

Tool views (MCP Apps, V3-P58) are framed from a second origin: the same
scheme and host as the page, on ``HIMARI_WEB_SANDBOX_PORT`` (default: the
port + 1). A reverse proxy must forward that port too, for the same host.

Models (V3-P64 §3): ``HIMARI_MODEL`` is the default; ``HIMARI_MODELS``
optionally lists every offered model as ``id[=effort]`` (comma-separated;
an id may hold ``:``, so the effort follows ``=``). The default must be
listed. Unset, the default model is the only one offered.

Trash (V3-P65): ``HIMARI_TRASH_DAYS`` (1..365, default 30) is how long a
deleted Conversation stays in the trash before it is deleted for good.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

HOST_VAR = "HIMARI_WEB_HOST"
PORT_VAR = "HIMARI_WEB_PORT"
SANDBOX_PORT_VAR = "HIMARI_WEB_SANDBOX_PORT"
ALLOWED_HOSTS_VAR = "HIMARI_WEB_ALLOWED_HOSTS"
ALLOWED_ORIGINS_VAR = "HIMARI_WEB_ALLOWED_ORIGINS"
SECURE_COOKIE_VAR = "HIMARI_WEB_SECURE_COOKIE"
DATA_ROOT_VAR = "HIMARI_DATA_ROOT"
MODEL_VAR = "HIMARI_MODEL"
MODELS_VAR = "HIMARI_MODELS"  # V3-P64: the offered models
REASONING_VAR = "HIMARI_REASONING_EFFORT"
SANDBOX_VAR = "HIMARI_SANDBOX"
CONTEXT_TOKENS_VAR = "HIMARI_CONTEXT_TOKENS"
TURN_BUDGET_VAR = "HIMARI_TURN_TOKEN_BUDGET"  # V3-P62: 0 turns it off
UNATTENDED_RUNS_VAR = "HIMARI_UNATTENDED_RUNS"  # 0 turns it off
MAX_RUNS_VAR = "HIMARI_MAX_RUNS"  # 0 (default): no Run limit
TRASH_DAYS_VAR = "HIMARI_TRASH_DAYS"  # V3-P65: days in the trash before a permanent delete
DEFAULT_TURN_BUDGET = 3_000_000
# Each request keeps 8,192 tokens for its answer (control.turn.MIN_OUTPUT_RESERVE),
# so a smaller budget could refuse even the first request (PR #48 review C2).
MIN_TURN_BUDGET = 16_384
DEFAULT_UNATTENDED_RUNS = 20
DEFAULT_TRASH_DAYS = 30  # as bootstrap.application.DEFAULT_TRASH_DAYS

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
    sandbox_port: int | None = None  # Tool views' origin (V3-P58); None: port + 1
    # One turn's guards (V3-P62); None turns one off.
    turn_budget: int | None = DEFAULT_TURN_BUDGET
    unattended_runs: int | None = DEFAULT_UNATTENDED_RUNS
    max_runs: int | None = None
    # The offered models and their default efforts (V3-P64); empty: model_id alone.
    models: tuple[tuple[str, str | None], ...] = ()
    trash_days: int = DEFAULT_TRASH_DAYS

    @property
    def view_port(self) -> int:
        return self.sandbox_port if self.sandbox_port is not None else self.port + 1

    def sandbox_origin(self, origin: str) -> str:
        """The sandbox origin that pairs with an allowed page origin: its
        scheme and host on the sandbox port."""
        parts = urlsplit(origin)
        host = parts.hostname or ""
        return f"{parts.scheme}://{f'[{host}]' if ':' in host else host}:{self.view_port}"

    @property
    def sandbox_origins(self) -> frozenset[str]:
        return frozenset(self.sandbox_origin(o) for o in self.allowed_origins)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebConfig:
        source: Mapping[str, str] = os.environ if env is None else env
        host = _non_empty(source.get(HOST_VAR), DEFAULT_HOST, HOST_VAR)
        port = _port(source.get(PORT_VAR))
        sandbox_port = _port(source.get(SANDBOX_PORT_VAR), SANDBOX_PORT_VAR, default=port + 1)
        if sandbox_port == port or sandbox_port > 65535:
            raise WebConfigError(f"{SANDBOX_PORT_VAR} must be a port other than {PORT_VAR}: views need their own origin")
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
        turn_budget = _optional(source.get(TURN_BUDGET_VAR), TURN_BUDGET_VAR, DEFAULT_TURN_BUDGET, MIN_TURN_BUDGET,
                                10**9)
        unattended = _optional(source.get(UNATTENDED_RUNS_VAR), UNATTENDED_RUNS_VAR, DEFAULT_UNATTENDED_RUNS, 1, 1000)
        max_runs = _optional(source.get(MAX_RUNS_VAR), MAX_RUNS_VAR, None, 1, 1000)
        trash_days = _optional(source.get(TRASH_DAYS_VAR), TRASH_DAYS_VAR, DEFAULT_TRASH_DAYS, 1, 365)
        if trash_days is None:
            raise WebConfigError(f"{TRASH_DAYS_VAR} must be between 1 and 365 (there is no keeping forever)")
        if turn_budget is None and max_runs is None:
            raise WebConfigError(f"{TURN_BUDGET_VAR}=0 needs {MAX_RUNS_VAR}: a turn needs a token budget or a Run limit")
        model_id = source.get(MODEL_VAR, "").strip() or DEFAULT_MODEL
        models_raw = source.get(MODELS_VAR)
        models = _models(models_raw, model_id) if models_raw is not None and models_raw.strip() else ()
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
            sandbox_port=sandbox_port,
            turn_budget=turn_budget,
            unattended_runs=unattended,
            max_runs=max_runs,
            models=models,
            trash_days=trash_days,
        )


def _non_empty(value: str | None, default: str, name: str) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise WebConfigError(f"{name} must not be empty when set")
    return value


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
