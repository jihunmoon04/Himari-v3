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
6. turning the server off in the conversation closes its views;
7. turning it back on offers them again ("Open view"), and one opens;
8. full screen (V3-P63): a view that declared it gets the button; a click
   covers the page, Esc, the button and Back return it inline, Forward
   brings it back, the view hears each change (host-context-changed); it
   may ask for it itself only right after the user clicked into it (not at
   start, not without a click); leaving and entering again at once, or
   going from one view to another, keeps the new full screen and its Back;
   a view that did not declare it has no button and is never switched.

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
PLAIN_URI = "ui://calliope/plain"  # the same probe, without "fullscreen" in its appCapabilities

# A minimal MCP App: logs what it receives, probes what it can reach.
VIEW_HTML = """<!DOCTYPE html><html><body style="margin:0"><button id="ask" onclick="requestMode('fullscreen')">ask</button><pre id="log"></pre><script>
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
  else if (m.method === "ui/notifications/host-context-changed")
    log("context", { mode: m.params.displayMode, dims: m.params.containerDimensions });
  else if (m.method === "ui/resource-teardown") parent.postMessage({ jsonrpc: "2.0", id: m.id, result: {} }, "*");
});
window.requestMode = async (mode) => {
  const r = await request("ui/request-display-mode", { mode });
  log("mode", r.result ? r.result.mode : r.error.code);
};
(async () => {
  const p = { origin: self.origin };
  try { localStorage.setItem("k", "v"); p.storage = "available"; } catch (e) { p.storage = "blocked"; }
  try { void parent.document.title; p.parent = "reachable"; } catch (e) { p.parent = "blocked"; }
  try { void top.document.title; p.top = "reachable"; } catch (e) { p.top = "blocked"; }
  try { await fetch("%(base)s/api/session"); p.api = "reachable"; } catch (e) { p.api = "blocked"; }
  log("probes", p);
  const init = await request("ui/initialize", { protocolVersion: "2026-01-26", appCapabilities: %(caps)s,
                                               clientInfo: { name: "probe", version: "1" } });
  const r = init.result;
  log("init", { version: r.protocolVersion, tool: r.hostContext.toolInfo ? r.hostContext.toolInfo.tool.name : null,
                schema: Boolean(r.hostContext.toolInfo && r.hostContext.toolInfo.tool.inputSchema),
                openLinks: Boolean(r.hostCapabilities.openLinks), serverTools: Boolean(r.hostCapabilities.serverTools),
                modes: r.hostContext.availableDisplayModes, mode: r.hostContext.displayMode });
  const call = await request("tools/call", { name: "echo", arguments: { text: "from the view" } });
  log("tools/call", call.error ? call.error.code : "answered");
  parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, "*");
  const auto = await request("ui/request-display-mode", { mode: "fullscreen" });  // with no click: refused
  log("auto", auto.result ? auto.result.mode : auto.error.code);
  parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { width: 300, height: 321 } }, "*");
})();
</script></body></html>"""
FULL_HTML = VIEW_HTML % {"base": BASE, "caps": '{ availableDisplayModes: ["inline", "fullscreen"] }'}
PLAIN_HTML = VIEW_HTML % {"base": BASE, "caps": "{}"}


def tool(name: str, uri: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name} with a view", "inputSchema": ECHO_SCHEMA,
            "_meta": {"ui": {"resourceUri": uri}}}


def fake_server() -> FakeMcpServer:
    fake = FakeMcpServer([tool("echo", VIEW_URI), tool("stamp", PLAIN_URI)])
    said = lambda args: {"content": [{"type": "text", "text": f"said {args['text']}"}],  # noqa: E731
                         "structuredContent": {"said": args["text"]}}
    fake.handlers = {"echo": said, "stamp": said}
    fake.resources = {uri: {"contents": [{"uri": uri, "mimeType": "text/html;profile=mcp-app", "text": html}]}
                      for uri, html in ((VIEW_URI, FULL_HTML), (PLAIN_URI, PLAIN_HTML))}
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


def contexts(frame: Frame) -> list[dict[str, Any]]:
    return [json.loads(line[8:]) for line in logged(frame) if line.startswith("context ")]


def wait_contexts(frame: Frame, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen = contexts(frame)
        if len(seen) >= count:
            return seen
        time.sleep(0.05)
    raise AssertionError(f"the view heard {contexts(frame)}, wanted {count} changes")


def full_screen(page: Page, view: Frame, check, shots: str | None) -> None:
    """8: the first view (declares full screen) through the button, Esc, Back and its own request."""
    box, button = page.locator(".app-view").first, page.locator(".app-view-full").first
    check("8: a view that declared full screen has the button", button.is_visible())
    url, entries = page.url, page.evaluate("history.length")
    button.click()
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    rect = box.bounding_box()
    seen = wait_contexts(view, 1)
    check("8: a click covers the page", rect == {"x": 0, "y": 0, "width": 1100, "height": 900}, rect)
    check("8: the view heard fullscreen with a fixed container",
          seen[-1]["mode"] == "fullscreen" and set(seen[-1]["dims"]) == {"width", "height"}
          and seen[-1]["dims"]["height"] > 800, seen)
    check("8: its label is still shown", page.locator(".app-view-label").first.is_visible())
    if shots:
        page.screenshot(path=os.path.join(shots, "3-fullscreen.png"))
    page.keyboard.press("Escape")
    expect(box).to_have_class("app-view", timeout=5000)
    seen = wait_contexts(view, 2)
    check("8: Esc returns it inline, and the view hears inline with its bound",
          seen[-1] == {"mode": "inline", "dims": {"width": seen[-1]["dims"]["width"], "maxHeight": 800}}, seen)
    expect(page.locator(".app-view-frame").first).to_have_attribute("style", "height: 321px;", timeout=5000)
    check("8: its inline height comes back", True)
    page.wait_for_timeout(300)
    check("8: leaving with Esc leaves no history entry", page.evaluate("history.length") == entries + 1
          and page.evaluate("history.state") is None, page.evaluate("[history.length, history.state]"))
    button.click()
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    page.go_back()
    expect(box).to_have_class("app-view", timeout=5000)
    check("8: Back returns it inline and stays on the page",
          page.url == url and page.locator(".app-view-frame").count() == 1, page.url)
    wait_contexts(view, 4)
    button.click()
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    button.click()  # now "Exit full screen"
    expect(box).to_have_class("app-view", timeout=5000)
    check("8: the button leaves it too", button.text_content() == "Full screen", button.text_content())
    wait_contexts(view, 6)
    page.keyboard.press("Escape")  # nothing to leave: Esc does nothing
    page.go_forward()  # back to the entry the button left
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    check("8: Forward brings it back to full screen", page.evaluate("history.state") is not None)
    page.keyboard.press("Escape")
    expect(box).to_have_class("app-view", timeout=5000)
    lines = logged(view)
    check("8: a view's request at start, with no click, is refused (answered inline)",
          'auto "inline"' in lines, lines)
    view.evaluate("requestMode('fullscreen')")
    lines = wait_logged(view, "mode")
    page.wait_for_timeout(300)
    check("8: a view's request without the user's click is refused (answered inline)",
          'mode "inline"' in lines and "is-fullscreen" not in (box.get_attribute("class") or ""), lines)
    view.click("#ask")  # the user clicks into the view, which asks
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    lines = logged(view)
    check("8: right after a click into it, the view's request is followed and answered fullscreen",
          'mode "fullscreen"' in lines, lines)
    page.keyboard.press("Escape")  # entering moved the focus to the host's button
    expect(box).to_have_class("app-view", timeout=5000)
    check("8: Esc leaves a full screen the view asked for", True)
    # Review B1: leave and enter again before the Back's popstate arrives.
    page.wait_for_timeout(300)
    button.click()
    expect(box).to_have_class("app-view is-fullscreen", timeout=5000)
    page.evaluate("(() => { const b = document.querySelector('.app-view-full'); b.click(); b.click(); })()")
    page.wait_for_timeout(800)
    check("8: leaving and entering again at once keeps full screen",
          "is-fullscreen" in (box.get_attribute("class") or ""), box.get_attribute("class"))
    state = page.evaluate("history.state")
    check("8: ... and its history entry, added once the Back landed", bool(state and state.get("himariAppView")), state)
    page.go_back()
    expect(box).to_have_class("app-view", timeout=5000)
    check("8: ... so Back still leaves it, on the page", page.url == url, page.url)


def full_screen_two(page: Page, check) -> None:
    """8: from one view's full screen straight to another's, and leave-then-enter across two views."""
    boxes, buttons = page.locator(".app-view"), page.locator(".app-view-full")
    url = page.url
    buttons.nth(0).click()
    expect(boxes.nth(0)).to_have_class("app-view is-fullscreen", timeout=5000)
    page.evaluate("document.querySelectorAll('.app-view-full')[1].click()")  # the other view's button
    page.wait_for_timeout(500)
    check("8: entering a second view returns the first inline: one at a time",
          boxes.nth(0).get_attribute("class") == "app-view"
          and boxes.nth(1).get_attribute("class") == "app-view is-fullscreen",
          [boxes.nth(0).get_attribute("class"), boxes.nth(1).get_attribute("class")])
    page.evaluate("(() => { const b = document.querySelectorAll('.app-view-full'); b[1].click(); b[0].click(); })()")
    page.wait_for_timeout(800)
    check("8: leaving one view and entering another at once keeps the new full screen",
          boxes.nth(0).get_attribute("class") == "app-view is-fullscreen"
          and boxes.nth(1).get_attribute("class") == "app-view",
          [boxes.nth(0).get_attribute("class"), boxes.nth(1).get_attribute("class")])
    page.go_back()
    expect(boxes.nth(0)).to_have_class("app-view", timeout=5000)
    check("8: ... and Back leaves it, on the page", page.url == url and page.locator(".app-view-frame").count() == 2,
          page.url)


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
        wait_logged(first, "result")
        page.wait_for_timeout(1500)  # let any later poll or lookup arrive: the result must still be there once
        lines = logged(first)
        check("1: the latest call's view opened by itself", page.locator(".app-view-frame").count() == 1)
        # V3-P68 §5.3: a call with a view keeps its row outside the folded work block, and is counted there.
        check("1: the row with the view is outside the work block",
              page.locator("#timeline > .item-tool-row .app-view-slot").count() == 1
              and page.locator(".item-work .app-view-slot").count() == 0)
        check("1: the work block counts it", page.locator(".item-work .work-count").first.inner_text().endswith(
            "1 tool call"), page.locator(".item-work .work-count").first.inner_text())
        check("1: tool-input reached the view", 'input {"text":"one"}' in lines, lines)
        check("1: tool-result reached the view exactly once", lines.count('result {"said":"one"}') == 1, lines)
        init = next((json.loads(line[5:]) for line in lines if line.startswith("init ")), None)
        check("1: ui/initialize answered (2026-01-26, full Tool, links yes, Tools no, inline of inline+fullscreen)",
              init == {"version": "2026-01-26", "tool": "echo", "schema": True, "openLinks": True,
                       "serverTools": False, "modes": ["inline", "fullscreen"], "mode": "inline"}, init)
        check("1: tools/call refused with -32601", "tools/call -32601" in lines, lines)
        probes = next((json.loads(line[7:]) for line in lines if line.startswith("probes ")), {})
        check("1: the view is opaque, without storage, parent, host or page API",
              probes == {"origin": "null", "storage": "blocked", "parent": "blocked", "top": "blocked",
                         "api": "blocked"}, probes)
        expect(page.locator(".app-view-frame").first).to_have_attribute("style", "height: 321px;", timeout=5000)
        check("1: size-changed resized the frame", True)
        if shots:
            page.screenshot(path=os.path.join(shots, "1-view.png"), full_page=True)
        full_screen(page, first, check, shots)

        # 2. Shown while running, result once stored.
        fake.call_delay = 2.0
        send(page, "slow")
        expect(page.locator(".app-view-frame")).to_have_count(2, timeout=5000)
        second = inner(page, 1)
        lines = wait_logged(second, "input")
        check("2: the view opened while its call ran (input, no result yet)",
              'input {"text":"slow"}' in lines and not any(l.startswith("result") for l in lines), lines)
        wait_logged(second, "result")
        page.wait_for_timeout(1500)
        lines = logged(second)
        check("2: the result reached it once stored, exactly once", lines.count('result {"said":"slow"}') == 1, lines)
        full_screen_two(page, check)

        # 3. Stop during a write: unknown, so closed with a note, never "cancelled".
        fake.call_delay = 3.0
        send(page, "write")
        expect(page.locator(".app-view-frame")).to_have_count(3, timeout=5000)
        third = inner(page, 2)
        wait_logged(third, "input")
        third.evaluate("requestMode('fullscreen')")
        lines = wait_logged(third, "mode")
        check("8: a view that did not declare full screen has no button and stays inline when it asks",
              not page.locator(".app-view-full").nth(2).is_visible() and "mode \"inline\"" in lines
              and page.locator(".app-view.is-fullscreen").count() == 0, lines)
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
        wait_logged(fourth, "cancelled")
        page.wait_for_timeout(1500)
        lines = logged(fourth)
        check("4: a stopped read got tool-cancelled once and kept its view",
              page.locator(".app-view-frame").count() == 3 and sum(l.startswith("cancelled") for l in lines) == 1
              and not any(l.startswith("result") for l in lines), lines)

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

        # 7. Back on: offered again, and a view opens.
        page.check("#tool-source-calliope")
        expect(page.locator(".app-view-open")).not_to_have_count(0, timeout=5000)
        check("7: turning it back on offered the views again",
              page.locator(".app-view-notice", has_text="This server is turned off in this conversation.").count() == 0)
        page.locator(".app-view-open").last.click()
        expect(page.locator(".app-view-frame")).to_have_count(1, timeout=10000)
        lines = wait_logged(inner(page, 0), "result")
        check("7: and a view opens again with its result", any(line.startswith("result ") for line in lines), lines)
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
