"""V3-P87 part 1 (§4, §11): ``try`` opens a wake on the current device in the most
recent conversation, the wake ends with its job, and a restart settles the
wakes it cut."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from himari.bootstrap.application import Application
from himari.bootstrap.proactive import ProactiveRefused, parse_quiet, wake_outcome
from himari.common import ASK_USER_TOOL_ID, CallId, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope
from himari.control.api import DriveResult, DriveState, JobStatus
from himari.data.api import EventKind, MessageOrigin, OriginKind, WakeKind, WakeReason, WakeStatus
from himari.proactive import ProactiveSettings
from himari.prompt.api import PROACTIVE_RULE, UserTurn
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")
ON = ProactiveSettings(enabled=True)


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


async def current_device(app: Application) -> str:
    import base64
    import secrets

    made = await app.devices.create(ME, "데스크톱")
    token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    device = (await app.devices.pair(made.code, token)).device.device_id
    app.hub.subscribe(ME, device)
    app.hub.signal(ME, device, 0, "Asia/Seoul")
    return device


async def ended(app: Application, wake_id: str) -> WakeStatus:
    for _ in range(100):
        record = await app.data.read_wake(ME, wake_id)
        if record.status is not WakeStatus.OPENED:
            return record.status
        await asyncio.sleep(0.01)
    return WakeStatus.OPENED


def test_try_speaks_first_on_the_current_device(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응"),)), NormalizedFinal((TextBlock("왔어?"),))])
        app = await opened(tmp_path, adapter, proactive=ON)
        try:
            device = await current_device(app)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            conversation, status = await app.try_wake(ME)
            assert conversation == scope.conversation_id and status.wake_id is not None
            done = await app.runner.wait(scope)
            assert done.result is not None and done.result.state is DriveState.COMPLETED
            assert await ended(app, status.wake_id) is WakeStatus.ANSWERED
            events = await app.data.read_events(scope)
            assert [e.kind for e in events][-2:] == [EventKind.WAKE, EventKind.ASSISTANT_FINAL]
            assert events[-2].payload.device_id == device  # type: ignore[union-attr]
            sent = adapter.prepared_inputs[-1]
            assert any(PROACTIVE_RULE in part for part in sent.stable_prefix)
            last = sent.conversation[-1]
            assert isinstance(last, UserTurn) and last.wake
            assert (await app.proactive_status(ME))["current_device"] == device
        finally:
            await app.close()

    asyncio.run(scenario())


def test_try_is_refused_with_a_reason(tmp_path: Path) -> None:
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()

    async def scenario() -> None:
        off = await opened(tmp_path / "off", ScriptedAdapter([]))
        try:
            with pytest.raises(ProactiveRefused) as refused:
                await off.try_wake(ME)
            assert refused.value.code == "proactive_off"
        finally:
            await off.close()
        app = await opened(tmp_path / "on", ScriptedAdapter([]), proactive=ON)
        try:
            with pytest.raises(ProactiveRefused) as refused:
                await app.try_wake(ME)
            assert refused.value.code == "no_device"
            await current_device(app)
            with pytest.raises(ProactiveRefused) as refused:
                await app.try_wake(ME)
            assert refused.value.code == "no_conversation"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_try_never_speaks_over_a_waiting_question(tmp_path: Path) -> None:
    async def scenario() -> None:
        ask = NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId(ASK_USER_TOOL_ID), {"question": "어디?"}),))
        app = await opened(tmp_path, ScriptedAdapter([ask]), proactive=ON)
        try:
            await current_device(app)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "여행", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            with pytest.raises(ProactiveRefused) as refused:
                await app.try_wake(ME)
            assert refused.value.code == "not_settled"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_restart_settles_the_wakes_it_cut(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await opened(tmp_path, ScriptedAdapter([NormalizedFinal((TextBlock("응"),))]), proactive=ON)
        scope = TrustedScope(ME, await app.create_conversation(ME))
        await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
        await app.runner.wait(scope)
        silent = await app.data.begin_wake(scope, (WakeReason(WakeKind.TRY),), "d1",
                                           keys=("loop:m1:2026-11-04",))  # cut before speaking
        await app.close()
        again = await opened(tmp_path, ScriptedAdapter([]), proactive=ON)
        try:
            record = await again.data.read_wake(ME, silent.payload.wake_id)  # type: ignore[union-attr]
            assert record.status is WakeStatus.FAILED
            assert await again.data.opened_wakes() == ()
            # it said nothing: its key is given back, so the loop may still be spoken of (part 2)
            assert await again.data.taken_keys(ME, ("loop:m1:2026-11-04",)) == frozenset()
        finally:
            await again.close()

    asyncio.run(scenario())


DONE = JobStatus(running=False, result=DriveResult(DriveState.COMPLETED))
ASKED = JobStatus(running=False, result=DriveResult(DriveState.WAITING_FOR_USER))
CANCELLED = JobStatus(running=False, result=DriveResult(DriveState.CANCELLED))


@pytest.mark.parametrize(("status", "after", "waiting", "outcome"), [
    (DONE, ["final"], False, (WakeStatus.ANSWERED, False)),
    (ASKED, ["calls"], True, (WakeStatus.ANSWERED, True)),
    (CANCELLED, [], False, (WakeStatus.CANCELLED, False)),
    (CANCELLED, ["calls"], False, (WakeStatus.FAILED, False)),
    (JobStatus(running=False, error="provider"), [], False, (WakeStatus.FAILED, False)),
    (JobStatus(running=False, result=DriveResult(DriveState.RUN_LIMIT_REACHED)), ["calls"], False,
     (WakeStatus.FAILED, False)),
    # the user won and their own turn answered before the wake was ended: not the wake's answer
    (CANCELLED, ["message", "final"], False, (WakeStatus.CANCELLED, False)),
    # at a restart (no job): from the Events
    (None, ["final"], False, (WakeStatus.ANSWERED, False)),
    (None, ["calls"], True, (WakeStatus.ANSWERED, True)),
    (None, ["calls", "answer", "final"], False, (WakeStatus.ANSWERED, True)),
    (None, ["calls"], False, (WakeStatus.FAILED, False)),
    (None, ["message", "final"], True, (WakeStatus.FAILED, False)),
])
def test_how_a_wake_ends(status: JobStatus | None, after: list[str], waiting: bool,
                         outcome: tuple[WakeStatus, bool]) -> None:
    from himari.common import CallRef, ConversationId, EventId, RunId
    from himari.data.api import AssistantFinal, AssistantToolCalls, ConversationEvent, UserMessage

    payloads = {"final": AssistantFinal((TextBlock("x"),)), "calls": AssistantToolCalls((), ()),
                "message": UserMessage((TextBlock("잠깐"),)),
                "answer": UserMessage((TextBlock("서울"),), answers_call=CallRef(RunId("r"), CallId("c0")))}
    kinds = {"final": EventKind.ASSISTANT_FINAL, "calls": EventKind.ASSISTANT_TOOL_CALLS,
             "message": EventKind.USER_MESSAGE, "answer": EventKind.USER_MESSAGE}
    events = [ConversationEvent(EventId(f"e{i}"), ConversationId("c"), i + 2, kinds[a], payloads[a], 1,  # type: ignore[arg-type]
                                datetime.now(UTC), origin_run_id=None if a in ("message", "answer") else RunId("r"))
              for i, a in enumerate(after)]
    assert wake_outcome(status, events, waiting) == outcome


def test_quiet_hours_parse() -> None:
    assert parse_quiet(None) == (time(23, 0), time(8, 0))
    assert parse_quiet("") == (None, None)
    assert parse_quiet("22:30-07:00") == (time(22, 30), time(7, 0))
    for wrong in ("22:30", "late-early"):
        with pytest.raises(ValueError):
            parse_quiet(wrong)


def test_only_a_checked_model_speaks_first(tmp_path: Path) -> None:
    """V3-P87 §13, owner 2026-11-01: a model under the speaking-first bar gets no wake and no "Speaking first" line."""
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응"),))], speaks_first=False)
        app = await opened(tmp_path, adapter, proactive=ON)
        try:
            await current_device(app)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            assert not any(PROACTIVE_RULE in part for part in adapter.prepared_inputs[-1].stable_prefix)
            with pytest.raises(ProactiveRefused) as refused:
                await app.try_wake(ME)
            assert refused.value.code == "model_unchecked"
            assert await app.data.opened_wakes() == () and await app.data.wakes_since(
                ME, datetime(2026, 1, 1, tzinfo=UTC)) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_checked_models_are_the_ones_that_passed() -> None:
    from himari.provider.openrouter import KNOWN_CAPABILITIES

    assert {m for m, c in KNOWN_CAPABILITIES.items() if c.speaks_first} == {
        "openai/gpt-6-luna", "anthropic/claude-haiku-5.5", "xiaomi/mimo-v2.6-pro", "deepseek/deepseek-v4.1-flash"}
    assert not KNOWN_CAPABILITIES["minimax/minimax-m3"].speaks_first  # 13 of 18: under the bar


def test_a_model_changed_before_the_wakes_turn_withdraws_it(tmp_path: Path) -> None:
    """PR #158 review P2-1: admitted on a checked model, the conversation's model changes
    before the job's first Run; the turn checks again, makes no Run and withdraws the wake."""
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응"),)), NormalizedFinal((TextBlock("왔어?"),))])
        app = await opened(tmp_path, adapter, proactive=ON)
        try:
            await current_device(app)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            sent = len(adapter.sent)
            _, status = await app.try_wake(ME)  # admitted: the job has not run yet
            adapter._speaks_first = False  # the model the next turn resolves is not checked
            done = await app.runner.wait(scope)
            assert done.result is not None and done.result.state is DriveState.CANCELLED
            assert done.result.detail == "model_unchecked"
            assert len(adapter.sent) == sent  # no Run
            assert await ended(app, status.wake_id or "") is WakeStatus.CANCELLED
        finally:
            await app.close()

    asyncio.run(scenario())
