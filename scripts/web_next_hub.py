"""A stand-in hub for the new page's browser checks (V3-P102 W2): ``/user/<op>`` and ``/user/stream`` as the
hub's contract (user-api-v1) has them, over real HTTP and SSE, with scripted projects, sessions and events.

- ``demo``: session 5 (claude@linux, working, a permission waiting) with a thought, an edit with its diff, a plan,
  the request and some text; its stream sends one more line ``LIVE_LINE`` 1.5 s after it opens. Session 6
  (codex@laptop) flips between idle and working every 3 s on the list stream.
- ``archive``: session 7 with ``LONG`` events (turns), to read the earlier ones.
- ``empty``: a project with no session.

Used by ``scripts/web_next_server.py``; not part of pytest.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

TOKEN = "hub-user-token"
LIVE_LINE = "A live line from the agent."
LONG = 450
AT = "2026-11-17T10:00:00Z"


def _session(sid: int, project: str, agent: str, runner: str, activity: str, task: int | None, last: int,
             **more: Any) -> dict[str, Any]:
    return {"id": sid, "project": project, "agent": agent, "runner": runner, "runner_connected": True,
            "profile": None, "state": "open", "closed_reason": None, "activity": activity, "task": task,
            "last_task": task, "model": "opus", "effort": None, "context": {"used": 42_000, "size": 200_000},
            "created_at": AT, "migrated": False, "closed_at": None, "first_seq": 1, "last_seq": last, "live": True,
            "queued": 0, "current": True, **more}


def _demo_events() -> list[dict[str, Any]]:
    bodies = [
        ("session", {"action": "new", "native_id": "n1"}),
        ("turn_start", {"task_id": 9, "prompt": "Fix the failing test", "agent": "claude", "model": "opus",
                        "effort": None, "mode": None, "prefix_chars": 0}),
        ("thought", {"text": "The test reads the wrong file."}),
        ("tool", {"id": "t1", "title": "Edit src/app.py", "kind": "edit", "status": "in_progress",
                  "input": "{\"path\": \"src/app.py\"}", "locations": ["src/app.py"]}),
        ("tool", {"id": "t1", "status": "completed",
                  "diffs": [{"path": "src/app.py", "old": "x = 1\ny = 2\n", "new": "x = 1\ny = 3\nz = 4\n"}]}),
        ("plan", {"entries": [{"content": "Fix the path", "status": "completed"},
                              {"content": "Run the tests", "status": "in_progress"}]}),
        ("request", {"interaction_id": "i1", "kind": "permission", "title": "Run pytest",
                     "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}]}),
        ("text", {"text": "I changed **y** and "}),
        ("text", {"text": "I am waiting to run the tests. <script>window.__pwned = 3</script>"}),
        ("usage", {"used": 42_000, "size": 200_000}),
    ]
    return [{"seq": n, "kind": kind, "attempt": 1, "at": AT, "body": body} for n, (kind, body) in enumerate(bodies, 1)]


def _long_events() -> list[dict[str, Any]]:
    out = []
    for n in range(1, LONG + 1):
        kind, body = (("turn_start", {"task_id": n, "prompt": f"Prompt {n // 2 + 1}"}) if n % 2
                      else ("turn_end", {"task_id": n - 1, "outcome": "done"}))
        out.append({"seq": n, "kind": kind, "attempt": n, "at": AT, "body": body})
    return out


class FakeHub:
    def __init__(self) -> None:
        self.events = {5: _demo_events(), 6: [], 7: _long_events()}
        self.sessions = {
            5: _session(5, "demo", "claude", "linux", "running", 9, len(self.events[5]), queued=1),
            6: _session(6, "demo", "codex", "laptop", "idle", None, 0, current=False),
            7: _session(7, "archive", "claude", "linux", "idle", None, LONG),
        }
        self.pending = [{"id": 1, "task_id": 9, "kind": "permission", "asks": "Run pytest",
                         "task": {"project": "demo", "agent": "claude", "runner": "linux", "prompt": "Fix the failing test"},
                         "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}]}]
        self.lock = threading.Lock()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                    return True
                self._reply(401, {"error": "token"})
                return False

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                op = self.path.removeprefix("/user/")
                self._reply(*hub.op(op, body))

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                query = parse_qs(urlsplit(self.path).query)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if "session" in query:
                        sid = int(query["session"][0])
                        after = int(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0])
                        hub.stream_session(self.wfile, sid, after)
                    else:
                        hub.stream_list(self.wfile)
                except OSError:
                    pass  # the reader went away

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def op(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            if op == "projects":
                return 200, {"projects": [{"name": n, "path": f"/p/{n}", "default_agent": "claude", "preset": None,
                                           "profile": None} for n in ("archive", "demo", "empty")],
                             "agents": [{"name": "claude", "installed": True, "resting_until": None, "modes": []}],
                             "profiles": [], "models": {}, "efforts": {}, "runners": []}
            if op == "sessions":
                return 200, {"sessions": sorted(self.sessions.values(), key=lambda s: -s["id"])}
            if op == "session":
                found = self.sessions.get(body.get("id"))
                if found is None:
                    return 400, {"error": "없는 세션"}
                return 200, {**found, "pending": [p for p in self.pending if p["task_id"] == found["task"]],
                             "attempts": [], "queued": [{"id": 10, "prompt": "Then update the docs",
                                                          "created_at": AT}] if found["queued"] else []}
            if op == "events":
                events = self.events.get(body.get("session"), [])
                after, limit = int(body.get("after", 0)), int(body.get("limit", 200))
                page = [e for e in events if e["seq"] > after][:limit]
                last = events[-1]["seq"] if events else 0
                return 200, {"events": page, "first_seq": 1 if events else 0, "last_seq": last,
                             "next": page[-1]["seq"] if page else after, "more": bool(page) and page[-1]["seq"] < last,
                             "reset": False}
            if op == "pending":
                return 200, {"pending": self.pending}
            return 400, {"error": f"op {op}"}

    @staticmethod
    def _send(out: Any, event: str, data: dict[str, Any], seq: int | None = None) -> None:
        head = f"id: {seq}\n" if seq is not None else ""
        out.write(f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        out.flush()

    def stream_list(self, out: Any) -> None:
        """The whole list, then session 6 flips between idle and working every 3 s (a ``changed`` each time).
        HIMARI's server shares one list stream among its browsers and keeps the list, so a browser that joins
        late sees whatever the last flip left: a check waits for the next flip, whichever way it goes."""
        with self.lock:
            self._send(out, "sessions", {"sessions": sorted(self.sessions.values(), key=lambda s: -s["id"])})
        while True:
            time.sleep(3)
            with self.lock:
                working = self.sessions[6]["activity"] == "running"
                self.sessions[6] = {**self.sessions[6], "activity": "idle" if working else "running",
                                    "task": None if working else 11}
                self._send(out, "changed", {"sessions": [self.sessions[6]]})

    def stream_session(self, out: Any, sid: int, after: int) -> None:
        with self.lock:
            session = self.sessions[sid]
            self._send(out, "state", {"first_seq": session["first_seq"], "last_seq": session["last_seq"],
                                      "reset": False, "session": session})
            for event in self.events[sid]:
                if event["seq"] > after:
                    self._send(out, "event", event, event["seq"])
        if sid == 5:
            time.sleep(1.5)
            with self.lock:
                if not any(e["body"].get("text") == LIVE_LINE for e in self.events[5]):
                    seq = len(self.events[5]) + 1
                    self.events[5].append({"seq": seq, "kind": "text", "attempt": 1, "at": AT,
                                           "body": {"text": " " + LIVE_LINE}})
                    self.sessions[5] = {**self.sessions[5], "last_seq": seq}
                for event in self.events[5]:
                    if event["seq"] > after and event["body"].get("text", "").strip() == LIVE_LINE:
                        self._send(out, "event", event, event["seq"])
        while True:
            time.sleep(5)
            out.write(b": ping\n\n")
            out.flush()
