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

V3-P103 (projects and device folders, user-api-v1 §4.6 with H8, H12, H13, H17): three devices. ``laptop`` takes
requests the ``local`` way (a pairing code; its "person" picks the folder ``PICK_SECONDS`` later), ``linux`` the
``user`` way (roots ``ROOT``; it takes a request ``DEVICE_SECONDS`` after it is sent), ``old`` is connected with
the shared token and takes none (``off``). ``gone`` is not connected: only the held list and a lost request name
it. Requests at the start: 1, HIMARI's ``create`` of ``from-himari`` on linux, waiting for the user; 2, a lost
``add_folder`` of ``work`` on laptop (its fence says applied); 3, a lost ``remove_folder`` of ``archive`` on gone
(no fence: force). Held: ``orphan`` on linux (no project of that name), ``work`` on laptop, ``ghost`` on gone. The
project ``SPECIAL`` has a name with ``/``, ``?``, ``#``, Hangul and spaces. ``hub_project_request`` on ``/mcp`` makes
HIMARI's request as the hub's MCP does (no path or pairing code in its answer, H11).

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
    {"name": "hub_project_request", "description": "Ask to make a project or add a device's folder.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string"}, "project": {"type": "string"},
                                                       "runner": {"type": "string"}, "path": {"type": "string"},
                                                       "why": {"type": "string"}},
                     "required": ["kind", "project", "runner"]}},
]
_ENDED = ("done", "refused", "expired", "failed", "cancelled")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _later(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
LIVE_LINE = "A live line from the agent."
LONG = 450
AT = "2026-11-17T10:00:00Z"
TURN_SECONDS = 3.0
PICK_SECONDS = 2.5  # V3-P103: the person at a local device picks the folder
DEVICE_SECONDS = 1.0  # a device takes a request, a fence answers
ROOT = "/home/u/code"
SPECIAL = "a/b?c#d 한글 x"
NEW_SECONDS = 1.5
TICK = 0.25


def _session(sid: int, project: str, agent: str, runner: str, activity: str, task: int | None, last: int,
             **more: Any) -> dict[str, Any]:
    return {"id": sid, "project": project, "agent": agent, "runner": runner, "runner_connected": True,
            "profile": None, "state": "open", "closed_reason": None, "activity": activity, "task": task,
            "last_task": task, "model": "opus", "effort": None, "context": {"used": 42_000, "size": 200_000},
            "created_at": AT, "migrated": False, "closed_at": None, "first_seq": 1, "last_seq": last, "live": True,
            "queued": 0, "current": True, "updated_at": AT, "title": None, **more}


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
        # V3-P103: projects, devices, requests
        self.projects: dict[str, dict[str, Any]] = {
            n: {"name": n, "path": f"/p/{n}", "default_agent": "claude", "preset": None, "profile": None}
            for n in ("archive", "demo", "empty", "work", SPECIAL)}
        self.devices: dict[str, dict[str, Any]] = {
            "laptop": {"approve": {"mode": "local", "roots": [], "why": None},
                       "folders": {"demo": "D:/code/demo"}, "agents": ["claude", "codex"]},
            "linux": {"approve": {"mode": "user", "roots": [ROOT], "why": None},
                      "folders": {"demo": f"{ROOT}/demo", "work": f"{ROOT}/work", SPECIAL: f"{ROOT}/special",
                                  "orphan": f"{ROOT}/orphan"},
                      "agents": ["claude", "codex"]},
            "old": {"approve": {"mode": "off", "roots": [], "why": "shared_token"}, "folders": {},
                    "agents": ["claude"]},
        }
        self.held = [{"project": "orphan", "runner": "linux", "created_at": AT},
                     {"project": "work", "runner": "laptop", "created_at": AT},
                     {"project": "ghost", "runner": "gone", "created_at": AT}]
        self.dirs = {ROOT: ["demo", "notes", "site", "work"], f"{ROOT}/notes": ["drafts"], f"{ROOT}/site": [],
                     f"{ROOT}/notes/drafts": [], f"{ROOT}/demo": [], f"{ROOT}/work": []}
        self.requests: dict[int, dict[str, Any]] = {}
        self.keys: dict[str, tuple[str, str, int]] = {}  # client_key: (op, input, request id)
        self.request_changes: dict[int, int] = {}  # request id: the generation of its last change
        self.projects_generation = 0
        self.next_request = 1
        self.next_task = 100
        self.next_session = 20
        self.generation = 0
        self.changed: dict[int, int] = {}  # session id: the generation of its last change
        self.lock = threading.RLock()
        self._add_request("create", "from-himari", "linux", "model", path=f"{ROOT}/from-himari", create_dir=True,
                          why="The user asked for a place for the new site.", state="waiting_user")
        self._add_request("add_folder", "work", "laptop", "user", state="lost", reason="runner_gone",
                          claimed_at=AT, result_path="D:/code/work")
        self._add_request("remove_folder", "archive", "gone", "user", path="/srv/archive", state="lost",
                          reason="no_result", claimed_at=AT)
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
                return 200, {"projects": sorted(self.projects.values(), key=lambda p: p["name"]),
                             "agents": [{"name": "claude", "installed": True, "resting_until": None,
                                         "modes": ["default", "plan"]},
                                        {"name": "codex", "installed": True, "resting_until": None, "modes": []}],
                             "profiles": [{"name": "careful", "agent": "claude", "description": "Asks first",
                                           "options": {}, "fresh": False}],
                             "models": {"claude": ["opus", "sonnet"]}, "efforts": {},
                             "runners": [{"name": name, "agents": d["agents"], "projects": sorted(d["folders"]),
                                          "connected": True, "approve": d["approve"], "folders": d["folders"]}
                                         for name, d in self.devices.items()],
                             "held": list(self.held)}
            if op.startswith("project") or op == "runner_dirs":
                return self._project_op(op, body)
            if op == "sessions":
                if body.get("state") == "closed":  # H2: the closed ones, the latest closed first
                    closed = [s for s in self.sessions.values() if s["state"] == "closed"]
                    return 200, {"sessions": sorted(closed, key=lambda s: (s["closed_at"] or "", s["id"]),
                                                    reverse=True)[:int(body.get("limit", 100))]}
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
            if op == "reopen":
                return self._reopen(body)
            if op == "rename":
                return self._rename(body)
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
                elif name == "hub_project_request":
                    status, reply = self._ask(args, "model")
                    if status != 200:
                        result = {"content": [{"type": "text", "text": reply["error"]}], "isError": True}
                    else:  # H11: the model's view has no device path it did not give, no pairing code
                        shown = {k: v for k, v in reply["request"].items() if k not in ("code", "result_path")}
                        result = {"content": [{"type": "text",
                                               "text": json.dumps({"request": shown}, ensure_ascii=False)}]}
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
        self._touch(sid, state="closed", closed_reason="user", activity="closed", queued=0,
                    closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return 200, {"closed": sid, "cancelled": cancelled}

    def _reopen(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """H2 (hub docs/sessions-h2-v0.1.md §3): a closed session, open again, unless nothing is left to resume."""
        sid = body.get("session")
        found = self.sessions.get(sid) if isinstance(sid, int) else None
        if found is None:
            return 400, {"error": f"없는 세션: {sid}"}
        if found["state"] == "closed":
            if found["closed_reason"] in ("forgot", "not_started", "limited"):
                return 400, {"error": "에이전트가 잃은 세션은 다시 열 수 없습니다. 새 대화를 여세요"}
            self._touch(sid, state="open", closed_reason=None, closed_at=None, activity="idle")
        return 200, {"reopened": sid}

    def _rename(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """H2 (§2): one line of up to 80 characters; empty or null clears the name."""
        sid = body.get("session")
        if not isinstance(sid, int) or sid not in self.sessions:
            return 400, {"error": f"없는 세션: {sid}"}
        if "title" not in body:
            return 400, {"error": "title을 주세요(지우려면 null이나 빈 글)"}
        title = (body["title"] or "").strip() or None
        if title is not None and len(title) > 80:
            return 400, {"error": "세션 이름은 80자까지입니다"}
        title_before = self.sessions[sid].get("title")
        self.sessions[sid] = {**self.sessions[sid], "title": title}  # a name is not activity: updated_at stays
        if title != title_before:
            self.generation += 1
            self.changed[sid] = self.generation
        return 200, {"session": sid, "title": title}

    # -- projects and device folders (V3-P103) ----------------------------------------------------------------------

    def _add_request(self, kind: str, project: str, runner: str, origin: str, **fields: Any) -> dict[str, Any]:
        rid = self.next_request
        self.next_request += 1
        request = {"id": rid, "kind": kind, "project": project, "runner": runner, "origin": origin, "path": None,
                   "create_dir": False, "agent": None, "why": None, "code": None, "mode": None,
                   "state": "waiting_device", "rev": 1, "result_path": None, "reason": None, "resolving": False,
                   "created_at": _now(), "decided_at": None, "expires_at": None, "claimed_at": None,
                   "ended_at": None, "same_name_on": [], **fields}
        self.requests[rid] = request
        self._request_changed(rid)
        return request

    def _request_changed(self, rid: int) -> None:
        self.generation += 1
        self.request_changes[rid] = self.generation

    def _projects_changed(self) -> None:
        self.generation += 1
        self.projects_generation = self.generation

    def _change(self, rid: int, **fields: Any) -> dict[str, Any]:
        request = self.requests[rid]
        request.update(fields, rev=request["rev"] + 1)
        if fields.get("state") in _ENDED:
            request["ended_at"] = _now()
        self._request_changed(rid)
        return request

    def _view(self, request: dict[str, Any]) -> dict[str, Any]:
        shown = dict(request)
        if shown["kind"] == "create":
            shown["same_name_on"] = sorted(h["runner"] for h in self.held if h["project"] == shown["project"])
        return shown

    def _later(self, seconds: float, work: Any) -> None:
        def run() -> None:
            time.sleep(seconds)
            with self.lock:
                work()
        threading.Thread(target=run, daemon=True).start()

    def _send_to_device(self, request: dict[str, Any]) -> None:
        """The device takes it: local after its person picks the folder, user and removals at once."""
        rid, device = request["id"], self.devices[request["runner"]]
        wait = PICK_SECONDS if request["mode"] == "local" else DEVICE_SECONDS

        def claim() -> None:
            if self.requests[rid]["state"] != "waiting_device":
                return
            self._change(rid, state="applying", claimed_at=_now())
            self._later(DEVICE_SECONDS / 2, apply)

        def apply() -> None:
            now = self.requests[rid]
            if now["state"] != "applying":
                return
            self._apply(now, device)

        self._later(wait, claim)

    def _apply(self, request: dict[str, Any], device: dict[str, Any]) -> None:
        name = request["project"]
        if request["kind"] == "remove_folder":
            device["folders"].pop(name, None)
            self._change(request["id"], state="done")
        else:
            where = request["path"] or f"D:/code/{name}"
            device["folders"][name] = where
            if name not in self.projects:
                self.projects[name] = {"name": name, "path": where, "default_agent": request["agent"],
                                       "preset": None, "profile": None}
            self._change(request["id"], state="done", result_path=where)
        self._projects_changed()

    def _device(self, runner: object, adding: bool) -> tuple[dict[str, Any] | None, str | None]:
        device = self.devices.get(runner) if isinstance(runner, str) else None
        if device is None:
            return None, f"기기 {runner}가 연결돼 있지 않습니다"
        if adding and device["approve"]["mode"] == "off":
            return None, f"기기 {runner}는 원격 등록을 받지 않습니다 (그 기기에서 himari-hub runner approve)"
        return device, None

    def _keyed(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """H13: a key seen before gives its request back (or is refused when the input differs)."""
        key = body.get("client_key")
        if key is None:
            return None
        given = json.dumps({k: v for k, v in body.items() if k != "client_key"}, sort_keys=True)
        found = self.keys.get(key)
        if found is None:
            return None
        if found[:2] != (op, given):
            return 400, {"error": "client_key_reused: 이 키는 다른 요청에 쓰였습니다"}
        return 200, {"request": self._view(self.requests[found[2]])}

    def _remember(self, op: str, body: dict[str, Any], rid: int) -> None:
        if body.get("client_key") is not None:
            given = json.dumps({k: v for k, v in body.items() if k != "client_key"}, sort_keys=True)
            self.keys[body["client_key"]] = (op, given, rid)

    def _ask(self, body: dict[str, Any], origin: str) -> tuple[int, dict[str, Any]]:
        kind, project = body.get("kind"), body.get("project")
        if kind not in ("create", "add_folder") or not isinstance(project, str) or not project.strip():
            return 400, {"error": "kind(create, add_folder)와 project가 필요합니다"}
        if kind == "create" and project in self.projects:
            return 400, {"error": f"이미 있는 프로젝트입니다: {project}"}
        if kind == "add_folder" and project not in self.projects:
            return 400, {"error": f"없는 프로젝트입니다: {project}"}
        device, refused = self._device(body.get("runner"), adding=True)
        if device is None:
            return 400, {"error": refused}
        if any(r["runner"] == body["runner"] and r["state"] in ("waiting_device", "applying")
               for r in self.requests.values()):
            return 400, {"error": f"기기 {body['runner']}에는 이미 처리 중인 요청이 있습니다"}
        mode = device["approve"]["mode"]
        path = body.get("path")
        if mode == "user" and (not isinstance(path, str) or not path.startswith(ROOT)):
            return 400, {"error": f"user 방식 기기에서는 roots 아래의 폴더가 필요합니다: {path}"}
        fields: dict[str, Any] = {"path": path, "create_dir": body.get("create_dir") is True,
                                  "agent": body.get("agent"), "why": body.get("why"), "mode": mode}
        if origin == "model":
            return 200, {"request": self._view(self._add_request(kind, project, body["runner"], origin,
                                                                 state="waiting_user", **fields))}
        request = self._add_request(kind, project, body["runner"], origin, expires_at=_later(300),
                                    code="482-913" if mode == "local" else None, **fields)
        self._send_to_device(request)
        return 200, {"request": self._view(request)}

    def _project_op(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        rid = body.get("id")
        request = self.requests.get(rid) if isinstance(rid, int) else None
        if op == "project_requests":
            if "ids" in body:  # H12
                ids = [i for i in body["ids"] if isinstance(i, int)][:50]
                return 200, {"requests": [self._view(self.requests[i]) for i in ids if i in self.requests],
                             "missing": [i for i in ids if i not in self.requests]}
            chosen = sorted(self.requests.values(), key=lambda r: -r["id"])
            if body.get("open"):
                chosen = [r for r in chosen if r["state"] not in _ENDED]
            else:
                chosen = chosen[:int(body.get("limit", 100))]
            return 200, {"requests": [self._view(r) for r in chosen]}
        if op == "project_request":
            again = self._keyed(op, body)
            if again is not None:
                return again
            status, reply = self._ask(body, "user")
            if status == 200:
                self._remember(op, body, reply["request"]["id"])
            return status, reply
        if op == "project_folder_remove":
            again = self._keyed(op, body)
            if again is not None:
                return again
            device, refused = self._device(body.get("runner"), adding=False)
            if device is None:
                return 400, {"error": refused}
            folder = device["folders"].get(body.get("project"))
            if folder is None:
                return 400, {"error": f"기기 {body.get('runner')}에 이 프로젝트의 폴더가 없습니다: {body.get('project')}"}
            request = self._add_request("remove_folder", body["project"], body["runner"], "user", path=folder,
                                        expires_at=_later(300))
            self._remember(op, body, request["id"])
            self._send_to_device(request)
            return 200, {"request": self._view(request)}
        if op in ("project_request_answer", "project_request_cancel", "project_request_resolve") and request is None:
            return 400, {"error": f"없는 요청: {rid}"}
        if op == "project_request_answer":
            assert request is not None
            if request["state"] != "waiting_user":
                return 400, {"error": f"허가를 기다리는 요청이 아닙니다: #{rid} ({request['state']})"}
            if body.get("approve") is not True:
                return 200, {"request": self._view(self._change(rid, state="refused", reason="refused_by_user",
                                                                decided_at=_now()))}
            mode = self.devices.get(request["runner"], {}).get("approve", {}).get("mode")
            self._change(rid, state="waiting_device", decided_at=_now(), expires_at=_later(300),
                         code="482-913" if mode == "local" else None)
            self._send_to_device(request)
            return 200, {"request": self._view(request)}
        if op == "project_request_cancel":
            assert request is not None
            if request["state"] in ("waiting_user", "waiting_device"):
                self._change(rid, state="cancelled", reason="cancelled_by_user")
            return 200, {"request": self._view(request)}
        if op == "project_request_resolve":
            return self._resolve(request, body)
        if op == "project_set":
            project = self.projects.get(body.get("project"))
            if project is None:
                return 400, {"error": f"없는 프로젝트입니다: {body.get('project')}"}
            for name, field in (("agent", "default_agent"), ("profile", "profile"), ("preset", "preset")):
                if name in body:
                    project[field] = body[name] or None
            self._projects_changed()
            return 200, {"project": project}
        if op == "project_adopt":
            held = next((h for h in self.held if h["project"] == body.get("project")
                         and h["runner"] == body.get("runner")), None)
            device, refused = self._device(body.get("runner"), adding=False)
            if held is None or device is None:
                return 400, {"error": refused or "그 기기에서 보류된 이름이 아닙니다"}
            name = held["project"]
            self.held.remove(held)
            device["folders"].setdefault(name, f"{ROOT}/{name}" if body["runner"] == "linux" else f"D:/code/{name}")
            self.projects.setdefault(name, {"name": name, "path": device["folders"][name], "default_agent": None,
                                            "preset": None, "profile": None})
            self._projects_changed()
            return 200, {"project": self.projects[name]}
        if op == "runner_dirs":
            device, refused = self._device(body.get("runner"), adding=False)
            if device is None or device["approve"]["mode"] != "user":
                return 400, {"error": refused or "user 방식이 아닙니다"}
            path = body.get("path")
            if path is None:
                return 200, {"path": None, "dirs": list(device["approve"]["roots"])}
            if path not in self.dirs:
                return 400, {"error": "roots 아래의 폴더가 아닙니다"}
            return 200, {"path": path, "dirs": self.dirs[path]}
        return 400, {"error": f"op {op}"}

    def _resolve(self, request: dict[str, Any] | None, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert request is not None
        rid = request["id"]
        if request["state"] != "lost":
            return 400, {"error": f"적용됐는지 모르는 요청이 아닙니다: #{rid} ({request['state']})"}
        if request["resolving"]:
            return 400, {"error": f"resolving: 요청 #{rid}을 이미 정리하는 중입니다 (기기에 묻는 중)"}
        if body.get("force") is True:
            applied = body.get("outcome") == "applied"
            self._settle(request, applied, "resolved_by_user")
            return 200, {"request": self._view(request)}
        if request["runner"] not in self.devices:
            return 400, {"error": f"기기가 연결돼야 정리할 수 있습니다: 기기 {request['runner']}가 연결돼 있지 않습니다"}
        self._change(rid, resolving=True)  # H17: the fence runs; the lock is let go while the device answers
        self.lock.release()
        try:
            time.sleep(DEVICE_SECONDS)
        finally:
            self.lock.acquire()
        self._settle(request, True, "resolved_by_fence")
        return 200, {"request": self._view(request)}

    def _settle(self, request: dict[str, Any], applied: bool, reason: str) -> None:
        if applied and request["kind"] != "remove_folder":
            device = self.devices.get(request["runner"])
            if device is not None:
                device["folders"][request["project"]] = request["result_path"] or f"D:/code/{request['project']}"
            if request["kind"] == "create" and request["project"] not in self.projects:
                self.projects[request["project"]] = {"name": request["project"], "path": "", "preset": None,
                                                     "default_agent": request["agent"], "profile": None}
        self._change(request["id"], state="done" if applied else "failed", reason=reason, resolving=False)
        self._projects_changed()

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
            told = self.generation
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
                # V3-P103: each request that changed, then a bare ``projects`` when projects or devices did
                for rid, generation in sorted(self.request_changes.items(), key=lambda item: item[1]):
                    if generation > told:
                        self._frame(out, "project_request", self._view(self.requests[rid]))
                if self.projects_generation > told:
                    self._frame(out, "projects", {})
                told = self.generation

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
