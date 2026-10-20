"""The deployment catalog uses provider limits and persists a valid choice."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from himari.bootstrap.application import Application, ModelRefused
from himari.common import PrincipalId, TrustedScope
from himari.provider.openrouter import OpenRouterAdapter


def test_offered_models_fit_the_deployment_budget_and_keep_their_choice(tmp_path: Path) -> None:
    async def scenario() -> None:
        models = [(model, None) for model in (
            "openai/gpt-6-luna", "openai/gpt-6.1-sol", "anthropic/claude-sonnet-5.5",
            "xiaomi/mimo-v2.6-pro", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash",
            "minimax/minimax-m3",
        )]
        # No request is made: opening and changing a model are local operations.
        app = await Application.open(tmp_path / "data", OpenRouterAdapter("unused"),
                                     model_id=models[0][0], models=models, context_tokens=200_000, sandbox=False)
        scope = TrustedScope(PrincipalId("operator"), await app.create_conversation(PrincipalId("operator")))
        try:
            assert not app.unverified_models
            for model, _ in models:
                await app.set_model(scope, model, None)
                status = await app.context_status(scope)
                assert status is not None and status.budget_tokens == 200_000
                assert status.hard_limit_tokens is not None and status.hard_limit_tokens >= 200_000
            # The top MiniMax provider exposes 512K, despite the global 1M label.
            status = await app.context_status(scope)
            assert status is not None and status.hard_limit_tokens == 262_144
            with pytest.raises(ModelRefused):
                await app.set_model(scope, "xiaomi/mimo-v2.6-pro", "high")
            assert (await app.model_choice(scope)).model_id == "minimax/minimax-m3"
        finally:
            await app.close()
        app = await Application.open(tmp_path / "data", OpenRouterAdapter("unused"),
                                     model_id=models[0][0], models=models, context_tokens=200_000, sandbox=False)
        try:
            assert (await app.model_choice(scope)).model_id == "minimax/minimax-m3"
        finally:
            await app.close()

    asyncio.run(scenario())
