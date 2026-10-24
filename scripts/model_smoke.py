"""Browser smoke test of a conversation's model (V3-P64 §7) on the real Web
server (opt-in evidence, not part of pytest).

Starts the page server on a temporary private data root with a scripted
provider that offers two models: the default "scripted-model" (no reasoning
efforts) and "big" (low/high). Headless Chromium drives the conversation menu:

1. the menu shows the model, following the default; no reasoning choice for
   a model without efforts;
2. choosing "big" shows its efforts; choosing one is stored as made;
3. the next answer is written by "big" at that effort, and its header names
   the model, because it changed since the answer before; the one after does not;
4. a change while a turn runs says it applies from the next message, and
   the running turn keeps its model;
5. "Default" follows the server's default model again;
6. a step of Tool calls without text, by a changed model, names it above
   the calls, and the answer after it does not (PR #53 review B1);
7. the narrow layout shows the menu without horizontal scroll.

Step 4 also checks that the note shows only after a change made during the
turn (PR #53 review C1).

Requires: pip install playwright (a Chromium for it, or --chrome PATH)
Usage:    python scripts/model_smoke.py [--chrome PATH] [--screenshots DIR]
Exit status 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest, NormalizedFinal  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.api import ProviderCapabilities  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"


class TwoModels(ScriptedAdapter):
    """"big" declares efforts; every answer can be held back to keep a turn running."""

    def __init__(self) -> None:
        write = ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": "a.txt", "content": "hi"})
        super().__init__([*(NormalizedFinal((TextBlock(f"answer {i}"),)) for i in range(1, 6)),
                          NormalizedToolCalls((write,)), NormalizedFinal((TextBlock("saved"),))])
        self.hold = threading.Event()
        self.hold.set()

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        if model_id == "big":
            return ProviderCapabilities(model_id, frozenset({"low", "high"}), True, context_tokens=300_000,
                                        max_output_tokens=60_000)
        return super().describe_capabilities(model_id)

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        while not self.hold.is_set():
            await asyncio.sleep(0.05)
        return await super().invoke_prepared(request)


def serve(root: str, provider: TwoModels) -> None:
    config = WebConfig("127.0.0.1", PORT, frozenset({"127.0.0.1"}), frozenset({BASE}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        return await Application.open(root, provider, model_id="scripted-model", sandbox=False,
                                      models=[("scripted-model", None), ("big", "low")])

    server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    secret = Path(root, "control", "operator.secret")
    for _ in range(100):
        if server.started and secret.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("the server did not start")


def sent_models(provider: TwoModels) -> list[tuple[str, str | None]]:
    return [(i.request_options.model_id, i.request_options.reasoning_effort) for i in provider.prepared_inputs]


def run(token: str, provider: TwoModels, chrome: str | None, shots: str | None, results: list[str]) -> None:
    errors: list[str] = []

    def check(name: str, ok: bool, seen: object = None) -> None:
        results.append(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (saw: {seen!r})"))

    def shot(page: Page, name: str) -> None:
        if shots:
            page.screenshot(path=os.path.join(shots, f"{name}.png"))

    def cookie() -> dict[str, str]:
        return {"cookie": "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())}

    def stored(cid: str) -> object:
        return httpx.get(f"{BASE}/api/conversations/{cid}/model", headers=cookie()).json()["chosen"]

    def send(text: str, added: int = 1) -> None:
        count = page.locator(".item-assistant").count()
        box.fill(text)
        box.press("Enter")
        expect(page.locator(".item-assistant")).to_have_count(count + added, timeout=10000)
        expect(page.locator("#composer-send")).to_be_enabled(timeout=10000)

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#home-new-chat")
        box = page.locator("#composer-text")
        expect(box).to_be_visible(timeout=5000)
        cid = httpx.get(f"{BASE}/api/conversations", headers=cookie()).json()["items"][0]["id"]
        model, effort = page.locator("#chat-menu-model-select"), page.locator("#chat-menu-effort-select")

        # 1. The default, and no effort for a model without efforts.
        page.click("#chat-menu-toggle")
        expect(page.locator("#chat-menu-model")).to_be_visible(timeout=5000)
        check("1: the model follows the default",
              model.input_value() == "" and model.locator("option").all_text_contents()
              == ["Default (scripted-model)", "scripted-model", "big"],
              model.locator("option").all_text_contents())
        check("1: no reasoning choice for a model without efforts", page.locator("#chat-menu-effort-field").is_hidden())
        shot(page, "1-default")
        page.click("#chat-menu-toggle")
        send("one")

        # 2. Choose big, then an effort.
        page.click("#chat-menu-toggle")
        model.select_option("big")
        expect(page.locator("#chat-menu-effort-field")).to_be_visible(timeout=5000)
        check("2: big shows its efforts",
              effort.locator("option").all_text_contents() == ["Default (low)", "low", "high"],
              effort.locator("option").all_text_contents())
        effort.select_option("high")
        page.wait_for_timeout(300)
        check("2: the choice is stored as made", stored(cid) == {"model": "big", "effort": "high"}, stored(cid))
        shot(page, "2-big-high")
        page.click("#chat-menu-toggle")

        # 3. The next answer is big's, named; the one after is not.
        send("two")
        send("three")
        labels = page.locator(".item-assistant .item-kind").all_text_contents()
        check("3: the request went to big at high", sent_models(provider)[1:] == [("big", "high"), ("big", "high")],
              sent_models(provider))
        check("3: only the answer after the change names its model", labels == ["HIMARI", "HIMARIbig", "HIMARI"],
              labels)
        shot(page, "3-labels")

        # 4. A change while a turn runs: noted, and the turn keeps its model.
        provider.hold.clear()
        box.fill("four")
        box.press("Enter")
        expect(page.locator("#composer-send")).to_be_disabled(timeout=5000)
        page.click("#chat-menu-toggle")
        page.wait_for_timeout(300)
        check("4: running without a change, no note", page.locator("#chat-menu-model-note").is_hidden())
        model.select_option("scripted-model")
        expect(page.locator("#chat-menu-model-note")).to_have_text("Applies from the next message.", timeout=5000)
        shot(page, "4-running")
        provider.hold.set()
        expect(page.locator("#composer-send")).to_be_enabled(timeout=10000)
        expect(page.locator("#chat-menu-model-note")).to_be_hidden(timeout=5000)
        page.click("#chat-menu-toggle")
        check("4: the running turn kept big", sent_models(provider)[3] == ("big", "high"), sent_models(provider))
        send("five")
        check("4: the next turn uses the new choice", sent_models(provider)[4] == ("scripted-model", None),
              sent_models(provider))

        # 5. Back to the default.
        page.click("#chat-menu-toggle")
        model.select_option("")
        page.wait_for_timeout(300)
        check("5: Default follows the server's default again", stored(cid) is None, stored(cid))

        # 6. A step of calls only, by a changed model (B1).
        model.select_option("big")
        page.wait_for_timeout(300)
        page.click("#chat-menu-toggle")
        send("six")  # intermediate calls now fold into the work disclosure
        last = page.locator(".item-work .work-model-name").last
        check("6: a step of calls only names its changed model",
              last.text_content() == "big", last.text_content())
        labels = page.locator(".item-assistant .item-kind").all_text_contents()
        check("6: the answer after it does not", labels[-1] == "HIMARI", labels)
        shot(page, "6-step")

        # 7. Narrow layout.
        page.set_viewport_size({"width": 380, "height": 800})
        page.wait_for_timeout(800)
        page.click("#mobile-model-toggle")
        model.select_option("scripted-model")
        expect(page.locator("#chat-menu-effort-field")).to_be_hidden()
        model.select_option("big")
        expect(page.locator("#chat-menu-effort-field")).to_be_visible(timeout=5000)
        shot(page, "7-narrow")
        check("7: the narrow layout has no horizontal scroll",
              bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")))

        # Native modal focus, dismissal and the one shared control tree.
        effort.select_option("high")
        expect(page.locator("#mobile-model-summary")).to_have_text("big · high")
        check("8: only one model selector exists", model.count() == 1)
        page.keyboard.press("Escape")
        expect(page.locator("#model-dialog")).not_to_be_visible()
        check("8: Escape restores focus", page.locator("#mobile-model-toggle").evaluate("n => n === document.activeElement"))
        page.click("#mobile-model-toggle")
        page.mouse.click(5, 5)
        expect(page.locator("#model-dialog")).not_to_be_visible()
        page.click("#mobile-model-toggle")
        model.select_option("")
        expect(page.locator("#chat-menu-effort-field")).to_be_hidden()
        page.click("#model-dialog-done")
        check("8: Done closes and Default still follows server", stored(cid) is None)

        for width in (320, 360, 390, 430, 768, 1099):
            page.set_viewport_size({"width": width, "height": 844})
            bar = page.locator("#topbar").bounding_box()
            targets = [page.locator(s).bounding_box() for s in
                       ("#sidebar-toggle", "#mobile-model-toggle", "#app-fullscreen", "#chat-menu-toggle")]
            check(f"9: {width}px header is 56px with 44px targets and no overflow",
                  bar is not None and bar["height"] == 56 and all(b and b["height"] >= 44 and b["width"] >= 44 for b in targets)
                  and page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (bar, targets))
        page.click("#mobile-model-toggle")
        page.set_viewport_size({"width": 1100, "height": 900})
        expect(page.locator("#model-dialog")).not_to_be_visible()
        expect(model).to_be_visible()
        check("10: desktop reuses the same controls inline", model.evaluate("n => n.closest('#topbar') !== null"))
        page.set_viewport_size({"width": 390, "height": 844})

        # Actual Fullscreen API; browser-driven exits must update the button.
        page.click("#app-fullscreen")
        page.wait_for_function("() => document.fullscreenElement === document.documentElement")
        expect(page.locator("#app-fullscreen")).to_have_attribute("aria-pressed", "true")
        check("11: native fullscreen enters", page.evaluate("document.fullscreenElement === document.documentElement"))
        page.click("#app-fullscreen")
        page.wait_for_function("() => !document.fullscreenElement")
        page.click("#app-fullscreen")
        page.wait_for_function("() => !!document.fullscreenElement")
        page.evaluate("document.exitFullscreen()")
        expect(page.locator("#app-fullscreen")).to_have_attribute("aria-pressed", "false")
        check("11: external fullscreen exit restores button state", page.locator("#app-fullscreen").get_attribute("aria-label") == "Enter fullscreen")
        page.evaluate("document.documentElement.requestFullscreen = () => Promise.reject(new Error('denied'))")
        page.click("#app-fullscreen")
        expect(page.locator("#status-bar")).to_contain_text("Fullscreen is unavailable")
        check("11: rejection leaves normal UI and enabled button", page.locator("#app-fullscreen").is_enabled()
              and page.locator("#app-fullscreen").get_attribute("aria-pressed") == "false")
        page.evaluate("Object.defineProperty(document, 'fullscreenEnabled', {value: false, configurable: true})")
        page.dispatch_event("fullscreenchange")
        expect(page.locator("#app-fullscreen")).to_be_hidden()
        check("11: unsupported fullscreen is hidden", page.locator("#app-fullscreen").is_hidden())
        check("no uncaught errors in the page", errors == [], errors)
        browser.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", help="a Chromium (e.g. chrome-headless-shell) instead of Playwright's own")
    parser.add_argument("--screenshots", help="write screenshots into this directory")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "data")
        os.mkdir(root, 0o700)
        provider = TwoModels()
        serve(root, provider)
        token = Path(root, "control", "operator.secret").read_text().strip()
        results: list[str] = []
        try:
            run(token, provider, args.chrome, args.screenshots, results)
        except Exception as error:  # noqa: BLE001 — a step that could not go on fails; what passed stays shown
            results.append(f"FAIL  the run stopped after the checks above: {type(error).__name__}: "
                           f"{str(error).splitlines()[0]}")
    for line in results:
        print(line)
    failed = sum(line.startswith("FAIL") for line in results)
    print(f"model smoke: {'FAIL' if failed else 'OK'} ({len(results) - failed}/{len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
