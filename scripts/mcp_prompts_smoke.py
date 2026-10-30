"""Browser smoke test of server prompts (V3-P60 §4) on the real Web server
(opt-in evidence, not part of pytest).

Starts the page server on a temporary private data root, with a scripted
provider and the fake MCP server offering two prompts, one with arguments.
Headless Chromium drives the composer:

1. "/" lists the prompts of the servers on here; typing filters them;
   arrow keys move the choice; Escape closes the list;
2. a prompt with arguments asks for them (a required one is required);
   "Fill in" puts the server's text, under its source line, into the
   composer — nothing is sent;
3. the user edits it and sends it: the model gets exactly that text as the
   user's message;
4. a prompt without arguments is filled in at once by Enter;
5. a prompt HIMARI cannot give as text (an assistant message) is refused
   with the reason, and the composer is left as it was;
6. with the server off in the conversation, "/" lists nothing;
7. text that only starts with "//" is a message, not a prompt search;
8. an answer still on its way never replaces what the user did meanwhile
   (PR #43 review B1): Cancel, editing the message, sending one, or
   choosing another prompt drops it;
9. the narrow layout shows the list and the form without horizontal scroll.

Requires: pip install playwright (a Chromium for it, or --chrome PATH)
Usage:    python scripts/mcp_prompts_smoke.py [--chrome PATH] [--screenshots DIR]
Exit status 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from mcp_fake import URL, FakeMcpServer  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import TextBlock  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedFinal  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
COMMENTARY = "해설은 근거 중심으로, 수마다 판정과 이유를 함께 씁니다."


def fake_server() -> FakeMcpServer:
    fake = FakeMcpServer()
    fake.capabilities = {"tools": {}, "prompts": {}}
    fake.prompts = [
        {"name": "calliope_commentary", "title": "Commentary", "description": "How to write the commentary.",
         "arguments": [{"name": "game", "description": "The game id", "required": True},
                       {"name": "ply", "description": "Which ply"}]},
        {"name": "calliope_check", "description": "Check a commentary."},
        {"name": "calliope_broken", "description": "Has an assistant message."},
    ]
    fake.prompt_messages = {
        "calliope_commentary": [{"role": "user", "content": {"type": "text", "text": COMMENTARY}}],
        "calliope_check": [{"role": "user", "content": {"type": "text", "text": "Check the commentary."}}],
        "calliope_broken": [{"role": "assistant", "content": {"type": "text", "text": "I will."}}],
    }
    return fake


def serve(root: str, fake: FakeMcpServer, provider: ScriptedAdapter) -> None:
    config = WebConfig("127.0.0.1", PORT, frozenset({"127.0.0.1"}), frozenset({BASE}), False,
                       root, "scripted-model", None, False)

    async def opener() -> Application:
        return await Application.open(root, provider, model_id="scripted-model", sandbox=False,
                                      mcp_transport=fake.transport())

    server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    secret = Path(root, "control", "operator.secret")
    for _ in range(100):
        if server.started and secret.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("the server did not start")


def options(page: Page) -> list[str]:
    return page.locator("#prompt-list .prompt-option-name").all_text_contents()


def run(token: str, fake: FakeMcpServer, provider: ScriptedAdapter, chrome: str | None,
        shots: str | None, results: list[str]) -> None:
    errors: list[str] = []

    def check(name: str, ok: bool, seen: object = None) -> None:
        results.append(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (saw: {seen!r})"))

    def shot(page: Page, name: str) -> None:
        if shots:
            page.screenshot(path=os.path.join(shots, f"{name}.png"))

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        page.fill("#login-token", token)
        page.click("#login-submit")
        page.click("#home-new-chat")
        box = page.locator("#composer-text")

        # 1. The list, filtering, keys.
        box.fill("/")
        expect(page.locator("#prompt-picker")).to_be_visible(timeout=5000)
        check("1: / lists the prompts of the servers on here",
              options(page) == ["calliope · Commentary", "calliope · calliope_check", "calliope · calliope_broken"],
              options(page))
        shot(page, "1-list")
        box.fill("/check")
        check("1: typing filters the list", options(page) == ["calliope · calliope_check"], options(page))
        box.fill("/")
        box.press("ArrowDown")
        check("1: arrow keys move the choice",
              page.locator("#prompt-list [aria-selected=true] .prompt-option-name").text_content()
              == "calliope · calliope_check")
        box.press("Escape")
        check("1: Escape closes the list", page.locator("#prompt-picker").is_hidden())

        # 2. Arguments, then the text in the composer; nothing sent.
        box.fill("/comm")
        box.press("Enter")
        expect(page.locator("#prompt-form")).to_be_visible(timeout=5000)
        check("2: the arguments are asked for",
              page.locator("#prompt-form label").all_text_contents() == ["game (required)", "ply"],
              page.locator("#prompt-form label").all_text_contents())
        shot(page, "2-form")
        page.click("#prompt-form-submit")
        check("2: a required argument is required", page.locator("#prompt-form").is_visible()
              and fake.methods.count("prompts/get") == 0)
        page.fill("#prompt-argument-0", "g-9594")
        page.fill("#prompt-argument-1", "10")
        page.click("#prompt-form-submit")
        expect(page.locator("#prompt-form")).to_be_hidden(timeout=5000)
        expected = f"[MCP prompt calliope/calliope_commentary · game=g-9594 ply=10]\n\n{COMMENTARY}"
        check("2: Fill in puts the text under its source line into the composer", box.input_value() == expected,
              box.input_value())
        check("2: nothing was sent", provider.prepared_inputs == [] and page.locator(".item-user").count() == 0)
        shot(page, "2-filled")

        # 3. Edited and sent: exactly that text is the user's message.
        box.press("Control+End")
        box.type(" 짧게 써 주세요.")
        sent = box.input_value()
        box.press("Enter")
        expect(page.locator(".item-user").first).to_be_visible(timeout=10000)
        for _ in range(100):
            if provider.prepared_inputs:
                break
            time.sleep(0.05)
        given = provider.prepared_inputs[0].conversation[-1].blocks[-1].text if provider.prepared_inputs else None  # type: ignore[union-attr]
        check("3: the model gets exactly the text the user sent", given == sent, given)

        # 4. No arguments: filled in at once.
        expect(page.locator("#composer-send")).to_be_enabled(timeout=10000)
        box.fill("/calliope_check")
        box.press("Enter")
        expect(box).to_have_value("[MCP prompt calliope/calliope_check]\n\nCheck the commentary.", timeout=5000)
        check("4: a prompt without arguments is filled in by Enter", True)

        # 5. Refused, with the reason; the composer stays.
        box.fill("/broken")
        box.press("Enter")
        expect(page.locator("#status-bar")).to_contain_text("assistant message", timeout=5000)
        check("5: a prompt that cannot be given as text is refused with the reason", box.input_value() == "/broken",
              box.input_value())

        # 6. The server off here: nothing listed.
        page.click("#chat-menu-toggle")
        page.locator("#chat-menu-tools-list input[type=checkbox]").first.uncheck()
        page.click("#chat-menu-toggle")
        page.wait_for_timeout(500)
        box.fill("/")
        page.wait_for_timeout(300)
        check("6: with the server off here, / lists nothing", page.locator("#prompt-picker").is_hidden())
        page.click("#chat-menu-toggle")
        page.locator("#chat-menu-tools-list input[type=checkbox]").first.check()
        page.click("#chat-menu-toggle")
        page.wait_for_timeout(500)

        # 7. "//" is a message.
        box.fill("//comm")
        page.wait_for_timeout(200)
        check("7: // does not search prompts", page.locator("#prompt-picker").is_hidden())

        # 8. A slow server: what the user did meanwhile wins (PR #43 review B1).
        def settle() -> None:
            page.wait_for_timeout(2500)  # past every delayed answer

        fake.prompt_delay = {"calliope_commentary": 1.5, "calliope_check": 1.5}
        box.fill("/comm")
        box.press("Enter")
        page.fill("#prompt-argument-0", "g-1")
        page.click("#prompt-form-submit")
        page.click("#prompt-form-cancel")
        box.fill("my own words")
        settle()
        check("8: after Cancel, a late answer does not replace the message", box.input_value() == "my own words",
              box.input_value())
        box.fill("/calliope_check")
        box.press("Enter")
        box.press("End")
        box.type(" and more")
        settle()
        check("8: after an edit, a late answer does not replace it", box.input_value() == "/calliope_check and more",
              box.input_value())
        box.fill("/calliope_check")
        box.press("Enter")  # chosen: the answer is on its way
        box.press("Escape")  # abandons it, like Cancel
        settle()
        check("8: after Escape, a late answer does not fill the box", box.input_value() == "/calliope_check",
              box.input_value())
        expect(page.locator("#composer-send")).to_be_enabled(timeout=10000)
        box.fill("/calliope_check")
        box.press("Enter")
        box.fill("a message of my own")
        box.press("Enter")
        settle()
        check("8: after sending a message, a late answer does not fill the box", box.input_value() == "",
              box.input_value())
        given = provider.prepared_inputs[-1].conversation[-1].blocks[-1].text if provider.prepared_inputs else None  # type: ignore[union-attr]
        check("8: the message sent is the user's", given == "a message of my own", given)
        expect(page.locator("#composer-send")).to_be_enabled(timeout=10000)
        fake.prompt_delay = {"calliope_check": 2.0, "calliope_commentary": 0.2}
        box.fill("/calliope_check")
        box.press("Enter")  # slow
        box.fill("/comm")
        box.press("Enter")  # another prompt, answered first
        page.fill("#prompt-argument-0", "g-2")
        page.click("#prompt-form-submit")
        expect(box).to_have_value("[MCP prompt calliope/calliope_commentary · game=g-2]\n\n" + COMMENTARY, timeout=5000)
        settle()
        check("8: the earlier prompt's late answer does not replace the later one",
              box.input_value().startswith("[MCP prompt calliope/calliope_commentary · game=g-2]"), box.input_value())
        fake.prompt_delay = {}
        box.fill("")

        # 9. Narrow layout.
        page.set_viewport_size({"width": 380, "height": 800})
        page.wait_for_timeout(800)  # the sidebar becomes a drawer: let it finish closing
        box.fill("/")
        expect(page.locator("#prompt-picker")).to_be_visible(timeout=5000)
        width = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        box.fill("/comm")
        box.press("Enter")
        expect(page.locator("#prompt-form")).to_be_visible(timeout=5000)
        shot(page, "9-narrow-form")
        width = width and page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        check("9: the narrow layout has no horizontal scroll", bool(width))
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
        mcp = {"servers": {"calliope": {"url": URL}}}
        Path(root, "mcp.json").write_text(json.dumps(mcp))
        os.chmod(os.path.join(root, "mcp.json"), 0o600)
        fake = fake_server()
        provider = ScriptedAdapter([NormalizedFinal((TextBlock("해설을 쓰겠습니다."),)),
                                    NormalizedFinal((TextBlock("네."),))])
        serve(root, fake, provider)
        token = Path(root, "control", "operator.secret").read_text().strip()
        results: list[str] = []
        try:
            run(token, fake, provider, args.chrome, args.screenshots, results)
        except Exception as error:  # noqa: BLE001 — a step that could not go on fails; what passed stays shown
            results.append(f"FAIL  the run stopped after the checks above: {type(error).__name__}: "
                           f"{str(error).splitlines()[0]}")
    for line in results:
        print(line)
    failed = sum(line.startswith("FAIL") for line in results)
    print(f"mcp prompts smoke: {'FAIL' if failed else 'OK'} ({len(results) - failed}/{len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
