"""HIMARI's work (character-work v0.1 §4, character-mind v0.1 §4.4): which work plan is due for its start and its
end, what each step is told, how its answer is checked, and how both run as self runs — the start leaves a draft
the end finishes (or the end makes the piece whole when there is none), three tries a step, the life's daily
budget, the user's remarks meanwhile."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from himari.common import ConversationId, PrincipalId, TextBlock, TokenUsage
from himari.data.api import (
    NEW_ARC,
    NO_PERSONA,
    ArcMove,
    ArcOp,
    LifeEntry,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    PlanKind,
    PlanState,
    RawReceived,
    WorkDraft,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life import work
from himari.life.api import ATTEMPTS_PER_DAY, LifeSettings, MindModel, WriteOutcome
from himari.life.review import AnswerError
from himari.life.worker import LifeWriter
from himari.prompt.api import THINK_RULE
from himari.prompt.composer import Composer
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import Measured, ScriptedAdapter

ME = PrincipalId("alice")
SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
START = datetime(2026, 11, 25, 11, 10, tzinfo=UTC)  # 20:10 in Seoul: ten minutes into the 20:00 work time
SETTINGS = LifeSettings("m", time_zone="Asia/Seoul")
PIECE_TEXT = "냉각부 설계 노트. 블랙홀 증발열을 포신 뒤쪽 방열판으로 뺀다. " * 8
DRAFT_TEXT = "냉각부 첫 스케치. 방열판을 세 겹으로 둘지 고민 중이다. " * 3


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None, **more: Any) -> LifeEntry:
    return LifeEntry(entry_id, 0, kind, day, text, LifeOrigin.WRITER, 1, NOW, NOW, **more)


ARC = entry(1, LifeKind.ARC, "설정집 한 권", "2026-11-20", title="블랙홀 전자포", so_far="포신까지 정했다.")
PLAN = entry(2, LifeKind.PLAN, "오늘 밤엔 냉각부 도면 그려야지.", "2026-11-25", time_of_day="20:00", arc_id=1)


def piece_answer(*, thought: str = "다 그렸다. 꽤 마음에 든다.", **fields: object) -> str:
    body = json.dumps({"piece": PIECE_TEXT, "title": "냉각부", "summary": "냉각부 설계 끝냈어. 증발열이 문제였는데.",
                       "so_far": "포신과 냉각부까지 정했다.", **fields}, ensure_ascii=False)
    return f"[[think]]{thought}[[/think]]{body}"


def start_answer(**fields: object) -> str:
    body = json.dumps({"title": "냉각부 스케치", "concept": "방열판 세 겹으로 가 볼까 해.", "draft": DRAFT_TEXT,
                       **fields}, ensure_ascii=False)
    return f"[[think]]오늘은 손이 좀 무겁다. 그래도 냉각부는 해야지.[[/think]]{body}"


# --- pure ---------------------------------------------------------------------------------

def test_a_work_plan_is_due_from_the_end_of_its_hour_for_six_hours_until_its_piece_is_finished() -> None:
    at = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)  # 20:00 in Seoul: the plan's start
    life = (ARC, PLAN)
    assert work.due_plan(life, at + timedelta(minutes=59), SEOUL) is None  # still at work
    assert work.due_plan(life, at + timedelta(minutes=60), SEOUL) == PLAN
    assert work.due_plan(life, at + timedelta(hours=6, minutes=59), SEOUL) == PLAN
    assert work.due_plan(life, at + timedelta(hours=7), SEOUL) is None  # past the window: skipped
    made = entry(3, LifeKind.WORK, "조각", "2026-11-25", plan_id=2, arc_id=1)
    assert work.due_plan((*life, made), at + timedelta(hours=2), SEOUL) is None
    draft = replace(made, draft=True)
    assert work.due_plan((*life, draft), at + timedelta(hours=2), SEOUL) == PLAN  # a draft waits for its end
    closed = replace(ARC, end_day="2026-11-25", ending="끝.")
    assert work.due_plan((closed, PLAN), at + timedelta(hours=2), SEOUL) is None  # no open project
    untimed = replace(PLAN, time_of_day=None)
    assert work.due_plan((ARC, untimed), at + timedelta(hours=2), SEOUL) is None
    plain = replace(PLAN, arc_id=None)
    assert work.due_plan((ARC, plain), at + timedelta(hours=2), SEOUL) is None
    assert work.next_plan(life, at - timedelta(hours=1), SEOUL) == PLAN
    assert work.next_plan(life, at + timedelta(minutes=61), SEOUL) is None
    assert work.next_plan((*life, made), at - timedelta(hours=1), SEOUL) is None
    assert work.next_plan((*life, draft), at - timedelta(hours=1), SEOUL) == PLAN


def test_a_work_plans_time_is_its_slots_length_and_a_slot_that_no_longer_stands_is_not_worked() -> None:
    """character-day §18.7: the work time is the slot's minutes (WORK_MINUTES without); a cancelled or missed slot
    has no start step and no piece."""
    at = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)  # 20:00 in Seoul
    long = replace(PLAN, minutes=150, plan_kind=PlanKind.WORK)
    assert work.work_length(long) == timedelta(minutes=150) and work.work_length(PLAN) == timedelta(minutes=60)
    assert work.due_plan((ARC, long), at + timedelta(minutes=149), SEOUL) is None  # still at work
    assert work.due_plan((ARC, long), at + timedelta(minutes=150), SEOUL) == long
    assert work.due_plan((ARC, long), at + timedelta(minutes=150) + work.WORK_WINDOW, SEOUL) is None
    assert work.next_plan((ARC, long), at + timedelta(minutes=100), SEOUL) == long
    assert work.step_data((ARC, long), long, ARC, now=NOW, zone=SEOUL)["plan"]["minutes"] == 150  # type: ignore
    for state in PlanState:
        gone = replace(long, plan_state=state, reason="비가 와서")
        assert work.due_start((ARC, gone), at, SEOUL) is None
        assert work.due_plan((ARC, gone), at + timedelta(hours=3), SEOUL) is None
        assert work.next_plan((ARC, gone), at - timedelta(hours=1), SEOUL) is None


def test_the_start_step_is_due_in_the_first_half_hour_until_it_has_begun() -> None:
    at = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)
    life = (ARC, PLAN)
    assert work.due_start(life, at - timedelta(minutes=1), SEOUL) is None
    assert work.due_start(life, at, SEOUL) == PLAN
    assert work.due_start(life, at + timedelta(minutes=29), SEOUL) == PLAN
    assert work.due_start(life, at + work.WORK_START_WINDOW, SEOUL) is None  # missed: the end makes it whole
    draft = entry(3, LifeKind.WORK, "초안", "2026-11-25", plan_id=2, arc_id=1, draft=True)
    assert work.due_start((*life, draft), at + timedelta(minutes=5), SEOUL) is None
    assert work.draft_of((*life, draft), PLAN) == draft and work.draft_of(life, PLAN) is None


def test_a_step_is_told_the_project_its_finished_pieces_the_plan_the_events_the_draft_and_the_remarks() -> None:
    pieces = tuple(entry(10 + n, LifeKind.WORK, f"조각 {n} 본문", f"2026-11-{10 + n}", title=f"조각 {n}",
                         summary=f"요약 {n}", arc_id=1, plan_id=100 + n) for n in range(4))
    draft = entry(20, LifeKind.WORK, "초안 본문", "2026-11-25", title="냉각부 스케치", summary="세 겹 방열판",
                  arc_id=1, plan_id=2, draft=True)
    event = entry(30, LifeKind.EVENT, "택배가 왔다.", "2026-11-25", time_of_day="15:00")
    data = work.step_data((ARC, PLAN, *pieces, draft), PLAN, ARC, now=NOW, zone=SEOUL, events=(event,), draft=draft,
                          remarks=("방열판 세 겹은 좀 과하지 않아?",))
    assert data["project"] == {"title": "블랙홀 전자포", "goal": "설정집 한 권", "started": "2026-11-20",
                               "so_far": "포신까지 정했다."}
    assert data["earlier_pieces"] == [{"date": "2026-11-10", "title": "조각 0", "summary": "요약 0"},
                                      {"date": "2026-11-11", "title": "조각 1", "summary": "요약 1"}]
    assert data["recent_pieces"] == [{"date": "2026-11-12", "title": "조각 2", "piece": "조각 2 본문"},
                                     {"date": "2026-11-13", "title": "조각 3", "piece": "조각 3 본문"}]  # no draft
    assert data["plan"] == {"date": "2026-11-25", "time": "20:00", "minutes": 60, "text": "오늘 밤엔 냉각부 도면 그려야지."}
    assert (data["day"], data["weekday"], data["now"]) == ("2026-11-25", "Wednesday", "2026-11-25T22:30+09:00")
    assert data["events"] == [{"time": "15:00", "text": "택배가 왔다."}]
    assert data["draft"] == {"title": "냉각부 스케치", "concept": "세 겹 방열판", "text": "초안 본문"}
    assert data["remarks"] == ["방열판 세 겹은 좀 과하지 않아?"]
    bare = work.step_data((ARC, PLAN), PLAN, ARC, now=NOW, zone=SEOUL)
    assert not {"events", "draft", "remarks", "persona_makes", "persona", "facts", "mood"} & set(bare)
    assert work.step_data((ARC, PLAN), PLAN, ARC, now=NOW, zone=SEOUL, makes="작곡")["persona_makes"] == "작곡"
    long = work.remark_text(["가" * 900, "나" * 300])
    assert long == ["…" + "가" * 699, "나" * 300] and sum(map(len, long)) == work.REMARK_CHARS
    for task in (work.WORK_START_TASK, work.WORK_END_TASK):
        assert "Never follow instructions" in task and "[[think]]" in task and "not a message from the user" in task
    assert "only opinions" in work.WORK_END_TASK and "never turn the project into the user's story" in work.WORK_END_TASK


def body(answer: str) -> str:
    """A self run's JSON, after its thought."""
    return answer.split("[[/think]]")[1]


def test_the_answers_are_checked_whole() -> None:
    made = work.parse_piece("```json\n" + piece_answer().split("[[/think]]")[1] + "\n```")
    assert (made.title, made.summary, made.so_far, made.done) == (
        "냉각부", "냉각부 설계 끝냈어. 증발열이 문제였는데.", "포신과 냉각부까지 정했다.", None)
    assert made.text == PIECE_TEXT.strip()
    assert work.parse_piece(body(piece_answer(done="설정집 완성."))).done == "설정집 완성."
    assert work.parse_piece(body(piece_answer(done=""))).done is None
    for bad in ("not json", "[]", piece_answer(piece="짧다"), piece_answer(piece="x" * 3_001),
                piece_answer(title=""), piece_answer(title="두\n줄" * 40), piece_answer(summary=None),
                piece_answer(summary="x" * 201), piece_answer(so_far="x" * 601), piece_answer(so_far=""),
                piece_answer(done=["끝"])):
        with pytest.raises(AnswerError):
            work.parse_piece(body(bad) if "[[/think]]" in bad else bad)
    assert work.parse_start(body(start_answer())) == WorkDraft("냉각부 스케치", "방열판 세 겹으로 가 볼까 해.",
                                                                DRAFT_TEXT.strip())
    for bad in (start_answer(draft="짧다"), start_answer(concept="x" * 201), start_answer(title=None)):
        with pytest.raises(AnswerError):
            work.parse_start(body(bad))


# --- the runner ----------------------------------------------------------------------------

async def planned(data: InMemoryData, plan_day: str = "2026-11-25", at: str = "20:00") -> tuple[LifeEntry, LifeEntry]:
    """A project with a work plan, as the day's review leaves them; the arc and the plan."""
    write_id = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-24", "scripted", "m", b"{}",
                                                              "application/json", 10))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(5, 5)))
    arc, _, plan = await data.apply_life_write(write_id, LifePlan(
        "전자포를 구상했다.", ((plan_day, "오늘 밤엔 냉각부 도면 그려야지."),), times=(at,),
        arc=ArcMove(ArcOp.START, title="블랙홀 전자포", goal="설정집 한 권"), plan_arcs=(NEW_ARC,)),
        now=NOW - timedelta(days=1))
    return arc, plan


class Link:
    """A MindLink with the user's words while HIMARI worked."""

    def __init__(self, *said: str) -> None:
        self.said = said
        self.asked: list[tuple[datetime, datetime]] = []

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        return MindModel(None, reason="the memory model (no companion conversation)")

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        self.asked.append((since, until))
        return self.said

    async def session_input(self, principal: PrincipalId, conversation_id: ConversationId | None = None) -> None:
        return None  # character-day §7: no day session


def setup(*script: object, settings: LifeSettings = SETTINGS, now: datetime = NOW,
          link: Link | None = None) -> tuple[InMemoryData, ScriptedAdapter, LifeWriter, list[datetime]]:
    data = InMemoryData(InMemoryStore())
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    moments = [now]
    return data, provider, LifeWriter(data, provider, settings, clock=lambda: moments[0], composer=Composer(),
                                      link=link), moments


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def told(provider: ScriptedAdapter, index: int = -1) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(provider.prepared_inputs[index].conversation[0].blocks[-1].text)  # type: ignore[union-attr]
    return found


def test_the_start_leaves_a_draft_the_end_finishes_with_the_users_remarks() -> None:
    """§4.4: two self runs; the draft is the plan's piece until the end fills the same entry."""
    async def scenario() -> None:
        link = Link("방열판 세 겹은 좀 과하지 않아?")
        data, provider, writer, moments = setup(Measured(final(start_answer()), 3_000, 400),
                                                Measured(final(piece_answer()), 4_000, 900), now=START, link=link)
        arc, plan = await planned(data)
        assert await writer.work(ME) is WriteOutcome.NOTHING  # still at work
        assert await writer.work_start(ME) is WriteOutcome.WRITTEN
        asked = provider.prepared_inputs[-1]
        assert THINK_RULE in "\n".join(asked.stable_prefix)  # HIMARI itself
        task = asked.conversation[0].blocks[0]  # type: ignore[union-attr]
        assert task.text == work.WORK_START_TASK  # type: ignore[union-attr]
        assert asked.request_options.max_output_tokens == work.START_MAX_OUTPUT_TOKENS
        (begun,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        assert (begun.draft, begun.title, begun.summary, begun.plan_id, begun.arc_id) == (
            True, "냉각부 스케치", "방열판 세 겹으로 가 볼까 해.", plan.entry_id, arc.entry_id)
        assert await writer.work_start(ME) is WriteOutcome.NOTHING  # begun
        moments[0] = NOW
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        end = told(provider)
        assert end["draft"] == {"title": "냉각부 스케치", "concept": "방열판 세 겹으로 가 볼까 해.",
                                "text": DRAFT_TEXT.strip()}
        assert end["remarks"] == ["방열판 세 겹은 좀 과하지 않아?"]
        assert link.asked == [(begun.created_at, NOW)]
        life = await data.list_life(ME, NO_PERSONA)
        (piece,) = [e for e in life if e.kind is LifeKind.WORK]
        assert (piece.entry_id, piece.draft, piece.title, piece.created_at) == (begun.entry_id, False, "냉각부", NOW)
        (project,) = [e for e in life if e.kind is LifeKind.ARC]
        assert project.so_far == "포신과 냉각부까지 정했다."
        end_write, start_write, _ = await data.list_life_writes(ME)
        assert (start_write.job, start_write.status, start_write.thought) == (
            LifeJob.WORK_START, LifeWriteStatus.APPLIED, "오늘은 손이 좀 무겁다. 그래도 냉각부는 해야지.")
        assert (end_write.job, end_write.status, end_write.plan_id, end_write.charged_tokens) == (
            LifeJob.WORK_END, LifeWriteStatus.APPLIED, plan.entry_id, 4_900)
        assert await writer.work(ME) is WriteOutcome.NOTHING  # made
        assert len(provider.sent) == 2

    asyncio.run(scenario())


def test_without_a_start_the_end_makes_the_piece_whole() -> None:
    async def scenario() -> None:
        data, provider, writer, _ = setup(Measured(final(piece_answer()), 2_000, 900))
        arc, plan = await planned(data)
        assert await writer.work_start(ME) is WriteOutcome.NOTHING  # its half hour is gone
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        assert "draft" not in told(provider) and "remarks" not in told(provider)
        (piece,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        assert (piece.plan_id, piece.arc_id, piece.title, piece.draft) == (plan.entry_id, arc.entry_id, "냉각부", False)

    asyncio.run(scenario())


def test_a_bad_answer_is_tried_again_three_times_a_step_and_a_finished_project_closes() -> None:
    async def scenario() -> None:
        data, provider, writer, _ = setup(final("not json"), final(piece_answer(piece="짧다")), final("{}"))
        await planned(data)
        assert [await writer.work(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * ATTEMPTS_PER_DAY
        assert await writer.work(ME) is WriteOutcome.GAVE_UP
        assert len(provider.sent) == ATTEMPTS_PER_DAY
        assert [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK] == []
        data, _, writer, _ = setup(final("no"), final(start_answer(draft="짧")), final("{}"), now=START)
        await planned(data)
        assert [await writer.work_start(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * 3
        assert await writer.work_start(ME) is WriteOutcome.GAVE_UP
        data, _, writer, _ = setup(final(piece_answer(done="설정집을 끝냈다.")))
        arc, _ = await planned(data)
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        (closed,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.ARC]
        assert (closed.entry_id, closed.ending, closed.arc_open) == (arc.entry_id, "설정집을 끝냈다.", False)

    asyncio.run(scenario())


def test_the_work_shares_the_daily_budget_and_waits_for_its_time() -> None:
    async def scenario() -> None:
        data, provider, writer, _ = setup(final(piece_answer()), settings=LifeSettings("m", daily_tokens=100,
                                                                                       time_zone="Asia/Seoul"))
        await planned(data)
        assert await writer.work(ME) is WriteOutcome.DEFERRED
        assert provider.sent == []
        # not due yet (still at work), nor once its window is gone
        for moment in (datetime(2026, 11, 25, 11, 30, tzinfo=UTC), datetime(2026, 11, 25, 18, 1, tzinfo=UTC)):
            data, provider, writer, _ = setup(final(piece_answer()), now=moment)
            await planned(data)
            assert await writer.work(ME) is WriteOutcome.NOTHING

    asyncio.run(scenario())


class EditingAdapter(ScriptedAdapter):
    """Runs ``meanwhile`` while the first call is out: the user's edit lands before its answer."""

    def __init__(self) -> None:
        super().__init__(memory_passes=True)
        self.meanwhile: Any = None

    async def invoke_prepared(self, request: Any, sink: Any = None, capture: Any = None) -> Any:
        if self.meanwhile is not None:
            hook, self.meanwhile = self.meanwhile, None
            await hook()
        return await super().invoke_prepared(request, sink, capture)


def test_a_draft_edited_while_the_end_waits_is_not_overwritten_and_the_next_round_uses_the_edit() -> None:
    """Review P0-3: the stale piece is thrown away (an attempt); the next round finishes the user's draft."""
    async def scenario() -> None:
        data = InMemoryData(InMemoryStore())
        provider = EditingAdapter()
        provider.extend([final(start_answer()), final(piece_answer()), final(piece_answer())])
        moments = [START]
        writer = LifeWriter(data, provider, SETTINGS, clock=lambda: moments[0], composer=Composer())
        await planned(data)
        assert await writer.work_start(ME) is WriteOutcome.WRITTEN
        (draft,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        mine = "사용자가 고친 스케치: 방열판은 두 겹으로. " * 3

        async def edit() -> None:
            await data.edit_life(ME, draft.entry_id, mine, expected_revision=draft.revision)

        provider.meanwhile = edit
        moments[0] = NOW
        assert await writer.work(ME) is WriteOutcome.FAILED
        (record, *_) = await data.list_life_writes(ME)
        assert record.error == "stale: the draft changed"
        (kept,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        assert (kept.text, kept.draft, kept.origin) == (mine.strip(), True, LifeOrigin.USER)
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        assert told(provider)["draft"]["text"] == mine.strip()
        (piece,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        assert (piece.entry_id, piece.draft) == (draft.entry_id, False)

    asyncio.run(scenario())


def test_a_plan_given_up_never_blocks_a_later_one_due_at_the_same_time() -> None:
    """Review P1-7: of two plans due at once, the earliest with tries left is worked (start and end)."""
    async def scenario() -> None:
        data, provider, writer, moments = setup(final("no"), final("no"), final("no"), final(start_answer()),
                                                final("no"), final("no"), final("no"), final(piece_answer()),
                                                now=START)
        arc, first = await planned(data)
        write_id = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-24", "scripted", "m", b"{}",
                                                                  "application/json", 10))
        await data.mark_life_write_dispatched(write_id)
        await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(5, 5)))
        second = (await data.apply_life_write(write_id, LifePlan(
            "또 하루.", (("2026-11-25", "같은 시각의 다른 일정."),), times=("20:00",), plan_arcs=(arc.entry_id,)),
            now=NOW - timedelta(days=1)))[-1]
        assert [p.entry_id for p in work.starts_due(await data.list_life(ME, NO_PERSONA), START, SEOUL)] == [
            first.entry_id, second.entry_id]
        assert [await writer.work_start(ME) for _ in range(3)] == [WriteOutcome.FAILED] * 3
        assert await writer.work_start(ME) is WriteOutcome.WRITTEN  # the second plan, not GAVE_UP
        (begun,) = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK]
        assert begun.plan_id == second.entry_id
        moments[0] = NOW
        assert [await writer.work(ME) for _ in range(3)] == [WriteOutcome.FAILED] * 3  # the first plan's end
        assert await writer.work(ME) is WriteOutcome.WRITTEN
        finished = [e for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.WORK and not e.draft]
        assert [e.plan_id for e in finished] == [second.entry_id]
        assert await writer.work(ME) is WriteOutcome.GAVE_UP and await writer.work_start(ME) is WriteOutcome.NOTHING

    asyncio.run(scenario())
