"""The screen requests (V3-P91 §4): how the screen Tool gets a device's screen.

``ScreenRequests`` answers the Tool's ``ScreenSource``: it checks what Presence
knows of the device (connected, awake, its settings), sends a
``screen_request`` event to that device's streams only, and waits
SCREEN_WAIT_SECONDS for the app to post the image (``deliver``) or say why not
(``refuse``). A request lives in this process only; a cancelled Tool call ends
it, and an answer that comes after is refused as ended. An image is taken only
if the device would still be asked now (awake, its settings on): one that comes
after the user turned viewing off or locked the computer is not given to the
model.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from himari.common import PrincipalId, TrustedScope
from himari.presence.hub import PresenceHub
from himari.tool.api import ScreenRefusal

SCREEN_WAIT_SECONDS = 15.0  # §4.3: the app takes one to three seconds
ENDED_KEEP_SECONDS = 600.0  # §4.2: how long an ended request still answers 410, not 404
APP_REFUSALS = {  # what the app may say (§4.2) → what the Tool tells the model
    "capture_failed": ScreenRefusal.CAPTURE_FAILED,
    "excluded": ScreenRefusal.CAPTURE_FAILED,  # an excluded program in front (§14 Q10): said as a failed capture
    "turned_off": ScreenRefusal.TURNED_OFF,
    "asleep": ScreenRefusal.AWAY,
}


class Answer(Enum):
    TAKEN = "taken"
    UNKNOWN = "unknown"  # no such request for this device: never was, another's, or long gone
    ENDED = "ended"  # it was answered, or ended (timed out, cancelled) a moment ago


@dataclass
class _Request:
    principal: PrincipalId
    device_id: str
    free_wake: bool
    future: asyncio.Future[bytes | ScreenRefusal]


class ScreenRequests:
    def __init__(self, hub: PresenceHub, *, wait_seconds: float = SCREEN_WAIT_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hub = hub
        self._wait = wait_seconds
        self._clock = clock
        self._pending: dict[str, _Request] = {}
        self._ended: dict[str, tuple[PrincipalId, str, float]] = {}  # request → whose, and until when
        # §6: the job a request belongs to, so the app moves its character on that screen; set once
        # the Runner exists (it is made after the Tools)
        self.job_of: Callable[[TrustedScope], str | None] = lambda scope: None

    def _refusal(self, principal: PrincipalId, device_id: str, free_wake: bool) -> ScreenRefusal | None:
        """Why the device may not be asked for its screen now; None when it may."""
        state = self._hub.states(principal).get(device_id)
        if state is None or not state.connected or state.locked or state.sleep is not None:
            return ScreenRefusal.AWAY  # §3.3 (Q3): only an awake computer
        if not state.screen_look:
            return ScreenRefusal.TURNED_OFF
        if free_wake and not state.free_wake_screen:
            return ScreenRefusal.NOT_HERE  # V3-P90 §10: a free wake only with its own setting on
        return None

    async def look(self, scope: TrustedScope, device_id: str, *, free_wake: bool = False) -> bytes | ScreenRefusal:
        refusal = self._refusal(scope.principal_id, device_id, free_wake)
        if refusal is not None:
            return refusal
        request_id = "sr_" + secrets.token_urlsafe(18)
        future: asyncio.Future[bytes | ScreenRefusal] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Request(scope.principal_id, device_id, free_wake, future)
        try:
            sent = self._hub.publish_device(scope.principal_id, device_id, "screen_request", {
                "request_id": request_id, "conversation": scope.conversation_id, "job_id": self.job_of(scope)})
            if not sent:
                return ScreenRefusal.AWAY
            try:
                return await asyncio.wait_for(asyncio.shield(future), self._wait)
            except TimeoutError:
                return ScreenRefusal.TIMEOUT
        finally:
            del self._pending[request_id]
            if not future.done():
                future.cancel()
            now = self._clock()
            self._ended = {k: v for k, v in self._ended.items() if v[2] > now}
            self._ended[request_id] = (scope.principal_id, device_id, now + ENDED_KEEP_SECONDS)

    def deliver(self, principal: PrincipalId, device_id: str, request_id: str, image: bytes) -> Answer:
        return self._settle(principal, device_id, request_id, image)

    def refuse(self, principal: PrincipalId, device_id: str, request_id: str, reason: str) -> Answer:
        return self._settle(principal, device_id, request_id, APP_REFUSALS[reason])

    def _settle(self, principal: PrincipalId, device_id: str, request_id: str,
                answer: bytes | ScreenRefusal) -> Answer:
        request = self._pending.get(request_id)
        if request is None or request.principal != principal or request.device_id != device_id:
            ended = self._ended.get(request_id)
            if ended is not None and ended[:2] == (principal, device_id) and ended[2] > self._clock():
                return Answer.ENDED  # §4.2: timed out, cancelled or answered a moment ago
            return Answer.UNKNOWN
        if request.future.done():
            return Answer.ENDED
        if isinstance(answer, bytes):  # turned off or locked while it was asked: not given to the model
            refusal = self._refusal(principal, device_id, request.free_wake)
            if refusal is not None:
                request.future.set_result(refusal)
                return Answer.ENDED
        request.future.set_result(answer)
        return Answer.TAKEN
