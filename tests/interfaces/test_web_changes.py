"""The review of the agent's file changes over the API (V3-P76 §4–§7, §10): a real runner (fake sandbox) on a
temporary home, the agent's Tool calls scripted."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from runnerkit import RunnerKit, start_runner  # noqa: E402
from testkit import calls, final  # noqa: E402
from webkit import Server  # noqa: E402

from himari.bootstrap.changes import ChangeReview  # noqa: E402
from himari.common import ConversationId, PrincipalId, TrustedScope  # noqa: E402
from himari.control.api import ControlError, ControlErrorCode  # noqa: E402
from himari.interfaces.web import app as web  # noqa: E402


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path / "runner")
    yield started
    started.close()


@pytest.fixture
def server(tmp_path: Path, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    s = Server(tmp_path, kit.socket)
    with s.client:
        s.sign_in()
        yield s


def refused(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code
    return error


def folder(kit: RunnerKit, cid: str) -> Path:
    """The Conversation's own folder (its first Workspace), made ahead to put files in."""
    made = kit.himari_home / "conversations" / cid
    made.mkdir(parents=True, exist_ok=True)
    return made


def agent(server: Server, cid: str, *steps: tuple[str, dict[str, Any]]) -> None:
    """One turn: the agent's Tool calls, one after another, then its answer."""
    server.provider.extend([*(calls((f"machine/{tool}", args)) for tool, args in steps), final("done")])
    assert server.post(f"/api/conversations/{cid}/messages", {"text": "go"}).status_code == 202
    for _ in range(500):
        if not server.client.get(f"/api/conversations/{cid}").json()["job"]["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("the turn did not end")


def changes(server: Server, cid: str) -> dict[str, Any]:
    response = server.client.get(f"/api/conversations/{cid}/changes")
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def by_name(listed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in listed["items"]}


def revert(server: Server, cid: str, path: Path, seen: dict[str, Any] | None = None) -> Any:
    return server.post(f"/api/conversations/{cid}/changes/revert",
                       {"path": str(path), **({"seen": seen} if seen is not None else {})})


def test_each_change_is_listed_and_reverted_by_its_first_and_last_state(server: Server, kit: RunnerKit) -> None:
    """§6's table: changed → the first version back; made → deleted; deleted → put back; made then deleted →
    nothing to do."""
    cid = server.new()
    here = folder(kit, cid)
    (here / "a.txt").write_text("A\n")
    (here / "gone.txt").write_text("G\n")
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "B\n"}),
          ("edit_file", {"path": "a.txt", "old_text": "B", "new_text": "C"}),
          ("write_file", {"path": "new.txt", "content": "N\n"}), ("delete_file", {"path": "gone.txt"}),
          ("write_file", {"path": "tmp.txt", "content": "T\n"}), ("delete_file", {"path": "tmp.txt"}))
    listed = changes(server, cid)
    items = by_name(listed)
    assert {n: (i["kind"], i["count"], i["state"], i["revertible"]) for n, i in items.items()} == {
        "a.txt": ("changed", 2, "ok", True), "new.txt": ("created", 1, "ok", True),
        "gone.txt": ("deleted", 1, "ok", True), "tmp.txt": ("none", 2, "ok", True)}
    assert items["a.txt"]["path"] == str(here / "a.txt") and listed["hidden"] == 0 and not listed["running"]
    diff = server.client.get(f"/api/conversations/{cid}/changes/diff", params={"path": str(here / "a.txt")}).json()
    assert diff["before"] == {"state": "present", "text": "A\n"} and diff["now"]["text"] == "C\n"
    assert diff["now"]["exists"] and not diff["changed"] and not diff["interleaved"]
    assert (diff["kind"], diff["state"], diff["revertible"]) == ("changed", "ok", True)
    made = server.client.get(f"/api/conversations/{cid}/changes/diff", params={"path": str(here / "new.txt")}).json()
    assert made["before"] == {"state": "absent", "text": None} and made["now"]["text"] == "N\n"
    deleted = server.client.get(f"/api/conversations/{cid}/changes/diff",
                                params={"path": str(here / "gone.txt")}).json()
    assert deleted["before"]["text"] == "G\n" and deleted["now"] == {"exists": False, "identity": None, "text": None}
    assert server.post(f"/api/conversations/{cid}/changes/revert-all", {}).json() == {"left": [], "hidden": 0}
    assert (here / "a.txt").read_text() == "A\n" and (here / "gone.txt").read_text() == "G\n"
    assert not (here / "new.txt").exists() and not (here / "tmp.txt").exists()
    assert changes(server, cid)["items"] == []
    # What the agent had written is kept as the owner's previous version; what was before it, as the agent's.
    versions = server.client.get("/api/machine/versions", params={"path": str(here / "a.txt")}).json()["items"]
    assert [v["reason"] for v in versions] == ["revert", "agent"]


@pytest.mark.parametrize(("steps", "end"), [
    ((("write_file", {"path": "a.txt", "content": "B\n"}), ("delete_file", {"path": "a.txt"})), "A\n"),  # put back
    ((("delete_file", {"path": "a.txt"}), ("write_file", {"path": "a.txt", "content": "B\n"})), "A\n"),  # B → A
])
def test_a_file_changed_and_deleted_or_deleted_and_made_again(server: Server, kit: RunnerKit,
                                                             steps: tuple[tuple[str, dict[str, Any]], ...],
                                                             end: str) -> None:
    cid = server.new()
    here = folder(kit, cid)
    (here / "a.txt").write_text("A\n")
    agent(server, cid, *steps)
    assert by_name(changes(server, cid))["a.txt"]["count"] == 2
    assert revert(server, cid, here / "a.txt").status_code == 200
    assert (here / "a.txt").read_text() == end and changes(server, cid)["items"] == []


def test_a_file_changed_since_is_reverted_only_as_it_was_seen(server: Server, kit: RunnerKit) -> None:
    cid = server.new()
    here = folder(kit, cid)
    (here / "a.txt").write_text("A\n")
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "B\n"}))
    stat = server.client.get("/api/machine/stat", params={"path": str(here / "a.txt")}).json()
    saved = server.client.put("/api/machine/file", json={"path": str(here / "a.txt"), "content": "owner's\n",
                                                         "expected": stat["identity"]}, headers={"origin": "http://testserver"})
    assert saved.status_code == 200, saved.text
    conflict = refused(revert(server, cid, here / "a.txt"), 409, "changed")
    assert conflict["now"] == {"exists": True, "identity": saved.json()["identity"]} and not conflict["interleaved"]
    diff = server.client.get(f"/api/conversations/{cid}/changes/diff", params={"path": str(here / "a.txt")}).json()
    assert diff["changed"] and diff["now"]["text"] == "owner's\n"
    (here / "a.txt").write_text("again\n")  # changed once more after it was shown: refused again
    refused(revert(server, cid, here / "a.txt", conflict["now"]), 409, "changed")
    now = refused(revert(server, cid, here / "a.txt"), 409, "changed")["now"]
    assert revert(server, cid, here / "a.txt", now).status_code == 200
    assert (here / "a.txt").read_text() == "A\n"
    # The owner's text it replaced is kept.
    versions = server.client.get("/api/machine/versions", params={"path": str(here / "a.txt")}).json()["items"]
    assert versions[0]["reason"] == "revert"
    # Deleted by the agent, made again by someone else: changed too.
    agent(server, cid, ("delete_file", {"path": "a.txt"}))
    (here / "a.txt").write_text("someone's\n")
    seen = refused(revert(server, cid, here / "a.txt"), 409, "changed")["now"]
    assert seen["exists"]
    assert revert(server, cid, here / "a.txt", seen).status_code == 200 and (here / "a.txt").read_text() == "A\n"


def test_a_change_between_the_agents_changes_is_interleaved(server: Server, kit: RunnerKit) -> None:
    cid = server.new()
    here = folder(kit, cid)
    (here / "a.txt").write_text("A\n")
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "B\n"}))
    (here / "a.txt").write_text("owner's\n")
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "D\n"}))
    assert by_name(changes(server, cid))["a.txt"]["interleaved"]
    conflict = refused(revert(server, cid, here / "a.txt"), 409, "changed")  # as the agent left it, yet not alone
    assert conflict["interleaved"]
    assert revert(server, cid, here / "a.txt", conflict["now"]).status_code == 200
    assert (here / "a.txt").read_text() == "A\n"


def test_a_refused_change_leaves_the_entry_as_it_was(server: Server, kit: RunnerKit) -> None:
    """§5's example: A→B, then a call the runner refuses: only that call's intent goes."""
    cid = server.new()
    here = folder(kit, cid)
    (here / "a.txt").write_text("A\n")
    (here / "sub").mkdir()
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "B\n"}), ("write_file", {"path": "sub", "content": "x"}),
          ("delete_file", {"path": "missing.txt"}))
    items = by_name(changes(server, cid))
    assert list(items) == ["a.txt"] and items["a.txt"]["count"] == 1
    store = server.application.changes  # type: ignore[union-attr]
    assert store is not None
    assert [i["state"] for i in store.load(cid)["intents"].values()] == ["done"]


def test_accepting_and_the_turn(server: Server, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = server.new()
    here = folder(kit, cid)
    agent(server, cid, ("write_file", {"path": "a.txt", "content": "A\n"}), ("write_file", {"path": "b.txt", "content": "B\n"}),
          ("run_command", {"argv": ["true"]}))
    listed = changes(server, cid)
    assert listed["commands_ran"] is not None and len(listed["items"]) == 2
    refused(server.post(f"/api/conversations/{cid}/changes/accept", {}), 400, "invalid_argument")
    application = server.application
    assert application is not None
    running = type("Status", (), {"running": True})()
    with monkeypatch.context() as m:  # §6: while a turn runs, nothing is accepted or reverted
        m.setattr(application.runner, "status", lambda scope: running)
        m.setattr(application.runner, "_running_in", lambda conversation_id: True)
        refused(server.post(f"/api/conversations/{cid}/changes/accept", {"all": True}), 409, "busy")
        refused(revert(server, cid, here / "a.txt"), 409, "busy")
        refused(server.post(f"/api/conversations/{cid}/changes/revert-all", {}), 409, "busy")
        assert changes(server, cid)["running"]
    # Review C1: and no turn starts while one is accepted or reverted.
    told: list[ControlErrorCode] = []
    accept = ChangeReview.accept

    async def accept_and_send(self: ChangeReview, conversation: str, paths: Any) -> None:
        with pytest.raises(ControlError) as busy:
            await application.runner.submit(TrustedScope(PrincipalId("anyone"), ConversationId(cid)), None)
        told.append(busy.value.code)
        await accept(self, conversation, paths)

    with monkeypatch.context() as m:
        m.setattr(ChangeReview, "accept", accept_and_send)
        assert server.post(f"/api/conversations/{cid}/changes/accept",
                           {"paths": [str(here / "a.txt")]}).status_code == 200
    assert told == [ControlErrorCode.BUSY]
    assert list(by_name(changes(server, cid))) == ["b.txt"] and (here / "a.txt").read_text() == "A\n"
    assert server.post(f"/api/conversations/{cid}/changes/accept", {"all": True}).status_code == 200
    assert changes(server, cid) == {"items": [], "hidden": 0, "commands_ran": None, "running": False}
    refused(revert(server, cid, here / "b.txt"), 404, "not_found")
    # Another principal's conversation, or none: not found.
    refused(server.client.get("/api/conversations/nope/changes"), 404, "not_found")


def test_a_path_hidden_now_is_counted_only(server: Server, kit: RunnerKit) -> None:
    """§10: the hidden list is applied on every request; a path that resolves elsewhere now is not shown either."""
    cid = server.new()
    here = folder(kit, cid)
    (here / "sub").mkdir()
    agent(server, cid, ("write_file", {"path": "sub/x.txt", "content": "X\n"}),
          ("write_file", {"path": "y.txt", "content": "Y\n"}))
    (here / "sub").rename(here / "elsewhere")
    (here / "sub").symlink_to(kit.home / ".ssh")
    listed = changes(server, cid)
    assert [i["name"] for i in listed["items"]] == ["y.txt"] and listed["hidden"] == 1
    hidden = str(here / "sub/x.txt")
    refused(server.client.get(f"/api/conversations/{cid}/changes/diff", params={"path": hidden}), 404, "not_found")
    refused(server.post(f"/api/conversations/{cid}/changes/revert", {"path": hidden}), 404, "not_found")
    refused(server.post(f"/api/conversations/{cid}/changes/accept", {"paths": [hidden]}), 404, "not_found")
    (here / "sub").unlink()
    (here / "sub").symlink_to(here / "elsewhere")  # the same name, another folder: still not shown
    assert changes(server, cid)["hidden"] == 1
    assert server.post(f"/api/conversations/{cid}/changes/revert-all", {}).json() == {"left": [], "hidden": 1}
    assert not (here / "y.txt").exists()  # the shown one was reverted; the hidden one left, counted
    assert server.post(f"/api/conversations/{cid}/changes/accept", {"all": True}).status_code == 200
    assert changes(server, cid)["hidden"] == 0


def test_what_cannot_be_reverted(server: Server, kit: RunnerKit) -> None:
    """§4.2, §7: no previous version (over the copy limit), or an outcome unknown: accepted only."""
    cid = server.new()
    here = folder(kit, cid)
    (here / "big.txt").write_text("x" * (1024 * 1024 + 1))
    agent(server, cid, ("write_file", {"path": "big.txt", "content": "small"}))
    item = by_name(changes(server, cid))["big.txt"]
    assert (item["state"], item["revertible"], item["kind"]) == ("no_version", False, "changed")
    assert refused(revert(server, cid, here / "big.txt"), 409, "not_revertible")["message"].startswith("Its previous")
    left = server.post(f"/api/conversations/{cid}/changes/revert-all", {}).json()  # review C3: left, and why
    assert [(i["path"], i["code"]) for i in left["left"]] == [(str(here / "big.txt"), "not_revertible")]
    assert left["left"][0]["message"].startswith("Its previous version could not be kept") and left["hidden"] == 0
    assert (here / "big.txt").read_text() == "small"


def test_a_change_that_cannot_be_recorded_is_not_made(server: Server, kit: RunnerKit) -> None:
    """§7 step 1: no intent recorded, nothing written."""
    cid = server.new()
    here = folder(kit, cid)
    store = server.application.changes  # type: ignore[union-attr]
    assert store is not None
    store.root.mkdir(parents=True, exist_ok=True)
    store.root.chmod(0o500)
    try:
        agent(server, cid, ("write_file", {"path": "a.txt", "content": "A\n"}))
    finally:
        store.root.chmod(0o700)
    assert not (here / "a.txt").exists()
    result = server.provider.prepared_inputs[-1]
    assert "could not be recorded" in str(result.conversation[-1])


def test_a_deleted_file_is_put_back_in_one_step(server: Server, kit: RunnerKit) -> None:
    """§4.1: the editor's Deleted files restore, never over a name taken since."""
    target = kit.home / "projects/a/notes.txt"
    target.write_text("notes\n")
    stat = server.client.get("/api/machine/stat", params={"path": str(target)}).json()
    assert server.post("/api/machine/file/delete", {"path": str(target), "expected": stat["identity"]}).status_code == 200
    version = server.client.get("/api/machine/versions/deleted", params={"path": str(target.parent)}).json()["items"][0]
    made = server.post(f"/api/machine/versions/{version['id']}/restore", {"path": str(target)})
    assert made.status_code == 201 and made.json()["path"] == str(target) and target.read_text() == "notes\n"
    refused(server.post(f"/api/machine/versions/{version['id']}/restore", {"path": str(target)}), 409, "changed")
    refused(server.post("/api/machine/versions/nope/restore", {"path": str(target)}), 404, "not_found")
