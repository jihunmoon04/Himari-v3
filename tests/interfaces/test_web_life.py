"""HIMARI's life over the Web API (character-life v0.1 §6): the chosen persona's, seen, added to, edited and
retired from the revision shown; a device may not use it (D4)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from webkit import SEND, Server  # noqa: E402

from himari.common import ConversationId, PrincipalId  # noqa: E402
from himari.data.api import (  # noqa: E402
    MAX_ARC_CHARS,
    MAX_ARC_TITLE_CHARS,
    MAX_DIARY_CHARS,
    MAX_LIFE_CHARS,
    MAX_LIFE_FACTS,
    MAX_WORK_CHARS,
    MAX_WORK_SUMMARY_CHARS,
    NEW_ARC,
    ArcMove,
    ArcOp,
    LifePlan,
    LifeWriteStart,
    RawReceived,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    WorkPiece,
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
    now = empty.pop("now")  # character-state §3: asleep in the default quiet hours (23:00-08:00), else nothing
    assert now in (None, {"asleep": True, "doing": None, "since": None})
    assert empty == {"persona": None, "enabled": True,
                     "writer": {"running": False, "told": "off (no HIMARI_MEMORY_MODEL)"}, "mood": None, "entries": [],
                     "world": {"weather": "off (no HIMARI_LOCATION)", "today": None,
                               "topics": "off (no story writer)"},
                     "work": {"running": False, "next": None},  # character-work §8
                     "mind": None,  # character-mind §8: no life runs here
                     "limits": {"facts": MAX_LIFE_FACTS, "chars": MAX_LIFE_CHARS, "diary_chars": MAX_DIARY_CHARS,
                                "arc_chars": MAX_ARC_CHARS, "arc_title_chars": MAX_ARC_TITLE_CHARS,
                                "work_chars": MAX_WORK_CHARS, "summary_chars": MAX_WORK_SUMMARY_CHARS}}
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
