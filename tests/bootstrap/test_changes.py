"""The review record (V3-P76 §5, §7): each outcome a Tool reports, as the entry it leaves."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest

from himari.bootstrap.changes import ChangeReview, ChangeStore
from himari.bootstrap.explorer import Explorer, ExplorerRefused
from himari.bootstrap.versions import VersionStore
from himari.common import CallId, ConversationId, PrincipalId, TrustedScope

SCOPE = TrustedScope(PrincipalId("operator"), ConversationId("conv_1"))
A, B, C = (1, 2, 3, 4), (1, 2, 5, 6), (1, 2, 7, 8)


@pytest.fixture
def store(tmp_path: Path) -> ChangeStore:
    return ChangeStore(tmp_path / "changes", VersionStore(tmp_path / "versions"))


def copy_of(data: bytes) -> int:
    read, write = os.pipe()
    os.write(write, data)
    os.close(write)
    return read


def change(store: ChangeStore, path: str, copy: int | None, prior: tuple[int, ...], after: tuple[int, ...] | None,
           complete: bool = True, begun: str | None = None) -> None:
    async def run() -> None:
        token = await store.begin(SCOPE, CallId("c"), begun or path)
        await store.kept(token, path, copy, prior, after, complete)

    asyncio.run(run())


def entry(store: ChangeStore, path: str) -> dict[str, Any]:
    found: dict[str, Any] = store.load("conv_1")["entries"][path]
    return found


def test_the_first_change_fixes_before_and_the_last_after(store: ChangeStore) -> None:
    change(store, "/p/a", copy_of(b"A"), A, B)
    change(store, "/p/a", copy_of(b"B"), B, C)
    kept = entry(store, "/p/a")
    assert (kept["before"], kept["after"], kept["state"], kept["interleaved"]) == ("present", list(C), "ok", False)
    assert store.versions.read("/p/a", kept["version"]) == b"A"  # the first change's copy only
    assert len(store.versions.of("/p/a")) == 1
    change(store, "/p/new", None, (), A)
    assert entry(store, "/p/new")["before"] == "absent"
    change(store, "/p/gone", copy_of(b"G"), A, ())
    assert entry(store, "/p/gone")["after"] == [] and store.versions.of("/p/gone")[0].reason == "agent_delete"


def test_what_makes_an_entry_worse(store: ChangeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    change(store, "/p/big", None, A, B)  # no copy (over 1 MiB, or a link)
    assert (entry(store, "/p/big")["before"], entry(store, "/p/big")["state"]) == ("unknown", "no_version")
    change(store, "/p/partial", None, (), None, complete=False)
    assert (entry(store, "/p/partial")["after"], entry(store, "/p/partial")["state"]) == (None, "uncertain")
    change(store, "/p/moved", copy_of(b"M"), A, B, begun="/p/link")  # landed elsewhere than recorded first
    assert entry(store, "/p/moved")["state"] == "uncertain"
    change(store, "/p/moved", copy_of(b"M"), B, C)
    assert entry(store, "/p/moved")["state"] == "uncertain"  # never better again

    def full(*args: object) -> None:
        raise OSError("the data folder is full")

    monkeypatch.setattr(VersionStore, "keep", full)
    change(store, "/p/unkept", copy_of(b"U"), A, B)  # the change stands; its previous version is lost, and said so
    assert (entry(store, "/p/unkept")["before"], entry(store, "/p/unkept")["state"]) == ("unknown", "no_version")


def test_another_change_between_the_agents_is_interleaved(store: ChangeStore) -> None:
    change(store, "/p/a", copy_of(b"A"), A, B)
    change(store, "/p/a", copy_of(b"X"), C, (9, 9, 9, 9))  # it found C where it had left B
    assert entry(store, "/p/a")["interleaved"] and entry(store, "/p/a")["state"] == "ok"


def test_a_record_that_cannot_be_written_leaves_the_intent_pending(store: ChangeStore,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """§7 step 4: the Tool still succeeds; the intent is pending until a restart makes it unknown."""
    async def run() -> None:
        token = await store.begin(SCOPE, CallId("c"), "/p/a")
        monkeypatch.setattr(ChangeStore, "save", lambda self, conversation, doc: (_ for _ in ()).throw(OSError()))
        await store.kept(token, "/p/a", copy_of(b"A"), A, B, True)  # does not raise

    asyncio.run(run())
    monkeypatch.undo()
    assert [i["state"] for i in store.load("conv_1")["intents"].values()] == ["pending"]
    assert store.recover() == 1 and entry(store, "/p/a")["state"] == "uncertain"


def test_a_change_cut_short_is_unknown_after_a_restart(store: ChangeStore) -> None:
    """§7: an intent still pending when HIMARI stopped: whether the file changed is unknown."""
    asyncio.run(store.begin(SCOPE, CallId("c0"), "/p/a"))
    assert store.recover() == 1 and store.recover() == 0
    assert [i["state"] for i in store.load("conv_1")["intents"].values()] == ["unknown"]
    assert entry(store, "/p/a") | {"first": None, "last": None} == {
        "before": "unknown", "version": None, "after": None, "state": "uncertain", "interleaved": False,
        "first": None, "last": None}
    store.forget("conv_1")
    assert store.load("conv_1")["entries"] == {}


def test_nothing_is_accepted_or_reverted_while_a_change_is_on_its_way(store: ChangeStore,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped turn's last change may still be answered after the turn ended: the review waits for it."""
    review = ChangeReview(store, cast(Explorer, None))  # refused before the explorer is asked

    async def refused() -> None:
        for attempt in (review.accept("conv_1", None), review.revert("conv_1", "/p/a", None),
                        review.revert_all("conv_1")):
            with pytest.raises(ExplorerRefused) as busy:
                await attempt
            assert busy.value.category == "busy"

    async def run() -> None:
        first = await store.begin(SCOPE, CallId("c1"), "/p/a")
        second = await store.begin(SCOPE, CallId("c2"), "/p/b")
        assert store.busy("conv_1") and not store.busy("conv_2")
        await refused()
        await store.kept(first, "/p/a", None, (), A, True)
        await refused()  # one still on its way
        await store.unknown(second)
        assert not store.busy("conv_1")
        third = await store.begin(SCOPE, CallId("c3"), "/p/c")
        await store.dropped(third)
        monkeypatch.setattr(ChangeStore, "save", lambda self, conversation, doc: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            await store.begin(SCOPE, CallId("c4"), "/p/d")  # not recorded: not on its way either
        monkeypatch.undo()
        assert not store.busy("conv_1")
        await review.accept("conv_1", None)

    asyncio.run(run())
    assert store.load("conv_1")["entries"] == {}


def test_the_agents_versions_never_push_out_the_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Q5: a budget each (their own roots)."""
    from himari.bootstrap import versions

    owner = VersionStore(tmp_path / "file-versions")
    agent = ChangeStore(tmp_path / "changes", VersionStore(tmp_path / "file-versions-agent"))
    owner.keep("/p/a", copy_of(b"mine"), "save")
    monkeypatch.setattr(versions, "MAX_BYTES", 10)
    for n in range(5):
        change(agent, f"/p/{n}", copy_of(b"agent's"), A, B)
    assert [v.reason for v in owner.of("/p/a")] == ["save"]
    assert sum(len(agent.versions.of(f"/p/{n}")) for n in range(5)) == 1  # only the newest fits its own budget
