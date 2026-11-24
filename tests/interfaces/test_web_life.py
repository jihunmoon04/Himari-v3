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

from himari.common import PrincipalId  # noqa: E402
from himari.data.api import MAX_DIARY_CHARS, MAX_LIFE_CHARS, MAX_LIFE_FACTS, LifePlan, LifeWriteStart, RawReceived  # noqa: E402
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
                     "limits": {"facts": MAX_LIFE_FACTS, "chars": MAX_LIFE_CHARS, "diary_chars": MAX_DIARY_CHARS}}
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


def test_life_needs_a_signed_in_user_and_an_allowed_origin(server: Server) -> None:
    assert server.client.post("/api/life", json={"text": "x", "persona_id": None}).status_code == 403  # no origin
    server.client.delete("/api/session", headers=SEND)
    assert server.client.get("/api/life").status_code == 401
