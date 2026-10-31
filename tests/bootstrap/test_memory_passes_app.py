"""Memory passes in the Application (V3-P85 §12, §4.1): they run only with a
checked model, hear the Runner's job events, and read a Conversation once it
went quiet after its job."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from himari.bootstrap.application import Application, _JobListeners, memory_pass_settings
from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import PassStatus
from himari.memory.api import MemoryPassSettings, PassOutcome
from himari.provider.api import NativeInvocationOutcome, NativeNoResponse, NativePreparedRequest, NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


def settings(adapter: ScriptedAdapter, **options: object) -> tuple[MemoryPassSettings | None, str]:
    values: dict[str, object] = {"memory": True, "auto": True, "model_id": "m", "idle_minutes": 10,
                                 "daily_tokens": 1_000_000, "time_zone": "Asia/Seoul", **options}
    return memory_pass_settings(adapter, **values)  # type: ignore[arg-type]


def test_memory_passes_run_only_with_a_model_checked_for_them() -> None:
    checked, unchecked = ScriptedAdapter(memory_passes=True), ScriptedAdapter()
    assert settings(checked, memory=False) == (None, "off (HIMARI_MEMORY=0)")
    assert settings(checked, auto=False) == (None, "off (HIMARI_MEMORY_AUTO=0)")
    assert settings(checked, model_id=None) == (None, "off (no HIMARI_MEMORY_MODEL)")
    assert settings(unchecked) == (None, "off (m is not checked for memory passes)")
    found, told = settings(checked, idle_minutes=5, daily_tokens=200_000)
    assert found == MemoryPassSettings("m", None, timedelta(minutes=5), 200_000, "Asia/Seoul")
    assert told == "on (m, after 5 quiet minutes, 200,000 tokens a day each)"


class Failing:
    def job_started(self, *_: object) -> None:
        raise RuntimeError("broken")

    accepted = run_ended = job_ended = job_started


class Hearing:
    def __init__(self) -> None:
        self.heard: list[str] = []

    def job_started(self, *_: object) -> None:
        self.heard.append("started")

    def accepted(self, *_: object) -> None:
        self.heard.append("accepted")

    def run_ended(self, *_: object) -> None:
        self.heard.append("run")

    def job_ended(self, *_: object) -> None:
        self.heard.append("ended")


def test_every_listener_hears_each_job_event_though_one_fails() -> None:
    hearing = Hearing()
    listeners = _JobListeners(Failing(), hearing)
    scope = TrustedScope(ME, ConversationId("c"))
    listeners.job_started(scope, None, None)  # type: ignore[arg-type]
    listeners.accepted(scope)
    listeners.run_ended(scope)
    listeners.job_ended(scope, None)  # type: ignore[arg-type]
    assert hearing.heard == ["started", "accepted", "run", "ended"]


def test_an_unchecked_model_runs_no_pass(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await Application.open(tmp_path / "data", ScriptedAdapter(), model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model")
        try:
            assert app.memory_passes is None
            assert app.auto_memory == "off (scripted-model is not checked for memory passes)"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_conversation_is_read_once_it_went_quiet_after_its_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        note = {"op": "add", "category": "project", "text": "Builds HIMARI.", "evidence": [1]}
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("Nice!"),))], memory_passes=True)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model")
        try:
            passes = app.memory_passes
            assert passes is not None and app.auto_memory.startswith("on (scripted-model")
            passes._settings = MemoryPassSettings("scripted-model", idle=timedelta(milliseconds=50))  # noqa: SLF001
            adapter.extend([NormalizedFinal((TextBlock(json.dumps({"notes": [note], "episode": None})),))])
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "I build HIMARI, the companion agent.")
            await app.runner.wait(scope)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if await app.data.list_memories(ME):
                    break
            assert [n.text for n in await app.data.list_memories(ME)] == ["Builds HIMARI."]
            (record,) = await app.data.list_passes(scope)
            assert (record.from_sequence, record.through_sequence) == (0, 2)
        finally:
            await app.close()

    asyncio.run(scenario())


class Hanging(ScriptedAdapter):
    """Sends and never answers: the process stops while it waits."""

    def __init__(self) -> None:
        super().__init__(memory_passes=True)
        self.waiting = asyncio.Event()

    async def invoke_prepared(self, request: NativePreparedRequest, sink: object = None,
                              capture: object = None) -> NativeInvocationOutcome:
        if b"memory-pass-input" not in request.body:
            return await super().invoke_prepared(request)
        self.sent.append(request)
        self.waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("never answered")


def test_on_sqlite_a_failure_and_a_stop_mid_call_are_counted_and_the_part_is_read_after_a_restart(
        tmp_path: Path) -> None:
    # PR #135 re-review: a Provider failure and a process restart together, on the real store.
    async def wait_for(check: Callable[[], Awaitable[bool]]) -> None:
        for _ in range(200):
            if await check():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("timed out")

    async def scenario() -> None:
        quick = MemoryPassSettings("scripted-model", idle=timedelta(milliseconds=30))
        failing = ScriptedAdapter([NormalizedFinal((TextBlock("Nice!"),)), NativeNoResponse("http_500")],
                                  memory_passes=True)
        app = await Application.open(tmp_path / "data", failing, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model")
        assert app.memory_passes is not None
        app.memory_passes._settings = quick  # noqa: SLF001
        scope = TrustedScope(ME, await app.create_conversation(ME))
        await app.send(scope, "I build HIMARI, the companion agent.")
        await app.runner.wait(scope)

        async def failed_once() -> bool:
            return (await app.data.read_memory_cursor(scope)).failures == 1

        await wait_for(failed_once)
        await app.close()

        hanging = Hanging()
        app = await Application.open(tmp_path / "data", hanging, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model")
        assert app.memory_passes is not None
        app.memory_passes._settings = quick  # noqa: SLF001
        app.memory_passes._clock = lambda: datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001 — past the retry
        task = asyncio.create_task(app.memory_passes.read(scope))
        await asyncio.wait_for(hanging.waiting.wait(), 5)
        task.cancel()  # the stop: the pass is left RUNNING, its call sent
        await asyncio.gather(task, return_exceptions=True)
        await app.close()

        note = {"op": "add", "category": "project", "text": "Builds HIMARI.", "evidence": [1]}
        answering = ScriptedAdapter([NormalizedFinal((TextBlock(json.dumps({"notes": [note], "episode": None})),))],
                                    memory_passes=True)
        app = await Application.open(tmp_path / "data", answering, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model")
        try:
            passes = app.memory_passes
            assert passes is not None
            first, cut = await app.data.list_passes(scope)
            assert (first.status, first.error, first.unmetered_tokens > 0) == (PassStatus.FAILED, "http_500", True)
            assert (cut.status, cut.reserved_tokens, cut.unmetered_tokens > 0) == (PassStatus.INTERRUPTED, 0, True)
            cursor = await app.data.read_memory_cursor(scope)
            assert (cursor.through_sequence, cursor.failures) == (0, 2)  # counted at this start
            passes._clock = lambda: datetime.now(UTC) + timedelta(hours=2)  # noqa: SLF001
            assert await passes.read(scope) == [PassOutcome.APPLIED, PassOutcome.NOTHING]
            assert [n.text for n in await app.data.list_memories(ME)] == ["Builds HIMARI."]
            assert (await app.data.read_memory_cursor(scope)).failures == 0
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_turn_sees_the_loops_and_the_recent_episodes_of_other_conversations(tmp_path: Path) -> None:
    # V3-P85 §8: read when the turn starts, its own Conversation's episodes left out.
    from himari.data.api import EpisodePlan, MemoryCategory, PassKind, PassStart
    from himari.prompt.api import state_block

    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("ok"),))] * 3)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None)
        try:
            earlier = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(earlier, "Let's fix the bubbles in the companion app.")
            await app.runner.wait(earlier)
            await app.data.rename_conversation(earlier, "말풍선")
            last = (await app.data.read_events(earlier))[-1].sequence
            pass_id = await app.data.begin_pass(earlier, PassStart(PassKind.LIVE, 0, last, last, "p", "m"))
            now = datetime.now(UTC)
            await app.data.apply_pass(pass_id, [], EpisodePlan("Fixed the bubbles together.", now, now, (1,), ()),
                                      now=now)
            await app.add_memory(ME, MemoryCategory.LOOP, "Interview on Nov 4.")
            await app.send(earlier, "And one more thing.")  # its own episode: not shown here
            await app.runner.wait(earlier)
            own = adapter.prepared_inputs[-1].current_state
            assert own.episodes == () and own.episode_count == 1
            fresh = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(fresh, "Where were we?")
            await app.runner.wait(fresh)
            state = adapter.prepared_inputs[-1].current_state
            assert [(f.episode.text, f.title) for f in state.episodes] == [("Fixed the bubbles together.", "말풍선")]
            block = state_block(state)
            assert block is not None and "recent_episodes" in block and "Interview on Nov 4." in block
        finally:
            await app.close()

    asyncio.run(scenario())
