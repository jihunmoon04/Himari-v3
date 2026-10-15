"""Browser probe of the MCP Apps sandbox design (V3-P58, docs/mcp-apps-v0.1.md §3.6).

Opt-in evidence, not part of pytest. Serves a main origin and a sandbox origin
on two loopback ports, shaped like the design: the main page with HIMARI's CSP
plus ``frame-src <sandbox>`` and an ``HttpOnly; SameSite=Strict`` cookie; the
proxy with the view's CSP as its response header; the view as an opaque
``srcdoc`` iframe (``sandbox="allow-scripts"``). Headless Chromium loads the
main page; the view reports what it could do, and the main origin logs every
request that reaches it.

Two runs:
- strict: the spec's CSP template with no declared domains. Everything the
  design promises must hold, and nothing may reach the main origin.
- control: ``connect-src`` also allows the main origin. The proxy's POST must
  arrive *with* the cookie (ports do not isolate cookies) and be refused by an
  OriginGuard-like check; the view's POST must arrive as Origin ``null``
  without the cookie.

Usage: python -I scripts/mcp_apps_probe/run.py [--chrome PATH]
Default PATH: Playwright's chrome-headless-shell under ~/.cache/ms-playwright.
Missing system libraries can be supplied with LD_LIBRARY_PATH.
Exit status 1 if any check fails.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
A, B = 18765, 18766
MAIN, SANDBOX = f"http://127.0.0.1:{A}", f"http://127.0.0.1:{B}"
MAIN_CSP = (f"default-src 'self'; frame-src {SANDBOX}; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'")
VIEW_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'{extra}; img-src 'self' data:; font-src 'self'; media-src 'self' data:; "
            f"frame-src 'none'; object-src 'none'; base-uri 'self'; frame-ancestors {MAIN}")


class State:
    log: list[dict[str, object]] = []
    view_csp = VIEW_CSP.format(extra="")


class Handler(BaseHTTPRequestHandler):
    name = ""

    def log_message(self, *args: object) -> None:
        pass

    def send(self, status: int, body: bytes, kind: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("cache-control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def record(self, status: int) -> None:
        State.log.append({"at": self.name, "method": self.command, "path": self.path, "status": status,
                          "origin": self.headers.get("origin"),
                          "cookie": "himari_session" in (self.headers.get("cookie") or "")})


class Main(Handler):
    name = "main"

    def do_GET(self) -> None:
        if self.path == "/":
            self.send(200, (HERE / "main.html").read_bytes(), "text/html; charset=utf-8",
                      {"content-security-policy": MAIN_CSP, "x-frame-options": "DENY",
                       "referrer-policy": "no-referrer",
                       "set-cookie": "himari_session=s3cret; HttpOnly; SameSite=Strict; Path=/"})
        elif self.path == "/main.js":
            self.send(200, (HERE / "main.js").read_bytes(), "text/javascript")
        else:
            self.record(200)
            has_cookie = "himari_session" in (self.headers.get("cookie") or "")
            self.send(200, b"SECRET" if has_cookie else b"nocookie", "text/plain")

    def do_POST(self) -> None:
        status = 204 if self.headers.get("origin") == MAIN else 403  # like OriginGuard
        self.record(status)
        self.send(status, b"", "text/plain")


class Sandbox(Handler):
    name = "sandbox"

    def do_GET(self) -> None:
        if self.path.startswith("/sandbox.js"):
            self.send(200, (HERE / "sandbox.js").read_bytes(), "text/javascript")
        elif self.path.startswith("/sandbox"):
            page = (HERE / "sandbox.html").read_text().replace("{HOSTS}", MAIN)
            self.send(200, page.encode(), "text/html; charset=utf-8",
                      {"content-security-policy": State.view_csp, "referrer-policy": "no-referrer",
                       "x-content-type-options": "nosniff"})
        else:
            self.record(200)
            self.send(200, b"<p>blank</p>", "text/html")


def default_chrome() -> str | None:
    found = sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return str(found[-1]) if found else None


def run_browser(chrome: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as profile:
        dom = subprocess.run(
            [chrome, "--no-sandbox", "--headless", f"--user-data-dir={profile}", "--virtual-time-budget=10000",
             "--dump-dom", MAIN + "/"],
            capture_output=True, text=True, timeout=60, check=False).stdout
    match = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    if not match or match.group(1) == "pending":
        return {}
    result: dict[str, object] = json.loads(html.unescape(match.group(1)))
    return result


def probe(chrome: str, loose: bool) -> tuple[dict[str, object], list[dict[str, object]]]:
    State.log = []
    State.view_csp = VIEW_CSP.format(extra=f" {MAIN}" if loose else "")
    servers = [ThreadingHTTPServer(("127.0.0.1", A), Main), ThreadingHTTPServer(("127.0.0.1", B), Sandbox)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return run_browser(chrome), list(State.log)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def check(results: list[tuple[str, bool, object]], name: str, ok: bool, seen: object) -> None:
    results.append((name, ok, seen))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default=os.environ.get("HIMARI_PROBE_CHROME") or default_chrome())
    chrome = parser.parse_args(argv).chrome
    if not chrome:
        print("no chrome-headless-shell found; pass --chrome")
        return 1
    print(subprocess.run([chrome, "--version"], capture_output=True, text=True, check=False).stdout.strip())
    results: list[tuple[str, bool, object]] = []

    out, log = probe(chrome, loose=False)
    view = out.get("view") if isinstance(out.get("view"), dict) else {}
    assert isinstance(view, dict)
    blocked = lambda key: str(view.get(key, "")).startswith("blocked:")  # noqa: E731
    violations = [str(v) for v in view.get("violations", [])] if isinstance(view.get("violations"), list) else []
    check(results, "strict: proxy handshake and ui/initialize round trip", view.get("initialize") == "2026-01-26",
          out.get("events"))
    check(results, "strict: srcdoc view runs under the proxy's frame-src 'none' / frame-ancestors",
          view.get("js") is True, view.get("js"))
    check(results, "strict: tool-input and tool-result reach the view",
          view.get("toolInput") == {"city": "Seoul"} and view.get("toolResult") == {"temp": 21},
          [view.get("toolInput"), view.get("toolResult")])
    check(results, "strict: the view is opaque", view.get("origin") == "null", view.get("origin"))
    for key in ("hostDom", "proxyDom", "storage", "cookie", "topNav"):
        check(results, f"strict: view {key} refused", blocked(key), view.get(key))
    check(results, "strict: view popup refused", view.get("popup") == "null", view.get("popup"))
    check(results, "strict: view inherits the CSP (connect-src and frame-src violations)",
          any(v.startswith("connect-src") for v in violations) and any(v.startswith("frame-src") for v in violations),
          violations)
    check(results, "strict: nothing reaches the main origin", log == [], log)

    out, log = probe(chrome, loose=True)
    from_proxy = [e for e in log if e["method"] == "POST" and "from=proxy" in str(e["path"])]
    from_view = [e for e in log if e["method"] == "POST" and "from=view" in str(e["path"])]
    check(results, "control: the proxy's POST carries the SameSite=Strict cookie and is refused (403)",
          len(from_proxy) == 1 and from_proxy[0]["cookie"] is True and from_proxy[0]["origin"] == SANDBOX
          and from_proxy[0]["status"] == 403, from_proxy)
    check(results, "control: the view's POST is Origin null without the cookie",
          len(from_view) == 1 and from_view[0]["cookie"] is False and from_view[0]["origin"] == "null", from_view)

    for name, ok, seen in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (saw: {seen!r})"))
    failed = sum(not ok for _, ok, _ in results)
    print(f"mcp apps probe: {'FAIL' if failed else 'OK'} ({len(results) - failed}/{len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
