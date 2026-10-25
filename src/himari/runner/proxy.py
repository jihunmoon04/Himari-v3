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
- The tunnel carries TLS only, and to the name asked for: the client's
  first message must be a TLS ClientHello whose server name (SNI) is the
  ``CONNECT`` name. Anything else is cut (non-TLS: closed; a missing or other
  name, or an encrypted ClientHello: a TLS ``unrecognized_name`` alert).
  What travels inside the TLS (an HTTP ``Host``, ``:authority``) is not seen:
  the list bounds the TLS servers reached, not the requests sent to them
  (§22.2).
- Every command has a socket of its own (``Proxy.open``), and at most
  ``MAX_CONNECTIONS`` connections in any state; all commands together at
  most ``MAX_TOTAL``. Over a limit is ``503`` at once: there is no queue, so
  one command cannot hold another's requests up (§22.2).
- A refusal is HTTP 403 with one line of reason. The log names the host,
  the bytes and the outcome, never the content.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
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
DNS_SECONDS = 10.0
CONNECT_SECONDS = 5.0  # per address
HELLO_SECONDS = 10.0
MAX_CONNECTIONS = 32  # one command's
MAX_TOTAL = 256  # all commands'
MAX_HELLO_BYTES = 65536
BUSY = "503 Service Unavailable"
DEFAULT_ALLOW = ("pypi.org", "files.pythonhosted.org", "repo.maven.apache.org", "repo1.maven.org",
                 "plugins.gradle.org", "services.gradle.org", "downloads.gradle.org", "github.com",
                 "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
                 "registry.npmjs.org")

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_TLS_HANDSHAKE, _CLIENT_HELLO = 22, 1
_SERVER_NAME, _ENCRYPTED_CLIENT_HELLO = 0x0000, 0xFE0D
UNRECOGNIZED_NAME, DECODE_ERROR = 112, 50  # TLS alerts
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
# Lookups have threads of their own: a slow resolver does not hold up the runner's file work.
_DNS = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="himari-proxy-dns")


class Refused(Exception):
    """The request is not allowed; the message is the reason the client sees
    (inside a tunnel, ``alert`` is the TLS alert it gets, or none: closed)."""

    def __init__(self, reason: str, status: str = "403 Forbidden", *, alert: int | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.alert = alert


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
    infos = await asyncio.get_running_loop().run_in_executor(
        _DNS, functools.partial(socket.getaddrinfo, name, port, type=socket.SOCK_STREAM))
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


async def client_hello(reader: asyncio.StreamReader) -> tuple[bytes, str]:
    """The client's first TLS message, read whole (it may span records), and
    the server name it asks for; Refused if it is not a ClientHello."""
    raw, message = bytearray(), bytearray()
    while True:
        header = await reader.readexactly(5)
        length = int.from_bytes(header[3:5], "big")
        if header[0] != _TLS_HANDSHAKE or header[1] != 3 or not 0 < length <= 16384:
            raise Refused("not TLS")
        body = await reader.readexactly(length)
        raw += header + body
        message += body
        if message[0] != _CLIENT_HELLO:
            raise Refused("not a TLS ClientHello", alert=DECODE_ERROR)
        if len(message) >= 4:
            size = 4 + int.from_bytes(message[1:4], "big")
            if size > MAX_HELLO_BYTES:
                raise Refused("TLS ClientHello too large", alert=DECODE_ERROR)
            if len(message) >= size:
                return bytes(raw), server_name(bytes(message[4:size]))


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def take(self, size: int) -> bytes:
        if self.at + size > len(self.data):
            raise Refused("malformed TLS ClientHello", alert=DECODE_ERROR)
        self.at += size
        return self.data[self.at - size:self.at]

    def number(self, width: int) -> int:
        return int.from_bytes(self.take(width), "big")

    def vector(self, width: int) -> _Cursor:
        return _Cursor(self.take(self.number(width)))

    @property
    def more(self) -> bool:
        return self.at < len(self.data)


def server_name(hello: bytes) -> str:
    """The normalized SNI host name of a ClientHello's body, or Refused: none,
    more than one, or an encrypted ClientHello (which hides the real one)."""
    body = _Cursor(hello)
    body.take(34)  # version, random
    for width in (1, 2, 1):  # session id, cipher suites, compression
        body.vector(width)
    extensions = body.vector(2)
    names: list[_Cursor] = []
    while extensions.more:
        kind, data = extensions.number(2), extensions.vector(2)
        if kind == _ENCRYPTED_CLIENT_HELLO:
            raise Refused("an encrypted ClientHello hides the server name", alert=UNRECOGNIZED_NAME)
        if kind == _SERVER_NAME:
            names.append(data)
    hosts: list[bytes] = []
    if len(names) == 1:
        entries = names[0].vector(2)
        while entries.more:
            kind, value = entries.number(1), entries.vector(2).data
            if kind == 0:  # host_name
                hosts.append(value)
    if len(hosts) != 1:
        raise Refused("the TLS ClientHello must name its server (SNI) once", alert=UNRECOGNIZED_NAME)
    try:
        return normalize(hosts[0].decode("ascii"))
    except (UnicodeDecodeError, Refused):
        raise Refused("the TLS server name is not a host name", alert=UNRECOGNIZED_NAME) from None


class Proxy:
    """The proxy: its rules, and the doors commands come in by. ``resolve``
    is the DNS lookup (tests replace it)."""

    def __init__(self, allow: Sequence[str], *, resolve: Resolver = _resolve) -> None:
        self.allow = tuple(allow)
        self._resolve = resolve
        self._doors: set[Door] = set()

    async def open(self, path: str) -> Door:
        """A socket for one command; ``Door.close`` when the command is done."""
        door = Door(self, path)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        door.server = await asyncio.start_unix_server(functools.partial(self._serve, door), path=path)
        os.chmod(path, 0o600)
        self._doors.add(door)
        return door

    async def close(self) -> None:
        """Close every door, and the tunnels through them."""
        for door in list(self._doors):
            await door.close()

    async def check(self, host: str, port: int) -> tuple[str, list[str]]:
        """The normalized name and its checked addresses, or Refused."""
        name = normalize(host)
        if port != PORT:
            raise Refused(f"only port {PORT} is allowed")
        if not allowed(name, self.allow):
            raise Refused(f"{name} is not on this server's allowed list ({', '.join(self.allow)})")
        try:
            async with asyncio.timeout(DNS_SECONDS):
                addresses = list(await self._resolve(name, port))
        except (OSError, TimeoutError):
            raise Refused(f"{name} could not be resolved", "502 Bad Gateway") from None
        if not addresses:
            raise Refused(f"{name} could not be resolved", "502 Bad Gateway")
        if not all(public(a) for a in addresses):
            raise Refused(f"{name} resolves to an address that is not public")
        return name, addresses

    async def _serve(self, door: Door, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        name, sent, received, outcome = "?", 0, 0, "refused"
        up_writer: asyncio.StreamWriter | None = None
        established = False
        door.open.add(writer)
        try:
            if len(door.open) > MAX_CONNECTIONS:
                raise Refused(f"this command already has {MAX_CONNECTIONS} connections through the proxy", BUSY)
            if sum(len(d.open) for d in self._doors) > MAX_TOTAL:
                raise Refused("the proxy is busy; try again later", BUSY)
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
            door.far.add(up_writer)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            established = True
            try:
                async with asyncio.timeout(HELLO_SECONDS):
                    hello, server = await client_hello(reader)
            except (asyncio.IncompleteReadError, TimeoutError):
                outcome = "no TLS ClientHello"
                return
            if server != name:
                raise Refused(f"the TLS server name {server} is not {name}", alert=UNRECOGNIZED_NAME)
            up_writer.write(hello)
            outcome = "tunnel"
            sent, received = await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            sent += len(hello)
        except Refused as refused:
            outcome = f"refused ({refused})"
            if not established:
                body = f"{refused}\n".encode()
                writer.write(f"HTTP/1.1 {refused.status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                             f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            elif refused.alert is not None:
                writer.write(bytes((21, 3, 3, 0, 2, 2, refused.alert)))  # a fatal TLS alert record
            with contextlib.suppress(OSError):
                await writer.drain()
            if refused.status == BUSY:  # answered unread: take the request in, or closing resets the reply
                with contextlib.suppress(OSError, TimeoutError):
                    async with asyncio.timeout(1):
                        await reader.read(MAX_HEAD_BYTES)
        except OSError:
            outcome = "broken"
        finally:
            door.open.discard(writer)
            writer.close()
            if up_writer is not None:
                door.far.discard(up_writer)
                up_writer.close()
            log.info("proxy %s %s up=%d down=%d", name, outcome, sent, received)


class Door:
    """One command's socket into the proxy, and its connections."""

    def __init__(self, proxy: Proxy, path: str) -> None:
        self.proxy, self.path = proxy, path
        self.server: asyncio.Server | None = None
        self.open: set[asyncio.StreamWriter] = set()  # the command's ends; the count
        self.far: set[asyncio.StreamWriter] = set()  # the servers' ends

    async def close(self) -> None:
        """Stop listening, end this command's tunnels, remove the socket."""
        self.proxy._doors.discard(self)
        if self.server is not None:
            self.server.close()
            for writer in [*self.open, *self.far]:  # a server may keep its end open after ours closed
                writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await self.server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)


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
