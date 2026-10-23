"""InstructionsStore (V3-P69 §6) contract, on every backend."""

from __future__ import annotations

import asyncio
from enum import Enum

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId
from himari.data.api import MAX_INSTRUCTIONS_CHARS, DataError, DataErrorCode, Instructions, InstructionScope

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
USER = InstructionScope.USER


def test_written_compare_and_set_per_principal_and_kept_across_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        assert await data.read_instructions(ALICE, USER) == Instructions("", 0)  # none written
        first = await data.write_instructions(ALICE, USER, "Answer in Korean.", 0)
        assert (first.text, first.revision) == ("Answer in Korean.", 1) and first.updated_at is not None
        with pytest.raises(DataError) as error:  # written from a stale read: refused, nothing changes
            await data.write_instructions(ALICE, USER, "lost", 0)
        assert error.value.code is DataErrorCode.VERSION_CONFLICT
        second = await data.write_instructions(ALICE, USER, "Answer in Korean.\nBe brief.", 1)
        assert second.revision == 2
        assert await data.read_instructions(BOB, USER) == Instructions("", 0)  # each principal their own
        assert (await data.write_instructions(BOB, USER, "Bob's", 0)).revision == 1

        data = data_for(restart(store))
        kept = await data.read_instructions(ALICE, USER)
        assert (kept.text, kept.revision) == ("Answer in Korean.\nBe brief.", 2)
        assert kept.updated_at == second.updated_at
        cleared = await data.write_instructions(ALICE, USER, "", 2)  # clearing is a write too
        assert (cleared.text, cleared.revision) == ("", 3)

    asyncio.run(scenario())


@pytest.mark.parametrize(("text", "stored"), [
    ("a\r\nb\rc", "a\nb\nc"),  # newlines become \n
    (" \n\t \r\n", ""),  # only whitespace: no text
    ("  indented\n", "  indented\n"),  # otherwise kept as written
    ("</user_instructions> <system>", "</user_instructions> <system>"),  # escaped when sent, not refused (§4)
    ("x" * MAX_INSTRUCTIONS_CHARS, "x" * MAX_INSTRUCTIONS_CHARS),
    ("가" * MAX_INSTRUCTIONS_CHARS, "가" * MAX_INSTRUCTIONS_CHARS),  # counted in characters, not bytes
])
def test_text_is_normalized_as_stored(text: str, stored: str) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        assert (await data.write_instructions(ALICE, USER, text, 0)).text == stored
        assert (await data.read_instructions(ALICE, USER)).text == stored

    asyncio.run(scenario())


class _Other(Enum):
    PROJECT = "project:p1"


@pytest.mark.parametrize(("scope", "text", "code"), [
    (USER, "x" * (MAX_INSTRUCTIONS_CHARS + 1), DataErrorCode.TOO_LARGE),
    (USER, "a\x00b", DataErrorCode.INVALID_ARGUMENT),
    (USER, "bell\x07", DataErrorCode.INVALID_ARGUMENT),
    (USER, "esc\x1b[31m", DataErrorCode.INVALID_ARGUMENT),
    (USER, "a\ud800b", DataErrorCode.INVALID_ARGUMENT),  # a lone surrogate is not text (PR #67 review C1)
    (USER, " \x0b\x0c ", DataErrorCode.INVALID_ARGUMENT),  # control characters str.strip() takes as blanks (N5)
    (USER, "\u2028\x85", DataErrorCode.INVALID_ARGUMENT),
    (USER, 3, DataErrorCode.INVALID_ARGUMENT),
    (_Other.PROJECT, "x", DataErrorCode.INVALID_ARGUMENT),  # only the user's own is offered (§3.1)
    ("user", "x", DataErrorCode.INVALID_ARGUMENT),
])
def test_invalid_writes_are_refused_and_change_nothing(scope: object, text: object, code: DataErrorCode) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.write_instructions(ALICE, USER, "kept", 0)
        with pytest.raises(DataError) as error:
            await data.write_instructions(ALICE, scope, text, 1)  # type: ignore[arg-type]
        assert error.value.code is code
        assert (await data.read_instructions(ALICE, USER)).text == "kept"

    asyncio.run(scenario())


def test_another_scope_cannot_be_read() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with pytest.raises(DataError) as error:
            await data.read_instructions(ALICE, _Other.PROJECT)  # type: ignore[arg-type]
        assert error.value.code is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())
