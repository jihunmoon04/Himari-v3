"""The hub's user side (V3-P95, V3-P102 §6): what coding agents wait on the user for, their sessions, and the user's
own work on them.

HIMARI hands project work to the hub (HIMARI v4), which runs coding agents
on the user's devices. The hub keeps the user's side apart from its MCP tools
(``/user/<op>`` with ``user.token``, contract ``docs/user-api-v1.md`` in the
hub), so the model that handed work over cannot answer its own agent's
request.

This is that path for the Web: the server holds the user token (a file only
the service account reads) and passes the signed-in operator's calls on.
The token never reaches the browser, the model or a Tool. What comes back is
rebuilt field by field from the contract (``*_view``): a field of the wrong
type is left out, a text is cut to its limit, an unknown event kind keeps
only its place (``seq``, ``kind``, ``at``) so a reader's cursor never stops.
The text in it was written by an agent; the page shows it only through its
safe renderers.

One short request per call, run off the event loop. The live streams are in
``hub_stream``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

TIMEOUT_SECONDS = 10.0
MAX_TEXT = 4000  # of one shown text: a request's title, a question, a choice
MAX_LONG = 70_000  # of an agent's text, a tool's input or output, one side of a diff (the hub keeps an event to 64 KB)
MAX_ITEMS = 50  # requests, and the choices or fields of one
MAX_SESSIONS = 500  # the hub's own limit for one ``sessions`` call
MAX_EVENTS = 1000  # the hub's own limit for one ``events`` call
MAX_PROMPT = 200_000  # of a prompt sent to an agent


class HubUnavailable(Exception):
    """The hub did not answer, or its token could not be read; nothing was sent or done."""


class HubUnknown(HubUnavailable):
    """The request may have reached the hub, but no answer came back (a timeout, a dropped connection, an
    unreadable reply): it may have been done. A ``send`` or ``new`` must not be repeated blindly (the hub keeps no
    key to drop a duplicate)."""


class HubMisconfigured(HubUnavailable):
    """The hub refused the token (401) or the address it was reached by (403): the operator's setup is wrong."""


class HubBusy(HubUnavailable):
    """The hub has no live stream left (503 ``streams``): a reader falls back to asking."""


class HubNotFound(Exception):
    """The hub has no such session (404, the live stream only)."""


class HubRefused(Exception):
    """The hub answered and refused (the request is no longer waiting, the choice is not one of its)."""

    @property
    def bad_cursor(self) -> bool:
        """``events`` was given a cursor past the session's end: the reader drops it and reads from 0."""
        return str(self).startswith("bad_cursor")


def _text(value: object) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def _choice(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("value"), (str, int, float, bool)):
        return None
    return {"value": raw["value"], "title": _text(raw.get("title")), "description": _text(raw.get("description"))}


def request_view(raw: object) -> dict[str, Any] | None:
    """One waiting request as the page gets it, or None when it is not one the page can answer."""
    if not isinstance(raw, dict):
        return None
    request_id, task_id, kind = raw.get("id"), raw.get("task_id"), raw.get("kind")
    if not isinstance(request_id, int) or not isinstance(task_id, int) or kind not in ("permission", "question"):
        return None
    view: dict[str, Any] = {"id": request_id, "task_id": task_id, "kind": kind, "asks": _text(raw.get("asks"))}
    task = raw.get("task")
    if isinstance(task, dict):
        view["task"] = {name: _text(task.get(name)) for name in ("project", "agent", "runner", "prompt")}
    if kind == "permission":
        given = raw.get("options")
        options: list[Any] = given if isinstance(given, list) else []
        view["options"] = [{"option_id": o["option_id"][:200], "name": _text(o.get("name")), "kind": _text(o.get("kind"))}
                           for o in options[:MAX_ITEMS] if isinstance(o, dict) and isinstance(o.get("option_id"), str)]
    else:
        asked = raw.get("fields")
        fields: dict[Any, Any] = asked if isinstance(asked, dict) else {}
        view["fields"] = [
            {"name": name[:200], "title": _text(spec.get("title")), "description": _text(spec.get("description")),
             "multiple": spec.get("multiple") is True,
             "choices": [c for c in map(_choice, (spec.get("choices") or [])[:MAX_ITEMS]) if c is not None]}
            for name, spec in list(fields.items())[:MAX_ITEMS] if isinstance(name, str) and isinstance(spec, dict)]
    return view


# -- the rest of the contract, field by field (user-api-v1 §3, §4, §6) ----------------------

_BAD = object()  # a field of the wrong type: left out
Convert = Callable[[object], object]


def _int(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) else _BAD


def _bool(value: object) -> object:
    return value if isinstance(value, bool) else _BAD


def _text_of(limit: int) -> Convert:
    return lambda value: value[:limit] if isinstance(value, str) else _BAD


_short, _long, _name = _text_of(MAX_TEXT), _text_of(MAX_LONG), _text_of(200)


def _id(value: object) -> object:
    """An id the hub gives as a number or a string (an interaction's, a tool call's)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else _short(value)


def _list_of(convert: Convert, limit: int = MAX_ITEMS) -> Convert:
    def converted(value: object) -> object:
        if not isinstance(value, list):
            return _BAD
        return [item for item in map(convert, value[:limit]) if item is not _BAD]
    return converted


def _record(spec: Mapping[str, Convert]) -> Convert:
    """Only the fields in ``spec``, only those the hub gave (an update leaves the rest as they were), only of the
    right type; ``None`` where the hub sent null."""
    def converted(value: object) -> object:
        if not isinstance(value, dict):
            return _BAD
        out: dict[str, object] = {}
        for name, convert in spec.items():
            if name in value:
                item = None if value[name] is None else convert(value[name])
                if item is not _BAD:
                    out[name] = item
        return out
    return converted


def _scalars(value: object) -> object:
    """A small mapping of names to plain values (a profile's options, a setup's counts)."""
    if not isinstance(value, dict):
        return _BAD
    return {key[:200]: item[:MAX_TEXT] if isinstance(item, str) else item
            for key, item in list(value.items())[:MAX_ITEMS]
            if isinstance(key, str) and isinstance(item, (int, float, bool, str))}


def _names_by_agent(value: object) -> object:
    if not isinstance(value, dict):
        return _BAD
    return {agent[:200]: [name[:200] for name in names[:MAX_ITEMS] if isinstance(name, str)]
            for agent, names in list(value.items())[:MAX_ITEMS] if isinstance(agent, str) and isinstance(names, list)}


_names = _list_of(_name)
TASK = _record({"id": _int, "status": _short, "agent": _short, "runner": _short, "profile": _short, "fresh": _bool,
                "model": _short, "effort": _short, "mode": _short, "prompt": _short, "created_at": _short,
                "finished_at": _short, "session": _int})
SESSION = _record({
    "id": _int, "project": _short, "agent": _short, "runner": _short, "runner_connected": _bool, "profile": _short,
    "state": _short, "closed_reason": _short, "activity": _short, "task": _int, "last_task": _int, "model": _short,
    "effort": _short, "context": _record({"used": _int, "size": _int}), "created_at": _short, "migrated": _bool,
    "closed_at": _short, "first_seq": _int, "last_seq": _int, "live": _bool, "queued": _int, "current": _bool,
    "updated_at": _short,  # H7 (V3-P102 §8), once the hub has it
})
_ATTEMPT = _record({"id": _int, "task_id": _int, "agent": _short, "runner": _short, "live": _bool,
                    "started_at": _short, "stop_requested_at": _short, "prompted": _bool, "ended_at": _short,
                    "outcome": _short, "why": _short})
_QUEUED = _record({"id": _int, "prompt": _short, "created_at": _short})
PROJECTS = _record({
    "projects": _list_of(_record({"name": _short, "path": _short, "default_agent": _short, "preset": _short,
                                  "profile": _short}), MAX_SESSIONS),
    "profiles": _list_of(_record({"name": _short, "agent": _short, "description": _short, "options": _scalars,
                                  "fresh": _bool}), MAX_SESSIONS),
    "models": _names_by_agent, "efforts": _names_by_agent,
    "agents": _list_of(_record({"name": _short, "installed": _bool, "resting_until": _short, "modes": _names})),
    "runners": _list_of(_record({"name": _short, "agents": _names, "projects": _list_of(_name, MAX_SESSIONS),
                                 "connected": _bool})),
})
_DIFF = _record({"path": _short, "old": _long, "new": _long, "cut": _bool, "more": _int})
BODIES: dict[str, Convert] = {
    "session": _record({"action": _short, "native_id": _short}),
    "setup": _record({"counts": _scalars, "tools": _list_of(_short)}),
    "turn_start": _record({"task_id": _int, "prompt": _long, "agent": _short, "model": _short, "effort": _short,
                           "mode": _short, "prefix_chars": _int}),
    "text": _record({"text": _long, "synthetic": _bool}),
    "thought": _record({"text": _long, "synthetic": _bool}),
    "tool": _record({"id": _id, "title": _short, "kind": _short, "status": _short, "input": _long, "output": _long,
                     "locations": _list_of(_short), "diffs": _list_of(_DIFF, 10)}),
    "plan": _record({"entries": _list_of(_record({"content": _short, "status": _short}), 200)}),
    "usage": _record({"used": _int, "size": _int}),
    "request": _record({"interaction_id": _id, "kind": _short, "title": _short, "message": _short,
                        "options": _list_of(_record({"option_id": _short, "name": _short, "kind": _short})),
                        "fields": _list_of(_record({"name": _short, "type": _short, "title": _short}))}),
    "answer": _record({"interaction_id": _id, "kind": _short, "outcome": _short, "option_id": _short,
                       "fields": _list_of(_short)}),
    "turn_end": _record({"task_id": _int, "outcome": _short, "why": _short, "stop_reason": _short,
                         "synthetic": _bool}),
    "truncated": _record({"group": _short, "events": _int, "bytes": _int, "first": _int, "last": _int}),
}
_EVENT_HEAD = _record({"seq": _int, "kind": _short, "attempt": _int, "at": _short, "truncated": _bool})


def _view(convert: Convert, raw: object) -> dict[str, Any] | None:
    shown = convert(raw)
    return shown if isinstance(shown, dict) else None


def task_view(raw: object) -> dict[str, Any] | None:
    """A task (§3.1, and its ``session`` where the hub gives it), or None without its id."""
    shown = _view(TASK, raw)
    return shown if shown is not None and isinstance(shown.get("id"), int) else None


def session_view(raw: object) -> dict[str, Any] | None:
    """A session as listed (§3.3), or None without its id."""
    shown = _view(SESSION, raw)
    return shown if shown is not None and isinstance(shown.get("id"), int) else None


def session_detail(raw: object) -> dict[str, Any] | None:
    """``session`` (§4.3): the listed fields, its waiting requests, recent attempts, and the prompts queued for it
    (here ``queued`` is that list, not the count of the listing)."""
    shown = session_view(raw)
    if shown is None or not isinstance(raw, dict):
        return None
    pending, attempts, queued = raw.get("pending"), raw.get("attempts"), raw.get("queued")
    shown["pending"] = [view for view in map(request_view, (pending if isinstance(pending, list) else [])[:MAX_ITEMS])
                        if view is not None]
    shown["attempts"] = _list_of(_ATTEMPT)(attempts) if isinstance(attempts, list) else []
    shown["queued"] = _list_of(_QUEUED, MAX_SESSIONS)(queued) if isinstance(queued, list) else []
    return shown


def event_view(raw: object) -> dict[str, Any] | None:
    """One event (§6), or None without its ``seq`` and ``kind``. An unknown kind keeps its place, body empty."""
    head = _view(_EVENT_HEAD, raw)
    if head is None or not isinstance(head.get("seq"), int) or not isinstance(head.get("kind"), str):
        return None
    convert = BODIES.get(head["kind"])
    body = raw.get("body") if isinstance(raw, dict) else None
    shown = (_view(convert, body) or {}) if convert is not None else {}
    if convert is not None and isinstance(body, dict) and body.get("truncated") is True:
        shown["truncated"] = True  # the hub cut a text of this event to fit (its events.py, ledger.py)
    head["body"] = shown
    return head


def sessions_of(items: object, limit: int = MAX_SESSIONS) -> list[dict[str, Any]]:
    return [view for view in map(session_view, (items if isinstance(items, list) else [])[:limit])
            if view is not None]


def projects_view(raw: object) -> dict[str, Any]:
    """``projects`` (§4.1): what the page offers to choose from."""
    return _view(PROJECTS, raw) or {}


def _options(**given: object) -> dict[str, object]:
    """The optional inputs the caller gave: one left out stays out (the hub's own default applies)."""
    return {name: value for name, value in given.items() if value is not None}


@dataclass(frozen=True)
class HubClient:
    """``url``: the hub server (``http://127.0.0.1:8791``); ``token_file``: its ``user.token``, read on each
    call (a hub that made a new one is followed without a restart)."""

    url: str
    token_file: str
    timeout: float = TIMEOUT_SECONDS

    def token(self) -> str:
        """The user token, read now; HubUnavailable without one (nothing is sent)."""
        try:
            with open(self.token_file, encoding="ascii") as file:
                token = file.read().strip()
        except (OSError, ValueError):
            raise HubUnavailable("the hub's user token could not be read") from None
        if not token:
            raise HubUnavailable("the hub's user token is empty")
        return token

    def _post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        token = self.token()
        request = urllib.request.Request(
            f"{self.url.rstrip('/')}/user/{op}", json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 — the operator's URL
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 400:  # the hub read the request and said no: its reason is safe to show
                try:
                    detail = json.loads(error.read() or b"{}").get("error")
                except (ValueError, AttributeError):
                    detail = None
                raise HubRefused(detail if isinstance(detail, str) else "the hub refused this") from None
            if error.code in (401, 403):
                raise HubMisconfigured(f"the hub answered HTTP {error.code}") from None
            raise HubUnavailable(f"the hub answered HTTP {error.code}") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
                raise HubUnavailable("the hub could not be reached") from None  # nothing reached it
            raise HubUnknown("the hub did not answer") from None
        except (OSError, ValueError):  # sent, and the answer was lost or unreadable: it may have been done
            raise HubUnknown("the hub's answer was lost") from None
        if not isinstance(reply, dict):
            raise HubUnknown("the hub's answer was not readable")
        return reply

    async def pending(self) -> list[dict[str, Any]]:
        reply = await asyncio.to_thread(self._post, "pending", {})
        items = reply.get("pending")
        return [view for view in map(request_view, (items if isinstance(items, list) else [])[:MAX_ITEMS])
                if view is not None]

    async def answer(self, request_id: int, answer: dict[str, Any]) -> None:
        """``answer``: exactly one of ``option_id`` (a permission), ``content`` (a question), ``refuse: True``."""
        await asyncio.to_thread(self._post, "answer", {"id": request_id, **answer})

    async def cancel(self, task_id: int) -> None:
        await asyncio.to_thread(self._post, "cancel", {"id": task_id})

    async def _call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post, op, body)

    async def projects(self) -> dict[str, Any]:
        return projects_view(await self._call("projects", {}))

    async def sessions(self, *, project: str | None = None, runner: str | None = None, closed: bool = False,
                       limit: int | None = None) -> list[dict[str, Any]]:
        reply = await self._call("sessions", _options(project=project, runner=runner, closed=closed or None,
                                                      limit=limit))
        return sessions_of(reply.get("sessions"))

    async def session(self, session_id: int) -> dict[str, Any]:
        found = session_detail(await self._call("session", {"id": session_id}))
        if found is None:
            raise HubUnknown("the hub's answer was not readable")
        return found

    async def events(self, session_id: int, after: int, limit: int | None = None) -> dict[str, Any]:
        """A page of the session's events after ``after`` (oldest first) and where it stands: ``next`` is the
        cursor for the next page, ``reset`` that ``after`` pointed into what was pruned."""
        reply = await self._call("events", _options(session=session_id, after=after, limit=limit))
        items = reply.get("events")
        page: dict[str, Any] = {"events": [view for view in map(event_view, (items if isinstance(items, list) else
                                                                            [])[:MAX_EVENTS]) if view is not None]}
        for name in ("first_seq", "last_seq", "next"):
            page[name] = reply.get(name) if isinstance(reply.get(name), int) else None
        page["more"], page["reset"] = reply.get("more") is True, reply.get("reset") is True
        return page

    async def _task(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        found = task_view((await self._call(op, body)).get("task"))
        if found is None:
            raise HubUnknown("the hub's answer was not readable")
        return found

    async def send(self, session_id: int, prompt: str, *, model: str | None = None, effort: str | None = None,
                   mode: str | None = None) -> dict[str, Any]:
        """A prompt for an open session; the task it became (queued behind the project's running one)."""
        return await self._task("send", {"session": session_id, "prompt": prompt,
                                         **_options(model=model, effort=effort, mode=mode)})

    async def new(self, project: str, prompt: str, *, agent: str | None = None, runner: str | None = None,
                  profile: str | None = None, model: str | None = None, effort: str | None = None,
                  mode: str | None = None) -> dict[str, Any]:
        """Another session of the project's lane, started with ``prompt``; the task (no session id yet)."""
        return await self._task("new", {"project": project, "prompt": prompt, **_options(
            agent=agent, runner=runner, profile=profile, model=model, effort=effort, mode=mode)})

    async def close(self, session_id: int) -> dict[str, Any]:
        reply = await self._call("close", {"session": session_id})
        cancelled = reply.get("cancelled")
        return {"closed": session_id, "cancelled": [task for task in (cancelled if isinstance(cancelled, list) else [])
                                                    if isinstance(task, int) and not isinstance(task, bool)]}

    async def preset(self, project: str, preset: str) -> dict[str, Any]:
        """The project's permission preset; an empty name clears it."""
        reply = await self._call("preset", {"project": project, "preset": preset})
        shown = reply.get("preset")
        return {"project": project, "preset": shown[:MAX_TEXT] if isinstance(shown, str) else None}
