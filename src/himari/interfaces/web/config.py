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
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

HOST_VAR = "HIMARI_WEB_HOST"
PORT_VAR = "HIMARI_WEB_PORT"
ALLOWED_HOSTS_VAR = "HIMARI_WEB_ALLOWED_HOSTS"
ALLOWED_ORIGINS_VAR = "HIMARI_WEB_ALLOWED_ORIGINS"
SECURE_COOKIE_VAR = "HIMARI_WEB_SECURE_COOKIE"
DATA_ROOT_VAR = "HIMARI_DATA_ROOT"
MODEL_VAR = "HIMARI_MODEL"
REASONING_VAR = "HIMARI_REASONING_EFFORT"
SANDBOX_VAR = "HIMARI_SANDBOX"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "openai/gpt-5-nano"

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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebConfig:
        source: Mapping[str, str] = os.environ if env is None else env
        host = _non_empty(source.get(HOST_VAR), DEFAULT_HOST, HOST_VAR)
        port = _port(source.get(PORT_VAR))
        hosts_raw, origins_raw = source.get(ALLOWED_HOSTS_VAR), source.get(ALLOWED_ORIGINS_VAR)
        allowed_hosts = _hosts(hosts_raw) if hosts_raw is not None else _DEFAULT_ALLOWED_HOSTS
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
        return cls(
            host=host,
            port=port,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            secure_cookie=secure_cookie,
            data_root=os.path.abspath(data_root),
            model_id=source.get(MODEL_VAR, "").strip() or DEFAULT_MODEL,
            reasoning_effort=effort,
            sandbox=_flag(source.get(SANDBOX_VAR), SANDBOX_VAR, default=True),
        )


def _non_empty(value: str | None, default: str, name: str) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise WebConfigError(f"{name} must not be empty when set")
    return value


def _port(value: str | None) -> int:
    if value is None:
        return DEFAULT_PORT
    try:
        port = int(value.strip())
    except ValueError:
        raise WebConfigError(f"{PORT_VAR} must be an integer") from None
    if not 1 <= port <= 65535:
        raise WebConfigError(f"{PORT_VAR} must be between 1 and 65535")
    return port


def _flag(value: str | None, name: str, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise WebConfigError(f"{name} must be a boolean (1/0, true/false)")


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
