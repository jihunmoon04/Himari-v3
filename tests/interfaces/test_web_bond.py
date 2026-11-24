"""The bond over the Web API (character-bond v0.1 §1): the chosen persona's entries, stage and memories' timeline,
seen, added to, edited, retired and set from the revision shown; a device may not use it (D4)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from webkit import SEND, Server  # noqa: E402

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope  # noqa: E402
from himari.data.api import (MAX_BOND_CHARS, MAX_BOND_ENTRIES, EpisodePlan, PassKind, PassStart,  # noqa: E402
                             UserMessage, WakeKind, WakeReason)
from himari.interfaces.web import app as web  # noqa: E402

ME = PrincipalId("operator")


def call(server: Server, fn: Any, *args: Any, **kwargs: Any) -> Any:
    assert server.client.portal is not None
    return server.client.portal.call(lambda: fn(*args, **kwargs))


def remembered(server: Server, said: str, episode: EpisodePlan) -> None:
    """A conversation with a user message and an applied live pass over it with an episode, straight through
    Data (as test_web_memory does)."""
    app = server.application
    assert app is not None
    scope = TrustedScope(ME, ConversationId(server.new()))
    call(server, app.data.append_event, scope, UserMessage((TextBlock(said),)))
    last = call(server, app.data.last_event, scope)[0]
    cursor = call(server, app.data.read_memory_cursor, scope).through_sequence
    pass_id = call(server, app.data.begin_pass, scope, PassStart(PassKind.LIVE, cursor, last, last, "p", "m"))
    call(server, app.data.apply_pass, pass_id, [], episode, now=datetime.now(UTC))


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        s.sign_in()
        yield s


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def test_the_chosen_personas_bond_is_seen_added_to_edited_set_and_retired(server: Server) -> None:
    empty = server.client.get("/api/bond").json()
    assert empty == {"persona": None, "enabled": True,
                     "stage": {"stage": "new", "since": None, "reason": None, "revision": 0},
                     "together_days": 0, "since": None, "today": [], "entries": [], "memories": [],
                     "limits": {"entries": MAX_BOND_ENTRIES, "chars": MAX_BOND_CHARS}}
    added = server.post("/api/bond", {"tag": "promise", "text": " 주말마다 영화 얘기 하기. ", "persona_id": None})
    assert added.status_code == 201
    promise = added.json()
    assert (promise["tag"], promise["text"], promise["month_day"], promise["origin"], promise["revision"]) == (
        "promise", "주말마다 영화 얘기 하기.", None, "user", 1)
    birthday = server.post("/api/bond", {"tag": "date", "text": "사용자 생일", "month_day": "03-02",
                                         "persona_id": None}).json()
    no_day = server.post("/api/bond", {"tag": "date", "text": "언젠가", "persona_id": None})
    assert (no_day.status_code, error_code(no_day)) == (422, "invalid_bond")
    too_long = server.post("/api/bond", {"tag": "joke", "text": "x" * (MAX_BOND_CHARS + 1), "persona_id": None})
    assert (too_long.status_code, error_code(too_long)) == (422, "too_large")
    odd_tag = server.post("/api/bond", {"tag": "nickname", "text": "x", "persona_id": None})
    assert odd_tag.status_code == 400  # not a tag: the body's own check

    seen = server.client.get("/api/bond").json()
    assert [e["id"] for e in seen["entries"]] == [promise["id"], birthday["id"]]  # promises first
    assert [(m["kind"], m["id"]) for m in seen["memories"]] == [("bond", birthday["id"]), ("bond", promise["id"])]
    assert server.client.get("/api/life").json()["entries"] == []  # the bond is not the life

    edited = server.client.put(f"/api/bond/{promise['id']}", json={"tag": "name", "text": "대장", "revision": 1},
                               headers=SEND)
    assert edited.status_code == 200 and (edited.json()["tag"], edited.json()["revision"]) == ("name", 2)
    stale = server.client.put(f"/api/bond/{promise['id']}", json={"tag": "name", "text": "x", "revision": 1},
                              headers=SEND)
    assert (stale.status_code, error_code(stale)) == (409, "revision_conflict")

    # the name moved the stage when the bond was read; the owner takes it back
    stage = server.client.get("/api/bond").json()["stage"]
    assert (stage["stage"], stage["reason"]) == ("familiar", f"b{promise['id']}")
    chosen = server.client.put("/api/bond/stage", json={"stage": "new", "persona_id": None,
                                                        "revision": stage["revision"]}, headers=SEND)
    assert chosen.status_code == 200 and (chosen.json()["stage"], chosen.json()["reason"]) == ("new", "user")
    again = server.client.put("/api/bond/stage", json={"stage": "close", "persona_id": None,
                                                       "revision": stage["revision"]}, headers=SEND)
    assert (again.status_code, error_code(again)) == (409, "revision_conflict")
    assert server.client.get("/api/bond").json()["stage"]["stage"] == "new"  # the old name does not move it

    gone = server.client.delete(f"/api/bond/{birthday['id']}", params={"revision": 1}, headers=SEND)
    assert gone.status_code == 204
    twice = server.client.delete(f"/api/bond/{birthday['id']}", params={"revision": 2}, headers=SEND)
    assert (twice.status_code, error_code(twice)) == (404, "not_found")

    lucy = server.post("/api/personas", {"name": "Lucy", "text": "이름: 루시."}).json()
    server.client.put("/api/personas/choice", json={"persona_id": lucy["id"], "revision": 0}, headers=SEND)
    stale_persona = server.post("/api/bond", {"tag": "joke", "text": "x", "persona_id": None})
    assert (stale_persona.status_code, error_code(stale_persona)) == (409, "persona_changed")
    stale_stage = server.client.put("/api/bond/stage", json={"stage": "close", "persona_id": None, "revision": 0},
                                    headers=SEND)
    assert (stale_stage.status_code, error_code(stale_stage)) == (409, "persona_changed")
    lucys = server.client.get("/api/bond").json()
    assert lucys["persona"] == {"id": lucy["id"], "name": "Lucy"} and lucys["entries"] == []  # its own bond


def test_the_memories_timeline_holds_the_episodes_too(server: Server) -> None:
    now = datetime.now(UTC) - timedelta(minutes=1)
    remembered(server, "발표 자료 같이 보자", EpisodePlan("같이 발표 자료를 다듬었다.", now, now, (1,), ()))
    server.post("/api/bond", {"tag": "first", "text": "처음으로 같이 밤샘", "persona_id": None})
    memories = server.client.get("/api/bond").json()["memories"]
    assert [m["kind"] for m in memories] == ["bond", "episode"]  # newest first
    assert memories[1]["text"] == "같이 발표 자료를 다듬었다."


def test_an_anniversary_wake_says_why_on_the_page() -> None:
    reasons = (WakeReason(WakeKind.ANNIVERSARY, days=100), WakeReason(WakeKind.ANNIVERSARY, years=1),
               WakeReason(WakeKind.ANNIVERSARY, entry_id=7))
    assert web.wake_summary(reasons) == (
        "HIMARI reached out · 100 days together · 1 year together · a day to mark (b7)")
    assert web.wake_reason_view(reasons[2]) == {"kind": "anniversary", "entry_id": 7}


def test_the_bond_needs_a_signed_in_user_and_an_allowed_origin(server: Server) -> None:
    assert server.client.post("/api/bond", json={"tag": "joke", "text": "x", "persona_id": None}).status_code == 403
    server.client.delete("/api/session", headers=SEND)
    assert server.client.get("/api/bond").status_code == 401
