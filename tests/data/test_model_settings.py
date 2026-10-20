"""ConversationModelSettings (V3-P64) contract, on every backend."""

from __future__ import annotations

import asyncio

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TrustedScope
from himari.data.api import MAX_EFFORT_CHARS, MAX_MODEL_ID_CHARS, DataError, DataErrorCode, ModelSetting

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


def test_a_choice_is_kept_as_made_per_conversation_and_survives_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        first = TrustedScope(ALICE, await data.create_conversation(ALICE))
        second = TrustedScope(ALICE, await data.create_conversation(ALICE))
        revision = (await data.read_current_state_basis(first)).state_revision
        assert await data.read_model_setting(first) is None
        await data.set_model_setting(first, ModelSetting("openai/gpt-6-luna", "high"))
        await data.set_model_setting(first, ModelSetting("meta/some-model:free"))  # replaces it, effort included
        assert await data.read_model_setting(first) == ModelSetting("meta/some-model:free", None)
        assert await data.read_model_setting(second) is None
        assert (await data.read_current_state_basis(first)).state_revision == revision  # not part of the Prompt

        data = data_for(store := restart(store))
        assert await data.read_model_setting(first) == ModelSetting("meta/some-model:free", None)
        await data.set_model_setting(first, None)  # back to the default
        await data.set_model_setting(first, None)  # removing nothing is fine
        assert await data.read_model_setting(first) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("setting", [
    ModelSetting(""), ModelSetting("a b"), ModelSetting("x" * (MAX_MODEL_ID_CHARS + 1)), ModelSetting("모델"),
    ModelSetting("m\n"), ModelSetting(3),  # type: ignore[arg-type]
    ModelSetting("m", ""), ModelSetting("m", "x" * (MAX_EFFORT_CHARS + 1)), ModelSetting("m", "hi gh"),
    ModelSetting("m", 1),  # type: ignore[arg-type]
    "openai/gpt-6-luna",
])
def test_invalid_choices_are_refused(setting: object) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        await data.set_model_setting(scope, ModelSetting("kept"))
        with pytest.raises(DataError) as error:
            await data.set_model_setting(scope, setting)  # type: ignore[arg-type]
        assert error.value.code is DataErrorCode.INVALID_ARGUMENT
        assert await data.read_model_setting(scope) == ModelSetting("kept")

    asyncio.run(scenario())


def test_the_longest_values_are_accepted() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        longest = ModelSetting("m" * MAX_MODEL_ID_CHARS, "e" * MAX_EFFORT_CHARS)
        await data.set_model_setting(scope, longest)
        assert await data.read_model_setting(scope) == longest

    asyncio.run(scenario())


def test_foreign_conversations_are_unreachable() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        foreign = TrustedScope(BOB, scope.conversation_id)
        for attempt in (data.read_model_setting(foreign), data.set_model_setting(foreign, ModelSetting("m"))):
            with pytest.raises(DataError) as error:
                await attempt
            assert error.value.code is DataErrorCode.NOT_AUTHORIZED
        assert await data.read_model_setting(scope) is None

    asyncio.run(scenario())
