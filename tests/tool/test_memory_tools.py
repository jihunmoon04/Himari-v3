"""The memory Tools (V3-P71 §6) on their own: what they tell the model and
the change they hand to Data. Committing that change is Data's
(tests/data/test_memories.py) and the Application's (tests/bootstrap)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from testkit import SCOPE, data_for, new_store

from himari.common import CallId, TextBlock, ToolId
from himari.data.api import MAX_LOOP_CHARS, MAX_MEMORY_CHARS, MemoryCategory, MemoryChange, MemoryChangeKind
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
    assert outcome.raw_result == {"memory": {"id": 1, "category": "preference", "text": "Short lists.", "revision": 1,
                                             "due": None, "valid_until": None}, "previous": None}
    assert asyncio.run(data.list_memories(ME)) == ()  # nothing is written until the result is committed


def test_a_rewrite_names_the_revision_it_read_and_keeps_the_old_text() -> None:
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.PROJECT, "Lives in Seoul."))
    outcome = run(MemorySave(data), data, category="project", text="Moves to Busan.", replaces="m1")
    assert said(outcome) == "Updated m1 (project)."
    assert outcome.memory_change == MemoryChange(MemoryChangeKind.SAVE, 1, MemoryCategory.PROJECT, "Moves to Busan.",
                                                 expected_revision=1)
    assert outcome.raw_result["previous"] == {  # type: ignore[index, call-overload]
        "id": 1, "category": "project", "text": "Lives in Seoul.", "revision": 1, "origin": "user", "source": None,
        "due": None, "due_instant": None, "valid_until": None, "valid_instant": None, "source_pass": None, "source_item": None, "user_approved_at": None}
    missing = run(MemorySave(data), data, category="project", text="x", replaces="m9")
    assert missing.status is ToolStatus.FAILED and said(missing) == "No note m9." and missing.memory_change is None


@pytest.mark.parametrize("unused", ["", "  ", None])
def test_an_unused_optional_argument_sent_empty_is_left_out(unused: object) -> None:
    # Seen with a real model (2026-10-24): "replaces": "" for a new note.
    data = fresh()
    outcome = run(MemorySave(data), data, category="user", text="Uses Go.", replaces=unused)
    assert said(outcome) == "Saved m1 (user)."
    assert said(run(MemorySearch(data), data, query="", category=unused)) == "No saved notes or episodes match."


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
                                             "origin": "user", "source": None, "due": None, "due_instant": None, "valid_until": None, "valid_instant": None, "source_pass": None, "source_item": None, "user_approved_at": None}}
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
    assert said(run(MemorySearch(data), data, query="rust", kind="notes")) == "No saved notes match."
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


# --- V3-P85 §5.5: loops, dates and episodes ----------------------------------------------------------------

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # Saturday 21:00 in Seoul


def saving(data: Any) -> MemorySave:
    return MemorySave(data, time_zone="Asia/Seoul", clock=lambda: NOW)


def test_a_loop_is_saved_with_its_date_and_a_passing_fact_with_its_end() -> None:
    data = fresh()
    outcome = run(saving(data), data, category="loop", text="Interview at Kakao.", due="2026-11-04",
                  valid_until="2026-11-05")
    change = outcome.memory_change
    assert said(outcome) == "Saved m1 (loop)." and change is not None
    assert (change.category, change.due_at, change.due_instant, change.valid_until) == (
        MemoryCategory.LOOP, "2026-11-04", datetime(2026, 11, 4, 14, 59, 59, 999999, tzinfo=UTC), None)
    fact = run(saving(data), data, category="user", text="Away in Jeju.", valid_until="2026-11-06T18:00+09:00",
               due="2026-11-04").memory_change
    assert fact is not None and (fact.valid_until, fact.valid_instant, fact.due_at) == (
        "2026-11-06T18:00+09:00", datetime(2026, 11, 6, 9, 0, tzinfo=UTC), None)  # a fact has no due
    for bad, why in (({"due": "next Friday"}, "due is a date"), ({"due": "2026-11-04T14:00"}, "due is a date"),
                     ({"due": "2030-01-01"}, "out of range"), ({"due": "2026-10-01"}, "out of range")):
        refused = run(saving(data), data, category="loop", text="x", **bad)
        assert refused.status is ToolStatus.FAILED and why in said(refused), bad
    long = run(saving(data), data, category="loop", text="가" * (MAX_LOOP_CHARS + 1))
    assert said(long) == f"text is at most {MAX_LOOP_CHARS} characters; shorten it"


def test_a_rewrite_keeps_the_date_it_does_not_give() -> None:
    data = fresh()
    first = run(saving(data), data, category="loop", text="Interview.", due="2026-11-04").memory_change
    assert first is not None
    asyncio.run(data.save_memory(ME, MemoryCategory.USER, "placeholder"))  # m2: ids are the store's to give
    stored = asyncio.run(data.read_memory(ME, 2))
    asyncio.run(data.restore_memory(ME, replace(stored, category=MemoryCategory.LOOP, text="Interview.",
                                                due_at=first.due_at, due_instant=first.due_instant),
                                    expected_revision=1))
    reworded = run(saving(data), data, category="loop", text="Interview at Kakao.", replaces="m2").memory_change
    assert reworded is not None and (reworded.due_at, reworded.due_instant) == (first.due_at, first.due_instant)
    moved = run(saving(data), data, category="loop", text="Interview.", replaces="m2", due="2026-11-05").memory_change
    assert moved is not None and moved.due_at == "2026-11-05"
    retyped = run(saving(data), data, category="project", text="Interviews.", replaces="m2").memory_change
    assert retyped is not None and retyped.due_at is None  # a project has no due date


def test_search_finds_loops_with_their_date_and_marks_an_ended_fact() -> None:
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.LOOP, "Interview."))
    loop = asyncio.run(data.read_memory(ME, 1))
    asyncio.run(data.restore_memory(ME, replace(loop, due_at="2026-11-04", due_instant=NOW + timedelta(days=4)),
                                    expected_revision=1))
    asyncio.run(data.save_memory(ME, MemoryCategory.USER, "Was in Jeju."))
    jeju = asyncio.run(data.read_memory(ME, 2))
    asyncio.run(data.restore_memory(ME, replace(jeju, valid_until="2026-10-20", valid_instant=NOW - timedelta(days=11)),
                                    expected_revision=1))
    lines = said(run(MemorySearch(data, clock=lambda: NOW), data, kind="notes")).splitlines()
    assert lines[0].startswith("m2 [user] Was in Jeju. (") and lines[0].endswith(") (expired)")
    assert lines[1].startswith("m1 [loop due 2026-11-04] Interview. (")


def test_search_finds_episodes_by_words_and_by_days() -> None:
    from testkit import user

    from himari.data.api import EpisodePlan, PassKind, PassStart

    data = fresh()

    async def episode(text: str, ended: datetime) -> None:
        await data.append_event(SCOPE, user(f"{text} — enough words here"))
        last = (await data.read_events(SCOPE))[-1].sequence
        cursor = (await data.read_memory_cursor(SCOPE)).through_sequence
        pass_id = await data.begin_pass(SCOPE, PassStart(PassKind.LIVE, cursor, last, last, "p", "m"))
        await data.apply_pass(pass_id, [], EpisodePlan(text, ended - timedelta(minutes=30), ended, (last,), ()),
                              now=ended)

    asyncio.run(episode("Fixed the bubbles.", datetime(2026, 10, 30, 12, 40, tzinfo=UTC)))
    asyncio.run(episode("Planned the memory design.", datetime(2026, 10, 24, 3, 0, tzinfo=UTC)))
    asyncio.run(data.rename_conversation(SCOPE, "말풍선"))
    search = MemorySearch(data, time_zone="Asia/Seoul", clock=lambda: NOW)
    assert said(run(search, data, query="bubbles", kind="episodes")) == (
        'e1 2026-10-30 21:10–21:40 "말풍선": Fixed the bubbles.')
    week = said(run(search, data, kind="episodes", **{"from": "2026-10-20", "to": "2026-10-31"})).splitlines()
    assert [line.split()[0] for line in week] == ["e2", "e1"]  # a range without words: in time order
    assert said(run(search, data, kind="episodes", **{"from": "2026-10-25"})).startswith("e1 ")
    assert said(run(search, data, kind="episodes", to="Friday")) == "to is a day, YYYY-MM-DD"
    assert said(run(search, data, query="bubbles", category="user")) == "No saved notes or episodes match."
    outcome = run(search, data, query="memory")
    assert outcome.raw_result == {"ids": (), "episodes": (2,)}  # frozen as results are


def test_a_rewrite_removes_a_date_sent_as_null_and_keeps_one_sent_empty() -> None:
    # PR #137 review: a date gone or now unknown could not be taken off a note.
    data = fresh()
    asyncio.run(data.save_memory(ME, MemoryCategory.LOOP, "Interview."))
    loop = asyncio.run(data.read_memory(ME, 1))
    asyncio.run(data.restore_memory(ME, replace(loop, due_at="2026-11-04", due_instant=NOW + timedelta(days=4)),
                                    expected_revision=1))
    cleared = run(saving(data), data, category="loop", text="Interview, date unknown.", replaces="m1", due=None)
    assert cleared.memory_change is not None and cleared.memory_change.due_at is None
    kept = run(saving(data), data, category="loop", text="Interview at Kakao.", replaces="m1", due="")
    assert kept.memory_change is not None and kept.memory_change.due_at == "2026-11-04"


def test_a_day_that_starts_a_clock_change_is_searched_from_its_local_midnight() -> None:
    # PR #137 review: 2026-11-01 in New York has 25 hours; its first hour was left out.
    from testkit import user

    from himari.data.api import EpisodePlan, PassKind, PassStart

    data = fresh()

    async def episode(text: str, ended: datetime) -> None:
        await data.append_event(SCOPE, user(f"{text} — enough words here"))
        last = (await data.read_events(SCOPE))[-1].sequence
        cursor = (await data.read_memory_cursor(SCOPE)).through_sequence
        pass_id = await data.begin_pass(SCOPE, PassStart(PassKind.LIVE, cursor, last, last, "p", "m"))
        await data.apply_pass(pass_id, [], EpisodePlan(text, ended, ended, (last,), ()), now=ended)

    asyncio.run(episode("Just after midnight.", datetime(2026, 11, 1, 4, 30, tzinfo=UTC)))  # 00:30 EDT
    asyncio.run(episode("The evening before.", datetime(2026, 11, 1, 3, 30, tzinfo=UTC)))  # 23:30 EDT, Oct 31
    search = MemorySearch(data, time_zone="America/New_York", clock=lambda: NOW)
    found = said(run(search, data, kind="episodes", **{"from": "2026-11-01", "to": "2026-11-01"})).splitlines()
    assert [line.split(": ", 1)[1] for line in found] == ["Just after midnight."]
