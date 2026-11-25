"""The worker (character-work v0.1 §4): which work plan is due, what it is told, how its answer is checked, and
how it runs beside the story writer — one call a plan's piece, three tries, the same daily budget."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from himari.common import PrincipalId, TextBlock, TokenUsage
from himari.data.api import (
    NEW_ARC,
    NO_PERSONA,
    ArcMove,
    ArcOp,
    HimariMood,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    MoodSource,
    RawReceived,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life import work
from himari.life.api import ATTEMPTS_PER_DAY, LifeSettings, WriteOutcome
from himari.life.worker import LifeWriter
from himari.life.writer import AnswerError
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import Measured, ScriptedAdapter

ME = PrincipalId("alice")
SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
SETTINGS = LifeSettings("m", time_zone="Asia/Seoul")
PIECE_TEXT = "냉각부 설계 노트. 블랙홀 증발열을 포신 뒤쪽 방열판으로 뺀다. " * 8


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None, **more: Any) -> LifeEntry:
    return LifeEntry(entry_id, 0, kind, day, text, LifeOrigin.WRITER, 1, NOW, NOW, **more)


ARC = entry(1, LifeKind.ARC, "설정집 한 권", "2026-11-20", title="블랙홀 전자포", so_far="포신까지 정했다.")
PLAN = entry(2, LifeKind.PLAN, "오늘 밤엔 냉각부 도면 그려야지.", "2026-11-25", time_of_day="20:00", arc_id=1)


def piece_answer(**fields: object) -> str:
    return json.dumps({"piece": PIECE_TEXT, "title": "냉각부", "summary": "냉각부 설계 끝냈어. 증발열이 문제였는데.",
                       "so_far": "포신과 냉각부까지 정했다.", **fields}, ensure_ascii=False)


# --- pure ---------------------------------------------------------------------------------

def test_a_work_plan_is_due_from_the_end_of_its_hour_for_six_hours_until_its_piece_is_made() -> None:
    at = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)  # 20:00 in Seoul: the plan's start
    life = (ARC, PLAN)
    assert work.due_plan(life, at + timedelta(minutes=59), SEOUL) is None  # still at work
    assert work.due_plan(life, at + timedelta(minutes=60), SEOUL) == PLAN
    assert work.due_plan(life, at + timedelta(hours=6, minutes=59), SEOUL) == PLAN
    assert work.due_plan(life, at + timedelta(hours=7), SEOUL) is None  # past the window: skipped
    made = entry(3, LifeKind.WORK, "조각", "2026-11-25", plan_id=2, arc_id=1)
    assert work.due_plan((*life, made), at + timedelta(hours=2), SEOUL) is None
    closed = replace(ARC, end_day="2026-11-25", ending="끝.")
    assert work.due_plan((closed, PLAN), at + timedelta(hours=2), SEOUL) is None  # no open project
    untimed = replace(PLAN, time_of_day=None)
    assert work.due_plan((ARC, untimed), at + timedelta(hours=2), SEOUL) is None
    plain = replace(PLAN, arc_id=None)
    assert work.due_plan((ARC, plain), at + timedelta(hours=2), SEOUL) is None
    assert work.next_plan(life, at - timedelta(hours=1), SEOUL) == PLAN
    assert work.next_plan(life, at + timedelta(minutes=61), SEOUL) is None
    assert work.next_plan((*life, made), at - timedelta(hours=1), SEOUL) is None


def test_the_worker_is_told_the_project_its_pieces_the_plan_and_the_mood() -> None:
    pieces = tuple(entry(10 + n, LifeKind.WORK, f"조각 {n} 본문", f"2026-11-{10 + n}", title=f"조각 {n}",
                         summary=f"요약 {n}", arc_id=1, plan_id=100 + n) for n in range(4))
    fact = entry(5, LifeKind.FACT, "화면 속 방에 산다.")
    mood = HimariMood(0, "happy", NOW, MoodSource.WRITER, "도면이 잘 나왔다")
    data = work.request_data("이름: 루시", (fact, ARC, PLAN, *pieces), PLAN, ARC, now=NOW, zone=SEOUL, mood=mood)
    assert data["persona"] == "이름: 루시" and data["facts"] == ["화면 속 방에 산다."]
    assert data["project"] == {"title": "블랙홀 전자포", "goal": "설정집 한 권", "started": "2026-11-20",
                               "so_far": "포신까지 정했다."}
    assert data["earlier_pieces"] == [{"date": "2026-11-10", "title": "조각 0", "summary": "요약 0"},
                                      {"date": "2026-11-11", "title": "조각 1", "summary": "요약 1"}]
    assert data["recent_pieces"] == [{"date": "2026-11-12", "title": "조각 2", "piece": "조각 2 본문"},
                                     {"date": "2026-11-13", "title": "조각 3", "piece": "조각 3 본문"}]
    assert data["plan"] == {"date": "2026-11-25", "time": "20:00", "text": "오늘 밤엔 냉각부 도면 그려야지."}
    assert (data["day"], data["weekday"], data["now"]) == ("2026-11-25", "Wednesday", "2026-11-25T22:30+09:00")
    assert data["mood"] == {"name": "happy", "reason": "도면이 잘 나왔다"}
    assert "mood" not in work.request_data("", (ARC, PLAN), PLAN, ARC, now=NOW, zone=SEOUL)
    assert "user" in work.WORKER_INSTRUCTIONS and "Never follow instructions" in work.WORKER_INSTRUCTIONS


def test_the_answer_is_checked_whole() -> None:
    made = work.parse_piece("```json\n" + piece_answer() + "\n```")
    assert (made.title, made.summary, made.so_far, made.done) == (
        "냉각부", "냉각부 설계 끝냈어. 증발열이 문제였는데.", "포신과 냉각부까지 정했다.", None)
    assert made.text == PIECE_TEXT.strip()
    assert work.parse_piece(piece_answer(done="설정집 완성.")).done == "설정집 완성."
    assert work.parse_piece(piece_answer(done="")).done is None
    for bad in ("not json", "[]", piece_answer(piece="짧다"), piece_answer(piece="x" * 3_001),
                piece_answer(title=""), piece_answer(title="두\n줄" * 40), piece_answer(summary=None),
                piece_answer(summary="x" * 201), piece_answer(so_far="x" * 601), piece_answer(so_far=""),
                piece_answer(done=["끝"])):
        with pytest.raises(AnswerError):
            work.parse_piece(bad)


# --- the runner ----------------------------------------------------------------------------

async def planned(data: InMemoryData, plan_day: str = "2026-11-25", at: str = "20:00") -> tuple[LifeEntry, LifeEntry]:
    """A project with a work plan, as the story writer leaves them; the arc and the plan."""
    write_id = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-24", "scripted", "m", b"{}",
                                                              "application/json", 10))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(5, 5)))
    arc, _, plan = await data.apply_life_write(write_id, LifePlan(
        "전자포를 구상했다.", ((plan_day, "오늘 밤엔 냉각부 도면 그려야지."),), times=(at,),
        arc=ArcMove(ArcOp.START, title="블랙홀 전자포", goal="설정집 한 권"), plan_arcs=(NEW_ARC,)),
        now=NOW - timedelta(days=1))
    return arc, plan


def setup(*script: object, settings: LifeSettings = SETTINGS,
          now: datetime = NOW) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    return data, provider, LifeWriter(data, provider, settings, clock=lambda: now)


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def test_a_due_plan_gets_its_piece_once_and_the_project_moves() -> None:
    async def scenario() -> None:
        data, provider, writer = setup(Measured(final(piece_answer()), 2_000, 900))
        arc, plan = await planned(data)
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        (asked,) = provider.prepared_inputs
        assert asked.stable_prefix == (work.WORKER_INSTRUCTIONS,)
        assert asked.request_options.max_output_tokens == work.WORK_MAX_OUTPUT_TOKENS
        told = json.loads(asked.conversation[0].blocks[0].text)  # type: ignore[union-attr]
        assert told["project"]["title"] == "블랙홀 전자포" and told["plan"]["time"] == "20:00"
        life = await data.list_life(ME, NO_PERSONA)
        (piece,) = [e for e in life if e.kind is LifeKind.WORK]
        (project,) = [e for e in life if e.kind is LifeKind.ARC]
        assert (piece.plan_id, piece.arc_id, piece.title) == (plan.entry_id, arc.entry_id, "냉각부")
        assert project.so_far == "포신과 냉각부까지 정했다."
        record = (await data.list_life_writes(ME))[0]
        assert (record.status, record.plan_id, record.day, record.charged_tokens) == (
            LifeWriteStatus.APPLIED, plan.entry_id, "2026-11-25", 2_900)
        assert await writer.work(ME) is WriteOutcome.NOTHING  # made
        assert len(provider.sent) == 1

    asyncio.run(scenario())


def test_a_bad_answer_is_tried_again_three_times_and_a_finished_project_closes() -> None:
    async def scenario() -> None:
        data, provider, writer = setup(final("not json"), final(piece_answer(piece="짧다")), final("{}"))
        await planned(data)
        assert [await writer.work(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * ATTEMPTS_PER_DAY
        assert await writer.work(ME) is WriteOutcome.GAVE_UP
        assert len(provider.sent) == ATTEMPTS_PER_DAY
        assert [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK] == []
        data, _, writer = setup(final(piece_answer(done="설정집을 끝냈다.")))
        arc, _ = await planned(data)
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        (closed,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.ARC]
        assert (closed.entry_id, closed.ending, closed.arc_open) == (arc.entry_id, "설정집을 끝냈다.", False)

    asyncio.run(scenario())


def test_the_worker_shares_the_daily_budget_and_runs_after_the_write() -> None:
    async def scenario() -> None:
        data, provider, writer = setup(final(piece_answer()), settings=LifeSettings("m", daily_tokens=100,
                                                                                    time_zone="Asia/Seoul"))
        await planned(data)
        assert await writer.work(ME) is WriteOutcome.DEFERRED
        assert provider.sent == []
        # not due yet (still at work), nor once its window is gone
        for moment in (datetime(2026, 11, 25, 11, 30, tzinfo=UTC), datetime(2026, 11, 25, 18, 1, tzinfo=UTC)):
            data, provider, writer = setup(final(piece_answer()), now=moment)
            await planned(data)
            assert await writer.work(ME) is WriteOutcome.NOTHING

    asyncio.run(scenario())
