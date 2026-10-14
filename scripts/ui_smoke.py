"""Browser smoke test of the Web UI (opt-in evidence, not part of pytest).

Starts the real server on a temporary private data root with a scripted
provider (a file write, an ask_user question, then a final answer that
contains hostile markup), drives it with headless Chromium and checks:
sign-in (wrong and right token), new chat, the tool row, the question
panel, the answer, safe rich text (no script execution, no javascript:
link), rename, reload keeping the HttpOnly session, the narrow layout and
sign-out.

Requires: pip install playwright && playwright install chromium
Usage:    python scripts/ui_smoke.py [--screenshots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedFinal, NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

PORT = 8799
HOSTILE = ("# Plan for Seoul\n\n**Day 1**: walk <script>window.__pwned = 1</script> "
           "[bad](javascript:window.__pwned=2) and [good](https://example.com)\n\n- palace\n- market")


def scripted() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": "plan.txt", "content": "draft"}),)),
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ASK_USER_TOOL_ID, {"question": "Which **city**?"}),)),
        NormalizedFinal((TextBlock(HOSTILE),)),
    ])


def serve(root: str) -> uvicorn.Server:
    config = WebConfig("127.0.0.1", PORT, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{PORT}"}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        return await Application.open(root, scripted(), model_id="scripted-model", sandbox=False)

    server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    secret = Path(root, "control", "operator.secret")
    for _ in range(100):
        if server.started and secret.exists():
            return server
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def run(base: str, token: str, shots: str | None) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", "wrong-token")
        page.click("#login-submit")
        expect(page.locator("#status-bar")).to_have_text("That token is not valid.", timeout=5000)
        page.fill("#login-token", token)
        page.click("#login-submit")
        expect(page.locator("#home-recent-empty")).to_be_visible()
        page.click("#home-new-chat")
        page.fill("#composer-text", "Plan a trip")
        page.press("#composer-text", "Enter")
        expect(page.locator("#question-text")).to_have_text("Which **city**?", timeout=10000)
        expect(page.locator(".item-tool-row .tool-row-summary")).to_have_text("✓ inside/write_file · done")
        if shots:
            page.screenshot(path=os.path.join(shots, "1-question.png"))
        page.fill("#composer-text", "Seoul")
        page.press("#composer-text", "Enter")
        final = page.locator(".item-assistant").last
        expect(final.locator("h1")).to_have_text("Plan for Seoul", timeout=10000)
        expect(page.locator("#question-panel")).to_be_hidden()
        assert final.locator("a").count() == 1 and final.locator("a").get_attribute("href") == "https://example.com/"
        assert "<script>" in final.inner_text() and page.evaluate("window.__pwned") is None
        if shots:
            page.screenshot(path=os.path.join(shots, "2-final.png"), full_page=True)
        page.once("dialog", lambda d: d.accept("Seoul trip"))
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-rename")
        expect(page.locator(".conv-row")).to_have_text("Seoul trip")
        page.reload()
        expect(page.locator(".home-recent-title")).to_have_text("Seoul trip")
        assert page.evaluate("document.cookie") == ""  # HttpOnly session
        page.set_viewport_size({"width": 390, "height": 800})
        page.locator(".home-recent-card").click()
        expect(page.locator("#surface-chat")).to_be_visible()
        page.wait_for_timeout(400)
        if shots:
            page.screenshot(path=os.path.join(shots, "3-narrow.png"))
        page.click("#sidebar-toggle")
        page.click("#nav-sign-out")
        expect(page.locator("#surface-login")).to_be_visible()
        browser.close()
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="himari-ui-") as tmp:
        root = os.path.join(tmp, "root")
        server = serve(root)
        try:
            errors = run(f"http://127.0.0.1:{PORT}", Path(root, "control", "operator.secret").read_text().strip(),
                         args.screenshots)
        finally:
            server.should_exit = True
            time.sleep(0.5)
    print("UI smoke: OK" if not errors else f"UI smoke: console errors {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
