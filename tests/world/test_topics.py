"""The day's topics (character-world v0.1 §3, W2): up to five titles out of a search result, searched at most once
a date whatever came of it. The search is a fake: no network, no MCP server."""

from __future__ import annotations

import asyncio
import json
from datetime import date

from himari.world.api import MAX_TOPIC_CHARS, MAX_TOPICS, TOPICS_QUERY
from himari.world.topics import SearchTopics, topic_titles


def test_titles_are_read_from_json_results_title_lines_headings_or_links() -> None:
    results = {"requestId": "r1", "results": [{"title": "첫눈 예보", "url": "https://a"}, {"url": "https://b"},
                                              {"title": " 새   영화\n개봉 ", "url": "https://c"}]}
    assert topic_titles(json.dumps(results, ensure_ascii=False)) == ("첫눈 예보", "새 영화 개봉")
    lines = ("Title: 첫눈 예보\nURL: https://a\nPublished Date: 2026-11-25\nText: ...\n\n"
             "**Title:** 야구 시상식\nURL: https://b\n\ntitle: 첫눈 예보\n")
    assert topic_titles(lines) == ("첫눈 예보", "야구 시상식")  # the same title once
    assert topic_titles("## 첫눈 예보\nsome text\n### 연말 공연 ###\n") == ("첫눈 예보", "연말 공연")
    assert topic_titles("- [첫눈 예보 소식](https://a.example/x) and [링크](https://b)") == ("첫눈 예보 소식",)
    assert topic_titles("nothing that looks like a title") == ()
    many = "\n".join(f"Title: 소식 {n}" for n in range(9))
    assert topic_titles(many) == tuple(f"소식 {n}" for n in range(MAX_TOPICS))
    (long,) = topic_titles("Title: " + "가" * 300)
    assert len(long) == MAX_TOPIC_CHARS and long.endswith("…")


def test_the_day_is_searched_once_whatever_came_of_it() -> None:
    async def scenario() -> None:
        asked: list[str] = []
        answers: list[object] = ["Title: 첫눈 예보\nTitle: 야구 시상식", RuntimeError("down"), "Title: 연말 공연"]

        async def search(query: str) -> str:
            asked.append(query)
            found = answers.pop(0)
            if isinstance(found, Exception):
                raise found
            assert isinstance(found, str)
            return found

        topics = SearchTopics(search)
        day = date(2026, 11, 25)
        assert await topics.topics(day) == ("첫눈 예보", "야구 시상식")
        assert await topics.topics(day) == ("첫눈 예보", "야구 시상식")  # kept: no second search
        assert asked == [TOPICS_QUERY]
        assert await topics.topics(date(2026, 11, 26)) == ()  # the search failed: none that date
        assert await topics.topics(date(2026, 11, 26)) == ()  # and it is not tried again that date
        assert await topics.topics(date(2026, 11, 27)) == ("연말 공연",)
        assert len(asked) == 3

    asyncio.run(scenario())


def test_a_search_that_does_not_answer_in_time_gives_no_topics() -> None:
    async def scenario() -> None:
        async def slow(query: str) -> str:
            await asyncio.sleep(5)
            return "Title: 늦은 소식"

        assert await SearchTopics(slow, seconds=0.05).topics(date(2026, 11, 25)) == ()

    asyncio.run(scenario())
