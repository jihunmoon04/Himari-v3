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
note, and the narrow layout. An eleventh (V3-P78) keeps drafts: one per
conversation and Home, back after a reload and an ended session, a send
accepted after the user left, another tab that changes nothing, and a
sign-out that deletes them. A twelfth (V3-P85, "passes") shows a memory pass's line
after the event it read (opened, an Undo, a sensitive note to review), the
Memory screen's open loops with their dates, a suggestion kept, a loop added
with a date, the Timeline (an edit) and the narrow layout.

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
from himari.provider.scripted import Measured, ScriptedAdapter, StreamCut  # noqa: E402

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
          context_tokens: int | None = None, turn_budget: int = 3_000_000, memory: bool = True,
          conversation_search: bool = True) -> uvicorn.Server:
    config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        application = await Application.open(root, provider or scripted(), model_id="scripted-model", sandbox=False,
                                             context_tokens=context_tokens, turn_budget=turn_budget, memory=memory,
                                             conversation_search=conversation_search)
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
    composer last. (A message sent while it is being created waits for it;
    run_trash checks that.)"""
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
        expect(page.locator("#question-text")).to_have_text("Which city?", timeout=10000)  # formatted, as above
        expect(page.locator("#question-dismiss")).to_be_visible()  # dismissed from the panel, not only the menu
        expect(page.locator("#question-text strong")).to_have_text("city")
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
        page.reload()  # V3-P79 §3.3: the conversation's address opens it again, its name on the tab
        expect(page.locator("#surface-chat")).to_be_visible()
        expect(page.locator("#topbar-title")).to_have_text("Seoul trip")
        expect(page).to_have_title("Seoul trip · HIMARI")
        page.click("#nav-home")
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
        expect(page.locator("#status-bar")).to_contain_text(  # why, in plain words, with what to do
            "Stopped: this turn used its token budget — Continue to go on with a new one")
        expect(page.locator("#status-bar")).not_to_contain_text("92000")
        page.locator("#status-bar .status-action").click()  # the server's own terms behind Details
        expect(page.locator("#status-bar")).to_contain_text("the turn used 92000 tokens;")
        expect(page.locator("#stop-text")).to_have_text(  # short, beside Continue, then the usage
            "Token budget used · 90k in · 2.0k out / 100k")
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
        expect(dialog).to_contain_text("Text that other conversations found with search stays in them.")  # V3-P72
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

        # Enter while New chat is still creating the conversation: the message
        # waits and goes to the new one, once even when Enter is pressed twice.
        new_chat(page, "#nav-new-chat")  # in a chat, as the user is when pressing New chat
        held: list[Any] = []
        page.route("**/api/conversations",
                   lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
        page.click("#nav-new-chat")
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(50)
        assert held
        page.fill("#composer-text", "second")
        page.press("#composer-text", "Enter")
        page.press("#composer-text", "Enter")
        held[0].continue_()
        expect(page.locator(".item-user")).to_have_count(1, timeout=10000)
        expect(page.locator(".item-user")).to_contain_text("second")
        expect(page.locator("#composer-text")).to_have_value("")
        page.unroute("**/api/conversations")

        # Trashed elsewhere while this page follows its work: it says so and goes home.
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
        # V3-P78 §3.2 (Q1): leaving asks first; Cancel stays with the upload, Leave cancels it.
        expect(page.get_by_role("dialog", name="A file is still uploading")).to_be_visible()
        page.get_by_role("button", name="Cancel").click()
        expect(page.locator("#surface-chat")).to_be_visible()
        page.click("#nav-home")
        page.get_by_role("button", name="Leave").click()
        expect(page.locator("#surface-home")).to_be_visible()
        held[0].continue_()
        page.wait_for_timeout(500)
        page.unroute("**/uploads?*")
        page.locator(f'#conversation-list .conv-row[data-id="{conversation_id}"]').click()
        expect(page.locator(".item-file").last).to_be_visible(timeout=10000)
        # The file that had finished is back in the conversation's draft (§3.1); the cancelled one is not.
        expect(page.locator("#attach-list .attach-chip")).to_have_count(1)
        expect(page.locator("#attach-list .attach-chip").first).to_have_text(re.compile(r"d\.txt · 7 bytes"))
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
        page.reload()  # the conversation's address opens it again (V3-P79), with the list beside it
        expect(page.locator(".conv-row.is-selected")).to_have_count(1)
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
        # V3-P89 §4: the companion persona, its own box under the instructions, before them in the request.
        # Its load failing once leaves it unloaded; Instructions again reads only it (PR #152 review P2).
        page.route("**/api/instructions/persona", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error": {"code": "unavailable", "message": "x"}}'))
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        expect(text).to_have_value("Answer in Korean.\nKeep it short.", timeout=5000)
        persona = page.locator("#persona-text")
        expect(page.locator("#persona-count")).to_have_text("Loading…")
        expect(persona).not_to_be_editable()
        page.unroute("**/api/instructions/persona")
        text.fill("Answer in Korean.\nKeep it short.\nDraft.")  # a draft of the instructions survives the retry
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        expect(page.locator("#persona-count")).to_have_text("0 / 10,000", timeout=5000)
        expect(text).to_have_value("Answer in Korean.\nKeep it short.\nDraft.")
        page.click("#instructions-discard")
        expect(page.locator("#persona-off")).to_be_hidden()  # companion mode is on here
        expect(page.locator("#persona-save")).to_be_disabled()
        persona.fill("Call me 선생님.")
        page.click("#persona-save")
        expect(page.locator("#status-bar")).to_have_text("Persona saved. Applies from the next message.", timeout=5000)
        expect(page.locator("#persona-saved")).to_contain_text("Saved ")
        if shots:
            page.screenshot(path=os.path.join(shots, "16b-instructions-persona.png"), full_page=True)
        new_chat(page, "#nav-new-chat")
        page.fill("#composer-text", "hi again")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("네, 알겠습니다.", timeout=10000)
        sent = "\n".join(provider.prepared_inputs[-1].stable_prefix)
        assert "<companion_persona>\nCall me 선생님.\n</companion_persona>" in sent
        assert sent.index("<companion_persona>") < sent.index("<user_instructions>")
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


def run_passes(base: str, token: str, shots: str | None, root: str) -> list[str]:
    """V3-P85 part 4: a memory pass's line after the event it read (opened, an Undo), the Memory screen's open
    loops with their dates, a suggestion kept, the Timeline (an edit), and the narrow layout."""
    from datetime import UTC, datetime, timedelta

    from himari.data.api import EpisodePlan, MemoryCategory, PassAction, PassItem, PassKind, PassStart

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="en-US")
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and not re.search(r"40[49]|422|ERR_FAILED", m.text) else None)  # 422: stale, on purpose
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.fill("#composer-text", "I build HIMARI. My interview is on Nov 4. I take pills.")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("ok.", timeout=10000)
        conversation_id = page.url.rsplit("/", 1)[-1]
        scope = TrustedScope(OPERATOR, ConversationId(conversation_id))
        now = datetime.now(UTC)

        async def seed(app: Application) -> str:
            last = (await app.data.last_event(scope))[0]
            pass_id = await app.data.begin_pass(scope, PassStart(PassKind.LIVE, 0, last, last, "p", "m"))
            await app.data.apply_pass(pass_id, [
                PassItem(0, '{"evidence": [1]}', PassAction.ADD, category=MemoryCategory.PROJECT,
                         text="Builds HIMARI."),
                PassItem(1, '{"evidence": [1]}', PassAction.ADD, category=MemoryCategory.LOOP,
                         text="Interview at Kakao.", due_at="2026-11-04", due_instant=now + timedelta(days=4)),
                PassItem(2, '{"evidence": [1]}', PassAction.PROPOSE, "sensitive_proposed", category=MemoryCategory.USER,
                         text="Takes pills."),
                PassItem(3, '{"evidence": [1]}', PassAction.PROPOSE, "sensitive_proposed", category=MemoryCategory.LOOP,
                         text="Doctor in September.", due_at="2026-09-01", due_instant=now - timedelta(days=60)),
            ], EpisodePlan("Talked about HIMARI and an interview.", now, now, (1,), (2,)), now=now)
            return pass_id

        on_server(root, seed)
        page.reload()
        line = page.locator("#timeline > .item-pass")
        expect(line).to_have_count(1, timeout=10000)
        expect(line.locator(".item-pass-summary")).to_have_text(
            "HIMARI remembered 2 things · 2 sensitive notes waiting for you")
        assert page.evaluate("[...document.querySelectorAll('#timeline > *')].pop().classList.contains('item-pass')")
        line.locator(".item-pass-summary").click()
        items = line.locator(".item-pass-item")
        expect(items).to_have_count(4)
        expect(items.nth(1)).to_contain_text("Open loop")
        expect(items.nth(1)).to_contain_text("(due 2026-11-04)")
        expect(items.nth(2).locator("button", has_text="Review")).to_have_count(1)
        items.nth(1).locator("button", has_text="Source").click()  # PR #138 review: to the message it rests on
        expect(page.locator('#timeline > .item-user[data-sequence="1"]')).to_have_class(re.compile("is-revealed"))
        if shots:
            page.screenshot(path=os.path.join(shots, "40-pass-line.png"))
        items.first.locator(".item-memory-undo").click()
        expect(page.locator("#status-bar")).to_have_text("HIMARI no longer remembers it.", timeout=5000)
        expect(items.first.locator(".item-memory-done")).to_have_text("Undone")
        assert [n.text for n in on_server(root, lambda app: app.memories(OPERATOR))] == ["Interview at Kakao."]

        items.nth(2).locator("button", has_text="Review").click()  # Review: the Memory screen and its suggestion
        expect(page.locator("#memory-suggestions")).to_contain_text("Takes pills.", timeout=10000)
        expect(page.locator("#memory-suggestions")).to_contain_text("Sensitive: kept only if you choose")
        expect(page.locator("#memory-suggestions")).to_contain_text("“I build HIMARI. My interview")  # its preview
        expect(page.locator(".memory-group").first).to_contain_text("Open loops")
        expect(page.locator(".memory-group").first).to_contain_text("Due 2026-11-04")
        expect(page.locator(".memory-group").first).to_contain_text("auto")
        if shots:
            page.screenshot(path=os.path.join(shots, "41-memory-loops-suggestions.png"))
        # Before keeping it: Compare (the notes of its kind) and Source (what the user said; PR #138 re-review).
        page.click("#memory-suggestions button[aria-label='Compare with About you']")
        expect(page.locator('.memory-group[data-category="user"]')).to_have_class(re.compile("is-revealed"))
        page.click("#memory-suggestions button[aria-label='Go to the message: Takes pills.']")
        expect(page.locator('#timeline > .item-user[data-sequence="1"]')).to_have_class(re.compile("is-revealed"),
                                                                                       timeout=10000)
        page.goto(base + "/settings/memory")
        page.click("#memory-suggestions button[aria-label='Keep: Takes pills.']")
        expect(page.locator("#status-bar")).to_have_text("Kept. HIMARI uses it from the next message.", timeout=5000)
        # A suggestion whose date passed long ago: kept only without it, after asking (PR #138 review); called
        # off, nothing is said to be kept.
        page.locator("#memory-suggestions button[aria-label^='Keep:']").first.click()
        expect(page.locator("#confirm-dialog")).to_contain_text("Its date (2026-09-01) has passed.", timeout=5000)
        page.click("#confirm-cancel")
        expect(page.locator("#status-bar")).to_have_text("Nothing was kept; the suggestion is still waiting.",
                                                         timeout=5000)
        page.locator("#memory-suggestions button[aria-label^='Keep:']").first.click()
        expect(page.locator("#confirm-dialog")).to_contain_text("Its date (2026-09-01) has passed.", timeout=5000)
        page.click("#confirm-ok")
        expect(page.locator("#memory-suggestions")).to_be_hidden(timeout=5000)
        doctor = next(n for n in on_server(root, lambda app: app.memories(OPERATOR)) if n.text == "Doctor in September.")
        assert doctor.due_at is None
        expect(page.locator("#memory-groups")).to_contain_text("auto · kept by you")

        # A loop of the user's own, with a date from the form.
        loops = page.locator(".memory-group").first
        loops.locator("button", has_text="Add").click()
        form = loops.locator(".memory-form")
        form.locator(".memory-form-text").fill("Car inspection.")
        form.locator(".memory-form-date").fill("2026-11-03")
        form.locator("button[type=submit]").click()
        expect(page.locator("#status-bar")).to_contain_text("Saved.", timeout=5000)
        notes = on_server(root, lambda app: app.memories(OPERATOR))
        inspection = next(n for n in notes if n.text == "Car inspection.")
        assert (inspection.category, inspection.due_at) == (MemoryCategory.LOOP, "2026-11-03")

        page.click("#memory-tab-timeline")
        rows = page.locator("#memory-timeline .memory-row")
        expect(rows).to_have_count(1, timeout=10000)
        expect(rows.first).to_contain_text("Talked about HIMARI and an interview.")
        rows.first.locator("button", has_text="Edit").click()
        page.locator("#memory-timeline .memory-form-text").fill("We planned the interview.")
        page.locator("#memory-timeline button[type=submit]").click()
        expect(rows.first).to_contain_text("We planned the interview.", timeout=5000)
        expect(rows.first).to_contain_text("edited by you")
        rows.first.locator("button", has_text="HIMARI's replies").click()  # the conversation, at what it rests on
        expect(page.locator('#timeline > .item-assistant[data-sequence="2"]')).to_have_class(
            re.compile("is-revealed"), timeout=10000)
        page.goto(base + "/settings/memory")
        page.click("#memory-tab-timeline")
        if shots:
            page.screenshot(path=os.path.join(shots, "42-memory-timeline.png"))

        # A fresh page at the narrow size, with the session cookie of the signed-in one.
        narrow = browser.new_context(viewport={"width": 390, "height": 800}, locale="en-US",
                                     storage_state=page.context.storage_state()).new_page()
        narrow.goto(base + "/settings/memory")
        expect(narrow.locator(".memory-group").first).to_contain_text("Open loops", timeout=10000)
        assert narrow.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        narrow.click("#memory-tab-timeline")
        expect(narrow.locator("#memory-timeline .memory-row")).to_have_count(1, timeout=10000)
        assert narrow.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if shots:
            narrow.screenshot(path=os.path.join(shots, "43-memory-narrow.png"))
        browser.close()
    return errors


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
        page.locator("#surface-memory .memory-form-text").fill("Short lists.")
        page.locator("#surface-memory .memory-form button[type=submit]").click()
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
        page.locator("#surface-memory .memory-form-text").fill("Builds HIMARI.")
        page.evaluate("document.getElementById('status-bar').textContent = ''")  # not the last save's line
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(state).to_have_text("Saved. HIMARI uses it from the next message.", timeout=5000)
        expect(count).to_have_text("3 of 1,000", timeout=5000)
        expect(page.locator("#surface-memory .memory-form")).to_have_count(0, timeout=5000)
        page.unroute("**/api/memories")
        # The server cannot be reached at all: unsure, and saving again looks first.
        page.route("**/api/memories", lambda route: route.abort())
        page.get_by_role("button", name="Add to References").click()
        page.locator("#surface-memory .memory-form-text").fill("Docs live in docs/.")
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(state).to_have_text("Could not confirm the save. Saving again checks first, so it is not saved twice.",
                                   timeout=5000)
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(state).to_have_text("Still could not reach the server; nothing was saved again.", timeout=5000)
        page.unroute("**/api/memories")
        page.locator("#surface-memory .memory-form button[type=submit]").click()  # it was not saved: saved now, once
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
        page.locator("#surface-memory .memory-form-text").fill("Short lists.")
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(state).to_have_text("The note was not saved. Try again.", timeout=5000)
        page.unroute("**/api/memories")
        page.locator("#surface-memory .memory-form .link-button", has_text="Cancel").click()
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
        page.locator("#surface-memory .memory-form-text").fill("Kept while off.")
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(state).to_have_text("Saved. Memory is off on this server, so models do not use it.", timeout=5000)
        expect(page.locator("#surface-memory .memory-form")).to_have_count(0, timeout=5000)  # repainted: no request left on the route
        page.unroute("**/api/memories")
        on_server(root, lambda app: app.delete_memory(OPERATOR, 5))
        page.click("#nav-appearance")
        page.click("#nav-memory")
        expect(page.locator("#memory-off")).to_be_hidden(timeout=5000)
        expect(count).to_have_text("2 of 1,000", timeout=5000)
        # An edit refused after a change made elsewhere: theirs shown, mine kept, saved again on purpose.
        page.get_by_role("button", name="Edit note m2").click()
        page.locator("#surface-memory .memory-form-text").fill("Short lists, no tables.")
        on_server(root, lambda app: app.edit_memory(OPERATOR, 2, MemoryCategory.PREFERENCE, "Long answers.", 1))
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(page.locator("#surface-memory .memory-form-conflict")).to_contain_text("Long answers.", timeout=5000)
        expect(page.locator("#surface-memory .memory-form-text")).to_have_value("Short lists, no tables.")
        page.locator("#surface-memory .memory-form button[type=submit]").click()
        expect(page.locator(".memory-row", has_text="Short lists, no tables.")).to_have_count(1, timeout=5000)
        page.get_by_role("button", name="Delete note m2").click()
        expect(dialog).to_contain_text("Conversations that already used it still show it.")
        page.click("#confirm-ok")
        expect(page.locator("#memory-count")).to_have_text("1 of 1,000", timeout=5000)
        # Leaving with an unsaved edit asks first (PR #77 review C2).
        page.get_by_role("button", name="Edit note m1").click()
        page.locator("#surface-memory .memory-form-text").fill("Writes Go at work, mostly.")
        page.click("#nav-home")
        expect(dialog).to_contain_text("Your changes to a memory are not saved.", timeout=5000)
        page.click("#confirm-cancel")
        expect(page.locator("#surface-memory")).to_be_visible()
        expect(page.locator("#surface-memory .memory-form-text")).to_have_value("Writes Go at work, mostly.")
        page.locator("#surface-memory .memory-form .link-button", has_text="Cancel").click()
        expect(page.locator("#surface-memory .memory-form")).to_have_count(0, timeout=5000)
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


SCENARIOS = ("main", "compaction", "budget", "trash", "files", "images", "work", "instructions", "memory",
             "streaming", "drafts", "addresses", "activity", "polish", "devices", "passes")


def run_drafts(base: str, token: str, shots: str | None) -> list[str]:
    """V3-P78 §3, §5.3: each place keeps its own draft, in the browser across a reload and an ended session;
    a message accepted after the user left leaves that draft; another tab never erases one; signing out
    deletes them all."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        box = page.locator("#composer-text")

        def sign_in() -> None:  # to the page's address: Home, or the conversation it showed (V3-P79)
            page.fill("#login-token", token)
            page.click("#login-submit")
            expect(page.locator("#surface-login")).to_be_hidden()

        def open_row(conversation: str) -> None:
            page.locator(f'#conversation-list .conv-row[data-id="{conversation}"]').click()
            expect(page.locator(f'#conversation-list .conv-row.is-selected[data-id="{conversation}"]')).to_be_visible()

        def settled() -> None:
            page.wait_for_timeout(700)  # past the composer's save delay (0.4 s)

        page.goto(base)
        sign_in()
        new_chat(page)
        a = page.locator(".conv-row.is-selected").get_attribute("data-id")
        box.fill("draft for A")
        new_chat(page, "#nav-new-chat")
        expect(box).to_have_value("")  # A's draft never follows into B (§1 A)
        b = page.locator(".conv-row.is-selected").get_attribute("data-id")
        assert a and b and a != b
        box.fill("draft for B")
        open_row(a)
        expect(box).to_have_value("draft for A")
        open_row(b)
        expect(box).to_have_value("draft for B")
        page.click("#nav-home")
        expect(box).to_have_value("")
        box.fill("home draft")
        open_row(a)
        expect(box).to_have_value("draft for A")
        page.click("#nav-home")
        expect(box).to_have_value("home draft")
        settled()

        page.reload()  # the session stays; the drafts come from the browser's store
        expect(page.locator("#surface-home")).to_be_visible()
        expect(box).to_have_value("home draft")
        open_row(a)
        expect(box).to_have_value("draft for A")
        if shots:
            page.screenshot(path=os.path.join(shots, "20-draft-restored.png"))

        # Sent, accepted after the user left: back in A the sent text is gone, and nothing says "maybe sent".
        held: list[Any] = []
        page.route("**/messages", lambda route: held.append(route))
        page.press("#composer-text", "Enter")
        while not held:
            page.wait_for_timeout(50)
        open_row(b)
        expect(box).to_have_value("draft for B")
        held[0].continue_()
        page.wait_for_timeout(500)
        page.unroute("**/messages")
        open_row(a)
        expect(page.locator(".item-user").last).to_contain_text("draft for A", timeout=10000)
        expect(box).to_have_value("")
        expect(page.locator("#status-bar")).not_to_contain_text("may have been sent")

        # Another tab on B that changes nothing, then goes away, erases nothing.
        other = context.new_page()
        other.goto(base)
        other.locator(f'#conversation-list .conv-row[data-id="{b}"]').click()
        expect(other.locator("#composer-text")).to_have_value("draft for B")
        other.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        other.close()
        page.reload()  # back to the conversation it showed (V3-P79)
        expect(page.locator("#surface-chat")).to_be_visible()
        open_row(b)
        expect(box).to_have_value("draft for B")

        # The session ends (a restart signs everyone out): what was typed is kept for the next sign-in.
        box.fill("typed before the session ended")
        settled()
        context.clear_cookies()
        page.locator(f'#conversation-list .conv-row[data-id="{a}"]').click()
        expect(page.locator("#surface-login")).to_be_visible()
        sign_in()
        open_row(b)
        expect(box).to_have_value("typed before the session ended")

        # Sign out: told how many are kept, then none are, in this tab or after signing in again.
        page.click("#nav-appearance")
        page.click("#nav-sign-out")
        expect(page.locator("#confirm-dialog")).to_contain_text("Unsaved drafts kept in this browser")
        page.click("#confirm-ok")
        expect(page.locator("#surface-login")).to_be_visible()
        sign_in()
        expect(box).to_have_value("")
        open_row(b)
        expect(box).to_have_value("")
        open_row(a)
        expect(box).to_have_value("")
        browser.close()
    return errors


class GatedAdapter(ScriptedAdapter):
    """Answers in order; the ones listed in ``gated`` wait until the test opens their gate."""

    def __init__(self, outputs: list[Any], gated: set[int]) -> None:
        super().__init__(outputs)
        self.gates = {n: threading.Event() for n in gated}
        self.calls = 0

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        n = self.calls
        self.calls += 1
        gate = self.gates.get(n)
        while gate is not None and not gate.is_set():
            await asyncio.sleep(0.05)
        return await super().invoke_prepared(request)


def activity_script() -> GatedAdapter:
    return GatedAdapter([
        NormalizedFinal((TextBlock("done with A"),)),  # 0: A works until released
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ASK_USER_TOOL_ID, {"question": "Which one?"}),)),  # 1
        NormalizedFinal((TextBlock("thanks"),)),  # 2: the answer
        NormalizedFinal((TextBlock("from the other tab"),)),  # 3: C, started in another tab, until released
    ], gated={0, 3})


def run_activity(base: str, token: str, shots: str | None, provider: GatedAdapter) -> list[str]:
    """V3-P80: other conversations at work or waiting, on the list and in the tab's title, drawn from the event
    stream (V3-P83 §8.1), which a hidden tab closes; one started in another tab appears; pasting files, text
    with a file, and text too long for a message."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "40" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        row = lambda cid: page.locator(f'#conversation-list .conv-row[data-id="{cid}"]')  # noqa: E731

        # V3-P83 §8.1: the event stream draws the activity; a hidden tab closes it, a shown one opens it again.
        streams: list[Any] = []
        ended: list[Any] = []
        reads: list[str] = []
        page.on("request", lambda r: streams.append(r) if r.url.endswith("/api/events")
                else reads.append(r.url) if r.url.endswith("/api/conversations/activity") else None)
        page.on("requestfailed", lambda r: ended.append(r) if r.url.endswith("/api/events") else None)
        page.on("requestfinished", lambda r: ended.append(r) if r.url.endswith("/api/events") else None)
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.wait_for_timeout(6000)
        assert len(streams) == 1 and not ended, (streams, ended)
        assert len(reads) <= 1, f"no 5 s reading while the stream is open: {reads}"
        visible = """state => { Object.defineProperty(document, 'visibilityState', {configurable: true,
            get: () => state}); document.dispatchEvent(new Event('visibilitychange')); }"""
        page.evaluate(visible, "hidden")
        page.wait_for_timeout(500)
        assert len(ended) == 1, "a hidden tab holds no event stream"
        page.evaluate(visible, "visible")
        page.wait_for_timeout(500)
        assert len(streams) == 2, "shown again, it opens a new one"
        a = page.locator(".conv-row.is-selected").get_attribute("data-id")
        page.fill("#composer-text", "work on A")
        page.press("#composer-text", "Enter")
        new_chat(page, "#nav-new-chat")
        b = page.locator(".conv-row.is-selected").get_attribute("data-id")
        expect(row(a).locator(".activity-dot.is-working")).to_be_visible(timeout=8000)
        if shots:
            page.screenshot(path=os.path.join(shots, "22-activity-working.png"))
        provider.gates[0].set()
        expect(row(a).locator(".activity-dot")).to_have_count(0, timeout=8000)

        # A asks; from B it shows as waiting, and the tab's title counts it.
        row(a).click()
        expect(page.locator(".item-assistant").last).to_contain_text("done with A", timeout=10000)
        page.fill("#composer-text", "ask me")
        page.press("#composer-text", "Enter")
        expect(page.locator("#question-panel")).to_be_visible(timeout=10000)
        row(b).click()
        expect(row(a).locator(".activity-dot.is-waiting")).to_be_visible(timeout=8000)
        expect(page).to_have_title(re.compile(r"^\(1\) "))
        if shots:
            page.screenshot(path=os.path.join(shots, "23-activity-waiting.png"))
        row(a).click()
        page.fill("#composer-text", "this one")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("thanks", timeout=10000)
        expect(row(a).locator(".activity-dot")).to_have_count(0, timeout=8000)
        expect(page).not_to_have_title(re.compile(r"^\("))

        # Started in another tab: it appears here, working.
        other = context.new_page()
        other.goto(base)
        new_chat(other, "#nav-new-chat")
        c = other.locator(".conv-row.is-selected").get_attribute("data-id")
        other.fill("#composer-text", "from elsewhere")
        other.press("#composer-text", "Enter")
        expect(row(c).locator(".activity-dot.is-working")).to_be_visible(timeout=12000)
        provider.gates[3].set()
        expect(row(c).locator(".activity-dot")).to_have_count(0, timeout=8000)
        other.close()

        # Paste (§4): a file alone is attached under a name of its own; text with a file is text; text too
        # long for a message goes as a .txt.
        row(b).click()
        paste = """([text, file]) => {
            const data = new DataTransfer();
            if (text !== null) data.setData("text/plain", text);
            if (file) data.items.add(new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" }));
            return document.getElementById("composer-text").dispatchEvent(
                new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
        }"""
        chips = page.locator("#attach-list .attach-chip")
        assert page.evaluate(paste, [None, True]) is False  # taken: attached
        expect(chips).to_have_count(1)
        expect(chips.first).to_contain_text(re.compile(r"^pasted-\d{8}-\d{6}\.png · 4 bytes"))  # uploaded
        assert page.evaluate(paste, ["a caption", True]) is True  # left to the browser: text
        expect(chips).to_have_count(1)
        assert page.evaluate(paste, ["x" * 32_001, False]) is False
        expect(chips).to_have_count(2)
        expect(chips.nth(1)).to_contain_text(re.compile(r"^pasted-\d{8}-\d{6}\.txt"))
        expect(page.locator("#status-bar")).to_contain_text("attached as a file")
        assert page.evaluate(paste, ["y" * 100, False]) is True  # short text: as always
        if shots:
            page.screenshot(path=os.path.join(shots, "24-paste.png"))
        browser.close()
    return errors


def run_polish(base: str, token: str, shots: str | None) -> list[str]:
    """Small things: Undo right after a move to the trash; the jump to the latest; the length count near the
    limit; Retry on a failed upload."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load" not in m.text
                else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        expect(page.locator(".login-card")).to_be_visible()  # the sign-in card, in the middle
        box = page.locator(".login-card").bounding_box()
        assert box is not None and abs((box["x"] + box["width"] / 2) - 640) < 140, box
        if shots:
            page.screenshot(path=os.path.join(shots, "26-sign-in.png"))
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        cid = page.locator(".conv-row.is-selected").get_attribute("data-id")
        page.fill("#composer-text", "a long one")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant").last).to_contain_text("Paragraph 40", timeout=10000)
        # A screen reader hears the new answer (its start), and not the history when a conversation opens.
        expect(page.locator("#announce")).to_contain_text("HIMARI answered: Paragraph 1:")
        page.evaluate("document.getElementById('announce').textContent = ''")
        page.reload()
        expect(page.locator(".item-assistant").last).to_contain_text("Paragraph 40", timeout=10000)
        page.wait_for_timeout(500)
        expect(page.locator("#announce")).to_have_text("")

        # The jump to the latest: shown once scrolled up, and it goes back down.
        jump = page.locator("#jump-latest")
        expect(jump).to_be_hidden()
        page.evaluate("document.getElementById('timeline').scrollTop = 0")
        expect(jump).to_be_visible()
        if shots:
            page.screenshot(path=os.path.join(shots, "25-jump.png"))
        page.click("#nav-home")  # the composer moves to Home: the button does not come along (review B2)
        expect(page.locator("#surface-home")).to_be_visible()
        expect(jump).to_be_hidden()
        page.locator(f'#conversation-list .conv-row[data-id="{cid}"]').click()
        expect(page.locator(".item-assistant").last).to_contain_text("Paragraph 40", timeout=10000)
        page.evaluate("document.getElementById('timeline').scrollTop = 0")
        expect(jump).to_be_visible()
        jump.click()
        expect(jump).to_be_hidden(timeout=5000)

        # The count near the limit, and over it.
        hint = page.locator("#composer-hint")
        page.fill("#composer-text", "x" * 30_500)
        expect(hint).to_have_text("30,500 / 32,000 characters")
        page.wait_for_timeout(1500)  # the conversation's own refreshes do not wipe it (review B1)
        page.click("#chat-menu-toggle")  # a detail read
        page.click("#chat-menu-toggle")
        expect(hint).to_have_text("30,500 / 32,000 characters")
        page.fill("#composer-text", "x" * 32_001)
        expect(hint).to_contain_text("too long to send")
        page.fill("#composer-text", "short")
        expect(hint).to_have_text("")

        # The list's heading for today.
        expect(page.locator("#conversation-list .conversation-group").first).to_have_text("Today")
        # While a file uploads, the hint says why Send waits; Retry: an upload that failed goes again from its chip.
        held: list[Any] = []
        page.route("**/uploads?*", lambda route: held.append(route))
        page.set_input_files("#composer-files", files=[{"name": "wait.txt", "mimeType": "text/plain", "buffer": b"w"}])
        expect(page.locator("#composer-hint")).to_contain_text("Uploading 1 file — Send waits for it")
        while not held:
            page.wait_for_timeout(50)
        held[0].abort()
        page.unroute("**/uploads?*")
        page.locator("#attach-list .attach-chip .attach-chip-remove").first.click()
        expect(page.locator("#composer-hint")).not_to_contain_text("Uploading")
        page.route("**/uploads?*", lambda route: route.abort())
        a_file = {"name": "note.txt", "mimeType": "text/plain", "buffer": b"hello"}
        page.set_input_files("#composer-files", files=[a_file])
        chip = page.locator("#attach-list .attach-chip")
        expect(chip).to_contain_text("did not finish", timeout=10000)
        page.unroute("**/uploads?*")
        chip.locator(".attach-chip-retry").click()
        expect(chip).to_have_text(re.compile(r"note\.txt · 5 bytes"), timeout=10000)
        expect(page.locator("#attach-list .attach-chip")).to_have_count(1)

        # Shortcuts: Cmd/Ctrl+Shift+O opens a new chat; Shift+Esc goes to the message box.
        page.locator("#timeline").click()
        page.keyboard.press("Shift+Escape")
        expect(page.locator("#composer-text")).to_be_focused()
        page.locator("#timeline").click()
        page.keyboard.press("Control+Shift+O")
        expect(page.locator("#conversation-list .conv-row")).to_have_count(2, timeout=10000)
        expect(page.locator("#composer-text")).to_be_focused()
        assert page.locator("#nav-new-chat").get_attribute("title").startswith("New chat (")
        page.click("#chat-menu-toggle")  # with a confirmation up, the shortcut does nothing (review B2)
        page.click("#chat-menu-delete")
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.keyboard.press("Control+Shift+O")
        page.wait_for_timeout(500)
        expect(page.locator("#conversation-list .conv-row")).to_have_count(2)
        page.click("#confirm-cancel")
        page.locator(f'#conversation-list .conv-row[data-id="{cid}"]').click()
        expect(page.locator(".item-assistant").last).to_contain_text("Paragraph 40", timeout=10000)

        # Undo right after a move to the trash: back, and open again.
        page.click("#chat-menu-toggle")
        page.click("#chat-menu-delete")
        page.click("#confirm-ok")
        expect(page.locator("#status-bar")).to_contain_text("Moved")
        page.locator("#status-bar .status-action").click()
        expect(page.locator(f'#conversation-list .conv-row.is-selected[data-id="{cid}"]')).to_be_visible()
        expect(page.locator("#status-bar")).to_contain_text("Restored")
        browser.close()
    return errors


CODE_ANSWER = "Here it is:\n\n```python\nprint('hi')\n```\n\nAnd **done**."
MOOD_MARK = "[[mood:happy]]"  # V3-P83 §7.2: the model writes it first; the page shows and copies the text without it


def run_addresses(base: str, token: str, shots: str | None) -> list[str]:
    """V3-P79: each surface at its address — Back and Forward, a refused leave keeping the address, an address
    opened before signing in, one that is not there, the tab's title, links in the list, sign-out — and Copy
    on an answer and on its code."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 860},
                                      permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "40" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        path = lambda: page.evaluate("location.pathname")  # noqa: E731

        def sign_in(on: Any) -> None:
            on.fill("#login-token", token)
            on.click("#login-submit")

        page.goto(base)
        sign_in(page)
        expect(page.locator("#surface-home")).to_be_visible()
        new_chat(page)
        a = page.locator(".conv-row.is-selected").get_attribute("data-id")
        assert path() == f"/c/{a}"
        page.fill("#composer-text", "first")
        page.press("#composer-text", "Enter")
        answer = page.locator(".item-assistant").last
        expect(answer).to_contain_text("done", timeout=10000)
        assert "[[mood" not in answer.inner_text(), "the mood mark is not shown"
        expect(page).to_have_title(re.compile(r".+ · HIMARI$"))

        # Copy: the code alone from its block, the Markdown as written from the answer (§5).
        code_copy = answer.locator(".rich-code-copy")
        code_copy.click()
        expect(code_copy).to_have_text("Copied")
        assert page.evaluate("navigator.clipboard.readText()") == "print('hi')"
        answer.locator(".item-actions .copy-button").click()
        assert page.evaluate("navigator.clipboard.readText()") == CODE_ANSWER
        expect(code_copy).to_have_text("Copy", timeout=4000)
        if shots:
            page.screenshot(path=os.path.join(shots, "21-copy.png"))

        new_chat(page, "#nav-new-chat")
        b = page.locator(".conv-row.is-selected").get_attribute("data-id")
        assert a != b and path() == f"/c/{b}"
        assert page.locator(f'#conversation-list .conv-row[data-id="{a}"]').get_attribute("href") == f"/c/{a}"
        page.evaluate("history.back()")
        expect(page.locator(f'#conversation-list .conv-row.is-selected[data-id="{a}"]')).to_be_visible()
        assert path() == f"/c/{a}"
        expect(page.locator(".item-assistant").last).to_contain_text("done")
        page.evaluate("history.forward()")
        expect(page.locator(f'#conversation-list .conv-row.is-selected[data-id="{b}"]')).to_be_visible()

        # A leave refused on Back keeps the surface and its address (§3.2).
        page.click("#nav-appearance")
        page.click("#nav-instructions")
        expect(page.locator("#surface-instructions")).to_be_visible()
        assert path() == "/settings/instructions"
        page.fill("#instructions-text", "not saved yet")
        page.evaluate("history.back()")
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.click("#confirm-cancel")
        expect(page.locator("#surface-instructions")).to_be_visible()
        page.wait_for_timeout(300)
        assert path() == "/settings/instructions"
        # The refused Back's entry now holds this surface's address (§3.2), so Back again goes one further.
        page.evaluate("history.back()")
        page.click("#confirm-ok")
        expect(page.locator(f'#conversation-list .conv-row.is-selected[data-id="{a}"]')).to_be_visible()
        assert path() == f"/c/{a}"

        # An address opened before signing in is where the sign-in goes, with the list beside it (§3.3).
        other = browser.new_context(viewport={"width": 1280, "height": 860}).new_page()
        other.goto(f"{base}/c/{a}")
        expect(other.locator("#surface-login")).to_be_visible()
        sign_in(other)
        expect(other.locator(f'#conversation-list .conv-row.is-selected[data-id="{a}"]')).to_be_visible()
        expect(other.locator(".item-assistant").last).to_contain_text("done", timeout=10000)
        assert other.evaluate("location.pathname") == f"/c/{a}"
        # One that is not here: Home, told, and Back does not go to it.
        other.goto(f"{base}/c/conv_999999")
        expect(other.locator("#surface-home")).to_be_visible()
        expect(other.locator("#status-bar")).to_contain_text("not here")
        assert other.evaluate("location.pathname") == "/"
        other.context.close()

        # Sign out leaves no conversation's address (§3.3).
        page.click("#nav-appearance")
        page.click("#nav-sign-out")
        expect(page.locator("#surface-login")).to_be_visible()
        assert path() == "/"
        browser.close()
    return errors


LONG_ANSWER = "\n\n".join(f"Paragraph {n}: the sea keeps moving under a long grey sky." for n in range(1, 41))


def streaming_script() -> ScriptedAdapter:
    """V3-P73: a long streamed answer, one that hangs part way (to be
    stopped), then a whole one; each starts after a moment of "thinking"."""
    return ScriptedAdapter([NormalizedFinal((TextBlock(LONG_ANSWER),)),
                            StreamCut(tuple("Half of an answer that never ends. " for _ in range(40)), asyncio.Event()),
                            NormalizedFinal((TextBlock("A whole answer."),))],
                           streaming=True, piece_delay=0.01, first_delay=1.5)


def run_streaming(base: str, token: str, shots: str | None) -> list[str]:
    """V3-P73 §7: the answer being written, the thinking line, a stopped
    answer, and an answer that still arrives when the live view is blocked."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        new_chat(page)
        page.fill("#composer-text", "Tell me about the sea")
        page.press("#composer-text", "Enter")
        writing = page.locator(".item-assistant.is-live")
        thinking = writing.locator(".thinking")
        expect(thinking).to_be_visible(timeout=5000)
        expect(thinking).to_have_class(re.compile(r"\bis-sun\b"))
        assert writing.locator(".thinking-word").inner_text().endswith("…")
        expect(writing.locator(".thinking-time")).to_have_text(re.compile(r"^· \d+s$"))
        expect(thinking.locator(".visually-hidden")).to_have_text("HIMARI is thinking")
        if shots:
            page.screenshot(path=os.path.join(shots, "10-thinking.png"))
        # A theme that sets --thinking-style: hidamari switches it at once.
        page.evaluate("""() => { const root = document.documentElement;
            root.style.setProperty('--thinking-style', 'hidamari'); root.dataset.theme = 'warm'; }""")
        expect(thinking).to_have_class(re.compile(r"\bis-hidamari\b"))
        page.evaluate("""() => { const root = document.documentElement;
            root.style.removeProperty('--thinking-style'); root.dataset.theme = 'classic'; }""")
        # The text arrives as it is written, in one item.
        expect(writing.locator(".rich-text")).to_contain_text("Paragraph 1:", timeout=10000)
        page.wait_for_function("() => (document.querySelector('.is-live .rich-text') || {textContent: ''})"
                               ".textContent.length > 600")
        assert writing.count() == 1 and "Paragraph 40:" not in writing.inner_text()
        # Followed while at the bottom; reading higher up, the view stays put while it writes.
        page.wait_for_function("() => (t => t.scrollHeight > t.clientHeight + 300)(document.getElementById('timeline'))",
                               timeout=10000)
        assert page.evaluate("(t => t.scrollHeight - t.scrollTop - t.clientHeight < 80)"
                             "(document.getElementById('timeline'))")
        page.evaluate("() => { document.getElementById('timeline').scrollTop = 0; }")
        page.wait_for_timeout(400)
        assert page.evaluate("document.getElementById('timeline').scrollTop") < 50
        assert writing.count() == 1
        # The finished answer replaces it, once.
        done = page.locator(".item-assistant:not(.is-live)")
        expect(done).to_contain_text("Paragraph 40:", timeout=20000)
        expect(writing).to_have_count(0)
        assert done.count() == 1
        expect(page.locator("#topbar-state-dot")).not_to_have_class(re.compile("is-active"), timeout=10000)
        # Stopped part way: it stays dimmed, with what it means.
        page.fill("#composer-text", "Again")
        page.press("#composer-text", "Enter")
        expect(writing.locator(".rich-text")).to_contain_text("Half of an answer", timeout=10000)
        page.click("#composer-cancel")
        stopped = page.locator(".item-assistant.is-live.is-stopped")
        expect(stopped).to_be_visible(timeout=10000)
        expect(stopped.locator(".live-note")).to_have_text(
            "Stopped — this part was not kept, and HIMARI will not see it.")
        if shots:
            stopped.scroll_into_view_if_needed()
            page.screenshot(path=os.path.join(shots, "11-stopped.png"))
        # With the live view blocked, the answer still arrives, whole.
        page.route("**/live", lambda route: route.abort())
        page.fill("#composer-text", "Once more")
        page.press("#composer-text", "Enter")
        expect(page.locator(".item-assistant:not(.is-live)").last).to_have_text(re.compile("A whole answer."),
                                                                               timeout=10000)
        expect(page.locator(".item-assistant.is-live:not(.is-stopped)")).to_have_count(0)
        assert page.locator(".item-user").last.inner_text().endswith("Once more")
        assert stopped.evaluate("n => n.nextElementSibling !== null")  # it stayed where it was
        page.unroute("**/live")
        page.reload()
        page.locator(".conv-row").first.click()
        expect(page.locator(".item-assistant:not(.is-live)").last).to_have_text(re.compile("A whole answer."),
                                                                               timeout=10000)
        expect(page.locator(".item-assistant.is-live")).to_have_count(0)  # not a record: gone on reload
        browser.close()
    return errors


def http(base: str, method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None = None
         ) -> tuple[int, dict[str, Any]]:
    """What a native device app sends: no cookie, no Origin (V3-P83 §4.1)."""
    import json
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={**headers, **({"content-type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def run_devices(base: str, token: str, shots: str | None) -> list[str]:
    """V3-P83 §4.1: add a device, read its code once, pair it as an app would, rename and revoke it."""
    import base64
    import secrets

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="en-US")
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("#login-token", token)
        page.click("#login-submit")
        expect(page.locator("#surface-home")).to_be_visible()
        page.click("#nav-appearance")
        page.click("#nav-devices")
        expect(page.locator("#surface-devices h1")).to_have_text("Devices")
        assert page.url.endswith("/settings/devices"), page.url
        expect(page.locator("#devices-empty")).to_be_visible()
        page.fill("#devices-add-name", "Desk")
        page.click("#devices-add button[type=submit]")
        expect(page.locator("#devices-code")).to_be_visible()
        expect(page.locator("#devices-code-name")).to_have_text("Desk")
        code = page.locator("#devices-code-value").inner_text()
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code), code
        rows = page.locator("#devices-list > li")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text("Waiting for its code until")
        if shots:
            page.screenshot(path=os.path.join(shots, "30-devices-code.png"))

        device_token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        status, paired = http(base, "POST", "/api/devices/pair", {"authorization": f"Pairing {code}"},
                              {"token": device_token})
        assert (status, paired) == (200, {"device_id": "d1", "name": "Desk"}), (status, paired)
        status, started = http(base, "GET", "/api/device", {"authorization": f"Bearer {device_token}"})
        assert status == 200 and started["device"]["name"] == "Desk", (status, started)

        page.reload()  # the code is shown once: gone after a reload
        expect(page.locator("#surface-devices")).to_be_visible()
        expect(page.locator("#devices-code")).to_be_hidden()
        expect(rows.first).to_contain_text("Last seen")

        # V3-P88 §8: what the app tells, shown as the server knows it; the page follows the notices
        import urllib.request

        bearer = {"authorization": f"Bearer {device_token}"}

        def hold_stream() -> None:  # the app's event stream: connected needs it open
            with urllib.request.urlopen(urllib.request.Request(base + "/api/events", headers=bearer)) as stream:
                while stream.read(1):
                    pass

        threading.Thread(target=hold_stream, daemon=True).start()
        time.sleep(0.5)
        status, _ = http(base, "POST", "/api/device/presence", bearer, {
            "idle_seconds": 0, "session": "active", "mic_or_camera": True,
            "activity": {"category": "coding", "app": "Visual Studio Code", "for_seconds": 2700},
            "media": {"title": "Song", "artist": "Singer"}})
        assert status == 204, status
        expect(rows.first).to_contain_text("coding in Visual Studio Code for 45m", timeout=15000)
        expect(rows.first).to_contain_text("playing Song – Singer")
        expect(rows.first).to_contain_text("microphone or camera in use")
        expect(page.locator("#devices-signals-note")).to_be_visible()
        status, _ = http(base, "POST", "/api/device/presence", bearer, {"idle_seconds": 0, "session": "locked"})
        assert status == 204, status
        expect(rows.first.locator(".device-signals")).to_have_text("Locked", timeout=15000)  # the window's end
        expect(page.locator("#devices-quiet-state")).to_contain_text("HIMARI may speak first")
        page.click("#devices-quiet-hour")
        expect(page.locator("#devices-quiet-state")).to_contain_text("Quiet until")
        status, started = http(base, "GET", "/api/device", bearer)
        assert status == 200 and started["quiet_until"], started  # the user's, for every device
        if shots:
            page.screenshot(path=os.path.join(shots, "30b-devices-signals.png"))
        page.click("#devices-quiet-clear")
        expect(page.locator("#devices-quiet-state")).to_contain_text("HIMARI may speak first")
        expect(page.locator("#devices-quiet-clear")).to_be_hidden()
        rows.first.get_by_role("button", name="Rename Desk").click()
        rows.first.locator("input").fill("Big desk")
        rows.first.locator("button[type=submit]").click()
        expect(page.locator("#status-bar")).to_contain_text("Renamed", timeout=5000)
        expect(rows.first).to_contain_text("Big desk")
        rows.first.get_by_role("button", name="Revoke Big desk").click()
        expect(page.locator("#confirm-title")).to_have_text("Revoke “Big desk”?")
        page.click("#confirm-ok")
        expect(page.locator("#status-bar")).to_contain_text("Revoked", timeout=5000)
        expect(rows).to_have_count(0)
        expect(page.locator("#devices-empty")).to_be_visible()
        status, _ = http(base, "GET", "/api/device", {"authorization": f"Bearer {device_token}"})
        assert status == 401, status
        if shots:
            page.screenshot(path=os.path.join(shots, "31-devices-empty.png"))
        browser.close()
    return errors


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
            # memory and search off: the budget was fitted to a prompt without their Tools (V3-P71, V3-P72)
            server = serve(small, PORT + 1, provider, context_tokens=2800, memory=False, conversation_search=False)
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
            provider = ScriptedAdapter([NormalizedFinal((TextBlock("네, 알겠습니다."),))] * 4)
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
        if "passes" in only:  # V3-P85 part 4
            passes = os.path.join(tmp, "passes")
            server = serve(passes, PORT + 15, ScriptedAdapter([NormalizedFinal((TextBlock("ok."),))] * 4))
            try:
                errors += run_passes(f"http://127.0.0.1:{PORT + 15}",
                                     Path(passes, "control", "operator.secret").read_text().strip(),
                                     args.screenshots, passes)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "drafts" in only:
            kept = os.path.join(tmp, "drafts")
            server = serve(kept, PORT + 10, ScriptedAdapter([NormalizedFinal((TextBlock("noted"),))]))
            try:
                errors += run_drafts(f"http://127.0.0.1:{PORT + 10}",
                                     Path(kept, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "addresses" in only:
            addressed = os.path.join(tmp, "addresses")
            server = serve(addressed, PORT + 11, ScriptedAdapter([NormalizedFinal((TextBlock(MOOD_MARK + CODE_ANSWER),))]))
            try:
                errors += run_addresses(f"http://127.0.0.1:{PORT + 11}",
                                        Path(addressed, "control", "operator.secret").read_text().strip(),
                                        args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "activity" in only:
            active = os.path.join(tmp, "activity")
            provider = activity_script()
            server = serve(active, PORT + 12, provider)
            try:
                errors += run_activity(f"http://127.0.0.1:{PORT + 12}",
                                       Path(active, "control", "operator.secret").read_text().strip(),
                                       args.screenshots, provider)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "devices" in only:
            paired = os.path.join(tmp, "devices")
            server = serve(paired, PORT + 14)
            try:
                errors += run_devices(f"http://127.0.0.1:{PORT + 14}",
                                      Path(paired, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "polish" in only:
            polished = os.path.join(tmp, "polish")
            server = serve(polished, PORT + 13, ScriptedAdapter([NormalizedFinal((TextBlock(LONG_ANSWER),))] * 2))
            try:
                errors += run_polish(f"http://127.0.0.1:{PORT + 13}",
                                     Path(polished, "control", "operator.secret").read_text().strip(), args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
        if "streaming" in only:
            streaming = os.path.join(tmp, "streaming")
            server = serve(streaming, PORT + 9, streaming_script())
            try:
                errors += run_streaming(f"http://127.0.0.1:{PORT + 9}",
                                        Path(streaming, "control", "operator.secret").read_text().strip(),
                                        args.screenshots)
            finally:
                server.should_exit = True
                time.sleep(0.5)
    print("UI smoke: OK" if not errors else f"UI smoke: console errors {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
