"""V3-P83 §6.3, §6.4: every conversation is told the time, read once when the turn starts."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from himari.bootstrap.application import Application
from himari.common import CallId, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope
from himari.prompt.api import COMPANION_HEAD, TIME_NOTE, UserTurn
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


def two_runs() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": "a.txt", "content": "x"}),)),
        NormalizedFinal((TextBlock("done"),)),
    ])


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


def test_a_turn_is_told_when_it_started_and_the_message_when_it_came(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = two_runs()
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hello")
            await app.runner.wait(scope)
            first, second = adapter.prepared_inputs
            now = first.current_state.now
            assert now is not None and now == second.current_state.now  # read once for the whole turn
            assert now.utcoffset() == timedelta(hours=9)  # HIMARI_TIME_ZONE's default, Asia/Seoul
            turn = first.conversation[0]
            assert isinstance(turn, UserTurn)
            line = turn.blocks[0]
            assert isinstance(line, TextBlock) and line.text.startswith("<time>") and line.text.endswith("+09:00</time>")
            assert TIME_NOTE in "\n".join(first.stable_prefix) and COMPANION_HEAD in first.stable_prefix[5]  # its own section
        finally:
            await app.close()

    asyncio.run(scenario())


def test_with_the_companion_off_the_time_is_still_told(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("hi"),))])
        app = await opened(tmp_path, adapter, companion=False, time_zone="Europe/London")
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hello")
            await app.runner.wait(scope)
            (sent,) = adapter.prepared_inputs
            assert TIME_NOTE in "\n".join(sent.stable_prefix) and COMPANION_HEAD not in "\n".join(sent.stable_prefix)
            now = sent.current_state.now
            assert now is not None and str(now.tzinfo) == "Europe/London"
        finally:
            await app.close()

    asyncio.run(scenario())
