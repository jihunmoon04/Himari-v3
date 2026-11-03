"""V3-P91 §4: a screen request goes to the one device it asks, and is answered once."""

from __future__ import annotations

import asyncio

import pytest

from himari.bootstrap.screens import ENDED_KEEP_SECONDS, Answer, ScreenRequests
from himari.common import ConversationId, PrincipalId, TrustedScope, thaw
from himari.presence.api import Session, Signals, Sleep, StreamEvent, Subscription
from himari.presence.hub import PresenceHub
from himari.tool.api import ScreenRefusal

ME = PrincipalId("operator")
SCOPE = TrustedScope(ME, ConversationId("c1"))
LOOKS = Signals(session=Session.ACTIVE, screen_look=True)


async def request_on(stream: Subscription) -> dict[str, object]:
    while True:
        event = await stream.next(2)
        assert event is not None, "no screen request came"
        if event.name == "screen_request":
            data = thaw(event.data)
            assert isinstance(data, dict)
            return data


async def names(stream: Subscription) -> list[str]:
    out: list[StreamEvent] = []
    while (event := await stream.next(0)) is not None:
        out.append(event)
    return [e.name for e in out]


def hub_with(signals: Signals = LOOKS) -> tuple[PresenceHub, Subscription]:
    hub = PresenceHub()
    stream = hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, "Asia/Seoul", signals)
    return hub, stream


def test_the_device_is_asked_alone_and_its_image_comes_back() -> None:
    async def scenario() -> None:
        hub, stream = hub_with()
        other, web = hub.subscribe(ME, "d2"), hub.subscribe(ME)
        requests = ScreenRequests(hub)
        requests.job_of = lambda scope: "1.job"
        looking = asyncio.create_task(requests.look(SCOPE, "d1"))
        asked = await request_on(stream)
        assert asked["conversation"] == "c1" and asked["job_id"] == "1.job"
        request_id = str(asked["request_id"])
        assert requests.deliver(ME, "d2", request_id, b"x") is Answer.UNKNOWN  # another device's token
        assert requests.deliver(PrincipalId("other"), "d1", request_id, b"x") is Answer.UNKNOWN
        assert requests.deliver(ME, "d1", request_id, b"image") is Answer.TAKEN
        assert requests.deliver(ME, "d1", request_id, b"again") is Answer.ENDED  # answered, not yet gone
        assert await looking == b"image"
        assert requests.deliver(ME, "d1", request_id, b"late") is Answer.ENDED  # §4.2: ended, not unknown
        assert requests.deliver(ME, "d2", request_id, b"late") is Answer.UNKNOWN  # still not another's
        assert "screen_request" not in await names(other) + await names(web)

    asyncio.run(scenario())


def test_the_app_says_why_not() -> None:
    async def scenario() -> list[object]:
        hub, stream = hub_with()
        requests = ScreenRequests(hub)
        out: list[object] = []
        for reason in ("capture_failed", "excluded", "turned_off", "asleep"):
            looking = asyncio.create_task(requests.look(SCOPE, "d1"))
            asked = await request_on(stream)
            assert requests.refuse(ME, "d1", str(asked["request_id"]), reason) is Answer.TAKEN
            out.append(await looking)
        return out

    assert asyncio.run(scenario()) == [ScreenRefusal.CAPTURE_FAILED, ScreenRefusal.CAPTURE_FAILED,
                                       ScreenRefusal.TURNED_OFF, ScreenRefusal.AWAY]


def test_what_presence_knows_refuses_before_asking() -> None:
    async def scenario(signals: Signals, *, free_wake: bool = False, subscribe: bool = True) -> object:
        hub = PresenceHub()
        stream = hub.subscribe(ME, "d1") if subscribe else None
        hub.signal(ME, "d1", 0, "Asia/Seoul", signals)
        found = await ScreenRequests(hub).look(SCOPE, "d1", free_wake=free_wake)
        if stream is not None:
            assert "screen_request" not in await names(stream)
        return found

    def run(*args: object, **kwargs: object) -> object:
        return asyncio.run(scenario(*args, **kwargs))  # type: ignore[arg-type]

    assert run(Signals(session=Session.ACTIVE)) is ScreenRefusal.TURNED_OFF  # an app that does not take requests
    assert run(Signals(session=Session.ACTIVE, screen_look=True, sleep_known=True, sleep=Sleep.IDLE)) \
        is ScreenRefusal.AWAY
    assert run(Signals(session=Session.LOCKED, screen_look=True)) is ScreenRefusal.AWAY
    assert run(LOOKS, subscribe=False) is ScreenRefusal.AWAY  # no stream: not connected
    assert run(LOOKS, free_wake=True) is ScreenRefusal.NOT_HERE  # V3-P90 §10: its own setting is off


def test_a_free_wake_asks_when_its_setting_is_on() -> None:
    async def scenario() -> None:
        hub, stream = hub_with(Signals(session=Session.ACTIVE, screen_look=True, free_wake_screen=True))
        requests = ScreenRequests(hub)
        looking = asyncio.create_task(requests.look(SCOPE, "d1", free_wake=True))
        asked = await request_on(stream)
        assert requests.deliver(ME, "d1", str(asked["request_id"]), b"image") is Answer.TAKEN
        assert await looking == b"image"

    asyncio.run(scenario())


def test_nothing_in_time_is_a_timeout_and_a_cancelled_call_ends_its_request() -> None:
    async def scenario() -> None:
        hub, stream = hub_with()
        requests = ScreenRequests(hub, wait_seconds=0.05)
        assert await requests.look(SCOPE, "d1") is ScreenRefusal.TIMEOUT
        late = await request_on(stream)
        assert requests.deliver(ME, "d1", str(late["request_id"]), b"x") is Answer.ENDED  # 410, not 404
        slow = ScreenRequests(hub, wait_seconds=10)
        looking = asyncio.create_task(slow.look(SCOPE, "d1"))
        asked = await request_on(stream)
        looking.cancel()  # the job stopped
        await asyncio.gather(looking, return_exceptions=True)
        assert slow.deliver(ME, "d1", str(asked["request_id"]), b"x") is Answer.ENDED
        assert slow.refuse(ME, "d1", str(asked["request_id"]), "capture_failed") is Answer.ENDED
        assert slow.deliver(ME, "d1", "sr_never", b"x") is Answer.UNKNOWN

    asyncio.run(scenario())


def test_an_ended_request_is_forgotten_after_a_while() -> None:
    async def scenario() -> None:
        hub, stream = hub_with()
        now = [0.0]
        requests = ScreenRequests(hub, wait_seconds=0.05, clock=lambda: now[0])
        assert await requests.look(SCOPE, "d1") is ScreenRefusal.TIMEOUT
        request_id = str((await request_on(stream))["request_id"])
        now[0] = ENDED_KEEP_SECONDS - 1
        assert requests.deliver(ME, "d1", request_id, b"x") is Answer.ENDED
        now[0] = ENDED_KEEP_SECONDS + 1
        assert requests.deliver(ME, "d1", request_id, b"x") is Answer.UNKNOWN

    asyncio.run(scenario())


@pytest.mark.parametrize(("then", "free_wake", "refusal"), [
    (Signals(session=Session.ACTIVE), False, ScreenRefusal.TURNED_OFF),  # viewing turned off
    (Signals(session=Session.LOCKED, screen_look=True), False, ScreenRefusal.AWAY),  # locked
    (Signals(session=Session.ACTIVE, screen_look=True), True, ScreenRefusal.NOT_HERE),  # free wake's setting off
])
def test_an_image_after_viewing_was_turned_off_is_not_given(then: Signals, free_wake: bool,
                                                           refusal: ScreenRefusal) -> None:
    async def scenario() -> None:
        hub, stream = hub_with(Signals(session=Session.ACTIVE, screen_look=True, free_wake_screen=True))
        requests = ScreenRequests(hub)
        looking = asyncio.create_task(requests.look(SCOPE, "d1", free_wake=free_wake))
        request_id = str((await request_on(stream))["request_id"])
        hub.signal(ME, "d1", 1, "Asia/Seoul", then)
        assert requests.deliver(ME, "d1", request_id, b"image") is Answer.ENDED
        assert await looking is refusal

    asyncio.run(scenario())
