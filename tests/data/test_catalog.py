"""ConversationCatalog (DATA-12, T0) contract, on every backend."""

from __future__ import annotations

import asyncio

import pytest
from testkit import FAKE, data_for, new_store, restart, user

from himari.common import ConversationId, EnvironmentKind, PrincipalId, TrustedScope, WorkspaceId
from himari.data.api import MAX_LIST_LIMIT, MAX_TITLE_CHARS, PREVIEW_CHARS, DataError, DataErrorCode, InitialActivation

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
OTHER = EnvironmentKind("other")


def test_create_is_one_t0_with_its_activations() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        cid = await data.create_conversation(ALICE, title="  Plan   the\ttrip ",
                                             activations=(InitialActivation(FAKE, WorkspaceId("ws_a")),))
        scope = TrustedScope(ALICE, cid)
        summary = await data.read_conversation(scope)
        assert summary.conversation_id == cid and summary.title == "Plan the trip"
        assert summary.last_sequence == 0 and summary.preview == "" and summary.last_activity_at == summary.created_at
        workspace = await data.resolve_conversation_workspace(scope)
        assert workspace.workspace_id == "ws_a" and workspace.cwd_relative == f"conversations/{cid}"
        binding = await data.resolve_default_binding(scope, FAKE)
        assert binding is not None and binding.cwd_relative == f"conversations/{cid}"
        second = await data.create_conversation(ALICE)
        assert second != cid and (await data.read_conversation(TrustedScope(ALICE, second))).title is None

    asyncio.run(scenario())


@pytest.mark.parametrize("title, activations", [
    ("x" * (MAX_TITLE_CHARS + 1), ()),
    ("ok", (InitialActivation(FAKE, WorkspaceId("a")), InitialActivation(FAKE, WorkspaceId("b")))),
])
def test_invalid_create_creates_nothing(title: str, activations: tuple[InitialActivation, ...]) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with pytest.raises(DataError) as exc:
            await data.create_conversation(ALICE, title=title, activations=activations)
        assert exc.value.code is DataErrorCode.INVALID_ARGUMENT
        assert await data.list_conversations(ALICE) == ()

    asyncio.run(scenario())


def test_list_is_own_conversations_by_latest_activity() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        first = await data.create_conversation(ALICE, title="first")
        second = await data.create_conversation(ALICE, title="second")
        await data.create_conversation(BOB, title="bob's")
        assert [s.conversation_id for s in await data.list_conversations(ALICE)] == [second, first]  # tie: newest first

        await data.append_event(TrustedScope(ALICE, first), user("hello   there\nfriend"))
        listed = await data.list_conversations(ALICE)
        assert [s.conversation_id for s in listed] == [first, second]
        assert listed[0].preview == "hello there friend" and listed[0].last_sequence == 1
        assert listed[0].last_activity_at > listed[0].created_at
        assert [s.title for s in await data.list_conversations(BOB)] == ["bob's"]

        assert [s.conversation_id for s in await data.list_conversations(ALICE, limit=1, offset=1)] == [second]
        for limit, offset in [(0, 0), (MAX_LIST_LIMIT + 1, 0), (1, -1)]:
            with pytest.raises(DataError) as exc:
                await data.list_conversations(ALICE, limit=limit, offset=offset)
            assert exc.value.code is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_preview_is_the_first_user_message_shortened() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        cid = await data.create_conversation(ALICE)
        await data.append_event(TrustedScope(ALICE, cid), user("y" * 500))
        await data.append_event(TrustedScope(ALICE, cid), user("later"))
        preview = (await data.read_conversation(TrustedScope(ALICE, cid))).preview
        assert len(preview) == PREVIEW_CHARS and preview.endswith("…") and preview.startswith("yyy")

    asyncio.run(scenario())


def test_foreign_conversations_are_unreachable() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        cid = await data.create_conversation(ALICE, title="secret")
        for call in (data.read_conversation(TrustedScope(BOB, cid)),
                     data.rename_conversation(TrustedScope(BOB, cid), "mine now"),
                     data.read_conversation(TrustedScope(ALICE, ConversationId("conv_missing")))):
            with pytest.raises(DataError) as exc:
                await call
            assert exc.value.code is DataErrorCode.NOT_AUTHORIZED
        assert (await data.read_conversation(TrustedScope(ALICE, cid))).title == "secret"

    asyncio.run(scenario())


def test_rename_and_ids_survive_restart() -> None:
    async def scenario() -> tuple[ConversationId, ConversationId]:
        store = new_store()
        data = data_for(store)
        cid = await data.create_conversation(ALICE, title="old")
        await data.rename_conversation(TrustedScope(ALICE, cid), "  new\nname ")
        with pytest.raises(DataError):
            await data.rename_conversation(TrustedScope(ALICE, cid), "z" * (MAX_TITLE_CHARS + 1))
        reopened = data_for(restart(store))
        assert (await reopened.read_conversation(TrustedScope(ALICE, cid))).title == "new name"
        await reopened.rename_conversation(TrustedScope(ALICE, cid), "   ")
        assert (await reopened.read_conversation(TrustedScope(ALICE, cid))).title is None
        return cid, await reopened.create_conversation(ALICE)

    old, new = asyncio.run(scenario())
    assert old != new  # ids are never reused across a restart
