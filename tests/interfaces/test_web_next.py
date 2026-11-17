"""The Web page (V3-P102 §7.2; at / since W6, after it grew at /next/): what is served, the one CSP header, the
build's files and KaTeX, and the old /next/ addresses moved."""

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
         '<script type="module" src="/assets/index-1.js"></script>')


def built(root: Path, index: str = INDEX) -> Path:
    dist = root / "dist"
    (dist / "assets" / "deep").mkdir(parents=True)
    (dist / "index.html").write_text(index, encoding="utf-8")
    (dist / "assets" / "index-1.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "assets" / "index-1.css").write_text("body{}", encoding="utf-8")
    (dist / "assets" / "deep" / "font.woff2").write_bytes(b"\0font")
    (dist / "assets" / "logo.svg").write_text("<svg onload='x()'/>", encoding="utf-8")  # a kind never served
    (dist / "secret.txt").write_text("no", encoding="utf-8")
    katex = dist / "assets" / "katex"  # as web/public puts it in the build
    (katex / "fonts").mkdir(parents=True)
    (katex / "katex.min.js").write_text("window.katex={}", encoding="utf-8")
    (katex / "katex.min.css").write_text(".katex{}", encoding="utf-8")
    (katex / "fonts" / "KaTeX_Main-Regular.woff2").write_bytes(b"wOF2font")
    (katex / "fonts" / "KaTeX_Main-Regular.ttf").write_bytes(b"ttf")
    (katex / "LICENSE").write_text("MIT", encoding="utf-8")
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
    # any address that is not the API or a file is the same page, which reads the address itself
    pages = [client.get(path) for path in ("/", "/c/abc", "/s/5", "/settings/memory", "/trash", "/anything/else")]
    nonces = []
    for page in pages:
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
        [csp] = csp_headers(page)  # one header: two policies would both apply
        nonce = re.search(r'name="csp-nonce" content="([A-Za-z0-9_-]+)"', page.text)
        assert nonce is not None and csp == style_nonce_csp(page_csp(), nonce.group(1))
        assert "script-src" not in csp  # scripts stay 'self' (default-src)
        nonces.append(nonce.group(1))
    assert len(set(nonces)) == len(nonces) and "__HIMARI_CSP_NONCE__" not in pages[0].text
    api = client.get("/api/session")
    assert csp_headers(api) == [page_csp()] and api.headers["cache-control"] == "no-store"
    assert api.json()["authenticated"] is False
    for not_a_page in ("/api", "/api/nope", "/api/conversations/x/nope"):  # the API's misses are never the page
        missing = client.get(not_a_page)
        assert missing.status_code in (401, 404) and "text/html" not in missing.headers["content-type"], not_a_page
    assert client.post("/", headers=SEND).status_code == 405  # a page to read only


def test_only_built_assets_of_known_kinds_are_served(client: TestClient) -> None:
    script = client.get("/assets/index-1.js")
    assert script.status_code == 200 and script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == next_page.ASSET_CACHE
    assert client.get("/assets/deep/font.woff2").headers["content-type"] == "font/woff2"
    # KaTeX, where the renderer loads it on the first formula
    assert client.get("/assets/katex/katex.min.js").headers["content-type"] == "text/javascript; charset=utf-8"
    assert client.get("/assets/katex/katex.min.css").headers["content-type"] == "text/css; charset=utf-8"
    font = client.get("/assets/katex/fonts/KaTeX_Main-Regular.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2" and font.content[:4] == b"wOF2"
    for missing in ("/assets/logo.svg", "/assets/nope.js", "/assets/..%2Fsecret.txt", "/assets/..%2Findex.html",
                    "/assets/deep", "/assets/katex/LICENSE", "/assets/katex/fonts/KaTeX_Main-Regular.ttf",
                    "/assets/katex%2F..%2F..%2Fsecret.txt", "/assets/app.js"):  # the old page's files are gone
        response = client.get(missing)
        assert response.status_code == 404 and "text/html" not in response.headers["content-type"], missing


def test_the_old_next_addresses_move_to_the_same_place(client: TestClient) -> None:
    moved = {"/next": "/", "/next/": "/", "/next/s/5": "/s/5", "/next/c/abc?x=1": "/c/abc?x=1",
             "/next/settings/memory": "/settings/memory",
             "/next//evil.example/x": "/evil.example/x",  # never another site's address
             "/next/%5Cevil.example": "/evil.example"}
    for old, new in moved.items():
        response = client.get(old, follow_redirects=False)
        assert response.status_code == 308 and response.headers["location"] == new, old


def test_without_a_build_only_the_api_is_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = serve(tmp_path, monkeypatch, None)
    try:
        assert started.get("/").status_code == 404
        assert started.get("/c/abc").status_code == 404
        assert started.get("/assets/index-1.js").status_code == 404
        assert started.get("/api/session").status_code == 200
    finally:
        started.__exit__(None, None, None)


def test_a_build_without_the_nonce_placeholder_is_not_served(tmp_path: Path) -> None:
    assert next_page.load(built(tmp_path, "<!doctype html><script src=x></script>")) is None
    assert next_page.load(tmp_path / "nothing") is None


def test_the_middleware_ignores_a_nonce_of_the_wrong_shape() -> None:
    from himari.interfaces.web.security import NONCE

    assert NONCE.fullmatch("abcDEF_-0123456789") and not NONCE.fullmatch("a'; script-src *")
