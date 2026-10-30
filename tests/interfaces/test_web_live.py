"""The live output stream (V3-P73 §6.1): what a browser is sent, against a
fake Runner state (coalesced wakeups, a Run that changed, the end, the
job's end, the time limit, shutdown), and the route over a real
Application (who may read it, how many at once)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import ConversationId, PrincipalId, RunId, TrustedScope, split_marks  # noqa: E402
from himari.control.api import JobStatus, LiveOutput  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web import live  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Server:
    def __init__(self, tmp_path: Path, provider: ScriptedAdapter) -> None:
        self.provider = provider
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                           data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                           sandbox=False)
        self.client = TestClient(web.create_app(config, opener))

    def sign_in(self) -> None:
        assert self.application is not None
        response = self.client.post("/api/session", json={"token": self.application.operator_secret}, headers=SEND)
        assert response.status_code == 204, response.text

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(path, json=body if body is not None else {}, headers=SEND)

    def wait_idle(self, cid: str) -> None:
        for _ in range(200):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("still running")

SCOPE = TrustedScope(PrincipalId("operator"), ConversationId("conv_1"))
R1, R2 = RunId("run_1"), RunId("run_2")


class FakeLive:
    """A Runner's live state; each wait applies the next scripted changes at once."""

    def __init__(self, changes: list[Callable[[FakeLive], None]]) -> None:
        self.run_id: RunId | None = None
        self.text = ""
        self.tools: list[str] = []
        self.thinking = self.truncated = self.ended = False
        self.version = 0
        self.running = True
        self.job = JobStatus(running=True)  # V3-P84 §6.3: whose job_id the frames carry
        self._changes = changes

    def start(self, run_id: RunId) -> None:
        self.run_id, self.text, self.tools, self.ended = run_id, "", [], False
        self.version += 1

    def write(self, text: str = "", tool: str | None = None, *, thinking: bool = False) -> None:
        self.text += text
        if tool:
            self.tools.append(tool)
        self.thinking |= thinking
        self.version += 1

    def end(self) -> None:
        self.ended = True
        self.version += 1

    def live_output(self, scope: TrustedScope, *, text: bool = True) -> LiveOutput | None:
        if self.run_id is None:
            return None
        return LiveOutput(self.run_id, self.text if text else "", tuple(self.tools), self.thinking, self.truncated,
                          self.ended, self.version, len(self.text))

    def live_text(self, scope: TrustedScope, run_id: RunId, start: int) -> str | None:
        return self.text[start:] if run_id == self.run_id else None

    def live_version(self, scope: TrustedScope) -> int:
        return self.version

    async def wait_live(self, scope: TrustedScope, after_version: int, timeout: float) -> int:
        await asyncio.sleep(0)
        if self._changes:
            self._changes.pop(0)(self)
        return self.version

    def status(self, scope: TrustedScope) -> JobStatus:
        return replace(self.job, running=self.running)


def events(source: FakeLive, closing: asyncio.Event | None = None, **options: Any) -> list[tuple[str, Any]]:
    async def collect() -> list[str]:
        return [f async for f in live.stream(source, SCOPE, closing or asyncio.Event(), **options)]

    out: list[tuple[str, Any]] = []
    for frame in asyncio.run(collect()):
        if frame.startswith("event: "):
            head, data = frame.strip().split("\n")
            out.append((head[len("event: "):], json.loads(data[len("data: "):])))
        else:
            out.append((frame.strip(), None))
    return out


def text_of(sent: list[tuple[str, Any]]) -> str:
    return "".join(d["text"] for e, d in sent if e in ("snapshot", "delta"))


def finish(source: FakeLive) -> None:
    source.end()
    source.running = False


def test_coalesced_changes_lose_nothing_and_end_with_done() -> None:
    def many(s: FakeLive) -> None:  # three pieces, a tool and thinking before the stream wakes once
        s.write("Hel", thinking=True)
        s.write("lo, ")
        s.write("Seoul", tool="echo")

    source = FakeLive([lambda s: s.start(R1), many, lambda s: s.write("."), finish])
    sent = events(source)
    assert sent[0] == ("retry: 1000", None) and sent[1] == ("idle", {})
    assert sent[2] == ("snapshot", {"run_id": R1, "job_id": None, "text": "", "moods": [], "moves": [], "tools": [],
                                    "thinking": False, "truncated": False, "ended": False})
    assert sent[3] == ("delta", {"run_id": R1, "job_id": None, "text": "Hello, Seoul", "moods": [], "moves": [],
                                 "tools": ["echo"], "thinking": True})
    assert text_of(sent) == "Hello, Seoul."
    assert sent[-2:] == [("end", {"run_id": R1, "job_id": None}), ("done", {})]


def test_a_run_that_changed_meanwhile_is_resynchronized_by_a_snapshot() -> None:
    def next_run(s: FakeLive) -> None:  # R1 ended and R2 began before the stream woke
        s.write(" lost?")
        s.end()
        s.start(R2)
        s.write("second")

    source = FakeLive([lambda s: s.start(R1), lambda s: s.write("first"), next_run, finish])
    sent = events(source)
    snapshots = [d for e, d in sent if e == "snapshot"]
    assert [s["run_id"] for s in snapshots] == [R1, R2] and snapshots[1]["text"] == "second"
    ends = [d["run_id"] for e, d in sent if e == "end"]
    assert ends == [R2]  # never guessed


def test_connecting_after_the_job_sends_the_ended_state_then_done() -> None:
    source = FakeLive([])
    source.start(R1)
    source.write("all of it")
    finish(source)
    sent = events(source)
    assert [e for e, _ in sent] == ["retry: 1000", "snapshot", "done"]
    assert sent[1][1]["text"] == "all of it" and sent[1][1]["ended"] is True


def moods_of(sent: list[tuple[str, Any]]) -> list[tuple[int, str]]:
    return [(m["at"], m["mood"]) for e, d in sent if e in ("snapshot", "delta") for m in d["moods"]]


def test_mood_marks_come_out_of_the_text_as_moods() -> None:
    """V3-P83 §7.3: a mark cut between wakeups waits until it is known."""
    pieces = ["[[mood:hap", "py]]좋아, ", "해 볼게. [", "[mood:thinking]] 음", "… `[[mood:sad]]` [["]
    source = FakeLive([lambda s: s.start(R1), *[(lambda s, p=p: s.write(p)) for p in pieces], finish])
    sent = events(source)
    assert text_of(sent) == "좋아, 해 볼게.  음… `[[mood:sad]]` [["  # the last [[ is known as text at the end
    assert moods_of(sent) == [(0, "happy"), (10, "thinking")]
    deltas = [d["text"] for e, d in sent if e == "delta"]
    assert deltas[0] == "좋아, " and deltas[-1] == "[["  # held back until the Run ended
    plain, moods, _, _ = split_marks("".join(pieces), complete=True)
    assert (text_of(sent), moods_of(sent)) == (plain, list(moods))  # the same as the recorded answer's display


def test_a_snapshot_mid_mark_holds_its_end_back() -> None:
    """Connecting while a mark is half written: the snapshot stops before it."""
    source = FakeLive([lambda s: s.write("py]] 됐다"), finish])
    source.start(R1)
    source.write("시작 [[mood:hap")
    sent = events(source)
    snapshot = sent[1][1]
    assert snapshot["text"] == "시작 " and snapshot["moods"] == []
    assert text_of(sent) == "시작  됐다" and moods_of(sent) == [(3, "happy")]


def test_a_fence_while_streaming_keeps_its_marks_as_text() -> None:
    source = FakeLive([lambda s: s.start(R1), lambda s: s.write("코드:\n``"), lambda s: s.write("`\n[[mood:sad]]\n"),
                       lambda s: s.write("```\n[[mood:happy]]끝"), finish])
    sent = events(source)
    assert text_of(sent) == "코드:\n```\n[[mood:sad]]\n```\n끝"
    assert moods_of(sent) == [(len("코드:\n```\n[[mood:sad]]\n```\n"), "happy")]


def test_a_long_answer_is_split_from_where_it_stood_not_from_its_start() -> None:
    """Re-review N1: each wakeup reads only what it has not confirmed yet."""
    read: list[int] = []

    class Counting(FakeLive):
        def live_text(self, scope: TrustedScope, run_id: RunId, start: int) -> str | None:
            text = super().live_text(scope, run_id, start)
            read.append(len(text or ""))
            return text

    piece = "가나다라 [[mood:happy]] `x` "
    source = Counting([lambda s: s.start(R1), *[(lambda s: s.write(piece)) for _ in range(2000)], finish])
    sent = events(source)
    assert len(moods_of(sent)) == 2000
    assert sum(read) <= 2 * len(source.text)  # not the 2,000 × half the text that starting over reads


def test_each_run_counts_its_moods_from_zero() -> None:
    def second(s: FakeLive) -> None:
        s.end()
        s.start(R2)
        s.write("[[mood:sad]]둘")

    source = FakeLive([lambda s: s.start(R1), lambda s: s.write("하나[[mood:happy]]"), second, finish])
    sent = events(source)
    snapshots = [d for e, d in sent if e == "snapshot"]
    assert snapshots[-1]["run_id"] == R2 and snapshots[-1]["moods"] == [{"at": 0, "mood": "sad"}]


def test_no_state_and_no_job_is_idle_then_done() -> None:
    source = FakeLive([])
    source.running = False
    assert [e for e, _ in events(source)] == ["retry: 1000", "idle", "done"]


def test_a_quiet_stream_sends_keep_alive_comments() -> None:
    source = FakeLive([lambda s: None, lambda s: None, finish])
    source.start(R1)
    assert [e for e, _ in events(source)].count(": keep-alive") == 2


def test_the_time_limit_ends_a_stream_without_done() -> None:
    now = [0.0]

    def later(s: FakeLive) -> None:
        now[0] += 400  # past MAX_SECONDS: the browser reconnects

    source = FakeLive([later] * 3)
    source.start(R1)
    sent = events(source, clock=lambda: now[0])
    assert "done" not in [e for e, _ in sent] and sent[-1][0] in (": keep-alive", "snapshot")


def test_shutdown_ends_every_stream_at_once() -> None:
    async def scenario() -> list[str]:
        closing = asyncio.Event()

        class Forever(FakeLive):
            async def wait_live(self, scope: TrustedScope, after_version: int, timeout: float) -> int:
                await asyncio.sleep(3600)
                return self.version

        source = Forever([])
        source.start(R1)
        frames: list[str] = []

        async def read() -> None:
            async for frame in live.stream(source, SCOPE, closing):
                frames.append(frame)

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.01)
        closing.set()
        await asyncio.wait_for(reader, 1)  # not an hour
        return frames

    frames = asyncio.run(scenario())
    assert frames[-1].startswith("event: snapshot")


# --- the route -----------------------------------------------------------------------------


@pytest.fixture
def streaming_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, ScriptedAdapter([final("a streamed answer")], streaming=True))
    with s.client:
        yield s


def read_stream(server: Server, path: str) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    with server.client.stream("GET", path) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        event = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                sent.append((event, line[len("data: "):]))
    return sent


def test_the_route_streams_the_conversations_own_output(streaming_server: Server) -> None:
    server = streaming_server
    assert server.client.get("/api/conversations/conv_x/live").status_code == 401
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "hi"}).status_code == 202
    server.wait_idle(cid)
    sent = read_stream(server, f"/api/conversations/{cid}/live")
    assert [e for e, _ in sent] == ["snapshot", "done"]
    snapshot = json.loads(sent[0][1])
    assert snapshot["text"] == "a streamed answer" and snapshot["ended"] is True
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    assert items[-1]["kind"] == "assistant_final" and items[-1]["run_id"] == snapshot["run_id"]  # what it replaces
    assert server.client.get("/api/conversations/conv_nobody/live").status_code == 404  # another's, or none


def test_too_many_open_streams_are_refused(streaming_server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    server = streaming_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    monkeypatch.setattr(live, "PER_PRINCIPAL", 0)
    response = server.client.get(f"/api/conversations/{cid}/live")
    assert response.status_code == 429 and response.json()["error"]["code"] == "too_many_streams"


def test_begin_shutdown_sets_the_closing_event(tmp_path: Path) -> None:
    server = Server(tmp_path, ScriptedAdapter([]))
    with server.client:
        app = server.client.app
        assert not app.state.closing.is_set()  # type: ignore[attr-defined]
        web.begin_shutdown(app)  # type: ignore[arg-type]
        for _ in range(100):
            if app.state.closing.is_set():  # type: ignore[attr-defined]
                break
            asyncio.run(asyncio.sleep(0.01))
        assert app.state.closing.is_set()  # type: ignore[attr-defined]


def test_a_slot_is_taken_with_its_check_and_given_back_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #83 review B1: requests arriving together each take a slot before any stream starts.
    monkeypatch.setattr(live, "PER_PRINCIPAL", 2)
    slots = live.Slots()
    first, second = slots.take("me"), slots.take("me")
    assert first is not None and second is not None
    assert slots.take("me") is None and slots.take("you") is not None  # per principal
    first()
    first()  # given back once, however often it is released
    assert slots.open("me") == 1
    assert slots.take("me") is not None and slots.take("me") is None


def test_an_open_stream_holds_its_slot_until_it_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Over a real server: TestClient returns a response only once the app is done with it.
    uvicorn = pytest.importorskip("uvicorn")
    import httpx

    from himari.provider.scripted import StreamCut

    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(live, "PER_PRINCIPAL", 1)
    provider = ScriptedAdapter([StreamCut(("held ",), asyncio.Event())], streaming=True)
    holder = Server(tmp_path, provider)  # its opener and config; served by uvicorn below
    config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"127.0.0.1"}),
                       allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                       data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                       sandbox=False)

    async def opener() -> Application:
        holder.application = await Application.open(tmp_path / "data", provider, model_id="scripted-model",
                                                    sandbox=False, cleanup_seconds=None)
        return holder.application

    server = uvicorn.Server(uvicorn.Config(web.create_app(config, opener), host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            assert holder.application is not None
            token = holder.application.operator_secret
            assert client.post("/api/session", json={"token": token}, headers=SEND).status_code == 204
            cid = client.post("/api/conversations", json={}, headers=SEND).json()["id"]
            assert client.post(f"/api/conversations/{cid}/messages", json={"text": "go"}, headers=SEND).status_code == 202
            seen: list[str] = []
            with client.stream("GET", f"/api/conversations/{cid}/live") as held:
                lines = held.iter_lines()
                for line in lines:
                    if line.startswith("event: "):
                        seen.append(line[len("event: "):])
                        if line == "event: snapshot":
                            break
                assert client.get(f"/api/conversations/{cid}/live").status_code == 429  # its one slot is held
                assert client.post(f"/api/conversations/{cid}/cancel", headers=SEND).status_code == 202
                seen += [line[len("event: "):] for line in lines if line.startswith("event: ")]
            assert seen[-1] == "done"
            again = client.get(f"/api/conversations/{cid}/live")  # the slot came back
            assert again.status_code == 200 and "event: done" in again.text
    finally:
        server.should_exit = True
        thread.join(10)


def test_requests_arriving_together_cannot_pass_the_limit(streaming_server: Server,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #83 review B1: two requests whose responses have not started yet. Counting when a
    # stream began let both through; the slot is now taken with the check.
    from starlette.requests import Request

    from himari.bootstrap.operator import OPERATOR
    from himari.data.api import MessageOrigin, OriginKind

    server = streaming_server
    server.sign_in()
    cid = server.post("/api/conversations").json()["id"]
    monkeypatch.setattr(live, "PER_PRINCIPAL", 1)
    app = server.client.app
    endpoint = next(r.endpoint for r in app.routes  # type: ignore[attr-defined]
                    if getattr(r, "path", "") == "/api/conversations/{conversation_id}/live")
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"})

    async def together() -> tuple[Any, int | None]:
        first = await endpoint(cid, request, by=web.Caller(OPERATOR, MessageOrigin(OriginKind.WEB)))  # not started
        try:
            await endpoint(cid, request, by=web.Caller(OPERATOR, MessageOrigin(OriginKind.WEB)))
        except web.ApiError as error:
            return first, error.status
        return first, None

    first, refused = server.client.portal.call(together)  # type: ignore[union-attr]
    assert refused == 429
    first._release()  # it never ran: give its slot back by hand


def test_frames_carry_their_job_and_moves() -> None:
    """V3-P84 §6.2–§6.3: each frame names its Run's job; a mark split by a wakeup is held back whole."""
    first = JobStatus(running=True, generation=1, started_at=datetime(2026, 10, 30, 12, tzinfo=UTC))
    second = JobStatus(running=True, generation=2, started_at=datetime(2026, 10, 30, 12, 1, tzinfo=UTC))

    def next_job(s: FakeLive) -> None:
        s.end()
        s.job = second
        s.start(R2)
        s.write("[[move:9,9]]저기")

    def begin(s: FakeLive) -> None:
        s.job = first
        s.start(R1)

    source = FakeLive([begin, lambda s: s.write("음… [[move:10"), lambda s: s.write("43,88]]여기"), next_job, finish])
    sent = [(e, d) for e, d in events(source) if e in ("snapshot", "delta", "end")]
    assert {d["job_id"] for e, d in sent if d["run_id"] == R1} == {first.job_id}
    assert {d["job_id"] for e, d in sent if d["run_id"] == R2} == {second.job_id} != {first.job_id}
    moves = [(d["run_id"], m) for e, d in sent for m in d.get("moves", [])]
    assert moves == [(R1, {"at": 3, "x": 1043, "y": 88}), (R2, {"at": 0, "x": 9, "y": 9})]
    assert "".join(d["text"] for e, d in sent if d["run_id"] == R1 and e != "end") == "음… 여기"
