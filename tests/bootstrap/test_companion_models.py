"""Companion model support and gating tests (V3-P83, P84, P87, P90, P91)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from himari.bootstrap.application import Application, ModelRefused
from himari.common import PrincipalId, TrustedScope
from himari.provider.api import ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES
from himari.provider.scripted import ScriptedAdapter

ALICE = PrincipalId("alice")


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
