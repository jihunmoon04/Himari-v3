"""PersonaStore (V3-P108 §5.2) contract, on every backend."""

from __future__ import annotations

import asyncio

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId
from himari.data.api import (
    MAX_PERSONA_CHARS,
    MAX_PERSONA_NAME_CHARS,
    MAX_PERSONAS,
    DataError,
    DataErrorCode,
    PersonaChoice,
)

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


def test_made_edited_and_kept_across_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        listed = await data.list_personas(ALICE)
        assert listed.personas == () and listed.choice == PersonaChoice(None, 0)
        assert await data.active_persona(ALICE) is None
        teacher = await data.create_persona(ALICE, "  Teacher ", "a\r\nb")
        assert (teacher.persona_id, teacher.name, teacher.text, teacher.revision) == (1, "Teacher", "a\nb", 1)
        friend = await data.create_persona(ALICE, "Friend", " \n ")  # only whitespace: no text yet
        assert (friend.persona_id, friend.text) == (2, "")
        edited = await data.update_persona(ALICE, 2, "Friend", "반말로.", 1)
        assert edited.revision == 2 and edited.created_at == friend.created_at
        with refused(DataErrorCode.VERSION_CONFLICT):  # edited from a stale read: nothing changes
            await data.update_persona(ALICE, 2, "Friend", "lost", 1)

        data = data_for(restart(store))
        kept = await data.list_personas(ALICE)
        assert [(p.persona_id, p.name, p.text, p.revision) for p in kept.personas] == [
            (1, "Teacher", "a\nb", 1), (2, "Friend", "반말로.", 2)]
        assert kept.personas[1].updated_at == edited.updated_at
        assert (await data.list_personas(BOB)).personas == ()  # each principal their own

    asyncio.run(scenario())


def test_choosing_and_the_active_persona() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        teacher = await data.create_persona(ALICE, "Teacher", "Call me 선생님.")
        friend = await data.create_persona(ALICE, "Friend", "Call me 친구.")
        first = await data.choose_persona(ALICE, teacher.persona_id, 0)
        assert (first.persona_id, first.revision) == (1, 1) and first.updated_at is not None
        assert await data.active_persona(ALICE) == teacher
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.choose_persona(ALICE, friend.persona_id, 0)
        with refused(DataErrorCode.NOT_FOUND):
            await data.choose_persona(ALICE, 9, 1)
        with refused(DataErrorCode.NOT_FOUND):  # another principal's persona is not one's own
            await data.choose_persona(BOB, teacher.persona_id, 0)
        edited = await data.update_persona(ALICE, teacher.persona_id, "Teacher", "Call me 쌤.", 1)
        assert await data.active_persona(ALICE) == edited  # the text as it is now
        none = await data.choose_persona(ALICE, None, 1)
        assert (none.persona_id, none.revision) == (None, 2) and await data.active_persona(ALICE) is None
        assert await data.active_persona(BOB) is None

    asyncio.run(scenario())


def test_deleting_the_chosen_persona_leaves_none_chosen_and_ids_are_not_reused() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        teacher = await data.create_persona(ALICE, "Teacher", "x")
        friend = await data.create_persona(ALICE, "Friend", "y")
        await data.choose_persona(ALICE, teacher.persona_id, 0)
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.delete_persona(ALICE, friend.persona_id, 2)
        unchanged = await data.delete_persona(ALICE, friend.persona_id, 1)  # not the chosen one
        assert (unchanged.persona_id, unchanged.revision) == (teacher.persona_id, 1)
        after = await data.delete_persona(ALICE, teacher.persona_id, 1)
        assert (after.persona_id, after.revision) == (None, 2)
        assert (await data.list_personas(ALICE)).choice.persona_id is None
        assert await data.active_persona(ALICE) is None
        with refused(DataErrorCode.NOT_FOUND):
            await data.delete_persona(ALICE, teacher.persona_id, 1)
        again = await data.create_persona(ALICE, "Teacher", "x")  # the name is free again
        assert again.persona_id == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("taken", ["Teacher", "teacher", " TEACHER "])
def test_names_are_unique_per_principal(taken: str) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.create_persona(ALICE, "Teacher", "")
        other = await data.create_persona(ALICE, "Other", "")
        with refused(DataErrorCode.DUPLICATE_IDENTITY):
            await data.create_persona(ALICE, taken, "")
        with refused(DataErrorCode.DUPLICATE_IDENTITY):
            await data.update_persona(ALICE, other.persona_id, taken, "", 1)
        assert (await data.create_persona(BOB, taken, "")).persona_id == 1  # another principal's names apart
        renamed = await data.update_persona(ALICE, 1, "TEACHER", "", 1)  # its own name, in another case
        assert renamed.name == "TEACHER"

    asyncio.run(scenario())


def test_nfc_names_are_the_same_name() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.create_persona(ALICE, "히마리", "")  # 히마리, decomposed
        with refused(DataErrorCode.DUPLICATE_IDENTITY):
            await data.create_persona(ALICE, "히마리", "")

    asyncio.run(scenario())


@pytest.mark.parametrize(("name", "text", "code"), [
    ("", "x", DataErrorCode.INVALID_ARGUMENT),
    ("   ", "x", DataErrorCode.INVALID_ARGUMENT),
    ("n" * (MAX_PERSONA_NAME_CHARS + 1), "x", DataErrorCode.INVALID_ARGUMENT),
    ("bell\x07", "x", DataErrorCode.INVALID_ARGUMENT),
    ("tab\tname", "x", DataErrorCode.INVALID_ARGUMENT),
    ("ok", "bell\x07", DataErrorCode.INVALID_ARGUMENT),
    ("ok", "가" * (MAX_PERSONA_CHARS + 1), DataErrorCode.TOO_LARGE),
])
def test_refused_names_and_texts(name: str, text: str, code: DataErrorCode) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with refused(code):
            await data.create_persona(ALICE, name, text)
        made = await data.create_persona(ALICE, "ok2", "")
        with refused(code):
            await data.update_persona(ALICE, made.persona_id, name, text, 1)
        assert [p.name for p in (await data.list_personas(ALICE)).personas] == ["ok2"]

    asyncio.run(scenario())


def test_the_longest_name_and_text_are_kept() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = await data.create_persona(ALICE, "가" * MAX_PERSONA_NAME_CHARS, "가" * MAX_PERSONA_CHARS)
        assert len(made.name) == MAX_PERSONA_NAME_CHARS and len(made.text) == MAX_PERSONA_CHARS

    asyncio.run(scenario())


def test_at_most_max_personas() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        for n in range(MAX_PERSONAS):
            await data.create_persona(ALICE, f"P{n}", "")
        with refused(DataErrorCode.LIMIT_EXCEEDED):
            await data.create_persona(ALICE, "One more", "")
        await data.delete_persona(ALICE, 1, 1)
        assert (await data.create_persona(ALICE, "One more", "")).persona_id == MAX_PERSONAS + 1

    asyncio.run(scenario())


def test_an_id_that_is_not_an_int_is_not_found() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.create_persona(ALICE, "Teacher", "")
        for bad in (True, "1", 1.0, 0, -1, 1 << 63, 10**20):  # review: one SQLite cannot hold is no persona
            with refused(DataErrorCode.NOT_FOUND):
                await data.update_persona(ALICE, bad, "x", "", 1)  # type: ignore[arg-type]
            with refused(DataErrorCode.NOT_FOUND):
                await data.choose_persona(ALICE, bad, 0)  # type: ignore[arg-type]
            with refused(DataErrorCode.NOT_FOUND):
                await data.delete_persona(ALICE, bad, 1)  # type: ignore[arg-type]
        with refused(DataErrorCode.INVALID_ARGUMENT):  # both stores check the name before the id
            await data.update_persona(ALICE, 999, "", "x", 1)

    asyncio.run(scenario())


def test_a_persona_is_saved_from_its_fields_and_its_text_is_made_from_them() -> None:
    """character-persona-fields §3: filled fields only, in their order; ``label: value`` lines (several lines under
    the label), each hidden story line marked for its stage, free last as written; the same fields, the same text."""
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        fields = {"free": "자유 글.", "speech": " 반말 ", "name": "루시", "examples": "- 왔어?\r\n- 밥은?",
                  "familiar": "사실 해커였다.\n\n[close] 이미 표시된 줄", "close": "  세계를 구하러 왔다.", "habits": "  "}
        made = await data.create_persona(ALICE, "Lucy", "ignored when fields are given", fields)
        assert dict(made.fields) == {"name": "루시", "speech": "반말", "examples": "- 왔어?\n- 밥은?",
                                     "familiar": "사실 해커였다.\n\n[close] 이미 표시된 줄",
                                     "close": "세계를 구하러 왔다.", "free": "자유 글."}
        assert list(made.fields) == ["name", "speech", "examples", "familiar", "close", "free"]
        assert made.text == ("이름: 루시\n말투: 반말\n대사 예시:\n- 왔어?\n- 밥은?\n"
                             "[familiar] 사실 해커였다.\n[familiar] 이미 표시된 줄\n[close] 세계를 구하러 왔다.\n자유 글.")
        again = await data.update_persona(ALICE, 1, "Lucy", "", 1, dict(made.fields))
        assert again.text == made.text and again.fields == made.fields
        only_free = await data.update_persona(ALICE, 1, "Lucy", "", 2, {"free": "  as written \n"})
        assert only_free.text == "  as written \n"  # free alone is the text itself (P3)
        texted = await data.update_persona(ALICE, 1, "Lucy", "옛 클라이언트의 글.", 3)
        assert dict(texted.fields) == {"free": "옛 클라이언트의 글."}  # text alone: the field free
        data = data_for(restart(store))
        assert (await data.list_personas(ALICE)).personas == (texted,)

    asyncio.run(scenario())


def test_a_persona_with_an_unknown_field_or_too_long_a_made_text_is_refused() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.create_persona(ALICE, "Lucy", "", {"age": "17"})
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.create_persona(ALICE, "Lucy", "", {"name": "a\x00b"})
        with refused(DataErrorCode.TOO_LARGE):  # each fits, the text made from them does not
            await data.create_persona(ALICE, "Lucy", "", {"world": "x" * (MAX_PERSONA_CHARS // 2),
                                                          "free": "y" * (MAX_PERSONA_CHARS // 2)})
        assert (await data.list_personas(ALICE)).personas == ()

    asyncio.run(scenario())
