"""HIMARI's routines over the Web API (character-routine v0.1 §13.4): the chosen persona's, read (the built-ins made on
first read), added, edited and deleted from the revision shown; the rules' refusals said; a slot placed for one shows
it in the life."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from webkit import SEND, Server  # noqa: E402

from himari.interfaces.web import app as web  # noqa: E402


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        s.sign_in()
        yield s


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def dinner(today: str, **more: object) -> dict[str, object]:
    return {"name": "저녁 식사", "text": "저녁 먹는 중", "start_from": "18:30", "start_to": "19:30",
            "minutes_min": 40, "minutes_max": 40, "weekdays": 31, "state": "trial", "trial_until": today,
            "himari": "move_cancel", **more}


def test_the_routines_are_read_added_edited_and_deleted(server: Server) -> None:
    read = server.client.get("/api/routines")
    assert read.status_code == 200
    body = read.json()
    today = body["today"]
    assert [(r["name"], r["builtin"], r["state"]) for r in body["routines"]] == [("잠", "sleep", "fixed"),
                                                                                ("일기", "diary", "fixed")]
    assert body["limits"]["routines"] == 20 and body["persona"] is None
    made = server.client.post("/api/routines", json={**dinner(today), "persona_id": None}, headers=SEND)
    assert made.status_code == 201
    routine = made.json()
    assert (routine["name"], routine["weekdays"], routine["trial_until"], routine["trial_ended"],
            routine["himari"]) == ("저녁 식사", 31, today, False, "move_cancel")
    wrong = server.client.post("/api/routines", json={**dinner(today, start_from="20:00"), "persona_id": None},
                               headers=SEND)
    assert (wrong.status_code, error_code(wrong)) == (409, "routine_refused")
    assert "earliest first" in wrong.json()["error"]["message"]
    other = server.client.post("/api/routines", json={**dinner(today), "persona_id": 5}, headers=SEND)
    assert (other.status_code, error_code(other)) == (409, "persona_changed")
    edited = server.client.put(f"/api/routines/{routine['id']}", json={**dinner(today, state="fixed"),
                                                                        "revision": 1}, headers=SEND)
    assert edited.status_code == 200 and edited.json()["state"] == "fixed" and edited.json()["trial_until"] is None
    stale = server.client.put(f"/api/routines/{routine['id']}", json={**dinner(today), "revision": 1}, headers=SEND)
    assert (stale.status_code, error_code(stale)) == (409, "revision_conflict")
    sleep = body["routines"][0]
    builtin = server.client.delete(f"/api/routines/{sleep['id']}?revision=1", headers=SEND)
    assert (builtin.status_code, error_code(builtin)) == (409, "routine_refused")
    gone = server.client.delete(f"/api/routines/{routine['id']}?revision=2", headers=SEND)
    assert gone.status_code == 204
    assert [r["name"] for r in server.client.get("/api/routines").json()["routines"]] == ["잠", "일기"]
    missing = server.client.put("/api/routines/999", json={**dinner(today), "revision": 1}, headers=SEND)
    assert missing.status_code == 404
    unsent = server.client.post("/api/routines", json={**dinner(today), "persona_id": None})
    assert unsent.status_code in (401, 403)  # no allowed origin header
