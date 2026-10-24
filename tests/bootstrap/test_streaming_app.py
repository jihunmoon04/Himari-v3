"""Streaming in the Application (V3-P73 §9): on by default for models that
declare it, off with ``streaming=False``; the job's live output is the
Runner's."""

from __future__ import annotations

import asyncio
from pathlib import Path

from testkit import final, user

from himari.bootstrap.application import Application
from himari.common import PrincipalId, TrustedScope
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


def test_an_answer_streams_by_default_and_its_live_output_stays_after_the_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("streamed answer")], streaming=True)
        app = await opened(tmp_path, adapter)
        try:
            assert app.streaming_enabled
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.runner.submit(scope, user("hi"))
            await app.runner.wait(scope)
            live = app.runner.live_output(scope)
            assert adapter.sent[-1].streamed and live is not None and live.ended and live.text == "streamed answer"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_off_asks_for_no_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("whole answer")], streaming=True)
        app = await opened(tmp_path, adapter, streaming=False)
        try:
            assert not app.streaming_enabled
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.runner.submit(scope, user("hi"))
            await app.runner.wait(scope)
            assert not adapter.sent[-1].streamed and app.runner.live_output(scope) is None
        finally:
            await app.close()

    asyncio.run(scenario())
