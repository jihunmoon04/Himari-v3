"""MCP over stdio (V3-P59 §5): one server's processes, each spoken to by lines.

HIMARI starts a stdio server itself, through a launcher Bootstrap gives it
(the server sandbox; V3-059: this package declares the shape it needs and
imports nothing of ``sandbox``). A launch that fails for any reason ran
nothing.

**Sessions.** A session is one process after a successful ``initialize``::

    STARTING → READY ⇄ CHECKING → CLOSING → CLOSED

Messages are one JSON object per line; answers are matched by id; a server's
notifications are ignored, its ``ping`` answered and any other request it
makes refused (HIMARI declares no capability that invites one). A line that
is not a JSON-RPC message, or one past the line bound, breaks the session:
it is closed.

**One ordered writer (§5.5).** Everything written to stdin — requests,
cancellations, pings, answers to the server's pings — goes through one
queue, written in order by one task. A request is admitted only when it is
put on the queue while the session is READY, in the same synchronous step
as that check; once admitted it is written in order whatever happens next.

**Cancelling is best effort (MCP; §5.5).** When a caller stops waiting
(Stop, or the call's timeout), ``notifications/cancelled`` and then a
``ping`` are queued and the session is CHECKING, with a deadline 10 s from
then. The cancelled request's answer is not awaited — a server following the
spec sends none — and is dropped if it comes. The session is READY again
when the latest ping is answered (an error answer counts: the server reads
and answers), and is closed if that answer has not come by the deadline. A
further cancellation queues its notification and a new ping without moving
the deadline. Calls that arrive meanwhile wait, not admitted.

**Closing.** stdin closed, then SIGTERM, then SIGKILL (the launcher's
``shutdown``). Every request written to the process and not yet answered is
then uncertain — the other callers' too.

**Certainty (§5.4).** Before a request is written: ``McpRefused`` (nothing
ran). After: ``McpUncertain`` unless the answer is one of the JSON-RPC errors
that mean the request was not carried out.

**Processes per server (§5.3).** ``shared``: one session for every
Conversation. ``per_conversation``: one per Conversation, at most ``max``;
the least recently used idle one makes room, and with none idle the call is
refused. A session unused for ``idle_seconds`` is closed. Failed starts in a
row are refused for 1, 2, 4 … 60 s before the next try. When a Conversation's
calls move to a new process, its first answer from it carries
``RESTART_NOTICE`` (the server's earlier identifiers may not hold).

**Listing.** At startup a separate session lists the Tools and reads the
views, then closes (``listing``); calls never use it.

**Which process (V3-P60 C6).** ``current_session`` names the process a
Conversation's next request goes to; ``request_in`` says which process
answered and, given one, sends only to it while it is still that
Conversation's and has not ended (else ``McpSessionChanged``, nothing sent).
A process that has ended is replaced at the next request, even before its
reader has noticed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Protocol

from himari.mcp.config import McpStdioConfig
from himari.mcp.jsonrpc import McpRefused, McpSessionChanged, McpUncertain, encode, notification, request, result_of
from himari.mcp.revision import REVISION

STARTUP_SECONDS = 15.0  # launch and initialize
CHECK_SECONDS = 10.0  # a check after a cancellation: the latest ping must be answered by then
SHUTDOWN_GRACE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
RESTART_NOTICE = ("This server was restarted: identifiers from its earlier answers may no longer apply; "
                  "check them before relying on them.")


class IsolatedProcess(Protocol):
    """The shape of a launched process (the sandbox's ``IsolatedProcess``)."""

    async def write_line(self, data: bytes) -> None: ...

    async def read_line(self) -> bytes: ...

    @property
    def returncode(self) -> int | None: ...

    def stderr_tail(self) -> bytes: ...

    async def shutdown(self, grace: float) -> None: ...


class ProcessLauncher(Protocol):
    """The shape of the sandbox's launcher for one server profile."""

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> IsolatedProcess: ...


class State(Enum):
    STARTING = "starting"
    READY = "ready"
    CHECKING = "checking"
    CLOSING = "closing"
    CLOSED = "closed"


class _WriteFailed(Exception):
    """A queued line was not written: the session's input broke or it closed."""


def _clock() -> float:
    return asyncio.get_running_loop().time()


class Session:
    def __init__(self, process: IsolatedProcess, generation: int) -> None:
        self.process = process
        self.generation = generation
        self.state = State.STARTING
        self.reason = ""
        self.last_used = _clock()
        self.settled = asyncio.Event()  # set while READY or CLOSED: a waiting call may go on
        self.initialize_result: Mapping[str, Any] = {}
        self._pending: dict[int, asyncio.Future[object]] = {}  # requests and pings awaiting an answer
        self._pings: set[int] = set()
        self._answered_at: dict[int, float] = {}  # ping id → when its answer was read
        self._latest_ping: asyncio.Future[object] | None = None
        self._latest_ping_id = 0
        self._ping_changed = asyncio.Event()
        self._deadline = 0.0
        self._next_id = 0
        self._queue: asyncio.Queue[tuple[bytes, asyncio.Future[None] | None]] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing: asyncio.Task[None] | None = None
        self._reader = asyncio.create_task(self._read())
        self._writer = asyncio.create_task(self._write_loop())

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def _set(self, state: State) -> None:
        self.state = state
        if state in (State.READY, State.CLOSED):
            self.settled.set()
        else:
            self.settled.clear()

    def _spawn(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the ordered writer -----------------------------------------------------

    def _enqueue(self, message: Mapping[str, Any], *, tracked: bool = False) -> asyncio.Future[None] | None:
        """Put one line on the queue (synchronously: nothing runs in between);
        with ``tracked``, a future that ends when it is written (or not)."""
        line = encode(message, compact=True)  # McpRefused: nothing is queued
        written: asyncio.Future[None] | None = None
        if tracked:
            written = asyncio.get_running_loop().create_future()
            written.add_done_callback(_settled)  # a waiter may be gone by then: nothing left unretrieved
        self._queue.put_nowait((line, written))
        return written

    async def _write_loop(self) -> None:
        while True:
            line, written = await self._queue.get()
            try:
                await self.process.write_line(line)
            except asyncio.CancelledError:
                if written is not None and not written.done():
                    written.set_exception(_WriteFailed())
                raise
            except Exception:
                if written is not None and not written.done():
                    written.set_exception(_WriteFailed())
                self._spawn(self.close("its input broke"))
                return
            if written is not None and not written.done():
                written.set_result(None)

    async def _send(self, message: Mapping[str, Any]) -> None:
        written = self._enqueue(message, tracked=True)
        assert written is not None
        try:
            await asyncio.shield(written)
        except _WriteFailed:
            raise McpRefused("the server's input broke") from None

    # --- requests -----------------------------------------------------------------

    async def start(self, capabilities: Mapping[str, Any]) -> Mapping[str, Any]:
        """``initialize`` and ``initialized``. Never cancelled (MCP forbids
        it): a failure or a timeout closes the session instead."""
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                result = await self._exchange(REVISION.initialize, REVISION.initialize_params(capabilities))
                REVISION.accept(result)
                await self._send(notification(REVISION.initialized))
        except (McpRefused, McpUncertain) as error:
            await self.close("it did not start")
            raise McpRefused(f"initialize: {error}") from None
        except Exception as error:
            await self.close("it did not start")
            reason = "no answer in time" if isinstance(error, TimeoutError) else type(error).__name__
            raise McpRefused(f"the server did not start ({reason})") from None
        except BaseException:
            await self.close("it did not start")
            raise
        self.initialize_result = result
        self._set(State.READY)
        return result

    async def _exchange(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id, future = self._register()
        await self._send(request(request_id, method, params))
        return result_of(await future, request_id, method)

    def _register(self) -> tuple[int, asyncio.Future[object]]:
        self._next_id += 1
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_settled)
        self._pending[self._next_id] = future
        return self._next_id, future

    async def request(self, method: str, params: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        # Admission (§5.5): the check and the queueing are one synchronous step.
        if self.state is not State.READY or self.process.returncode is not None:
            raise McpRefused(f"{method}: the server process is not ready")  # nothing queued
        request_id, future = self._register()
        try:
            written = self._enqueue(request(request_id, method, params), tracked=True)
        except McpRefused:
            self._pending.pop(request_id, None)  # nothing was queued: the session is not left waiting for it
            raise
        assert written is not None
        self.last_used = _clock()
        sent = False
        try:
            # One time limit for writing and waiting (PR #34 review B2): a
            # server that stops reading its stdin fills the pipe.
            async with asyncio.timeout(timeout):
                try:
                    await asyncio.shield(written)
                except _WriteFailed:
                    self._pending.pop(request_id, None)
                    raise McpUncertain(f"{method}: the request could not be fully written") from None
                sent = True
                message = await asyncio.shield(future)
        except TimeoutError:
            if sent:
                self._cancel(request_id, "timed out")
                raise McpUncertain(f"{method}: no answer in {timeout:g} s") from None
            # The pipe is stuck behind or inside this request: the stream cannot be trusted.
            self._pending.pop(request_id, None)
            self._spawn(self.close("it did not read its input in time"))
            raise McpUncertain(f"{method}: the request could not be written in {timeout:g} s; "
                               "the server may have read part of it") from None
        except asyncio.CancelledError:
            # Admitted: it is written in order whatever happens, so it is cancelled like one in flight.
            self._cancel(request_id, "cancelled by the user")
            raise
        finally:
            self.last_used = _clock()
        return result_of(message, request_id, method)

    # --- cancelling and checking (§5.5) -----------------------------------------------

    def _cancel(self, request_id: int, reason: str) -> None:
        """Best effort: the cancellation, then a ping, queued in this order and
        in one synchronous step; the session is CHECKING until the latest ping
        is answered, or closed at the check's deadline."""
        future = self._pending.pop(request_id, None)
        if future is None or self.state not in (State.READY, State.CHECKING):
            return
        if not future.done():
            future.cancel()
        self._enqueue(notification("notifications/cancelled", {"requestId": request_id, "reason": reason}))
        ping_id, ping = self._register()
        self._pings.add(ping_id)
        self._enqueue(request(ping_id, "ping", {}))
        self._latest_ping, self._latest_ping_id = ping, ping_id
        self._ping_changed.set()
        if self.state is State.READY:
            self._deadline = _clock() + CHECK_SECONDS  # not moved by later cancellations
            self._set(State.CHECKING)
            self._spawn(self._check())

    async def _check(self) -> None:
        while self.state is State.CHECKING:
            ping = self._latest_ping
            assert ping is not None
            remaining = self._deadline - _clock()
            if remaining <= 0:
                await self.close("it did not answer a ping in time")
                return
            self._ping_changed.clear()
            changed: asyncio.Future[Any] = asyncio.ensure_future(self._ping_changed.wait())
            awaited: set[asyncio.Future[Any]] = {ping, changed}
            try:
                await asyncio.wait(awaited, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
            if self.state is not State.CHECKING or ping is not self._latest_ping or not ping.done():
                continue
            # The final gate (PR #36 review B1): only an answer read by the
            # deadline counts, however late this task runs after it.
            answered = self._answered_at.get(self._latest_ping_id)
            if ping.cancelled() or answered is None or answered > self._deadline:
                await self.close("it did not answer a ping in time")
                return
            self._forget_pings()  # answered, result or error: it reads and answers
            self._set(State.READY)
            return

    def _forget_pings(self) -> None:
        for ping_id in self._pings:
            future = self._pending.pop(ping_id, None)
            if future is not None and not future.done():
                future.cancel()
        self._pings.clear()
        self._answered_at.clear()

    # --- reading ----------------------------------------------------------------------

    async def _read(self) -> None:
        try:
            while True:
                line = await self.process.read_line()
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    raise _Broken("it wrote a line that is not JSON") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise _Broken("it wrote a message that is not JSON-RPC")
                if "method" in message:
                    if "id" in message:
                        self._answer(message)
                    continue  # a notification: ignored
                request_id = message.get("id")
                if not isinstance(request_id, int) or isinstance(request_id, bool):
                    raise _Broken("it answered a request HIMARI did not send")
                if request_id in self._pings:
                    self._answered_at[request_id] = _clock()
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
                # an answer to nothing pending (a cancelled request's, a late ping's) is dropped: MCP says so
        except asyncio.CancelledError:
            raise
        except _Broken as broken:
            self._spawn(self.close(str(broken)))
        except Exception:
            self._spawn(self.close("the server process ended"))

    def _answer(self, message: Mapping[str, Any]) -> None:
        if message.get("method") == "ping":
            answer: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            answer = {"jsonrpc": "2.0", "id": message["id"],
                      "error": {"code": -32601, "message": "HIMARI does not offer this"}}
        self._enqueue(answer)  # in order with everything else; a failed write closes the session

    # --- closing ----------------------------------------------------------------------

    async def close(self, reason: str) -> None:
        if self._closing is None:
            self._set(State.CLOSING)  # at once: nothing is admitted from here on
            self._closing = asyncio.create_task(self._close(reason))
        await asyncio.shield(self._closing)

    async def _close(self, reason: str) -> None:
        self.reason = reason
        self._set(State.CLOSING)
        self._forget_pings()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpUncertain(f"the server process was ended ({reason}); the call may have run"))
        self._pending.clear()
        self._ping_changed.set()
        current = asyncio.current_task()
        for task in (self._reader, self._writer):
            if task is not current:
                task.cancel()
        while not self._queue.empty():  # lines never written: their waiters learn it
            _, written = self._queue.get_nowait()
            if written is not None and not written.done():
                written.set_exception(_WriteFailed())
        try:
            await self.process.shutdown(SHUTDOWN_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 — the launcher's own failure must not stop the close
            pass
        self._set(State.CLOSED)


def _settled(future: asyncio.Future[Any]) -> None:
    """Mark an outcome as seen: its caller may have stopped waiting."""
    if not future.cancelled():
        future.exception()


class _Broken(Exception):
    pass


class StdioServer:
    """One configured stdio server: its sessions, by Conversation or shared."""

    def __init__(self, source: str, config: McpStdioConfig, launcher: ProcessLauncher, *,
                 capabilities: Mapping[str, Any], timeout_seconds: float) -> None:
        self.source = source
        self._config = config
        self._launcher = launcher
        self._capabilities = dict(capabilities)
        self._timeout = timeout_seconds
        self._sessions: dict[str | None, Session] = {}
        self._starting: dict[str | None, asyncio.Task[Session]] = {}
        self._failures: dict[str | None, tuple[int, float]] = {}  # failed starts in a row, retry time
        self._generation = 0
        self._seen: dict[str, int] = {}  # Conversation → the generation of its last answer
        self._restarted: set[str] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._allocating = asyncio.Lock()
        self._closed = False

    # --- calls ----------------------------------------------------------------

    async def request(self, method: str, params: Mapping[str, Any], key: str | None) -> Mapping[str, Any]:
        return (await self.request_in(method, params, key))[1]

    def current_session(self, key: str | None) -> Session | None:
        """The process the key's next request would go to, if it has one
        that is not closing (V3-P60 C6)."""
        session = self._sessions.get(self._slot(key))
        if session is None or session.state not in (State.READY, State.CHECKING) or session.process.returncode is not None:
            return None
        return session

    async def request_in(self, method: str, params: Mapping[str, Any], key: str | None,
                         pinned: object | None = None) -> tuple[Session, Mapping[str, Any]]:
        """``request``, saying which process answered; with ``pinned``, only
        that process, while it is still the key's (else McpSessionChanged,
        nothing sent). One that ends mid-request is uncertain, as any call."""
        if pinned is None:
            session = await self._session(key)
        else:
            while True:
                if self._closed:
                    raise McpRefused("the server is shut down")
                current = self.current_session(key)
                if current is None or current is not pinned or current.process.returncode is not None:
                    raise McpSessionChanged(f"{method}: the process it was meant for was replaced")
                if current.state is State.READY:
                    session = current
                    break
                await current.settled.wait()  # CHECKING: not admitted while waiting
        if key is not None:  # the Conversation used this process: told at its next answer if it is replaced
            seen = self._seen.get(key)
            if seen is not None and seen != session.generation:
                self._restarted.add(key)
            self._seen[key] = session.generation
        return session, await session.request(method, params, self._timeout)

    def take_restart(self, key: str | None) -> bool:
        if key is None or key not in self._restarted:
            return False
        self._restarted.discard(key)
        return True

    async def release(self, key: str) -> None:
        """V3-P65 §6.3: the Conversation was moved to the trash: end its own
        process (``per_conversation`` only), freeing its place. If it is
        restored, its next call starts a new process and is told so
        (``RESTART_NOTICE``), as after any restart."""
        if self._config.scope != "per_conversation":
            return
        starting = self._starting.get(key)
        if starting is not None:
            try:
                await asyncio.shield(starting)
            except Exception:  # noqa: BLE001 — a start that failed has nothing to close
                pass
        self._failures.pop(key, None)
        session = self._sessions.get(key)
        if session is not None:
            await session.close("its Conversation was deleted")

    def _slot(self, key: str | None) -> str | None:
        return key if self._config.scope == "per_conversation" else None

    async def _session(self, key: str | None) -> Session:
        slot = self._slot(key)
        while True:
            if self._closed:
                raise McpRefused("the server is shut down")
            session = self._sessions.get(slot)
            if session is not None and session.state is not State.CLOSED:
                if session.process.returncode is not None:  # ended, before its reader noticed: replaced
                    await session.close("it ended")
                    continue
                if session.state is State.READY:
                    return session
                await session.settled.wait()  # CHECKING or CLOSING: not admitted while waiting
                continue
            async with self._allocating:  # one allocation at a time (PR #34 review B1)
                if self._closed:  # closed while this call waited for the lock (review B3)
                    raise McpRefused("the server is shut down")
                session = self._sessions.get(slot)
                if session is not None and session.state is not State.CLOSED:
                    continue  # started meanwhile
                task = self._starting.get(slot)
                if task is None:
                    self._check_backoff(slot)
                    await self._make_room(slot)
                    task = asyncio.create_task(self._start(slot))
                    self._starting[slot] = task  # holds its place before the lock is released
            return await asyncio.shield(task)  # a caller's Stop ends its wait, not the start

    def _check_backoff(self, slot: str | None) -> None:
        failed = self._failures.get(slot)
        if failed is not None and _clock() < failed[1]:
            raise McpRefused(f"the server failed to start; trying again in {failed[1] - _clock():.0f} s")

    async def _make_room(self, slot: str | None) -> None:
        """Under the allocation lock: until a place is free, close the least
        recently used idle session (marked CLOSING at once, so no request
        is admitted to it meanwhile); with none idle, refuse."""
        if self._config.scope != "per_conversation":
            return
        while True:
            live = [s for k, s in self._sessions.items() if k != slot and s.state is not State.CLOSED]
            if len(live) + len(self._starting) < self._config.max_processes:
                return
            idle = sorted((s for s in live if s.state is State.READY and not s.busy), key=lambda s: s.last_used)
            if not idle:
                raise McpRefused("this server has no free session; try again")
            await idle[0].close("its place went to another Conversation")

    async def _start(self, slot: str | None) -> Session:
        try:
            session = await self._launch()
            if self._closed:  # aclose began during the launch: this process must not outlive it
                await session.close("HIMARI is shutting down")
                raise McpRefused("the server is shut down")
            self._failures.pop(slot, None)
            self._sessions[slot] = session
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap_idle())
            return session
        except McpRefused:
            count = self._failures.get(slot, (0, 0.0))[0] + 1
            self._failures[slot] = (count, _clock() + min(BACKOFF_MAX_SECONDS, 2.0 ** (count - 1)))
            raise
        finally:
            self._starting.pop(slot, None)

    async def _launch(self) -> Session:
        self._generation += 1
        config = self._config
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                process = await self._launcher.launch([config.command, *config.args], dict(config.env), config.cwd)
        except Exception as error:  # noqa: BLE001 — whatever the sandbox refused, nothing ran
            raise McpRefused(f"the server could not be started: {error or type(error).__name__}") from None
        session = Session(process, self._generation)
        await session.start(self._capabilities)
        return session

    async def listing(self) -> Session:
        """A session of its own for the startup listing (closed by the caller)."""
        return await self._launch()

    async def _reap_idle(self) -> None:
        idle = self._config.idle_seconds
        while not self._closed:
            await asyncio.sleep(min(60.0, idle / 4))
            for session in list(self._sessions.values()):
                if session.state is State.READY and not session.busy and _clock() - session.last_used >= idle:
                    await session.close("it was idle")

    async def aclose(self) -> None:
        """Under the allocation lock, so no start is registered after this
        (PR #34 review B3): every start already registered is waited for,
        then every session is closed."""
        async with self._allocating:
            self._closed = True
            starting = list(self._starting.values())
        if self._reaper is not None:
            self._reaper.cancel()
        for task in starting:
            try:
                await asyncio.shield(task)
            except Exception:  # noqa: BLE001 — a start that failed has nothing to close
                pass
        await asyncio.gather(*(s.close("HIMARI is shutting down") for s in self._sessions.values()))


class StdioChannel:
    """The client's channel for calls: sessions chosen by Conversation."""

    def __init__(self, server: StdioServer) -> None:
        self._server = server

    async def start(self) -> Mapping[str, Any]:
        raise McpRefused("a stdio server is listed through its own session")

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]:
        return await self._server.request(method, params, key)

    async def request_in(self, method: str, params: Mapping[str, Any], *, key: str | None = None,
                         session: object | None = None) -> tuple[object, Mapping[str, Any]]:
        return await self._server.request_in(method, params, key, session)

    def current_session(self, key: str | None) -> object | None:
        return self._server.current_session(key)

    def take_restart(self, key: str | None) -> bool:
        return self._server.take_restart(key)

    async def release(self, key: str) -> None:
        await self._server.release(key)

    async def aclose(self) -> None:
        await self._server.aclose()


class ListingChannel:
    """The client's channel for the startup listing: one session, then closed."""

    def __init__(self, server: StdioServer, timeout_seconds: float) -> None:
        self._server = server
        self._timeout = timeout_seconds
        self._session: Session | None = None

    async def start(self) -> Mapping[str, Any]:
        self._session = await self._server.listing()
        return self._session.initialize_result

    async def request(self, method: str, params: Mapping[str, Any], *, key: str | None = None) -> Mapping[str, Any]:
        if self._session is None:
            await self.start()
        assert self._session is not None
        return await self._session.request(method, params, self._timeout)

    async def request_in(self, method: str, params: Mapping[str, Any], *, key: str | None = None,
                         session: object | None = None) -> tuple[object, Mapping[str, Any]]:
        if session is not None and session is not self._session:
            raise McpSessionChanged(f"{method}: the listing session it was meant for is gone")
        return self._session, await self.request(method, params, key=key)

    def current_session(self, key: str | None) -> object | None:
        return self._session

    def take_restart(self, key: str | None) -> bool:
        return False

    async def release(self, key: str) -> None:
        pass  # the listing session is no Conversation's

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close("the listing is done")
