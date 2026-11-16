"""The new Web page at /next/ beside the old one (V3-P102 §7.2, W1): what is served, and the one CSP header."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from webkit import ORIGIN, SEND  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web import next_page  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.interfaces.web.security import page_csp, style_nonce_csp  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

REAL_LOAD = next_page.load
INDEX = ('<!doctype html><meta name="csp-nonce" content="__HIMARI_CSP_NONCE__">'
         '<script type="module" src="/next/assets/index-1.js"></script>')


def built(root: Path, index: str = INDEX) -> Path:
    dist = root / "dist"
    (dist / "assets" / "deep").mkdir(parents=True)
    (dist / "index.html").write_text(index, encoding="utf-8")
    (dist / "assets" / "index-1.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "assets" / "index-1.css").write_text("body{}", encoding="utf-8")
    (dist / "assets" / "deep" / "font.woff2").write_bytes(b"\0font")
    (dist / "assets" / "logo.svg").write_text("<svg onload='x()'/>", encoding="utf-8")  # a kind never served
    (dist / "secret.txt").write_text("no", encoding="utf-8")
    return dist


def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dist: Path | None) -> TestClient:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(next_page, "load", lambda: None if dist is None else REAL_LOAD(dist))
    opened: list[Application] = []

    async def opener() -> Application:
        opened.append(await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="scripted-model",
                                             sandbox=False, cleanup_seconds=None))
        return opened[0]

    config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                       allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                       model_id="scripted-model", reasoning_effort=None, sandbox=False)
    client = TestClient(web.create_app(config, opener))
    client.__enter__()
    return client


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    started = serve(tmp_path, monkeypatch, built(tmp_path))
    yield started
    started.__exit__(None, None, None)


def csp_headers(response: object) -> list[str]:
    return response.headers.get_list("content-security-policy")  # type: ignore[attr-defined, no-any-return]


def test_the_page_has_one_csp_with_a_fresh_style_nonce(client: TestClient) -> None:
    first, second = client.get("/next/"), client.get("/next/c/abc")  # any address: the same page
    nonces = []
    for page in (first, second):
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        [csp] = csp_headers(page)  # one header: two policies would both apply
        nonce = re.search(r'name="csp-nonce" content="([A-Za-z0-9_-]+)"', page.text)
        assert nonce is not None and csp == style_nonce_csp(page_csp(), nonce.group(1))
        assert "script-src" not in csp  # scripts stay 'self' (default-src)
        nonces.append(nonce.group(1))
    assert nonces[0] != nonces[1] and "__HIMARI_CSP_NONCE__" not in first.text
    old = client.get("/")  # the old page keeps its policy, no nonce
    assert csp_headers(old) == [page_csp()]
    api = client.get("/api/session")
    assert csp_headers(api) == [page_csp()]


def test_only_built_assets_of_known_kinds_are_served(client: TestClient) -> None:
    script = client.get("/next/assets/index-1.js")
    assert script.status_code == 200 and script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == next_page.ASSET_CACHE
    assert client.get("/next/assets/deep/font.woff2").headers["content-type"] == "font/woff2"
    for missing in ("/next/assets/logo.svg", "/next/assets/nope.js", "/next/assets/..%2Fsecret.txt",
                    "/next/assets/..%2Findex.html", "/next/assets/deep"):
        assert client.get(missing).status_code == 404, missing
    assert client.get("/next", follow_redirects=False).headers["location"] == "/next/"
    assert client.post("/next/", headers=SEND).status_code == 405


def test_without_a_build_next_is_404_and_the_old_page_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = serve(tmp_path, monkeypatch, None)
    try:
        assert started.get("/next/").status_code == 404
        assert started.get("/next/assets/index-1.js").status_code == 404
        assert started.get("/").status_code == 200
    finally:
        started.__exit__(None, None, None)


def test_a_build_without_the_nonce_placeholder_is_not_served(tmp_path: Path) -> None:
    assert next_page.load(built(tmp_path, "<!doctype html><script src=x></script>")) is None
    assert next_page.load(tmp_path / "nothing") is None


def test_the_middleware_ignores_a_nonce_of_the_wrong_shape() -> None:
    from himari.interfaces.web.security import NONCE

    assert NONCE.fullmatch("abcDEF_-0123456789") and not NONCE.fullmatch("a'; script-src *")
