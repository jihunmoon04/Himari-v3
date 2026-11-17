"""The hub's live streams, relayed to the Web (V3-P102 §6).

The hub serves Server-Sent Events at ``GET /user/stream`` (user-api-v1 §5):
the session list (``sessions`` whole, then ``changed``) or one session's
events (``state``, ``event`` with its ``seq`` as the id, ``reset``). The
browser never reaches the hub; this server reads those streams with the
user token and passes them on, rebuilt field by field (``hub``'s views).

- **The list is shared.** One upstream for the whole server, whatever the
  number of browsers. The server keeps the whole list: ``sessions``
  replaces it, ``changed`` merges by id. A browser that joins gets that
  whole list first, then the changes; when the upstream comes back after a
  break its new ``sessions`` replaces the list here and in every browser.
  With no browser left the upstream is closed after ``LINGER_SECONDS``.
- **A session's events are not shared.** Each browser has its own cursor
  (``after``, ``Last-Event-ID``), so each gets its own upstream, opened
  with that cursor, ended when the browser leaves. At most
  ``SESSION_STREAMS`` at once: the hub's streams are 32 for everyone.
- **When the hub has no stream left** (503 ``streams``): a session stream
  is refused (``HubBusy``) and the page reads ``events`` instead; the list
  is read with ``sessions`` every ``POLL_SECONDS`` and sent as
  ``sessions``, while the stream is tried again with backoff. An upstream
  that is replaced is closed before the new one opens.
- **Backpressure.** Each browser of the list has a bounded queue
  (``QUEUE_FRAMES`` or ``QUEUE_BYTES``); one that falls behind is closed
  (it comes back and gets the whole list), so it never holds the shared
  reader. A session stream reads its upstream only as fast as its browser
  takes it (a small queue in between).
- **Sign-in.** Each browser stream checks its own sign-in every
  ``CHECK_SECONDS`` on its own timer, whether events flow or not, and ends
  when it no longer holds (signed out, expired). A comment line goes to the
  browser on the same timer when nothing else did.
- **Projects (V3-P103 §4.2).** ``project_request`` (one request, rebuilt) and
  ``projects`` (read the projects again) go to every browser as they come;
  nothing of them is kept here. Events may be lost while the upstream is
  away, so ``projects`` follows every new ``sessions`` from the hub, and
  while the list is read by asking it goes out with the first read and every
  ``PROJECTS_POLL_SECONDS``.
- **Timeouts.** An upstream that sends nothing for ``READ_SECONDS`` (the
  hub pings every 15 s) is treated as gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx

from himari.interfaces.web.hub import (
    HubBusy,
    HubClient,
    HubMisconfigured,
    HubNotFound,
    HubRefused,
    HubUnavailable,
    event_view,
    project_request_view,
    session_view,
    sessions_of,
)
from himari.interfaces.web.live import frame

log = logging.getLogger(__name__)

READ_SECONDS = 30.0  # twice the hub's ping
CONNECT_SECONDS = 10.0
CHECK_SECONDS = 15.0
LINGER_SECONDS = 30.0
POLL_SECONDS = 10.0
PROJECTS_POLL_SECONDS = 30.0  # V3-P103 §4.2: while polling, how often the page is told to read projects again
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
SESSION_STREAMS = 16
QUEUE_FRAMES = 256
QUEUE_BYTES = 4 * 1024 * 1024
SESSION_QUEUE = 64  # between a session's upstream and its browser
RETRY_MS = 1000
KEEP_ALIVE = ": keep-alive\n\n"


class HubStreamLimit(Exception):
    """This server already has ``SESSION_STREAMS`` session streams open."""


@dataclass(frozen=True)
class Message:
    """One Server-Sent Event as the hub sent it."""

    event: str
    data: str
    id: str | None = None


async def messages(lines: AsyncIterator[str]) -> AsyncIterator[Message]:
    """The events of a ``text/event-stream`` (comments, ``retry`` and unknown fields are dropped)."""
    event, last_id = "message", None
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                yield Message(event, "\n".join(data), last_id)
            event, data, last_id = "message", [], None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
        elif name == "id":
            last_id = value


def _json(message: Message) -> dict[str, Any] | None:
    try:
        data = json.loads(message.data)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def event_frame(seq: int, data: dict[str, Any]) -> str:
    return f"id: {seq}\n{frame('event', data)}"


class Upstream:
    """Opens the hub's stream with the user token, mapping a refusal before any event to the client's errors."""

    def __init__(self, client: HubClient) -> None:
        self._client = client
        self._http: httpx.AsyncClient | None = None

    def _session(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(READ_SECONDS, connect=CONNECT_SECONDS),
                                           follow_redirects=False, trust_env=False)
        return self._http

    async def open(self, params: dict[str, str], last_event_id: int | None = None
                   ) -> tuple[AsyncExitStack, AsyncIterator[Message]]:
        token = self._client.token()  # HubUnavailable: nothing is sent
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(self._session().stream(
                "GET", f"{self._client.url.rstrip('/')}/user/stream", params=params, headers=headers))
            if response.status_code != 200:
                raise _refusal(response.status_code, await response.aread())
        except BaseException as error:
            await stack.aclose()
            if isinstance(error, httpx.HTTPError):
                raise HubUnavailable("the hub could not be reached") from None
            raise
        return stack, messages(response.aiter_lines())

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _refusal(status: int, body: bytes) -> Exception:
    try:
        detail = json.loads(body or b"{}").get("error")
    except (ValueError, AttributeError):
        detail = None
    reason = detail if isinstance(detail, str) else ""
    if status == 400:
        return HubRefused(reason or "the hub refused this")
    if status == 404:
        return HubNotFound(reason or "session")
    if status in (401, 403):
        return HubMisconfigured(f"the hub answered HTTP {status}")
    if status == 503 and reason == "streams":
        return HubBusy("the hub has no stream left")
    return HubUnavailable(f"the hub answered HTTP {status}")


async def _next(source: asyncio.Queue[Any] | asyncio.Event, closing: asyncio.Event, timeout: float) -> tuple[bool, Any]:
    """(True, item) when ``source`` gave one within ``timeout``, (False, None) on a timeout or when closing."""
    waiting: asyncio.Future[Any] = asyncio.ensure_future(
        source.get() if isinstance(source, asyncio.Queue) else source.wait())
    stop = asyncio.ensure_future(closing.wait())
    try:
        await asyncio.wait((waiting, stop), timeout=max(0.0, timeout), return_when=asyncio.FIRST_COMPLETED)
    finally:  # also when the caller is cancelled (the browser left): neither helper outlives this call
        stop.cancel()
        if not waiting.done():
            waiting.cancel()  # a queue's get gives its item back when cancelled
    if waiting.done() and not waiting.cancelled():
        return True, waiting.result()
    return False, None


# -- the shared list ------------------------------------------------------------------------------

@dataclass(eq=False)
class ListReader:
    """One browser's place in the shared list: frames waiting for it, bounded."""

    frames: deque[str] = field(default_factory=deque)
    size: int = 0
    behind: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, text: str) -> None:
        if self.behind:
            return
        size = len(text.encode("utf-8"))  # what it costs on the wire, not its characters
        if len(self.frames) >= QUEUE_FRAMES or self.size + size > QUEUE_BYTES:
            self.behind = True  # this browser fell behind: it is closed, and comes back to the whole list
            self.frames.clear()
        else:
            self.frames.append(text)
            self.size += size
        self.ready.set()

    def take(self) -> list[str]:
        taken = list(self.frames)
        self.frames.clear()
        self.size = 0
        self.ready.clear()
        return taken


class ListRelay:
    """The hub's session list for every browser of this server, from one upstream."""

    def __init__(self, client: HubClient, upstream: Upstream, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client, self._upstream, self._clock = client, upstream, clock
        self._readers: set[ListReader] = set()
        self._sessions: dict[int, dict[str, Any]] | None = None  # None: not read yet
        self._reachable = True
        self._task: asyncio.Task[None] | None = None
        self._idle_since: float | None = None
        self._projects_told: float | None = None  # while polling: when ``projects`` last went out

    @property
    def readers(self) -> int:
        return len(self._readers)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _whole(self) -> str:
        assert self._sessions is not None
        return frame("sessions", {"sessions": [self._sessions[key] for key in sorted(self._sessions, reverse=True)]})

    def join(self) -> ListReader:
        reader = ListReader()
        if self._sessions is not None:
            reader.offer(self._whole())
        if not self._reachable:
            reader.offer(frame("hub", {"reachable": False}))
        self._readers.add(reader)
        self._idle_since = None
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="hub-list-relay")
        return reader

    def leave(self, reader: ListReader) -> None:
        self._readers.discard(reader)
        if not self._readers:
            self._idle_since = self._clock()

    def _send(self, text: str) -> None:
        for reader in list(self._readers):
            reader.offer(text)

    def _replace(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = {item["id"]: item for item in sessions}
        self._mark(True)
        self._send(self._whole())

    def _mark(self, reachable: bool) -> None:
        if reachable != self._reachable:
            self._reachable = reachable
            self._send(frame("hub", {"reachable": reachable}))

    def _idle(self) -> bool:
        return (not self._readers and self._idle_since is not None
                and self._clock() - self._idle_since >= LINGER_SECONDS)

    async def _run(self) -> None:
        attempt = 0
        try:
            while not self._idle():
                try:
                    stack, stream = await self._upstream.open({})
                except HubBusy:
                    await self._poll(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)] * 3)
                    attempt += 1
                    continue
                except (HubUnavailable, HubRefused, HubNotFound) as error:
                    log.warning("hub list stream: %s", type(error).__name__)  # never the hub's words (V3-P103 §4)
                    self._mark(False)
                    await self._pause(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                    attempt += 1
                    continue
                async with stack:
                    attempt = 0
                    await self._read(stream)
                self._mark(False)  # the hub ended it, or went quiet
                await self._pause(BACKOFF_SECONDS[0])
        finally:
            self._task = None

    async def _read(self, stream: AsyncIterator[Message]) -> None:
        reading = asyncio.ensure_future(self._consume(stream))
        try:
            while not reading.done():
                await asyncio.wait((reading,), timeout=LINGER_SECONDS)
                if self._idle():
                    return
            reading.result()
        except (httpx.HTTPError, HubUnavailable):
            pass
        finally:
            reading.cancel()

    async def _consume(self, stream: AsyncIterator[Message]) -> None:
        async for message in stream:
            data = _json(message)
            if data is None:
                continue
            if message.event == "sessions":  # a new upstream: what happened while it was away may be lost
                self._replace(sessions_of(data.get("sessions"), limit=100_000))
                self._send(frame("projects", {}))
                self._projects_told = None  # polling again later tells it at once
            elif message.event == "project_request":
                shown = project_request_view(data)
                if shown is not None:
                    self._send(frame("project_request", shown))
            elif message.event == "projects":
                self._send(frame("projects", {}))
            elif message.event == "changed" and self._sessions is not None:
                changed = sessions_of(data.get("sessions"), limit=100_000)
                for item in changed:
                    self._sessions[item["id"]] = item
                if changed:
                    self._send(frame("changed", {"sessions": changed}))

    async def _poll(self, seconds: float) -> None:
        """The hub has no stream left: the list by asking, until it is tried again."""
        deadline = self._clock() + seconds
        while not self._idle():
            try:
                self._replace(await self._client.sessions(limit=500))
            except (HubUnavailable, HubRefused):
                self._mark(False)
            else:
                now = self._clock()
                if self._projects_told is None or now - self._projects_told >= PROJECTS_POLL_SECONDS:
                    self._projects_told = now  # nothing else tells of projects, devices or requests now
                    self._send(frame("projects", {}))
            if self._clock() >= deadline:
                return
            await self._pause(POLL_SECONDS)

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def stop(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def list_frames(relay: ListRelay, reader: ListReader, signed_in: Callable[[], bool], closing: asyncio.Event,
                      *, check: float = CHECK_SECONDS) -> AsyncIterator[str]:
    """One browser's list stream: ``sessions`` (replace), ``changed`` (merge by id), ``hub`` (reachable or not),
    ``project_request`` and ``projects`` (V3-P103 §4.2)."""
    try:
        yield f"retry: {RETRY_MS}\n\n"
        next_check = time.monotonic() + check
        while not closing.is_set():
            if reader.behind:
                return
            got, _ = await _next(reader.ready, closing, next_check - time.monotonic())
            if got:
                if time.monotonic() >= next_check:  # changes that never stop do not put the check off
                    if not signed_in():
                        return
                    next_check = time.monotonic() + check
                for text in reader.take():
                    yield text
                if reader.behind:
                    return
                continue
            if closing.is_set() or not signed_in():
                return
            yield KEEP_ALIVE
            next_check = time.monotonic() + check
    finally:
        relay.leave(reader)


# -- one session ----------------------------------------------------------------------------------

class SessionStreams:
    """Each browser's own upstream for one session, at most ``limit`` at once."""

    def __init__(self, upstream: Upstream, *, limit: int = SESSION_STREAMS) -> None:
        self._upstream, self._limit = upstream, limit
        self.open_now = 0

    async def open(self, session_id: int, after: int, last_event_id: int | None
                   ) -> tuple[AsyncExitStack, AsyncIterator[Message]]:
        """Before the browser's response starts, so a refusal is an HTTP error: HubStreamLimit, HubBusy,
        HubNotFound, HubRefused (a bad cursor), HubUnavailable. The caller gives the slot back with ``release``."""
        if self.open_now >= self._limit:
            raise HubStreamLimit
        self.open_now += 1
        try:
            return await self._upstream.open({"session": str(session_id), "after": str(after)}, last_event_id)
        except BaseException:
            self.open_now -= 1
            raise

    def release(self) -> None:
        self.open_now = max(0, self.open_now - 1)


async def session_frames(stack: AsyncExitStack, stream: AsyncIterator[Message], signed_in: Callable[[], bool],
                         closing: asyncio.Event, *, check: float = CHECK_SECONDS) -> AsyncIterator[str]:
    """One browser's session stream: ``state`` (first, and when the session's listing changes; ``reset`` true:
    start again from ``first_seq``), ``event`` (id = ``seq``), ``reset``, and ``hub_unavailable`` when the hub
    ends it or goes quiet (the browser reconnects with its last ``seq``)."""
    queue: asyncio.Queue[Message | None] = asyncio.Queue(SESSION_QUEUE)

    async def pump() -> None:
        try:
            async for message in stream:
                await queue.put(message)
        except (httpx.HTTPError, HubUnavailable):
            pass
        await queue.put(None)  # the hub ended it; not when cancelled (the browser left, nobody reads the queue)

    reading = asyncio.ensure_future(pump())
    try:
        if not signed_in():  # the response may start well after the route checked
            return
        yield f"retry: {RETRY_MS}\n\n"
        next_check = time.monotonic() + check
        while not closing.is_set():
            got, message = await _next(queue, closing, next_check - time.monotonic())
            if not got:
                if closing.is_set() or not signed_in():
                    return
                yield KEEP_ALIVE
                next_check = time.monotonic() + check
                continue
            if message is None:
                yield frame("hub_unavailable", {})
                return
            if time.monotonic() >= next_check:
                if not signed_in():
                    return
                next_check = time.monotonic() + check
            text = _session_frame(message)
            if text is not None:
                yield text
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)  # gone before the upstream closes under it
        await stack.aclose()


def _session_frame(message: Message) -> str | None:
    data = _json(message)
    if data is None:
        return None
    if message.event == "event":
        shown = event_view(data)
        return None if shown is None else event_frame(shown["seq"], shown)
    if message.event in ("state", "reset"):
        out: dict[str, Any] = {name: data[name] if isinstance(data.get(name), int) else None
                               for name in ("first_seq", "last_seq")}
        if message.event == "state":
            out["reset"] = data.get("reset") is True
            out["session"] = session_view(data.get("session"))
        return frame(message.event, out)
    return None
