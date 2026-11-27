"""HIMARI's life over the Web API (character-life v0.1 §6): the chosen persona's, seen, added to, edited and
retired from the revision shown; a device may not use it (D4)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")

from webkit import SEND, Server  # noqa: E402

from himari.common import ConversationId, PrincipalId  # noqa: E402
from himari.data.api import (  # noqa: E402
    MAX_ARC_CHARS,
    MAX_ARC_TITLE_CHARS,
    MAX_DIARY_CHARS,
    MAX_EVENT_CHARS,
    MAX_LIFE_CHARS,
    MAX_LIFE_FACTS,
    MAX_WORK_CHARS,
    MAX_WORK_SUMMARY_CHARS,
    NEW_ARC,
    ArcMove,
    ArcOp,
    FellBy,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeJob,
    RawReceived,
    WorldEvent,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    WokeBy,
    WorkPiece,
    life_day,
)
from himari.interfaces.web import app as web  # noqa: E402

ME = PrincipalId("operator")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        s.sign_in()
        yield s


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def test_the_chosen_personas_life_is_seen_added_to_edited_and_retired(server: Server) -> None:
    empty = server.client.get("/api/life").json()
    assert server.application is not None
    today = datetime.now(ZoneInfo(server.application.time_zone)).date()  # character-routine §9.2: the server's
    assert (empty.pop("today"), empty.pop("since")) == (today.isoformat(), (today - timedelta(days=30)).isoformat())
    now = empty.pop("now")  # character-day §4.5: never asleep by the clock; the day keeper's sleep (awake here)
    assert now is None
    # character-day §18.11: the sleep now (no slot placed yet: no look ran) and no sleep yet
    assert (empty.pop("sleep"), empty.pop("sleeps")) == ({"state": "awake"}, [])
    assert empty == {"persona": None, "enabled": True,
                     "writer": {"running": False, "told": "off (no HIMARI_MEMORY_MODEL)"}, "mood": None, "entries": [],
                     "world": {"weather": "off (no HIMARI_LOCATION)", "today": None,
                               "topics": "off (no story writer)", "next": None},  # character-mind §8: no world
                     "work": {"running": False, "next": None},  # character-work §8
                     "mind": None,  # character-mind §8: no life runs here
                     "limits": {"facts": MAX_LIFE_FACTS, "chars": MAX_LIFE_CHARS, "diary_chars": MAX_DIARY_CHARS,
                                "arc_chars": MAX_ARC_CHARS, "arc_title_chars": MAX_ARC_TITLE_CHARS,
                                "work_chars": MAX_WORK_CHARS, "summary_chars": MAX_WORK_SUMMARY_CHARS,
                                "event_chars": MAX_EVENT_CHARS}}
    added = server.post("/api/life", {"text": " 비 오는 날을 좋아한다. ", "persona_id": None})
    assert added.status_code == 201
    fact = added.json()
    assert (fact["kind"], fact["day"], fact["time"], fact["text"], fact["origin"], fact["revision"]) == (
        "fact", None, None, "비 오는 날을 좋아한다.", "user", 1)

    lucy = server.post("/api/personas", {"name": "Lucy", "text": "이름: 루시."}).json()
    assert server.client.put("/api/personas/choice", json={"persona_id": lucy["id"], "revision": 0},
                             headers=SEND).status_code == 200
    stale = server.post("/api/life", {"text": "the page showed no persona", "persona_id": None})
    assert (stale.status_code, error_code(stale)) == (409, "persona_changed")
    seen = server.client.get("/api/life").json()
    assert seen["persona"] == {"id": lucy["id"], "name": "Lucy"} and seen["entries"] == []  # its own life

    # the story writer's day for Lucy, as Data keeps it
    async def written() -> None:
        assert server.application is not None
        data = server.application.data
        write_id = await data.begin_life_write(ME, LifeWriteStart(lucy["id"], "2099-01-01", "p", "m", b"{}", "j", 1))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        await data.apply_life_write(write_id, LifePlan("먼 미래의 일기.", (("2099-01-02", "산책."),)),
                                    now=datetime.now(UTC))

    server.client.portal.call(written)  # type: ignore[union-attr]
    entries = server.client.get("/api/life").json()["entries"]
    assert [(e["kind"], e["day"], e["origin"]) for e in entries] == [
        ("plan", "2099-01-02", "writer"), ("diary", "2099-01-01", "writer")]
    diary = entries[1]
    edited = server.client.put(f"/api/life/{diary['id']}", json={"text": "내가 고친 일기.", "revision": 1},
                               headers=SEND)
    assert edited.status_code == 200 and (edited.json()["origin"], edited.json()["revision"]) == ("user", 2)
    old = server.client.put(f"/api/life/{diary['id']}", json={"text": "x", "revision": 1}, headers=SEND)
    assert (old.status_code, error_code(old)) == (409, "revision_conflict")
    too_long = server.client.put(f"/api/life/{diary['id']}", json={"text": "x" * (MAX_DIARY_CHARS + 1),
                                                                     "revision": 2}, headers=SEND)
    assert (too_long.status_code, error_code(too_long)) == (422, "too_large")
    blank = server.post("/api/life", {"text": " ", "persona_id": lucy["id"]})
    assert (blank.status_code, error_code(blank)) == (422, "invalid_life")

    gone = server.client.delete(f"/api/life/{diary['id']}", params={"revision": 2}, headers=SEND)
    assert gone.status_code == 204
    again = server.client.delete(f"/api/life/{diary['id']}", params={"revision": 3}, headers=SEND)
    assert (again.status_code, error_code(again)) == (404, "not_found")
    assert [e["kind"] for e in server.client.get("/api/life").json()["entries"]] == ["plan"]
    # no persona again: its life is still there
    server.client.put("/api/personas/choice", json={"persona_id": None, "revision": 1}, headers=SEND)
    assert [e["text"] for e in server.client.get("/api/life").json()["entries"]] == ["비 오는 날을 좋아한다."]


def test_an_arc_is_seen_with_its_title_and_status_and_its_title_and_goal_are_edited(server: Server) -> None:
    """character-world §4: an arc's title, status, start, end and ending; a diary names the arc it moved."""
    async def written() -> None:
        assert server.application is not None
        data = server.application.data
        for day, move in (("2099-01-01", ArcMove(ArcOp.START, title="그림 연습", goal="풍경화 한 장")),
                          ("2099-01-03", ArcMove(ArcOp.CLOSE, entry_id=1, ending="완성했다."))):
            write_id = await data.begin_life_write(ME, LifeWriteStart(0, day, "p", "m", b"{}", "j", 1))
            await data.mark_life_write_dispatched(write_id)
            await data.record_life_write(write_id, RawReceived(b"{}", "j"))
            await data.apply_life_write(write_id, LifePlan(f"{day}의 일기.", arc=move), now=datetime.now(UTC))

    server.client.portal.call(written)  # type: ignore[union-attr]
    arc, last, first = server.client.get("/api/life").json()["entries"]
    assert (arc["kind"], arc["title"], arc["text"], arc["status"], arc["day"], arc["end_day"], arc["ending"]) == (
        "arc", "그림 연습", "풍경화 한 장", "closed", "2099-01-01", "2099-01-03", "완성했다.")
    assert (last["kind"], last["arc_id"], first["arc_id"]) == ("diary", arc["id"], arc["id"])
    edited = server.client.put(f"/api/life/{arc['id']}", json={"text": "인물화 한 장", "title": "수채화 연습",
                                                               "revision": arc["revision"]}, headers=SEND)
    assert edited.status_code == 200
    assert (edited.json()["title"], edited.json()["text"], edited.json()["origin"]) == ("수채화 연습", "인물화 한 장",
                                                                                         "user")
    titled = server.client.put(f"/api/life/{first['id']}", json={"text": "일기", "title": "제목",
                                                                  "revision": first["revision"]}, headers=SEND)
    assert (titled.status_code, error_code(titled)) == (422, "invalid_life")  # only an arc has a title
    long = server.client.put(f"/api/life/{arc['id']}", json={"text": "g", "title": "x" * (MAX_ARC_TITLE_CHARS + 1),
                                                             "revision": edited.json()["revision"]}, headers=SEND)
    assert (long.status_code, error_code(long)) == (422, "too_large")


def test_the_user_ends_an_open_project_from_the_revision_shown(server: Server) -> None:
    """character-routine §11.4: ended with its one-line ending (kept as the user's, the day it started too); a
    change since, a closed or unknown project, an ending too long or blank are refused."""
    async def written() -> None:
        assert server.application is not None
        data = server.application.data
        for day, title in (("2099-01-01", "그림 연습"), ("2099-01-02", "우쿨렐레")):
            write_id = await data.begin_life_write(ME, LifeWriteStart(0, day, "p", "m", b"{}", "j", 1))
            await data.mark_life_write_dispatched(write_id)
            await data.record_life_write(write_id, RawReceived(b"{}", "j"))
            await data.apply_life_write(write_id, LifePlan(f"{day}의 일기.",
                                                           arc=ArcMove(ArcOp.START, title=title, goal="천천히")),
                                        now=datetime.now(UTC))

    server.client.portal.call(written)  # type: ignore[union-attr]
    arcs = {e["title"]: e for e in server.client.get("/api/life").json()["entries"] if e["kind"] == "arc"}
    arc = arcs["그림 연습"]
    stale = server.client.post(f"/api/life/arcs/{arc['id']}/end", json={"ending": "끝.", "revision": 2},
                               headers=SEND)
    assert (stale.status_code, error_code(stale)) == (409, "revision_conflict")
    long = server.client.post(f"/api/life/arcs/{arc['id']}/end",
                              json={"ending": "x" * (MAX_ARC_CHARS + 1), "revision": 1}, headers=SEND)
    assert (long.status_code, error_code(long)) == (422, "too_large")
    ended = server.client.post(f"/api/life/arcs/{arc['id']}/end", json={"ending": " 다 그렸다. ", "revision": 1},
                               headers=SEND)
    assert ended.status_code == 200
    body = ended.json()
    assert server.application is not None
    assert (body["status"], body["ending"], body["origin"], body["revision"], body["end_day"]) == (
        "closed", "다 그렸다.", "user", 2, life_day(datetime.now(UTC), ZoneInfo(server.application.time_zone)))
    again = server.client.post(f"/api/life/arcs/{arc['id']}/end", json={"ending": "또.", "revision": 2},
                               headers=SEND)
    assert again.status_code == 404
    unknown = server.client.post("/api/life/arcs/999/end", json={"ending": "끝.", "revision": 1}, headers=SEND)
    assert unknown.status_code == 404
    other = arcs["우쿨렐레"]
    blank = server.client.post(f"/api/life/arcs/{other['id']}/end", json={"ending": "  ", "revision": 1},
                               headers=SEND)
    assert blank.status_code == 422
    unsigned = server.client.post(f"/api/life/arcs/{other['id']}/end", json={"ending": "끝.", "revision": 1})
    assert unsigned.status_code in (401, 403)  # no allowed origin header


def test_a_project_shows_its_so_far_its_pieces_and_the_next_work_plan(server: Server) -> None:
    """character-work §8: a work plan names its project and is the next one until its piece is made; the
    piece is seen with its title, summary, project and plan, and its text, title and summary are edited."""
    async def planned() -> None:
        assert server.application is not None
        data = server.application.data
        write_id = await data.begin_life_write(ME, LifeWriteStart(0, "2099-01-01", "p", "m", b"{}", "j", 1))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        await data.apply_life_write(write_id, LifePlan(
            "전자포를 구상했다.", (("2099-01-02", "내일 밤엔 냉각부 도면 그려야지."),), times=("21:00",),
            arc=ArcMove(ArcOp.START, title="블랙홀 전자포", goal="설정집 한 권"), plan_arcs=(NEW_ARC,)),
            now=datetime.now(UTC))

    async def worked(plan_id: int) -> None:
        assert server.application is not None
        data = server.application.data
        write_id = await data.begin_life_write(ME, LifeWriteStart(0, "2099-01-02", "p", "m", b"{}", "j", 1,
                                                                  plan_id=plan_id))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        await data.apply_work_write(write_id, WorkPiece("냉각부 설계. " * 40, "냉각부", "냉각부 끝냈어.",
                                                        "포신과 냉각부까지 정했다."), now=datetime.now(UTC))

    server.client.portal.call(planned)  # type: ignore[union-attr]
    seen = server.client.get("/api/life").json()
    arc, plan, _diary = seen["entries"]
    assert (arc["kind"], arc["so_far"], plan["kind"], plan["arc_id"]) == ("arc", None, "plan", arc["id"])
    assert seen["work"]["next"]["id"] == plan["id"]
    server.client.portal.call(worked, plan["id"])  # type: ignore[union-attr]
    seen = server.client.get("/api/life").json()
    assert seen["work"]["next"] is None  # made
    arc, piece, plan, _diary = seen["entries"]
    assert arc["so_far"] == "포신과 냉각부까지 정했다." and arc["status"] == "open"
    assert (piece["kind"], piece["day"], piece["time"], piece["title"], piece["summary"], piece["arc_id"],
            piece["plan_id"]) == ("work", "2099-01-02", "21:00", "냉각부", "냉각부 끝냈어.", arc["id"], plan["id"])
    edited = server.client.put(f"/api/life/{piece['id']}", json={"text": "고친 조각.", "title": "냉각부 (고침)",
                                                                 "summary": "고쳤어.", "revision": 1}, headers=SEND)
    assert edited.status_code == 200
    assert (edited.json()["text"], edited.json()["title"], edited.json()["summary"], edited.json()["origin"]) == (
        "고친 조각.", "냉각부 (고침)", "고쳤어.", "user")
    summed = server.client.put(f"/api/life/{plan['id']}", json={"text": "일정", "summary": "요약",
                                                                "revision": plan["revision"]}, headers=SEND)
    assert (summed.status_code, error_code(summed)) == (422, "invalid_life")  # only a piece has a summary
    gone = server.client.delete(f"/api/life/{piece['id']}", params={"revision": 2}, headers=SEND)
    assert gone.status_code == 204


def test_a_kept_thought_is_seen_with_what_it_is_about_and_only_deleted(server: Server) -> None:
    """character-mind §8: the kept thoughts come last, with what each is about and when it expires; a save is
    refused (only deleted)."""
    async def kept() -> None:
        assert server.application is not None
        await server.application.data.apply_thought_changes(ME, 0, [ThoughtChange(
            ThoughtOp.KEEP, "요즘 지쳐 보인다.", ThoughtAbout.USER, sequences=(2,))],
            conversation=ConversationId("conv_x"), source="mpass_1", now=datetime.now(UTC))

    server.client.portal.call(kept)  # type: ignore[union-attr]
    (thought,) = server.client.get("/api/life").json()["entries"]
    assert (thought["kind"], thought["text"], thought["about"], thought["origin"], thought["day"]) == (
        "thought", "요즘 지쳐 보인다.", "user", "conversation", None)
    assert thought["expires_at"] > thought["created_at"]
    edited = server.client.put(f"/api/life/{thought['id']}", json={"text": "고쳐 봄", "revision": 1}, headers=SEND)
    assert (edited.status_code, error_code(edited)) == (422, "invalid_life")
    gone = server.client.delete(f"/api/life/{thought['id']}", params={"revision": 1}, headers=SEND)
    assert gone.status_code == 204
    assert server.client.get("/api/life").json()["entries"] == []


def test_life_needs_a_signed_in_user_and_an_allowed_origin(server: Server) -> None:
    assert server.client.post("/api/life", json={"text": "x", "persona_id": None}).status_code == 403  # no origin
    server.client.delete("/api/session", headers=SEND)
    assert server.client.get("/api/life").status_code == 401


def test_an_event_is_seen_with_its_day_and_time_and_is_edited_and_deleted(server: Server) -> None:
    """character-mind §8: what the world made happen to HIMARI, in the Events group."""
    async def happened() -> None:
        assert server.application is not None
        data = server.application.data
        write_id = await data.begin_life_write(ME, LifeWriteStart(0, "2099-01-01", "p", "m", b"{}", "j", 1,
                                                                  job=LifeJob.WORLD))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        await data.add_events(write_id, [WorldEvent("15:00", "택배가 늦게 왔다.")], now=datetime.now(UTC))

    server.client.portal.call(happened)  # type: ignore[union-attr]
    (event,) = server.client.get("/api/life").json()["entries"]
    assert (event["kind"], event["day"], event["time"], event["text"], event["origin"]) == (
        "event", "2099-01-01", "15:00", "택배가 늦게 왔다.", "writer")
    edited = server.client.put(f"/api/life/{event['id']}", json={"text": "택배가 일찍 왔다.", "revision": 1},
                               headers=SEND)
    assert (edited.status_code, edited.json()["text"], edited.json()["origin"]) == (200, "택배가 일찍 왔다.", "user")
    long = server.client.put(f"/api/life/{event['id']}", json={"text": "x" * (MAX_EVENT_CHARS + 1), "revision": 2},
                             headers=SEND)
    assert (long.status_code, error_code(long)) == (422, "too_large")
    assert server.client.delete(f"/api/life/{event['id']}", params={"revision": 2}, headers=SEND).status_code == 204


def test_the_life_sends_the_last_30_days_and_its_history_reads_on_from_there(server: Server) -> None:
    """character-routine §9.1: /api/life sends the events (as the plans and diaries) of the last 30 days and later;
    /api/life/history reads the dates before, by default from where /api/life stops, with the cancelled slots and
    the sleeps that fell then, and says whether anything older is kept."""
    assert server.application is not None
    zone = ZoneInfo(server.application.time_zone)
    today = datetime.now(zone).date()
    day = {n: (today - timedelta(days=n)).isoformat() for n in (0, 29, 30, 31, 45, 59, 60, 61, 90, 200, 229)}

    async def lived() -> None:
        assert server.application is not None
        data = server.application.data
        for n in (0, 30, 31, 45, 90, 200):
            write_id = await data.begin_life_write(ME, LifeWriteStart(0, day[n], "p", "m", b"{}", "j", 1,
                                                                      job=LifeJob.WORLD))
            await data.mark_life_write_dispatched(write_id)
            await data.record_life_write(write_id, RawReceived(b"{}", "j"))
            await data.add_events(write_id, [WorldEvent("15:00", f"{n}일 전의 일.")], now=datetime.now(UTC))
        write_id = await data.begin_life_write(ME, LifeWriteStart(0, day[61], "p", "m", b"{}", "j", 1))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j"))
        (_, plan) = await data.apply_life_write(write_id, LifePlan("61일 전의 일기.", ((day[60], "산책."),),
                                                                   times=("15:00",)), now=datetime.now(UTC))
        await data.cancel_plan(ME, plan.entry_id, "비가 와서", origin=LifeOrigin.USER, zone=str(zone),
                               now=datetime.combine(today - timedelta(days=61), time(9), zone))
        for n in (29, 59):  # a sleep that fell at 23:00 n days ago
            at = datetime.combine(today - timedelta(days=n), time(23), zone)
            sleep = await data.open_sleep(ME, 0, at=at, how=FellBy.SCHEDULE)
            await data.end_sleep(ME, sleep.sleep_id, at=at + timedelta(hours=8), how=WokeBy.SCHEDULE)

    server.client.portal.call(lived)  # type: ignore[union-attr]
    life = server.client.get("/api/life").json()
    assert (life["today"], life["since"]) == (day[0], day[30])
    assert [(e["kind"], e["day"]) for e in life["entries"]] == [("event", day[0]), ("event", day[30])]
    assert [s["fell_at"][:10] for s in life["sleeps"]] == [
        datetime.combine(today - timedelta(days=29), time(23), zone).astimezone(UTC).date().isoformat()]
    assert life["sleeps"][0]["day"] == day[29]  # the date it fell asleep in the user's zone, as the entries'

    first = server.client.get("/api/life/history").json()  # the 30 dates before /api/life's first
    assert (first["from"], first["before"], first["more"]) == (day[60], day[30], True)
    assert [(e["kind"], e["day"]) for e in first["entries"]] == [("event", day[31]), ("event", day[45]),
                                                                  ("plan", day[60])]
    plan = first["entries"][2]
    assert (plan["plan_state"], plan["reason"], plan["time"]) == ("cancelled", "비가 와서", "15:00")
    assert len(first["sleeps"]) == 1 and set(first["sleeps"][0]) == set(life["sleeps"][0])
    assert first["sleeps"][0]["how_woke"] == "schedule"

    second = server.client.get("/api/life/history", params={"before": first["from"], "days": 30}).json()
    assert (second["from"], second["before"], second["more"]) == (day[90], day[60], True)
    assert [(e["kind"], e["day"]) for e in second["entries"]] == [("diary", day[61]), ("event", day[90])]
    assert second["sleeps"] == []
    # review 2: nothing from 91 to 199 days ago — the next read reaches back to the 30 dates ending at the one that
    # has something, never a page of nothing
    third = server.client.get("/api/life/history", params={"before": second["from"]}).json()
    assert (third["from"], third["before"], third["more"]) == (day[229], day[90], False)
    assert [(e["kind"], e["day"]) for e in third["entries"]] == [("event", day[200])]
    one = server.client.get("/api/life/history", params={"before": day[30], "days": 1}).json()
    assert (one["from"], [e["day"] for e in one["entries"]], one["more"]) == (day[31], [day[31]], True)
    for bad in ({"days": 0}, {"days": 61}, {"days": "abc"}, {"days": "1.5"}, {"days": ""}, {"before": "2026-13-01"},
                {"before": "2026-1-01"}, {"before": "0001-01-01"}):
        refused = server.client.get("/api/life/history", params=bad)
        assert (refused.status_code, error_code(refused)) == (400, "invalid_argument"), bad
    server.client.delete("/api/session", headers=SEND)
    assert server.client.get("/api/life/history").status_code == 401
