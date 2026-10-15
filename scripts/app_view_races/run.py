"""Browser regression test for the Tool view bridge's async orderings
(static/app_view.js; PR #28 review B1/B2). Opt-in evidence, not pytest.

Loads the real app_view.js in a harness page whose api() answers only when
the test says so, and frames a fake view that counts what it receives. Each
case forces one ordering exactly:

- B1: the view API already answers "done" (the result reaches the view at
  initialized), then the tool_result event arrives and the view is asked
  again: the result must be delivered once, not twice.
- B1: same, with the event arriving before the view is initialized.
- B2: the source is turned off (in this tab, or in another tab through the
  BroadcastChannel) while the view request is in flight: when the answer
  arrives, no frame may be created.
- B2: the source is turned off while a running view is asking for its
  result: it must not come back, even with a changed view.
- Turning the source back on brings "Open view" back.

Requires: pip install playwright (a Chromium for it, or --chrome PATH)
Usage:    python scripts/app_view_races/run.py [--chrome PATH] [--app-view FILE]
          (--app-view: test another copy of app_view.js, e.g. an older one)
Exit status 1 if any check fails.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
APP_VIEW = HERE.parents[1] / "src" / "himari" / "interfaces" / "web" / "static" / "app_view.js"
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
OFF = "This server is turned off in this conversation."


def serve(app_view: Path) -> ThreadingHTTPServer:
    files = {"/": (HERE / "harness.html", "text/html"), "/harness.js": (HERE / "harness.js", "text/javascript"),
             "/fakeview.html": (HERE / "fakeview.html", "text/html"), "/assets/app_view.js": (app_view, "text/javascript")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            found = files.get(self.path)
            self.send_response(200 if found else 404)
            self.send_header("content-type", found[1] if found else "text/plain")
            self.send_header("cache-control", "no-store")
            self.end_headers()
            if found:
                self.wfile.write(found[0].read_bytes())

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait(page: Page, script: str, timeout: int = 5000) -> None:
    page.wait_for_function(script, timeout=timeout)


def results_delivered(page: Page) -> int:
    received = page.evaluate("harness.received()") or []
    return sum(m == "ui/notifications/tool-result" for m in received)


def done_then_late_event(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1'); harness.views.autoOpen()")
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.answer('done')")
    wait(page, "(harness.received() || []).includes('ui/notifications/tool-result')")
    page.evaluate("harness.resultEvent('c1')")  # the poll brings the tool_result event afterwards
    page.wait_for_timeout(200)
    if page.evaluate("harness.pending.length") == 1:
        page.evaluate("harness.answer('done')")
    page.wait_for_timeout(600)
    count = results_delivered(page)
    return count == 1, count


def done_then_event_before_initialized(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1'); harness.views.autoOpen()")
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.answer('done'); harness.resultEvent('c1')")  # before the view's handshake
    page.wait_for_timeout(200)
    if page.evaluate("harness.pending.length") == 1:
        page.evaluate("harness.answer('done')")
    wait(page, "(harness.received() || []).includes('ui/notifications/tool-result')")
    page.wait_for_timeout(600)
    count = results_delivered(page)
    return count == 1, count


def off_here_while_opening(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1'); harness.views.autoOpen()")
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.views.sources([{source: 'calliope', enabled: false}], true)")
    page.evaluate("harness.answer('running')")  # the answer from before the source was turned off
    page.wait_for_timeout(600)
    seen = (page.evaluate("harness.frames()"), page.evaluate("harness.notices()"))
    return seen[0] == 0 and OFF in seen[1], seen


def off_in_another_tab_while_opening(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1'); harness.views.autoOpen()")
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.broadcastOff('calliope')")
    page.wait_for_timeout(200)  # BroadcastChannel delivers asynchronously
    page.evaluate("harness.answer('running')")
    page.wait_for_timeout(600)
    seen = (page.evaluate("harness.frames()"), page.evaluate("harness.notices()"))
    return seen[0] == 0 and OFF in seen[1], seen


def off_while_finishing(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1'); harness.views.autoOpen()")
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.answer('running')")
    wait(page, "harness.frames() === 1")
    page.evaluate("harness.resultEvent('c1')")  # finish() asks for the result...
    wait(page, "harness.pending.length === 1")
    page.evaluate("harness.views.sources([{source: 'calliope', enabled: false}], true)")  # ...the source goes off...
    page.evaluate("harness.answer('done', {digest: 'd2'})")  # ...and a changed view would be mounted again
    page.wait_for_timeout(1500)
    seen = (page.evaluate("harness.frames()"), page.evaluate("harness.notices()"))
    return seen[0] == 0 and OFF in seen[1], seen


def back_on(page: Page) -> tuple[bool, object]:
    page.evaluate("harness.row('c1')")
    page.evaluate("harness.views.sources([{source: 'calliope', enabled: false}], true); harness.views.autoOpen()")
    page.wait_for_timeout(200)
    off = (page.evaluate("harness.pending.length"), page.locator(".app-view-open").count())
    page.evaluate("harness.views.sources([{source: 'calliope', enabled: true}])")
    on = page.locator(".app-view-open").count()
    return off == (0, 0) and on == 1, {"while off (requests, buttons)": off, "buttons when back on": on}


CASES: list[tuple[str, Callable[[Page], tuple[bool, object]]]] = [
    ("B1: 'done' at open, then the late tool_result event: one tool-result", done_then_late_event),
    ("B1: the event before the view's handshake: one tool-result", done_then_event_before_initialized),
    ("B2: turned off here while the view request is in flight: no frame", off_here_while_opening),
    ("B2: turned off in another tab while it is in flight: no frame", off_in_another_tab_while_opening),
    ("B2: turned off while a running view asks for its result: not mounted again", off_while_finishing),
    ("turned off: no request, no 'Open view'; back on: 'Open view' again", back_on),
]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", help="a Chromium (e.g. chrome-headless-shell) instead of Playwright's own")
    parser.add_argument("--app-view", type=Path, default=APP_VIEW, help="the app_view.js to test")
    args = parser.parse_args(argv)
    server = serve(args.app_view)
    failed = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": args.chrome} if args.chrome else {}))
            for name, case in CASES:
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(BASE + "/")
                page.wait_for_function("window.harness !== undefined")
                try:
                    ok, seen = case(page)
                except Exception as error:  # noqa: BLE001 — a case that breaks is a failure, with its reason
                    ok, seen = False, f"{type(error).__name__}: {str(error).splitlines()[0]}"
                ok = ok and not errors
                failed += not ok
                print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (saw: {seen!r} {errors or ''})"))
                page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"app_view races: {'FAIL' if failed else 'OK'} ({len(CASES) - failed}/{len(CASES)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
