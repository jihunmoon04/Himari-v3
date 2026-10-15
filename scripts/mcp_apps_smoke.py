"""Browser smoke test of Tool views (MCP Apps, V3-P58) on the real Web server
(opt-in evidence, not part of pytest).

Starts the page server and the sandbox server (two ports, one signer) on a
temporary private data root, with a scripted provider and the fake MCP
server whose Tools have a view. The view is a small MCP App that records
what it receives and what it can reach. Headless Chromium drives the UI:

1. the latest call's view opens by itself; the bridge round trip works
   (ui/initialize, tool-input, tool-result, size-changed); the view is
   opaque, without storage, parent or host access, or the page's API;
   tools/call is refused (-32601);
2. a view opens while its call runs and gets the result when it is stored;
3. Stop during a write call: the outcome is unknown, so the view is closed
   with a note (never "cancelled");
4. Stop during a read call: not run, so the view gets tool-cancelled;
5. at most 3 views at once: the oldest goes back to "Open view";
6. turning the server off in the conversation closes its views.

Requires: pip install playwright (a Chromium for it, or --chrome PATH)
Usage:    python scripts/mcp_apps_smoke.py [--chrome PATH] [--screenshots DIR]
Exit status 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from mcp_fake import ECHO_SCHEMA, URL, FakeMcpServer  # noqa: E402
from playwright.sync_api import Frame, Page, expect, sync_playwright  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.interfaces.web.sandbox import SandboxSigner, create_sandbox_app  # noqa: E402
from himari.provider.api import NormalizedFinal, NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
VIEW_URI = "ui://calliope/probe"

# A minimal MCP App: logs what it receives, probes what it can reach.
VIEW_HTML = """<!DOCTYPE html><html><body style="margin:0"><pre id="log"></pre><script>
const log = (k, v) => { const d = document.createElement("div"); d.dataset.k = k;
  d.textContent = k + " " + JSON.stringify(v); document.getElementById("log").append(d); };
let next = 0; const waiting = new Map();
const request = (method, params) => { const id = ++next;
  parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*"); return new Promise((r) => waiting.set(id, r)); };
window.addEventListener("message", (e) => {
  const m = e.data;
  if (m && m.method === undefined && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); return; }
  if (!m || !m.method) return;
  if (m.method === "ui/notifications/tool-input") log("input", m.params.arguments);
  else if (m.method === "ui/notifications/tool-result") log("result", m.params.structuredContent || null);
  else if (m.method === "ui/notifications/tool-cancelled") log("cancelled", m.params.reason);
  else if (m.method === "ui/resource-teardown") parent.postMessage({ jsonrpc: "2.0", id: m.id, result: {} }, "*");
});
(async () => {
  const p = { origin: self.origin };
  try { localStorage.setItem("k", "v"); p.storage = "available"; } catch (e) { p.storage = "blocked"; }
  try { void parent.document.title; p.parent = "reachable"; } catch (e) { p.parent = "blocked"; }
  try { void top.document.title; p.top = "reachable"; } catch (e) { p.top = "blocked"; }
  try { await fetch("%(base)s/api/session"); p.api = "reachable"; } catch (e) { p.api = "blocked"; }
  log("probes", p);
  const init = await request("ui/initialize", { protocolVersion: "2026-01-26", appCapabilities: {},
                                               clientInfo: { name: "probe", version: "1" } });
  const r = init.result;
  log("init", { version: r.protocolVersion, tool: r.hostContext.toolInfo ? r.hostContext.toolInfo.tool.name : null,
                schema: Boolean(r.hostContext.toolInfo && r.hostContext.toolInfo.tool.inputSchema),
                openLinks: Boolean(r.hostCapabilities.openLinks), serverTools: Boolean(r.hostCapabilities.serverTools) });
  const call = await request("tools/call", { name: "echo", arguments: { text: "from the view" } });
  log("tools/call", call.error ? call.error.code : "answered");
  parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, "*");
  parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { width: 300, height: 321 } }, "*");
})();
</script></body></html>""" % {"base": BASE}


def tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name} with a view", "inputSchema": ECHO_SCHEMA,
            "_meta": {"ui": {"resourceUri": VIEW_URI}}}


def fake_server() -> FakeMcpServer:
    fake = FakeMcpServer([tool("echo"), tool("stamp")])
    said = lambda args: {"content": [{"type": "text", "text": f"said {args['text']}"}],  # noqa: E731
                         "structuredContent": {"said": args["text"]}}
    fake.handlers = {"echo": said, "stamp": said}
    fake.resources = {VIEW_URI: {"contents": [{"uri": VIEW_URI, "mimeType": "text/html;profile=mcp-app",
                                               "text": VIEW_HTML}]}}
    return fake


def calls(name: str, text: str) -> NormalizedToolCalls:
    return NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId(f"calliope/{name}"), {"text": text}),))


def script() -> ScriptedAdapter:
    return ScriptedAdapter([
        calls("echo", "one"), NormalizedFinal((TextBlock("first"),)),
        calls("echo", "slow"), NormalizedFinal((TextBlock("second"),)),
        calls("stamp", "write"),  # stopped while running: claimed write → unknown
        calls("echo", "read"),  # stopped while running: not claimed → cancelled
        calls("echo", "five"), NormalizedFinal((TextBlock("fifth"),)),
    ])


def serve(root: str, fake: FakeMcpServer) -> None:
    config = WebConfig("127.0.0.1", PORT, frozenset({"127.0.0.1"}), frozenset({BASE}), False,
                       root, "scripted-model", None, False)
    signer = SandboxSigner()

    async def opener() -> Application:
        return await Application.open(root, script(), model_id="scripted-model", sandbox=False,
                                      mcp_transport=fake.transport())

    servers = [uvicorn.Server(uvicorn.Config(create_app(config, opener, signer), host="127.0.0.1", port=PORT,
                                             log_level="warning")),
               uvicorn.Server(uvicorn.Config(create_sandbox_app(["127.0.0.1"], signer), host="127.0.0.1",
                                             port=config.view_port, log_level="warning"))]
    for server in servers:
        threading.Thread(target=server.run, daemon=True).start()
    secret = Path(root, "control", "operator.secret")
    for _ in range(100):
        if all(s.started for s in servers) and secret.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("servers did not start")


def inner(page: Page, index: int) -> Frame:
    """The view's own (opaque) frame inside the index-th proxy on the page."""
    proxy = page.locator(".app-view-frame").nth(index).element_handle().content_frame()
    assert proxy is not None
    for _ in range(100):
        if proxy.child_frames:
            return proxy.child_frames[0]
        time.sleep(0.05)
    raise AssertionError("the proxy framed no view")


def logged(frame: Frame) -> list[str]:
    return frame.evaluate("[...document.querySelectorAll('#log div')].map((d) => d.textContent)")


def wait_logged(frame: Frame, prefix: str, timeout: float = 10.0) -> list[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = logged(frame)
        if any(line.startswith(prefix) for line in lines):
            return lines
        time.sleep(0.1)
    raise AssertionError(f"the view never logged {prefix!r}: {logged(frame)}")


def wait_call_started(fake: FakeMcpServer, count: int, timeout: float = 10.0) -> None:
    """Until the server has received ``count`` tools/call requests: Stop must
    land while the call runs, not while its arguments are still checked."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(json.loads(r.content).get("method") == "tools/call" for r in fake.requests) >= count:
            return
        time.sleep(0.05)
    raise AssertionError("the call never reached the server")


def send(page: Page, text: str) -> None:
    page.fill("#composer-text", text)
    page.press("#composer-text", "Enter")


def run(token: str, fake: FakeMcpServer, chrome: str | None, shots: str | None) -> list[str]:
    results: list[str] = []
    errors: list[str] = []

    def check(name: str, ok: bool, seen: object = None) -> None:
        results.append(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (saw: {seen!r})"))

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#home-new-chat")

        # 1. Auto-open, bridge round trip, isolation.
        send(page, "one")
        expect(page.locator(".app-view-label")).to_have_text("App view from calliope · sandboxed", timeout=10000)
        first = inner(page, 0)
        lines = wait_logged(first, "result")
        check("1: the latest call's view opened by itself", page.locator(".app-view-frame").count() == 1)
        check("1: tool-input reached the view", 'input {"text":"one"}' in lines, lines)
        check("1: tool-result reached the view", 'result {"said":"one"}' in lines, lines)
        init = next((json.loads(line[5:]) for line in lines if line.startswith("init ")), None)
        check("1: ui/initialize answered (2026-01-26, full Tool, links yes, Tools no)",
              init == {"version": "2026-01-26", "tool": "echo", "schema": True, "openLinks": True,
                       "serverTools": False}, init)
        check("1: tools/call refused with -32601", "tools/call -32601" in lines, lines)
        probes = next((json.loads(line[7:]) for line in lines if line.startswith("probes ")), {})
        check("1: the view is opaque, without storage, parent, host or page API",
              probes == {"origin": "null", "storage": "blocked", "parent": "blocked", "top": "blocked",
                         "api": "blocked"}, probes)
        expect(page.locator(".app-view-frame").first).to_have_attribute("style", "height: 321px;", timeout=5000)
        check("1: size-changed resized the frame", True)
        if shots:
            page.screenshot(path=os.path.join(shots, "1-view.png"), full_page=True)

        # 2. Shown while running, result once stored.
        fake.call_delay = 2.0
        send(page, "slow")
        expect(page.locator(".app-view-frame")).to_have_count(2, timeout=5000)
        second = inner(page, 1)
        lines = wait_logged(second, "input")
        check("2: the view opened while its call ran (input, no result yet)",
              'input {"text":"slow"}' in lines and not any(l.startswith("result") for l in lines), lines)
        lines = wait_logged(second, "result")
        check("2: the result reached it once stored", 'result {"said":"slow"}' in lines, lines)

        # 3. Stop during a write: unknown, so closed with a note, never "cancelled".
        fake.call_delay = 3.0
        send(page, "write")
        expect(page.locator(".app-view-frame")).to_have_count(3, timeout=5000)
        third = inner(page, 2)
        wait_logged(third, "input")
        wait_call_started(fake, 3)
        page.click("#composer-cancel")
        note = page.locator(".app-view-notice", has_text="The outcome of this call is unknown.")
        expect(note).to_have_count(1, timeout=10000)
        check("3: a stopped write closed its view with 'outcome unknown'", page.locator(".app-view-frame").count() == 2)

        # 4. Stop during a read: not run, so tool-cancelled.
        page.wait_for_timeout(500)
        send(page, "read")
        expect(page.locator(".app-view-frame")).to_have_count(3, timeout=5000)
        fourth = inner(page, 2)
        wait_logged(fourth, "input")
        wait_call_started(fake, 4)
        page.click("#composer-cancel")
        lines = wait_logged(fourth, "cancelled")
        check("4: a stopped read got tool-cancelled and kept its view",
              page.locator(".app-view-frame").count() == 3 and not any(l.startswith("result") for l in lines), lines)

        # 5. At most three: the oldest goes back to "Open view".
        fake.call_delay = 0.0
        page.wait_for_timeout(500)
        send(page, "five")
        expect(page.locator(".item-assistant").last).to_have_text("HIMARIfifth", timeout=10000)
        page.wait_for_timeout(1500)  # the oldest view's teardown (at most 1 s)
        check("5: at most three views at once", page.locator(".app-view-frame").count() == 3,
              page.locator(".app-view-frame").count())
        check("5: the oldest went back to 'Open view'", page.locator(".app-view-open").count() >= 1)
        if shots:
            page.screenshot(path=os.path.join(shots, "2-three-views.png"), full_page=True)

        # 6. Turning the server off here closes its views.
        page.click("#chat-menu-toggle")
        page.uncheck("#tool-source-calliope")
        expect(page.locator(".app-view-frame")).to_have_count(0, timeout=10000)
        check("6: turning the server off closed every view",
              page.locator(".app-view-notice", has_text="This server is turned off in this conversation.").count() >= 1)
        check("no uncaught errors in the page", errors == [], errors)
        browser.close()
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", help="a Chromium (e.g. chrome-headless-shell) instead of Playwright's own")
    parser.add_argument("--screenshots", help="write screenshots into this directory")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "data")
        os.mkdir(root, 0o700)
        mcp = {"servers": {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"},
                                                              "stamp": {"effect": "scoped_write"}}, "ui": {}}}}
        Path(root, "mcp.json").write_text(json.dumps(mcp))
        os.chmod(os.path.join(root, "mcp.json"), 0o600)
        fake = fake_server()
        serve(root, fake)
        token = Path(root, "control", "operator.secret").read_text().strip()
        results = run(token, fake, args.chrome, args.screenshots)
    for line in results:
        print(line)
    failed = sum(line.startswith("FAIL") for line in results)
    print(f"mcp apps smoke: {'FAIL' if failed else 'OK'} ({len(results) - failed}/{len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
