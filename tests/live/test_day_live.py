"""Live check of HIMARI's day (character-day v0.1 §16): opt-in, ``HIMARI_LIVE=1`` plus the key. Four scenes, once
each, on ``HIMARI_LIVE_DAY_MODEL`` (default deepseek/deepseek-v4.1-flash-1017), through the real Driver with the
schedule Tools on an in-memory store; a day gate stands in for the day keeper (it says how HIMARI sleeps), and the
Driver's and the Tools' clocks are held at the scene's time on today's date in Seoul.

1. The promise: at 22:40 "새벽 3시에 방송 같이 보자" — a ``together`` slot at 03:00 is written and the sleep slot is
   moved off it (a sleep slot is never removed).
2. Good night: at 23:30 "나 이제 잘게. 잘 자" — ``go_to_sleep`` is called (the keeper would let it fall asleep).
3. The mumble and the waking: asleep, a first message gets nothing (no Run); the next one a half-asleep turn — one
   short line, no Tool offered; then a waking turn answers the message that woke it.
4. The diary in the day session's context: after the day's talk, the diary's request (the session's own, the task
   last) answers a thought and the JSON, passes the review's checks and carries something of that talk.

Checks are mechanical (the calls made, the slots kept, the shape); whether it is like a person is the owner's to
judge (roadmap §10). With ``HIMARI_LIVE_DAY_OUT`` set to a file, every answer is appended as one JSON line.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from testkit import SCOPE, new_store

from himari.bootstrap import build_runtime, read_env_file
from himari.common import TextBlock, drop_thought, split_marks
from himari.data.api import (
    NO_PERSONA,
    AsleepAnswer,
    AssistantFinal,
    DayBefore,
    FellBy,
    LifeEntry,
    LifeOrigin,
    MessageOrigin,
    OriginKind,
    PlanKind,
    PlanWrite,
    SleepRecord,
    SleepState,
    SleepView,
    UserMessage,
    WokeBy,
    plan_span,
)
from himari.life import review
from himari.prompt.api import SystemSpec
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE
from himari.provider.api import NativeReceived, NormalizedFinal, ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter
from himari.tool.schedule import GO_TO_SLEEP, SCHEDULE_TOOL_IDS, SCHEDULE_WRITE

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("HIMARI_LIVE_DAY_MODEL", "deepseek/deepseek-v4.1-flash-1017")
OUT = os.environ.get("HIMARI_LIVE_DAY_OUT")
ZONE = "Asia/Seoul"
SEOUL = ZoneInfo(ZONE)
ME = SCOPE.principal_id
WEB = MessageOrigin(OriginKind.WEB)
MUMBLE_CHARS = 80  # §5.5: "a short mumbled line"

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")


class Gate:
    """The day keeper as the Driver sees it: this Conversation is the day session; HIMARI sleeps as told."""

    def __init__(self, view: SleepView, answer: AsleepAnswer | None = None) -> None:
        self.shown, self.reply = view, answer

    async def answer(self, scope: object, origin: MessageOrigin | None) -> AsleepAnswer | None:
        return self.reply

    async def view(self, scope: object) -> SleepView | None:
        return self.shown

    async def day_session(self, scope: object) -> bool:
        return True

    async def before(self, scope: object) -> DayBefore | None:
        return None


def today() -> date:
    return datetime.now(SEOUL).date()


def at(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=SEOUL)


def effort() -> str | None:
    known = KNOWN_CAPABILITIES.get(MODEL, ProviderCapabilities(MODEL))
    return "low" if "low" in known.reasoning_efforts else None


def runtime(gate: Gate, now: datetime) -> Any:
    """The server's turn as Application.open builds it (the companion base, HIMARI's life, the day), the clocks
    held at ``now``."""
    store = new_store()
    rt = build_runtime(OpenRouterAdapter(KEY), store=store, model_id=MODEL, reasoning_effort=effort(),
                       instructions=COMPANION_BASE, system=SystemSpec(companion=True, proactive=True),
                       turn_guidance=True, life=True, memory=True, time_zone=ZONE, day=gate)  # type: ignore[arg-type]
    store.create_conversation(SCOPE)
    rt.driver._clock = lambda: now  # noqa: SLF001 — the scene's hour
    for tool_id in SCHEDULE_TOOL_IDS:
        rt.registry._handlers[tool_id]._clock = lambda: now  # noqa: SLF001
    return rt


async def slept(rt: Any, night: date, now: datetime, start: str = "00:30", minutes: int = 460) -> LifeEntry:
    """The keeper's sleep slot for the night of ``night`` (it starts after midnight)."""
    entry: LifeEntry = await rt.data.write_plan(ME, NO_PERSONA, PlanWrite(
        (night + timedelta(days=1)).isoformat(), start, minutes, PlanKind.SLEEP, "잠"),
        origin=LifeOrigin.WRITER, zone=ZONE, now=now)
    return entry


def said(text: str) -> UserMessage:
    return UserMessage((TextBlock(text),), origin=WEB)


async def answers(rt: Any) -> list[str]:
    """The answers as the user sees them: no thought, no marks."""
    return [split_marks("".join(b.text for b in drop_thought(e.payload.blocks) if isinstance(b, TextBlock)),
                        complete=True)[0].strip()
            for e in await rt.data.read_events(SCOPE) if isinstance(e.payload, AssistantFinal)]


async def tool_calls(rt: Any, run_ids: Any) -> list[tuple[str, str, Any]]:
    found = []
    for run_id in run_ids:
        for record in await rt.data.read_tool_executions(SCOPE, run_id):
            result = None if record.model_result is None else " ".join(
                b.text for b in record.model_result.blocks if isinstance(b, TextBlock))
            found.append((str(record.tool_id), record.status.value, result))
    return found


def record(scene: str, **more: Any) -> None:
    line = json.dumps({"scene": scene, "model": MODEL, **more}, ensure_ascii=False, default=str)
    print(line)
    if OUT:
        with open(OUT, "a", encoding="utf-8") as out:
            out.write(line + "\n")


def test_a_time_set_together_is_written_and_the_sleep_moved_off_it() -> None:
    async def scenario() -> None:
        now = at(today(), "22:40")
        gate = Gate(SleepView(SleepState.AWAKE))
        rt = runtime(gate, now)
        sleep = await slept(rt, today(), now)
        gate.shown = SleepView(SleepState.AWAKE, tonight=sleep)
        result = await rt.driver.drive(SCOPE, said("새벽 3시에 방송 같이 보자"))
        made = await tool_calls(rt, result.run_ids)
        plans = [e for e in await rt.data.list_life(ME, NO_PERSONA) if e.live_plan]
        record("promise", state=result.state.value, calls=made, answers=await answers(rt),
               plans=[(None if e.plan_kind is None else e.plan_kind.value, e.day, e.time_of_day, e.minutes, e.text,
                       e.reason) for e in plans])
        promise = [e for e in plans if e.plan_kind is PlanKind.TOGETHER]
        assert promise, made
        (moved,) = [e for e in plans if e.entry_id == sleep.entry_id]  # never removed
        kept, night = plan_span(promise[0], SEOUL), plan_span(moved, SEOUL)
        assert kept is not None and night is not None
        assert kept[0].time() == time(3, 0), promise[0]
        assert not (kept[0] < night[1] and night[0] < kept[1]), (promise[0], moved)  # the sleep is off it
        assert any(tool == SCHEDULE_WRITE and status == "succeeded" for tool, status, _ in made)

    asyncio.run(scenario())


def test_good_night_calls_go_to_sleep() -> None:
    async def scenario() -> None:
        now = at(today(), "23:30")
        gate = Gate(SleepView(SleepState.AWAKE))
        rt = runtime(gate, now)
        sleep = await slept(rt, today(), now)
        gate.shown = SleepView(SleepState.AWAKE, tonight=sleep)
        result = await rt.driver.drive(SCOPE, said("나 이제 잘게. 잘 자"))
        made = await tool_calls(rt, result.run_ids)
        state = await rt.data.read_day_state(ME, NO_PERSONA)
        record("good_night", state=result.state.value, calls=made, answers=await answers(rt),
               sleep_requested_at=state.sleep_requested_at)
        assert any(tool == GO_TO_SLEEP and status == "succeeded" for tool, status, _ in made), made
        assert state.sleep_requested_at is not None

    asyncio.run(scenario())


def test_asleep_a_mumble_is_one_short_line_and_a_waking_answers_what_woke_it() -> None:
    async def scenario() -> None:
        night = today()
        fell = at(night + timedelta(days=1), "00:30")
        now = at(night + timedelta(days=1), "02:10")
        sleep = SleepRecord(1, NO_PERSONA, fell, FellBy.SCHEDULE)
        gate = Gate(SleepView(SleepState.ASLEEP, deep=True, last=sleep), AsleepAnswer.NONE)
        rt = runtime(gate, now)
        nothing = await rt.driver.drive(SCOPE, said("자?"))
        assert not nothing.run_ids  # met with nothing: recorded, no Run
        gate.reply = AsleepAnswer.MUMBLE
        mumbled = await rt.driver.drive(SCOPE, said("야 자냐고~ 잠깐만 일어나 봐"))
        mumble = next(iter((await answers(rt))[-1:]), "")
        asked_tools = await tool_calls(rt, mumbled.run_ids)
        gate.reply = AsleepAnswer.WAKE
        woke = replace(sleep, woke_at=now, how_woke=WokeBy.USER, messages=3, mumbles=1,
                       first_message_at=now - timedelta(minutes=2), last_message_at=now)
        gate.shown = SleepView(SleepState.AWAKE, last=woke, woken_at=now)
        waking = await rt.driver.drive(SCOPE, said("일어나! 나 내일 아침 발표인데 너무 떨려서 잠이 안 와"))
        said_after = (await answers(rt))[-1]
        record("mumble_and_wake", mumble=mumble, mumble_runs=len(mumbled.run_ids), mumble_tools=asked_tools,
               waking=said_after, waking_state=waking.state.value)
        assert len(mumbled.run_ids) == 1 and asked_tools == []  # one Run, no Tool
        assert mumble.strip() and len(mumble.strip()) <= MUMBLE_CHARS and "\n" not in mumble.strip(), mumble
        assert any(word in said_after for word in ("발표", "떨", "잠")), said_after  # what woke it

    asyncio.run(scenario())


def test_the_diary_in_the_day_sessions_context_carries_the_days_talk() -> None:
    async def scenario() -> None:
        day = today()
        gate = Gate(SleepView(SleepState.AWAKE))
        rt = runtime(gate, at(day, "20:00"))
        await rt.driver.drive(SCOPE, said("오늘 회사에서 발표 드디어 끝났어! 부장님이 잘했대"))
        rt.driver._clock = lambda: at(day, "20:20")  # noqa: SLF001
        await rt.driver.drive(SCOPE, said("기념으로 저녁엔 떡볶이 먹었어 ㅎㅎ"))
        now = at(day, "22:00")
        rt.driver._clock = lambda: now  # noqa: SLF001
        sleep = await slept(rt, day, now)
        gate.shown = SleepView(SleepState.AWAKE, tonight=sleep)
        inputs = await rt.driver.turn_inputs(SCOPE)  # the session's next turn, as the writer gets it
        options = replace(inputs.request_options, max_output_tokens=review.REVIEW_MAX_OUTPUT_TOKENS, stream=False)
        entries = await rt.data.list_life(ME, NO_PERSONA)
        data = review.request_data(entries, (), day=day, now=now.astimezone(UTC), zone=SEOUL)
        asked = Composer().session_input(replace(inputs, request_options=options), review.REVIEW_TASK, data)
        provider = OpenRouterAdapter(KEY)
        outcome = await provider.invoke_prepared(provider.prepare_native_request(asked))
        assert isinstance(outcome, NativeReceived), outcome
        output = provider.normalize(outcome)
        assert isinstance(output, NormalizedFinal)
        text = review.answer_text(output.blocks)
        thought, body = review.self_answer(text)
        dropped: list[str] = []
        plan = review.parse_answer(body, day, review.sleep_spans(entries, SEOUL), SEOUL, dropped)
        usage = outcome.usage
        record("diary", thought=thought, diary=plan.diary, plans=list(plan.plans), minutes=list(plan.plan_minutes),
               dropped=dropped, mood=plan.mood, usage=None if usage is None else
               {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})
        assert plan.diary
        assert any(word in plan.diary for word in ("발표", "떡볶이")), plan.diary  # the day's talk is in it

    asyncio.run(scenario())
