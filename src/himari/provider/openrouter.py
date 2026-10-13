"""OpenRouter ProviderAdapter (OpenAI-compatible Chat Completions).

- Wire-exact raw capture (V3-048): ``prepare_native_request`` builds the exact
  request bytes; ``invoke_prepared`` sends exactly those bytes with httpx. No
  SDK, no implicit retries. Credentials live only in transport headers.
- Tool ids such as ``inside/run_command`` are encoded reversibly as
  ``inside__run_command`` (OpenRouter function names allow ``[A-Za-z0-9_-]``),
  so a stored response can be re-normalized after a restart without any
  adapter state.
- Current State is rendered as a trailing user-role message *after* the last
  ToolCall group, so call/result adjacency is never broken (CPP §6.1 rule 6).
- Reasoning: ``RequestOptions.reasoning_effort`` → ``reasoning.effort``;
  reasoning tokens are recorded from usage. Replaying ``reasoning_details``
  (continuation) is not implemented yet (PR B, V3-049).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from himari.common import (
    AttachmentRef,
    CallId,
    ContentBlock,
    RawRepresentation,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    thaw,
)
from himari.prompt.api import (
    AssistantTurn,
    CompressedHistory,
    CurrentStateProjection,
    ModelInput,
    ToolGroupTurn,
    UserTurn,
)
from himari.provider.api import (
    NativeInvocationOutcome,
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedModelOutput,
    NormalizedToolCalls,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    validate_tool_calls,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter's documented reasoning.effort values.
REASONING_EFFORTS = frozenset({"max", "xhigh", "high", "medium", "low", "minimal", "none"})
_TOOL_ID = re.compile(r"^[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*(?:/[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*)?$")


def encode_tool_name(tool_id: str) -> str:
    """``kind/name`` → ``kind__name``. Rejects ids that could not round-trip."""
    if not _TOOL_ID.match(tool_id) or "__" in tool_id:
        raise ProviderError(ProviderErrorCode.UNSUPPORTED_CAPABILITY, f"tool id {tool_id!r} cannot be encoded")
    name = tool_id.replace("/", "__")
    if len(name) > 64:
        raise ProviderError(ProviderErrorCode.UNSUPPORTED_CAPABILITY, f"tool id {tool_id!r} is too long")
    return name


def decode_tool_name(name: str) -> ToolId:
    return ToolId(name.replace("__", "/", 1))


def _text(blocks: Sequence[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, AttachmentRef):
            raise ProviderError(ProviderErrorCode.UNSUPPORTED_CAPABILITY, "attachments are not supported yet")
        parts.append(block.text)
    return "\n".join(parts)


def _render_state(state: CurrentStateProjection) -> str | None:
    basis = state.basis
    observed = {o.binding_id: o.status.value for o in state.observations}
    lines: list[str] = []
    if basis.environments:
        lines.append("environments:")
        bindings = {b.binding_id: b for b in basis.bindings}
        for env in basis.environments:
            line = f"- {env.environment_kind} (instance {env.environment_instance_id})"
            binding = bindings.get(env.default_binding_id) if env.default_binding_id else None
            if binding is not None:
                status = observed.get(binding.binding_id, "unknown")
                line += f", default workspace binding {binding.binding_id} -> {binding.workspace_id} [{status}]"
            lines.append(line)
    if basis.conversation_workspace is not None:
        lines.append(f"working directory: {basis.conversation_workspace.cwd_relative}")
    if basis.artifacts:
        lines.append("artifacts:")
        for artifact in basis.artifacts:
            content = json.dumps(thaw(artifact.content), ensure_ascii=False, sort_keys=True)
            lines.append(f"- {artifact.kind} {artifact.artifact_id} (revision {artifact.revision}): {content}")
    if not lines:
        return None
    return (
        "<runtime_state>\n"
        "Current runtime state, provided by the system (not written by the user):\n"
        + "\n".join(lines)
        + "\n</runtime_state>"
    )


class OpenRouterAdapter:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: Mapping[str, ProviderCapabilities] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        self._api_key = api_key
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._transport = transport  # tests inject httpx.MockTransport
        self._capabilities = dict(capabilities or {})

    def __repr__(self) -> str:  # never leak the key through logs
        return f"OpenRouterAdapter(endpoint={self._endpoint!r})"

    @property
    def provider_key(self) -> str:
        return "openrouter"

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return self._capabilities.get(
            model_id,
            ProviderCapabilities(model_id=model_id, reasoning_efforts=REASONING_EFFORTS, parallel_tool_calls=True),
        )

    # --- PROVIDER-02: no network ------------------------------------------------

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        options = model_input.request_options
        caps = self.describe_capabilities(options.model_id)
        body: dict[str, Any] = {"model": options.model_id, "messages": self._messages(model_input)}
        if model_input.tool_catalog:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": encode_tool_name(d.tool_id),
                        "description": d.description,
                        "parameters": thaw(d.input_schema),
                    },
                }
                for d in model_input.tool_catalog
            ]
        if options.reasoning_effort is not None:
            if options.reasoning_effort not in caps.reasoning_efforts:
                raise ProviderError(
                    ProviderErrorCode.UNSUPPORTED_CAPABILITY,
                    f"reasoning_effort {options.reasoning_effort!r} not supported by {options.model_id}",
                )
            body["reasoning"] = {"effort": options.reasoning_effort}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return NativePreparedRequest(
            body=raw,
            content_type="application/json",
            raw_representation=RawRepresentation.WIRE_EXACT,
            endpoint=self._endpoint,
            digest=hashlib.sha256(raw).hexdigest(),
        )

    def _messages(self, model_input: ModelInput) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if model_input.stable_prefix:
            messages.append({"role": "system", "content": "\n\n".join(model_input.stable_prefix)})
        for part in model_input.conversation:
            if isinstance(part, UserTurn):
                messages.append({"role": "user", "content": _text(part.blocks)})
            elif isinstance(part, AssistantTurn):
                messages.append({"role": "assistant", "content": _text(part.blocks)})
            elif isinstance(part, CompressedHistory):
                summary = _text(part.summary)
                messages.append({"role": "user", "content": f"<earlier_conversation_summary>\n{summary}\n</earlier_conversation_summary>"})
            elif isinstance(part, ToolGroupTurn):
                messages.extend(self._tool_group(part))
        state = _render_state(model_input.current_state)
        if state is not None:
            messages.append({"role": "user", "content": state})
        return messages

    @staticmethod
    def _tool_group(group: ToolGroupTurn) -> list[dict[str, Any]]:
        calls = sorted(group.calls, key=lambda c: c.ordinal)
        missing = [c.call_id for c in calls if c.call_id not in group.results]
        if missing:
            raise ProviderError(ProviderErrorCode.INCOMPLETE_HISTORY, f"{group.run_id}: no result for {missing}")
        commentary = _text(group.commentary) if group.commentary else None
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": commentary,
            "tool_calls": [
                {
                    "id": c.call_id,
                    "type": "function",
                    "function": {
                        "name": encode_tool_name(c.tool_id),
                        "arguments": json.dumps(thaw(c.arguments), ensure_ascii=False, separators=(",", ":")),
                    },
                }
                for c in calls
            ],
        }
        results: list[dict[str, Any]] = []
        for c in calls:
            result = group.results[c.call_id]
            text = _text(result.blocks)
            results.append(
                {"role": "tool", "tool_call_id": c.call_id, "content": f"[tool error] {text}" if result.is_error else text}
            )
        return [assistant, *results]

    # --- PROVIDER-01: exactly one send -----------------------------------------------

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": request.content_type}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                response = await client.post(request.endpoint, content=request.body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return NativeNoResponse(f"connect_failed:{type(exc).__name__}")  # nothing was sent
        except httpx.TransportError as exc:
            return NativeUncertain(f"transport:{type(exc).__name__}")  # may have been sent/processed
        if response.status_code != 200:
            return NativeNoResponse(f"http_{response.status_code}")
        body = response.content
        try:
            doc = json.loads(body)
        except ValueError:
            return NativeReceived(body, response.headers.get("content-type", ""))  # normalize will reject
        if isinstance(doc, dict) and "error" in doc and not doc.get("choices"):
            code = doc["error"].get("code") if isinstance(doc["error"], dict) else None
            return NativeNoResponse(f"provider_error_{code}")
        return NativeReceived(body, response.headers.get("content-type", "application/json"), _usage(doc))

    # --- PROVIDER-03 ------------------------------------------------------------------

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        try:
            doc = json.loads(received.body)
            message = doc["choices"][0]["message"]
            content = message.get("content")
            raw_calls = message.get("tool_calls") or []
            calls = tuple(
                ToolCall(
                    call_id=CallId(tc["id"]),
                    ordinal=i,
                    tool_id=decode_tool_name(tc["function"]["name"]),
                    arguments=json.loads(tc["function"].get("arguments") or "{}"),
                )
                for i, tc in enumerate(raw_calls)
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, f"{type(exc).__name__}: {exc}") from exc
        if any(not isinstance(c.arguments, Mapping) for c in calls):
            raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "tool arguments must be a JSON object")
        text = content if isinstance(content, str) else ""
        if calls:
            validate_tool_calls(calls)
            return NormalizedToolCalls(calls, (TextBlock(text),) if text.strip() else ())
        if not text.strip():
            raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "empty response")
        return NormalizedFinal((TextBlock(text),))


def _usage(doc: object) -> TokenUsage | None:
    if not isinstance(doc, dict) or not isinstance(doc.get("usage"), dict):
        return None
    usage = doc["usage"]
    completion = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        reasoning_tokens=completion.get("reasoning_tokens") if isinstance(completion, dict) else None,
        cached_input_tokens=prompt.get("cached_tokens") if isinstance(prompt, dict) else None,
    )
