"""Touch navigation smoke over an isolated real server and a fake machine.

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
from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="himari-touch-") as tmp:
        root = str(Path(tmp) / "data")
        server = serve(root, port=8809)
        try:
            token = Path(root, "control/operator.secret").read_text().strip()
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in (390, 768, 1024, 1366):
                    context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=width < 1100)
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
                                        {"name": "note.txt", "kind": "file", "size": 12, "modified": 0}]}
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

                    if width < 1100:
                        expect(page.locator("#workspace-switch")).to_be_visible()
                        page.locator("#workspace-files").click()
                    else:
                        page.locator("#chat-files-toggle").click()
                    panel = page.locator("#file-panel")
                    expect(panel.get_by_title("/home/demo/nested", exact=True)).to_be_visible()
                    if width >= 1100:
                        heights = [page.locator(selector).bounding_box()["height"] for selector in
                                   (".sidebar-head", "#topbar", "#file-panel .files-head")]
                        assert max(heights) - min(heights) < 1, heights
                    assert page.locator(".sidebar-head").evaluate(
                        "e => getComputedStyle(e).borderBottomColor === 'rgba(0, 0, 0, 0)'")
                    panel.get_by_title("/home/demo/nested", exact=True).click()
                    panel.get_by_title("/home/demo/nested/note.txt", exact=True).click()
                    expect(panel.locator(".cm-content")).to_contain_text("touch note")
                    panel.get_by_role("button", name="Go back in files", exact=True).click()
                    expect(panel.get_by_title("/home/demo/nested/nested", exact=True)).to_be_visible()
                    panel.get_by_role("button", name="Go to parent folder", exact=True).click()
                    expect(panel.get_by_title("/home/demo/note.txt", exact=True)).to_be_visible()
                    panel.get_by_title("/home/demo/note.txt", exact=True).click()
                    editor = panel.locator(".cm-content")
                    editor.click()
                    editor.press("ControlOrMeta+End")
                    page.keyboard.type("unsaved")
                    panel.get_by_role("button", name="Go back in files", exact=True).click()
                    expect(page.locator("#confirm-dialog")).to_be_visible()
                    page.locator("#confirm-dialog").get_by_role("button", name="Cancel", exact=True).click()
                    expect(editor).to_contain_text("unsaved")
                    if width < 1100:
                        page.locator("#workspace-chat").click()
                        expect(page.locator("#composer-text")).to_have_value("draft to keep")
                        page.locator("#workspace-files").click()
                        expect(editor).to_contain_text("unsaved")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.evaluate("document.documentElement.dataset.theme = 'warm'; window.scrollTo(0, 5000)")
                    assert page.evaluate("scrollY === 0 && getComputedStyle(document.documentElement).backgroundColor === getComputedStyle(document.body).backgroundColor")
                    if args.screenshots:
                        page.wait_for_timeout(400)  # capture the settled view, after shell motion
                        page.screenshot(path=str(args.screenshots / f"touch-files-{width}.png"))
                    if width < 1100:
                        page.locator("#workspace-chat").click()
                        page.locator("#sidebar-toggle").click()
                        if page.locator("#app-fullscreen").is_visible():
                            page.locator("#app-fullscreen").click()
                            page.wait_for_function("document.fullscreenElement !== null")
                            assert page.locator(".sidebar-head").evaluate("e => parseFloat(getComputedStyle(e).paddingLeft) >= 72")
                            page.locator("#sidebar-close").click()
                            page.wait_for_timeout(400)
                            assert page.locator("#topbar").evaluate("e => parseFloat(getComputedStyle(e).paddingLeft) >= 72")
                            if args.screenshots:
                                page.screenshot(path=str(args.screenshots / f"touch-fullscreen-{width}.png"))
                            page.evaluate("document.exitFullscreen()")
                        page.set_viewport_size({"width": width, "height": 450})
                        expect(page.locator("#workspace-switch")).to_be_visible()
                        assert page.locator("#composer-text").bounding_box()["y"] < 450
                    checks.append({"width": width, "file_back_and_up": True, "cancel_keeps_editor": True,
                                   "draft_and_editor_survive_view_switch": width < 1100, "root_scroll": 0})
                    context.close()
                browser.close()
            assert not errors, errors
            print(json.dumps({"checks": checks, "page_errors": errors}))
        finally:
            server.should_exit = True


if __name__ == "__main__":
    main()
