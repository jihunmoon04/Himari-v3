"""The real world around HIMARI's life (character-world v0.1 §2, §3): what the story writer and a turn
may know of it, behind two small ports the composition root fills in.

- **Weather** (W1): today's summary where the user is (``HIMARI_LOCATION``), read from Open-Meteo a few
  times a day and kept in memory only; a failed read leaves the last one, or none. It is the **user's
  side** of the real world: HIMARI's own world (the persona's) may be another.
- **Topics** (W2): up to MAX_TOPICS titles of what is talked about in Korea that day, from a search Tool
  of a configured MCP server, once a day, for the story writer only (never a turn).

Nothing here is stored, and nothing here is an instruction: both reach a model as data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from himari.common import DeepFrozen

TOPICS_QUERY = "오늘 한국에서 화제인 소식"  # W2: the one fixed query
MAX_TOPICS = 5  # W2: the titles the writer is shown
MAX_TOPIC_CHARS = 120  # one title, cut


@dataclass(frozen=True)
class WeatherDay(DeepFrozen):
    """One day's weather summary where the user is (§2): its condition in words (from the WMO
    weather code), the lowest and highest temperature (°C) and the rain (mm) and snow (cm)."""

    day: date
    condition: str
    low_c: float | None = None
    high_c: float | None = None
    rain_mm: float = 0.0
    snow_cm: float = 0.0

    def line(self) -> str:
        """The one line a model is told, e.g. "light rain, 3 to 9°C, rain 2.1 mm"."""
        parts = [self.condition]
        if self.low_c is not None and self.high_c is not None:
            parts.append(f"{round(self.low_c)} to {round(self.high_c)}°C")
        if self.rain_mm >= 0.1:
            parts.append(f"rain {round(self.rain_mm, 1):g} mm")
        if self.snow_cm >= 0.1:
            parts.append(f"snow {round(self.snow_cm, 1):g} cm")
        return ", ".join(parts)


class WeatherSource(Protocol):
    """§2: the weather as last read, from memory — never a network call on the caller's path."""

    def weather_on(self, day: date) -> WeatherDay | None:
        """That date's summary (the user's date), or None when none was read for it."""
        ...


class TopicSource(Protocol):
    """§3: the day's topic titles — read at most once a date, whatever came of it."""

    async def topics(self, day: date) -> tuple[str, ...]:
        """Up to MAX_TOPICS titles for that date; () when there are none or the search failed."""
        ...


def parse_location(text: str) -> tuple[float, float]:
    """``HIMARI_LOCATION``: "latitude,longitude" in degrees (e.g. "37.57,126.98"). ValueError otherwise."""
    latitude, sep, longitude = text.strip().partition(",")
    if not sep:
        raise ValueError("a location is latitude,longitude")
    lat, lon = float(latitude.strip()), float(longitude.strip())
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("a latitude is -90..90 and a longitude -180..180")
    return lat, lon
