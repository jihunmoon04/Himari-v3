"""ExternalSourceSettings (V3-P53) contract, on every backend."""

from __future__ import annotations

import asyncio

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TrustedScope
from himari.data.api import MAX_EXTERNAL_SETTINGS, DataError, DataErrorCode, ExternalSourceSetting

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


def test_choices_are_kept_per_conversation_and_survive_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        first = TrustedScope(ALICE, await data.create_conversation(ALICE))
        second = TrustedScope(ALICE, await data.create_conversation(ALICE))
        revision = (await data.read_current_state_basis(first)).state_revision
        await data.set_external_setting(first, "zeta", True)
        await data.set_external_setting(first, "calliope", False)
        await data.set_external_setting(first, "calliope", True)  # replaces the choice
        assert await data.read_external_settings(first) == (ExternalSourceSetting("calliope", True),
                                                            ExternalSourceSetting("zeta", True))
        assert await data.read_external_settings(second) == ()
        assert (await data.read_current_state_basis(first)).state_revision == revision  # the catalog's key covers it

        data = data_for(store := restart(store))
        await data.set_external_setting(first, "zeta", None)  # back to the default
        await data.set_external_setting(first, "never-set", None)  # removing nothing is fine
        assert await data.read_external_settings(first) == (ExternalSourceSetting("calliope", True),)

    asyncio.run(scenario())


@pytest.mark.parametrize("source, enabled", [
    ("a/b", True), ("", True), ("bad name", False), ("x" * 65, True), ("ok", "yes"), (3, True),
])
def test_invalid_choices_are_refused(source: object, enabled: object) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        with pytest.raises(DataError) as error:
            await data.set_external_setting(scope, source, enabled)  # type: ignore[arg-type]
        assert error.value.code is DataErrorCode.INVALID_ARGUMENT
        assert await data.read_external_settings(scope) == ()

    asyncio.run(scenario())


def test_the_number_of_choices_is_bounded() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        for i in range(MAX_EXTERNAL_SETTINGS):
            await data.set_external_setting(scope, f"s{i}", True)
        await data.set_external_setting(scope, "s0", False)  # changing one is not adding one
        with pytest.raises(DataError):
            await data.set_external_setting(scope, "one-more", True)
        await data.set_external_setting(scope, "s1", None)
        await data.set_external_setting(scope, "one-more", True)

    asyncio.run(scenario())


def test_foreign_conversations_are_unreachable() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = TrustedScope(ALICE, await data.create_conversation(ALICE))
        foreign = TrustedScope(BOB, scope.conversation_id)
        for attempt in (data.read_external_settings(foreign), data.set_external_setting(foreign, "calliope", True)):
            with pytest.raises(DataError) as error:
                await attempt
            assert error.value.code is DataErrorCode.NOT_AUTHORIZED
        assert await data.read_external_settings(scope) == ()

    asyncio.run(scenario())
