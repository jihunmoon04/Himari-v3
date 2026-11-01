"""Explicit JSON codec for the Data DTOs a durable backend stores.

Every type is encoded by hand with a ``type`` tag where a union is involved,
and decoding accepts only those tags: no class is ever looked up by a name
read from storage. Opaque values (Tool arguments, raw results, Artifact
content, snapshot bodies, continuation payloads) are stored as their JSON
text; ``thaw``/``deep_freeze`` keep Mapping/Sequence semantics identical to
the in-memory store. A malformed stored value raises ``ValueError``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from himari.common import (
    ArtifactId,
    AttachmentRef,
    FileReference,
    CallId,
    CallRef,
    ContentBlock,
    EventId,
    JsonValue,
    ModelToolResult,
    ProviderContinuation,
    RunId,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    deep_freeze,
    thaw,
)
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    CompletedGroupRef,
    EventKind,
    EventPayload,
    EventRef,
    MessageOrigin,
    OriginKind,
    PromptBasis,
    ToolResultRecord,
    TriggerRef,
    UserMessage,
)


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def dump_json(value: JsonValue) -> str:
    """An opaque JSON value (frozen Mapping/tuple → JSON object/array)."""
    return dumps(thaw(value))


def load_json(text: str) -> JsonValue:
    return cast(JsonValue, deep_freeze(json.loads(text)))


def _obj(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"expected an object, got {type(value).__name__}")
    return value


def _optional_str(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


# --- content ------------------------------------------------------------------


def _block(block: ContentBlock) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, FileReference):  # V3-P76 §3.3
        return {"type": "file_reference", "path": block.path, "identity": list(block.identity), "start": block.start,
                "end": block.end, "start_line": block.start_line, "end_line": block.end_line,
                "excerpt": block.excerpt, "truncated": block.truncated, "size": block.size}
    data: dict[str, object] = {"type": "attachment", "file_id": block.file_id, "version": block.version}
    if block.screen:  # V3-P84 §4.3: written only when true, so older events read as before
        data["screen"] = True
    return data


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def _block_from(value: object) -> ContentBlock:
    data = _obj(value)
    match data.get("type"):
        case "text":
            return TextBlock(str(data["text"]))
        case "attachment":
            return AttachmentRef(str(data["file_id"]), int(data["version"]), data.get("screen") is True)
        case "file_reference":
            identity = data.get("identity")
            if not isinstance(identity, list):
                raise ValueError("a file reference's identity is a list")
            return FileReference(str(data["path"]), tuple(int(v) for v in identity), _optional_int(data.get("start")),
                                 _optional_int(data.get("end")), _optional_int(data.get("start_line")),
                                 _optional_int(data.get("end_line")), str(data.get("excerpt", "")),
                                 bool(data.get("truncated", False)), _optional_int(data.get("size")))
    raise ValueError(f"unknown content block {data.get('type')!r}")


def _blocks(blocks: tuple[ContentBlock, ...]) -> list[dict[str, object]]:
    return [_block(b) for b in blocks]


def _blocks_from(value: object) -> tuple[ContentBlock, ...]:
    if not isinstance(value, list):
        raise ValueError("expected a block list")
    return tuple(_block_from(v) for v in value)


def _call_ref(ref: CallRef) -> dict[str, object]:
    return {"run_id": ref.run_id, "call_id": ref.call_id}


def _call_ref_from(value: object) -> CallRef:
    data = _obj(value)
    return CallRef(RunId(str(data["run_id"])), CallId(str(data["call_id"])))


def _model_result_obj(result: ModelToolResult) -> dict[str, object]:
    return {"blocks": _blocks(result.blocks), "is_error": result.is_error}


def _model_result_obj_from(value: object) -> ModelToolResult:
    data = _obj(value)
    return ModelToolResult(_blocks_from(data["blocks"]), bool(data["is_error"]))


def model_result(result: ModelToolResult) -> str:
    return dumps(_model_result_obj(result))


def model_result_from(text: str) -> ModelToolResult:
    return _model_result_obj_from(json.loads(text))


# --- Conversation Event payloads ---------------------------------------------------


def _origin(origin: MessageOrigin) -> dict[str, object]:
    data: dict[str, object] = {"kind": origin.kind.value}
    if origin.device_id is not None:
        data["device_id"] = origin.device_id
    return data


def _origin_from(value: object) -> MessageOrigin:
    data = _obj(value)
    device = data.get("device_id")
    return MessageOrigin(OriginKind(str(data["kind"])), None if device is None else str(device))


def payload(value: EventPayload) -> str:
    match value:
        case UserMessage(blocks=blocks, answers_call=answers, origin=origin):
            data: dict[str, object] = {"blocks": _blocks(blocks),
                                       "answers_call": None if answers is None else _call_ref(answers)}
            if origin is not None:  # V3-P83: an optional field; older messages have none
                data["origin"] = _origin(origin)
            return dumps(data)
        case AssistantFinal(blocks=blocks):
            return dumps({"blocks": _blocks(blocks)})
        case AssistantToolCalls(calls=calls, commentary=commentary):
            return dumps({
                "calls": [
                    {"call_id": c.call_id, "ordinal": c.ordinal, "tool_id": c.tool_id, "arguments": thaw(c.arguments)}
                    for c in calls
                ],
                "commentary": _blocks(commentary),
            })
        case ToolResultRecord(call=call, model_result=result):
            return dumps({"call": _call_ref(call), "model_result": _model_result_obj(result)})
    raise TypeError(f"not an event payload: {type(value).__name__}")


def payload_from(kind: EventKind, text: str) -> EventPayload:
    data = _obj(json.loads(text))
    match kind:
        case EventKind.USER_MESSAGE:
            answers, origin = data.get("answers_call"), data.get("origin")
            return UserMessage(_blocks_from(data["blocks"]), None if answers is None else _call_ref_from(answers),
                               None if origin is None else _origin_from(origin))
        case EventKind.ASSISTANT_FINAL:
            return AssistantFinal(_blocks_from(data["blocks"]))
        case EventKind.ASSISTANT_TOOL_CALLS:
            calls = tuple(
                ToolCall(CallId(str(c["call_id"])), int(c["ordinal"]), ToolId(str(c["tool_id"])), _obj(c["arguments"]))
                for c in (_obj(v) for v in data["calls"])
            )
            return AssistantToolCalls(calls, _blocks_from(data["commentary"]))
        case EventKind.TOOL_RESULT:
            return ToolResultRecord(_call_ref_from(data["call"]), _model_result_obj_from(data["model_result"]))
    raise ValueError(f"unknown event kind {kind!r}")


# --- Run metadata ---------------------------------------------------------------------


def trigger_refs(refs: tuple[TriggerRef, ...]) -> str:
    return dumps([
        {"type": "event", "event_id": r.event_id} if isinstance(r, EventRef) else {"type": "group", "run_id": r.run_id}
        for r in refs
    ])


def trigger_refs_from(text: str) -> tuple[TriggerRef, ...]:
    refs: list[TriggerRef] = []
    for value in json.loads(text):
        data = _obj(value)
        match data.get("type"):
            case "event":
                refs.append(EventRef(EventId(str(data["event_id"]))))
            case "group":
                refs.append(CompletedGroupRef(RunId(str(data["run_id"]))))
            case other:
                raise ValueError(f"unknown trigger ref {other!r}")
    return tuple(refs)


def prompt_basis(basis: PromptBasis) -> str:
    return dumps({
        "event_sequence": basis.event_sequence,
        "state_revision": basis.state_revision,
        "snapshot_revision": basis.snapshot_revision,
        "artifact_revisions": dict(basis.artifact_revisions),
        "context_key": basis.context_key,
        "state_key": basis.state_key,
    })


def prompt_basis_from(text: str) -> PromptBasis:
    data = _obj(json.loads(text))
    snapshot = data["snapshot_revision"]
    return PromptBasis(
        int(data["event_sequence"]),
        int(data["state_revision"]),
        None if snapshot is None else int(snapshot),
        {ArtifactId(k): int(v) for k, v in _obj(data["artifact_revisions"]).items()},
        _optional_str(data.get("context_key")),  # absent in Runs stored before V3-P48
        _optional_str(data.get("state_key")),  # absent in Runs stored before V3-P89
    )


def usage(value: TokenUsage | None) -> str | None:
    if value is None:
        return None
    return dumps({
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "reasoning_tokens": value.reasoning_tokens,
        "cached_input_tokens": value.cached_input_tokens,
        "cache_write_tokens": value.cache_write_tokens,
    })


def usage_from(text: str | None) -> TokenUsage | None:
    if text is None:
        return None
    data = _obj(json.loads(text))
    return TokenUsage(data["input_tokens"], data["output_tokens"], data["reasoning_tokens"], data["cached_input_tokens"],
                      data.get("cache_write_tokens"))  # absent in rows written before V3-P62


def continuation(value: ProviderContinuation | None) -> str | None:
    if value is None:
        return None
    return dumps({"provider_key": value.provider_key, "model_id": value.model_id, "payload": thaw(value.payload)})


def continuation_from(text: str | None) -> ProviderContinuation | None:
    if text is None:
        return None
    data = _obj(json.loads(text))
    return ProviderContinuation(str(data["provider_key"]), str(data["model_id"]), cast(JsonValue, data["payload"]))
