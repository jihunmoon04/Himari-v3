"""HIMARI's side of the runner contract (``himari.runner.api``)."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Mapping, Sequence
from typing import Any

from himari.runner.api import (
    WRITES,
    NotSent,
    ProtocolError,
    RunnerError,
    RunnerUnavailable,
    recv_frame,
    send_frame,
)

CONNECT_SECONDS = 5.0


class RunnerClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    async def call(self, op: str, args: Mapping[str, Any], fds: Sequence[int] = ()) -> tuple[dict[str, Any], list[int]]:
        """The result and the fds that came with it (the caller closes them).
        RunnerError: refused. NotSent: the runner was not reached. Any other
        RunnerUnavailable: the request was sent, its outcome is unknown.
        Cancelling closes the sending side, which cancels a running command;
        a cancelled write returns only once the runner has closed the
        connection — its work has ended — so the caller's path locks outlive
        it (PR #87 review C1)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        sock.setblocking(False)
        received: list[int] = []
        connected = False
        try:
            try:
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, self.socket_path),
                                       CONNECT_SECONDS)
                connected = True
                await send_frame(sock, {"op": op, "args": dict(args)}, fds)
            except (OSError, TimeoutError) as error:
                raise NotSent(f"the machine runner is not reachable ({type(error).__name__})") from None
            try:
                answer, received = await recv_frame(sock)
            except (OSError, ProtocolError, ValueError) as error:
                raise RunnerUnavailable(f"the machine runner did not answer ({type(error).__name__})") from None
            if answer.get("ok") is True and isinstance(answer.get("result"), dict):
                return answer["result"], received
            for fd in received:
                os.close(fd)
            received = []
            failure = answer.get("error")
            if isinstance(failure, dict):
                raise RunnerError(str(failure.get("category", "other")), str(failure.get("message", "")) or None)
            raise RunnerUnavailable("a malformed answer")
        except BaseException as error:
            for fd in received:
                os.close(fd)
            if isinstance(error, asyncio.CancelledError) and connected and op in WRITES:
                await _settle(sock)
            raise
        finally:
            sock.close()


async def _settle(sock: socket.socket) -> None:
    """Tell the runner to stop, then wait — through further cancellations —
    until it closes the connection: it does so only once the work has ended
    (a command killed and reaped, a file operation returned), or when it
    exits, which ends the work too."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    closed = asyncio.ensure_future(_closed(sock))
    while not closed.done():
        try:
            await asyncio.wait([closed])  # never cancels ``closed`` itself
        except asyncio.CancelledError:
            pass


async def _closed(sock: socket.socket) -> None:
    """Read to the end, closing any descriptors an answer brought."""
    while True:
        try:
            _, fds = await recv_frame(sock)
        except (OSError, ProtocolError, ValueError, RunnerUnavailable):
            return  # the end (or a broken connection): the runner is done with it
        for fd in fds:
            os.close(fd)
