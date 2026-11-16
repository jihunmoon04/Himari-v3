"""The hub's whole user path over the Web (V3-P102 §6): its sessions, events, the user's own work on them, and the
live streams relayed to the browser, against a stand-in hub server."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
import queue
import threading
from collections.abc import AsyncGenerator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from webkit import ORIGIN, SEND  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

from himari.interfaces.web import hub_stream  # noqa: E402
from himari.interfaces.web.hub import (  # noqa: E402
    HubBusy,
    HubClient,
    HubNotFound,
    event_view,
    session_detail,
    task_view,
)

TOKEN = "user-secret"
SESSION = {"id": 5, "project": "demo", "agent": "claude", "runner": "linux", "runner_connected": True,
           "profile": None, "state": "open", "closed_reason": None, "activity": "running", "task": 9,
           "last_task": 8, "model": "opus", "effort": None, "context": {"used": 1200, "size": 200000},
           "created_at": "2026-11-16T10:00:00Z", "migrated": False, "closed_at": None, "first_seq": 1,
           "last_seq": 2, "live": True, "queued": 1, "current": True}
TASK = {"id": 9, "status": "queued", "agent": "claude", "runner": "linux", "profile": None, "fresh": False,
        "model": None, "effort": None, "mode": None, "prompt": "fix it", "created_at": "2026-11-16T10:00:00Z",
        "finished_at": None}
EVENTS: list[dict[str, Any]] = [{"seq": 1, "kind": "turn_start", "attempt": 3, "at": "2026-11-16T10:00:01Z",
           "body": {"task_id": 9, "prompt": "fix it", "agent": "claude", "model": "opus", "effort": None,
                    "mode": None, "prefix_chars": 0}},
          {"seq": 2, "kind": "tool", "attempt": 3, "at": "2026-11-16T10:00:02Z",
           "body": {"id": "t1", "title": "Edit a.py", "kind": "edit", "status": "completed",
                    "diffs": [{"path": "a.py", "old": None, "new": "x = 1\n"}]}}]


def sse(event: str, data: object, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n"


class Hub:
    """``/user/<op>`` and ``/user/stream`` as the hub serves them. A session stream sends ``session_frames`` and
    ends; the list stream sends what is put on ``list_frames`` until ``None``. ``busy``: no stream left."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies: dict[str, tuple[int, dict[str, Any]]] = {}
        self.streams: list[tuple[dict[str, list[str]], str | None]] = []
        self.session_frames: list[str] = [sse("state", {"first_seq": 1, "last_seq": 2, "reset": False,
                                                        "session": SESSION})] + [
            sse("event", event, event["seq"]) for event in EVENTS]
        self.list_frames: queue.Queue[str | None] = queue.Queue()
        self.busy = False
        self.token_file: Path | None = None
        self.ended = 0  # list streams whose reader went away (a write to it failed)
        self.on_stream: Callable[[], object] | None = None  # runs as a stream request arrives
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _reply(self, status: int, reply: dict[str, Any]) -> None:
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                op = self.path.removeprefix("/user/")
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "token"})
                hub.calls.append((op, body))
                status, reply = hub.replies.get(op, (200, {}))
                if status == 0:  # done, and the answer lost on the way: the connection drops
                    self.close_connection = True
                    return
                self._reply(status, reply)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "token"})
                query = parse_qs(parts.query)
                hub.streams.append((query, self.headers.get("Last-Event-ID")))
                if hub.on_stream is not None:
                    hub.on_stream()
                if hub.busy:
                    return self._reply(503, {"error": "streams"})
                if query.get("session") == ["404"]:
                    return self._reply(404, {"error": "session"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if "session" in query:
                        for text in hub.session_frames:
                            self.wfile.write(text.encode())
                            self.wfile.flush()
                        return
                    while (item := hub.list_frames.get()) is not None:
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                except OSError:
                    hub.ended += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.list_frames.put(None)
        self.server.shutdown()
        self.server.server_close()


def client_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **hub_settings: Any) -> TestClient:
    """A signed-in Web server (as in test_web_hub)."""
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    opened: list[Application] = []

    async def opener() -> Application:
        opened.append(await Application.open(tmp_path / "data", ScriptedAdapter([]), model_id="scripted-model",
                                             sandbox=False, cleanup_seconds=None))
        return opened[0]

    config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                       allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(tmp_path / "data"),
                       model_id="scripted-model", reasoning_effort=None, sandbox=False, **hub_settings)
    client = TestClient(web.create_app(config, opener))
    client.__enter__()
    assert client.post("/api/session", json={"token": opened[0].operator_secret}, headers=SEND).status_code == 204
    return client


@pytest.fixture
def hub() -> Iterator[Hub]:
    started = Hub()
    yield started
    started.close()


@pytest.fixture
def client(tmp_path: Path, hub: Hub, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    token = tmp_path / "user.token"
    token.write_text(TOKEN + "\n", encoding="ascii")
    started = client_for(tmp_path, monkeypatch, hub_url=hub.url, hub_user_token_file=str(token))
    yield started
    started.__exit__(None, None, None)


def frames(text: str) -> list[tuple[str | None, str, Any]]:
    """(id, event, data) of each frame with an event; comments and ``retry`` left out."""
    out: list[tuple[str | None, str, Any]] = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            out.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
    return out


# -- the views ----------------------------------------------------------------------------------

def test_an_event_keeps_only_the_contracts_fields_of_the_right_type() -> None:
    shown = event_view({"seq": 4, "kind": "tool", "attempt": 1, "at": "t", "extra": "x",
                        "body": {"id": "t1", "status": "in_progress", "input": 7, "secret": "no",
                                 "diffs": [{"path": "a", "old": None, "new": "b", "cut": True}, "bad"]}})
    assert shown == {"seq": 4, "kind": "tool", "attempt": 1, "at": "t",
                     "body": {"id": "t1", "status": "in_progress",  # input of the wrong type: left out
                              "diffs": [{"path": "a", "old": None, "new": "b", "cut": True}]}}
    # an update names only what changed: nothing else is filled in, so a reader merges it as it is
    update = event_view({"seq": 5, "kind": "tool", "body": {"id": "t1", "status": "completed"}})
    assert update is not None and update["body"] == {"id": "t1", "status": "completed"}
    # an unknown kind keeps its place (its seq moves the cursor), not its body
    assert event_view({"seq": 6, "kind": "future", "at": "t", "body": {"x": 1}}) == {
        "seq": 6, "kind": "future", "at": "t", "body": {}}
    assert event_view({"kind": "text", "body": {}}) is None and event_view({"seq": True, "kind": "text"}) is None
    long = event_view({"seq": 7, "kind": "text", "body": {"text": "가" * 100_000}})
    assert long is not None and len(long["body"]["text"]) == 70_000


def test_a_session_and_a_task_as_the_page_gets_them() -> None:
    detail = session_detail({**SESSION, "token": TOKEN, "queued": [{"id": 10, "prompt": "next", "created_at": "t"}],
                             "attempts": [{"id": 3, "task_id": 9, "outcome": None, "why": "x" * 900}],
                             "pending": [{"id": 1, "task_id": 9, "kind": "permission", "asks": "Write",
                                          "options": [{"option_id": "allow", "name": "Yes", "kind": "allow_once"}]}]})
    assert detail is not None and TOKEN not in json.dumps(detail)
    assert detail["queued"] == [{"id": 10, "prompt": "next", "created_at": "t"}]
    assert detail["attempts"][0]["outcome"] is None and len(detail["attempts"][0]["why"]) == 900
    assert detail["pending"][0]["options"] == [{"option_id": "allow", "name": "Yes", "kind": "allow_once"}]
    task = task_view({**TASK, "session": 5})
    assert task is not None and task["session"] == 5 and task_view({"status": "done"}) is None


# -- the calls ----------------------------------------------------------------------------------

def test_the_reads_reach_the_hub_and_come_back_as_views(client: TestClient, hub: Hub) -> None:
    hub.replies = {"projects": (200, {"projects": [{"name": "demo", "path": "/p", "default_agent": "claude",
                                                    "preset": None, "profile": None}],
                                      "agents": [{"name": "claude", "installed": True, "resting_until": None,
                                                  "modes": ["default", "plan"]}],
                                      "models": {"claude": ["opus"]}, "efforts": {}, "profiles": [], "runners": []}),
                   "sessions": (200, {"sessions": [SESSION, {"no": "id"}]}),
                   "session": (200, {**SESSION, "pending": [], "attempts": [], "queued": []}),
                   "events": (200, {"events": EVENTS, "first_seq": 1, "last_seq": 2, "next": 2, "more": False,
                                    "reset": False})}
    projects = client.get("/api/hub/projects").json()
    assert projects["agents"][0]["modes"] == ["default", "plan"] and projects["projects"][0]["name"] == "demo"
    assert client.get("/api/hub/sessions", params={"project": "demo", "closed": "true"}).json() == {
        "sessions": [SESSION]}
    assert client.get("/api/hub/sessions/5").json()["queued"] == []
    page = client.get("/api/hub/sessions/5/events", params={"after": 0, "limit": 2}).json()
    assert [event["seq"] for event in page["events"]] == [1, 2] and page["next"] == 2
    assert hub.calls == [("projects", {}), ("sessions", {"project": "demo", "closed": True, "limit": 100}),
                         ("session", {"id": 5}), ("events", {"session": 5, "after": 0, "limit": 2})]
    assert TOKEN not in json.dumps([projects, page])


def test_the_users_own_work_on_a_session(client: TestClient, hub: Hub) -> None:
    hub.replies = {"send": (200, {"task": {**TASK, "session": 5}}), "new": (200, {"task": TASK}),
                   "close": (200, {"closed": 5, "cancelled": [10, "x"]}),
                   "preset": (200, {"project": "demo", "preset": "careful"})}
    sent = client.post("/api/hub/sessions/5/messages", json={"prompt": "go on", "mode": "plan"}, headers=SEND)
    assert sent.status_code == 200 and sent.json()["task"]["session"] == 5
    started = client.post("/api/hub/sessions", json={"project": "demo", "prompt": "start", "agent": "codex"},
                          headers=SEND)
    assert started.json() == {"task": TASK}
    assert client.post("/api/hub/sessions/5/close", headers=SEND).json() == {"closed": 5, "cancelled": [10]}
    assert client.put("/api/hub/projects/demo/preset", json={"preset": "careful"}, headers=SEND).json() == {
        "project": "demo", "preset": "careful"}
    assert hub.calls == [("send", {"session": 5, "prompt": "go on", "mode": "plan"}),
                         ("new", {"project": "demo", "prompt": "start", "agent": "codex"}),
                         ("close", {"session": 5}), ("preset", {"project": "demo", "preset": "careful"})]
    for bad in ({"prompt": ""}, {"prompt": "x", "other": 1}, {"prompt": "x", "model": ""}):
        assert client.post("/api/hub/sessions/5/messages", json=bad, headers=SEND).status_code == 400, bad
    assert len(hub.calls) == 4  # nothing more reached the hub


def test_the_hubs_refusals_are_told_apart(client: TestClient, hub: Hub, tmp_path: Path) -> None:
    hub.replies = {"events": (400, {"error": "bad_cursor: after 9 > last_seq 2"}),
                   "send": (400, {"error": "열린 세션이 아님"})}
    cursor = client.get("/api/hub/sessions/5/events", params={"after": 9})
    assert cursor.status_code == 409 and cursor.json()["error"]["code"] == "hub_bad_cursor"
    refused = client.post("/api/hub/sessions/5/messages", json={"prompt": "x"}, headers=SEND)
    assert refused.status_code == 409 and refused.json()["error"] == {"code": "hub_refused",
                                                                      "message": "열린 세션이 아님"}
    (tmp_path / "user.token").write_text("wrong", encoding="ascii")
    wrong = client.get("/api/hub/sessions")
    assert wrong.status_code == 503 and wrong.json()["error"]["code"] == "hub_misconfigured"
    hub.close()
    (tmp_path / "user.token").write_text(TOKEN, encoding="ascii")
    gone = client.get("/api/hub/projects")
    assert gone.status_code == 503 and gone.json()["error"]["code"] == "hub_unavailable"


def test_without_a_hub_every_route_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(tmp_path, monkeypatch)
    try:
        assert client.get("/api/hub/sessions").status_code == 404
        assert client.get("/api/hub/stream").status_code == 404
        assert client.get("/api/hub/sessions/5/stream").status_code == 404
        assert client.post("/api/hub/sessions", json={"project": "p", "prompt": "x"}, headers=SEND).status_code == 404
    finally:
        client.__exit__(None, None, None)


# -- a session's live stream ---------------------------------------------------------------------

def test_a_sessions_stream_is_relayed_with_its_cursor(client: TestClient, hub: Hub) -> None:
    hub.session_frames.append(sse("event", {"seq": 3, "kind": "future", "at": "t", "body": {"x": 1}}, 3))
    hub.session_frames.append(": ping\n\n")
    with client.stream("GET", "/api/hub/sessions/5/stream", params={"after": 0},
                       headers={"Last-Event-ID": "1"}) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    shown = frames(text)
    assert shown[0][1] == "state" and shown[0][2]["session"]["id"] == 5 and shown[0][2]["reset"] is False
    assert [(seq, name) for seq, name, _ in shown[1:]] == [("1", "event"), ("2", "event"), ("3", "event"),
                                                         (None, "hub_unavailable")]  # the hub ended it
    assert shown[3][2]["body"] == {}  # an unknown kind keeps its place
    assert hub.streams[-1] == ({"session": ["5"], "after": ["0"]}, "1")  # the browser's own cursor
    assert TOKEN not in text


def test_a_sessions_stream_that_cannot_open_is_an_http_error(client: TestClient, hub: Hub) -> None:
    missing = client.get("/api/hub/sessions/404/stream")
    assert missing.status_code == 404
    hub.busy = True
    busy = client.get("/api/hub/sessions/5/stream")
    assert busy.status_code == 503 and busy.json()["error"]["code"] == "hub_stream_limit"


def test_the_session_streams_are_counted() -> None:
    async def scenario() -> None:
        class Opened(hub_stream.Upstream):
            async def open(self, params: dict[str, str], last_event_id: int | None = None) -> Any:
                if params["session"] == "2":
                    raise HubNotFound("session")
                return object(), object()

        streams = hub_stream.SessionStreams(Opened(HubClient("http://127.0.0.1:1", "none")), limit=1)
        await streams.open(1, 0, None)
        with pytest.raises(hub_stream.HubStreamLimit):
            await streams.open(1, 0, None)
        streams.release()
        with pytest.raises(HubNotFound):  # a refused open gives its slot back
            await streams.open(2, 0, None)
        assert streams.open_now == 0

    asyncio.run(scenario())


def run_session(lines: list[str], signed_in: Callable[[], bool], *, gap: float = 0.0, check: float = 0.05
                ) -> list[str]:
    async def scenario() -> list[str]:
        async def feed() -> Any:
            for line in lines:
                await asyncio.sleep(gap)
                yield line

        closing = asyncio.Event()
        out = []
        async for text in hub_stream.session_frames(AsyncExitStack(), hub_stream.messages(feed()),
                                                    signed_in, closing, check=check):
            out.append(text)
            if len(out) > 200:
                break
        return out

    return asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_signed_out_browser_loses_its_stream_even_while_events_flow() -> None:
    state = {"in": True}
    lines = []
    for seq in range(1, 60):
        lines += [f"id: {seq}", "event: event", 'data: {"seq": %d, "kind": "text", "body": {"text": "x"}}' % seq, ""]
    calls = 0

    def signed_in() -> bool:
        nonlocal calls
        calls += 1
        if calls >= 2:
            state["in"] = False
        return state["in"]

    out = run_session(lines, signed_in, gap=0.005, check=0.05)
    sent = [text for text in out if text.startswith("id: ")]
    assert 0 < len(sent) < 59 and "hub_unavailable" not in "".join(out)  # ended for the sign-in, not the hub


# -- the shared list -------------------------------------------------------------------------------

def run_list(hub: Hub, act: Callable[[hub_stream.ListRelay], Any]) -> Any:
    async def scenario() -> Any:
        client = HubClient(hub.url, str(hub.token_file))
        upstream = hub_stream.Upstream(client)
        relay = hub_stream.ListRelay(client, upstream)
        try:
            return await asyncio.wait_for(act(relay), 10)
        finally:
            await relay.stop()
            await upstream.aclose()

    return asyncio.run(scenario())


@pytest.fixture
def listed(hub: Hub, tmp_path: Path) -> Hub:
    hub.token_file = tmp_path / "user.token"
    hub.token_file.write_text(TOKEN, encoding="ascii")
    return hub


async def until(reader: hub_stream.ListReader, count: int) -> list[tuple[str | None, str, Any]]:
    got: list[tuple[str | None, str, Any]] = []
    while len(got) < count:
        await reader.ready.wait()
        got += frames("".join(reader.take()))
    return got


def test_one_upstream_for_every_browser_and_a_late_one_gets_the_whole_list(listed: Hub) -> None:
    other = {**SESSION, "id": 6, "activity": "idle"}

    async def act(relay: hub_stream.ListRelay) -> None:
        first = relay.join()
        listed.list_frames.put(sse("sessions", {"sessions": [SESSION, other]}))
        assert [s["id"] for s in (await until(first, 1))[0][2]["sessions"]] == [6, 5]
        listed.list_frames.put(sse("changed", {"sessions": [{**SESSION, "activity": "waiting"}]}))
        assert (await until(first, 1))[0][1:] == ("changed", {"sessions": [{**SESSION, "activity": "waiting"}]})
        late = relay.join()  # joins after: the whole list as it is now, merged
        whole = (await until(late, 1))[0]
        assert whole[1] == "sessions" and {s["id"]: s["activity"] for s in whole[2]["sessions"]} == {
            5: "waiting", 6: "idle"}
        assert len(listed.streams) == 1  # still one upstream
        listed.list_frames.put(None)  # the hub ends the stream; it comes back with a new whole list
        listed.list_frames.put(sse("sessions", {"sessions": [other]}))
        got = await until(first, 2)
        assert got[0] == (None, "hub", {"reachable": False})
        assert got[-1][1] == "sessions" and [s["id"] for s in got[-1][2]["sessions"]] == [6]  # replaced, not merged

    run_list(listed, act)


def test_with_no_stream_left_the_list_is_read_by_asking(listed: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_stream, "POLL_SECONDS", 0.05)
    listed.busy = True
    listed.replies["sessions"] = (200, {"sessions": [SESSION]})

    async def act(relay: hub_stream.ListRelay) -> None:
        reader = relay.join()
        got = await until(reader, 2)
        assert all(name == "sessions" for _, name, _ in got) and got[0][2]["sessions"][0]["id"] == 5
        assert ("sessions", {"limit": 500}) in listed.calls

    run_list(listed, act)


def test_a_browser_that_falls_behind_is_closed_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_stream, "QUEUE_FRAMES", 3)
    reader = hub_stream.ListReader()
    for _ in range(5):
        reader.offer("event: changed\ndata: {}\n\n")
    assert reader.behind and reader.take() == []

    async def scenario() -> list[str]:
        relay = hub_stream.ListRelay(HubClient("http://127.0.0.1:1", "none"),
                                     hub_stream.Upstream(HubClient("http://127.0.0.1:1", "none")))
        closing = asyncio.Event()
        return [text async for text in hub_stream.list_frames(relay, reader, lambda: True, closing)]

    assert asyncio.run(asyncio.wait_for(scenario(), 5)) == [f"retry: {hub_stream.RETRY_MS}\n\n"]


def test_the_list_upstream_closes_when_no_browser_is_left(listed: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_stream, "LINGER_SECONDS", 0.1)

    async def act(relay: hub_stream.ListRelay) -> None:
        reader = relay.join()
        listed.list_frames.put(sse("sessions", {"sessions": [SESSION]}))
        await until(reader, 1)
        relay.leave(reader)
        for _ in range(100):
            if not relay.running:
                break
            await asyncio.sleep(0.05)
        assert not relay.running
        for _ in range(100):  # and the hub sees its stream closed: its next writes fail
            if listed.ended:
                break
            listed.list_frames.put(": ping\n\n")
            await asyncio.sleep(0.05)
        assert listed.ended == 1

    run_list(listed, act)


def test_a_signed_out_browser_loses_its_list() -> None:
    async def scenario() -> list[str]:
        relay = hub_stream.ListRelay(HubClient("http://127.0.0.1:1", "none"),
                                     hub_stream.Upstream(HubClient("http://127.0.0.1:1", "none")))
        reader = hub_stream.ListReader()
        return [text async for text in hub_stream.list_frames(relay, reader, lambda: False, asyncio.Event(),
                                                              check=0.05)]

    assert asyncio.run(asyncio.wait_for(scenario(), 5)) == [f"retry: {hub_stream.RETRY_MS}\n\n"]


def test_a_busy_hub_is_told_apart() -> None:
    assert isinstance(hub_stream._refusal(503, b'{"error": "streams"}'), HubBusy)
    assert not isinstance(hub_stream._refusal(503, b'{"error": "stopping"}'), HubBusy)


def test_a_body_the_hub_cut_says_so() -> None:
    shown = event_view({"seq": 8, "kind": "tool", "truncated": True,
                        "body": {"id": "t2", "status": "completed", "output": "…", "truncated": True}})
    assert shown is not None and shown["truncated"] is True and shown["body"]["truncated"] is True
    unknown = event_view({"seq": 9, "kind": "future", "body": {"truncated": True}})
    assert unknown is not None and unknown["body"] == {}


def test_the_list_queue_counts_bytes_not_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_stream, "QUEUE_BYTES", 10)
    reader = hub_stream.ListReader()
    reader.offer("가가가가")  # 4 characters, 12 bytes
    assert reader.behind


def test_a_signed_out_browser_loses_its_list_even_while_changes_flow() -> None:
    async def scenario() -> tuple[int, bool]:
        relay = hub_stream.ListRelay(HubClient("http://127.0.0.1:1", "none"),
                                     hub_stream.Upstream(HubClient("http://127.0.0.1:1", "none")))
        reader = hub_stream.ListReader()
        checks = []

        def signed_in() -> bool:
            checks.append(1)
            return False

        async def changes() -> None:
            while True:
                reader.take()  # never falls behind: the stream must end for the sign-in alone
                reader.offer("event: changed\ndata: {}\n\n")
                await asyncio.sleep(0.002)

        feeding = asyncio.ensure_future(changes())
        try:
            sent = [text async for text in hub_stream.list_frames(relay, reader, signed_in, asyncio.Event(),
                                                                  check=0.05)]
        finally:
            feeding.cancel()
        return len(sent), bool(checks)

    count, checked = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert checked and count >= 1


def test_a_browser_that_leaves_leaves_no_task_behind() -> None:
    async def scenario() -> set[asyncio.Task[Any]]:
        async def endless() -> Any:
            seq = 0
            while True:
                seq += 1
                yield hub_stream.Message("event", json.dumps({"seq": seq, "kind": "text", "body": {"text": "x"}}),
                                         str(seq))

        before = asyncio.all_tasks()
        frames_of = cast(AsyncGenerator[str, None],
                         hub_stream.session_frames(AsyncExitStack(), endless(), lambda: True, asyncio.Event()))
        await anext(frames_of)  # retry
        await anext(frames_of)  # the first event; the pump has filled its queue behind it
        await asyncio.sleep(0.05)
        await frames_of.aclose()  # the browser left
        reader = hub_stream.ListReader()
        waiting = asyncio.ensure_future(hub_stream._next(reader.ready, asyncio.Event(), 10))
        await asyncio.sleep(0.01)
        waiting.cancel()  # a list browser that leaves while waiting
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.sleep(0.01)
        return asyncio.all_tasks() - before - {asyncio.current_task()}

    assert asyncio.run(asyncio.wait_for(scenario(), 5)) == set()


def test_a_prompt_whose_answer_was_lost_is_not_called_unsent(client: TestClient, hub: Hub) -> None:
    hub.replies = {"send": (0, {}), "new": (0, {})}
    for path, body in (("/api/hub/sessions/5/messages", {"prompt": "go"}),
                       ("/api/hub/sessions", {"project": "demo", "prompt": "go"})):
        lost = client.post(path, json=body, headers=SEND)
        assert lost.status_code == 503 and lost.json()["error"]["code"] == "hub_unknown", path
    assert [op for op, _ in hub.calls] == ["send", "new"]  # it did reach the hub
    hub.close()
    gone = client.post("/api/hub/sessions/5/messages", json={"prompt": "go"}, headers=SEND)
    assert gone.json()["error"]["code"] == "hub_unavailable"  # nothing reached it


def test_signed_out_while_the_session_stream_opened(client: TestClient, hub: Hub) -> None:
    hub.on_stream = lambda: client.delete("/api/session", headers=SEND)
    refused = client.get("/api/hub/sessions/5/stream")
    assert refused.status_code == 401
    hub.on_stream = None
    assert client.get("/api/hub/sessions").status_code == 401  # signed out indeed
