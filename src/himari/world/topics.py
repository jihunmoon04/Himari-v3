"""The day's topics (character-world v0.1 §3, W2): titles from a search, once a date.

``SearchTopics`` asks the search it is given (the composition root's: a configured MCP server's search
Tool) the fixed query TOPICS_QUERY at most once a date — whatever came of it, so a failure is not tried
again that date — and keeps the titles of that one date in memory. ``topic_titles`` reads titles out of a
search result as the model would have seen it: a JSON document with ``results`` (each with a ``title``),
``Title:`` lines, or Markdown headings and links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import date

from himari.world.api import MAX_TOPIC_CHARS, MAX_TOPICS, TOPICS_QUERY

log = logging.getLogger(__name__)

SEARCH_SECONDS = 30.0
_TITLE_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?title(?:\*\*)?\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]\n]{4,})\]\(https?://[^)\s]+\)")


def _clean(title: str) -> str:
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") else c for c in title)
    text = " ".join(text.replace("**", "").split()).strip(" -–—|")
    return text if len(text) <= MAX_TOPIC_CHARS else text[: MAX_TOPIC_CHARS - 1] + "…"


def _json_titles(text: str) -> list[str] | None:
    try:
        found = json.loads(text)
    except ValueError:
        return None
    results = found.get("results") if isinstance(found, dict) else found
    if not isinstance(results, list):
        return None
    return [item["title"] for item in results if isinstance(item, dict) and isinstance(item.get("title"), str)]


def topic_titles(text: str) -> tuple[str, ...]:
    """Up to MAX_TOPICS distinct titles, cleaned and cut to MAX_TOPIC_CHARS; () when none is found."""
    raw = _json_titles(text.strip())
    if not raw:
        raw = _TITLE_LINE.findall(text) or _HEADING.findall(text) or _LINK.findall(text)
    titles: list[str] = []
    seen: set[str] = set()
    for candidate in raw:
        title = _clean(candidate)
        folded = title.casefold()
        if title and folded not in seen:
            seen.add(folded)
            titles.append(title)
        if len(titles) == MAX_TOPICS:
            break
    return tuple(titles)


class SearchTopics:
    """W2: a TopicSource over one search (a query in, the result's text out)."""

    def __init__(self, search: Callable[[str], Awaitable[str]], *, query: str = TOPICS_QUERY,
                 seconds: float = SEARCH_SECONDS) -> None:
        self._search = search
        self._query = query
        self._seconds = seconds
        self._lock = asyncio.Lock()
        self._kept: tuple[date, tuple[str, ...]] | None = None  # the last date searched and what it found

    async def topics(self, day: date) -> tuple[str, ...]:
        async with self._lock:
            if self._kept is not None and self._kept[0] == day:
                return self._kept[1]
            try:
                async with asyncio.timeout(self._seconds):
                    titles = topic_titles(await self._search(self._query))
            except Exception as error:  # noqa: BLE001 — no topics that date, whatever the search did
                log.warning("the day's topics could not be read (%s)", type(error).__name__)
                titles = ()
            self._kept = (day, titles)
            return titles
