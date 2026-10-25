"""The network proxy of the machine profile (V3-P74 §11).

A command's network namespace has loopback only; its one way out is this
proxy, reached through the bridge the sandbox helper starts on
``127.0.0.1:3128`` (§11.2). The proxy listens on a Unix socket of the
runner's own, outside the sandbox.

Rules (§11.3):
- ``CONNECT name:443`` only (HTTPS). Plain HTTP is refused: every host on
  the default list speaks HTTPS, and one tunnel per connection leaves no
  request to re-check (§22.2).
- The name is normalized (lower case, no trailing dot, IDNA A-label) and
  must be on the allow list: an exact name, or ``*.suffix`` for the names
  under it. IP literals in any spelling are refused.
- The name is resolved once and **every** address checked; one that is not
  public (loopback, private, link-local, CGNAT, multicast, reserved, an
  IPv4 address inside IPv6) refuses the request. The connection goes to the
  checked addresses only, so a second DNS answer cannot change it.
- A refusal is HTTP 403 with one line of reason. The log names the host,
  the bytes and the outcome, never the content.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence

log = logging.getLogger("himari.runner.proxy")

PORT = 443
MAX_HEAD_BYTES = 8192
HEAD_SECONDS = 10.0
CONNECT_SECONDS = 5.0  # per address
MAX_TUNNELS = 64
DEFAULT_ALLOW = ("pypi.org", "files.pythonhosted.org", "repo.maven.apache.org", "repo1.maven.org",
                 "plugins.gradle.org", "services.gradle.org", "downloads.gradle.org", "github.com",
                 "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
                 "registry.npmjs.org")

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class Refused(Exception):
    """The request is not allowed; the message is the reason the client sees."""

    def __init__(self, reason: str, status: str = "403 Forbidden") -> None:
        super().__init__(reason)
        self.status = status


def normalize(name: str) -> str:
    """A host name as compared: lower case, no trailing dot, A-labels.
    Refused: anything that is not a DNS name, and IP literals."""
    if not name or len(name) > 253:
        raise Refused("not a host name")
    name = name.lower()
    if name.endswith("."):
        name = name[:-1]
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        raise Refused("not a host name") from None
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise Refused("not a host name")
    if labels[-1].isdigit() or _inet_aton(name):  # 127.1, 2130706433, 0x7f.1 ...
        raise Refused("IP addresses are not allowed; use the host's name")
    return name


def _inet_aton(name: str) -> bool:
    try:
        socket.inet_aton(name)
    except OSError:
        return False
    return True


def allow_list(entries: Iterable[str]) -> tuple[str, ...]:
    """The configured list, each entry normalized (ValueError if one is not a name)."""
    normal = []
    for entry in entries:
        wildcard = entry.startswith("*.")
        try:
            name = normalize(entry[2:] if wildcard else entry)
        except Refused as refused:
            raise ValueError(f"{entry}: {refused}") from None
        normal.append(f"*.{name}" if wildcard else name)
    return tuple(normal)


def allowed(name: str, allow: Sequence[str]) -> bool:
    return any(name == entry or (entry.startswith("*.") and name.endswith(entry[1:])) for entry in allow)


def public(address: str) -> bool:
    """Only an address on the public internet; an IPv4 address carried in
    IPv6 is judged as itself."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (
            ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in _NAT64 else None)
        if inner is not None:
            return public(str(inner))
        if ip.teredo is not None:
            return False
    elif ip in _CGNAT:
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


async def _resolve(name: str, port: int) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def target(head: bytes) -> tuple[str, int]:
    """The request's ``CONNECT`` target, or Refused."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
        method, where, version = line.split(" ")
    except (UnicodeDecodeError, ValueError):
        raise Refused("malformed request", "400 Bad Request") from None
    if not version.startswith("HTTP/1."):
        raise Refused("malformed request", "400 Bad Request")
    if method != "CONNECT":
        raise Refused("only HTTPS is allowed through this proxy (plain HTTP is not)")
    host, _, port = where.rpartition(":")
    if host.startswith("["):
        raise Refused("IP addresses are not allowed; use the host's name")
    if not host or not port.isdigit():
        raise Refused("malformed request", "400 Bad Request")
    return host, int(port)


class Proxy:
    """The proxy server. ``resolve`` is the DNS lookup (tests replace it)."""

    def __init__(self, allow: Sequence[str], *, resolve: Resolver = _resolve) -> None:
        self.allow = tuple(allow)
        self._resolve = resolve
        self._server: asyncio.Server | None = None
        self._slots = asyncio.Semaphore(MAX_TUNNELS)
        self._open: set[asyncio.StreamWriter] = set()

    async def start(self, path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        self._server = await asyncio.start_unix_server(self._serve, path=path)
        os.chmod(path, 0o600)

    async def close(self) -> None:
        """Stop listening and end the open tunnels."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._open):
                writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await self._server.wait_closed()

    async def check(self, host: str, port: int) -> tuple[str, list[str]]:
        """The normalized name and its checked addresses, or Refused."""
        name = normalize(host)
        if port != PORT:
            raise Refused(f"only port {PORT} is allowed")
        if not allowed(name, self.allow):
            raise Refused(f"{name} is not on this server's allowed list ({', '.join(self.allow)})")
        try:
            addresses = list(await self._resolve(name, port))
        except OSError:
            raise Refused(f"{name} could not be resolved", "502 Bad Gateway") from None
        if not addresses:
            raise Refused(f"{name} could not be resolved", "502 Bad Gateway")
        if not all(public(a) for a in addresses):
            raise Refused(f"{name} resolves to an address that is not public")
        return name, addresses

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        name, sent, received, outcome = "?", 0, 0, "refused"
        up_writer: asyncio.StreamWriter | None = None
        self._open.add(writer)
        try:
            async with self._slots:
                try:
                    async with asyncio.timeout(HEAD_SECONDS):
                        head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                    return
                if len(head) > MAX_HEAD_BYTES:
                    raise Refused("request head too large", "400 Bad Request")
                host, port = target(head)
                name = host
                name, addresses = await self.check(host, port)
                upstream = await _connect(addresses, port)
                if upstream is None:
                    raise Refused(f"{name} could not be reached", "502 Bad Gateway")
                up_reader, up_writer = upstream
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                outcome = "tunnel"
                sent, received = await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except Refused as refused:
            outcome = f"refused ({refused})"
            body = f"{refused}\n".encode()
            writer.write(f"HTTP/1.1 {refused.status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            with contextlib.suppress(OSError):
                await writer.drain()
        except OSError:
            outcome = "broken"
        finally:
            self._open.discard(writer)
            writer.close()
            if up_writer is not None:
                up_writer.close()
            log.info("proxy %s %s up=%d down=%d", name, outcome, sent, received)


async def _connect(addresses: Sequence[str], port: int
                   ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """The first checked address that answers (never a new lookup)."""
    for address in addresses:
        try:
            async with asyncio.timeout(CONNECT_SECONDS):
                return await asyncio.open_connection(address, port)
        except (OSError, TimeoutError):
            continue
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """Copy until end of stream, then half-close the other side; the bytes copied."""
    total = 0
    try:
        while data := await reader.read(65536):
            total += len(data)
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, RuntimeError):
        writer.close()
    return total
