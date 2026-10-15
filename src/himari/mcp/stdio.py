"""MCP over stdio (V3-P59 §5): one server's processes, each spoken to by lines.

HIMARI starts a stdio server itself, through a launcher Bootstrap gives it
(the server sandbox; V3-059: this package declares the shape it needs and
imports nothing of ``sandbox``). A launch that fails for any reason ran
nothing.

**Sessions.** A session is one process after a successful ``initialize``::

    STARTING → READY ⇄ DRAINING → CLOSING → CLOSED

New requests are written only while READY. Messages are one JSON object per
line; answers are matched by id; a server's notifications are ignored, its
``ping`` answered and any other request it makes refused (HIMARI declares no
capability that invites one). A line that is not a JSON-RPC message, or one
past the line bound, breaks the session: it is closed.

**Cancelling (§5.5).** When a caller stops waiting (Stop, or the call's
timeout), ``notifications/cancelled`` is sent and the request id goes on the
session's cancelled list with a deadline 10 s away; the session is DRAINING.
Answers to cancelled requests are dropped. The session returns to READY only
when every cancelled request has been answered, and is closed as soon as one
passes its deadline. Calls that arrive meanwhile wait (unwritten).

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
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Protocol

from himari.mcp.config import McpStdioConfig
from himari.mcp.jsonrpc import McpRefused, McpUncertain, notification, request, result_of
from himari.mcp.revision import REVISION

STARTUP_SECONDS = 15.0  # launch and initialize
CANCEL_GRACE_SECONDS = 10.0
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
    DRAINING = "draining"
    CLOSING = "closing"
    CLOSED = "closed"


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
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._cancelled: dict[int, float] = {}  # request id → deadline
        self._drained = asyncio.Event()
        self._next_id = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing: asyncio.Task[None] | None = None
        self._reader = asyncio.create_task(self._read())

    @property
    def busy(self) -> bool:
        return bool(self._pending or self._cancelled)

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

    async def start(self, capabilities: Mapping[str, Any]) -> Mapping[str, Any]:
        """``initialize`` and ``initialized``. Never cancelled (MCP forbids
        it): a failure or a timeout closes the session instead."""
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                result = await self._exchange(REVISION.initialize, REVISION.initialize_params(capabilities))
                REVISION.accept(result)
                await self._write(notification(REVISION.initialized))
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
        await self._write(request(request_id, method, params))
        return result_of(await future, request_id, method)

    def _register(self) -> tuple[int, asyncio.Future[object]]:
        self._next_id += 1
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        return self._next_id, future

    async def _write(self, message: Mapping[str, Any]) -> None:
        await self.process.write_line(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())

    async def request(self, method: str, params: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        if self.state is not State.READY or self.process.returncode is not None:
            raise McpRefused(f"{method}: the server process is not ready")  # nothing written
        self.last_used = _clock()
        request_id, future = self._register()
        try:
            await self._write(request(request_id, method, params))
        except Exception:
            self._pending.pop(request_id, None)
            self._spawn(self.close("its input broke"))
            raise McpUncertain(f"{method}: the request could not be fully written") from None
        except asyncio.CancelledError:
            self._cancel(request_id, "cancelled by the user")
            raise
        try:
            async with asyncio.timeout(timeout):
                message = await asyncio.shield(future)
        except TimeoutError:
            self._cancel(request_id, "timed out")
            raise McpUncertain(f"{method}: no answer in {timeout:g} s") from None
        except asyncio.CancelledError:
            self._cancel(request_id, "cancelled by the user")
            raise
        finally:
            self.last_used = _clock()
        return result_of(message, request_id, method)

    def _cancel(self, request_id: int, reason: str) -> None:
        future = self._pending.pop(request_id, None)
        if future is None or self.state not in (State.READY, State.DRAINING):
            return
        if not future.done():
            future.cancel()
        self._cancelled[request_id] = _clock() + CANCEL_GRACE_SECONDS
        self._spawn(self._notify_cancelled(request_id, reason))
        if self.state is State.READY:
            self._set(State.DRAINING)
            self._spawn(self._drain())
        self._drained.set()  # the watcher recomputes its deadline

    async def _notify_cancelled(self, request_id: int, reason: str) -> None:
        try:
            await self._write(notification("notifications/cancelled", {"requestId": request_id, "reason": reason}))
        except Exception:
            await self.close("its input broke")

    async def _drain(self) -> None:
        """READY again once every cancelled request is answered; closed as
        soon as one passes its deadline."""
        while self.state is State.DRAINING:
            if not self._cancelled:
                self._set(State.READY)
                return
            wait = min(self._cancelled.values()) - _clock()
            if wait <= 0:
                await self.close("a cancelled request was not answered in time")
                return
            self._drained.clear()
            try:
                await asyncio.wait_for(self._drained.wait(), wait)
            except TimeoutError:
                pass

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
                        self._spawn(self._answer(message))
                    continue  # a notification: ignored
                request_id = message.get("id")
                if not isinstance(request_id, int) or isinstance(request_id, bool):
                    raise _Broken("it answered a request HIMARI did not send")
                if self._cancelled.pop(request_id, None) is not None:
                    self._drained.set()
                    continue  # the caller is gone; MCP says to ignore it
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except _Broken as broken:
            self._spawn(self.close(str(broken)))
        except Exception:
            self._spawn(self.close("the server process ended"))

    async def _answer(self, message: Mapping[str, Any]) -> None:
        if message.get("method") == "ping":
            answer: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            answer = {"jsonrpc": "2.0", "id": message["id"],
                      "error": {"code": -32601, "message": "HIMARI does not offer this"}}
        try:
            await self._write(answer)
        except Exception:
            await self.close("its input broke")

    async def close(self, reason: str) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(reason))
        await asyncio.shield(self._closing)

    async def _close(self, reason: str) -> None:
        self.reason = reason
        self._set(State.CLOSING)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpUncertain(f"the server process was ended ({reason}); the call may have run"))
        self._pending.clear()
        self._cancelled.clear()
        self._drained.set()
        if self._reader is not asyncio.current_task():
            self._reader.cancel()
        try:
            await self.process.shutdown(SHUTDOWN_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 — the launcher's own failure must not stop the close
            pass
        self._set(State.CLOSED)


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
        self._closed = False

    # --- calls ----------------------------------------------------------------

    async def request(self, method: str, params: Mapping[str, Any], key: str | None) -> Mapping[str, Any]:
        session = await self._session(key)
        if key is not None:  # the Conversation used this process: told at its next answer if it is replaced
            seen = self._seen.get(key)
            if seen is not None and seen != session.generation:
                self._restarted.add(key)
            self._seen[key] = session.generation
        return await session.request(method, params, self._timeout)

    def take_restart(self, key: str | None) -> bool:
        if key is None or key not in self._restarted:
            return False
        self._restarted.discard(key)
        return True

    def _slot(self, key: str | None) -> str | None:
        return key if self._config.scope == "per_conversation" else None

    async def _session(self, key: str | None) -> Session:
        slot = self._slot(key)
        while True:
            if self._closed:
                raise McpRefused("the server is shut down")
            session = self._sessions.get(slot)
            if session is not None and session.state is not State.CLOSED:
                if session.state is State.READY:
                    return session
                await session.settled.wait()  # DRAINING or CLOSING: nothing written while waiting
                continue
            task = self._starting.get(slot)
            if task is None:
                self._check_backoff(slot)
                await self._make_room(slot)
                task = asyncio.create_task(self._start(slot))
                self._starting[slot] = task
            return await asyncio.shield(task)  # a caller's Stop ends its wait, not the start

    def _check_backoff(self, slot: str | None) -> None:
        failed = self._failures.get(slot)
        if failed is not None and _clock() < failed[1]:
            raise McpRefused(f"the server failed to start; trying again in {failed[1] - _clock():.0f} s")

    async def _make_room(self, slot: str | None) -> None:
        if self._config.scope != "per_conversation":
            return
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
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
        for task in list(self._starting.values()):
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

    def take_restart(self, key: str | None) -> bool:
        return self._server.take_restart(key)

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

    def take_restart(self, key: str | None) -> bool:
        return False

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close("the listing is done")
