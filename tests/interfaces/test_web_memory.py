"""The notes over the Web API (V3-P71 §7.2)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from testkit import final  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.bootstrap.operator import derive_cli_bearer  # noqa: E402
from himari.common import CallId, PrincipalId, ToolCall, ToolId  # noqa: E402
from himari.data.api import MAX_MEMORIES, MAX_MEMORY_CHARS  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ORIGIN = "http://testserver"
SEND = {"origin": ORIGIN}


class Server:
    def __init__(self, tmp_path: Path, memory: bool = True) -> None:
        self.provider = ScriptedAdapter([])
        self.application: Application | None = None

        async def opener() -> Application:
            self.application = await Application.open(tmp_path / "data", self.provider, model_id="scripted-model",
                                                      sandbox=False, cleanup_seconds=None, memory=memory)
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

    def add(self, category: str, text: str) -> Any:
        return self.client.post("/api/memories", json={"category": category, "text": text}, headers=SEND)

    def listing(self) -> dict[str, Any]:
        return self.client.get("/api/memories").json()  # type: ignore[no-any-return]

    def wait(self, cid: str) -> None:
        for _ in range(200):
            if not self.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("still running")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path)
    with s.client:
        yield s


def test_add_edit_delete_and_list(server: Server) -> None:
    server.sign_in()
    listing = server.listing()
    assert {k: v for k, v in listing.items() if k != "auto"} == {
        "memories": [], "max_count": MAX_MEMORIES, "max_chars": MAX_MEMORY_CHARS, "max_loop_chars": 300,
        "enabled": True}
    assert listing["auto"]["running"] is False and listing["auto"]["told"] == "off (no HIMARI_MEMORY_MODEL)"
    added = server.add("preference", "  Short lists.\r\n")
    assert added.status_code == 201
    body = added.json()
    assert {k: body[k] for k in ("id", "category", "text", "origin", "source", "revision")} == {
        "id": 1, "category": "preference", "text": "Short lists.", "origin": "user", "source": None, "revision": 1}
    server.add("user", "Writes Go.")
    assert [m["id"] for m in server.listing()["memories"]] == [2, 1]  # user before preference

    edited = server.client.put("/api/memories/1", json={"category": "project", "text": "Builds HIMARI.",
                                                        "revision": 1}, headers=SEND)
    assert edited.status_code == 200 and (edited.json()["category"], edited.json()["revision"]) == ("project", 2)
    stale = server.client.put("/api/memories/1", json={"category": "project", "text": "lost", "revision": 1},
                              headers=SEND)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
    assert server.client.delete("/api/memories/1?revision=1", headers=SEND).status_code == 409
    assert server.client.delete("/api/memories/1?revision=2", headers=SEND).status_code == 204
    gone = server.client.delete("/api/memories/1", headers=SEND)
    assert gone.status_code == 404 and gone.json()["error"] == {"code": "not_found", "message": "No such note."}
    assert [m["text"] for m in server.listing()["memories"]] == ["Writes Go."]


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"category": "user", "text": "x" * (MAX_MEMORY_CHARS + 1)}, 422, "too_large"),
    ({"category": "user", "text": "  "}, 422, "invalid_memory"),
    ({"category": "user", "text": "esc \u001b[2J"}, 422, "invalid_memory"),
])
def test_refused_writes_say_why_and_never_echo(server: Server, body: dict[str, Any], status: int, code: str) -> None:
    server.sign_in()
    refused = server.client.post("/api/memories", json=body, headers=SEND)
    assert refused.status_code == status and refused.json()["error"]["code"] == code
    assert "\u001b" not in refused.text and "xxxx" not in refused.text
    assert server.listing()["memories"] == []


@pytest.mark.parametrize("body", [{"category": "mood", "text": "x"}, {"text": "x"}, {"category": "user"},
                                  {"category": "user", "text": 3}, {"category": "user", "text": "x", "id": 4}])
def test_malformed_bodies_are_refused(server: Server, body: dict[str, Any]) -> None:
    server.sign_in()
    assert server.client.post("/api/memories", json=body, headers=SEND).status_code in (400, 422)
    assert server.client.put("/api/memories/1", json={**body, "revision": 1}, headers=SEND).status_code in (400, 404, 422)
    assert server.listing()["memories"] == []


def test_a_full_store_says_so(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    server.sign_in()
    monkeypatch.setattr("himari.data.sqlite.MAX_MEMORIES", 1)
    assert server.add("user", "one").status_code == 201
    refused = server.add("user", "two")
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "memory_full"


def test_a_note_the_model_saved_names_its_conversation_and_the_delete_notice_counts_it(server: Server) -> None:
    server.sign_in()
    cid = server.client.post("/api/conversations", json={"title": "Trip"}, headers=SEND).json()["id"]
    server.provider.extend([NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                                          {"category": "project", "text": "Plans a trip."}),)),
                            final("Noted.")])
    assert server.client.post(f"/api/conversations/{cid}/messages", json={"text": "remember"},
                              headers=SEND).status_code == 202
    server.wait(cid)
    (note,) = server.listing()["memories"]
    assert (note["origin"], note["source"]) == ("model", {"conversation": cid, "title": "Trip", "preview": "remember",
                                                       "in_trash": False})
    assert server.client.get(f"/api/conversations/{cid}/delete-notice").json()["memory_count"] == 1
    assert server.client.post(f"/api/conversations/{cid}/trash", headers=SEND).status_code == 200
    (note,) = server.listing()["memories"]
    assert note["source"] == {"conversation": cid, "title": None, "preview": "", "in_trash": True}
    assert server.client.delete(f"/api/conversations/{cid}", headers=SEND).status_code == 204
    assert server.listing()["memories"] == []


def test_sign_in_origin_and_the_cli_bearer(server: Server) -> None:
    assert server.client.get("/api/memories").status_code == 401
    assert server.add("user", "x").status_code == 401
    server.sign_in()
    assert server.client.post("/api/memories", json={"category": "user", "text": "x"}).status_code == 403
    assert server.client.post("/api/memories", json={"category": "user", "text": "x"},
                              headers={"origin": "http://evil.example"}).status_code == 403
    assert server.application is not None
    bearer = derive_cli_bearer(server.application.operator_secret)
    server.client.cookies.clear()
    saved = server.client.post("/api/memories", json={"category": "user", "text": "from cli"},
                               headers={"authorization": f"Bearer {bearer}"})
    assert saved.status_code == 201


def test_memory_off_still_manages_the_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # V3-P71 §9 (rev. 4): off stops only the model's use of the notes.
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    server = Server(tmp_path, memory=False)
    with server.client:
        server.sign_in()
        assert server.listing()["enabled"] is False
        assert server.client.get("/api/server").json()["memory"] is False
        assert server.add("user", "Writes Go.").status_code == 201
        assert server.client.put("/api/memories/1", json={"category": "user", "text": "Writes Rust.", "revision": 1},
                                 headers=SEND).status_code == 200
        assert [m["text"] for m in server.listing()["memories"]] == ["Writes Rust."]
        assert server.client.delete("/api/memories/1?revision=2", headers=SEND).status_code == 204
        assert server.listing()["memories"] == []


def test_himari_memory_turns_it_off() -> None:
    from himari.interfaces.web.config import WebConfigError

    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).memory is True  # V3-P71 §9: on by default
    assert WebConfig.from_env({**base, "HIMARI_MEMORY": "0"}).memory is False
    assert WebConfig.from_env({**base, "HIMARI_MEMORY": " off "}).memory is False
    with pytest.raises(WebConfigError):
        WebConfig.from_env({**base, "HIMARI_MEMORY": "sometimes"})


def memory_results(server: Server, cid: str) -> list[dict[str, Any]]:
    items = server.client.get(f"/api/conversations/{cid}/events").json()["items"]
    return [e for e in items if e["kind"] == "tool_result"]


def test_a_memory_call_carries_its_line_and_undo_puts_it_back(server: Server) -> None:
    # V3-P71 §7.1: the events say what each memory call changed; the Undo
    # names the call only, the server reads what to put back.
    server.sign_in()
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]
    server.provider.extend([NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                                          {"category": "user", "text": "Uses Go."}),)),
                            final("Noted."),
                            NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                                          {"category": "user", "text": "Uses Rust.",
                                                           "replaces": "m1"}),)),
                            final("Noted.")])
    for _ in range(2):
        server.client.post(f"/api/conversations/{cid}/messages", json={"text": "remember"}, headers=SEND)
        server.wait(cid)
    first, second = memory_results(server, cid)
    assert first["memory"] == {"action": "saved", "id": 1, "text": "Uses Go.", "undo": False}  # rewritten since
    assert second["memory"] == {"action": "updated", "id": 1, "text": "Uses Rust.", "undo": True}

    path = f"/api/conversations/{cid}/calls/{second['run_id']}/{second['call_id']}/memory-undo"
    assert server.client.post(path).status_code == 403  # no origin: refused like any write
    undone = server.client.post(path, headers=SEND)
    assert undone.status_code == 200
    note = undone.json()["memory"]
    assert (note["text"], note["origin"], note["revision"]) == ("Uses Go.", "model", 3)
    assert note["source"] == {"conversation": cid, "title": None, "preview": "remember", "in_trash": False}
    again = server.client.post(path, headers=SEND)
    assert again.status_code == 409 and again.json()["error"]["code"] == "revision_conflict"
    assert [r["memory"]["undo"] for r in memory_results(server, cid)] == [False, False]

    gone = f"/api/conversations/{cid}/calls/{first['run_id']}/c9/memory-undo"
    refused = server.client.post(gone, headers=SEND)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "not_found"


def test_undo_of_a_new_note_deletes_it(server: Server) -> None:
    server.sign_in()
    cid = server.client.post("/api/conversations", json={}, headers=SEND).json()["id"]
    server.provider.extend([NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"),
                                                          {"category": "project", "text": "Plans a trip."}),)),
                            final("Noted.")])
    server.client.post(f"/api/conversations/{cid}/messages", json={"text": "remember"}, headers=SEND)
    server.wait(cid)
    (result,) = memory_results(server, cid)
    path = f"/api/conversations/{cid}/calls/{result['run_id']}/{result['call_id']}/memory-undo"
    assert server.client.post(path, headers=SEND).json() == {"memory": None}
    assert server.listing()["memories"] == []
    assert server.client.post(path, headers=SEND).status_code == 404


def test_himari_conversation_search_turns_it_off() -> None:  # V3-P72 §9, beside memory's own switch
    from himari.interfaces.web.config import WebConfigError

    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).conversation_search is True
    off = WebConfig.from_env({**base, "HIMARI_CONVERSATION_SEARCH": "0"})
    assert (off.conversation_search, off.memory) == (False, True)
    with pytest.raises(WebConfigError):
        WebConfig.from_env({**base, "HIMARI_CONVERSATION_SEARCH": "maybe"})


def test_himari_streaming_turns_it_off() -> None:  # V3-P73 §9
    from himari.interfaces.web.config import WebConfigError

    base = {"HIMARI_DATA_ROOT": "/srv/himari"}
    assert WebConfig.from_env(base).streaming is True
    off = WebConfig.from_env({**base, "HIMARI_STREAMING": "0"})
    assert (off.streaming, off.conversation_search, off.memory) == (False, True, True)
    with pytest.raises(WebConfigError):
        WebConfig.from_env({**base, "HIMARI_STREAMING": "maybe"})


# --- V3-P85 part 4: loops and dates, pass lines and Undo, proposals, the Timeline ----------------------------


def call(server: Server, fn: Any, *args: Any, **kwargs: Any) -> Any:
    assert server.client.portal is not None
    return server.client.portal.call(lambda: fn(*args, **kwargs))


def conversation(server: Server) -> tuple[str, Any]:
    from himari.common import ConversationId, PrincipalId, TrustedScope

    created = server.client.post("/api/conversations", json={}, headers=SEND)
    assert created.status_code in (200, 201), created.text
    cid = created.json()["id"]
    return cid, TrustedScope(PrincipalId("operator"), ConversationId(cid))


def passed(server: Server, scope: Any, said: str, items: list[Any], episode: Any = None) -> str:
    """A user message and an applied live pass over it, straight through Data."""
    from himari.common import TextBlock
    from himari.data.api import PassKind, PassStart, UserMessage

    app = server.application
    assert app is not None
    call(server, app.data.append_event, scope, UserMessage((TextBlock(said),)))
    last = call(server, app.data.last_event, scope)[0]
    cursor = call(server, app.data.read_memory_cursor, scope).through_sequence
    pass_id: str = call(server, app.data.begin_pass, scope, PassStart(PassKind.LIVE, cursor, last, last, "p", "m"))
    call(server, app.data.apply_pass, pass_id, items, episode, now=datetime.now(UTC))
    return pass_id


def test_a_loop_has_its_date_in_the_browsers_zone_and_an_edit_keeps_or_clears_it(server: Server) -> None:
    server.sign_in()
    added = server.client.post("/api/memories", json={"category": "loop", "text": "Interview.", "due": "2026-11-04",
                                                      "time_zone": "Asia/Seoul"}, headers=SEND)
    assert added.status_code == 201 and (added.json()["due"], added.json()["valid_until"]) == ("2026-11-04", None)
    note = call(server, server.application.data.read_memory, PrincipalId("operator"), 1)  # type: ignore[union-attr]
    assert note.due_instant == datetime(2026, 11, 4, 14, 59, 59, 999999, tzinfo=UTC)
    kept = server.client.put("/api/memories/1", json={"category": "loop", "text": "Interview at Kakao.",
                                                      "revision": 1}, headers=SEND)
    assert kept.status_code == 200 and kept.json()["due"] == "2026-11-04"  # neither date named: kept
    cleared = server.client.put("/api/memories/1", json={"category": "loop", "text": "Interview.", "revision": 2,
                                                         "due": None}, headers=SEND)
    assert cleared.status_code == 200 and cleared.json()["due"] is None
    fact = server.client.post("/api/memories", json={"category": "user", "text": "Away.", "valid_until": "2026-01-01",
                                                     "due": "2026-11-04"}, headers=SEND)
    assert fact.status_code == 201 and (fact.json()["valid_until"], fact.json()["expired"], fact.json()["due"]) == (
        "2026-01-01", True, None)
    bad = server.client.post("/api/memories", json={"category": "loop", "text": "x", "due": "next Friday"},
                             headers=SEND)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_date"


def test_a_pass_shows_its_lines_after_the_event_it_read_and_each_can_be_undone(server: Server) -> None:
    from himari.data.api import MemoryCategory, PassAction, PassItem

    server.sign_in()
    cid, scope = conversation(server)
    first = passed(server, scope, "I build HIMARI. Interview on Nov 4.", [
        PassItem(0, "{}", PassAction.ADD, category=MemoryCategory.PROJECT, text="Builds HIMARI."),
        PassItem(1, "{}", PassAction.ADD, category=MemoryCategory.LOOP, text="Interview."),
        PassItem(2, "{}", PassAction.PROPOSE, "sensitive_proposed", category=MemoryCategory.USER, text="Pills."),
        PassItem(3, "{}", PassAction.DROP, "bad_evidence")])
    passed(server, scope, "The interview went well.", [
        PassItem(0, "{}", PassAction.FORGET, memory_id=2, expected_revision=1)])
    lines = server.client.get(f"/api/conversations/{cid}/memory-passes").json()["passes"]
    assert [(p["through_sequence"], [(i["action"], i["text"], i["undo"]) for i in p["items"]]) for p in lines] == [
        (1, [("remembered", "Builds HIMARI.", True), ("open_loop", "Interview.", False),
             ("waiting", "Pills.", False)]),
        (2, [("closed_loop", "Interview.", True)])]  # the loop moved on since the first pass: no Undo there
    back = server.client.post(f"/api/conversations/{cid}/memory-passes/{lines[1]['pass_id']}/0/undo", headers=SEND)
    assert back.status_code == 200 and back.json()["memory"]["text"] == "Interview."
    again = server.client.post(f"/api/conversations/{cid}/memory-passes/{lines[1]['pass_id']}/0/undo", headers=SEND)
    assert again.status_code == 404
    removed = server.client.post(f"/api/conversations/{cid}/memory-passes/{first}/0/undo", headers=SEND)
    assert removed.status_code == 200 and removed.json() == {"memory": None}
    assert [m["text"] for m in server.listing()["memories"]] == ["Interview."]
    other, _ = conversation(server)
    elsewhere = server.client.post(f"/api/conversations/{other}/memory-passes/{first}/1/undo", headers=SEND)
    assert elsewhere.status_code == 404  # a pass is undone in its own conversation only


def test_proposals_are_kept_or_discarded_and_the_timeline_pages_and_edits(server: Server) -> None:
    from himari.data.api import EpisodePlan, MemoryCategory, PassAction, PassItem

    server.sign_in()
    cid, scope = conversation(server)
    now = datetime.now(UTC)
    passed(server, scope, "Things.", [
        PassItem(0, "{}", PassAction.PROPOSE, "sensitive_proposed", category=MemoryCategory.USER, text="Pills."),
        PassItem(1, "{}", PassAction.PROPOSE, "proposed", category=MemoryCategory.USER, text="Lived in Tokyo.")],
        EpisodePlan("Talked about things.", now, now, (1,), ()))
    proposals = server.client.get("/api/memories/proposals").json()["proposals"]
    assert [(p["kind"], p["text"], p["conversation"]["id"]) for p in proposals] == [
        ("sensitive", "Pills.", cid), ("past", "Lived in Tokyo.", cid)]
    assert [p["evidence"] for p in proposals] == [[], []]
    keep = server.client.post(f"/api/memories/proposals/{proposals[1]['pass_id']}/1", json={"decision": "keep"},
                              headers=SEND)
    assert keep.status_code == 201 and keep.json()["kept_by_user"] is True
    drop = server.client.post(f"/api/memories/proposals/{proposals[0]['pass_id']}/0", json={"decision": "discard"},
                              headers=SEND)
    assert drop.status_code == 204
    twice = server.client.post(f"/api/memories/proposals/{proposals[0]['pass_id']}/0", json={"decision": "keep"},
                               headers=SEND)
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "already_decided"
    for n in range(2):
        passed(server, scope, f"More {n}.", [], EpisodePlan(f"Later {n}.", now + timedelta(hours=n + 1),
                                                            now + timedelta(hours=n + 1), (1,), ()))
    page = server.client.get("/api/episodes?limit=2").json()
    assert [e["text"] for e in page["episodes"]] == ["Later 1.", "Later 0."] and page["next"]
    rest = server.client.get(f"/api/episodes?limit=2&cursor={page['next']}").json()
    assert [e["text"] for e in rest["episodes"]] == ["Talked about things."] and rest["next"] is None
    first = page["episodes"][0]
    edited = server.client.put(f"/api/episodes/{first['id']}", json={"text": "My words.", "revision": 1}, headers=SEND)
    assert edited.status_code == 200 and edited.json()["origin"] == "user"
    stale = server.client.put(f"/api/episodes/{first['id']}", json={"text": "x", "revision": 1}, headers=SEND)
    assert stale.status_code == 409
    assert server.client.delete(f"/api/episodes/{first['id']}?revision=2", headers=SEND).status_code == 204
    assert server.client.get("/api/episodes?cursor=bad").status_code == 400
    notice = server.client.get(f"/api/conversations/{cid}/delete-notice").json()
    assert (notice["memory_count"], notice["episode_count"]) == (1, 2)


def test_a_backfill_or_retry_says_when_memory_passes_do_not_run(server: Server) -> None:
    server.sign_in()
    cid, _ = conversation(server)
    off = server.client.post("/api/memory-passes/backfill", json={"conversation_id": cid}, headers=SEND)
    assert off.status_code == 409 and off.json()["error"]["code"] == "memory_passes_off"
    assert server.client.post(f"/api/conversations/{cid}/memory-passes/x/retry", headers=SEND).status_code == 409
    assert server.client.get("/api/server").json()["auto_memory"] == "off (no HIMARI_MEMORY_MODEL)"


def test_a_stale_suggestion_is_refused_then_kept_without_its_date(server: Server) -> None:
    from himari.data.api import MemoryCategory, PassAction, PassItem

    server.sign_in()
    _, scope = conversation(server)
    pass_id = passed(server, scope, "An old plan.", [
        PassItem(0, "{}", PassAction.PROPOSE, "proposed", category=MemoryCategory.LOOP, text="Dentist.",
                 due_at="2026-01-01", due_instant=datetime.now(UTC) - timedelta(days=30))])
    (waiting,) = server.client.get("/api/memories/proposals").json()["proposals"]
    assert waiting["evidence"] == []  # this record cites none; a pass's cites its user messages
    stale = server.client.post(f"/api/memories/proposals/{pass_id}/0", json={"decision": "keep"}, headers=SEND)
    assert stale.status_code == 422 and stale.json()["error"]["code"] == "stale_date"
    kept = server.client.post(f"/api/memories/proposals/{pass_id}/0", json={"decision": "keep", "due": None},
                              headers=SEND)
    assert kept.status_code == 201 and (kept.json()["text"], kept.json()["due"]) == ("Dentist.", None)


def test_lines_carry_their_evidence_and_a_cursor_must_have_a_zone(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from himari.bootstrap import application as app_module
    from himari.data.api import MemoryCategory, PassAction, PassItem

    server.sign_in()
    cid, scope = conversation(server)
    passed(server, scope, "I build HIMARI.", [PassItem(0, '{"evidence": [1]}', PassAction.ADD,
                                                       category=MemoryCategory.PROJECT, text="Builds HIMARI.")])
    passed(server, scope, "I write Go.", [PassItem(0, '{"evidence": [2]}', PassAction.ADD,
                                                   category=MemoryCategory.USER, text="Writes Go.")])
    lines = server.client.get(f"/api/conversations/{cid}/memory-passes").json()["passes"]
    assert [[i["evidence"] for i in line["items"]] for line in lines] == [[[1]], [[2]]]
    monkeypatch.setattr(app_module, "PASS_LINES_LIMIT", 1)
    newest = server.client.get(f"/api/conversations/{cid}/memory-passes").json()["passes"]
    assert [line["items"][0]["text"] for line in newest] == ["Writes Go."]  # at most the newest PASS_LINES_LIMIT
    naive = base64.urlsafe_b64encode(b"2026-10-31T12:00:00|5").decode()
    assert server.client.get(f"/api/episodes?cursor={naive}").status_code == 400
    squared = base64.urlsafe_b64encode("2026-10-31T12:00:00+00:00|\u00b2".encode()).decode()  # "²".isdigit()
    assert server.client.get(f"/api/episodes?cursor={squared}").status_code == 400


def test_a_loops_reminder_from_the_browser_kept_moved_or_cleared(server: Server) -> None:
    """V3-P87 §5.3, §9: the browser's local time in its zone; an edit that does not name it keeps it."""
    server.sign_in()
    added = server.client.post("/api/memories", json={"category": "loop", "text": "Medicine.", "due": "2026-11-04",
                                                      "remind_at": "2026-11-04T21:30", "time_zone": "Asia/Seoul"},
                               headers=SEND)
    body = added.json()
    assert added.status_code == 201, body
    assert (body["remind_at"], body["remind_instant"], body["remind_state"]) == (
        "2026-11-04T21:30+09:00", "2026-11-04T12:30:00+00:00", "scheduled")
    assert body["checks_in_at"] == "2026-11-04T09:00:00+00:00"  # its date's 18:00 in Seoul
    kept = server.client.put("/api/memories/1", json={"category": "loop", "text": "Medicine at night.",
                                                      "due": "2026-11-05", "revision": 1}, headers=SEND).json()
    assert (kept["due"], kept["remind_at"], kept["remind_state"]) == ("2026-11-05", "2026-11-04T21:30+09:00",
                                                                       "scheduled")
    moved = server.client.put("/api/memories/1", json={"category": "loop", "text": "Medicine.", "revision": 2,
                                                       "remind_at": "2026-11-04T22:00+09:00"}, headers=SEND).json()
    assert (moved["remind_at"], moved["due"]) == ("2026-11-04T22:00+09:00", "2026-11-05")
    cleared = server.client.put("/api/memories/1", json={"category": "loop", "text": "Medicine.", "revision": 3,
                                                         "remind_at": None}, headers=SEND).json()
    assert (cleared["remind_at"], cleared["remind_state"]) == (None, None)
    for bad in ("2026-11-04", "tonight"):
        refused = server.client.post("/api/memories", json={"category": "loop", "text": "x", "remind_at": bad},
                                     headers=SEND)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "invalid_date", bad
    fact = server.client.post("/api/memories", json={"category": "user", "text": "Away.",
                                                     "remind_at": "2026-11-04T21:30+09:00"}, headers=SEND).json()
    assert fact["remind_at"] is None  # only a loop has a reminder
