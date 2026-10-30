"""Unified workspace smoke over an isolated real server and a fake machine.

Requires Playwright Chromium. No production data or model requests are used.
python scripts/touch_smoke.py --screenshots /tmp/himari-touch
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ui_smoke import serve
from playwright.sync_api import Locator, expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    checks: list[dict[str, object]] = []
    pdf_bytes = (Path(__file__).resolve().parents[1] / "tests/fixtures/pdf/table.pdf").read_bytes()
    with tempfile.TemporaryDirectory(prefix="himari-touch-") as tmp:
        root = str(Path(tmp) / "data")
        server = serve(root, port=8809)
        try:
            token = Path(root, "control/operator.secret").read_text().strip()
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in (320, 390, 768, 1024, 1180, 1366):
                    context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=width < 1200)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def machine(route: object) -> None:
                        url = urlsplit(route.request.url)
                        path = parse_qs(url.query).get("path", ["~"])[0]
                        path = "/home/demo" if path == "~" else path
                        if url.path.endswith("/stat"):
                            data = {"path": path, "kind": "file" if path.endswith(".txt") else "directory",
                                    "identity": [1, 2, 3, 4]}
                        elif url.path.endswith("/list"):
                            data = {"path": path, "parent": path.rsplit("/", 1)[0] or "/", "next": None,
                                    "workspace": None, "entries": [
                                        {"name": "nested", "kind": "directory", "size": None, "modified": 0},
                                        {"name": "note.txt", "kind": "file", "size": 12, "modified": 0},
                                        {"name": "sample.pdf", "kind": "file", "size": len(pdf_bytes), "modified": 0}]}
                        elif url.path.endswith("/download") and path.endswith(".pdf"):
                            route.fulfill(body=pdf_bytes, content_type="application/pdf",
                                          headers={"x-himari-identity": f"1,2,{len(pdf_bytes)},4"})
                            return
                        elif url.path.endswith("/file") and path.endswith(".pdf"):
                            data = {"path": path, "kind": "pdf", "editable": False, "size": len(pdf_bytes),
                                    "identity": [1, 2, len(pdf_bytes), 4], "modified": 0, "truncated": False}
                        elif url.path.endswith("/file"):
                            data = {"path": path, "kind": "text", "text": "touch note\n", "editable": True,
                                    "size": 11, "identity": [1, 2, 3, 4], "modified": 0, "truncated": False}
                        elif url.path.endswith("/conversations"):
                            data = {"path": path, "own": False, "refused": None, "above": [], "conversations": []}
                        else:
                            route.fallback()
                            return
                        route.fulfill(json=data)

                    page.route("**/api/machine/**", machine)
                    page.route("**/api/workspaces", lambda route: route.fulfill(json={"available": True, "items": []}))
                    page.route("**/api/conversations/*/workspaces", lambda route: route.fulfill(json={"items": [
                        {"path": "/home/demo", "working": True, "own": True, "binding_id": "b1",
                         "workspace_id": "w1", "label": "Demo"}]}))
                    page.goto("http://127.0.0.1:8809")
                    page.locator("#login-token").fill(token)
                    page.locator("#login-submit").click()
                    page.locator("#home-new-chat").click()
                    expect(page.locator("#composer-text")).to_be_focused()
                    expect(page.locator("#chat-menu-model-select")).to_be_visible()
                    assert page.locator("#model-dialog").count() == 0
                    assert page.locator("#chat-menu-model-select").bounding_box()["height"] >= 36
                    page.locator("#composer-text").fill("draft to keep")
                    height = page.locator("#topbar").bounding_box()["height"]
                    expect(page.locator("#workspace-switch")).to_be_hidden()
                    cycle = page.locator("#chat-files-toggle")
                    cycle_x = cycle.bounding_box()["x"]
                    more_x = page.locator("#chat-menu-toggle").bounding_box()["x"]
                    model_x = page.locator("#chat-menu-model-select").bounding_box()["x"]
                    expect(page.locator("#workspace-context, #workspace-file-tools")).to_have_count(0)
                    modes = ("chat-files", "files", "files-chat", "chat")

                    def next_mode(mode: str) -> None:
                        cycle.click()
                        expect(page.locator("#workspace-panes")).to_have_attribute("data-mode", mode)
                        assert page.locator("#topbar").bounding_box()["height"] == height
                        assert abs(cycle.bounding_box()["x"] - cycle_x) < .5
                        assert abs(page.locator("#chat-menu-toggle").bounding_box()["x"] - more_x) < .5
                        expect(page.locator("#chat-menu-model-select")).to_be_visible()
                        assert abs(page.locator("#chat-menu-model-select").bounding_box()["x"] - model_x) < .5
                        # More always means conversation details, even with files as the only pane.
                        page.locator("#chat-menu-toggle").click()
                        expect(page.locator("#chat-menu-rename")).to_be_visible()
                        page.locator("#chat-menu-toggle").click()

                    def file_control(name: str) -> Locator:
                        return page.locator("#file-panel .files-head").get_by_role("button", name=name, exact=True)

                    next_mode(modes[0])
                    panel = page.locator("#file-panel")
                    body = panel.locator(".files-body")
                    expect(body.get_by_title("/home/demo/nested", exact=True)).to_be_visible()
                    chat_box = page.locator("#main").bounding_box()
                    file_box = panel.bounding_box()
                    assert (file_box["y"] >= chat_box["y"] + chat_box["height"] - 1 if width < 1100
                            else file_box["x"] >= chat_box["x"] + chat_box["width"] - 1)
                    assert page.locator("#topbar .files-head").count() == 0
                    expect(panel.locator(".files-head")).to_be_visible()
                    assert page.locator(".sidebar-head").evaluate(
                        "e => getComputedStyle(e).borderBottomColor === 'rgba(0, 0, 0, 0)'")
                    body.get_by_title("/home/demo/nested", exact=True).click()
                    body.get_by_title("/home/demo/nested/note.txt", exact=True).click()
                    expect(panel.locator(".cm-content")).to_contain_text("touch note")
                    file_control("Go back in files").click()
                    expect(body.get_by_title("/home/demo/nested/nested", exact=True)).to_be_visible()
                    file_control("Go to parent folder").click()
                    expect(body.get_by_title("/home/demo/note.txt", exact=True)).to_be_visible()
                    page.keyboard.press("Escape")
                    body.get_by_title("/home/demo/note.txt", exact=True).click()
                    editor = panel.locator(".cm-content")
                    editor.click()
                    editor.press("ControlOrMeta+End")
                    page.keyboard.type("unsaved")
                    file_control("Go back in files").click()
                    expect(page.locator("#confirm-dialog")).to_be_visible()
                    page.locator("#confirm-dialog").get_by_role("button", name="Cancel", exact=True).click()
                    expect(editor).to_contain_text("unsaved")
                    page.keyboard.press("Escape")
                    next_mode(modes[1])
                    expect(page.locator("#main")).to_be_hidden()
                    expect(page.locator("#topbar")).to_be_visible()
                    next_mode(modes[2])
                    expect(page.locator("#main")).to_be_visible()
                    expect(page.locator("#composer-text")).to_have_value("draft to keep")
                    expect(editor).to_contain_text("unsaved")
                    page.wait_for_timeout(250)
                    chat_box = page.locator("#main").bounding_box()
                    file_box = panel.bounding_box()
                    assert (chat_box["y"] >= file_box["y"] + file_box["height"] - 1 if width < 1100
                            else chat_box["x"] >= file_box["x"] + file_box["width"] - 1)
                    next_mode(modes[3])
                    expect(panel).to_be_hidden()
                    expect(page.locator("#chat-menu-model-select")).to_be_visible()
                    expect(page.locator("#composer-text")).to_have_value("draft to keep")
                    next_mode(modes[0])
                    expect(editor).to_contain_text("unsaved")
                    # Rapid clicks skip an in-flight snapshot and commit each cycle once.
                    page.evaluate("for (let i = 0; i < 4; i++) document.getElementById('chat-files-toggle').click()")
                    expect(page.locator("#workspace-panes")).to_have_attribute("data-mode", modes[0])
                    expect(editor).to_contain_text("unsaved")
                    expect(page.locator("#composer-text")).to_have_value("draft to keep")
                    page.wait_for_timeout(250)
                    grip = page.locator("#file-panel-grip")
                    before_resize = panel.bounding_box()
                    grip.focus()
                    page.keyboard.press("ArrowUp" if width < 1100 else "ArrowLeft")
                    after_resize = panel.bounding_box()
                    axis = "height" if width < 1100 else "width"
                    assert after_resize[axis] > before_resize[axis] + 1
                    # Reduced motion uses the same cycle with no view-transition delay.
                    page.emulate_media(reduced_motion="reduce")
                    for mode in modes[1:] + modes[:1]:
                        next_mode(mode)
                    expect(editor).to_contain_text("unsaved")
                    page.emulate_media(reduced_motion="no-preference")
                    assert page.locator("#confirm-dialog").is_visible() is False
                    assert page.locator("#file-panel-expand, #file-panel-close").count() == 0
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.evaluate("document.documentElement.dataset.theme = 'warm'; window.scrollTo(0, 5000)")
                    assert page.evaluate("scrollY === 0 && getComputedStyle(document.documentElement).backgroundColor === getComputedStyle(document.body).backgroundColor")
                    if args.screenshots:
                        page.wait_for_timeout(400)  # capture the settled view, after shell motion
                        page.screenshot(path=str(args.screenshots / f"workspace-split-{width}.png"))
                    if width >= 1100:
                        page.locator("#sidebar-close").click()
                    else:
                        # The drawer keeps its deployed transform animation.
                        assert "transform" in page.locator("#sidebar").evaluate("e => getComputedStyle(e).transitionProperty")
                    expect(page.locator("#app-fullscreen")).to_be_visible()
                    assert page.locator("#app-fullscreen").bounding_box()["x"] < page.locator("#sidebar-toggle").bounding_box()["x"]
                    page.locator("#sidebar-toggle").click()
                    expect(page.locator("#sidebar-fullscreen")).to_be_visible()
                    assert page.locator("#sidebar-fullscreen").bounding_box()["x"] < page.locator("#sidebar-close").bounding_box()["x"]
                    page.locator("#sidebar-fullscreen").click()
                    page.wait_for_function("() => document.fullscreenElement !== null")
                    if width < 1200:
                        assert page.locator(".sidebar-head").evaluate("e => parseFloat(getComputedStyle(e).paddingLeft) >= 72")
                    button_box = page.locator("#sidebar-close").bounding_box()
                    sidebar_box = page.locator("#sidebar").bounding_box()
                    assert button_box["x"] + button_box["width"] <= sidebar_box["x"] + sidebar_box["width"]
                    page.locator("#sidebar-close").click()
                    page.wait_for_timeout(250)
                    expect(page.locator("#app-fullscreen")).to_be_visible()
                    if width < 1100:
                        assert page.locator("#topbar").evaluate("e => parseFloat(getComputedStyle(e).paddingLeft) >= 72")
                    page.locator("#app-fullscreen").click()
                    page.wait_for_function("() => document.fullscreenElement === null")
                    if width < 1100:
                        page.set_viewport_size({"width": width, "height": 450})
                        expect(page.locator("#chat-files-toggle")).to_be_visible()
                        assert page.locator("#composer-text").bounding_box()["y"] < 450
                        # A short viewport and multiline draft must not consume the PDF or composer.
                        file_control("Go back in files").click()
                        page.locator("#confirm-dialog").get_by_role("button", name="Discard", exact=True).click()
                        panel.locator(".files-open").filter(has_text="sample.pdf").click()
                        expect(panel.locator(".pdf-view")).to_be_visible()
                        panel.get_by_role("button", name="Pen", exact=True).click()
                        page.set_viewport_size({"width": width, "height": 350})
                        page.locator("#composer-text").fill("multiline draft\n" * 30)
                        page.wait_for_function("() => document.querySelector('#file-panel .pdf-view').clientHeight >= 40")
                        compose = page.locator("#composer").bounding_box()
                        main_box = page.locator("#main").bounding_box()
                        assert compose["y"] >= main_box["y"] and compose["y"] + compose["height"] <= main_box["y"] + main_box["height"] + 1
                        if args.screenshots:
                            page.screenshot(path=str(args.screenshots / f"workspace-pdf-short-{width}.png"))
                        page.set_viewport_size({"width": 390, "height": 700})
                        expect(panel.locator(".files-ink-bar .files-zoom-tools")).to_be_visible()
                        page.set_viewport_size({"width": 820, "height": 900})
                        expect(panel.locator(".files-head .files-zoom-tools")).to_be_visible()
                        expect(panel.get_by_role("button", name="Pen", exact=True)).to_have_attribute("aria-pressed", "true")
                        expect(page.locator("#composer-text")).to_have_value("multiline draft\n" * 30)
                    checks.append({"width": width, "file_back_and_up": True, "cancel_keeps_editor": True,
                                   "cycle": list(modes), "header_height_preserved": height,
                                   "draft_and_editor_survive_view_switch": True, "root_scroll": 0})
                    context.close()
                browser.close()
            assert not errors, errors
            print(json.dumps({"checks": checks, "page_errors": errors}))
        finally:
            server.should_exit = True


if __name__ == "__main__":
    main()
