"""HIMARI's side of the runner contract (``himari.runner.api``)."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Mapping, Sequence
from typing import Any

from himari.runner.api import NotSent, ProtocolError, RunnerError, RunnerUnavailable, recv_frame, send_frame

CONNECT_SECONDS = 5.0


class RunnerClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    async def call(self, op: str, args: Mapping[str, Any], fds: Sequence[int] = ()) -> tuple[dict[str, Any], list[int]]:
        """The result and the fds that came with it (the caller closes them).
        RunnerError: refused. NotSent: the runner was not reached. Any other
        RunnerUnavailable: the request was sent, its outcome is unknown.
        Cancelling closes the connection, which cancels a running command."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        sock.setblocking(False)
        received: list[int] = []
        try:
            try:
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, self.socket_path),
                                       CONNECT_SECONDS)
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
        except BaseException:
            for fd in received:
                os.close(fd)
            raise
        finally:
            sock.close()
