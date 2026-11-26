"""memory_search over HIMARI's own life (character-mind v0.1 §6, §14.7): kind ``life`` (and ``all``) finds the turn's
persona's facts, diaries and events — the persona the Driver put in the call's context, never one the model names —
and only where the life is kept."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from testkit import SCOPE, data_for, new_store

from himari.common import CallId, TextBlock, TokenUsage
from himari.data.api import LifeJob, LifePlan, LifeWriteStart, RawReceived, WorldEvent
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.memory import MemorySearch, memory_tools

ME = SCOPE.principal_id
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


async def lived(data: Any) -> None:
    """Persona 1's fact, diary of 11-24 and an event of 11-25; persona 2's fact."""
    await data.add_life_fact(ME, 1, "고양이를 무서워한다.")
    await data.add_life_fact(ME, 2, "다른 페르소나의 고양이.")
    for job, day in ((LifeJob.REVIEW, "2026-11-24"), (LifeJob.WORLD, "2026-11-25")):
        write_id = await data.begin_life_write(ME, LifeWriteStart(1, day, "p", "m", b"{}", "j", 10, job=job))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "j", TokenUsage(1, 1)))
        if job is LifeJob.REVIEW:
            await data.apply_life_write(write_id, LifePlan("유나랑 고양이 카페에 갔다. 무서웠다."), now=NOW)
        else:
            await data.add_events(write_id, [WorldEvent("15:00", "택배로 고양이 인형이 왔다.")], now=NOW)


def run(tool: MemorySearch, persona_id: int | None, **arguments: Any) -> ToolOutcome:
    invocation = ToolInvocation(tool.definition.tool_id, CallId("c0"), arguments)
    outcome: ToolOutcome = asyncio.run(tool.run(invocation, ToolContext(SCOPE, persona_id=persona_id)))
    return outcome


def said(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def test_kind_life_finds_the_turns_personas_facts_diaries_and_events() -> None:
    store = new_store()
    data = data_for(store)
    asyncio.run(lived(data))
    search = MemorySearch(data, life=data)
    found = run(search, 1, query="고양이", kind="life")
    assert found.status is ToolStatus.SUCCEEDED
    assert said(found).splitlines() == ["[happened to you 2026-11-25 15:00] 택배로 고양이 인형이 왔다.",
                                        "[your fact] 고양이를 무서워한다.",
                                        "[your diary 2026-11-24] 유나랑 고양이 카페에 갔다. 무서웠다."]
    assert len(found.raw_result["life"]) == 3  # type: ignore[index, arg-type, call-overload]
    assert said(run(search, 2, query="고양이", kind="life")) == "[your fact] 다른 페르소나의 고양이."
    days = run(search, 1, query="고양이", kind="life", **{"from": "2026-11-24", "to": "2026-11-24"})
    assert said(days).splitlines() == ["[your diary 2026-11-24] 유나랑 고양이 카페에 갔다. 무서웠다."]
    everything = said(run(search, 1, query="고양이"))  # all: notes, episodes and the life
    assert "[your fact] 고양이를 무서워한다." in everything
    assert said(run(search, 1, query="강아지", kind="life")) == "Nothing of your own life matches."


def test_without_the_life_or_the_turns_persona_kind_life_is_refused_and_all_leaves_it_out() -> None:
    store = new_store()
    data = data_for(store)
    asyncio.run(lived(data))
    for search, persona_id in ((MemorySearch(data), 1), (MemorySearch(data, life=data), None)):
        refused = run(search, persona_id, query="고양이", kind="life")
        assert refused.status is ToolStatus.FAILED and "not kept here" in said(refused)
        assert said(run(search, persona_id, query="고양이")) == "No saved notes or episodes match."
    (_, _, tool) = memory_tools(data, life=data)
    assert isinstance(tool, MemorySearch) and "life" in str(tool.definition.input_schema)


def test_an_event_still_ahead_today_has_not_happened_yet() -> None:
    """Review P1-4: at 09:00 the 15:00 event is not found (life or all); at 16:00 it is."""
    store = new_store()
    data = data_for(store)
    asyncio.run(lived(data))
    for at, seen in ((datetime(2026, 11, 25, 0, 0, tzinfo=UTC), False), (datetime(2026, 11, 25, 7, 0, tzinfo=UTC), True)):
        search = MemorySearch(data, life=data, time_zone="Asia/Seoul", clock=lambda at=at: at)  # type: ignore[misc]
        for kind in ("life", "all"):
            assert ("택배로 고양이 인형이 왔다." in said(run(search, 1, query="고양이", kind=kind))) is seen
