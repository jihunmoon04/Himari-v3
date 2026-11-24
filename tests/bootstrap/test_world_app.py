"""The world in the Application (character-world v0.1): the persona's hidden lines told from the stage they wait
for, the weather where the user is in a turn's state (HIMARI_LOCATION, read by the ``weather`` job), and the day's
topics for the story writer from a configured MCP server's search Tool. No network: fakes answer."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp_fake import URL as MCP_URL
from mcp_fake import ECHO_SCHEMA, FakeMcpServer

from himari.bootstrap.application import Application, world_weather
from himari.bootstrap.world import WEATHER_SECONDS
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import BondStage
from himari.life.api import WriteOutcome
from himari.prompt.api import state_data
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter
from himari.world.api import TOPICS_QUERY

ME = PrincipalId("operator")
SEOUL = ZoneInfo("Asia/Seoul")


def said(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def test_a_hidden_persona_line_is_told_from_the_next_turn_after_its_stage(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([said("응.")] * 3)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None)
        try:
            text = "이름: 루시\n[familiar] 고양이를 키웠었다.\n[close] 사실 예전에 해커였다."
            lucy = await app.data.create_persona(ME, "Lucy", text)
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            scope = TrustedScope(ME, await app.create_conversation(ME))

            async def system() -> str:
                await app.send(scope, "안녕")
                await app.runner.wait(scope)
                return "\n".join(adapter.prepared_inputs[-1].stable_prefix)

            first = await system()
            assert "이름: 루시" in first and "고양이" not in first and "해커" not in first
            standing = await app.data.bond_stage(ME, lucy.persona_id)
            await app.data.set_bond_stage(ME, lucy.persona_id, BondStage.FAMILIAR,
                                          expected_revision=standing.revision)
            second = await system()
            assert "\n고양이를 키웠었다." in second and "[familiar]" not in second and "해커" not in second
            standing = await app.data.bond_stage(ME, lucy.persona_id)
            await app.data.set_bond_stage(ME, lucy.persona_id, BondStage.CLOSE, expected_revision=standing.revision)
            assert "\n사실 예전에 해커였다." in await system()
            (listed,) = (await app.data.list_personas(ME)).personas
            assert listed.text == text  # the Web shows the owner's text as written
        finally:
            await app.close()

    asyncio.run(scenario())


def forecast(today: str) -> dict[str, Any]:
    return {"daily": {"time": [today], "weather_code": [61], "temperature_2m_min": [3.2],
                      "temperature_2m_max": [9.4], "rain_sum": [2.14], "snowfall_sum": [0.0]}}


@pytest.mark.parametrize("life", [True, False])
def test_a_turn_is_told_todays_weather_where_the_user_is(tmp_path: Path, life: bool) -> None:
    async def scenario() -> None:
        today = datetime.now(UTC).astimezone(SEOUL).date().isoformat()
        asked: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            return httpx.Response(200, json=forecast(today))

        adapter = ScriptedAdapter([said("응.")])
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, time_zone="Asia/Seoul", life=life,
                                     location=(37.57, 126.98), weather_transport=httpx.MockTransport(answer))
        try:
            assert app.world_weather == world_weather((37.57, 126.98), life=life, companion=True)
            if not life:
                assert app.weather is None and app.world_weather == "off (HIMARI's life is off)"
                assert "weather" not in app.ticker._jobs  # noqa: SLF001
                return
            assert app.world_weather == "on (Open-Meteo at 37.57,126.98)" and "weather" in app.ticker._jobs  # noqa: SLF001
            assert app.weather is not None
            await app.weather.refresh()
            assert app.weather_today() == "light rain, 3 to 9°C, rain 2.1 mm"
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "오늘 어때?")
            await app.runner.wait(scope)
            told = state_data(adapter.prepared_inputs[-1].current_state)["world"]
            assert isinstance(told, dict) and told["weather"] == "light rain, 3 to 9°C, rain 2.1 mm"
            assert asked and dict(asked[0].url.params)["latitude"] == "37.5700"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_without_a_location_there_is_no_weather() -> None:
    assert world_weather(None, life=True, companion=True) == "off (no HIMARI_LOCATION)"
    assert world_weather((37.57, 126.98), life=True, companion=False) == "off (HIMARI's life is off)"


SEARCH_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}, "numResults": {"type": "number"}},
                 "required": ["query"]}


def search_server(tmp_path: Path, *, name: str = "web_search_exa") -> tuple[Path, FakeMcpServer]:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "mcp.json").write_text(json.dumps({"servers": {"exa": {
        "url": MCP_URL, "tools": {"echo": {"effect": "read"}, name: {"effect": "read"}}}}}))
    (data / "mcp.json").chmod(0o600)
    fake = FakeMcpServer([{"name": "echo", "description": "Echo.", "inputSchema": ECHO_SCHEMA},
                          {"name": name, "description": "Search the web.", "inputSchema": SEARCH_SCHEMA}])
    fake.handlers[name] = lambda arguments: {"content": [{"type": "text", "text": (
        "Title: 첫눈 예보\nURL: https://a.example\n\nTitle: 야구 시상식\nURL: https://b.example")}]}
    return data, fake


@pytest.mark.parametrize("topics", [True, False])
def test_the_story_writer_is_told_the_days_topics_from_the_search_tool(tmp_path: Path, topics: bool) -> None:
    async def scenario() -> None:
        data, fake = search_server(tmp_path)
        day = json.dumps({"diary": "오늘은 그림을 그렸다. 차를 마셨다. 일찍 잤다.", "plans": [], "facts": []},
                         ensure_ascii=False)
        adapter = ScriptedAdapter([said(day)], memory_passes=True)
        app = await Application.open(data, adapter, model_id="scripted-model", sandbox=False, cleanup_seconds=None,
                                     memory_model="scripted-model", mcp_transport=fake.transport(), environ={},
                                     life_topics=topics)
        try:
            assert app.world_topics == ("on (exa/web_search_exa)" if topics else "off (HIMARI_LIFE_TOPICS=0)")
            assert app.life_writer is not None
            assert await app.life_writer.write(ME) is WriteOutcome.WRITTEN
            told = json.loads(adapter.prepared_inputs[-1].conversation[0].blocks[0].text)  # type: ignore[union-attr]
            if not topics:
                assert "topics" not in told and fake.calls == []
                return
            assert told["topics"] == ["첫눈 예보", "야구 시상식"]
            assert fake.calls == [("web_search_exa", {"query": TOPICS_QUERY, "numResults": 10})]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_without_a_search_tool_or_a_story_writer_there_are_no_topics(tmp_path: Path) -> None:
    async def scenario() -> None:
        data, fake = search_server(tmp_path, name="fetch_page")  # no "search" in its name
        app = await Application.open(data, ScriptedAdapter(memory_passes=True), model_id="scripted-model",
                                     sandbox=False, cleanup_seconds=None, memory_model="scripted-model",
                                     mcp_transport=fake.transport(), environ={})
        try:
            assert app.world_topics == "off (no MCP search Tool)"
        finally:
            await app.close()
        again = await Application.open(data, ScriptedAdapter(), model_id="scripted-model", sandbox=False,
                                       cleanup_seconds=None, mcp_transport=fake.transport(), environ={})
        try:
            assert again.world_topics == "off (no story writer)"
        finally:
            await again.close()

    asyncio.run(scenario())


def test_the_weather_job_reads_a_few_times_a_day() -> None:
    assert timedelta(hours=1) <= timedelta(seconds=WEATHER_SECONDS) <= timedelta(hours=6)
