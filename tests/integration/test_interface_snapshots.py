"""V3-P93: acceptance pins UI support across tool rounds, answers, compression and restart."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from testkit import SCOPE, RecordingTool, calls, final, new_store, restart

from himari.bootstrap import Runtime, Store, build_runtime
from himari.bootstrap.interfaces import ConfiguredInterfaces
from himari.common import ASK_USER_TOOL_ID, ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.control.api import DriveState, TurnPresence
from himari.data.api import (
    DataError, InterfaceCapabilities, InterfaceKind, InterfaceSnapshot, MessageOrigin, OriginKind,
    UserMessage, Wake, WakeKind, WakeReason, interface_kind,
)
from himari.prompt.api import CompressedHistory, ContextBudget, PresenceView, SystemSpec
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import ToolOutcome, ToolStatus
from testkit import text_result

WEB = MessageOrigin(OriginKind.WEB)
CLI = MessageOrigin(OriginKind.CLI)
DESK = MessageOrigin(OriginKind.DEVICE, "d1")


class Source:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls = 0

    async def read(self, scope: object, origin: MessageOrigin | None) -> InterfaceSnapshot:
        self.calls += 1
        kind = interface_kind(origin)
        return InterfaceSnapshot(kind, InterfaceCapabilities(self.enabled, kind is InterfaceKind.WEB))


def make(provider: ScriptedAdapter, source: Source | ConfiguredInterfaces, *, store: Store | None = None,
         tools: tuple[RecordingTool, ...] = (), budget: ContextBudget | None = None, limit: int = 16) -> Runtime:
    fresh = store is None
    store = new_store() if store is None else store
    rt = build_runtime(provider, store=store, interfaces=source, tools=tools, context_budget=budget,
                       max_runs_per_drive=limit, system=SystemSpec(companion=True))
    if fresh:
        store.create_conversation(SCOPE)
    return rt


def message(origin: MessageOrigin | None = WEB) -> UserMessage:
    return UserMessage((TextBlock("help"),), origin=origin)


def test_capabilities_are_sampled_once_before_the_first_run() -> None:
    async def scenario() -> None:
        source = Source()

        def change(*_: object) -> ToolOutcome:
            source.enabled = False
            return ToolOutcome(ToolStatus.SUCCEEDED, text_result("ok"))

        tool = RecordingTool("probe", behavior=change)
        provider = ScriptedAdapter([calls(("probe", {})), final("done")])
        rt = make(provider, source, tools=(tool,))
        assert (await rt.driver.drive(SCOPE, message())).state is DriveState.COMPLETED
        assert source.calls == 1
        snapshots = [mi.current_state.interface for mi in provider.prepared_inputs]
        assert snapshots == [InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, True))] * 2

    asyncio.run(scenario())


def test_accepted_input_is_restored_after_restart_without_resampling() -> None:
    async def scenario() -> None:
        first = Source()
        rt = make(ScriptedAdapter([]), first)
        await rt.driver.accept(SCOPE, message())
        assert first.calls == 1
        second = Source(False)
        provider = ScriptedAdapter([final("done")])
        again = make(provider, second, store=restart(rt.store))
        assert (await again.driver.resume(SCOPE)).state is DriveState.COMPLETED
        assert second.calls == 0
        assert provider.prepared_inputs[0].current_state.interface == InterfaceSnapshot(
            InterfaceKind.WEB, InterfaceCapabilities(True, True))

    asyncio.run(scenario())


def test_answer_snapshot_survives_compression_past_the_answer_and_restart() -> None:
    async def scenario() -> None:
        source = Source()
        tool = RecordingTool("probe")
        provider = ScriptedAdapter([calls((ASK_USER_TOOL_ID, {"question": "which?"})),
                                    calls(("probe", {})), final("SUMMARY")])
        rt = make(provider, source, tools=(tool,), budget=ContextBudget(10000), limit=1)
        assert (await rt.driver.drive(SCOPE, message())).state is DriveState.WAITING_FOR_USER
        assert (await rt.driver.drive(SCOPE, message(CLI))).state is DriveState.RUN_LIMIT_REACHED
        answer = next(e for e in await rt.data.read_events(SCOPE)
                      if isinstance(e.payload, UserMessage) and e.payload.answers_call is not None)
        assert (await rt.driver.compact(SCOPE)).state is DriveState.COMPACTED
        snapshot = await rt.data.get_latest_snapshot(SCOPE)
        assert snapshot is not None and snapshot.covers_through_sequence >= answer.sequence
        summary_request = str(provider.prepared_inputs[-1].conversation)
        assert "file_download" not in summary_request and "workspace_browser" not in summary_request
        provider2 = ScriptedAdapter([final("done")])
        changed = Source(False)
        again = make(provider2, changed, store=restart(rt.store), tools=(tool,), budget=ContextBudget(10000))
        assert (await again.driver.resume(SCOPE)).state is DriveState.COMPLETED
        mi = provider2.prepared_inputs[-1]
        assert isinstance(mi.conversation[0], CompressedHistory)
        assert mi.current_state.interface == InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True, False))
        assert changed.calls == 0

    asyncio.run(scenario())


def test_wake_keeps_its_accepted_snapshot_after_restart() -> None:
    async def scenario() -> None:
        # Simulate future confirmed device support at the InterfaceSource port; phase 1's resolver stays false.
        rt = make(ScriptedAdapter([], companion=True), Source())
        event = await rt.driver.accept_wake(SCOPE, (WakeReason(WakeKind.TRY),), "d1")
        assert isinstance(event.payload, Wake)
        provider = ScriptedAdapter([final("hello")], companion=True)
        changed = Source(False)
        again = make(provider, changed, store=restart(rt.store))
        assert (await again.driver.resume_wake(SCOPE, event.payload.wake_id)).state is DriveState.COMPLETED
        assert provider.prepared_inputs[0].current_state.interface == event.payload.interface
        assert changed.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("origin,kind", [(WEB, InterfaceKind.WEB), (CLI, InterfaceKind.CLI),
                                       (DESK, InterfaceKind.WINDOWS_COMPANION), (None, InterfaceKind.UNKNOWN)])
def test_legacy_input_never_infers_capabilities_from_live_support(origin: MessageOrigin | None, kind: InterfaceKind) -> None:
    async def scenario() -> None:
        source = Source()
        provider = ScriptedAdapter([final("done")], companion=True)
        rt = make(provider, source)
        await rt.data.append_event(SCOPE, message(origin))  # a pre-P93 event, no snapshot
        await rt.driver.resume(SCOPE)
        assert provider.prepared_inputs[0].current_state.interface == InterfaceSnapshot(kind)
        assert source.calls == 0

    asyncio.run(scenario())


def test_input_cannot_supply_its_own_capabilities_or_a_different_kind() -> None:
    async def scenario() -> None:
        rt = make(ScriptedAdapter([]), ConfiguredInterfaces())
        forged = InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, True))
        await rt.driver.accept(SCOPE, replace(message(), interface=forged))
        first = (await rt.data.read_events(SCOPE))[0].payload
        assert isinstance(first, UserMessage) and first.interface == InterfaceSnapshot(InterfaceKind.WEB)
        with pytest.raises(DataError):
            await rt.driver.accept(SCOPE, replace(message(DESK), interface=forged))
        assert len(await rt.data.read_events(SCOPE)) == 1

    asyncio.run(scenario())


def test_a_rejected_answer_does_not_consume_the_waiting_call() -> None:
    async def scenario() -> None:
        rt = make(ScriptedAdapter([calls((ASK_USER_TOOL_ID, {"question": "which?"}))]), Source())
        result = await rt.driver.drive(SCOPE, message())
        assert result.waiting_call is not None
        before = await rt.data.read_events(SCOPE)
        with pytest.raises(DataError):
            await rt.data.answer_pending_call(SCOPE, result.waiting_call, replace(
                message(CLI), interface=InterfaceSnapshot(InterfaceKind.WEB)))
        assert await rt.data.read_events(SCOPE) == before
        assert await rt.data.find_waiting_call(SCOPE) == result.waiting_call
        answer = replace(message(CLI), interface=InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True)))
        await rt.data.answer_pending_call(SCOPE, result.waiting_call, answer)
        recorded = [e.payload for e in await rt.data.read_events(SCOPE)
                    if isinstance(e.payload, UserMessage) and e.payload.answers_call is not None]
        assert len(recorded) == 1 and recorded[0].interface == answer.interface

    asyncio.run(scenario())


def test_parallel_conversations_do_not_share_their_input_snapshot() -> None:
    async def scenario() -> None:
        ready = asyncio.Event()

        class Together(Source):
            async def read(self, scope: object, origin: MessageOrigin | None) -> InterfaceSnapshot:
                found = await super().read(scope, origin)
                if self.calls == 2:
                    ready.set()
                await ready.wait()
                return found

        provider = ScriptedAdapter([final("one"), final("two")])
        rt = make(provider, Together())
        other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))
        rt.store.create_conversation(other)
        results = await asyncio.wait_for(asyncio.gather(rt.driver.drive(SCOPE, message()),
                                                      rt.driver.drive(other, message(CLI))), timeout=5)
        assert all(r.state is DriveState.COMPLETED for r in results)
        assert {mi.current_state.interface for mi in provider.prepared_inputs} == {
            InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, True)),
            InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True, False))}

    asyncio.run(scenario())


def test_live_device_switch_does_not_change_the_turn_interface() -> None:
    async def scenario() -> None:
        class Presence:
            current = "A"

            async def read(self, principal: PrincipalId, origin: MessageOrigin | None) -> TurnPresence:
                return TurnPresence(PresenceView(("device", "A"), self.current, ()))

        presence = Presence()

        def switch(*_: object) -> ToolOutcome:
            presence.current = "B"
            return ToolOutcome(ToolStatus.SUCCEEDED, text_result("switched"))

        tool = RecordingTool("switch", behavior=switch)
        provider = ScriptedAdapter([calls(("switch", {})), final("done"), final("web")], companion=True)
        rt = make(provider, ConfiguredInterfaces(True, True, frozenset({SCOPE.principal_id})), tools=(tool,))
        rt.driver._presence = presence
        await rt.driver.drive(SCOPE, message(DESK))
        assert presence.current == "B"
        for mi in provider.prepared_inputs:
            assert mi.current_state.interface == InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION)
        await rt.driver.drive(SCOPE, message())
        state = provider.prepared_inputs[-1].current_state
        assert state.presence is not None and state.presence.current_device == "B"
        assert state.interface == InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, True))

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("origin,kind", [(WEB, InterfaceKind.WEB), (CLI, InterfaceKind.CLI),
                                       (DESK, InterfaceKind.WINDOWS_COMPANION), (None, InterfaceKind.UNKNOWN)])
def test_phase_one_capabilities_follow_server_and_authenticated_interface(
        enabled: bool, origin: MessageOrigin | None, kind: InterfaceKind) -> None:
    found = asyncio.run(ConfiguredInterfaces(enabled, enabled, frozenset({SCOPE.principal_id})).read(SCOPE, origin))
    assert found == InterfaceSnapshot(kind, InterfaceCapabilities(
        enabled and kind in (InterfaceKind.WEB, InterfaceKind.CLI), enabled and kind is InterfaceKind.WEB))


def test_workspace_browser_is_false_without_the_principals_machine_permission() -> None:
    source = ConfiguredInterfaces(True, True, frozenset({PrincipalId("someone_else")}))
    assert asyncio.run(source.read(SCOPE, WEB)) == InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, False))
