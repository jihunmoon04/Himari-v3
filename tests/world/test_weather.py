"""The weather where the user is (character-world v0.1 §2, W1): Open-Meteo's daily summaries, read a few times a
day into memory; a failed read keeps what was read before. No network: a fake transport answers."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import httpx
import pytest

from himari.world.api import WeatherDay, parse_location
from himari.world.weather import OPEN_METEO_URL, OpenMeteoWeather, condition, parse_forecast

ANSWER = {
    "latitude": 37.56, "longitude": 126.98, "timezone": "Asia/Seoul",
    "daily_units": {"time": "iso8601", "weather_code": "wmo code"},
    "daily": {
        "time": ["2026-11-24", "2026-11-25", "2026-11-26"],
        "weather_code": [3, 61, 71],
        "temperature_2m_min": [1.4, 3.2, -2.6],
        "temperature_2m_max": [8.0, 9.4, 1.5],
        "rain_sum": [0.0, 2.14, 0.0],
        "snowfall_sum": [0.0, 0.0, 3.5],
    },
}


def test_a_days_summary_is_one_line() -> None:
    assert WeatherDay(date(2026, 11, 25), "light rain", 3.2, 9.4, 2.14).line() == "light rain, 3 to 9°C, rain 2.1 mm"
    assert WeatherDay(date(2026, 11, 26), "light snow", -2.6, 1.5, 0.0, 3.5).line() == "light snow, -3 to 2°C, snow 3.5 cm"
    assert WeatherDay(date(2026, 11, 26), "overcast").line() == "overcast"  # no temperatures: none told
    assert (condition(0), condition(95), condition(42), condition("3"), condition(True)) == (
        "clear sky", "thunderstorm", "unknown weather", "unknown weather", "unknown weather")


def test_an_answer_becomes_its_days_and_one_with_none_is_refused() -> None:
    days = parse_forecast(ANSWER)
    assert list(days) == [date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)]
    assert days[date(2026, 11, 25)] == WeatherDay(date(2026, 11, 25), "light rain", 3.2, 9.4, 2.14, 0.0)
    bads: list[dict[str, Any]] = [{}, {"daily": []}, {"daily": {"time": ["x"], "weather_code": [1]}},
                                  {"daily": {"time": ["2026-11-25"]}}]
    for bad in bads:
        with pytest.raises(ValueError):
            parse_forecast(bad)


def test_the_weather_is_read_into_memory_and_a_failure_keeps_the_last_read() -> None:
    async def scenario() -> None:
        asked: list[httpx.Request] = []
        answers: list[Any] = [ANSWER, httpx.Response(500), b"not json", {"daily": {}}]

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            found = answers.pop(0)
            if isinstance(found, httpx.Response):
                return found
            return httpx.Response(200, content=found if isinstance(found, bytes) else json.dumps(found).encode())

        weather = OpenMeteoWeather(37.57, 126.98, "Asia/Seoul", transport=httpx.MockTransport(answer))
        assert weather.weather_on(date(2026, 11, 25)) is None  # nothing read yet: no weather
        await weather.refresh()
        today = weather.weather_on(date(2026, 11, 25))
        assert today is not None and today.line() == "light rain, 3 to 9°C, rain 2.1 mm"
        (request,) = asked
        assert str(request.url).startswith(OPEN_METEO_URL)
        params = dict(request.url.params)
        assert (params["latitude"], params["longitude"], params["timezone"], params["past_days"],
                params["forecast_days"]) == ("37.5700", "126.9800", "Asia/Seoul", "1", "2")
        assert params["daily"].split(",") == ["weather_code", "temperature_2m_min", "temperature_2m_max", "rain_sum",
                                              "snowfall_sum"]
        for _ in range(3):  # a server error, an answer that is not JSON, one with no day: the last read stays
            await weather.refresh()
            assert weather.weather_on(date(2026, 11, 25)) == today
        assert weather.weather_on(date(2026, 11, 28)) is None
        await weather.aclose()

    asyncio.run(scenario())


def test_a_location_is_latitude_and_longitude() -> None:
    assert parse_location(" 37.57, 126.98 ") == (37.57, 126.98)
    assert parse_location("-33.9,18.4") == (-33.9, 18.4)
    for bad in ("37.57", "north,east", "91,0", "0,181", ""):
        with pytest.raises(ValueError):
            parse_location(bad)
