"""The machine profile's network proxy (V3-P74 §11.3) and its configuration."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from typing import Any

import pytest

from himari.runner import proxy
from himari.runner.config import ConfigError, parse
from himari.runner.proxy import DEFAULT_ALLOW, Proxy, Refused, allow_list, allowed, normalize, public, target


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


def exchange(p: Proxy, request: bytes, *, then: bytes = b"") -> tuple[bytes, list[bytes]]:
    """Send ``request`` through the proxy's socket; with ``then``, send it
    into the tunnel and read until the far end closes."""
    received: list[bytes] = []

    async def go() -> bytes:
        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.read(100))
            writer.write(b"pong")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy.PORT = server.sockets[0].getsockname()[1]
        folder = tempfile.mkdtemp(prefix="px-")
        path = os.path.join(folder, "p.sock")
        await p.start(path)
        try:
            assert os.stat(path).st_mode & 0o777 == 0o600
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
            await p.close()
            server.close()
            os.unlink(path)
            os.rmdir(folder)

    return asyncio.run(go()), received


@pytest.fixture
def loopback_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test's upstream is on 127.0.0.1: let that one address count as public."""
    monkeypatch.setattr(proxy, "public", lambda a: a == "127.0.0.1" or public(a))
    monkeypatch.setattr(proxy, "PORT", 443)


def test_a_tunnel_to_an_allowed_name(loopback_counts: None) -> None:
    resolve = resolver({"pypi.org": ["127.0.0.1"]})
    answer, received = exchange(Proxy(("pypi.org",), resolve=resolve),
                                b"CONNECT PyPI.org.:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n", then=b"ping")
    assert answer == b"HTTP/1.1 200 Connection established\r\n\r\npong" and received == [b"ping"]
    assert resolve.seen == ["pypi.org"]  # resolved once, the normalized name


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


def test_another_port_and_an_unknown_name(loopback_counts: None) -> None:
    async def check() -> None:
        p = Proxy(("pypi.org", "gone.example"), resolve=resolver({}))
        with pytest.raises(Refused, match="only port 443"):
            await p.check("pypi.org", 22)
        with pytest.raises(Refused) as refused:
            await p.check("gone.example", 443)
        assert refused.value.status == "502 Bad Gateway"

    asyncio.run(check())


def test_the_network_configuration(tmp_path: Any) -> None:
    base = {"client_uid": os.getuid()}
    config = parse(base)
    assert config.network == DEFAULT_ALLOW and config.state_dir.endswith("/.local/state/himari-runner")
    assert config.proxy_socket == config.state_dir + "/proxy.sock"
    assert parse({**base, "network": {"enabled": False}}).network is None
    assert parse({**base, "network": {"allow": []}}).network is None
    assert parse({**base, "network": {"allow": ["*.Example.com"]}}).network == ("*.example.com",)
    refused: list[dict[str, Any]] = [{"network": {"allow": ["127.0.0.1"]}}, {"network": {"enabled": "yes"}},
                                     {"network": []}, {"state_dir": "/" + "x" * 100}, {"state_dir": "relative"}]
    for bad in refused:
        with pytest.raises(ConfigError):
            parse({**base, **bad})


def test_closing_ends_open_tunnels(loopback_counts: None) -> None:
    """The runner stopping does not wait for a tunnel whose ends stay open."""
    async def go() -> None:
        async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()  # never answers, never closes first
            writer.close()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        proxy.PORT = server.sockets[0].getsockname()[1]
        folder = tempfile.mkdtemp(prefix="px-")
        path = os.path.join(folder, "p.sock")
        p = Proxy(("pypi.org",), resolve=resolver({"pypi.org": ["127.0.0.1"]}))
        await p.start(path)
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(f"CONNECT pypi.org:{proxy.PORT} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        async with asyncio.timeout(3):
            await p.close()
            assert await reader.read() == b""
        writer.close()
        server.close()
        os.unlink(path)
        os.rmdir(folder)

    asyncio.run(go())
