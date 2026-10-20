"""A Conversation's model (V3-P64) end to end, on both Data backends."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from testkit import SCOPE, RecordingTool, calls, final, runtime, user

from himari.bootstrap import build_runtime
from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.control.api import DriveState, ModelCatalog, ModelOffer
from himari.data.api import ModelSetting, RunPurpose
from himari.prompt.api import ContextBudget, ModelInput
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest
from himari.provider.openrouter import OpenRouterAdapter
from himari.provider.scripted import ScriptedAdapter

OTHER = TrustedScope(PrincipalId("alice"), ConversationId("conv_2"))
LARGE = ModelOffer("large", "low", frozenset({"low", "high"}), ContextBudget(limit_tokens=100_000), 4_000)
SMALL = ModelOffer("small", None, frozenset(), ContextBudget(limit_tokens=3000), 2_000)
MODELS = ModelCatalog((LARGE, SMALL), "large")


def sync(case: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(case)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(case(*args, **kwargs))

    return run


def models_of(adapter: ScriptedAdapter) -> list[tuple[str, str | None]]:
    return [(i.request_options.model_id, i.request_options.reasoning_effort) for i in adapter.prepared_inputs]


async def run_models(rt: Any, scope: TrustedScope, run_ids: tuple[Any, ...]) -> list[str]:
    return [(await rt.data.read_run(scope, run_id)).model_id for run_id in run_ids]


@sync
async def test_each_conversation_uses_its_own_choice_and_the_runs_record_it() -> None:
    rt, adapter = runtime([final("a"), final("b"), final("c")], models=MODELS)
    rt.store.create_conversation(OTHER)
    await rt.data.set_model_setting(SCOPE, ModelSetting("small"))
    await rt.data.set_model_setting(OTHER, ModelSetting("large", "high"))
    first = await rt.driver.drive(SCOPE, user("one"))
    other = await rt.driver.drive(OTHER, user("two"))
    await rt.data.set_model_setting(SCOPE, None)  # back to the default
    third = await rt.driver.drive(SCOPE, user("three"))
    assert models_of(adapter) == [("small", None), ("large", "high"), ("large", "low")]
    assert await run_models(rt, SCOPE, first.run_ids + third.run_ids) == ["small", "large"]
    assert await run_models(rt, OTHER, other.run_ids) == ["large"]


class ChangingAdapter(ScriptedAdapter):
    """Runs ``change`` as its first request is sent: the user picking another
    model while the turn is under way."""

    def __init__(self, script: Any, change: Callable[[], Coroutine[Any, Any, None]]) -> None:
        super().__init__(script)
        self._change: Callable[[], Coroutine[Any, Any, None]] | None = change

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        if self._change is not None:
            change, self._change = self._change, None
            await change()
        return await super().invoke_prepared(request)


@sync
async def test_a_change_during_a_turn_applies_from_the_next_turn() -> None:
    holder: dict[str, Any] = {}

    async def change() -> None:
        await holder["rt"].data.set_model_setting(SCOPE, ModelSetting("small"))

    adapter = ChangingAdapter([calls(("echo", {})), final("done"), final("next")], change)
    rt, _ = runtime(provider=adapter, tools=[RecordingTool("echo")], models=MODELS)
    holder["rt"] = rt
    turn = await rt.driver.drive(SCOPE, user("go"))
    assert turn.state is DriveState.COMPLETED and len(turn.run_ids) == 2
    following = await rt.driver.drive(SCOPE, user("more"))
    assert [m for m, _ in models_of(adapter)] == ["large", "large", "small"]  # both Runs of the first turn kept it
    assert await run_models(rt, SCOPE, turn.run_ids + following.run_ids) == ["large", "large", "small"]


@sync
async def test_a_smaller_model_compacts_first_and_the_summary_is_written_by_it() -> None:
    rt, adapter = runtime([final("first answer"), final("PART"), final("SUMMARY"), final("second answer")],
                          models=MODELS)
    await rt.driver.drive(SCOPE, user("가" * 2500))  # about 2,500 tokens: fine for the large model
    await rt.data.set_model_setting(SCOPE, ModelSetting("small"))  # 80% of 3,000 is 2,400
    status = await rt.driver.context_status(SCOPE)
    assert status is not None and not status.measured  # the large model's count is not the small one's
    result = await rt.driver.drive(SCOPE, user("second question"))
    assert result.state is DriveState.COMPLETED and len(result.compaction_run_ids) == 2  # a chain: too large for one
    for run_id in result.compaction_run_ids:
        record = await rt.data.read_run(SCOPE, run_id)
        assert record.purpose is RunPurpose.COMPACTION and record.model_id == "small"
    assert [m for m, _ in models_of(adapter)] == ["large", "small", "small", "small"]
    status = await rt.driver.context_status(SCOPE)
    assert status is not None and status.summary == "SUMMARY"


@sync
async def test_the_context_status_shows_the_model_the_next_turn_would_use() -> None:
    rt, _ = runtime([final("a")], models=MODELS)
    status = await rt.driver.context_status(SCOPE)
    assert status is not None and status.budget_tokens == 100_000
    await rt.data.set_model_setting(SCOPE, ModelSetting("small"))
    status = await rt.driver.context_status(SCOPE)
    assert status is not None and status.budget_tokens == 3000


@pytest.mark.parametrize("setting, used, fallback", [
    (ModelSetting("gone"), ("large", "low"), "gone is no longer offered; using the default model large"),
    (ModelSetting("large", "max"), ("large", "low"),
     "the reasoning effort 'max' is no longer offered for large; using its default"),
    (ModelSetting("large", "high"), ("large", "high"), None),
])
def test_a_choice_no_longer_offered_falls_back_and_says_so(setting: ModelSetting, used: tuple[str, str],
                                                           fallback: str | None) -> None:
    async def scenario() -> None:
        rt, adapter = runtime([final("a")], models=MODELS)
        await rt.data.set_model_setting(SCOPE, setting)
        resolved = await rt.driver._resolve_model(SCOPE)
        assert resolved.fallback == fallback
        assert (await rt.driver.drive(SCOPE, user("hi"))).state is DriveState.COMPLETED
        assert models_of(adapter) == [used]
        assert await rt.data.read_model_setting(SCOPE) == setting  # the choice itself is kept as made

    asyncio.run(scenario())


# --- Continuation across a model switch (V3-049, V3-P64 §6.1), on the wire --------------

NANO, HAIKU = "openai/gpt-5-nano", "anthropic/claude-haiku-5.5"


class FakeOpenRouter:
    """Answers every request; a turn's answer carries reasoning details naming
    the model and the question, so a replay shows whose it is."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        last = body["messages"][-1]["content"]
        if "tools" not in body:  # a summarizing call
            message: dict[str, Any] = {"role": "assistant", "content": "SUMMARY"}
        else:
            message = {"role": "assistant", "content": f"answer to {last}",
                       "reasoning_details": [{"type": "reasoning.text", "text": f"{body['model']}:{last}"}]}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})


def replayed(body: dict[str, Any]) -> list[str]:
    return [d["text"] for m in body["messages"] if m["role"] == "assistant" for d in m.get("reasoning_details", ())]


@pytest.mark.parametrize("compacted", [False, True])
def test_only_continuations_of_the_requests_model_still_in_the_prompt_are_replayed(compacted: bool) -> None:
    async def scenario() -> None:
        fake = FakeOpenRouter()
        budget = ContextBudget(limit_tokens=100_000)
        models = ModelCatalog((ModelOffer(NANO, budget=budget), ModelOffer(HAIKU, budget=budget)), NANO)
        rt = build_runtime(OpenRouterAdapter("k", transport=httpx.MockTransport(fake.handle)), models=models)
        rt.store.create_conversation(SCOPE)
        await rt.driver.drive(SCOPE, user("q1"))  # nano
        await rt.driver.drive(SCOPE, user("q2"))  # nano
        if compacted:  # q1 and its answer become the summary; q2's turn is the kept tail
            assert (await rt.driver.compact(SCOPE)).state is DriveState.COMPACTED
        await rt.data.set_model_setting(SCOPE, ModelSetting(HAIKU))
        await rt.driver.drive(SCOPE, user("q3"))
        assert replayed(fake.bodies[-1]) == []  # nothing of nano's goes to haiku
        await rt.data.set_model_setting(SCOPE, None)
        await rt.driver.drive(SCOPE, user("q4"))  # nano again
        expected = [f"{NANO}:q2"] if compacted else [f"{NANO}:q1", f"{NANO}:q2"]
        assert replayed(fake.bodies[-1]) == expected  # haiku's q3 turn is sent without its details
        assert [b["model"] for b in fake.bodies if "tools" in b] == [NANO, NANO, HAIKU, NANO]

    asyncio.run(scenario())


def test_a_model_refusing_tool_calls_fails_the_turn_once_and_keeps_the_answer() -> None:
    """B1: requests always carry Tools (ask_user); a model without Tool
    calling is refused by OpenRouter. The turn ends PROVIDER_FAILED, the error
    body stays on the Run, nothing is resent, and another model works."""

    async def scenario() -> None:
        seen: list[str] = []
        refusal = {"error": {"code": 404, "message": "No endpoints found that support tool use."}}

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append(body["model"])
            assert body["tools"]  # ask_user at least
            if body["model"] == "some/no-tools-model":
                return httpx.Response(404, json=refusal)
            message = {"role": "assistant", "content": "fine"}
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})

        models = ModelCatalog((ModelOffer(NANO), ModelOffer("some/no-tools-model")), NANO)
        rt = build_runtime(OpenRouterAdapter("k", transport=httpx.MockTransport(handle)), models=models)
        rt.store.create_conversation(SCOPE)
        await rt.data.set_model_setting(SCOPE, ModelSetting("some/no-tools-model"))
        failed = await rt.driver.drive(SCOPE, user("hi"))
        assert failed.state is DriveState.PROVIDER_FAILED and failed.detail == "http_404"
        (run_id,) = failed.run_ids
        assert json.loads((await rt.data.read_raw_response(SCOPE, run_id)).body) == refusal
        assert seen == ["some/no-tools-model"]  # sent once
        await rt.data.set_model_setting(SCOPE, None)
        assert (await rt.driver.drive(SCOPE, user("again"))).state is DriveState.COMPLETED
        assert seen == ["some/no-tools-model", NANO]

    asyncio.run(scenario())


def test_a_model_input_is_composed_with_the_turns_options() -> None:
    """The scripted adapter sees exactly the options the Run records."""
    rt, adapter = runtime([final("a")], models=MODELS)
    asyncio.run(rt.data.set_model_setting(SCOPE, ModelSetting("large", "high")))
    asyncio.run(rt.driver.drive(SCOPE, user("hi")))
    (sent,) = adapter.prepared_inputs
    assert isinstance(sent, ModelInput) and sent.request_options.reasoning_effort == "high"
    assert sent.request_options.provider_key == "scripted" and sent.request_options.session_key
