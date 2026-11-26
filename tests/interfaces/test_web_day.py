"""HIMARI's day over the Web API (character-day v0.1 §18.11): the day session listed with its date and whether it is
closed, opened for a device; a message a sleeping HIMARI met (202 ``asleep``), a closed day refusing one (409
``day_closed``), how each message was met in the events and the diaries written in the session's context; the
schedule written, moved and refused (409 ``plan_refused``); the sleep and the latest sleeps; a poke; the stream's
``himari_sleep`` and ``day_session``."""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import OPERATOR  # noqa: E402
from himari.common import ConversationId, TextBlock, TrustedScope, thaw  # noqa: E402
from himari.data.api import (  # noqa: E402
    NO_PERSONA,
    FellBy,
    UserMessage,
    LifeJob,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    PlanKind,
    PlanWrite,
    RawReceived,
)
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web import event_stream  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.life import keeper as day_keeper  # noqa: E402
from himari.life.day import session_title  # noqa: E402
from himari.presence import devices as presence_devices  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Bare:
    """The same client without the Web session's cookie: what a native app sends."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        saved = dict(self._client.cookies)
        self._client.cookies.clear()
        try:
            return self._client.request(method, url, **kwargs)
        finally:
            for name, value in saved.items():
                self._client.cookies.set(name, value)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


class Server:
    """A Web server with HIMARI's life and the companion on: its day keeper runs (at noon, tests/conftest.py)."""

    def __init__(self, tmp_path: Path) -> None:
        self.provider = ScriptedAdapter([], companion=True)
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None, companion=True, life=True)
            return self.application

        config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                           allowed_origins=frozenset({ORIGIN}), secure_cookie=False,
                           data_root=str(tmp_path / "data"), model_id="scripted-model", reasoning_effort=None,
                           sandbox=False)
        self.client = TestClient(web.create_app(config, opener))
        self.device = Bare(self.client)

    @property
    def app(self) -> Application:
        assert self.application is not None
        return self.application

    def call(self, function: Any, *args: Any) -> Any:
        return self.client.portal.call(function, *args)  # type: ignore[union-attr]

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(path, json=body, headers=SEND)

    def paired(self, name: str = "데스크톱") -> dict[str, str]:
        code = self.post("/api/devices", {"name": name}).json()["code"]
        token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        response = self.device.post("/api/devices/pair", json={"token": token},
                                    headers={"authorization": f"Pairing {code}"})
        assert response.status_code == 200, response.text
        return {"authorization": f"Bearer {token}"}

    def day(self) -> str:
        """Today's day session (GET /api/device opens it)."""
        return str(self.client.get("/api/device").json()["recent_conversation"]["id"])

    def asleep(self, hours: float = 1) -> None:
        """HIMARI fell asleep ``hours`` before the keeper's now (deep within three hours)."""
        at = day_keeper.wall_clock() - timedelta(hours=hours)
        self.call(lambda: self.app.data.open_sleep(OPERATOR, NO_PERSONA, at=at, how=FellBy.SCHEDULE))

    def wait(self, cid: str) -> None:
        for _ in range(200):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("still running")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(presence_devices, "FAILED_PAIRING_DELAY_SECONDS", 0)
    monkeypatch.setattr(event_stream, "MAX_SECONDS", 0.2)
    s = Server(tmp_path)
    with s.client:
        s.app.devices._failure_delay = 0  # type: ignore[attr-defined]
        response = s.client.post("/api/session", json={"token": s.app.operator_secret}, headers=SEND)
        assert response.status_code == 204, response.text
        yield s


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def frames_of(body: str) -> list[tuple[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        lines = block.strip().split("\n")
        if lines[0].startswith("event: "):
            out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def today(server: Server) -> str:
    return datetime.now(ZoneInfo(server.app.time_zone)).date().isoformat()


def test_a_device_is_given_todays_day_session_opened_when_none_is_and_the_list_shows_its_date(
        server: Server) -> None:
    me = server.paired()
    state = server.device.get("/api/device", headers=me).json()
    recent = state["recent_conversation"]
    assert recent["title"] == session_title(datetime.now(ZoneInfo(server.app.time_zone)).date())
    assert state["features"]["day_session"] == 1 and state["himari"] == {"sleep": "awake"}
    assert server.device.get("/api/device", headers=me).json()["recent_conversation"] == recent  # the same one
    assert server.client.get("/api/device").json()["recent_conversation"] == recent  # the Web's too
    hello = frames_of(server.device.get("/api/events", headers=me).text)[0]
    assert hello[0] == "hello" and hello[1]["recent_conversation"] == recent
    assert hello[1]["himari"] == {"sleep": "awake"} and hello[1]["features"]["day_session"] == 1
    other = server.post("/api/conversations", {}).json()["id"]
    listed = {c["id"]: c for c in server.client.get("/api/conversations").json()["items"]}
    assert (listed[recent["id"]]["day_session"], listed[recent["id"]]["closed"]) == (today(server), False)
    assert (listed[other]["day_session"], listed[other]["closed"]) == (None, False)
    detail = server.client.get(f"/api/conversations/{recent['id']}").json()["conversation"]
    assert (detail["day_session"], detail["closed"]) == (today(server), False)


def test_a_closed_day_session_refuses_a_message_records_nothing_and_a_new_one_opens(server: Server) -> None:
    cid = server.day()
    server.call(lambda: server.app.data.close_day_session(OPERATOR, ConversationId(cid), datetime.now(UTC)))
    refused = server.post(f"/api/conversations/{cid}/messages", {"text": "안녕"})
    assert (refused.status_code, error_code(refused)) == (409, "day_closed")
    assert server.client.get(f"/api/conversations/{cid}/events").json()["items"] == []  # nothing was recorded
    listed = {c["id"]: c for c in server.client.get("/api/conversations").json()["items"]}
    assert listed[cid]["closed"] is True
    assert server.day() != cid  # a new day session opens


def test_a_closed_day_is_never_continued_resumed_or_compacted(server: Server) -> None:
    """§3.2 (review 2): a day closed with a message still unanswered — nothing to continue; Continue and compaction
    are 409 ``day_closed``, as a message is, and nothing is written."""
    cid = server.day()
    scope = TrustedScope(OPERATOR, ConversationId(cid))
    server.call(lambda: server.app.data.append_event(scope, UserMessage((TextBlock("남은 말"),))))
    assert server.client.get(f"/api/conversations/{cid}").json()["continuable"] is True
    server.call(lambda: server.app.data.close_day_session(OPERATOR, ConversationId(cid), datetime.now(UTC)))
    assert server.client.get(f"/api/conversations/{cid}").json()["continuable"] is False
    for path in ("resume", "compact"):
        refused = server.client.post(f"/api/conversations/{cid}/{path}", headers=SEND)
        assert (refused.status_code, error_code(refused)) == (409, "day_closed"), path
    assert len(server.client.get(f"/api/conversations/{cid}/events").json()["items"]) == 1
    assert not server.client.get(f"/api/conversations/{cid}").json()["job"]["running"]


def test_a_day_fenced_for_its_closing_takes_no_new_message(server: Server) -> None:
    """§6 (review 3): while its last job settles, the day session is fenced — a message is 409 ``day_closed``
    (nothing recorded), Continue too; another conversation is not."""
    cid = server.day()
    keeper = server.app.keeper
    assert keeper is not None
    keeper._fenced[OPERATOR] = ConversationId(cid)  # noqa: SLF001 — as the keeper fences it at the closing hour
    keeper.busy = lambda scope: True  # its job still runs: the keeper's own looks leave the fence in place
    refused = server.post(f"/api/conversations/{cid}/messages", {"text": "아직 안 자?"})
    assert (refused.status_code, error_code(refused)) == (409, "day_closed")
    assert server.client.get(f"/api/conversations/{cid}/events").json()["items"] == []
    resumed = server.client.post(f"/api/conversations/{cid}/resume", headers=SEND)
    assert (resumed.status_code, error_code(resumed)) == (409, "day_closed")
    assert server.client.get(f"/api/conversations/{cid}").json()["continuable"] is False
    other = server.post("/api/conversations", {}).json()["id"]
    server.provider.extend([final("응")])
    assert server.post(f"/api/conversations/{other}/messages", {"text": "여기는?"}).status_code == 202


def test_a_sleeping_himari_meets_messages_with_nothing_then_a_mumble_then_waking(server: Server) -> None:
    """§5.5: deep sleep — the first message is only recorded (202 ``asleep: none``, no job, nothing to continue),
    the second gets a half-asleep turn with no Tools, the third wakes it; the events say how each was met."""
    cid = server.day()
    server.provider.extend([final("응, 놀자!")])  # an earlier job, while awake
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "놀자"}).json()["asleep"] is None
    server.wait(cid)
    server.asleep()
    shown = server.client.get("/api/life").json()["sleep"]
    assert (shown["state"], shown["deep"]) == ("asleep", True)  # (tonight's slot, placed as the turn began)
    first = server.post(f"/api/conversations/{cid}/messages", {"text": "자?"})
    assert first.status_code == 202 and first.json()["asleep"] == "none" and first.json()["running"] is False
    # review 6: a receipt of its own — no job, never the earlier job's id or result
    assert (first.json()["job_id"], first.json()["state"], first.json()["started_at"]) == (None, None, None)
    detail = server.client.get(f"/api/conversations/{cid}").json()
    assert detail["continuable"] is False and detail["job"]["running"] is False
    server.provider.extend([final("음냐… 오늘 재밌었…")])
    second = server.post(f"/api/conversations/{cid}/messages", {"text": "자는구나"})
    assert second.status_code == 202 and second.json()["asleep"] == "mumble"
    server.wait(cid)
    asked = server.provider.prepared_inputs[-1]
    assert asked.request_options.half_asleep and asked.request_options.day_session and asked.tool_catalog == ()
    server.provider.extend([final("으, 왜… 깼잖아.")])
    third = server.post(f"/api/conversations/{cid}/messages", {"text": "일어나!"})
    assert third.status_code == 202 and third.json()["asleep"] == "wake"
    server.wait(cid)
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    said = [(i["text"], i["while_asleep"]) for i in items if i["kind"] == "user_message"]
    assert said == [("놀자", None), ("자?", "none"), ("자는구나", "mumble"), ("일어나!", "wake")]
    assert [i["text"] for i in items if i["kind"] == "assistant_final"] == [
        "응, 놀자!", "음냐… 오늘 재밌었…", "으, 왜… 깼잖아."]
    life = server.client.get("/api/life").json()
    assert life["sleep"]["state"] == "awake"
    if "tonight" in life["sleep"]:  # placed by the keeper as a job ended: the slot's date, start and length
        assert set(life["sleep"]["tonight"]) == {"id", "day", "time", "minutes"}
    (record,) = life["sleeps"]
    assert {k: record[k] for k in ("how_fell", "how_woke", "messages", "mumbles", "closed_day")} == {
        "how_fell": "schedule", "how_woke": "user", "messages": 3, "mumbles": 1, "closed_day": False}
    assert record["fell_at"] and record["woke_at"]
    me = server.paired()  # a device is never told how a message was met
    device_items = server.device.get(f"/api/conversations/{cid}/events", headers=me).json()["items"]
    assert all("while_asleep" not in i for i in device_items)


def test_the_diary_written_in_the_day_session_is_a_line_of_its_timeline_for_the_owner(server: Server) -> None:
    cid = server.day()
    server.provider.extend([final("응, 좋았어!")])
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "오늘 좋았지?"}).status_code == 202
    server.wait(cid)

    async def written() -> None:
        data = server.app.data
        write_id = await data.begin_life_write(OPERATOR, LifeWriteStart(
            NO_PERSONA, today(server), "p", "m", b"{}", "j", 1, job=LifeJob.REVIEW,
            conversation_id=ConversationId(cid)))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        await data.apply_life_write(write_id, LifePlan("같이 이야기해서 좋은 하루였다."), now=datetime.now(UTC))

    server.call(written)
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    assert [i["kind"] for i in items] == ["user_message", "assistant_final", "diary"]
    diary = items[-1]
    assert (diary["sequence"], diary["text"]) == (items[-2]["sequence"], "같이 이야기해서 좋은 하루였다.")
    assert diary["created_at"]
    me = server.paired()
    assert [i["kind"] for i in server.device.get(f"/api/conversations/{cid}/events", headers=me).json()["items"]] == [
        "user_message", "assistant_final"]  # the owner's Web only


def test_the_schedule_is_written_moved_and_refused_with_the_schedules_own_sentence(server: Server) -> None:
    tomorrow = (datetime.now(ZoneInfo(server.app.time_zone)).date() + timedelta(days=1)).isoformat()
    added = server.post("/api/life/plans", {"day": tomorrow, "time": "15:00", "minutes": 90, "plan_kind": "own",
                                            "text": "산책", "persona_id": None})
    assert added.status_code == 201, added.text
    slot = added.json()
    assert {k: slot[k] for k in ("kind", "day", "time", "minutes", "plan_kind", "plan_state", "reason", "origin",
                                 "revision")} == {
        "kind": "plan", "day": tomorrow, "time": "15:00", "minutes": 90, "plan_kind": "own", "plan_state": None,
        "reason": None, "origin": "user", "revision": 1}
    moved = server.client.put(f"/api/life/plans/{slot['id']}", headers=SEND, json={
        "day": tomorrow, "time": "16:00", "minutes": 60, "plan_kind": "own", "text": "산책", "revision": 1})
    assert moved.status_code == 200 and (moved.json()["time"], moved.json()["revision"]) == ("16:00", 2)
    stale = server.client.put(f"/api/life/plans/{slot['id']}", headers=SEND, json={
        "day": tomorrow, "time": "17:00", "minutes": 60, "plan_kind": "own", "text": "산책", "revision": 1})
    assert (stale.status_code, error_code(stale)) == (409, "revision_conflict")
    for kind, at, words in (("sleep", "23:30", "A sleep slot is only moved, never made"),
                            ("sleep", "18:00", "A sleep slot starts between 21:00 and 05:00"),
                            ("together", "18:00", "Only HIMARI makes a together slot")):
        refused = server.post("/api/life/plans", {"day": tomorrow, "time": at, "minutes": 420, "plan_kind": kind,
                                                  "text": "x", "persona_id": None})
        assert (refused.status_code, error_code(refused)) == (409, "plan_refused"), kind
        assert words in refused.json()["error"]["message"]
    elsewhere = server.post("/api/life/plans", {"day": tomorrow, "time": "18:00", "minutes": 60, "plan_kind": "own",
                                                "text": "x", "persona_id": 7})
    assert (elsewhere.status_code, error_code(elsewhere)) == (409, "persona_changed")

    async def placed() -> int:  # the keeper's slot for tonight
        night = await server.app.data.write_plan(OPERATOR, NO_PERSONA, PlanWrite(tomorrow, "00:30", 480,
                                                                                 PlanKind.SLEEP, "잠"),
                                                 origin=LifeOrigin.WRITER, zone=server.app.time_zone,
                                                 now=datetime.now(UTC))
        return night.entry_id

    sleep_id = server.call(placed)
    kept = server.client.delete(f"/api/life/{sleep_id}?revision=1", headers=SEND)
    assert (kept.status_code, error_code(kept)) == (409, "plan_refused")
    edited = server.client.put(f"/api/life/{sleep_id}", headers=SEND, json={"text": "밤샘", "revision": 1})
    assert (edited.status_code, error_code(edited)) == (409, "plan_refused")  # review 13: only moved, as a slot
    over = server.post("/api/life/plans", {"day": tomorrow, "time": "01:00", "minutes": 60, "plan_kind": "own",
                                           "text": "야식", "persona_id": None})
    assert (over.status_code, error_code(over)) == (409, "plan_refused")
    assert "sleep slot" in over.json()["error"]["message"]
    entries = {e["id"]: e for e in server.client.get("/api/life").json()["entries"]}
    assert (entries[sleep_id]["plan_kind"], entries[sleep_id]["minutes"]) == ("sleep", 480)
    assert server.client.delete(f"/api/life/{slot['id']}?revision=2", headers=SEND).status_code == 204


def test_a_poke_counts_against_a_sleeping_himari_and_its_stir_opens_a_turn_on_that_device(server: Server) -> None:
    me = server.paired()
    cid = server.device.get("/api/device", headers=me).json()["recent_conversation"]["id"]
    assert server.device.post("/api/device/poke", headers=me).json() == {"answer": None}  # awake
    web_poke = server.client.post("/api/device/poke", headers=SEND)
    assert (web_poke.status_code, error_code(web_poke)) == (403, "not_a_device")
    server.asleep()
    stream = server.call(server.app.presence.subscribe, OPERATOR)
    assert server.device.post("/api/device/poke", headers=me).json() == {"answer": "none"}
    server.provider.extend([final("음…")])
    assert server.device.post("/api/device/poke", headers=me).json() == {"answer": "mumble"}
    server.wait(cid)
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    wakes = [i for i in items if i["kind"] == "wake"]
    assert [(w["wake"]["device_id"], [r["kind"] for r in w["wake"]["reasons"]]) for w in wakes] == [("d1", ["poke"])]
    assert server.provider.prepared_inputs[-1].request_options.half_asleep
    told = []
    while (event := server.call(stream.next, 0)) is not None:
        told.append((event.name, thaw(event.data)))
    assert ("himari_sleep", {"sleep": "half_asleep"}) in told
    (record,) = server.client.get("/api/life").json()["sleeps"]
    assert (record["messages"], record["mumbles"]) == (0, 1)  # pokes are not messages


def test_the_stream_tells_a_day_session_opened(server: Server) -> None:
    stream = server.call(server.app.presence.subscribe, OPERATOR)
    cid = server.day()
    told = []
    while (event := server.call(stream.next, 0)) is not None:
        told.append((event.name, thaw(event.data)))
    title = session_title(datetime.now(ZoneInfo(server.app.time_zone)).date())
    assert ("day_session", {"conversation": cid, "title": title, "closed": False}) in told
