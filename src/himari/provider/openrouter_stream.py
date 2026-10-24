"""OpenRouter streamed responses (V3-P73 §3).

- ``SseReader`` splits a ``text/event-stream`` body into the ``data`` of
  each event, by bytes (LF, CRLF or CR line ends, a CR at a chunk's end
  waiting for the next), however the body is chunked. Comment lines
  (``: OPENROUTER PROCESSING``) and other fields are skipped.
- ``StreamView`` follows a stream while it arrives: it hands what the model
  writes to the sink (text, a tool call's name, that reasoning arrived) and
  notes the end marker, a provider error and the usage. It never raises: it
  only shows, and the stored body is normalized afterwards.
- ``assemble`` merges a whole stored stream into the message a non-streamed
  response carries, so the adapter validates both alike. It fails closed:
  a stream with no ``[DONE]``, an error chunk, data that is not JSON, more
  than one choice, a tool call fragment out of order, or reasoning details
  that cannot be joined without guessing (§3.3.1: items join on index, type
  and id only; a second signature, an index seen before or a redacted
  encrypted item) is an invalid response — never a guessed continuation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from himari.provider.api import OutputDelta, OutputSink, ProviderError, ProviderErrorCode

EVENT_STREAM = "text/event-stream"
DONE = "[DONE]"
_JOINED = ("text", "summary", "data")  # reasoning detail fields that arrive in pieces
_SET_ONCE = ("id", "format", "signature")  # may arrive late, never change


def is_event_stream(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == EVENT_STREAM


class SseReader:
    """The ``data`` of each complete event, in order (WHATWG SSE parsing)."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._cr = False  # the last byte seen was a CR: a following LF belongs to it
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        events: list[str] = []
        start = 0
        for index, byte in enumerate(chunk):
            if byte not in (0x0A, 0x0D):
                continue
            if byte == 0x0A and self._cr and index == start:
                self._cr = False
                start = index + 1
                continue
            self._line += chunk[start:index]
            self._end_line(events)
            self._cr = byte == 0x0D
            start = index + 1
        if start < len(chunk):
            self._cr = False
            self._line += chunk[start:]
        return events

    def _end_line(self, events: list[str]) -> None:
        line = self._line.decode("utf-8", errors="replace")
        self._line.clear()
        if not line:
            if self._data:
                events.append("\n".join(self._data))
                self._data = []
            return
        if line.startswith(":"):
            return
        name, _, value = line.partition(":")
        if name == "data":
            self._data.append(value[1:] if value.startswith(" ") else value)


def payloads(body: bytes) -> list[str]:
    """The events of a whole stored body. An event the body does not close
    with a blank line is dropped, as the SSE rules say."""
    return SseReader().feed(body)


class StreamView:
    """What a stream shows while it arrives. Never raises."""

    def __init__(self, sink: OutputSink | None, decode_tool: Any) -> None:
        self._sink = sink
        self._decode_tool = decode_tool
        self._named: set[int] = set()
        self._thinking = False
        self.done = False
        self.error: object = None  # the provider error a chunk carried
        self.usage: object = None  # the last usage object

    def take(self, payload: str) -> None:
        if payload == DONE:
            self.done = True
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            return
        if not isinstance(chunk, dict):
            return
        if chunk.get("error") is not None:
            self.error = chunk["error"]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk
        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        if (delta.get("reasoning") or delta.get("reasoning_details")) and not self._thinking:
            self._thinking = True
            self._show(OutputDelta(thinking=True))
        for call in delta.get("tool_calls") or ():
            if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name and call["index"] not in self._named:
                self._named.add(call["index"])
                self._show(OutputDelta(tool=str(self._decode_tool(name))))
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._show(OutputDelta(text=content))

    def _show(self, delta: OutputDelta) -> None:
        if self._sink is None:
            return
        try:
            self._sink(delta)
        except Exception:  # noqa: BLE001 — display only: a sink never fails the Run
            pass


def _invalid(reason: str) -> ProviderError:
    return ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, f"stream: {reason}")


class _Details:
    """``reasoning_details`` joined by §3.3.1, or the reasons it cannot be."""

    def __init__(self) -> None:
        self.present = False  # the key came at all: [] is kept apart from absent
        self.items: list[dict[str, Any]] = []
        self._seen: set[object] = set()

    def add(self, raw: object) -> None:
        if not isinstance(raw, list):
            raise _invalid("reasoning_details must be a list")
        self.present = True
        for item in raw:
            if not isinstance(item, dict):
                raise _invalid("reasoning_details items must be objects")
            index = item.get("index")
            last = self.items[-1] if self.items else None
            if last is not None and index is not None and last.get("index") == index:
                self._join(last, item)
                continue
            if index is not None and index in self._seen:
                raise _invalid(f"reasoning detail index {index!r} came back")
            if index is not None:
                self._seen.add(index)
            self.items.append(dict(item))

    @staticmethod
    def _join(item: dict[str, Any], piece: dict[str, Any]) -> None:
        if piece.get("type") != item.get("type"):
            raise _invalid("reasoning detail pieces of one index differ in type")
        for key, value in piece.items():
            if key in ("index", "type"):
                continue
            if key in _JOINED and isinstance(value, str):
                current = item.get(key)
                if current is not None and not isinstance(current, str):
                    raise _invalid(f"reasoning detail {key} is not text")
                item[key] = (current or "") + value
            elif key in _SET_ONCE or key not in item:
                if value is None:
                    item.setdefault(key, None)
                elif item.get(key) is None:
                    item[key] = value
                elif item[key] != value:
                    raise _invalid(f"reasoning detail {key} changed within one item")
            elif item[key] != value:
                raise _invalid(f"reasoning detail {key} changed within one item")

    def result(self) -> list[dict[str, Any]]:
        for item in self.items:
            if item.get("type") == "reasoning.encrypted" and item.get("data") == "[REDACTED]":
                raise _invalid("encrypted reasoning arrived redacted and cannot be replayed")
        return self.items


def _tool_piece(calls: list[dict[str, Any]], raw: object) -> None:
    if not isinstance(raw, dict) or not isinstance(raw.get("index"), int) or isinstance(raw.get("index"), bool):
        raise _invalid("a tool call piece needs an integer index")
    index = raw["index"]
    if index == len(calls):
        calls.append({"type": "function", "function": {"name": None, "arguments": ""}})
    elif not 0 <= index < len(calls):
        raise _invalid(f"tool call index {index} out of order")
    call = calls[index]
    if raw.get("type") not in (None, "function"):
        raise _invalid("tool call type must be 'function'")
    if raw.get("id") is not None:
        if call.get("id") not in (None, raw["id"]):
            raise _invalid("a tool call's id changed")
        call["id"] = raw["id"]
    function = raw.get("function")
    if function is None:
        return
    if not isinstance(function, dict):
        raise _invalid("tool call function must be an object")
    name = function.get("name")
    if name is not None:
        if call["function"]["name"] not in (None, name):
            raise _invalid("a tool call's name changed")
        call["function"]["name"] = name
    arguments = function.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, str):
            raise _invalid("tool call arguments must arrive as text")
        call["function"]["arguments"] += arguments


def assemble(events: Iterable[str]) -> dict[str, Any]:
    """The response a stream amounts to, shaped like a non-streamed one."""
    text: list[str] = []
    reasoning: list[str] = []
    details = _Details()
    calls: list[dict[str, Any]] = []
    finish: object = None
    usage: object = None
    done = False
    for payload in events:
        if done:
            raise _invalid("data after [DONE]")
        if payload == DONE:
            done = True
            continue
        try:
            chunk = json.loads(payload)
        except ValueError as exc:
            raise _invalid("data is not JSON") from exc
        if not isinstance(chunk, dict):
            raise _invalid("a chunk is not an object")
        if chunk.get("error") is not None:
            raise _invalid("a chunk carries an error")
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise _invalid("a chunk must carry at most one choice")
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise _invalid("the choice must be the first one")
        if choice.get("error") is not None:
            raise _invalid("a choice carries an error")
        if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
            finish = choice["finish_reason"]
        delta = choice.get("delta", {})
        if delta is None:
            continue
        if not isinstance(delta, dict):
            raise _invalid("delta must be an object")
        if delta.get("role") not in (None, "assistant"):
            raise _invalid(f"delta.role={delta.get('role')!r}, expected 'assistant'")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _invalid("delta.content must be text")
            text.append(content)
        piece = delta.get("reasoning")
        if piece is not None:
            if not isinstance(piece, str):
                raise _invalid("delta.reasoning must be text")
            reasoning.append(piece)
        if "reasoning_details" in delta and delta["reasoning_details"] is not None:
            details.add(delta["reasoning_details"])
        for raw in delta.get("tool_calls") or ():
            _tool_piece(calls, raw)
    if not done:
        raise _invalid("it ended without [DONE]")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    if details.present:
        message["reasoning_details"] = details.result()
    if reasoning:
        message["reasoning"] = "".join(reasoning)
    doc: dict[str, Any] = {"choices": [{"finish_reason": finish, "message": message}]}
    if usage is not None:
        doc["usage"] = usage
    return doc
