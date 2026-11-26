"""The day session's turns in the Driver (character-day v0.1 §5.5, §18.4, §18.6, §10): a message a sleeping HIMARI
meets with nothing is recorded and starts no Run (nor is ever answered on its own); a mumble is one Run, half asleep,
offered no Tool; a waking is an ordinary turn, the schedule Tools offered there only; a schedule Tool's change is
written with its result, refused with the sentence the model can act on; a turn with no answer at all goes on, once,
on the backup model."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from testkit import SCOPE, calls, final, new_store

from himari.bootstrap import build_runtime
from himari.common import TextBlock
from himari.control.api import DriveState, ModelCatalog, ModelOffer
from himari.control.runner import BackgroundRunner
from himari.data.api import night_of
from himari.data.api import (
    AsleepAnswer,
    DataError,
    DataErrorCode,
    DayBefore,
    WakeKind,
    WakeReason,
    LifeKind,
    LifeOrigin,
    MessageOrigin,
    OriginKind,
    PlanKind,
    PlanWrite,
    SleepState,
    SleepView,
    UserMessage,
)
from himari.prompt.api import SystemSpec
from himari.provider.api import NativeNoResponse, retryable
from himari.provider.scripted import ScriptedAdapter
from himari.tool.schedule import GO_TO_SLEEP, SCHEDULE_TOOL_IDS, SCHEDULE_WRITE

ZONE = "Asia/Seoul"
WEB = MessageOrigin(OriginKind.WEB)


class Gate:
    """A day keeper as the Driver sees it: this Conversation is the day session (or not), asleep or not, closed (or
    fenced for its closing) or not."""

    def __init__(self, answer: AsleepAnswer | None = None, state: SleepState = SleepState.AWAKE, *,
                 day: bool = True, closed: bool = False) -> None:
        self.reply, self.state, self.day, self.closed = answer, state, day, closed
        self.asked: list[MessageOrigin | None] = []

    async def answer(self, scope: object, origin: MessageOrigin | None) -> AsleepAnswer | None:
        self.asked.append(origin)
        return self.reply if self.day else None

    async def view(self, scope: object) -> SleepView | None:
        return SleepView(self.state) if self.day else None

    async def day_session(self, scope: object) -> bool:
        return self.day

    async def before(self, scope: object) -> DayBefore | None:
        return None

    async def admit(self, scope: object) -> None:
        if self.closed:
            raise DataError(DataErrorCode.DAY_CLOSED, "closed")


def day_runtime(gate: Gate, script: list[object], *, models: ModelCatalog | None = None,
                backup: str | None = None) -> tuple[object, ScriptedAdapter]:
    adapter = ScriptedAdapter(script)  # type: ignore[arg-type]
    store = new_store()
    rt = build_runtime(adapter, store=store, life=True, system=SystemSpec(companion=True), time_zone=ZONE,
                       day=gate, models=models, backup_model=backup)  # type: ignore[arg-type]
    store.create_conversation(SCOPE)
    return rt, adapter


def said(text: str) -> UserMessage:
    return UserMessage((TextBlock(text),), origin=WEB)


def test_a_message_met_with_nothing_is_recorded_and_starts_no_run_and_never_one_of_its_own() -> None:
    async def scenario() -> None:
        gate = Gate(AsleepAnswer.NONE, SleepState.ASLEEP)
        rt, adapter = day_runtime(gate, [])
        event = await rt.driver.accept(SCOPE, said("자?"))  # type: ignore[attr-defined]
        assert event.payload.while_asleep is AsleepAnswer.NONE and gate.asked == [WEB]
        result = await rt.driver.resume(SCOPE)  # type: ignore[attr-defined]
        assert result.state is DriveState.IDLE and adapter.sent == []
        driven = await rt.driver.drive(SCOPE, said("일어나"))  # type: ignore[attr-defined]
        assert (driven.state, driven.detail) == (DriveState.IDLE, "asleep") and adapter.sent == []
        # a client's own "while_asleep" is never kept: the keeper decides
        gate.reply = None
        event = await rt.driver.accept(SCOPE, UserMessage((TextBlock("x"),), origin=WEB,  # type: ignore[attr-defined]
                                                          while_asleep=AsleepAnswer.WAKE))
        assert event.payload.while_asleep is None

    asyncio.run(scenario())


def test_a_mumble_is_one_run_half_asleep_with_no_tool() -> None:
    async def scenario() -> None:
        rt, adapter = day_runtime(Gate(AsleepAnswer.MUMBLE, SleepState.ASLEEP),
                                  [calls(("schedule_read", {"days": 2}))])
        await rt.driver.accept(SCOPE, said("일어나 봐"))  # type: ignore[attr-defined]
        result = await rt.driver.resume(SCOPE)  # type: ignore[attr-defined]
        assert result.state is DriveState.RUN_LIMIT_REACHED and len(adapter.sent) == 1  # one Run, never more
        asked = adapter.prepared_inputs[0]
        assert asked.tool_catalog == () and asked.request_options.half_asleep
        assert asked.current_state.sleep is not None and asked.current_state.sleep.state is SleepState.HALF_ASLEEP
        records = await rt.data.read_tool_executions(SCOPE, result.run_ids[0])  # type: ignore[attr-defined]
        assert [r.status.value for r in records] == ["cancelled"]  # nothing runs half asleep

    asyncio.run(scenario())


def test_a_waking_is_an_ordinary_turn_with_the_schedule_tools_offered_only_in_the_day_session() -> None:
    async def scenario() -> None:
        rt, adapter = day_runtime(Gate(AsleepAnswer.WAKE, SleepState.AWAKE), [final("…응, 왜?")])
        await rt.driver.accept(SCOPE, said("일어나!"))  # type: ignore[attr-defined]
        assert (await rt.driver.resume(SCOPE)).state is DriveState.COMPLETED  # type: ignore[attr-defined]
        asked = adapter.prepared_inputs[0]
        assert SCHEDULE_TOOL_IDS <= {d.tool_id for d in asked.tool_catalog}
        assert asked.request_options.day_session and not asked.request_options.half_asleep
        assert asked.current_state.schedule == () and asked.current_state.sleep == SleepView(SleepState.AWAKE)
        rt, adapter = day_runtime(Gate(day=False), [final("응")])
        await rt.driver.drive(SCOPE, said("안녕"))  # type: ignore[attr-defined]
        asked = adapter.prepared_inputs[0]
        assert not SCHEDULE_TOOL_IDS & {d.tool_id for d in asked.tool_catalog}
        assert asked.current_state.sleep is None and not asked.request_options.day_session

    asyncio.run(scenario())


def test_a_schedule_write_is_kept_with_its_result_and_a_refusal_is_said_to_the_model() -> None:
    async def scenario() -> None:
        local = datetime.now(ZoneInfo(ZONE))
        tomorrow, after = (local.date() + timedelta(days=1)).isoformat(), (local.date() + timedelta(days=2)).isoformat()
        rt, adapter = day_runtime(Gate(), [
            calls((SCHEDULE_WRITE, {"date": tomorrow, "start": "15:00", "minutes": 90, "kind": "together",
                                    "text": "같이 게임", "reason": None}),
                  (SCHEDULE_WRITE, {"date": after, "start": "03:00", "minutes": 120, "kind": "together",
                                    "text": "방송 같이 보기"})),
            final("적어 뒀어.")])
        sleep = await rt.data.write_plan(SCOPE.principal_id, 0, PlanWrite(  # type: ignore[attr-defined]
            after, "00:30", 460, PlanKind.SLEEP, "잠"), origin=LifeOrigin.WRITER, zone=ZONE, now=local)
        result = await rt.driver.drive(SCOPE, said("내일 3시에 같이 게임하자"))  # type: ignore[attr-defined]
        assert result.state is DriveState.COMPLETED
        written, refused = await rt.data.read_tool_executions(SCOPE, result.run_ids[0])  # type: ignore[attr-defined]
        assert written.status.value == "succeeded"
        assert refused.status.value == "failed" and refused.model_result is not None
        told = refused.model_result.blocks[0].text  # type: ignore[union-attr]
        assert told.startswith("Not written: It overlaps your sleep slot") and f"id {sleep.entry_id}" in told
        plans = [e for e in await rt.data.list_life(SCOPE.principal_id, 0) if e.kind is LifeKind.PLAN]  # type: ignore[attr-defined]
        promise = next(e for e in plans if e.plan_kind is PlanKind.TOGETHER)
        assert (promise.day, promise.time_of_day, promise.minutes, promise.origin) == (
            tomorrow, "15:00", 90, LifeOrigin.HIMARI)

    asyncio.run(scenario())


def test_go_to_sleep_is_refused_by_day_with_its_reason() -> None:
    async def scenario() -> None:
        rt, adapter = day_runtime(Gate(), [calls((GO_TO_SLEEP, {"reason": "잘 자"})), final("잘 자~")])
        night = night_of(datetime.now(ZoneInfo(ZONE)))
        if night is not None:  # tonight's slot, as the keeper places it on first need (the Tool only moves it)
            await rt.data.write_plan(SCOPE.principal_id, 0, PlanWrite(  # type: ignore[attr-defined]
                night.isoformat(), "21:00", 600, PlanKind.SLEEP, "잠"), origin=LifeOrigin.WRITER, zone=ZONE,
                now=datetime.now(ZoneInfo(ZONE)))
        result = await rt.driver.drive(SCOPE, said("잘 자"))  # type: ignore[attr-defined]
        (record,) = await rt.data.read_tool_executions(SCOPE, result.run_ids[0])  # type: ignore[attr-defined]
        local = datetime.now(ZoneInfo(ZONE)).time()
        if record.status.value == "failed":  # 05:00 to 21:00 here: not yet
            assert "You can go to sleep early from 21:00" in record.model_result.blocks[0].text  # type: ignore[union-attr]
        else:
            state = await rt.data.read_day_state(SCOPE.principal_id, 0)  # type: ignore[attr-defined]
            assert state.sleep_requested_at is not None and (local.hour >= 21 or local.hour <= 5)

    asyncio.run(scenario())


def test_a_run_with_no_answer_at_all_goes_on_once_on_the_backup_model() -> None:
    """§10, §18.4: a retryable failure moves the rest of the turn to the backup (the failed Run stays); never a
    second time, never from the backup itself, never for a failure ours to fix."""
    models = ModelCatalog((ModelOffer("main"), ModelOffer("backup")), "main")

    async def scenario() -> None:
        rt, adapter = day_runtime(Gate(day=False), [NativeNoResponse("http_503"), final("예비로 답함")],
                                  models=models, backup="backup")
        result = await rt.driver.drive(SCOPE, said("안녕"))  # type: ignore[attr-defined]
        assert result.state is DriveState.COMPLETED and len(result.run_ids) == 2
        first = await rt.data.read_run(SCOPE, result.run_ids[0])  # type: ignore[attr-defined]
        second = await rt.data.read_run(SCOPE, result.run_ids[1])  # type: ignore[attr-defined]
        assert (first.model_id, first.status.value, second.model_id) == ("main", "failed_no_response", "backup")
        assert (await rt.data.read_model_setting(SCOPE)) is None  # type: ignore[attr-defined]  # the choice stays
        rt, _ = day_runtime(Gate(day=False), [NativeNoResponse("http_503"), NativeNoResponse("http_502")],
                            models=models, backup="backup")
        result = await rt.driver.drive(SCOPE, said("안녕"))  # type: ignore[attr-defined]
        assert (result.state, result.detail, len(result.run_ids)) == (DriveState.PROVIDER_FAILED, "http_502", 2)
        rt, _ = day_runtime(Gate(day=False), [NativeNoResponse("http_400")], models=models, backup="backup")
        result = await rt.driver.drive(SCOPE, said("안녕"))  # type: ignore[attr-defined]
        assert (result.state, len(result.run_ids)) == (DriveState.PROVIDER_FAILED, 1)

    asyncio.run(scenario())
    with pytest.raises(ValueError, match="not offered"):
        day_runtime(Gate(), [], models=models, backup="nowhere")


@pytest.mark.parametrize(("code", "falls_back"), [
    ("connect_failed:ConnectError", True), ("http_408", True), ("http_429", True), ("http_500", True),
    ("http_503", True), ("provider_error_502", True), ("provider_error_429", True), ("provider_error_None", True),
    ("http_400", False), ("http_401", False), ("http_402", False), ("http_403", False),
    # review 12: the account's and ours in a stream's or body's error too
    ("provider_error_400", False), ("provider_error_401", False), ("provider_error_402", False),
    ("provider_error_403", False), ("image_missing", False), ("", False), ("http_5xx", False)])
def test_a_retryable_failure_is_one_another_model_may_not_share(code: str, falls_back: bool) -> None:
    assert retryable(code) is falls_back


def test_a_mumble_with_no_answer_goes_on_the_backup_model_its_one_exception_to_one_run() -> None:
    """§5.5, §10 (review 12): a mumble is one Run — but a primary that answered nothing at all leaves the turn's one
    Run to the backup model (the failed Run stays recorded): two Runs, the second answering, never a third."""
    models = ModelCatalog((ModelOffer("main"), ModelOffer("backup")), "main")

    async def scenario() -> None:
        rt, adapter = day_runtime(Gate(AsleepAnswer.MUMBLE, SleepState.ASLEEP),
                                  [NativeNoResponse("http_503"), final("음냐…")], models=models, backup="backup")
        await rt.driver.accept(SCOPE, said("자?"))  # type: ignore[attr-defined]
        result = await rt.driver.resume(SCOPE)  # type: ignore[attr-defined]
        assert result.state is DriveState.COMPLETED and len(result.run_ids) == 2
        runs = [await rt.data.read_run(SCOPE, r) for r in result.run_ids]  # type: ignore[attr-defined]
        assert [(r.model_id, r.status.value) for r in runs][0] == ("main", "failed_no_response")
        assert runs[1].model_id == "backup" and adapter.prepared_inputs[1].request_options.half_asleep
        rt, adapter = day_runtime(Gate(AsleepAnswer.MUMBLE, SleepState.ASLEEP),
                                  [NativeNoResponse("http_503"), calls(("schedule_read", {"days": 2}))],
                                  models=models, backup="backup")
        await rt.driver.accept(SCOPE, said("자?"))  # type: ignore[attr-defined]
        result = await rt.driver.resume(SCOPE)  # type: ignore[attr-defined]
        assert result.state is DriveState.RUN_LIMIT_REACHED and len(adapter.sent) == 2  # the backup's one Run only

    asyncio.run(scenario())


def test_a_closed_or_closing_day_takes_no_message_wake_resume_or_compaction() -> None:
    """§3.2, §6 (review 2, 3): the Driver asks the keeper before anything of a message or a wake is written; the Runner
    refuses a resume or a compaction at once in a day closing or closed (DAY_CLOSED)."""
    async def scenario() -> None:
        gate = Gate(closed=True)
        rt, adapter = day_runtime(gate, [])
        for refused in (lambda: rt.driver.accept(SCOPE, said("늦었다")),  # type: ignore[attr-defined]
                        lambda: rt.driver.drive(SCOPE, said("늦었다")),  # type: ignore[attr-defined]
                        lambda: rt.driver.accept_wake(SCOPE, (WakeReason(WakeKind.TRY),), "d1")):  # type: ignore[attr-defined]
            with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.DAY_CLOSED):
                await refused()
        assert await rt.data.read_events(SCOPE) == () and gate.asked == []  # type: ignore[attr-defined]
        runner = BackgroundRunner(rt.driver)  # type: ignore[attr-defined]
        runner.day_closing = lambda scope: True
        yielded: list[object] = []

        async def yield_wake(scope: object) -> None:  # second review 9: a running wake would be cancelled here
            yielded.append(scope)

        runner._yield_wake = yield_wake  # type: ignore[method-assign]  # noqa: SLF001
        for submitted in (lambda: runner.submit(SCOPE, None), lambda: runner.submit_compaction(SCOPE),
                          lambda: runner.submit(SCOPE, said("늦었다"))):
            with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.DAY_CLOSED):
                await submitted()
        assert not runner.status(SCOPE).running and adapter.sent == []
        assert yielded == []  # a fenced day's wake is left to settle: the message never cancels it

    asyncio.run(scenario())
