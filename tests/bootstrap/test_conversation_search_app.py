"""Conversation search in the Application (V3-P72): through the Dispatcher's
schema check, across Conversations, the current one's compacted part only,
and the switch."""

from __future__ import annotations

import asyncio
from pathlib import Path

from testkit import calls, final, user

from himari.bootstrap.application import Application
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import ToolResultRecord
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")
SEARCH = {"conversation_search", "conversation_read"}


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


async def results(app: Application, scope: TrustedScope) -> list[str]:
    out = []
    for event in await app.data.read_events(scope):
        if isinstance(event.payload, ToolResultRecord):
            block = event.payload.model_result.blocks[0]
            assert isinstance(block, TextBlock)
            out.append(block.text)
    return out


def test_a_message_of_another_conversation_is_found_and_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("noted"),
                                   calls(("conversation_search", {"query": "배포 순서", "conversation": None})),
                                   final("found")])
        app = await opened(tmp_path, adapter)
        try:
            earlier = TrustedScope(ME, await app.create_conversation(ME))
            await app.runtime.driver.drive(earlier, user("배포 순서는 데이터 먼저, 그다음 웹으로 하자"))
            now = TrustedScope(ME, await app.create_conversation(ME))
            adapter.extend([calls(("conversation_read", {"conversation": earlier.conversation_id, "from": None})),
                            final("read")])
            await app.runtime.driver.drive(now, user("지난번에 정한 배포 순서가 뭐였지?"))
            await app.runtime.driver.drive(now, user("그 대화를 더 보여 줘"))
            found, read = await results(app, now)
            assert found.startswith(f"{earlier.conversation_id} (untitled) · ")
            assert found.endswith("#1 user: 배포 순서는 데이터 먼저, 그다음 웹으로 하자")
            assert read.splitlines()[0] == f"{earlier.conversation_id} (untitled)"
            assert "#2 HIMARI (" in read and "noted" in read
            assert SEARCH <= {d.tool_id for d in adapter.prepared_inputs[-1].tool_catalog}
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_current_conversation_is_searched_only_once_compacted(tmp_path: Path) -> None:
    # The owner's point (§3.2): the part a summary replaced must stay findable.
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("conversation_search", {"query": "zebra"})), final("a1"), final("a2"),
                                   final("SUMMARY"),
                                   calls(("conversation_search", {"query": "zebra third"})), final("a3")])
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            for text in ("The ZEBRA path is /srv/z.", "second"):
                await app.runtime.driver.drive(scope, user(text))
            await app.runtime.driver.compact(scope)  # as "Compact now"
            status = await app.context_status(scope)
            assert status is not None and status.covered_through_sequence == 5
            await app.runtime.driver.drive(scope, user("third"))
            before, after = await results(app, scope)
            assert before == "No past messages match."  # in front of the model, exactly: not searched
            (line,) = after.splitlines()  # not "third" (#7): past the summary
            assert line.startswith(f"{scope.conversation_id} this conversation, before its summary · ")
            assert line.endswith("#1 user: The ZEBRA path is /srv/z.")
        finally:
            await app.close()

    asyncio.run(scenario())


def test_off_offers_neither_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("ok")])
        app = await opened(tmp_path, adapter, conversation_search=False)
        try:
            assert not app.conversation_search_enabled
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.runtime.driver.drive(scope, user("hi"))
            assert not SEARCH & {d.tool_id for d in adapter.prepared_inputs[-1].tool_catalog}
        finally:
            await app.close()

    asyncio.run(scenario())
