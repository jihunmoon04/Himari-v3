"""The seams between Control and presence (V3-P83 §2.2), made here because
Control does not know presence.

- ``CompanionEvents`` is the Runner's ``JobListener``: it publishes ``job``,
  ``conversation`` and ``activity`` on the principal's event stream (§8.1).
- ``CompanionPresence`` is the Driver's ``PresenceSource``: where the user
  is, from the paired devices and what ``Presence`` knows now (§6.2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.control.api import JobStatus, TurnPresence
from himari.data.api import MessageOrigin, OriginKind
from himari.presence.api import Devices, EventHub, Presence
from himari.prompt.api import DevicePresence, PresenceView

log = logging.getLogger(__name__)

PRESENCE_DEVICES = 8  # devices the model is told of (§6.2)


class WaitingConversations(Protocol):
    async def list_waiting_conversations(self, principal: PrincipalId) -> Sequence[ConversationId]: ...


def origin_view(origin: MessageOrigin | None) -> dict[str, Any] | None:
    """Appendix A.3: ``{"kind": "device", "device_id": "d3"}``, ``{"kind": "web"}``, ``{"kind": "cli"}``."""
    if origin is None:
        return None
    return {"kind": origin.kind.value, **({"device_id": origin.device_id} if origin.device_id is not None else {})}


class CompanionEvents:
    """The Runner's listener. It keeps the origin of each running job, so
    ``activity`` can say where each working conversation's message came from."""

    def __init__(self, hub: EventHub, waiting: WaitingConversations) -> None:
        self._hub = hub
        self._waiting = waiting
        # Each working conversation's origin and job (V3-P84 §6.3: an app that reconnects finds its job).
        self._working: dict[tuple[PrincipalId, ConversationId], tuple[MessageOrigin | None, str | None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # Each principal's latest activity change: a read that a later change overtook is
        # not published (PR #124 review B1), so an older state never lands after a newer one.
        self._revisions: dict[PrincipalId, int] = {}

    async def activity(self, principal: PrincipalId) -> dict[str, Any]:
        """V3-P80's working and waiting conversations, with the working ones' origins (§8.1)."""
        working = sorted((cid, origin, job) for (who, cid), (origin, job) in self._working.items() if who == principal)
        return {"working": [{"id": cid, "origin": origin_view(origin), "job_id": job} for cid, origin, job in working],
                "waiting": sorted(await self._waiting.list_waiting_conversations(principal))}

    def activity_changed(self, principal: PrincipalId) -> None:
        """Publish ``activity`` soon (it reads Data): after a job, a trash, a restore.
        Only the read for the latest change is published."""
        revision = self._revisions[principal] = self._revisions.get(principal, 0) + 1
        task = asyncio.get_running_loop().create_task(self._publish_activity(principal, revision))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish_activity(self, principal: PrincipalId, revision: int) -> None:
        try:
            activity = await self.activity(principal)
        except Exception:  # noqa: BLE001 — a missed activity is caught up by the next hello
            log.exception("could not read the activity")
            return
        if self._revisions.get(principal) == revision:  # else a later change's read publishes
            self._hub.publish(principal, "activity", activity)

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None, status: JobStatus) -> None:
        self._working[(scope.principal_id, scope.conversation_id)] = (origin, status.job_id)
        self._hub.publish(scope.principal_id, "job", {"conversation": scope.conversation_id, "state": "started",
                                                      "origin": origin_view(origin), "job_id": status.job_id})
        self.activity_changed(scope.principal_id)

    def accepted(self, scope: TrustedScope) -> None:
        self._hub.publish(scope.principal_id, "conversation", {"conversation": scope.conversation_id})

    def run_ended(self, scope: TrustedScope) -> None:
        self._hub.publish(scope.principal_id, "conversation", {"conversation": scope.conversation_id})

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None:
        self._working.pop((scope.principal_id, scope.conversation_id), None)
        outcome = None if status.result is None else status.result.state.value
        self._hub.publish(scope.principal_id, "job", {"conversation": scope.conversation_id, "state": "ended",
                                                      "outcome": outcome, "error": status.error,
                                                      "job_id": status.job_id})
        self._hub.publish(scope.principal_id, "conversation", {"conversation": scope.conversation_id})
        self.activity_changed(scope.principal_id)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class CompanionPresence:
    """§6.2: told only when the companion is on and a device is paired."""

    def __init__(self, devices: Devices, presence: Presence, time_zone: str, *, enabled: bool) -> None:
        self._devices = devices
        self._presence = presence
        self._zone = ZoneInfo(time_zone)
        self._enabled = enabled

    async def read(self, principal: PrincipalId, sent_from: MessageOrigin | None) -> TurnPresence:
        if not self._enabled:
            return TurnPresence()
        paired = [d for d in await self._devices.list(principal) if d.paired_at is not None]
        if not paired:
            return TurnPresence()
        names = {d.device_id: d.name for d in paired}
        states = self._presence.states(principal)
        current = self._presence.current(principal)
        sent: tuple[str, str | None] | None = None
        time_zone = None
        if sent_from is not None:
            sent = (sent_from.kind.value, names.get(sent_from.device_id or "") if sent_from.kind is OriginKind.DEVICE
                    else None)
            state = states.get(sent_from.device_id or "") if sent_from.kind is OriginKind.DEVICE else None
            time_zone = None if state is None else state.time_zone
        shown: list[DevicePresence] = []
        for device in paired[:PRESENCE_DEVICES]:
            state = states.get(device.device_id)
            if state is not None and state.connected:
                idle = None if state.idle_seconds is None else int(state.idle_seconds // 60)
                shown.append(DevicePresence(device.name, True, idle_minutes=idle))
            else:
                seen = device.last_seen_at
                shown.append(DevicePresence(device.name, False, last_seen=None if seen is None
                                            else seen.astimezone(self._zone).date().isoformat()))
        view = PresenceView(sent, None if current is None else names.get(current), tuple(shown))
        return TurnPresence(view, time_zone)
