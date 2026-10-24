"""Memory in the Application (V3-P71): the Tools write notes with their
results, the notes reach the model in the Current State from the next turn,
the user's changes take effect at the next turn, and a permanent delete
takes the notes a Conversation's calls wrote."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from himari.bootstrap.application import Application
from himari.common import CallId, CallRef, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope
import pytest

from himari.data.api import DataError, DataErrorCode, Memory, MemoryCategory, MemoryOrigin, ToolResultRecord
from himari.prompt.api import MEMORY_NOTE, ModelInput, ToolGroupTurn, state_block
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


def call(tool: str, arguments: dict[str, Any]) -> NormalizedToolCalls:
    return NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId(tool), arguments),))


def final(text: str = "done") -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


async def turn(app: Application, scope: TrustedScope, text: str = "hi") -> None:
    await app.send(scope, text)
    await app.runner.wait(scope)


def texts(model_input: ModelInput) -> list[str] | None:
    notes = model_input.current_state.memories
    return None if notes is None else [n.text for n in notes]


def last_result(model_input: ModelInput) -> str:
    step = model_input.conversation[-1]
    assert isinstance(step, ToolGroupTurn)
    (result,) = step.results.values()
    block = result.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def test_a_saved_note_reaches_the_model_from_the_next_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "user", "text": " Uses Go at work. "}), final(),
                                   final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, scope, "I use Go at work; remember that.")
            first, after_tool = adapter.prepared_inputs
            assert texts(first) == texts(after_tool) == []  # the turn keeps what it started with
            assert last_result(after_tool) == "Saved m1 (user)."
            (note,) = await app.memories(ME)
            assert (note.text, note.origin, note.source_conversation) == ("Uses Go at work.", MemoryOrigin.MODEL,
                                                                          scope.conversation_id)
            await turn(app, scope, "next")
            assert texts(adapter.prepared_inputs[-1]) == ["Uses Go at work."]
            assert MEMORY_NOTE in adapter.prepared_inputs[-1].stable_prefix
            tools = {d.tool_id for d in adapter.prepared_inputs[-1].tool_catalog}
            assert {"memory_save", "memory_forget", "memory_search"} <= tools
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_users_changes_apply_at_the_next_turn_of_an_open_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final(), final(), final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            note = await app.add_memory(ME, MemoryCategory.PREFERENCE, "Short lists.")
            await turn(app, scope)
            assert texts(adapter.prepared_inputs[-1]) == ["Short lists."]
            await app.edit_memory(ME, note.memory_id, MemoryCategory.PREFERENCE, "Short lists, no tables.", 1)
            await turn(app, scope)
            assert texts(adapter.prepared_inputs[-1]) == ["Short lists, no tables."]
            await app.delete_memory(ME, note.memory_id)
            await turn(app, scope)
            assert texts(adapter.prepared_inputs[-1]) == []
        finally:
            await app.close()

    asyncio.run(scenario())


def test_rewrite_forget_and_search_through_the_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([
            call("memory_save", {"category": "project", "text": "Moves to Busan.", "replaces": "m1"}), final(),
            call("memory_search", {"query": "busan"}), final(),
            call("memory_forget", {"id": "m1"}), final(),
        ])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.add_memory(ME, MemoryCategory.PROJECT, "Lives in Seoul.")
            await turn(app, scope)
            assert last_result(adapter.prepared_inputs[-1]) == "Updated m1 (project)."
            (note,) = await app.memories(ME)
            assert (note.text, note.revision, note.origin) == ("Moves to Busan.", 2, MemoryOrigin.MODEL)
            await turn(app, scope)
            assert last_result(adapter.prepared_inputs[-1]).startswith("m1 [project] Moves to Busan. (")
            await turn(app, scope)
            assert last_result(adapter.prepared_inputs[-1]) == "Forgot m1."
            assert await app.memories(ME) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_rewrite_in_another_conversation_records_the_source_an_undo_puts_back(tmp_path: Path) -> None:
    # V3-P71 §7.1 (rev. 4): the earlier state in raw_result.previous names
    # where the note came from, so an Undo can restore that too.
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "project", "text": "Plans a trip."}), final(),
                                   call("memory_save", {"category": "project", "text": "Plans a trip to Jeju.",
                                                        "replaces": "m1"}), final()])
        app = await opened(tmp_path, adapter)
        try:
            first = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, first)
            (before,) = await app.memories(ME)
            second = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, second)
            (after,) = await app.memories(ME)
            assert after.source_conversation == second.conversation_id and after.source_call is not None
            records = await app.data.read_tool_executions(second, after.source_call.run_id)
            (record,) = [r for r in records if r.call == after.source_call]
            assert before.source_call is not None
            assert record.raw_result["previous"] == {  # type: ignore[index, call-overload]
                "id": 1, "category": "project", "text": "Plans a trip.", "revision": 1, "origin": "model",
                "source": {"conversation": first.conversation_id, "run": before.source_call.run_id,
                           "call": before.source_call.call_id}}
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_note_changed_after_the_tool_read_it_fails_the_call_and_stays(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "user", "text": "lost", "replaces": "m1"}), final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.add_memory(ME, MemoryCategory.USER, "Kept.")
            read = app.data.read_memory

            async def stale(principal: PrincipalId, memory_id: int) -> Memory:  # as if edited in between
                return replace(await read(principal, memory_id), revision=0)

            app.data.read_memory = stale  # type: ignore[method-assign]
            await turn(app, scope)
            result = last_result(adapter.prepared_inputs[-1])
            assert result.startswith("Nothing was saved: the note was changed meanwhile")
            assert [n.text for n in await app.memories(ME)] == ["Kept."]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_permanent_delete_takes_the_notes_its_calls_wrote(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "project", "text": "Plans a trip."}), final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.add_memory(ME, MemoryCategory.USER, "Mine.")
            await turn(app, scope)
            assert (await app.delete_notice(scope)).memories == 1
            await app.trash(scope)
            assert len(await app.memories(ME)) == 2  # the trash can still be undone
            await app.purge(scope)
            assert [n.text for n in await app.memories(ME)] == ["Mine."]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_memory_off_offers_no_tools_and_no_notes(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final()])
        app = await opened(tmp_path, adapter, memory=False)
        try:
            assert not app.memory_enabled
            await app.add_memory(ME, MemoryCategory.USER, "Kept, but not used.")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, scope)
            (sent,) = adapter.prepared_inputs
            assert texts(sent) is None and MEMORY_NOTE not in sent.stable_prefix
            assert not {d.tool_id for d in sent.tool_catalog} & {"memory_save", "memory_forget", "memory_search"}
            assert state_block(sent.current_state) is None or "user_memory" not in (state_block(sent.current_state) or "")
        finally:
            await app.close()

    asyncio.run(scenario())


async def result_calls(app: Application, scope: TrustedScope) -> list[CallRef]:
    return [e.payload.call for e in await app.data.read_events(scope) if isinstance(e.payload, ToolResultRecord)]


async def lines(app: Application, scope: TrustedScope) -> list[tuple[str, int, str, bool]]:
    calls = await result_calls(app, scope)
    found = await app.memory_lines(scope, [c.run_id for c in calls])
    return [(line.action, line.memory_id, line.text, line.undo) for c in calls if (line := found.get(c)) is not None]


def test_each_memory_call_has_a_line_and_undo_while_the_note_is_as_it_left_it(tmp_path: Path) -> None:
    # V3-P71 §7.1: forgot and already-saved lines have no Undo; a failed call has no line.
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "user", "text": "Uses Go."}), final(),
                                   call("memory_save", {"category": "user", "text": "Uses Go."}), final(),
                                   call("memory_save", {"category": "user", "text": "x", "replaces": "m9"}), final(),
                                   call("memory_save", {"category": "user", "text": "Uses Rust.", "replaces": "m1"}),
                                   final(),
                                   call("memory_forget", {"id": "m1"}), final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            for _ in range(3):
                await turn(app, scope)
            assert await lines(app, scope) == [("saved", 1, "Uses Go.", True), ("already", 1, "Uses Go.", False)]
            await turn(app, scope)
            assert await lines(app, scope) == [("saved", 1, "Uses Go.", False), ("already", 1, "Uses Go.", False),
                                               ("updated", 1, "Uses Rust.", True)]
            await app.edit_memory(ME, 1, MemoryCategory.USER, "Uses Rust and Go.", 2)
            assert (await lines(app, scope))[-1] == ("updated", 1, "Uses Rust.", False)  # edited since
            with pytest.raises(DataError) as error:
                await app.undo_memory(scope, (await result_calls(app, scope))[-1])
            assert error.value.code is DataErrorCode.VERSION_CONFLICT
            await turn(app, scope)
            assert (await lines(app, scope))[-1] == ("forgot", 1, "Uses Rust and Go.", False)
            calls = await result_calls(app, scope)
            for refused in (calls[-1], calls[1], CallRef(calls[0].run_id, CallId("nope"))):
                with pytest.raises(DataError) as error:  # a forget, an already-saved, no such call
                    await app.undo_memory(scope, refused)
                assert error.value.code is DataErrorCode.NOT_FOUND
        finally:
            await app.close()

    asyncio.run(scenario())


def test_undo_a_new_note_deletes_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "user", "text": "Uses Go."}), final()])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, scope)
            (saved_by,) = await result_calls(app, scope)
            assert await app.undo_memory(scope, saved_by) is None
            assert await app.memories(ME) == ()
            assert await lines(app, scope) == [("saved", 1, "Uses Go.", False)]
            with pytest.raises(DataError) as error:  # once
                await app.undo_memory(scope, saved_by)
            assert error.value.code is DataErrorCode.NOT_FOUND
        finally:
            await app.close()

    asyncio.run(scenario())


def test_undo_a_rewrite_from_another_conversation_puts_back_its_text_and_source(tmp_path: Path) -> None:
    # The review's case (rev. 4): A saves, B rewrites, B's change undone,
    # then B deleted for good — the note stays, A's again.
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "project", "text": "Plans a trip."}), final(),
                                   call("memory_save", {"category": "project", "text": "Plans a trip to Jeju.",
                                                        "replaces": "m1"}), final()])
        app = await opened(tmp_path, adapter)
        try:
            first = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, first)
            (before,) = await app.memories(ME)
            second = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, second)
            (rewrote,) = await result_calls(app, second)
            restored = await app.undo_memory(second, rewrote)
            assert restored is not None
            assert (restored.text, restored.origin, restored.source_conversation, restored.source_call,
                    restored.revision) == ("Plans a trip.", MemoryOrigin.MODEL, first.conversation_id,
                                           before.source_call, 3)
            await app.trash(second)
            await app.purge(second)
            assert [n.text for n in await app.memories(ME)] == ["Plans a trip."]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_undo_never_brings_back_a_source_deleted_for_good(tmp_path: Path) -> None:
    # Re-review B1 (rev. 5): A saves, B rewrites, A deleted for good, then B's change undone.
    async def scenario() -> None:
        adapter = ScriptedAdapter([call("memory_save", {"category": "project", "text": "Plans a trip."}), final(),
                                   call("memory_save", {"category": "project", "text": "Plans a trip to Jeju.",
                                                        "replaces": "m1"}), final()])
        app = await opened(tmp_path, adapter)
        try:
            first = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, first)
            second = TrustedScope(ME, await app.create_conversation(ME))
            await turn(app, second)
            await app.trash(first)
            await app.purge(first)
            (rewrote,) = await result_calls(app, second)
            with pytest.raises(DataError) as error:
                await app.undo_memory(second, rewrote)
            assert error.value.code is DataErrorCode.NOT_FOUND
            assert [(n.text, n.source_conversation) for n in await app.memories(ME)] == [
                ("Plans a trip to Jeju.", second.conversation_id)]
        finally:
            await app.close()

    asyncio.run(scenario())
