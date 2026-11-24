"""The weather where the user is (character-world v0.1 §2, W1), from Open-Meteo (no key).

``OpenMeteoWeather.refresh`` reads the daily summaries of yesterday, today and tomorrow (in the server's
zone) in one short request and keeps them in memory; the server runs it as the periodic job ``weather`` a
few times a day. A turn and the story writer only read what is kept (``weather_on``), so neither ever
waits on the network. A failed read keeps what was read before (a date drops out once it is no longer
in it): when nothing is kept for a date, there is simply no weather.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from himari.world.api import WeatherDay

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_SECONDS = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
DAILY = "weather_code,temperature_2m_min,temperature_2m_max,rain_sum,snowfall_sum"

# WMO weather interpretation codes (as Open-Meteo documents them), in words.
CONDITIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "light snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def condition(code: object) -> str:
    """A WMO code in words; "unknown weather" for one not listed."""
    if isinstance(code, bool) or not isinstance(code, (int, float)) or not math.isfinite(code):
        return "unknown weather"
    return CONDITIONS.get(int(code), "unknown weather")


def _number(values: object, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def parse_forecast(body: Mapping[str, Any]) -> dict[date, WeatherDay]:
    """The days of an Open-Meteo ``daily`` answer. ValueError when there is none to use."""
    daily = body.get("daily")
    if not isinstance(daily, Mapping) or not isinstance(daily.get("time"), list):
        raise ValueError("no daily weather in the answer")
    days: dict[date, WeatherDay] = {}
    codes = daily.get("weather_code")
    for index, when in enumerate(daily["time"]):
        try:
            day = date.fromisoformat(when) if isinstance(when, str) else None
        except ValueError:
            day = None
        if day is None or not isinstance(codes, list) or index >= len(codes):
            continue
        days[day] = WeatherDay(day, condition(codes[index]), _number(daily.get("temperature_2m_min"), index),
                               _number(daily.get("temperature_2m_max"), index),
                               _number(daily.get("rain_sum"), index) or 0.0,
                               _number(daily.get("snowfall_sum"), index) or 0.0)
    if not days:
        raise ValueError("no usable day in the answer")
    return days


class OpenMeteoWeather:
    """W1: one location's weather, kept in memory (a WeatherSource)."""

    def __init__(self, latitude: float, longitude: float, time_zone: str, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._params = {"latitude": f"{latitude:.4f}", "longitude": f"{longitude:.4f}", "daily": DAILY,
                        "timezone": time_zone, "past_days": "1", "forecast_days": "2"}
        self._client = httpx.AsyncClient(transport=transport, timeout=REQUEST_SECONDS, follow_redirects=False)
        self._days: dict[date, WeatherDay] = {}

    async def refresh(self) -> None:
        """One read; a failure is logged and keeps what was read before."""
        try:
            response = await self._client.get(OPEN_METEO_URL, params=self._params)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ValueError("the answer is too large")
            body = response.json()
            if not isinstance(body, Mapping):
                raise ValueError("the answer is not an object")
            self._days = parse_forecast(body)
        except (httpx.HTTPError, ValueError) as error:
            log.warning("the weather could not be read (%s); kept what was read before", type(error).__name__)

    def weather_on(self, day: date) -> WeatherDay | None:
        return self._days.get(day)

    async def aclose(self) -> None:
        await self._client.aclose()
