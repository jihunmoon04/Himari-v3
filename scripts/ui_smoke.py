"""Browser smoke test of the Web UI (opt-in evidence, not part of pytest).

Starts the real server on a temporary private data root with a scripted
provider (a file write, an ask_user question, then a final answer that
contains hostile markup), drives it with headless Chromium and checks:
sign-in (wrong and right token), new chat, the tool row, the question
panel, the answer, safe rich text (no script execution, no javascript:
link), formulas drawn by KaTeX (prices and a broken formula left as
written), the context size in the conversation menu, "Compact now" and its
divider, rename, reload keeping
the HttpOnly session, the narrow layout and sign-out. A second server with
a small context budget compacts a conversation and checks the timeline
divider and its summary. A third server with a small turn token budget
(V3-P62) shows the turn's usage while it works, stops it at the budget
with a Continue button, and finishes it with that button. A fourth
(V3-P65) deletes a conversation: the confirmation (what stays, the work it
stops; Cancel and Escape change nothing), the move to the trash, restore,
delete for good, and a page whose conversation was trashed elsewhere. A
fifth (V3-P66) attaches files: chips with their state, removing one, a
message of files, downloading an attachment and a file the model gave,
dropping a file, an upload that ends after the user left, and the narrow
layout. A sixth (V3-P67) shows images: the thumbnail of an attached photo
(its model copy, loaded from the preview route), the images a tool
returned, downloading the original by pressing a thumbnail, an HTML file
named .png that stays a plain file chip, and the narrow layout. A seventh
(V3-P68) folds tool steps into work blocks: the latest progress line while
the turn runs, a presented file outside the block, a question's lead-in,
calls without a result after a pause (never "not run": a recovery or an
omitted result looks the same), "Activity · N steps" once answered, the same after
a reload, and a block split at the compaction boundary. An eighth (V3-P69)
edits the user's instructions: the count and limit, a save that the next
message's request carries, a save refused after a change made elsewhere
(the text kept; load theirs or save mine), leaving with unsaved changes,
and the narrow layout. A ninth (V3-P71) remembers: the Remembered and
Updated lines outside the work block, an Undo refused once the note changed,
an Undo that puts the note back (and the next request carries it), the
lines after a reload, the memory screen (add, an edit refused after a change
made elsewhere and saved again, delete, the source link), the delete
confirmations' note count, a save whose answer was lost (recognized, never
saved twice, never mistaken for a note that said the same), the off notice and
what a save says then, leaving with an unsaved edit, a delete for good taking the
note, and the narrow layout.

Requires: pip install playwright && playwright install chromium
Usage:    python scripts/ui_smoke.py [--screenshots DIR] [--only main,compaction,...]
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
from typing import Any

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
           "[bad](javascript:window.__pwned=2) and [good](https://example.com)\n\n- palace\n- market\n\n"
           "Area $\\pi r_1^2$ with \\(x_1 + y_1\\), tickets $5 and $10, and\n$$\n\\frac{a}{b} = \\sum_{i=1}^{n} x_i\n$$\n"
           "and $\\frac{a$ left as written. Too wide for a phone: "
           "$\\left(\\sum_{i=1}^{n} x_i y_i + \\sum_{j=1}^{m} z_j w_j + \\sum_{k=1}^{p} u_k v_k + \\sum_{l=1}^{q} s_l t_l\\right)^2$.")


def scripted() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": "plan.txt", "content": "draft"}),)),
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ASK_USER_TOOL_ID, {"question": "Which **city**?"}),)),
        NormalizedFinal((TextBlock(HOSTILE),)),
        NormalizedFinal((TextBlock("Manual summary: a trip to Seoul."),)),  # "Compact now"
    ])


OPENED: dict[str, tuple[Application, asyncio.AbstractEventLoop]] = {}  # root -> the server's application and loop


def serve(root: str, port: int = PORT, provider: ScriptedAdapter | None = None,
          context_tokens: int | None = None, turn_budget: int = 3_000_000, memory: bool = True) -> uvicorn.Server:
    config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        application = await Application.open(root, provider or scripted(), model_id="scripted-model", sandbox=False,
                                             context_tokens=context_tokens, turn_budget=turn_budget, memory=memory)
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


def new_chat(page: Any, button: str = "#home-new-chat") -> None:
    """Start a conversation and wait until it is open: the page focuses its
    composer last. A message sent while it is still being created is not
    sent (the draft stays in the box), which is how a fast test lost one."""
    page.click(button)
    expect(page.locator("#composer-text")).to_be_focused(timeout=10000)


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
        # Formulas: KaTeX (loaded with the first one) draws four, the prices stay text.
        expect(final.locator(".rich-math .katex")).to_have_count(4, timeout=10000)
        expect(final.locator(".rich-math.is-wide")).to_have_count(0)
        expect(final.locator("div.rich-math-display .katex-display")).to_have_count(1)
        expect(final.locator(".rich-math.is-refused")).to_have_text("$\\frac{a$")
        assert "tickets $5 and $10, and" in final.inner_text()
        assert page.evaluate("document.fonts.ready.then(() => document.fonts.check('16px KaTeX_Main'))")
        assert final.locator("a").count() == 1
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
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-rename")
        page.fill("#rename-name", "Seoul trip")
        page.get_by_role("button", name="Save name", exact=True).click()
        expect(page.locator(".conv-row")).to_have_text("Seoul trip")
        page.reload()
        expect(page.locator(".home-recent-title")).to_have_text("Seoul trip")
        assert page.evaluate("document.cookie") == ""  # HttpOnly session
        page.set_viewport_size({"width": 390, "height": 800})
        page.locator(".home-recent-card").click()
        expect(page.locator("#surface-chat")).to_be_visible()
        # The formula too wide for the phone gets its own line and scrolls;
        # the conversation itself does not scroll sideways.
        wide = page.locator(".item-assistant .rich-math.is-wide")
        expect(wide).to_have_count(1, timeout=10000)
        assert wide.evaluate("n => n.scrollWidth > n.clientWidth")
        assert page.evaluate("(t => t.scrollWidth <= t.clientWidth)(document.getElementById('timeline'))")
        page.set_viewport_size({"width": 1280, "height": 860})
        expect(wide).to_have_count(0)
        page.set_viewport_size({"width": 390, "height": 800})
        expect(wide).to_have_count(1)
        page.wait_for_timeout(400)
        if shots:
            page.screenshot(path=os.path.join(shots, "3-narrow.png"))
        page.click("#sidebar-toggle")
        page.click("#nav-appearance")
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
        new_chat(page)
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
        expect(page.locator("#chat-menu-context")).to_contain_text("of 2.8k tokens")
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
        new_chat(page)
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


def run_trash(base: str, token: str, shots: str | None, root: str) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "404" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.fill("#composer-text", "hello")
        page.press("#composer-text", "Enter")
        expect(page.locator("#composer-hint")).to_contain_text("HIMARI is working", timeout=5000)
        dialog = page.locator("#confirm-dialog")
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("The work under way is stopped first.")
        page.click("#confirm-cancel")  # nothing changes
        expect(dialog).to_be_hidden()
        expect(page.locator(".item-assistant").last).to_contain_text("first answer", timeout=10000)

        held: list[Any] = []  # a notice that comes back after the user left the conversation asks nothing
        page.route("**/delete-notice", lambda route: held.append(route))
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        while not held:
            page.wait_for_timeout(50)
        page.click("#nav-home")
        expect(page.locator("#surface-home")).to_be_visible()
        held[0].continue_()
        page.wait_for_timeout(500)
        expect(dialog).to_be_hidden()
        page.unroute("**/delete-notice")
        page.locator("#conversation-list .conv-row").click()
        expect(page.locator(".item-assistant").last).to_contain_text("first answer")

        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        expect(page.locator("#confirm-title")).to_have_text("Delete “hello”?")
        expect(dialog).to_contain_text("deleted for good after 30 days")
        expect(dialog).to_contain_text("Data stored by external services is not deleted.")
        expect(dialog).not_to_contain_text("stopped first")
        assert page.evaluate("document.activeElement.id") == "confirm-cancel"  # the safe choice has the focus
        if shots:
            page.screenshot(path=os.path.join(shots, "7-delete-confirm.png"))
        page.click("#confirm-ok")
        expect(page.locator("#surface-home")).to_be_visible(timeout=10000)
        expect(page.locator("#status-bar")).to_contain_text("Moved “hello” to the trash.")
        expect(page.locator("#conversation-list .conv-row")).to_have_count(0)

        page.click("#nav-trash")
        row = page.locator("#trash-list .trash-row")
        expect(row).to_have_count(1)
        expect(row.locator(".row-title")).to_have_text("hello")
        expect(row.locator(".row-meta")).to_contain_text("deleted for good on")
        expect(page.locator("#trash-hint")).to_contain_text("deleted for good 30 days after")
        if shots:
            page.screenshot(path=os.path.join(shots, "8-trash.png"))
        row.get_by_text("Restore").click()
        expect(page.locator("#status-bar")).to_have_text("Restored “hello”.")
        expect(page.locator("#trash-empty")).to_be_visible()
        expect(page.locator("#conversation-list .conv-row")).to_have_count(1)

        page.locator("#conversation-list .conv-row").click()
        expect(page.locator(".item-assistant").last).to_contain_text("first answer")  # all of it came back
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        page.click("#confirm-ok")
        expect(page.locator("#surface-home")).to_be_visible(timeout=10000)
        page.click("#nav-trash")
        expect(row).to_have_count(1)
        row.get_by_text("Delete now").click()
        expect(page.locator("#confirm-title")).to_have_text("Delete “hello” for good?")
        page.keyboard.press("Escape")  # nothing changes
        expect(dialog).to_be_hidden()
        expect(row).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "9-trash-narrow.png"))
        page.set_viewport_size({"width": 1280, "height": 860})
        row.get_by_text("Delete now").click()
        page.click("#confirm-ok")
        expect(page.locator("#status-bar")).to_have_text("Deleted “hello” for good.")
        expect(page.locator("#trash-empty")).to_be_visible()

        # Trashed elsewhere while this page follows its work: it says so and goes home.
        new_chat(page, "#nav-new-chat")
        page.fill("#composer-text", "second")
        page.press("#composer-text", "Enter")
        expect(page.locator("#composer-hint")).to_contain_text("HIMARI is working", timeout=5000)
        conversation_id = page.locator(".conv-row.is-selected").get_attribute("data-id")
        assert conversation_id
        application, loop = OPENED[root]
        scope = TrustedScope(OPERATOR, ConversationId(conversation_id))
        asyncio.run_coroutine_threadsafe(application.trash(scope), loop).result(40)
        expect(page.locator("#status-bar")).to_have_text(
            "That conversation is no longer here; it may have been moved to the trash.", timeout=10000)
        expect(page.locator("#surface-home")).to_be_visible()
        browser.close()
    return errors


def run_files(base: str, token: str, shots: str | None, tmp: str) -> list[str]:
    errors: list[str] = []
    a_txt, b_html = Path(tmp, "a.txt"), Path(tmp, "b.html")
    a_txt.write_text("hello")
    b_html.write_text("<script>window.__pwned = 3</script>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, accept_downloads=True)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.set_input_files("#composer-files", [str(a_txt), str(b_html)])
        chips = page.locator("#attach-list .attach-chip")
        expect(chips).to_have_count(2)
        expect(chips.nth(0)).to_have_text(re.compile(r"a\.txt · 5 bytes"), timeout=10000)
        expect(chips.nth(1)).to_contain_text("b.html", timeout=10000)
        expect(page.locator("#status-bar")).to_contain_text("Commands cannot run on this server")
        chips.nth(1).get_by_role("button").click()  # removed from this message only
        expect(chips).to_have_count(1)
        if shots:
            page.screenshot(path=os.path.join(shots, "10-attach.png"))
        page.press("#composer-text", "Enter")  # files alone make a message
        user = page.locator(".item-user").last
        expect(user.locator(".file-chip .file-name")).to_have_text("a.txt", timeout=10000)
        expect(user.locator(".file-chip .file-meta")).to_have_text("5 bytes · Download")
        expect(user.locator(".file-chip .file-type")).to_have_text("TXT")
        expect(page.locator("#attach-list")).to_be_hidden()
        expect(page.locator(".item-assistant").last).to_contain_text("got the files", timeout=10000)
        with page.expect_download() as done:
            user.locator(".file-chip").click()
        assert Path(done.value.path()).read_text() == "hello", "the attachment downloads as sent"
        assert done.value.suggested_filename == "a.txt"

        page.fill("#composer-text", "give it back")
        page.press("#composer-text", "Enter")
        card = page.locator(".item-file").last
        expect(card).to_contain_text("HIMARI gave you a file", timeout=10000)
        expect(card.locator(".file-card .file-name")).to_have_text("a.txt")
        expect(card.locator(".file-card .file-meta")).to_have_text("5 bytes · Download")
        with page.expect_download() as given:
            card.locator(".file-card").click()
        assert Path(given.value.path()).read_text() == "hello"
        assert page.evaluate("window.__pwned === undefined")

        page.evaluate("""() => {
          const data = new DataTransfer();
          data.items.add(new File(["dropped"], "d.txt", { type: "text/plain" }));
          const zone = document.getElementById("surface-chat");
          zone.dispatchEvent(new DragEvent("dragover", { dataTransfer: data, bubbles: true, cancelable: true }));
          zone.dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
        }""")
        expect(chips).to_have_count(1)
        expect(chips.nth(0)).to_have_text(re.compile(r"d\.txt · 7 bytes"), timeout=10000)
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "11-files-narrow.png"))
        page.set_viewport_size({"width": 1280, "height": 860})

        held: list[Any] = []  # an upload that ends after the user left attaches nothing
        page.route("**/uploads?*", lambda route: held.append(route))
        page.set_input_files("#composer-files", [str(a_txt)])
        while not held:
            page.wait_for_timeout(50)
        conversation_id = page.locator(".conv-row.is-selected").get_attribute("data-id")
        page.click("#nav-home")
        expect(page.locator("#surface-home")).to_be_visible()
        held[0].continue_()
        page.wait_for_timeout(500)
        page.unroute("**/uploads?*")
        page.locator(f'#conversation-list .conv-row[data-id="{conversation_id}"]').click()
        expect(page.locator(".item-file").last).to_be_visible(timeout=10000)
        expect(page.locator("#attach-list")).to_be_hidden()
        browser.close()
    return errors


class InProcessViews:
    """Model copies made here with Pillow: the smoke checks the pages, not
    the image sandbox (that is tests/sandbox_host's job)."""

    available = True

    async def make(self, original: bytes) -> Any:
        import io

        from PIL import Image

        from himari.data.api import PreparedView

        with Image.open(io.BytesIO(original)) as opened:
            image = opened.convert("RGB")
        image.thumbnail((1568, 1568))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return PreparedView(out.getvalue(), "image/jpeg", image.width, image.height)


class SeeingAdapter(ScriptedAdapter):
    """A scripted model that takes images (V3-P67 §7.1)."""

    def describe_capabilities(self, model_id: str) -> Any:
        from himari.provider.api import ProviderCapabilities

        return ProviderCapabilities(model_id=model_id, parallel_tool_calls=True, image_input=True)


def run_images(base: str, token: str, shots: str | None, tmp: str) -> list[str]:
    from PIL import Image

    errors: list[str] = []
    photo, fake = Path(tmp, "photo.png"), Path(tmp, "fake.png")
    Image.new("RGB", (800, 600), (30, 110, 200)).save(photo)
    fake.write_text("<html><script>window.__pwned = 4</script></html>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, accept_downloads=True)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.set_input_files("#composer-files", [str(photo)])
        expect(page.locator("#attach-list .attach-chip").nth(0)).to_have_text(  # uploaded: its size is shown
            re.compile(r"photo\.png · [0-9.]+ KB"), timeout=10000)
        expect(page.locator("#status-bar")).not_to_contain_text("Commands cannot run")  # an image needs none
        page.fill("#composer-text", "what is this?")
        page.press("#composer-text", "Enter")
        user = page.locator(".item-user").last
        thumb = user.locator(".file-thumb img")
        expect(thumb).to_be_visible(timeout=10000)
        page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=thumb.element_handle())
        assert thumb.evaluate("img => img.naturalWidth") == 800, "the copy of an 800 px photo keeps its size"
        assert "800×600" in (user.locator(".file-thumb").get_attribute("title") or "")
        assert (thumb.bounding_box() or {}).get("height", 999) <= 240.5, "a thumbnail is at most 240 px high"
        expect(page.locator(".item-assistant").last).to_contain_text("a blue photo", timeout=10000)
        page.wait_for_timeout(300)
        assert page.evaluate("""() => { const t = document.getElementById("timeline");
          return t.scrollHeight - t.scrollTop - t.clientHeight < 80; }"""), "the answer below a photo is in view"
        user.locator(".file-preview-button").click()
        expect(page.locator("#image-dialog")).to_be_visible()
        expect(page.locator("#image-dialog-image")).to_have_attribute("alt", "photo.png")
        page.click("#image-dialog-close")
        with page.expect_download() as done:
            user.locator(".file-thumb-download").click()
        assert Path(done.value.path()).read_bytes() == photo.read_bytes(), "pressed, the original downloads"

        page.fill("#composer-text", "look at it with the tool")
        page.press("#composer-text", "Enter")
        returned = page.locator(".item-tool-images").last
        expect(returned).to_contain_text("Image from the tool", timeout=10000)
        returned_thumb = returned.locator(".file-thumb img")
        page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=returned_thumb.element_handle())
        expect(page.locator(".item-assistant").last).to_contain_text("still blue", timeout=10000)
        if shots:
            page.screenshot(path=os.path.join(shots, "12-images.png"))

        page.set_input_files("#composer-files", [str(fake)])
        expect(page.locator("#attach-list .attach-chip").nth(0)).to_have_text(
            re.compile(r"fake\.png · 48 bytes"), timeout=10000)
        page.press("#composer-text", "Enter")
        user = page.locator(".item-user").last
        expect(user.locator(".file-chip")).to_contain_text("fake.png", timeout=10000)
        expect(user.locator(".file-thumb")).to_have_count(0)  # bytes decide, not the name or the type
        expect(page.locator(".item-assistant").last).to_contain_text("not an image", timeout=10000)
        assert page.evaluate("window.__pwned === undefined")

        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "13-images-narrow.png"))
        browser.close()
    return errors


class SlowerAdapter(ScriptedAdapter):
    """Long enough between steps that a 1 s poll sees each one live."""

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        await asyncio.sleep(2.5)
        return await super().invoke_prepared(request)


def work_script() -> SlowerAdapter:
    def step(commentary: str | None, *calls: tuple[str, dict[str, Any]]) -> NormalizedToolCalls:
        return NormalizedToolCalls(tuple(ToolCall(CallId(f"c{n}"), n, ToolId(tool), arguments)
                                         for n, (tool, arguments) in enumerate(calls)),
                                   (TextBlock(commentary),) if commentary else ())

    return SlowerAdapter([
        step("Looking through the code", ("inside/write_file", {"path": "a.txt", "content": "a"})),
        step(None, ("inside/write_file", {"path": "b.txt", "content": "b"})),
        step("Implementing\nthe parser", ("inside/present_file", {"path": "a.txt"})),
        step("I found two configs.", (ASK_USER_TOOL_ID, {"question": "Which config?"})),
        step("Testing", ("inside/write_file", {"path": "c.txt", "content": "c"}),
             ("inside/write_file", {"path": "d.txt", "content": "d"})),
        NormalizedFinal((TextBlock("All done."),)),
    ])


def run_work_progress(base: str, token: str, shots: str | None) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.fill("#composer-text", "Build it")
        page.press("#composer-text", "Enter")
        blocks = page.locator("#timeline > .item-work")
        first = blocks.first
        # While it runs: one dim line, the latest progress, in a closed block.
        expect(first.locator(".work-line")).to_have_text("Looking through the code", timeout=10000)
        expect(first.locator(".work-dot")).to_have_count(1)
        expect(first.locator(".work-line")).to_have_text("Implementing", timeout=10000)  # its first line only
        expect(first.locator(".work-count")).to_have_text(" · 3 steps")
        assert first.evaluate("b => !b.open")
        if shots:
            page.screenshot(path=os.path.join(shots, "13-work-live.png"))
        expect(page.locator("#timeline > .item-file")).to_have_count(1)  # the presented file stays outside
        expect(first.locator(".item-file")).to_have_count(0)
        # The question ends the block; the text that came with it is its lead-in.
        expect(page.locator("#question-text")).to_have_text("Which config?", timeout=10000)
        expect(page.locator(".item-assistant").filter(has_text="I found two configs.")).to_have_count(1)
        expect(first.locator(".work-count")).to_have_text("Activity · 3 steps")
        expect(first.locator(".work-dot")).to_have_count(0)
        expect(blocks).to_have_count(1)
        # Answer, then pause while the model answers: its two calls do not run.
        page.fill("#composer-text", "the first")
        page.press("#composer-text", "Enter")
        expect(page.locator("#composer-pause")).to_be_visible(timeout=5000)
        page.click("#composer-pause")
        expect(page.locator("#stop-panel")).to_be_visible(timeout=10000)
        expect(blocks).to_have_count(2)
        expect(blocks.last.locator(".work-count")).to_have_text("2 steps · 2 results unavailable")
        expect(blocks.last.locator(".work-line")).to_have_count(0)
        expect(blocks.last.locator(".tool-row-summary").first).to_have_text("… inside/write_file · no result")
        page.click("#stop-continue")
        expect(page.locator(".item-assistant").last).to_contain_text("All done.", timeout=15000)
        expect(blocks.last.locator(".work-count")).to_have_text("Activity · 2 steps")
        expect(page.locator(".work-dot")).to_have_count(0)
        if shots:
            page.screenshot(path=os.path.join(shots, "14-work-done.png"))
        # The same after a reload; opened, a block shows its steps.
        page.reload()
        page.locator(".conv-row").first.click()
        expect(blocks).to_have_count(2, timeout=10000)
        assert [blocks.nth(n).locator(".work-count").inner_text() for n in range(2)] == ["Activity · 3 steps", "Activity · 2 steps"]
        assert page.evaluate("[...document.querySelectorAll('.item-work')].every(b => !b.open)")
        first.locator(".work-summary").click()
        expect(first.locator(".work-progress")).to_have_count(2)
        expect(first.locator(".item-tool-row")).to_have_count(3)
        expect(first.locator(".tool-row-summary").first).to_have_text("✓ inside/write_file · done")
        # V3-P68 §5.6: a compaction boundary inside a block splits it, the divider between.
        covered = int(first.locator(".item-tool-row").nth(1).get_attribute("data-sequence") or 0) - 1

        def summarized(route: Any) -> None:
            body = route.fetch().json()
            body.update(covered_through_sequence=covered, summary="Earlier work, summarized.", compactions=1)
            route.fulfill(json=body)

        page.route("**/context", summarized)
        page.locator(".conv-row").first.click()
        expect(blocks).to_have_count(3, timeout=10000)
        expect(page.locator(".context-divider")).to_have_count(1)
        assert page.evaluate("""() => {
            const d = document.querySelector('.context-divider');
            return d.previousElementSibling.matches('.item-work') && d.nextElementSibling.matches('.item-work');
        }""")
        assert [blocks.nth(n).locator(".work-count").inner_text() for n in range(3)] == [
            "Activity · 1 step", "Activity · 2 steps", "Activity · 2 steps"]
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "15-work-narrow.png"))
        page.set_viewport_size({"width": 1280, "height": 860})
        page.unroute("**/context")
        # PR #65 owner review B1: a call without a result is never "not run" — its outcome may be
        # unknown (recovery) or its result kept out of the conversation (ConversationRecord.OMIT).
        cid = page.locator(".conv-row.is-selected").get_attribute("data-id")
        call = {"sequence": 2, "kind": "assistant_tool_calls", "created_at": None, "text": "", "model": None,
                "run_id": "r1", "calls": [{"call_id": "c0", "tool_id": "calliope/stamp", "arguments": {},
                                           "app_view": False}]}
        asked = {"sequence": 1, "kind": "user_message", "created_at": None, "text": "Stamp it", "answers_call": None,
                 "answers_run": None, "attachments": []}
        answer = {"sequence": 3, "kind": "assistant_final", "created_at": None, "text": "Stamped.", "model": None}
        def events_of(items: list[dict[str, Any]]) -> Any:
            def events(route: Any) -> None:
                first_page = "after=0" in route.request.url
                route.fulfill(json={"items": items if first_page else [], "last_sequence": len(items)})
            return events

        def detail_of(state: str) -> Any:
            def detail(route: Any) -> None:
                body = route.fetch().json()
                body["job"].update(running=False, state=state, error=None, stop_requested=None)
                body.update(continuable=False, waiting=None)
                route.fulfill(json=body)
            return detail

        for items, state in (([asked, call], "recovery_required"), ([asked, call, answer], "completed")):
            events, detail = events_of(items), detail_of(state)
            page.route(re.compile(rf"/api/conversations/{cid}/events\?"), events)
            page.route(re.compile(rf"/api/conversations/{cid}$"), detail)
            page.locator(".conv-row").first.click()
            expect(blocks).to_have_count(1, timeout=10000)
            expect(blocks.first.locator(".work-count")).to_have_text("1 step · 1 result unavailable")
            expect(blocks.first.locator(".tool-row-summary")).to_have_text("… calliope/stamp · no result")
            assert "not run" not in page.locator("#timeline").inner_text()
            page.unroute(re.compile(rf"/api/conversations/{cid}/events\?"))
            page.unroute(re.compile(rf"/api/conversations/{cid}$"))
        browser.close()
    return errors


def set_elsewhere(root: str, text: str) -> None:
    """A save made somewhere else (another tab, the CLI) while the page edits."""
    application, loop = OPENED[root]
    current = asyncio.run_coroutine_threadsafe(application.instructions(OPERATOR), loop).result(10)
    asyncio.run_coroutine_threadsafe(application.set_instructions(OPERATOR, text, current.revision), loop).result(10)


def run_instructions(base: str, token: str, shots: str | None, root: str, provider: ScriptedAdapter) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="en-US")  # "10,000"
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        text, save = page.locator("#instructions-text"), page.locator("#instructions-save")
        expect(page.locator("#instructions-count")).to_have_text("0 / 10,000", timeout=5000)
        expect(save).to_be_disabled()
        text.fill("가" * 10_001)  # counted in characters, not bytes: over the limit, no save
        expect(page.locator("#instructions-count")).to_have_class(re.compile(r"\bis-over\b"))
        expect(save).to_be_disabled()
        text.fill("😀" * 5_001)  # 10,002 UTF-16 units but 5,001 code points, as the server counts: allowed
        expect(page.locator("#instructions-count")).to_have_text("5,001 / 10,000")
        expect(save).to_be_enabled()
        page.click("#instructions-discard")  # back to the saved (empty) text
        expect(text).to_have_value("")
        expect(page.locator("#instructions-discard")).to_be_disabled()
        text.fill("Answer in Korean.\nKeep it short.")
        expect(save).to_be_enabled()
        save.click()
        expect(page.locator("#status-bar")).to_have_text("Saved. Applies from the next message.", timeout=5000)
        expect(page.locator("#instructions-saved")).to_contain_text("Saved ")
        expect(save).to_be_disabled()  # nothing left to save
        if shots:
            page.screenshot(path=os.path.join(shots, "16-instructions.png"))
        # The next message's request carries them, after the operator's instructions.
        new_chat(page, "#nav-new-chat")
        page.fill("#composer-text", "hi")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("네, 알겠습니다.", timeout=10000)
        assert "<user_instructions>\nAnswer in Korean.\nKeep it short.\n</user_instructions>" in \
            provider.prepared_inputs[-1].stable_prefix[-1]
        # Changed elsewhere meanwhile: refused, the text kept, the user chooses.
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        expect(text).to_have_value("Answer in Korean.\nKeep it short.", timeout=5000)
        text.fill("Mine.")
        set_elsewhere(root, "Theirs.")
        save.click()
        expect(page.locator("#instructions-conflict")).to_be_visible(timeout=5000)
        expect(page.locator("#status-bar")).to_have_text("Changed elsewhere since you opened it. Choose below.")
        expect(text).to_have_value("Mine.")
        page.click("#instructions-load")
        expect(text).to_have_value("Theirs.", timeout=5000)
        expect(page.locator("#instructions-conflict")).to_be_hidden()
        text.fill("Mine again.")
        set_elsewhere(root, "Theirs again.")
        save.click()
        expect(page.locator("#instructions-conflict")).to_be_visible(timeout=5000)
        expect(page.locator("#instructions-load")).to_be_focused()  # the keyboard lands on a choice
        set_elsewhere(root, "Theirs a third time.")  # changed again after the user was told: not overwritten
        page.click("#instructions-overwrite")
        expect(page.locator("#status-bar")).to_have_text("Changed again elsewhere; choose again.", timeout=5000)
        expect(page.locator("#instructions-load")).to_be_focused()
        expect(text).to_have_value("Mine again.")
        page.click("#instructions-overwrite")
        expect(page.locator("#status-bar")).to_have_text("Saved. Applies from the next message.", timeout=5000)
        expect(text).to_have_value("Mine again.")
        application, loop = OPENED[root]
        assert asyncio.run_coroutine_threadsafe(application.instructions(OPERATOR), loop).result(10).text == \
            "Mine again."
        # Leaving with unsaved changes asks first: from Home and from a conversation in the list.
        text.fill("Not saved.")
        page.click("#nav-home")
        expect(page.locator("#confirm-dialog")).to_be_visible(timeout=5000)
        page.click("#confirm-cancel")
        expect(page.locator("#surface-instructions")).to_be_visible()
        expect(text).to_have_value("Not saved.")
        page.locator(".conv-row").first.click()
        expect(page.locator("#confirm-dialog")).to_be_visible(timeout=5000)
        page.click("#confirm-ok")
        expect(page.locator("#surface-chat")).to_be_visible(timeout=5000)
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        expect(text).to_have_value("Mine again.", timeout=5000)
        # A save that meets a signed-out session (e.g. a server restart) keeps the draft for after sign-in.
        text.fill("Draft kept.")
        page.context.clear_cookies()
        save.click()
        expect(page.locator("#surface-login")).to_be_visible(timeout=5000)
        expect(page.locator("#status-bar")).to_have_text("Signed out; your instructions are kept. Sign in to continue.")
        page.fill("#login-token", token)
        page.click("#login-submit")
        expect(page.locator("#surface-instructions")).to_be_visible(timeout=5000)
        expect(text).to_have_value("Draft kept.")
        expect(save).to_be_enabled()
        save.click()
        expect(page.locator("#status-bar")).to_have_text("Saved. Applies from the next message.", timeout=5000)
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "17-instructions-narrow.png"))
        browser.close()
    return errors


def memory_script() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                      {"category": "user", "text": "Writes Go at work."}),)),
        NormalizedFinal((TextBlock("Noted."),)),
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                      {"category": "user", "text": "Writes Go and Rust.", "replaces": "m1"}),)),
        NormalizedFinal((TextBlock("Updated."),)),
        NormalizedFinal((TextBlock("ok."),)),
    ])


def on_server(root: str, call: Any) -> Any:
    """Run ``call(application)`` (a coroutine) on the server's own loop."""
    application, loop = OPENED[root]
    return asyncio.run_coroutine_threadsafe(call(application), loop).result(10)


def run_memory(base: str, token: str, shots: str | None, root: str, provider: ScriptedAdapter) -> list[str]:
    from himari.data.api import MemoryCategory

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="en-US")
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and not re.search(r"40[49]|ERR_FAILED", m.text) else None)  # aborted on purpose
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.fill("#composer-text", "Remember: I write Go at work.")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("Noted.", timeout=10000)
        lines = page.locator("#timeline > .item-memory")
        expect(lines).to_have_count(1)
        expect(lines.first).to_contain_text("Remembered")
        expect(lines.first.locator(".item-memory-undo")).to_have_text("Undo")
        expect(lines.first.locator(".item-memory-text")).to_have_text("Writes Go at work.")
        assert page.locator(".item-work .item-memory").count() == 0  # never folded into the work block
        expect(page.locator(".item-work .item-tool-row")).to_have_count(1)  # the call itself is in the block
        page.fill("#composer-text", "I write Rust too.")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("Updated.", timeout=10000)
        expect(lines).to_have_count(2)
        expect(lines.nth(1)).to_contain_text("Updated memory")
        expect(lines.nth(1).locator(".item-memory-text")).to_have_text("Writes Go and Rust.")
        if shots:
            page.screenshot(path=os.path.join(shots, "18-memory-lines.png"))
        # The first line's Undo is stale (the note was rewritten since): refused, the button goes.
        lines.first.locator(".item-memory-undo").click()
        expect(page.locator("#status-bar")).to_have_text("The note was changed since; see Settings › Memory.",
                                                         timeout=5000)
        expect(lines.first.locator(".item-memory-undo")).to_have_count(0)
        lines.nth(1).locator(".item-memory-undo").click()
        expect(page.locator("#status-bar")).to_have_text("The note is back as it was.", timeout=5000)
        expect(lines.nth(1).locator(".item-memory-done")).to_have_text("Undone")
        (note,) = on_server(root, lambda app: app.memories(OPERATOR))
        assert (note.text, note.revision) == ("Writes Go at work.", 3)
        page.fill("#composer-text", "thanks")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("ok.", timeout=10000)
        memories = provider.prepared_inputs[-1].current_state.memories
        assert memories is not None and [m.text for m in memories] == ["Writes Go at work."]
        page.reload()
        page.locator(".conv-row").first.click()
        expect(lines).to_have_count(2, timeout=10000)
        expect(page.locator(".item-memory-undo")).to_have_count(0)  # neither call left the note as it is now

        # The delete confirmation counts the notes the conversation saved.
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        dialog = page.locator("#confirm-dialog")
        expect(dialog).to_contain_text("1 note HIMARI saved in this conversation is deleted when it is deleted for good.",
                                       timeout=5000)
        page.click("#confirm-cancel")

        # The memory screen.
        page.click("#nav-appearance")
        page.click("#nav-memory")
        expect(page.locator("#surface-memory")).to_be_visible(timeout=5000)
        expect(page.locator("#memory-count")).to_have_text("1 of 1,000", timeout=5000)
        expect(page.locator("#memory-off")).to_be_hidden()
        row = page.locator(".memory-row").first
        expect(row).to_contain_text("Writes Go at work.")
        expect(row.locator(".memory-source")).to_contain_text("From “")
        expect(row.locator(".memory-source")).to_contain_text(" · HIMARI · ")
        page.get_by_role("button", name="Add to Preferences").click()
        page.locator(".memory-form-text").fill("Short lists.")
        page.locator(".memory-form button[type=submit]").click()
        expect(page.locator("#status-bar")).to_have_text("Saved. HIMARI uses it from the next message.", timeout=5000)
        expect(page.locator("#memory-count")).to_have_text("2 of 1,000", timeout=5000)
        expect(page.locator(".memory-row", has_text="Short lists.")).to_contain_text("Added by you")
        # The answer to a save is lost after the server saved it: recognized, never saved twice (PR #77 review C1).
        state, count = page.locator("#status-bar"), page.locator("#memory-count")

        def lose_answer(route: Any) -> None:
            if route.request.method != "POST":
                route.continue_()
                return
            route.fetch()  # the server saves it
            route.abort()  # the page never hears back

        page.route("**/api/memories", lose_answer)
        page.get_by_role("button", name="Add to Projects").click()
        page.locator(".memory-form-text").fill("Builds HIMARI.")
        page.evaluate("document.getElementById('status-bar').textContent = ''")  # not the last save's line
        page.locator(".memory-form button[type=submit]").click()
        expect(state).to_have_text("Saved. HIMARI uses it from the next message.", timeout=5000)
        expect(count).to_have_text("3 of 1,000", timeout=5000)
        expect(page.locator(".memory-form")).to_have_count(0, timeout=5000)
        page.unroute("**/api/memories")
        # The server cannot be reached at all: unsure, and saving again looks first.
        page.route("**/api/memories", lambda route: route.abort())
        page.get_by_role("button", name="Add to References").click()
        page.locator(".memory-form-text").fill("Docs live in docs/.")
        page.locator(".memory-form button[type=submit]").click()
        expect(state).to_have_text("Could not confirm the save. Saving again checks first, so it is not saved twice.",
                                   timeout=5000)
        page.locator(".memory-form button[type=submit]").click()
        expect(state).to_have_text("Still could not reach the server; nothing was saved again.", timeout=5000)
        page.unroute("**/api/memories")
        page.locator(".memory-form button[type=submit]").click()  # it was not saved: saved now, once
        expect(count).to_have_text("4 of 1,000", timeout=5000)
        assert sorted(n.text for n in on_server(root, lambda app: app.memories(OPERATOR))) == [
            "Builds HIMARI.", "Docs live in docs/.", "Short lists.", "Writes Go at work."]
        for gone in (3, 4):
            on_server(root, lambda app, gone=gone: app.delete_memory(OPERATOR, gone))
        page.click("#nav-appearance")
        page.click("#nav-memory")
        expect(count).to_have_text("2 of 1,000", timeout=5000)
        # The same text as a note already there, lost before it reached the server: not taken for saved
        # (integration review C2).
        page.route("**/api/memories", lambda route: route.abort() if route.request.method == "POST"
                   else route.continue_())
        page.get_by_role("button", name="Add to Preferences").click()
        page.locator(".memory-form-text").fill("Short lists.")
        page.locator(".memory-form button[type=submit]").click()
        expect(state).to_have_text("The note was not saved. Try again.", timeout=5000)
        page.unroute("**/api/memories")
        page.locator(".memory-form .link-button", has_text="Cancel").click()
        expect(count).to_have_text("2 of 1,000", timeout=5000)

        # Memory off (as the server says): the notice, and a save that does not promise use (review C3).
        def off(route: Any) -> None:
            if route.request.method != "GET":
                route.continue_()
                return
            response = route.fetch()
            route.fulfill(response=response, json={**response.json(), "enabled": False})

        page.route("**/api/memories", off)
        page.click("#nav-appearance")
        page.click("#nav-memory")
        expect(page.locator("#memory-off")).to_be_visible(timeout=5000)
        page.get_by_role("button", name="Add to Projects").click()
        page.locator(".memory-form-text").fill("Kept while off.")
        page.locator(".memory-form button[type=submit]").click()
        expect(state).to_have_text("Saved. Memory is off on this server, so models do not use it.", timeout=5000)
        expect(page.locator(".memory-form")).to_have_count(0, timeout=5000)  # repainted: no request left on the route
        page.unroute("**/api/memories")
        on_server(root, lambda app: app.delete_memory(OPERATOR, 5))
        page.click("#nav-appearance")
        page.click("#nav-memory")
        expect(page.locator("#memory-off")).to_be_hidden(timeout=5000)
        expect(count).to_have_text("2 of 1,000", timeout=5000)
        # An edit refused after a change made elsewhere: theirs shown, mine kept, saved again on purpose.
        page.get_by_role("button", name="Edit note m2").click()
        page.locator(".memory-form-text").fill("Short lists, no tables.")
        on_server(root, lambda app: app.edit_memory(OPERATOR, 2, MemoryCategory.PREFERENCE, "Long answers.", 1))
        page.locator(".memory-form button[type=submit]").click()
        expect(page.locator(".memory-form-conflict")).to_contain_text("Long answers.", timeout=5000)
        expect(page.locator(".memory-form-text")).to_have_value("Short lists, no tables.")
        page.locator(".memory-form button[type=submit]").click()
        expect(page.locator(".memory-row", has_text="Short lists, no tables.")).to_have_count(1, timeout=5000)
        page.get_by_role("button", name="Delete note m2").click()
        expect(dialog).to_contain_text("Conversations that already used it still show it.")
        page.click("#confirm-ok")
        expect(page.locator("#memory-count")).to_have_text("1 of 1,000", timeout=5000)
        # Leaving with an unsaved edit asks first (PR #77 review C2).
        page.get_by_role("button", name="Edit note m1").click()
        page.locator(".memory-form-text").fill("Writes Go at work, mostly.")
        page.click("#nav-home")
        expect(dialog).to_contain_text("Your changes to a memory are not saved.", timeout=5000)
        page.click("#confirm-cancel")
        expect(page.locator("#surface-memory")).to_be_visible()
        expect(page.locator(".memory-form-text")).to_have_value("Writes Go at work, mostly.")
        page.locator(".memory-form .link-button", has_text="Cancel").click()
        expect(page.locator(".memory-form")).to_have_count(0, timeout=5000)
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            page.screenshot(path=os.path.join(shots, "19-memory-narrow.png"))
        page.set_viewport_size({"width": 1280, "height": 860})
        page.locator(".memory-source-link").click()  # the source opens its conversation
        expect(page.locator("#surface-chat")).to_be_visible(timeout=5000)
        expect(lines).to_have_count(2, timeout=10000)

        # Deleted for good from the trash: the confirmation says so, and the note goes with it.
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        page.click("#confirm-ok")
        expect(page.locator("#surface-home")).to_be_visible(timeout=10000)
        page.click("#nav-trash")
        page.locator(".trash-row .danger-button").click()
        expect(dialog).to_contain_text("1 note HIMARI saved in this conversation is deleted with it.", timeout=5000)
        page.click("#confirm-ok")
        expect(page.locator("#trash-empty")).to_be_visible(timeout=10000)
        assert on_server(root, lambda app: app.memories(OPERATOR)) == ()
        browser.close()
    return errors


SCENARIOS = ("main", "compaction", "budget", "trash", "files", "images", "work", "instructions", "memory")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots")
    parser.add_argument("--only", help="comma-separated scenarios to run: " + ", ".join(SCENARIOS))
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else set(SCENARIOS)
    if only - set(SCENARIOS):
        parser.error(f"unknown scenarios: {', '.join(sorted(only - set(SCENARIOS)))}")
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="himari-ui-") as tmp:
        if "main" in only:
            root = os.path.join(tmp, "root")
            server = serve(root)
            try:
                errors += run(f"http://127.0.0.1:{PORT}",
                              Path(root, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "compaction" in only:
            small = os.path.join(tmp, "small")
            provider = ScriptedAdapter([NormalizedFinal((TextBlock(SUMMARY),))] * 12)  # every answer and summary alike
            # memory off: the budget was fitted to a prompt without the memory Tools (V3-P71)
            server = serve(small, PORT + 1, provider, context_tokens=2800, memory=False)
            try:
                errors += run_compaction(f"http://127.0.0.1:{PORT + 1}",
                                         Path(small, "control", "operator.secret").read_text().strip(), args.screenshots,
                                         small)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "budget" in only:
            budget = os.path.join(tmp, "budget")
            server = serve(budget, PORT + 2, budget_script(), turn_budget=100_000)
            try:
                errors += run_turn_budget(f"http://127.0.0.1:{PORT + 2}",
                                          Path(budget, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "trash" in only:
            trash = os.path.join(tmp, "trash")
            server = serve(trash, PORT + 3, SlowAdapter([NormalizedFinal((TextBlock("first answer"),)),
                                                         NormalizedFinal((TextBlock("second answer"),))]))
            try:
                errors += run_trash(f"http://127.0.0.1:{PORT + 3}",
                                    Path(trash, "control", "operator.secret").read_text().strip(), args.screenshots, trash)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "files" in only:
            files = os.path.join(tmp, "files")
            server = serve(files, PORT + 4, ScriptedAdapter([
                NormalizedFinal((TextBlock("got the files"),)),
                NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/present_file"),
                                              {"path": "uploads/a.txt"}),)),
                NormalizedFinal((TextBlock("here it is"),)),
            ]))
            try:
                errors += run_files(f"http://127.0.0.1:{PORT + 4}",
                                    Path(files, "control", "operator.secret").read_text().strip(), args.screenshots, tmp)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "images" in only:
            from himari.bootstrap import application as application_module

            async def in_process(root: Path, sandbox: bool) -> InProcessViews:
                return InProcessViews()

            real = application_module._image_views
            application_module._image_views = in_process  # type: ignore[assignment]
            images = os.path.join(tmp, "images")
            server = serve(images, PORT + 5, SeeingAdapter([
                NormalizedFinal((TextBlock("a blue photo"),)),
                NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/read_file"),
                                              {"path": "uploads/photo.png"}),)),
                NormalizedFinal((TextBlock("still blue"),)),
                NormalizedFinal((TextBlock("not an image"),)),
            ]))
            try:
                errors += run_images(f"http://127.0.0.1:{PORT + 5}",
                                     Path(images, "control", "operator.secret").read_text().strip(), args.screenshots, tmp)
            finally:
                application_module._image_views = real  # type: ignore[assignment]
                server.should_exit = True
                time.sleep(0.5)
        if "work" in only:
            work = os.path.join(tmp, "work")
            server = serve(work, PORT + 6, work_script())
            try:
                errors += run_work_progress(f"http://127.0.0.1:{PORT + 6}",
                                            Path(work, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "instructions" in only:
            instructions = os.path.join(tmp, "instructions")
            provider = ScriptedAdapter([NormalizedFinal((TextBlock("네, 알겠습니다."),))])
            server = serve(instructions, PORT + 7, provider)
            try:
                errors += run_instructions(f"http://127.0.0.1:{PORT + 7}",
                                           Path(instructions, "control", "operator.secret").read_text().strip(),
                                           args.screenshots, instructions, provider)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "memory" in only:
            memory = os.path.join(tmp, "memory")
            provider = memory_script()
            server = serve(memory, PORT + 8, provider)
            try:
                errors += run_memory(f"http://127.0.0.1:{PORT + 8}",
                                     Path(memory, "control", "operator.secret").read_text().strip(),
                                     args.screenshots, memory, provider)
            finally:
                server.should_exit = True
                time.sleep(0.5)
    print("UI smoke: OK" if not errors else f"UI smoke: console errors {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
