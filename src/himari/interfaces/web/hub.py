"""The hub's user side (V3-P95): the requests coding agents wait on, and the user's answers.

HIMARI hands project work to the hub (HIMARI v4), which runs coding agents
on the user's devices. An agent that wants to write a file, or has a
question, waits on the hub for the *user*: the hub keeps that apart from its
MCP tools (``/user/<op>`` with ``user.token``), so the model that handed the
work over cannot answer its own agent's request.

This is that path for the Web: the server holds the user token (a file only
the service account reads) and passes the signed-in operator's answers on.
The token never reaches the browser, the model or a Tool. What comes back is
rebuilt field by field; the text in it was written by an agent and is shown
as plain text only.

Standard library only: one short request per call, run off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

TIMEOUT_SECONDS = 10.0
MAX_TEXT = 4000  # of one shown text: a request's title, a question, a choice
MAX_ITEMS = 50  # requests, and the choices or fields of one


class HubUnavailable(Exception):
    """The hub did not answer, or its token could not be read; nothing was sent or done."""


class HubRefused(Exception):
    """The hub answered and refused (the request is no longer waiting, the choice is not one of its)."""


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


@dataclass(frozen=True)
class HubClient:
    """``url``: the hub server (``http://127.0.0.1:8791``); ``token_file``: its ``user.token``, read on each
    call (a hub that made a new one is followed without a restart)."""

    url: str
    token_file: str
    timeout: float = TIMEOUT_SECONDS

    def _post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            with open(self.token_file, encoding="ascii") as file:
                token = file.read().strip()
        except (OSError, ValueError):
            raise HubUnavailable("the hub's user token could not be read") from None
        if not token:
            raise HubUnavailable("the hub's user token is empty")
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
            raise HubUnavailable(f"the hub answered HTTP {error.code}") from None
        except (OSError, ValueError):
            raise HubUnavailable("the hub could not be reached") from None
        if not isinstance(reply, dict):
            raise HubUnavailable("the hub's answer was not readable")
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
