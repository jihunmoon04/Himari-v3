"""The review of an agent's file changes (V3-P76 §4.2–§7, §10).

``ChangeStore`` is the record (the ``ChangeLog`` port the machine file
Tools use, and what the owner's review reads): one JSON file per
Conversation in HIMARI's data folder, replaced atomically on every change,
each Conversation's changes in order under one lock.

- An **entry** per file (its real path): ``before`` — what the file was
  before its first change since review, fixed from then on (``present``
  with an agent version, ``absent``, or ``unknown``); ``after`` — its last
  known state (an identity, ``[]`` deleted, null unknown); ``state`` — ok,
  no_version or uncertain, only ever worse; ``interleaved`` — something
  else changed it between two of the agent's changes.
- An **intent** per Tool call: pending until the runner answers; dropped
  on a refusal; done, or unknown when the answer was lost (or HIMARI
  stopped meanwhile: ``recover``). The calls between their intent and its
  outcome are counted (``busy``): a turn's last answer may still be on its
  way after the turn ended (it was stopped).

``ChangeReview`` is the owner's side: the list, a diff, accepting
(forgetting an entry) and reverting (§6's table, by the first and the last
known state), each path checked against the runner's hidden list now.
Reverting is the owner's write: what the file holds then is kept as a
previous version of the owner's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from himari.bootstrap.explorer import Explorer, ExplorerRefused, text_of
from himari.bootstrap.versions import VersionStore
from himari.common import CallId, TrustedScope
from himari.environment.machine.changes import ABSENT, Identity

log = logging.getLogger("himari.changes")

OK, NO_VERSION, UNCERTAIN = "ok", "no_version", "uncertain"
_WORSE = {OK: 0, NO_VERSION: 1, UNCERTAIN: 2}
PRESENT, NONE, UNKNOWN = "present", "absent", "unknown"  # an entry's ``before``
_CONVERSATION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class _Token:
    conversation: str
    intent: str


@dataclass(frozen=True)
class ChangeView:
    path: str
    kind: str  # "created" | "changed" | "deleted" | "none" (made, then deleted) | "unknown"
    state: str  # ok | no_version | uncertain | expired (its previous version is gone)
    interleaved: bool
    count: int  # the agent's changes of it since review
    changed_at: datetime

    @property
    def revertible(self) -> bool:
        return self.state == OK


@dataclass(frozen=True)
class ChangesView:
    items: tuple[ChangeView, ...]
    hidden: int  # entries whose path is hidden now: counted, never shown (§10)
    commands_ran: datetime | None  # a command ran since review began (§9)


@dataclass(frozen=True)
class SideView:
    state: str  # "present" | "absent" | "unknown" | "expired"
    text: str | None  # None: not text, too large, or not there
    identity: Identity | None = None  # the file now (``now`` only)


@dataclass(frozen=True)
class ChangeDiff:
    path: str
    kind: str
    before: SideView
    now: SideView
    changed: bool  # the file is not as the agent left it
    interleaved: bool


@dataclass(frozen=True)
class RevertedAll:
    """What a revert of all left: each shown file not reverted, and why
    (one that cannot be reverted too); the hidden ones only counted (§10)."""

    left: tuple[tuple[str, ExplorerRefused | ChangeConflict], ...]
    hidden: int


class ChangeConflict(Exception):
    """§6: the file is not as the agent left it (or was changed between the
    agent's changes): not reverted. ``now``: what it is (ABSENT: no file)."""

    def __init__(self, path: str, now: Identity, interleaved: bool) -> None:
        super().__init__(f"{os.path.basename(path)} was changed by something other than this conversation's HIMARI "
                         "since; reverting undoes that too.")
        self.path = path
        self.now = now
        self.interleaved = interleaved


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _kind(entry: dict[str, Any]) -> str:
    after = entry.get("after")
    if after is None:
        return "unknown"
    if after == []:
        return "none" if entry["before"] == NONE else "deleted"
    return "created" if entry["before"] == NONE else "changed"


def _worsen(entry: dict[str, Any], state: str) -> None:
    if _WORSE[state] > _WORSE[entry["state"]]:
        entry["state"] = state


def _empty() -> dict[str, Any]:
    return {"entries": {}, "intents": {}, "commands_ran": None}


class ChangeStore:
    """The record (``ChangeLog``) and its file per Conversation."""

    def __init__(self, root: Path, versions: VersionStore) -> None:
        self.root = root
        self.versions = versions  # the agent's: a store and budget of their own (Q5)
        self._locks: dict[str, asyncio.Lock] = {}
        self._open: dict[str, int] = {}  # calls begun and not yet answered, per Conversation

    def busy(self, conversation: str) -> bool:
        """A Tool call's change in this Conversation is not answered yet."""
        return bool(self._open.get(conversation))

    def _close(self, token: _Token) -> None:
        left = self._open.get(token.conversation, 0) - 1
        if left > 0:
            self._open[token.conversation] = left
        else:
            self._open.pop(token.conversation, None)

    def lock(self, conversation: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation, asyncio.Lock())

    def _file(self, conversation: str) -> Path:
        if not _CONVERSATION.match(conversation):
            raise ValueError("a conversation id")
        return self.root / f"{conversation}.json"

    def load(self, conversation: str) -> dict[str, Any]:
        try:
            doc = json.loads(self._file(conversation).read_text("utf-8"))
        except FileNotFoundError:
            return _empty()
        if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict) \
                or not isinstance(doc.get("intents"), dict):
            raise OSError(f"the change record of {conversation} is not readable")
        return doc

    def save(self, conversation: str, doc: dict[str, Any]) -> None:
        target = self._file(conversation)
        if not doc["entries"] and not doc["intents"] and doc.get("commands_ran") is None:
            target.unlink(missing_ok=True)
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.root / f".{conversation}.{secrets.token_hex(4)}.tmp"
        with open(temp, "w", encoding="utf-8") as out:
            json.dump(doc, out)
            out.flush()
            os.fsync(out.fileno())
        temp.replace(target)

    def _change(self, conversation: str, edit: Callable[[dict[str, Any]], None]) -> None:
        doc = self.load(conversation)
        edit(doc)
        self.save(conversation, doc)

    async def transact(self, conversation: str, edit: Callable[[dict[str, Any]], None]) -> None:
        """``edit`` the record on a thread, under the Conversation's lock."""
        async with self.lock(conversation):
            await asyncio.to_thread(self._change, conversation, edit)

    # --- the ChangeLog port (environment/machine/changes.py) -------------------------------

    async def begin(self, scope: TrustedScope, call: CallId, path: str) -> object:
        intent = secrets.token_hex(8)

        def add(doc: dict[str, Any]) -> None:
            doc["intents"][intent] = {"call": str(call), "path": path, "state": "pending"}

        token = _Token(scope.conversation_id, intent)
        self._open[token.conversation] = self._open.get(token.conversation, 0) + 1
        try:
            await self.transact(scope.conversation_id, add)
        except BaseException:
            self._close(token)
            raise
        return token

    async def kept(self, token: object, path: str, copy: int | None, prior: Identity, after: Identity | None,
                   complete: bool) -> None:
        assert isinstance(token, _Token)
        held = [copy] if copy is not None else []

        def apply(doc: dict[str, Any]) -> None:
            intent = doc["intents"].get(token.intent)
            entry = doc["entries"].get(path)
            now = _now()
            if entry is None:  # the first change since review: what it was before is fixed now (§5)
                entry = {"before": NONE, "version": None, "after": None, "state": OK, "interleaved": False,
                         "first": now}
                if prior != ABSENT:
                    entry["before"] = UNKNOWN
                    entry["state"] = NO_VERSION
                    if held:
                        fd = held.pop()  # the store closes it, kept or not
                        try:
                            version = self.versions.keep(path, fd, "agent_delete" if after == ABSENT else "agent")
                            entry.update(before=PRESENT, version=version.version_id, state=OK)
                        except OSError:
                            log.exception("the previous version of %s could not be kept", path)
                doc["entries"][path] = entry
            elif entry["after"] is not None and list(prior) != entry["after"]:
                entry["interleaved"] = True  # changed by something else since the agent's last change
            entry["after"] = None if after is None else list(after)
            if not complete or after is None or (intent is not None and intent["path"] != path):
                _worsen(entry, UNCERTAIN)
            entry["last"] = now
            if intent is not None:
                intent.update(state="done", path=path)

        try:
            await self.transact(token.conversation, apply)
        except Exception:  # noqa: BLE001 — the intent stays pending: recover() makes it unknown
            log.exception("the change of %s could not be recorded", path)
        finally:
            self._close(token)
            for fd in held:
                os.close(fd)

    async def dropped(self, token: object) -> None:
        assert isinstance(token, _Token)

        def drop(doc: dict[str, Any]) -> None:
            doc["intents"].pop(token.intent, None)

        try:
            await self.transact(token.conversation, drop)
        except Exception:  # noqa: BLE001 — a pending intent left: recover() makes it unknown
            log.exception("a refused change's intent could not be dropped")
        finally:
            self._close(token)

    async def unknown(self, token: object) -> None:
        assert isinstance(token, _Token)
        try:
            await self.transact(token.conversation, lambda doc: _unknown(doc, token.intent))
        except Exception:  # noqa: BLE001 — a pending intent left: recover() makes it unknown
            log.exception("an unknown change could not be recorded")
        finally:
            self._close(token)

    async def commands_ran(self, scope: TrustedScope) -> None:
        def mark(doc: dict[str, Any]) -> None:
            doc["commands_ran"] = _now()

        try:
            await self.transact(scope.conversation_id, mark)
        except Exception:  # noqa: BLE001 — a notice, not a record of a change
            log.exception("could not note that a command ran")

    # --- the rest --------------------------------------------------------------------------

    def recover(self) -> int:
        """At start (§7): every intent still pending was cut short; whether
        its file changed is unknown. How many there were."""
        found = 0
        if not self.root.is_dir():
            return 0
        for record in self.root.glob("*.json"):
            conversation = record.stem
            try:
                doc = self.load(conversation)
                pending = [k for k, v in doc["intents"].items() if v.get("state") == "pending"]
                for intent in pending:
                    _unknown(doc, intent)
                if pending:
                    self.save(conversation, doc)
                    found += len(pending)
            except (OSError, ValueError):
                log.exception("the change record %s could not be recovered", record.name)
        return found

    def forget(self, conversation: str) -> None:
        """The Conversation is gone (purged): its record goes too."""
        self._file(conversation).unlink(missing_ok=True)


def _unknown(doc: dict[str, Any], intent_id: str) -> None:
    intent = doc["intents"].get(intent_id)
    if intent is None:
        return
    intent["state"] = "unknown"
    entry = doc["entries"].get(intent["path"])
    if entry is None:
        now = _now()
        doc["entries"][intent["path"]] = {"before": UNKNOWN, "version": None, "after": None, "state": UNCERTAIN,
                                          "interleaved": False, "first": now, "last": now}
    else:
        entry["after"] = None
        _worsen(entry, UNCERTAIN)


def _forget(doc: dict[str, Any], paths: Sequence[str]) -> None:
    for path in paths:
        doc["entries"].pop(path, None)
    doc["intents"] = {k: v for k, v in doc["intents"].items()
                      if v["path"] not in paths and (doc["entries"] or v["state"] == "pending")}
    if not doc["entries"]:
        doc["commands_ran"] = None


class ChangeReview:
    """The owner's side (§5, §6): reads and writes go through the explorer
    (the runner, its hidden list); reverting keeps what it replaces."""

    def __init__(self, store: ChangeStore, explorer: Explorer) -> None:
        self.store = store
        self.explorer = explorer

    async def _shown(self, path: str) -> bool:
        """The path is not hidden now, and still resolves to itself."""
        try:
            return await self.explorer.visible(path) == path
        except ExplorerRefused as refused:
            if refused.category in ("not_found", "not_directory", "invalid", "access", "links", "not_regular"):
                return False
            raise

    async def _entry(self, conversation: str, path: str) -> dict[str, Any]:
        entry: dict[str, Any] | None = (await asyncio.to_thread(self.store.load, conversation))["entries"].get(path)
        if entry is None or not await self._shown(path):
            raise ExplorerRefused("not_found", "No such change.")
        return entry

    def _state(self, path: str, entry: dict[str, Any]) -> str:
        if entry["state"] == OK and entry["before"] == PRESENT and not self.store.versions.has(path, entry["version"]):
            return "expired"
        return str(entry["state"])

    async def view(self, conversation: str) -> ChangesView:
        doc = await asyncio.to_thread(self.store.load, conversation)
        items: list[ChangeView] = []
        hidden = 0
        for path, entry in doc["entries"].items():
            if not await self._shown(path):
                hidden += 1
                continue
            count = sum(1 for v in doc["intents"].values() if v["path"] == path and v["state"] != "pending")
            items.append(ChangeView(path, _kind(entry), self._state(path, entry), bool(entry["interleaved"]),
                                    count, datetime.fromisoformat(entry.get("last") or entry["first"])))
        # §7: the ones that cannot be reverted first, then the latest.
        items.sort(key=lambda c: c.changed_at, reverse=True)
        items.sort(key=lambda c: c.state == OK)
        ran = doc.get("commands_ran")
        return ChangesView(tuple(items), hidden, datetime.fromisoformat(ran) if ran else None)

    async def _now(self, path: str) -> Identity:
        try:
            found = await self.explorer.stat(path)
        except ExplorerRefused as refused:
            if refused.category == "not_found":
                return ABSENT
            raise
        if found.get("kind") != "file" or found.get("path") != path:
            raise ExplorerRefused("not_regular", f"{os.path.basename(path)} is not a file now.")
        return tuple(int(v) for v in found["identity"])

    async def diff(self, conversation: str, path: str) -> ChangeDiff:
        entry = await self._entry(conversation, path)
        state = self._state(path, entry)
        if entry["before"] == PRESENT and state != "expired":
            data = await asyncio.to_thread(self.store.versions.read, path, entry["version"])
            before = SideView(PRESENT if data is not None else "expired", None if data is None else text_of(data))
        else:
            before = SideView("expired" if state == "expired" else entry["before"], None)
        try:
            shown = await self.explorer.show(path)
            now = SideView(PRESENT, shown.text if shown.kind == "text" and shown.editable else None, shown.identity)
        except ExplorerRefused as refused:
            if refused.category != "not_found":
                raise
            now = SideView(NONE, None, ABSENT)
        after = entry["after"]
        changed = after is None or tuple(after) != now.identity
        return ChangeDiff(path, _kind(entry), before, now, changed, bool(entry["interleaved"]))

    def _idle(self, conversation: str) -> None:
        """Under the lock: no Tool call's change is still on its way (its
        turn stopped, its answer not in yet)."""
        if self.store.busy(conversation):
            raise ExplorerRefused("busy", "HIMARI is still finishing a change here: try again shortly.")

    async def accept(self, conversation: str, paths: Sequence[str] | None) -> None:
        """Forget the entries (``None``: all, the hidden ones too): nothing is written."""
        if paths is not None:
            for path in paths:
                await self._entry(conversation, path)
        async with self.store.lock(conversation):
            self._idle(conversation)

            def accept(doc: dict[str, Any]) -> None:
                _forget(doc, list(doc["entries"]) if paths is None else paths)

            await asyncio.to_thread(self.store._change, conversation, accept)

    async def revert(self, conversation: str, path: str, seen: Identity | None) -> None:
        """§6: back to what it was before the agent's first change. ``seen``:
        the state the owner saw and chose to revert anyway (after a
        ChangeConflict); without it, only a file as the agent left it."""
        async with self.store.lock(conversation):
            self._idle(conversation)
            await self._revert(conversation, path, seen)

    async def revert_all(self, conversation: str) -> RevertedAll:
        """Each shown entry in turn; what was left, and why."""
        left: list[tuple[str, ExplorerRefused | ChangeConflict]] = []
        async with self.store.lock(conversation):
            self._idle(conversation)
            view = await self.view(conversation)
            for item in view.items:
                try:
                    await self._revert(conversation, item.path, None)
                except (ExplorerRefused, ChangeConflict) as why:
                    left.append((item.path, why))
        return RevertedAll(tuple(left), view.hidden)

    async def _revert(self, conversation: str, path: str, seen: Identity | None) -> None:
        entry = await self._entry(conversation, path)
        state = self._state(path, entry)
        if state != OK:
            raise ExplorerRefused("not_revertible", {
                NO_VERSION: "Its previous version could not be kept: accept it, or take an earlier version by hand.",
                UNCERTAIN: "Whether HIMARI changed it, or how, is unknown: it cannot be reverted; accept it.",
                "expired": "Its previous version has expired: it cannot be reverted; accept it.",
            }[state])
        content = None
        if entry["before"] == PRESENT:
            content = await asyncio.to_thread(self.store.versions.read, path, entry["version"])
            if content is None:
                raise ExplorerRefused("not_revertible", "Its previous version has expired: it cannot be reverted.")
        now = await self._now(path)
        if entry["interleaved"] or tuple(entry["after"]) != now:
            if seen is None or tuple(seen) != now:
                raise ChangeConflict(path, now, bool(entry["interleaved"]))
        # §6's table: what to do follows from the first state and the one now.
        if content is not None and now != ABSENT:
            await self.explorer.save(path, content, now, reason="revert")
        elif content is not None:
            await self.explorer.install(path, content)
        elif now != ABSENT:
            await self.explorer.delete(path, now)
        log.info("reverted the agent's change of %s", path)
        await asyncio.to_thread(self.store._change, conversation, lambda doc: _forget(doc, [path]))
