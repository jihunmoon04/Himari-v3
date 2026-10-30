"""V3-P83 §2.2, §6.2, §8.1: the Runner's events on the stream, and where the user is in the model's input."""

from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from himari.bootstrap.application import Application
from himari.common import CallId, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope, thaw
from himari.data.api import MessageOrigin, OriginKind
from himari.presence.api import StreamEvent, Subscription
from himari.prompt.api import DevicePresence, PresenceView
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")
WEB = MessageOrigin(OriginKind.WEB)


def two_runs() -> list[Any]:
    return [NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/write_file"),
                                          {"path": "a.txt", "content": "x"}),)),
            NormalizedFinal((TextBlock("done"),))]


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


async def drained(subscription: Subscription) -> list[StreamEvent]:
    for _ in range(5):
        await asyncio.sleep(0)  # the activity is published by a task (it reads Data)
    out = []
    while (event := await subscription.next(0)) is not None:
        out.append(event)
    return out


async def paired(app: Application, name: str = "데스크톱") -> str:
    made = await app.devices.create(ME, name)
    token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return (await app.devices.pair(made.code, token)).device.device_id


def test_the_runners_events_reach_the_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await opened(tmp_path, ScriptedAdapter(two_runs()))
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            stream = app.hub.subscribe(ME)
            origin = MessageOrigin(OriginKind.DEVICE, "d1")
            await app.send(scope, "hello", origin=origin)
            await app.runner.wait(scope)
            got = [(e.name, thaw(e.data)) for e in await drained(stream)]
            cid = scope.conversation_id
            assert [g for g in got if g[0] != "activity"] == [
                ("conversation", {"conversation": cid}),  # accepted
                ("job", {"conversation": cid, "state": "started", "origin": {"kind": "device", "device_id": "d1"}}),
                ("conversation", {"conversation": cid}), ("conversation", {"conversation": cid}),  # two Runs
                ("job", {"conversation": cid, "state": "ended", "outcome": "completed", "error": None}),
                ("conversation", {"conversation": cid}),
            ]
            activities = [data for name, data in got if name == "activity"]
            assert activities[-1] == {"working": [], "waiting": []}
            assert await app.events.activity(ME) == {"working": [], "waiting": []}  # type: ignore[union-attr]
            await app.trash(scope)
            assert [e.name for e in await drained(stream)] == ["activity"]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_model_is_told_where_the_user_is(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([*two_runs(), NormalizedFinal((TextBlock("web"),))])
        app = await opened(tmp_path, adapter)
        try:
            desk = await paired(app)
            await paired(app, "노트북")
            app.hub.subscribe(ME, desk)
            app.hub.signal(ME, desk, 120, "Europe/London")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hello", origin=MessageOrigin(OriginKind.DEVICE, desk))
            await app.runner.wait(scope)
            first, second = adapter.prepared_inputs
            told = PresenceView(("device", "데스크톱"), "데스크톱",
                                (DevicePresence("데스크톱", True, idle_minutes=2), DevicePresence("노트북", False)))
            assert first.current_state.presence == told
            assert second.current_state.presence == told  # read once for the turn
            now = first.current_state.now
            assert now is not None and str(now.tzinfo) == "Europe/London"  # the device's zone
            await app.send(scope, "from the web", origin=WEB)
            await app.runner.wait(scope)
            last = adapter.prepared_inputs[-1].current_state
            assert last.presence is not None and last.presence.sent_from == ("web", None)
            assert last.now is not None and last.now.utcoffset() == timedelta(hours=9)  # the server's
        finally:
            await app.close()

    asyncio.run(scenario())


def test_nothing_is_told_without_a_paired_device_or_with_the_companion_off(tmp_path: Path) -> None:
    async def scenario(companion: bool, pair: bool) -> PresenceView | None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("hi"),))])
        root = tmp_path / f"{companion}-{pair}"
        root.mkdir()
        app = await opened(root, adapter, companion=companion)
        try:
            if pair:
                await paired(app)
            else:
                await app.devices.create(ME, "기다리는 기기")  # not paired yet: not told
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hello", origin=WEB)
            await app.runner.wait(scope)
            return adapter.prepared_inputs[0].current_state.presence
        finally:
            await app.close()

    assert asyncio.run(scenario(companion=True, pair=False)) is None
    assert asyncio.run(scenario(companion=False, pair=True)) is None
    assert asyncio.run(scenario(companion=True, pair=True)) is not None
