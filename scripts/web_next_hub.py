"""A stand-in hub for the new page's browser checks (V3-P102 W2-W3): ``/user/<op>`` and ``/user/stream`` as the
hub's contract (user-api-v1) has them, over real HTTP and SSE, with scripted projects, sessions and events.

- ``demo``: session 5 (claude@linux, working, a permission waiting) with a thought, an edit with its diff, a plan,
  the request and some text; its stream sends one more line ``LIVE_LINE`` 1.5 s after it opens. Session 6
  (codex@laptop) flips between idle and working every 3 s on the list stream.
- ``archive``: session 7 with ``LONG`` events (turns), to read the earlier ones.
- ``empty``: a project with no session.
- ``work`` (W3, the user's own work): session 8 (claude@linux, idle) runs what is sent to it for ``TURN_SECONDS``,
  and what is sent meanwhile waits its turn; session 9 (claude@laptop) waits on a permission (request 2); session
  10 (codex@linux) on a question (request 4, a color); session 12's agent has lost it, so what is sent to it goes on
  in a new session. ``new`` opens a session ``NEW_SECONDS`` after it is asked. Every reply of a turn is
  ``Done: <prompt>``; a prompt that mentions "fail" ends failed with ``The tests did not pass.``

``/mcp`` (W4) is the hub's MCP side for HIMARI, with ``MCP_TOKEN``: ``hub_delegate`` (a task opening a session of
the project's lane, as ``new`` does; an unknown project is refused) and ``hub_task`` (the task as the hub's tool
gives it). It also has one prompt (W6b): ``review``, whose argument ``topic`` it puts in ``Review <topic> carefully.``

The streams follow every change: a session stream sends new events and a new ``state`` when the session's listing
changes; the list stream sends ``changed`` for every session that changed.

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
MCP_TOKEN = "hub-mcp-token"
_MCP_TOOLS = [
    {"name": "hub_delegate", "description": "Hand a task to a coding agent in a project.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "prompt": {"type": "string"},
                                                       "agent": {"type": "string"}},
                     "required": ["project", "prompt"]}},
    {"name": "hub_task", "description": "A task's state.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
]
LIVE_LINE = "A live line from the agent."
LONG = 450
AT = "2026-11-17T10:00:00Z"
TURN_SECONDS = 3.0
NEW_SECONDS = 1.5
TICK = 0.25


def _session(sid: int, project: str, agent: str, runner: str, activity: str, task: int | None, last: int,
             **more: Any) -> dict[str, Any]:
    return {"id": sid, "project": project, "agent": agent, "runner": runner, "runner_connected": True,
            "profile": None, "state": "open", "closed_reason": None, "activity": activity, "task": task,
            "last_task": task, "model": "opus", "effort": None, "context": {"used": 42_000, "size": 200_000},
            "created_at": AT, "migrated": False, "closed_at": None, "first_seq": 1, "last_seq": last, "live": True,
            "queued": 0, "current": True, "updated_at": AT, **more}


def _numbered(bodies: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"seq": n, "kind": kind, "attempt": 1, "at": AT, "body": body} for n, (kind, body) in enumerate(bodies, 1)]


def _demo_events() -> list[dict[str, Any]]:
    return _numbered([
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
    ])


def _long_events() -> list[dict[str, Any]]:
    out = []
    for n in range(1, LONG + 1):
        kind, body = (("turn_start", {"task_id": n, "prompt": f"Prompt {n // 2 + 1}"}) if n % 2
                      else ("turn_end", {"task_id": n - 1, "outcome": "done"}))
        out.append({"seq": n, "kind": kind, "attempt": n, "at": AT, "body": body})
    return out


def _task(tid: int, prompt: str, agent: str, runner: str, project: str, status: str, session: int | None,
          **more: Any) -> dict[str, Any]:
    return {"id": tid, "status": status, "agent": agent, "runner": runner, "profile": None, "fresh": False,
            "model": None, "effort": None, "mode": None, "prompt": prompt, "created_at": AT, "finished_at": None,
            "project": project, "session": session, "result": None, **more}


class FakeHub:
    def __init__(self) -> None:
        self.events: dict[int, list[dict[str, Any]]] = {
            5: _demo_events(), 6: [], 7: _long_events(), 8: [],
            9: _numbered([("turn_start", {"task_id": 30, "prompt": "Write the notes", "agent": "claude"}),
                          ("request", {"interaction_id": 2, "kind": "permission", "title": "Write notes.md",
                                       "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"},
                                                   {"option_id": "reject", "name": "Reject", "kind": "reject_once"}]})]),
            10: _numbered([("turn_start", {"task_id": 40, "prompt": "Paint the button", "agent": "codex"}),
                           ("request", {"interaction_id": 4, "kind": "question", "message": "Which color?",
                                        "fields": [{"name": "color", "type": "string", "title": "Color"}]})]),
            12: [],
        }
        self.sessions = {
            5: _session(5, "demo", "claude", "linux", "running", 9, len(self.events[5]), queued=1),
            6: _session(6, "demo", "codex", "laptop", "idle", None, 0, current=False),
            7: _session(7, "archive", "claude", "linux", "idle", None, LONG),
            8: _session(8, "work", "claude", "linux", "idle", None, 0, current=False),
            9: _session(9, "work", "claude", "laptop", "waiting", 30, 2),
            10: _session(10, "work", "codex", "linux", "waiting", 40, 2),
            12: _session(12, "work", "claude", "linux", "idle", None, 0, forgets=True),
        }
        self.tasks: dict[int, dict[str, Any]] = {
            9: _task(9, "Fix the failing test", "claude", "linux", "demo", "waiting", 5),
            30: _task(30, "Write the notes", "claude", "laptop", "work", "waiting", 9),
            40: _task(40, "Paint the button", "codex", "linux", "work", "waiting", 10),
        }
        self.pending = [
            {"id": 1, "task_id": 9, "kind": "permission", "asks": "Run pytest",
             "task": {"project": "demo", "agent": "claude", "runner": "linux", "prompt": "Fix the failing test"},
             "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}]},
            {"id": 2, "task_id": 30, "kind": "permission", "asks": "Write notes.md",
             "task": {"project": "work", "agent": "claude", "runner": "laptop", "prompt": "Write the notes"},
             "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"},
                         {"option_id": "reject", "name": "Reject", "kind": "reject_once"}]},
            {"id": 4, "task_id": 40, "kind": "question", "asks": "Which color?",
             "task": {"project": "work", "agent": "codex", "runner": "linux", "prompt": "Paint the button"},
             "fields": {"color": {"title": "Color", "description": None, "multiple": False,
                                  "choices": [{"value": "red", "title": "Red", "description": None},
                                              {"value": "blue", "title": "Blue", "description": None}]}}},
        ]
        self.answers: list[dict[str, Any]] = []  # what the page answered
        self.next_task = 100
        self.next_session = 20
        self.generation = 0
        self.changed: dict[int, int] = {}  # session id: the generation of its last change
        self.lock = threading.RLock()
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
                if self.path == "/mcp":
                    if self.headers.get("Authorization") != f"Bearer {MCP_TOKEN}":
                        return self._reply(401, {"error": "token"})
                    message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                    answer = hub.mcp(message)
                    if answer is None:  # a notification
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    return self._reply(200, answer)
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

    # -- changes (under the lock) --------------------------------------------------------------------------------

    def _touch(self, sid: int, **change: Any) -> None:
        self.sessions[sid] = {**self.sessions[sid], **change, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.generation += 1
        self.changed[sid] = self.generation

    def _event(self, sid: int, kind: str, body: dict[str, Any]) -> None:
        seq = len(self.events[sid]) + 1
        self.events[sid].append({"seq": seq, "kind": kind, "attempt": 1, "at": AT, "body": body})
        self._touch(sid, last_seq=seq)

    def _queued(self, sid: int) -> list[dict[str, Any]]:
        return [t for t in self.tasks.values() if t["status"] == "queued" and t.get("sent_to") == sid]

    def _start(self, sid: int, task: dict[str, Any]) -> None:
        """The task's turn in the session: it starts now and ends after TURN_SECONDS (unless stopped)."""
        task.update(status="running", session=sid)
        self._touch(sid, activity="running", task=task["id"], last_task=task["id"], queued=len(self._queued(sid)))
        self._event(sid, "turn_start", {"task_id": task["id"], "prompt": task["prompt"], "agent": task["agent"],
                                        "model": task["model"], "effort": task["effort"], "mode": task["mode"]})
        threading.Thread(target=self._finish, args=(sid, task["id"]), daemon=True).start()

    def _finish(self, sid: int, tid: int) -> None:
        time.sleep(TURN_SECONDS)
        with self.lock:
            task = self.tasks[tid]
            if task["status"] != "running":
                return  # stopped meanwhile
            if "fail" in task["prompt"].lower():
                self._end(sid, task, "failed", "The tests did not pass.")
            else:
                self._end(sid, task, "done", f"Done: {task['prompt']}")

    def _end(self, sid: int, task: dict[str, Any], outcome: str, text: str | None) -> None:
        if text:
            self._event(sid, "text", {"text": text})
        self._event(sid, "turn_end", {"task_id": task["id"], "outcome": outcome})
        task.update(status="done" if outcome == "done" else "failed", result=text or "중지됐습니다", finished_at=AT)
        waiting = self._queued(sid)
        self._touch(sid, activity="idle", task=None, queued=len(waiting))
        if waiting:
            self._start(sid, waiting[0])

    def _new_session(self, project: str, agent: str, runner: str, action: str = "new") -> int:
        sid = self.next_session
        self.next_session += 1
        self.events[sid] = []
        self.sessions[sid] = _session(sid, project, agent, runner, "idle", None, 0)
        self._event(sid, "session", {"action": action, "native_id": f"n{sid}"})
        return sid

    def _task_view(self, task: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in task.items() if k != "sent_to"}
        view["result_truncated"] = False
        view["pending"] = [p for p in self.pending if p["task_id"] == task["id"]]
        return view

    # -- the user path ---------------------------------------------------------------------------------------------

    def op(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            if op == "projects":
                return 200, {"projects": [{"name": n, "path": f"/p/{n}", "default_agent": "claude", "preset": None,
                                           "profile": None} for n in ("archive", "demo", "empty", "work")],
                             "agents": [{"name": "claude", "installed": True, "resting_until": None,
                                         "modes": ["default", "plan"]},
                                        {"name": "codex", "installed": True, "resting_until": None, "modes": []}],
                             "profiles": [], "models": {"claude": ["opus", "sonnet"]}, "efforts": {}, "runners": []}
            if op == "sessions":
                return 200, {"sessions": sorted(self.sessions.values(), key=lambda s: -s["id"])}
            if op == "session":
                found = self.sessions.get(body.get("id"))
                if found is None:
                    return 400, {"error": "없는 세션"}
                queued = ([{"id": 10, "prompt": "Then update the docs", "created_at": AT}] if found["id"] == 5
                          else [{"id": t["id"], "prompt": t["prompt"][:200], "created_at": AT}
                                for t in self._queued(found["id"])])
                return 200, {**found, "pending": [p for p in self.pending if p["task_id"] == found["task"]],
                             "attempts": [], "queued": queued}
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
            if op == "task":
                task = self.tasks.get(body.get("id"))
                if task is None:
                    return 400, {"error": f"없는 작업: {body.get('id')}"}
                return 200, {"task": self._task_view(task)}
            if op == "send":
                return self._send(body)
            if op == "new":
                return self._new(body)
            if op == "cancel":
                return self._cancel(body)
            if op == "answer":
                return self._answer(body)
            if op == "close":
                return self._close(body)
            return 400, {"error": f"op {op}"}

    def _send(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sid = body.get("session")
        session = self.sessions.get(sid) if isinstance(sid, int) else None
        if session is None or session["state"] != "open":
            return 400, {"error": f"열린 세션이 아닙니다: {sid} (없거나 닫힘)"}
        tid = self.next_task
        self.next_task += 1
        task = _task(tid, str(body.get("prompt")), session["agent"], session["runner"], session["project"], "queued",
                     None, model=body.get("model"), effort=body.get("effort"), mode=body.get("mode"), sent_to=sid)
        self.tasks[tid] = task
        if session.get("forgets"):  # the agent lost this session: the task goes on in a new one
            self._touch(sid, state="closed", closed_reason="forgot", activity="closed", closed_at=AT)
            self._start(self._new_session(session["project"], session["agent"], session["runner"], "forgot"), task)
        elif session["activity"] in ("running", "waiting"):
            self._touch(sid, queued=len(self._queued(sid)))
        else:
            self._start(sid, task)
        return 200, {"task": {**self._task_view(task), "session": sid}}

    def mcp(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message of HIMARI's MCP client, as the hub's ``/mcp`` answers it."""
        method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}, "prompts": {}}, "serverInfo": {"name": "fake-hub", "version": "0"}}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "review", "title": "Review something",
                                   "description": "Ask for a careful review.",
                                   "arguments": [{"name": "topic", "description": "What to review", "required": True}]}]}
        elif method == "prompts/get":
            topic = (params.get("arguments") or {}).get("topic", "")
            result = {"messages": [{"role": "user", "content": {"type": "text", "text": f"Review {topic} carefully."}}]}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _MCP_TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            with self.lock:
                if name == "hub_delegate" and args.get("project") not in {s["project"] for s in self.sessions.values()}:
                    result = {"content": [{"type": "text", "text": f"모르는 프로젝트: {args.get('project')}"}],
                              "isError": True}
                elif name == "hub_delegate":
                    _, reply = self._new(args)
                    result = {"content": [{"type": "text", "text": json.dumps(reply["task"], ensure_ascii=False)}]}
                elif name == "hub_task" and args.get("id") in self.tasks:
                    view = self._task_view(self.tasks[args["id"]])
                    result = {"content": [{"type": "text", "text": json.dumps(view, ensure_ascii=False)}]}
                else:
                    result = {"content": [{"type": "text", "text": f"없는 작업이나 도구: {name}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _new(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tid = self.next_task
        self.next_task += 1
        agent = str(body.get("agent") or "claude")
        task = _task(tid, str(body.get("prompt")), agent, "linux", str(body.get("project")), "queued", None,
                     model=body.get("model"), effort=body.get("effort"), mode=body.get("mode"))
        self.tasks[tid] = task

        def opens() -> None:  # the session exists only once the task runs
            time.sleep(NEW_SECONDS)
            with self.lock:
                if task["status"] == "queued":
                    self._start(self._new_session(task["project"], agent, "linux"), task)

        threading.Thread(target=opens, daemon=True).start()
        return 200, {"task": {k: v for k, v in self._task_view(task).items() if k not in ("session", "pending")}}

    def _cancel(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        task = self.tasks.get(body.get("id"))
        if task is None:
            return 400, {"error": f"없는 작업: {body.get('id')}"}
        if task["status"] == "queued":
            task.update(status="failed", result="중지됐습니다")
            sid = task.get("sent_to")
            if sid in self.sessions:
                self._touch(sid, queued=len(self._queued(sid)))
        elif task["status"] in ("running", "waiting") and task.get("session") in self.sessions:
            self.pending = [p for p in self.pending if p["task_id"] != task["id"]]
            self._end(task["session"], task, "stopped", None)
        return 200, {"cancelling": task["id"]}

    def _answer(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        asked = next((p for p in self.pending if p["id"] == body.get("id")), None)
        if asked is None:
            return 400, {"error": f"답을 기다리는 요청이 아닙니다: {body.get('id')}"}
        self.pending.remove(asked)
        self.answers.append(body)
        task = self.tasks[asked["task_id"]]
        sid = task["session"]
        self._event(sid, "answer", {"interaction_id": asked["id"], "kind": asked["kind"],
                                    "outcome": "refused" if body.get("refuse") else "answered",
                                    **({"option_id": body["option_id"]} if "option_id" in body else {})})
        if sid == 5:  # the demo session stays as its own checks read it
            return 200, {"answered": asked["id"]}
        if "content" in body:
            reply = f"You chose {', '.join(str(v) for v in body['content'].values()) or 'nothing'}."
        else:
            reply = f"{'Allowed' if body.get('option_id') == 'allow' else 'Not allowed'}: {asked['asks']}."
        task["status"] = "running"
        self._end(sid, task, "done", reply)
        return 200, {"answered": asked["id"]}

    def _close(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sid = body.get("session")
        found = self.sessions.get(sid) if isinstance(sid, int) else None
        if found is None:
            return 400, {"error": "없는 세션"}
        if found["activity"] in ("running", "waiting"):
            return 400, {"error": "작업 중인 세션은 닫을 수 없습니다. 먼저 중지하세요"}
        cancelled = [t["id"] for t in self._queued(sid)]
        for tid in cancelled:
            self.tasks[tid].update(status="failed", result="세션이 닫혔습니다")
        self._touch(sid, state="closed", closed_reason="user", activity="closed", queued=0, closed_at=AT)
        return 200, {"closed": sid, "cancelled": cancelled}

    # -- the streams -----------------------------------------------------------------------------------------------

    @staticmethod
    def _frame(out: Any, event: str, data: dict[str, Any], seq: int | None = None) -> None:
        head = f"id: {seq}\n" if seq is not None else ""
        out.write(f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        out.flush()

    def stream_list(self, out: Any) -> None:
        """The whole list, then every change; session 6 flips between idle and working every 3 s. HIMARI's server
        shares one list stream among its browsers and keeps the list, so a browser that joins late sees whatever
        the last flip left: a check waits for the next flip, whichever way it goes."""
        with self.lock:
            self._frame(out, "sessions", {"sessions": sorted(self.sessions.values(), key=lambda s: -s["id"])})
            seen = self.generation
        flipped = time.monotonic()
        while True:
            time.sleep(TICK)
            with self.lock:
                if time.monotonic() - flipped >= 3:
                    flipped = time.monotonic()
                    working = self.sessions[6]["activity"] == "running"
                    self._touch(6, activity="idle" if working else "running", task=None if working else 11)
                moved = [self.sessions[sid] for sid, generation in self.changed.items() if generation > seen]
                seen = self.generation
                if moved:
                    self._frame(out, "changed", {"sessions": moved})

    def stream_session(self, out: Any, sid: int, after: int) -> None:
        def listed(session: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in session.items() if k not in ("last_seq", "updated_at")}

        with self.lock:
            session = self.sessions[sid]
            self._frame(out, "state", {"first_seq": session["first_seq"], "last_seq": session["last_seq"],
                                       "reset": False, "session": session})
            sent = after
            for event in self.events[sid]:
                if event["seq"] > sent:
                    self._frame(out, "event", event, event["seq"])
                    sent = event["seq"]
            shown = listed(session)
        if sid == 5:
            time.sleep(1.5)
            with self.lock:
                if not any(e["body"].get("text") == " " + LIVE_LINE for e in self.events[5]):
                    seq = len(self.events[5]) + 1
                    self.events[5].append({"seq": seq, "kind": "text", "attempt": 1, "at": AT,
                                           "body": {"text": " " + LIVE_LINE}})
                    self.sessions[5] = {**self.sessions[5], "last_seq": seq}
                for event in self.events[5]:
                    if event["seq"] > after and event["body"].get("text", "").strip() == LIVE_LINE:
                        self._frame(out, "event", event, event["seq"])
                        sent = max(sent, event["seq"])
        quiet = time.monotonic()
        while True:
            time.sleep(TICK)
            with self.lock:
                session = self.sessions[sid]
                for event in self.events[sid]:
                    if event["seq"] > sent:
                        self._frame(out, "event", event, event["seq"])
                        sent = event["seq"]
                        quiet = time.monotonic()
                if listed(session) != shown:
                    shown = listed(session)
                    self._frame(out, "state", {"first_seq": session["first_seq"], "last_seq": session["last_seq"],
                                               "reset": False, "session": session})
                    quiet = time.monotonic()
            if time.monotonic() - quiet > 5:
                out.write(b": ping\n\n")
                out.flush()
                quiet = time.monotonic()
