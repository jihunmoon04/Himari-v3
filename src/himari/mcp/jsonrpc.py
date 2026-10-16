"""JSON-RPC 2.0 for MCP, whatever carries it (V3-P59 §5.2): messages,
answers matched to requests, and what an answer says about whether the
request was carried out (V3-055).

- ``McpRefused``: the request was not carried out — it never left, or the
  answer is a parse / invalid request / unknown method / invalid params
  error (decided before any Tool runs).
- ``McpUncertain``: it was sent and whether it took effect is unknown — any
  other error, an answer that does not fit, no answer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class McpError(Exception):
    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code  # the JSON-RPC error code, when the server answered with one


class McpRefused(McpError):
    """The server did not run the request."""


class McpUncertain(McpError):
    """The request was sent; whether it took effect is unknown."""


class McpSessionChanged(McpRefused):
    """A request meant for one session was not sent: that session is gone
    or replaced (V3-P60 C6). Nothing went to another session in its place."""


# JSON-RPC errors that mean the request was not carried out.
REFUSALS = frozenset({-32700, -32600, -32601, -32602})


def request(request_id: int, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}


def notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def parse(data: bytes) -> object:
    try:
        return json.loads(data)
    except (ValueError, RecursionError):  # RecursionError: nested deeper than the parser can follow
        raise McpUncertain("the response is not usable JSON") from None


def result_of(message: object, request_id: int, method: str) -> Mapping[str, Any]:
    """The result of the answer to ``request_id``, or the error it means."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise McpUncertain(f"{method}: the response does not answer the request")
    if "error" in message:
        error = message["error"]
        code = error.get("code") if isinstance(error, dict) else None
        text = error.get("message") if isinstance(error, dict) else None
        failure = McpRefused if code in REFUSALS else McpUncertain
        raise failure(f"{method}: server error {code}: {str(text)[:200]}", code)
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpUncertain(f"{method}: the response has no result object")
    return result
