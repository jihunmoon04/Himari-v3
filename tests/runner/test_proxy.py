"""The machine profile's network proxy (V3-P74 §11.3) and its configuration."""

from __future__ import annotations

import asyncio
import os
import ssl
import tempfile
from collections.abc import Sequence
from typing import Any

import pytest

from himari.runner import proxy
from himari.runner.config import ConfigError, parse
from himari.runner.proxy import (
    DEFAULT_ALLOW,
    UNRECOGNIZED_NAME,
    Proxy,
    Refused,
    allow_list,
    allowed,
    client_hello,
    normalize,
    public,
    server_name,
    target,
)


@pytest.mark.parametrize(("name", "normal"), [("PyPI.org.", "pypi.org"), ("bücher.example", "xn--bcher-kva.example"),
                                             ("a-b.c", "a-b.c")])
def test_names_are_normalized(name: str, normal: str) -> None:
    assert normalize(name) == normal


@pytest.mark.parametrize("name", ["127.0.0.1", "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "1.2.3.4.",
                                  "example.123", "", "a..b", "-a.b", "a_b.c", "a b.c", "x" * 64 + ".com"])
def test_ip_literals_and_non_names_are_refused(name: str) -> None:
    with pytest.raises(Refused):
        normalize(name)


def test_the_allow_list_is_exact_or_a_wildcard_below() -> None:
    allow = allow_list(["PyPI.org", "*.githubusercontent.com"])
    assert allow == ("pypi.org", "*.githubusercontent.com")
    assert allowed("pypi.org", allow) and not allowed("evil-pypi.org", allow) and not allowed("x.pypi.org", allow)
    assert allowed("raw.githubusercontent.com", allow) and allowed("a.b.githubusercontent.com", allow)
    assert not allowed("githubusercontent.com", allow) and not allowed("xgithubusercontent.com", allow)
    with pytest.raises(ValueError):
        allow_list(["10.0.0.1"])


@pytest.mark.parametrize(("address", "ok"), [
    ("151.101.0.223", True), ("2a04:4e42::223", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("240.0.0.1", False), ("255.255.255.255", False), ("::1", False), ("::", False), ("fc00::1", False),
    ("fe80::1", False), ("ff02::1", False), ("::ffff:127.0.0.1", False), ("::ffff:151.101.0.223", True),
    ("2002:7f00:1::", False), ("64:ff9b::a00:1", False), ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),
])
def test_only_public_addresses(address: str, ok: bool) -> None:
    assert public(address) is ok


def test_the_request_line() -> None:
    assert target(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n") == ("pypi.org", 443)
    for head, status in ((b"GET http://pypi.org/ HTTP/1.1\r\n\r\n", "403 Forbidden"),
                         (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", "403 Forbidden"),
                         (b"CONNECT pypi.org HTTP/1.1\r\n\r\n", "400 Bad Request"),
                         (b"CONNECT pypi.org:443 SPDY/3\r\n\r\n", "400 Bad Request"),
                         (b"\xff\r\n\r\n", "400 Bad Request")):
        with pytest.raises(Refused) as refused:
            target(head)
        assert refused.value.status == status


def resolver(answers: dict[str, list[str]]) -> Any:
    seen: list[str] = []

    async def resolve(name: str, port: int) -> Sequence[str]:
        seen.append(name)
        if name not in answers:
            raise OSError("no such name")
        return answers[name]

    resolve.seen = seen  # type: ignore[attr-defined]
    return resolve


def hello(server: str | None) -> bytes:
    """A real client's first TLS flight (Python's ssl), asking for ``server``."""
    context = ssl.create_default_context()
    if server is None:
        context.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    with pytest.raises(ssl.SSLWantReadError):
        context.wrap_bio(incoming, outgoing, server_hostname=server).do_handshake()
    return outgoing.read()


def made_hello(extensions: list[tuple[int, bytes]]) -> bytes:
    """A ClientHello body with these extensions (and nothing to offer)."""
    listed = b"".join(kind.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data for kind, data in extensions)
    return b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + len(listed).to_bytes(2, "big") + listed


def sni(*names: bytes) -> tuple[int, bytes]:
    entries = b"".join(b"\x00" + len(n).to_bytes(2, "big") + n for n in names)
    return 0, len(entries).to_bytes(2, "big") + entries


def records(message: bytes, size: int) -> bytes:
    """A handshake message cut into TLS records of ``size`` bytes."""
    return b"".join(b"\x16\x03\x01" + len(part).to_bytes(2, "big") + part
                    for part in (message[i:i + size] for i in range(0, len(message), size)))


def read_hello(data: bytes) -> tuple[bytes, str]:
    async def go() -> tuple[bytes, str]:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await client_hello(reader)

    return asyncio.run(go())


def test_the_server_name_of_a_client_hello() -> None:
    flight = hello("PyPI.org")
    assert read_hello(flight) == (flight, "pypi.org")
    message = b"\x01" + len(body := made_hello([sni(b"pypi.org")])).to_bytes(3, "big") + body
    split = records(message, 7)  # one ClientHello over several records
    assert read_hello(split + b"more") == (split, "pypi.org")


@pytest.mark.parametrize(("body", "reason"), [
    (made_hello([]), "SNI"),
    (made_hello([sni()]), "SNI"),
    (made_hello([sni(b"a.example", b"b.example")]), "SNI"),
    (made_hello([sni(b"a.example"), sni(b"b.example")]), "SNI"),
    (made_hello([sni(b"pypi.org"), (0xFE0D, b"\x00")]), "encrypted ClientHello"),
    (made_hello([sni(b"127.0.0.1")]), "not a host name"),
    (made_hello([sni(b"pypi.org")])[:-3], "malformed"),
])
def test_client_hellos_that_do_not_say_where_they_go(body: bytes, reason: str) -> None:
    with pytest.raises(Refused, match=reason) as refused:
        server_name(body)
    assert refused.value.alert is not None


@pytest.mark.parametrize("data", [b"GET / HTTP/1.1\r\n\r\n", b"\x17\x03\x03\x00\x05hello",
                                  b"\x16\x03\x01\x00\x00"])
def test_what_is_not_tls_is_refused(data: bytes) -> None:
    with pytest.raises(Refused, match="not TLS"):
        read_hello(data)
    with pytest.raises(Refused, match="not a TLS ClientHello"):
        read_hello(records(b"\x02\x00\x00\x00", 4))
    with pytest.raises(Refused, match="too large"):
        read_hello(records(b"\x01\x01\x00\x00", 4))


async def open_door(p: Proxy) -> str:
    """A door of ``p`` in a fresh folder; its path."""
    path = os.path.join(tempfile.mkdtemp(prefix="px-"), "p.sock")
    await p.open(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    return path


async def close_all(p: Proxy, path: str) -> None:
    await p.close()
    assert not os.path.exists(path)
    os.rmdir(os.path.dirname(path))


def exchange(p: Proxy, request: bytes, *, then: bytes = b"") -> tuple[bytes, list[bytes]]:
    """Send ``request`` through a door of the proxy; with ``then``, send it
    into the tunnel and read until the far end closes."""
    received: list[bytes] = []

    async def go() -> bytes:
        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while len(data) < len(then) and (chunk := await reader.read(65536)):
                data += chunk
            received.append(data)
            writer.write(b"pong")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy.PORT = server.sockets[0].getsockname()[1]
        path = await open_door(p)
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(request.replace(b":443", f":{proxy.PORT}".encode()))
            if then:
                head = await reader.readuntil(b"\r\n\r\n")
                writer.write(then)
                answer = head + await reader.read()
            else:
                answer = await reader.read()
            writer.close()
            return answer
        finally:
            await close_all(p, path)
            server.close()

    return asyncio.run(go()), received


@pytest.fixture
def loopback_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test's upstream is on 127.0.0.1: let that one address count as public."""
    monkeypatch.setattr(proxy, "public", lambda a: a == "127.0.0.1" or public(a))
    monkeypatch.setattr(proxy, "PORT", 443)


def test_a_tunnel_to_an_allowed_name(loopback_counts: None) -> None:
    resolve = resolver({"pypi.org": ["127.0.0.1"]})
    flight = hello("pypi.org")
    answer, received = exchange(Proxy(("pypi.org",), resolve=resolve),
                                b"CONNECT PyPI.org.:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n", then=flight)
    assert answer == b"HTTP/1.1 200 Connection established\r\n\r\npong" and received == [flight]
    assert resolve.seen == ["pypi.org"]  # resolved once, the normalized name


@pytest.mark.parametrize(("then", "alert"), [
    (hello("evil.example"), True),  # another site behind the same addresses
    (hello(None), True),
    (b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n", False),
])
def test_the_tunnel_carries_tls_to_the_name_asked_for_only(loopback_counts: None, then: bytes, alert: bool) -> None:
    answer, received = exchange(Proxy(("pypi.org",), resolve=resolver({"pypi.org": ["127.0.0.1"]})),
                                b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", then=then)
    established = b"HTTP/1.1 200 Connection established\r\n\r\n"
    assert answer == established + (bytes((21, 3, 3, 0, 2, 2, UNRECOGNIZED_NAME)) if alert else b"")
    assert received == [b""]  # nothing reached the far end


@pytest.mark.parametrize(("request_head", "answers", "reason"), [
    (b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n", {"evil.example": ["127.0.0.1"]}, b"not on this server's allowed"),
    (b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", {"pypi.org": ["127.0.0.1", "10.0.0.1"]}, b"not public"),
    (b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", {"pypi.org": ["192.168.0.1"]}, b"not public"),
    (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", {}, b"IP addresses are not allowed"),
    (b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n", {}, b"only HTTPS"),
])
def test_refusals_say_why(loopback_counts: None, request_head: bytes, answers: dict[str, list[str]],
                          reason: bytes) -> None:
    answer, received = exchange(Proxy(("pypi.org",), resolve=resolver(answers)), request_head)
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n") and reason in answer and received == []


def test_another_port_an_unknown_name_and_a_slow_resolver(loopback_counts: None,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(name: str, port: int) -> Sequence[str]:
        await asyncio.Event().wait()
        return []

    async def check() -> None:
        p = Proxy(("pypi.org", "gone.example"), resolve=resolver({}))
        with pytest.raises(Refused, match="only port 443"):
            await p.check("pypi.org", 22)
        for slow in (False, True):
            if slow:
                monkeypatch.setattr(proxy, "DNS_SECONDS", 0.1)
                p = Proxy(("gone.example",), resolve=never)
            with pytest.raises(Refused) as refused:
                await p.check("gone.example", 443)
            assert refused.value.status == "502 Bad Gateway"

    asyncio.run(check())


def test_a_command_waits_on_its_own_connections_only(loopback_counts: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command holding all its connections idle: its next one is refused at
    once, another command's goes through; and all commands have a limit too."""
    monkeypatch.setattr(proxy, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(proxy, "MAX_TOTAL", 3)

    async def go() -> None:
        p = Proxy(("pypi.org",), resolve=resolver({"pypi.org": ["192.168.0.1"]}))
        busy, other = await open_door(p), await open_door(p)
        held = [await asyncio.open_unix_connection(busy) for _ in range(2)]

        async def ask(path: str) -> bytes:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
            async with asyncio.timeout(3):
                answer = await reader.read()
            writer.close()
            return answer

        await asyncio.sleep(0.05)
        assert (await ask(busy)).startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
        assert b"not public" in await ask(other)  # through to the rules: not held up
        third = await asyncio.open_unix_connection(other)
        await asyncio.sleep(0.05)
        assert b"the proxy is busy" in await ask(other)  # 2 + 1 held, all commands' limit
        for _, writer in [*held, third]:
            writer.close()
        await close_all(p, busy)
        os.rmdir(os.path.dirname(other))

    asyncio.run(go())


def test_a_tunnel_without_a_client_hello_ends(loopback_counts: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "HELLO_SECONDS", 0.1)
    answer, received = exchange(Proxy(("pypi.org",), resolve=resolver({"pypi.org": ["127.0.0.1"]})),
                                b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", then=b"\x16\x03")
    assert answer == b"HTTP/1.1 200 Connection established\r\n\r\n" and received == [b""]


def test_the_network_configuration(tmp_path: Any) -> None:
    base = {"client_uid": os.getuid()}
    config = parse(base)
    assert config.network == DEFAULT_ALLOW and config.state_dir.endswith("/.local/state/himari-runner")
    assert config.proxy_socket("ab") == config.state_dir + "/proxy-ab.sock"
    assert parse({**base, "network": {"enabled": False}}).network is None
    assert parse({**base, "network": {"allow": []}}).network is None
    assert parse({**base, "network": {"allow": ["*.Example.com"]}}).network == ("*.example.com",)
    refused: list[dict[str, Any]] = [{"network": {"allow": ["127.0.0.1"]}}, {"network": {"enabled": "yes"}},
                                     {"network": []}, {"state_dir": "/" + "x" * 85}, {"state_dir": "relative"}]
    for bad in refused:
        with pytest.raises(ConfigError):
            parse({**base, **bad})


def test_closing_ends_open_tunnels(loopback_counts: None, caplog: pytest.LogCaptureFixture) -> None:
    """The command ending (or the runner stopping) ends its tunnels, both
    ends, though the server keeps its end open after the proxy's half-close."""
    caplog.set_level("INFO", logger="himari.runner.proxy")

    async def go() -> None:
        done = asyncio.Event()

        async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()  # never answers, never closes first
            await done.wait()  # nor after the proxy's half-close
            writer.close()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        proxy.PORT = server.sockets[0].getsockname()[1]
        p = Proxy(("pypi.org",), resolve=resolver({"pypi.org": ["127.0.0.1"]}))
        path = await open_door(p)
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(f"CONNECT pypi.org:{proxy.PORT} HTTP/1.1\r\n\r\n".encode() + hello("pypi.org"))
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        await asyncio.sleep(0.05)
        async with asyncio.timeout(3):
            await close_all(p, path)
            assert await reader.read() == b""
            while not any("pypi.org tunnel" in r.getMessage() for r in caplog.records):  # the tunnel has ended
                await asyncio.sleep(0.01)
        writer.close()
        done.set()
        server.close()

    asyncio.run(go())
