"""Browser smoke test of the Web UI (opt-in evidence, not part of pytest).

Starts the real server on a temporary private data root with a scripted
provider (a file write, an ask_user question, then a final answer that
contains hostile markup), drives it with headless Chromium and checks:
sign-in (wrong and right token), new chat, the tool row, the question
panel, the answer, safe rich text (no script execution, no javascript:
link), the context size in the conversation menu, "Compact now" and its
divider, rename, reload keeping
the HttpOnly session, the narrow layout and sign-out. A second server with
a small context budget compacts a conversation and checks the timeline
divider and its summary. A third server with a small turn token budget
(V3-P62) shows the turn's usage while it works, stops it at the budget
with a Continue button, and finishes it with that button.

Requires: pip install playwright && playwright install chromium
Usage:    python scripts/ui_smoke.py [--screenshots DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
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
from himari.bootstrap.operator import OPERATOR  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, CallId, ConversationId, TextBlock, ToolCall, ToolId, TrustedScope  # noqa: E402
from himari.data.api import UserMessage  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedFinal, NormalizedToolCalls  # noqa: E402
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest  # noqa: E402
from himari.provider.scripted import Measured, ScriptedAdapter  # noqa: E402

PORT = 8799
HOSTILE = ("# Plan for Seoul\n\n**Day 1**: walk <script>window.__pwned = 1</script> "
           "[bad](javascript:window.__pwned=2) and [good](https://example.com)\n\n- palace\n- market")


def scripted() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": "plan.txt", "content": "draft"}),)),
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ASK_USER_TOOL_ID, {"question": "Which **city**?"}),)),
        NormalizedFinal((TextBlock(HOSTILE),)),
        NormalizedFinal((TextBlock("Manual summary: a trip to Seoul."),)),  # "Compact now"
    ])


OPENED: dict[str, tuple[Application, asyncio.AbstractEventLoop]] = {}  # root -> the server's application and loop


def serve(root: str, port: int = PORT, provider: ScriptedAdapter | None = None,
          context_tokens: int | None = None, turn_budget: int = 1_000_000) -> uvicorn.Server:
    config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        application = await Application.open(root, provider or scripted(), model_id="scripted-model", sandbox=False,
                                             context_tokens=context_tokens, turn_budget=turn_budget)
        OPENED[root] = (application, asyncio.get_running_loop())
        return application

    server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=port, log_level="warning"))
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
        page.click("#chat-menu-toggle")
        expect(page.locator("#chat-menu-context")).to_contain_text("of 128k tokens · summarizes at 102k")
        expect(page.locator(".context-divider")).to_have_count(0)  # nothing compacted yet
        page.click("#chat-menu-compact")
        expect(page.locator("#status-bar")).to_have_text(
            "Compacted: earlier messages now reach HIMARI as a summary", timeout=10000)
        expect(page.locator(".context-divider")).to_have_count(1, timeout=10000)
        page.locator(".context-divider summary").click()
        expect(page.locator(".context-divider-text")).to_have_text("Manual summary: a trip to Seoul.")
        page.click("#chat-menu-toggle")
        expect(page.locator("#chat-menu-context")).to_contain_text("of 128k tokens")
        page.click("#chat-menu-toggle")
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


SUMMARY = "SUMMARY of the start <b>not markup</b>"


def accept_unanswered(root: str, conversation_id: str) -> None:
    """A message the server committed but never answered (as after a restart)."""
    application, loop = OPENED[root]
    scope = TrustedScope(OPERATOR, ConversationId(conversation_id))
    message = UserMessage((TextBlock("an unanswered question"),))
    asyncio.run_coroutine_threadsafe(application.runtime.driver.accept(scope, message), loop).result(10)


def run_compaction(base: str, token: str, shots: str | None, root: str) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#home-new-chat")
        for n, text in enumerate(("가" * 550, "가" * 550, "가" * 550, "last question")):
            page.fill("#composer-text", text)
            page.press("#composer-text", "Enter")
            expect(page.locator(".item-assistant")).to_have_count(n + 1, timeout=10000)
            expect(page.locator("#topbar-state-dot")).not_to_have_class("topbar-state-dot is-active", timeout=10000)
        divider = page.locator(".context-divider")
        expect(divider).to_have_count(1, timeout=10000)
        expect(divider.locator(".context-divider-label")).to_contain_text("Earlier messages reach HIMARI as a summary")
        # It sits above the latest exchange and below what it summarizes.
        assert page.evaluate("""() => {
            const d = document.querySelector('.context-divider');
            const after = d.nextElementSibling, before = d.previousElementSibling;
            return before !== null && after !== null && Number(after.dataset.sequence) > Number(before.dataset.sequence);
        }""")
        divider.locator("summary").click()
        expect(divider.locator(".context-divider-text")).to_have_text(SUMMARY)  # shown as text, not markup
        assert divider.locator("b").count() == 0
        page.click("#chat-menu-toggle")
        expect(page.locator("#chat-menu-context")).to_contain_text("of 2.0k tokens")
        if shots:
            page.screenshot(path=os.path.join(shots, "4-compacted.png"), full_page=True)
        page.click("#chat-menu-toggle")
        # Compacting answers nothing: an unanswered message keeps its warning.
        conversation_id = page.locator(".conv-row.is-selected").get_attribute("data-id")
        assert conversation_id
        accept_unanswered(root, conversation_id)
        page.locator(".conv-row").first.click()
        expect(page.locator("#status-bar")).to_contain_text("Not answered yet")
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-compact")
        # One message, after the job: the compaction result and the warning together.
        expect(page.locator("#status-bar")).to_have_text(
            re.compile(r"^(Compacted|Nothing to compact)[^.]*\. Not answered yet"), timeout=10000)
        browser.close()
    return errors


class SlowAdapter(ScriptedAdapter):
    """Each answer takes a moment, so the page shows a turn while it works."""

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        await asyncio.sleep(1.0)
        return await super().invoke_prepared(request)


def budget_script() -> SlowAdapter:
    def write(n: int) -> Measured:
        call = ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": f"{n}.txt", "content": "x"})
        return Measured(NormalizedToolCalls((call,)), input_tokens=45_000, output_tokens=1_000)

    # Two Runs use 92k of 100k: a third (its input and the 8,192 reserve) would not fit.
    return SlowAdapter([write(0), write(1), write(2),
                        Measured(NormalizedFinal((TextBlock("All three files are written."),)), 1_000, 100)])


def run_turn_budget(base: str, token: str, shots: str | None) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#home-new-chat")
        page.fill("#composer-text", "Write three files")
        page.press("#composer-text", "Enter")
        expect(page.locator("#composer-hint")).to_have_text("HIMARI is working… · 45k in · 1.0k out / 100k",
                                                            timeout=10000)
        expect(page.locator("#stop-panel")).to_be_visible(timeout=10000)
        expect(page.locator("#status-bar")).to_contain_text(  # and why: what the next request would need
            "Stopped: this turn's token budget is used (the turn used 92000 tokens;")
        expect(page.locator("#stop-text")).to_have_text("This turn · 90k in · 2.0k out / 100k")
        expect(page.locator("#composer-hint")).to_have_text("")
        if shots:
            page.screenshot(path=os.path.join(shots, "5-budget.png"))
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "6-budget-narrow.png"))
        page.set_viewport_size({"width": 1280, "height": 860})
        page.click("#stop-continue")
        expect(page.locator(".item-assistant").last).to_contain_text("All three files are written.", timeout=10000)
        expect(page.locator("#stop-panel")).to_be_hidden()
        expect(page.locator("#status-bar")).to_have_text("")
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
        small = os.path.join(tmp, "small")
        provider = ScriptedAdapter([NormalizedFinal((TextBlock(SUMMARY),))] * 12)  # every answer and summary alike
        server = serve(small, PORT + 1, provider, context_tokens=2000)
        try:
            errors += run_compaction(f"http://127.0.0.1:{PORT + 1}",
                                     Path(small, "control", "operator.secret").read_text().strip(), args.screenshots,
                                     small)
        finally:
            server.should_exit = True
            time.sleep(0.5)
        budget = os.path.join(tmp, "budget")
        server = serve(budget, PORT + 2, budget_script(), turn_budget=100_000)
        try:
            errors += run_turn_budget(f"http://127.0.0.1:{PORT + 2}",
                                      Path(budget, "control", "operator.secret").read_text().strip(), args.screenshots)
        finally:
            server.should_exit = True
            time.sleep(0.5)
    print("UI smoke: OK" if not errors else f"UI smoke: console errors {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
