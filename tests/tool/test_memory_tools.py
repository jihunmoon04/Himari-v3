"""The memory Tools (V3-P71 §6) on their own: what they tell the model and
the change they hand to Data. Committing that change is Data's
(tests/data/test_memories.py) and the Application's (tests/bootstrap)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from testkit import SCOPE, data_for, new_store

from himari.common import CallId, TextBlock, ToolId
from himari.data.api import MAX_MEMORY_CHARS, MemoryCategory, MemoryChange, MemoryChangeKind
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.schema import validate
from himari.tool.memory import SEARCH_LIMIT, MemoryForget, MemorySave, MemorySearch, memory_tools, note_id

ME = SCOPE.principal_id


def fresh() -> Any:
    store = new_store()
    store.create_conversation(SCOPE)
    return data_for(store)


def run(tool: Any, data: Any, **arguments: Any) -> ToolOutcome:
    invocation = ToolInvocation(tool.definition.tool_id, CallId("c0"), arguments)
    outcome: ToolOutcome = asyncio.run(tool.run(invocation, ToolContext(SCOPE, data)))
    return outcome


def said(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def test_the_tools_are_internal_and_their_effects_declared() -> None:
    save, forget, search = memory_tools(fresh())
    assert [t.definition.tool_id for t in (save, forget, search)] == ["memory_save", "memory_forget", "memory_search"]
    assert all(t.definition.environment_kind is None and t.definition.external_source is None
               for t in (save, forget, search))
    assert [t.definition.effect_profile for t in (save, forget, search)] == [
        EffectProfile.SCOPED_WRITE, EffectProfile.SCOPED_WRITE, EffectProfile.READ]


@pytest.mark.parametrize(("raw", "parsed"), [("m12", 12), ("12", 12), (" m3 ", 3), ("m0", None), ("x1", None),
                                             (12, None), ("m" + "9" * 11, None)])
def test_note_ids(raw: object, parsed: int | None) -> None:
    assert note_id(raw) == parsed


def test_a_new_note_is_handed_over_with_an_allocated_id() -> None:
    data = fresh()
    outcome = run(MemorySave(data), data, category="preference", text="  Short lists.\n")
    assert outcome.status is ToolStatus.SUCCEEDED and said(outcome) == "Saved m1 (preference)."
    assert outcome.memory_change == MemoryChange(MemoryChangeKind.SAVE, 1, MemoryCategory.PREFERENCE, "Short lists.")
    assert outcome.raw_result == {"memory": {"id": 1, "category": "preference", "text": "Short lists.", "revision": 1},
                                  "previous": None}
    assert asyncio.run(data.list_memories(ME)) == ()  # nothing is written until the result is committed


def test_a_rewrite_names_the_revision_it_read_and_keeps_the_old_text() -> None:
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.PROJECT, "Lives in Seoul."))
    outcome = run(MemorySave(data), data, category="project", text="Moves to Busan.", replaces="m1")
    assert said(outcome) == "Updated m1 (project)."
    assert outcome.memory_change == MemoryChange(MemoryChangeKind.SAVE, 1, MemoryCategory.PROJECT, "Moves to Busan.",
                                                 expected_revision=1)
    assert outcome.raw_result["previous"] == {  # type: ignore[index, call-overload]
        "id": 1, "category": "project", "text": "Lives in Seoul.", "revision": 1, "origin": "user", "source": None}
    missing = run(MemorySave(data), data, category="project", text="x", replaces="m9")
    assert missing.status is ToolStatus.FAILED and said(missing) == "No note m9." and missing.memory_change is None


@pytest.mark.parametrize("unused", ["", "  ", None])
def test_an_unused_optional_argument_sent_empty_is_left_out(unused: object) -> None:
    # Seen with a real model (2026-10-24): "replaces": "" for a new note.
    data = fresh()
    outcome = run(MemorySave(data), data, category="user", text="Uses Go.", replaces=unused)
    assert said(outcome) == "Saved m1 (user)."
    assert said(run(MemorySearch(data), data, query="", category=unused)) == "No saved notes match."


def test_line_endings_are_made_lf_as_the_store_does() -> None:
    # Review C1: a CR used to be refused as a control character before the store's own fix-up.
    data = fresh()
    outcome = run(MemorySave(data), data, category="user", text="Uses Go.\r\nWrites Python.\r")
    assert outcome.memory_change is not None and outcome.memory_change.text == "Uses Go.\nWrites Python."


@pytest.mark.parametrize(("tool", "arguments"), [
    (MemorySave, {"category": "user", "text": "Uses Go.", "replaces": None}),
    (MemorySave, {"category": "user", "text": "Uses Go.", "replaces": ""}),
    (MemorySearch, {"query": None, "category": None}),
    (MemorySearch, {"query": "go", "category": None}),
])
def test_the_schema_takes_an_unused_optional_argument_sent_as_null(tool: Any, arguments: dict[str, Any]) -> None:
    # Integration review C1: the Dispatcher validates the schema before run(), so the
    # handler's "null is left out" must be allowed by the schema too.
    assert validate(arguments, tool.definition.input_schema) == []
    assert validate({**arguments, "category": "mood"}, tool.definition.input_schema) != []


def test_the_same_note_again_changes_nothing() -> None:
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.USER, "Uses Go."))
    outcome = run(MemorySave(data), data, category="user", text="uses go.")
    assert said(outcome) == "Already saved as m1." and outcome.memory_change is None
    other = run(MemorySave(data), data, category="project", text="Uses Go.")  # another category is another note
    assert other.memory_change is not None


@pytest.mark.parametrize(("arguments", "message"), [
    ({"category": "mood", "text": "x"}, "category is one of user, preference, project, reference"),
    ({"category": "user", "text": "  "}, "text is one or two sentences about the user"),
    ({"category": "user"}, "text is one or two sentences about the user"),
    ({"category": "user", "text": "bell\x07"}, "text may not contain control characters"),
    ({"category": "user", "text": "x" * (MAX_MEMORY_CHARS + 1)}, f"text is at most {MAX_MEMORY_CHARS} characters"),
    ({"category": "user", "text": "x", "replaces": "first"}, "replaces is a note's id, as m12"),
])
def test_bad_arguments_fail_before_anything_is_allocated(arguments: dict[str, Any], message: str) -> None:
    data = fresh()
    outcome = run(MemorySave(data), data, **arguments)
    assert outcome.status is ToolStatus.FAILED and said(outcome).startswith(message) and outcome.memory_change is None
    assert asyncio.run(data.allocate_memory_id(ME)) == 1  # no id was given out


def test_forget() -> None:
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.USER, "Old."))
    outcome = run(MemoryForget(data), data, id="m1")
    assert said(outcome) == "Forgot m1."
    assert outcome.memory_change == MemoryChange(MemoryChangeKind.FORGET, 1, expected_revision=1)
    assert outcome.raw_result == {"memory": {"id": 1, "category": "user", "text": "Old.", "revision": 1,
                                             "origin": "user", "source": None}}
    assert said(run(MemoryForget(data), data, id="m2")) == "No note m2."
    assert said(run(MemoryForget(data), data, id="one")) == "id is a note's id, as m12"


def test_search_ranks_by_words_then_newest() -> None:
    data = fresh()
    for category, text in ((MemoryCategory.PROJECT, "HIMARI runs on Go."),
                           (MemoryCategory.PROJECT, "Calliope is in Python."),
                           (MemoryCategory.REFERENCE, "HIMARI's docs are in docs/."),
                           (MemoryCategory.USER, "Writes Go and Python.")):
        asyncio.run(data.save_memory(ME, category, text))
    lines = said(run(MemorySearch(data), data, query="ＧＯ python")).splitlines()  # NFKC, case-folded
    assert [line.split()[0] for line in lines] == ["m4", "m2", "m1"]
    assert lines[0].startswith("m4 [user] Writes Go and Python. (")
    assert said(run(MemorySearch(data), data, query="himari", category="reference")).startswith("m3 [reference]")
    assert said(run(MemorySearch(data), data, query="rust")) == "No saved notes match."
    twice = said(run(MemorySearch(data), data, query="calliope calliope himari")).splitlines()
    assert [line.split()[0] for line in twice] == ["m3", "m2", "m1"]  # a repeated word counts once: newest first
    newest = said(run(MemorySearch(data), data)).splitlines()
    assert [line.split()[0] for line in newest] == ["m4", "m3", "m2", "m1"]
    assert said(run(MemorySearch(data), data, category="mood")).startswith("category is one of")


def test_search_shows_at_most_search_limit() -> None:
    data = fresh()
    for i in range(SEARCH_LIMIT + 3):
        asyncio.run(data.save_memory(ME, MemoryCategory.REFERENCE, f"path {i}"))
    lines = said(run(MemorySearch(data), data, query="path")).splitlines()
    assert len(lines) == SEARCH_LIMIT + 1 and lines[-1].startswith("… 3 more match")


def test_ids_without_a_namespace() -> None:
    assert all("/" not in str(t.definition.tool_id) for t in memory_tools(fresh()))
    assert ToolId("memory_save") == MemorySave.definition.tool_id
