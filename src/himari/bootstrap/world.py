"""The real world's sources (character-world v0.1 §2, §3), as the composition root builds them.

- The weather (W1) is Open-Meteo's for ``HIMARI_LOCATION``, refreshed by the periodic job ``weather``.
- The day's topics (W2) come from the first configured MCP server, in ``mcp.json`` order, that was
  connected at startup and registered a Tool whose own name holds "search" and whose input takes a
  ``query`` text: the search itself is that Tool, called outside any Conversation with the fixed query.
  No such Tool, or ``HIMARI_LIFE_TOPICS=0``: no topics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from himari.common import JsonValue
from himari.mcp.tools import McpConnection, McpTool, render_result

WEATHER_SECONDS = 3 * 3600.0  # W1: "a few times a day"
SEARCH_COUNT = 10  # results asked for when the Tool takes a count (titles are cut to MAX_TOPICS after)
_COUNT_NAMES = ("numResults", "num_results", "count", "limit")


def _takes_query(tool: McpTool) -> bool:
    properties = tool.definition.input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    query = properties.get("query")
    return isinstance(query, Mapping) and query.get("type", "string") == "string"


def search_tool(mcp: McpConnection) -> McpTool | None:
    """§3: the first usable search Tool among the connected servers (in their configured order)."""
    for status in mcp.statuses:
        if not status.available:
            continue
        for handler in mcp.handlers:
            if (handler.definition.external_source == status.source and "search" in handler.name.casefold()
                    and _takes_query(handler)):
                return handler
    return None


def topic_search(tool: McpTool) -> Callable[[str], Awaitable[str]]:
    """The search a SearchTopics asks: ``query`` (and a result count when the Tool takes one) in,
    the result as the model would have read it out. A result the server marks as an error raises."""
    properties = tool.definition.input_schema.get("properties")
    count = next((name for name in _COUNT_NAMES if isinstance(properties, Mapping) and name in properties), None)

    async def search(query: str) -> str:
        arguments: dict[str, JsonValue] = {"query": query}
        if count is not None:
            arguments[count] = SEARCH_COUNT
        result = await tool.client.call_tool(tool.name, arguments)
        if result.get("isError") is True:
            raise RuntimeError("the search Tool answered with an error")
        return render_result(result)

    return search
