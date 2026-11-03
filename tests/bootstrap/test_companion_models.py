"""Companion model support and gating tests (V3-P83, P84, P87, P90, P91)."""

from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from himari.bootstrap.application import Application, ModelRefused
from himari.common import PrincipalId, TrustedScope
from himari.provider.api import ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES
from himari.provider.scripted import ScriptedAdapter
from himari.control.api import ControlError, ControlErrorCode, DriveState
from himari.data.api import (MemoryCategory, MessageOrigin, ModelSetting, NoteDates, OriginKind, PromptBasis,
                             UserMessage, Wake, WakeKind, WakeReason, WakeStatus, parse_note_date)
from himari.bootstrap.proactive import ProactiveRefused
from himari.proactive import ProactiveSettings
from himari.common import TextBlock
from testkit import calls, final

ALICE = PrincipalId("alice")


def mixed_provider() -> FakeProvider:
    return FakeProvider({
        "standard-model": ProviderCapabilities("standard-model", speaks_first=True, context_tokens=100_000),
        "companion-model": ProviderCapabilities("companion-model", image_input=True, screen_points=True,
                                                screen_tool=True, speaks_first=True, free_wake=True, companion=True,
                                                context_tokens=100_000),
    })


class FakeProvider(ScriptedAdapter):
    def __init__(self, models: dict[str, ProviderCapabilities]) -> None:
        super().__init__()
        self._models = models

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        if model_id in self._models:
            return self._models[model_id]
        return super().describe_capabilities(model_id)


def test_companion_capable_models_in_openrouter_catalog() -> None:
    """Only models verified for all companion features (image, points, tool, wake, free) qualify."""
    capable = {
        model_id
        for model_id, caps in KNOWN_CAPABILITIES.items()
        if caps.companion
    }
    assert capable == {
        "anthropic/claude-haiku-5.5",
        "xiaomi/mimo-v2.6-pro",
        "deepseek/deepseek-v4.1-flash",
    }
    for model_id in capable:
        assert KNOWN_CAPABILITIES[model_id].companion_capable

    # Verify why each excluded model fails companion_capable:
    luna = KNOWN_CAPABILITIES["openai/gpt-6-luna"]
    assert luna.screen_tool and luna.screen_points and luna.speaks_first and not luna.free_wake
    assert not luna.companion and not luna.companion_capable

    sol = KNOWN_CAPABILITIES["openai/gpt-6.1-sol"]
    assert sol.screen_tool and sol.screen_points and not sol.speaks_first
    assert not sol.companion and not sol.companion_capable

    sonnet = KNOWN_CAPABILITIES["anthropic/claude-sonnet-5.5"]
    assert sonnet.screen_tool and sonnet.screen_points and not sonnet.speaks_first
    assert not sonnet.companion and not sonnet.companion_capable

    nano = KNOWN_CAPABILITIES["openai/gpt-5-nano"]
    assert not nano.screen_tool and not nano.screen_points
    assert not nano.companion and not nano.companion_capable

    minimax = KNOWN_CAPABILITIES["minimax/minimax-m3"]
    assert not minimax.screen_tool and not minimax.speaks_first
    assert not minimax.companion and not minimax.companion_capable


def test_application_companion_model_views(tmp_path: Path) -> None:
    async def run() -> None:
        provider = FakeProvider({
            "companion-a": ProviderCapabilities(
                "companion-a", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=True, companion=True, context_tokens=100_000,
            ),
            "standard-b": ProviderCapabilities(
                "standard-b", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=False, free_wake=False, companion=False, context_tokens=100_000,
            ),
        })

        app = await Application.open(
            tmp_path / "app",
            provider,
            model_id="companion-a",
            models=(("companion-a", None), ("standard-b", None)),
            sandbox=False,
        )
        assert app.companion_models == ("companion-a",)
        views = {m.model_id: m for m in app.models}
        assert views["companion-a"].companion is True
        assert views["standard-b"].companion is False
        await app.close()

    asyncio.run(run())


def test_explicit_unoffered_companion_default_is_a_startup_error(tmp_path: Path) -> None:
    async def run() -> None:
        app = None
        try:
            with pytest.raises(ValueError, match="companion default model.*not offered"):
                app = await Application.open(tmp_path / "app", mixed_provider(), model_id="standard-model",
                                             models=(("standard-model", None), ("companion-model", None)),
                                             companion_default_model="missing-model", sandbox=False)
        finally:
            if app is not None:
                await app.close()
    asyncio.run(run())


def test_automatic_companion_default_prefers_deepseek_only_when_offered(tmp_path: Path) -> None:
    async def run() -> None:
        from dataclasses import replace
        from himari.bootstrap.application import DEFAULT_COMPANION_MODEL

        provider = mixed_provider()
        for preferred in (False, True):
            models: tuple[tuple[str, str | None], ...] = (("standard-model", None), ("companion-model", None))
            if preferred:
                provider._models[DEFAULT_COMPANION_MODEL] = replace(provider._models["companion-model"],
                                                                    model_id=DEFAULT_COMPANION_MODEL)
                models = (*models, (DEFAULT_COMPANION_MODEL, None))
            app = await Application.open(tmp_path / str(preferred), provider, model_id="standard-model",
                                         models=models, sandbox=False)
            try:
                assert app.companion_default_model == (DEFAULT_COMPANION_MODEL if preferred else "companion-model")
            finally:
                await app.close()
    asyncio.run(run())


def test_device_rejects_removed_model_fallback_while_web_can_use_it(tmp_path: Path) -> None:
    async def run() -> None:
        provider = mixed_provider()
        provider.extend([final("web answer")])
        app = await Application.open(tmp_path / "app", provider, model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)), sandbox=False)
        try:
            cid = await app.create_conversation(ALICE, initial_model="companion-model")
            scope = TrustedScope(ALICE, cid)
            await app.data.set_model_setting(scope, ModelSetting("retired-model", None))
            assert (await app.model_choice(scope)).model_id == "standard-model"
            with pytest.raises(ModelRefused, match="companion model"):
                await app.send(scope, "device", origin=MessageOrigin(OriginKind.DEVICE, "d1"))
            assert await app.data.read_events(scope) == () and not provider.sent
            await app.send(scope, "web", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            assert provider.prepared_inputs[-1].request_options.model_id == "standard-model"
        finally:
            await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("wake", [False, True])
def test_device_execution_rechecks_the_resolved_model_after_acceptance(tmp_path: Path, wake: bool) -> None:
    async def run() -> None:
        provider = mixed_provider()
        provider.extend([final("반가워요")])
        app = await Application.open(tmp_path / "app", provider, model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)),
                                     companion_default_model="companion-model", sandbox=False)
        try:
            cid = await app.create_conversation(ALICE, initial_model="companion-model")
            scope = TrustedScope(ALICE, cid)
            driver = app.runtime.driver
            if wake:
                event = await driver.accept_wake(scope, (WakeReason(WakeKind.TRY),), "d1")
                assert isinstance(event.payload, Wake)
                wake_id = event.payload.wake_id
            else:
                await driver.accept(scope, UserMessage((TextBlock("hello"),), origin=MessageOrigin(OriginKind.DEVICE, "d1")))
            # Web may change the saved model between durable acceptance and background execution.
            await app.set_model(scope, "standard-model", None)
            done = await driver.resume_wake(scope, wake_id) if wake else await driver.resume(scope)
            if wake:
                assert done.state is DriveState.CANCELLED and done.detail == "model_unchecked"
                assert (await app.data.read_wake(ALICE, wake_id)).status is WakeStatus.CANCELLED
            else:
                assert done.state is DriveState.REQUEST_REJECTED and "companion_unsupported" in done.detail
            assert not provider.sent and not done.run_ids and not done.compaction_run_ids
            await app.set_model(scope, "companion-model", None)
            if wake:
                event = await driver.accept_wake(scope, (WakeReason(WakeKind.TRY),), "d1")
                assert isinstance(event.payload, Wake)
                wake_id = event.payload.wake_id
            done = await driver.resume_wake(scope, wake_id) if wake else await driver.resume(scope)
            assert done.state is DriveState.COMPLETED
            assert (await app.data.read_run(scope, done.run_ids[-1])).model_id == "companion-model"
        finally:
            await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("kind", [WakeKind.TRY, WakeKind.LOOP_DUE, WakeKind.REMINDER])
@pytest.mark.parametrize("compatible", [False, True])
def test_wakes_select_the_latest_compatible_user_conversation(
    tmp_path: Path, kind: WakeKind, compatible: bool
) -> None:
    """A newer Luna-like Web conversation never steals a wake or a reminder from the companion."""
    async def run() -> None:
        provider = mixed_provider()
        provider.extend([final("first"), final("web"), final("안녕하세요")])
        app = await Application.open(tmp_path / "app", provider, model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)), sandbox=False,
                                     cleanup_seconds=None,
                                     proactive=ProactiveSettings(enabled=True, quiet_start=None, quiet_end=None))
        try:
            await app.ticker.close()  # drive the scheduler explicitly; no wall-clock job may race this test
            made = await app.devices.create(ALICE, "desktop")
            token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            device = (await app.devices.pair(made.code, token)).device.device_id
            app.hub.subscribe(ALICE, device)
            app.hub.signal(ALICE, device, 0, "Asia/Seoul")
            older = None
            if compatible:
                older = TrustedScope(ALICE, await app.create_conversation(ALICE, initial_model="companion-model"))
                await app.send(older, "first", origin=MessageOrigin(OriginKind.WEB))
                await app.runner.wait(older)
            recent = TrustedScope(ALICE, await app.create_conversation(ALICE))
            await app.send(recent, "latest Web message", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(recent)
            untouched = await app.data.read_events(recent)
            sent = len(provider.sent)
            note = None
            scheduler = app.scheduler
            assert scheduler is not None
            if kind is not WakeKind.TRY:
                ago = timedelta(minutes=1) if kind is WakeKind.REMINDER else timedelta(hours=2)
                at = parse_note_date((datetime.now(UTC) - ago).isoformat(timespec="minutes"), "UTC")
                assert at is not None
                dates = (NoteDates(remind_at=at[0], remind_instant=at[1]) if kind is WakeKind.REMINDER
                         else NoteDates(due_at=at[0], due_instant=at[1]))
                note = await app.data.save_memory(ALICE, MemoryCategory.LOOP, "약속을 알려 주세요", dates=dates)
                scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=15)
            if kind is WakeKind.TRY:
                if compatible:
                    cid, _ = await app.try_wake(ALICE)
                    assert older is not None and cid == older.conversation_id
                else:
                    with pytest.raises(ProactiveRefused) as refused:
                        await app.try_wake(ALICE)
                    assert refused.value.code == "no_conversation"
            else:
                look = await scheduler.look(ALICE)
                assert look.outcome == ("spoke" if compatible else "no_conversation")
            if compatible:
                assert older is not None
                status = await app.runner.wait(older)
                assert status.result is not None and status.result.state is DriveState.COMPLETED
                assert status.wake_id is not None
                for _ in range(100):
                    record = await app.data.read_wake(ALICE, status.wake_id)
                    if not record.status.in_progress:
                        break
                    await asyncio.sleep(0.01)
                assert record.status is WakeStatus.ANSWERED and record.conversation_id == older.conversation_id
                assert record.reasons[0].kind is kind
                assert provider.prepared_inputs[-1].request_options.model_id == "companion-model"
                assert len(provider.sent) == sent + 1
            else:
                assert len(provider.sent) == sent
                assert await app.data.wakes_since(ALICE, datetime(2026, 1, 1, tzinfo=UTC)) == ()
            if note is not None and kind is WakeKind.REMINDER:
                saved = await app.data.read_memory(ALICE, note.memory_id)
                assert saved.remind_state == ("delivered" if compatible else "scheduled")
            assert await app.data.read_events(recent) == untouched
        finally:
            await app.close()
    asyncio.run(run())


def test_wake_admission_rejects_non_companion_before_persisting_or_claiming_reminder(tmp_path: Path) -> None:
    async def run() -> None:
        app = await Application.open(tmp_path / "app", mixed_provider(), model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)), sandbox=False)
        try:
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE, initial_model="companion-model"))
            # Selection was valid; a Web model change wins before admission.
            await app.set_model(scope, "standard-model", None)
            at = parse_note_date(datetime.now(UTC).isoformat(timespec="minutes"), "UTC")
            assert at is not None
            note = await app.data.save_memory(ALICE, MemoryCategory.LOOP, "알림",
                                              dates=NoteDates(remind_at=at[0], remind_instant=at[1]))
            key = "reminder:test"
            assert not await app.runtime.driver.free_wake_ok(scope)
            for reason in (WakeReason(WakeKind.TRY), WakeReason(WakeKind.REMINDER, memory_id=note.memory_id)):
                with pytest.raises(ControlError) as refused:
                    await app.runtime.driver.accept_wake(scope, (reason,), "d1", keys=(key,),
                                                         reminders=(note.memory_id,))
                assert refused.value.code is ControlErrorCode.MODEL_UNCHECKED
            assert await app.data.read_events(scope) == ()
            assert await app.data.opened_wakes() == ()
            assert await app.data.taken_keys(ALICE, (key,)) == frozenset()
            assert (await app.data.read_memory(ALICE, note.memory_id)).remind_state == "scheduled"
        finally:
            await app.close()
    asyncio.run(run())


def test_companion_recent_search_continues_past_a_page_and_resolves_removed_models(tmp_path: Path) -> None:
    async def run() -> None:
        app = await Application.open(tmp_path / "app", mixed_provider(), model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)), sandbox=False)
        try:
            older = TrustedScope(ALICE, await app.create_conversation(ALICE, initial_model="companion-model"))
            await app.data.append_event(older, UserMessage((TextBlock("companion"),)))
            for i in range(51):
                scope = TrustedScope(ALICE, await app.data.create_conversation(ALICE))
                await app.data.append_event(scope, UserMessage((TextBlock(str(i)),)))
                if i == 50:
                    await app.data.set_model_setting(scope, ModelSetting("removed-companion-model", None))
            recent = await app.recent_companion_conversation(ALICE)
            assert recent is not None and recent.conversation_id == older.conversation_id
            await app.data.trash_conversation(older)
            assert await app.recent_companion_conversation(ALICE) is None
        finally:
            await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("folded", [False, True])
def test_resuming_a_device_tool_round_cannot_lose_the_companion_gate(tmp_path: Path, folded: bool) -> None:
    async def run() -> None:
        provider = mixed_provider()
        provider.extend([calls(("inside/read_file", {"path": "missing.txt"})), final("finished")])
        app = await Application.open(tmp_path / "app", provider, model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)), sandbox=False)
        try:
            cid = await app.create_conversation(ALICE, initial_model="companion-model")
            scope = TrustedScope(ALICE, cid)
            driver = app.runtime.driver
            await driver.accept(scope, UserMessage((TextBlock("look"),), origin=MessageOrigin(OriginKind.DEVICE, "d1")))
            assert (await driver.resume(scope, max_runs=1)).state is DriveState.RUN_LIMIT_REACHED
            assert len(provider.sent) == 1
            if folded:
                state = await app.data.read_current_state_basis(scope)
                await app.data.replace_latest_snapshot(scope, expected_revision=None, schema_version=1,
                                                       body={"summary": "earlier device input"}, covers_through_sequence=1,
                                                       basis=PromptBasis(1, state.state_revision))
            await app.set_model(scope, "standard-model", None)
            done = await driver.resume(scope)
            assert done.state is DriveState.REQUEST_REJECTED and "companion_unsupported" in done.detail
            assert len(provider.sent) == 1
            await app.set_model(scope, "companion-model", None)
            assert (await driver.resume(scope)).state is DriveState.COMPLETED
            assert provider.prepared_inputs[-1].request_options.model_id == "companion-model"
        finally:
            await app.close()
    asyncio.run(run())


def test_companion_models_only_enforcement_at_startup(tmp_path: Path) -> None:
    async def run() -> None:
        provider = FakeProvider({
            "companion-a": ProviderCapabilities(
                "companion-a", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=True, companion=True, context_tokens=100_000,
            ),
            "standard-b": ProviderCapabilities(
                "standard-b", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=False, free_wake=False, companion=False, context_tokens=100_000,
            ),
        })

        # 1. Refuse if default model is not companion capable
        with pytest.raises(ValueError, match="does not satisfy companion requirements"):
            await Application.open(
                tmp_path / "app1",
                provider,
                model_id="standard-b",
                companion_models_only=True,
                sandbox=False,
            )

        # 2. Refuse if an offered model is not companion capable under companion_models_only
        with pytest.raises(ValueError, match="models not verified for companion mode offered under companion_models_only"):
            await Application.open(
                tmp_path / "app2",
                provider,
                model_id="companion-a",
                models=(("companion-a", None), ("standard-b", None)),
                companion_models_only=True,
                sandbox=False,
            )

        # 3. Allow when all models are companion capable
        app = await Application.open(
            tmp_path / "app3",
            provider,
            model_id="companion-a",
            models=(("companion-a", None),),
            companion_models_only=True,
            sandbox=False,
        )
        assert app.companion_models_only is True
        await app.close()

    asyncio.run(run())


def test_companion_models_only_set_model_refused(tmp_path: Path) -> None:
    async def run() -> None:
        provider = FakeProvider({
            "companion-a": ProviderCapabilities(
                "companion-a", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=True, companion=True, context_tokens=100_000,
            ),
            "companion-b": ProviderCapabilities(
                "companion-b", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=True, companion=True, context_tokens=100_000,
            ),
            "standard-c": ProviderCapabilities(
                "standard-c", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=False, free_wake=False, companion=False, context_tokens=100_000,
            ),
        })

        app = await Application.open(
            tmp_path / "app",
            provider,
            model_id="companion-a",
            models=(("companion-a", None), ("companion-b", None), ("standard-c", None)),
            companion_models_only=False,  # companion mode enabled, but strict only off
            sandbox=False,
        )
        cid1 = await app.data.create_conversation(ALICE)
        scope1 = TrustedScope(ALICE, cid1)

        # Without companion_models_only, standard-c is allowed
        choice = await app.set_model(scope1, "standard-c", None)
        assert choice.model_id == "standard-c"
        await app.close()

        # With companion_models_only, set_model refuses non-companion models
        app2 = await Application.open(
            tmp_path / "app2",
            provider,
            model_id="companion-a",
            models=(("companion-a", None), ("companion-b", None)),
            companion_models_only=True,
            sandbox=False,
        )
        cid2 = await app2.data.create_conversation(ALICE)
        scope2 = TrustedScope(ALICE, cid2)

        # companion-b is allowed
        choice_b = await app2.set_model(scope2, "companion-b", None)
        assert choice_b.model_id == "companion-b"

        # standard-c is not offered -> unknown_model
        with pytest.raises(ModelRefused) as exc_info:
            await app2.set_model(scope2, "standard-c", None)
        assert exc_info.value.code == "unknown_model"

        # If a non-companion model were somehow in offers, companion_unsupported is raised
        app2.companion_models = ("companion-a",)
        with pytest.raises(ModelRefused) as exc_info2:
            await app2.set_model(scope2, "companion-b", None)
        assert exc_info2.value.code == "companion_unsupported"

        await app2.close()

    asyncio.run(run())


def test_provider_capabilities_companion_invariant() -> None:
    """P2: companion=True without all 5 companion capabilities must raise ValueError."""
    # Lacks free_wake and speaks_first:
    with pytest.raises(ValueError, match="cannot declare companion=True without all companion capabilities: speaks_first, free_wake"):
        ProviderCapabilities(
            "incomplete-1",
            image_input=True,
            screen_points=True,
            screen_tool=True,
            speaks_first=False,
            free_wake=False,
            companion=True,
        )

    # Valid model with all 5 capabilities passes:
    valid = ProviderCapabilities(
        "complete-1",
        image_input=True,
        screen_points=True,
        screen_tool=True,
        speaks_first=True,
        free_wake=True,
        companion=True,
    )
    assert valid.companion is True
    assert valid.companion_capable is True

    # P2: DeepFrozen.__post_init__ preserves deep-freezing of mutable collections
    mutable_efforts = {"low", "high"}
    frozen_caps = ProviderCapabilities(
        "complete-2",
        reasoning_efforts=mutable_efforts,  # type: ignore[arg-type]
        image_input=True,
        screen_points=True,
        screen_tool=True,
        speaks_first=True,
        free_wake=True,
        companion=True,
    )
    assert isinstance(frozen_caps.reasoning_efforts, frozenset)
    assert frozen_caps.reasoning_efforts == frozenset({"low", "high"})


def test_companion_default_model_and_device_conversation(tmp_path: Path) -> None:
    """Option A: companion devices default to companion_default_model on new conversation."""
    async def run() -> None:
        provider = FakeProvider({
            "companion-deepseek": ProviderCapabilities(
                "companion-deepseek", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=True, companion=True, context_tokens=100_000,
            ),
            "standard-luna": ProviderCapabilities(
                "standard-luna", image_input=True, screen_points=True, screen_tool=True,
                speaks_first=True, free_wake=False, companion=False, context_tokens=100_000,
            ),
        })

        # 1. Reject if companion_default_model is an offered model that is not companion capable
        with pytest.raises(ValueError, match="the companion default model 'standard-luna' does not satisfy companion requirements"):
            await Application.open(
                tmp_path / "app_err",
                provider,
                model_id="standard-luna",
                models=(("standard-luna", None), ("companion-deepseek", None)),
                companion_default_model="standard-luna",
                sandbox=False,
            )

        # 2. Server with standard-luna default and companion-deepseek for companion devices
        app = await Application.open(
            tmp_path / "app_ok",
            provider,
            model_id="standard-luna",
            models=(("standard-luna", None), ("companion-deepseek", None)),
            companion_default_model="companion-deepseek",
            sandbox=False,
        )
        assert app.model_id == "standard-luna"
        assert app.companion_default_model == "companion-deepseek"

        # Web / CLI conversation (initial_model is None) follows server default model (standard-luna)
        web_cid = await app.create_conversation(ALICE)
        web_choice = await app.model_choice(TrustedScope(ALICE, web_cid))
        assert web_choice.model_id == "standard-luna"
        assert web_choice.chosen is None

        # Device conversation (initial_model set to companion_default_model) uses companion-deepseek
        device_cid = await app.create_conversation(ALICE, initial_model=app.companion_default_model)
        device_choice = await app.model_choice(TrustedScope(ALICE, device_cid))
        assert device_choice.model_id == "companion-deepseek"
        assert device_choice.chosen is not None
        assert device_choice.chosen.model_id == "companion-deepseek"
        assert web_choice.fallback is None
        assert device_choice.fallback is None

        # Verify actual Run execution records the model_id of the chosen model
        from testkit import final
        provider.extend([final("안녕!"), final("반가워!")])

        web_scope = TrustedScope(ALICE, web_cid)
        await app.send(web_scope, text="hi from web")
        while app.runner.status(web_scope).running:
            await asyncio.sleep(0.01)
        web_events = await app.data.read_events(web_scope)
        web_run_id = next(e.origin_run_id for e in web_events if e.origin_run_id is not None)
        web_run = await app.data.read_run(web_scope, web_run_id)
        assert web_run.model_id == "standard-luna"

        device_scope = TrustedScope(ALICE, device_cid)
        await app.send(device_scope, text="hi from device")
        while app.runner.status(device_scope).running:
            await asyncio.sleep(0.01)
        device_events = await app.data.read_events(device_scope)
        device_run_id = next(e.origin_run_id for e in device_events if e.origin_run_id is not None)
        device_run = await app.data.read_run(device_scope, device_run_id)
        assert device_run.model_id == "companion-deepseek"

        await app.close()

    asyncio.run(run())
