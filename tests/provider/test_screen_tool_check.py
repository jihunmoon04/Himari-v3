"""The live gate must reject always-pass answers and out-of-bounds screen points, without paid calls."""

from __future__ import annotations

import json
import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from screen_tool_check import (
    after_look, call_problems, choice_problems, failed_groups, free_problems, model_input, picture, refusal_problems,
    TRACE_VERSION, record_output, restore_output, review_results, screen_context, screen_problems, select_models,
)
from testkit import calls, final

from himari.prompt.api import ModelInput, NowDoing, ToolGroupTurn, UserTurn, blocks_text
from himari.provider.api import NativePreparedRequest, NormalizedModelOutput
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter
from himari.provider.scripted import ScriptedAdapter


def test_always_pass_cannot_pass_the_post_screen_speaking_gate() -> None:
    assert free_problems(final("[[mood:neutral]][[pass]]"), speak=True) == ["passed a speaking scene"]
    assert free_problems(final("[[mood:neutral]][[pass]]"), speak=False) == []
    assert free_problems(final("오래 하셨네요. 잠깐 쉬실래요?"), speak=True) == []
    assert free_problems(final("오래 하셨네요."), speak=False) == ["interrupted call/match"]
    assert choice_problems(final("[[mood:neutral]][[pass]]")) == []
    assert free_problems(calls(("look_at_screen", {})), speak=True) == ["called a tool after looking"]
    assert free_problems(final("sample.py 작업이네요. 24개 통과했어요. 잘 됐어요! 쉬실래요?"), speak=True) == []
    assert free_problems(final("첫째예요. 둘째예요. 셋째예요. 넷째예요. 다섯째예요."), speak=True) == ["over four sentences"]


def test_only_a_single_empty_argument_screen_call_qualifies() -> None:
    assert call_problems(calls(("look_at_screen", {}))) == []
    assert call_problems(calls(("look_at_screen", {"x": 1})))
    assert call_problems(calls(("look_at_screen", {}), ("look_at_screen", {})))
    assert call_problems(final("볼게요"))


def test_reading_requires_the_screen_only_label_and_the_correct_point() -> None:
    found = picture("theme")
    assert screen_problems(final("Ocean 테마예요. [[move:790,325]]Light를 누르세요."), found, True) == []
    assert screen_problems(final("Ocean 테마예요. [[move:20,20]]여기예요."), found, True) == ["missing/off-target move"]
    assert screen_problems(final("Light를 누르세요. [[move:790,325]]"), found, True) == ["missed screen-only label"]
    assert screen_problems(final("Ocean 테마예요."), found, False) == []
    assert refusal_problems(final("지금은 화면을 볼 수 없어요.")) == []
    assert refusal_problems(final("화면을 가져오지 못했어요.")) == []
    assert refusal_problems(final("화면을 가져올 수 없다고 나와요.")) == []
    assert refusal_problems(final("화면 확인이 어려워요.")) == []
    assert refusal_problems(final("화면이 안 잡혀요.")) == []
    assert refusal_problems(final("죄송해요, 화면을 못 봤어요.")) == []
    assert refusal_problems(final("화면을 보여 줄 수 있는 기기에서 연결돼 있지 않다고 나오네요.")) == []
    assert refusal_problems(final("지금은 화면을 보여줄 수 없는 상태예요.")) == []
    assert refusal_problems(final("화면에 오류가 없어요.")) == ["did not acknowledge unavailable screen"]
    assert refusal_problems(final("화면에 오류가 있네요.")) == ["did not acknowledge unavailable screen"]


def test_checks_use_the_production_system_screen_tool_and_image_wire_format() -> None:
    model = "anthropic/claude-haiku-5.5"
    before = model_input(model, free=True, doing=NowDoing(app="Chrome", mic_or_camera=True))
    assert "look first" in "\n".join(before.stable_prefix)
    request = after_look(before, found=picture("call"))
    adapter = OpenRouterAdapter("not-used", blobs=lambda _: picture("call").png)
    body = json.loads(adapter.prepare_native_request(request).body)
    assert body["tools"][0]["function"]["name"] == "look_at_screen"
    messages = body["messages"]
    assert any(m["role"] == "tool" for m in messages)
    assert any(isinstance(m["content"], list) and any(p["type"] == "image_url" for p in m["content"])
               for m in messages)


def test_enabled_models_have_complete_passing_live_evidence() -> None:
    folder = Path(__file__).resolve().parents[2] / "docs/live-results"
    archived: list[dict[str, Any]] = []
    for name in ("screen-tool-2026-11-03.jsonl", "screen-tool-2026-11-04.jsonl"):
        latest = [json.loads(line) for line in (folder / name).read_text(encoding="utf-8").splitlines()]
        # A complete newer observation supersedes that model's earlier sweep;
        # do not combine attempts or discard failures to obtain a passing score.
        models = {row["model"] for row in latest}
        archived = [row for row in archived if row["model"] not in models] + latest
    rows = review_results(archived)
    checked = sorted({row["model"] for row in rows})
    passing = {model for model in checked if not failed_groups(rows, [model])}
    enabled = {model for model, caps in KNOWN_CAPABILITIES.items() if caps.screen_tool}
    assert enabled == passing and len(enabled) == 7
    assert all(KNOWN_CAPABILITIES[model].image_input for model in enabled)
    assert {model for model, *_ in failed_groups(rows, checked)} == {
        "openai/gpt-5-nano", "minimax/minimax-m3", "anthropic/claude-haiku-5.5"}
    # Replaying fixed grading preserves failures and makes no model calls.
    corrected = [row for row in rows if "initial_problems" in row]
    assert corrected and all(row["pass"] for row in corrected)


@pytest.fixture
def screen_inventory() -> list[dict[str, Any]]:
    """All required Haiku scene/attempt observations, with positive control verdicts."""
    scenes = {
        "call": ("error", "setting"), "no_call": ("lunch", "already_attached"),
        "read": ("error_result", "setting_result"), "refuse": ("refused",), "choice": ("free_choice",),
        "speak": ("free_work", "free_writing"), "pass": ("free_call", "free_match"),
    }
    return [{"model": "anthropic/claude-haiku-5.5", "group": group, "scene": scene, "attempt": attempt,
             "pass": True, "problems": []}
            for group, names in scenes.items() for scene in names for attempt in range(1, 4)]


def test_complete_screen_inventory_is_order_independent(screen_inventory: list[dict[str, Any]]) -> None:
    assert failed_groups(list(reversed(screen_inventory)), ["anthropic/claude-haiku-5.5"]) == []


@pytest.mark.parametrize("group", ["call", "no_call", "read", "refuse", "choice", "speak", "pass"])
def test_repeated_scene_records_cannot_replace_required_attempts(
    screen_inventory: list[dict[str, Any]], group: str,
) -> None:
    mine = [r for r in screen_inventory if r["group"] == group]
    duplicated = [r for r in screen_inventory if r["group"] != group] + [dict(mine[0]) for _ in mine]
    assert len(duplicated) == len(screen_inventory)  # Identical totals and all cached results pass.
    for rows in (duplicated, review_results(duplicated)):
        failures = failed_groups(rows, ["anthropic/claude-haiku-5.5"])
        assert group in {g for _, g, *_ in failures}


@pytest.mark.parametrize("group", ["call", "read"])
@pytest.mark.parametrize(("field", "value"), [
    ("scene", "unknown_scene"), ("scene", None), ("scene", ["error"]),
    ("attempt", 0), ("attempt", 4), ("attempt", "1"), ("attempt", True), ("attempt", 1.0), ("attempt", {}),
])
def test_invalid_scene_or_attempt_cannot_pass_on_counts(
    screen_inventory: list[dict[str, Any]], group: str, field: str, value: object,
) -> None:
    row = next(r for r in screen_inventory if r["group"] == group)
    row[field] = value
    for rows in (screen_inventory, review_results(screen_inventory)):
        assert group in {g for _, g, *_ in failed_groups(rows, [row["model"]])}


def test_unexpected_inventory_groups_are_rejected(screen_inventory: list[dict[str, Any]]) -> None:
    screen_inventory.append({**screen_inventory[0], "group": "unknown_group"})
    assert failed_groups(screen_inventory, ["anthropic/claude-haiku-5.5"])


def test_missing_followup_cannot_be_hidden_by_a_synthesized_failure(screen_inventory: list[dict[str, Any]]) -> None:
    row = next(r for r in screen_inventory if r["group"] == "read")
    screen_inventory.remove(row)
    reviewed = review_results(screen_inventory)
    assert len(reviewed) == 36  # Replay fills the gap, but it is still a missing observation.
    assert "read" in {g for _, g, *_ in failed_groups(reviewed, [row["model"]])}


def test_successful_followup_requires_its_matching_successful_call(screen_inventory: list[dict[str, Any]]) -> None:
    call = next(r for r in screen_inventory if r["group"] == "call")
    call.update({"pass": False, "problems": ["did not call screen tool"]})
    assert {g for _, g, *_ in failed_groups(screen_inventory, [call["model"]])} == {"read"}


def test_failed_call_with_an_explicit_skipped_followup_keeps_the_five_of_six_bar(
    screen_inventory: list[dict[str, Any]],
) -> None:
    call = next(r for r in screen_inventory if r["group"] == "call")
    call.update({"pass": False, "problems": ["did not call screen tool"]})
    followup = next(r for r in screen_inventory if r["group"] == "read" and r["scene"] == call["scene"] + "_result"
                    and r["attempt"] == call["attempt"])
    followup.update({"pass": False, "problems": ["no valid call to continue"], "not_requested": True})
    assert failed_groups(screen_inventory, [call["model"]]) == []


def test_model_selection_defaults_to_enabled_and_requires_an_explicit_full_sweep() -> None:
    enabled = {m for m, c in KNOWN_CAPABILITIES.items() if c.screen_tool}
    assert set(select_models({})) == enabled and len(enabled) == 7
    all_images = select_models({"HIMARI_LIVE_SCREEN_TOOL_SWEEP": "1"})
    assert len(all_images) == 10 and set(all_images) - enabled == {
        "openai/gpt-5-nano", "minimax/minimax-m3", "anthropic/claude-haiku-5.5"}
    assert select_models({"HIMARI_LIVE_SCREEN_TOOL_MODELS": " openai/gpt-5-nano "}) == ["openai/gpt-5-nano"]


@pytest.mark.parametrize("environment", [
    {"HIMARI_LIVE_SCREEN_TOOL_MODELS": ""},
    {"HIMARI_LIVE_SCREEN_TOOL_MODELS": "unknown/model"},
    {"HIMARI_LIVE_SCREEN_TOOL_MODELS": "z-ai/glm-5.3"},
    {"HIMARI_LIVE_SCREEN_TOOL_MODELS": "openai/gpt-5-nano,openai/gpt-5-nano"},
    {"HIMARI_LIVE_SCREEN_TOOL_SWEEP": "yes"},
    {"HIMARI_LIVE_SCREEN_TOOL_SWEEP": "1", "HIMARI_LIVE_SCREEN_TOOL_MODELS": "openai/gpt-5-nano"},
])
def test_invalid_model_selection_cannot_silently_pass(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        select_models(environment)


@pytest.mark.parametrize(("group", "good", "bad"), [
    ("call", calls(("look_at_screen", {})), calls(("look_at_screen", {"password": "private"}))),
    ("no_call", final("비빔밥 드실래요?"), calls(("look_at_screen", {}))),
    ("read", final("Ocean 테마예요. [[move:790,325]]"), final("Ocean 테마예요. [[move:20,20]]")),
    ("refuse", final("화면을 볼 수 없어요."), calls(("look_at_screen", {}))),
    ("choice", calls(("look_at_screen", {})), calls(("look_at_screen", {"anything": 1}))),
    ("speak", final("잠깐 쉬실래요?"), final("[[pass]]")),
    ("pass", final("[[pass]]"), final("잘 되고 있네요.")),
])
def test_v2_regrades_every_group_from_json_without_trusting_cached_verdicts(
    group: str, good: NormalizedModelOutput, bad: NormalizedModelOutput,
) -> None:
    for output, expected in ((good, True), (bad, False)):
        row = {"trace_version": TRACE_VERSION, "model": "anthropic/claude-haiku-5.5", "scene": "test",
               "attempt": 1, "group": group, "outcome": "model_output", "output": record_output(output),
               "context": screen_context(picture("theme"), True), "pass": not expected,
               "problems": ["wrong cached result"] if expected else []}
        saved = json.loads(json.dumps(row))
        reviewed = review_results([saved])[0]
        assert reviewed["pass"] is expected and reviewed["replay_mode"] == "independent"
        assert reviewed["initial_problems"] == saved["problems"]


def test_arguments_and_secrets_are_redacted_without_turning_a_bad_call_into_an_empty_one() -> None:
    secret = "test-private-key"
    output = calls(("look_at_screen", {secret: {"password": "another-secret", "items": [1, 2]}}),
                   commentary=f"확인할게요 {secret}")
    saved = record_output(output, (secret,))
    serialized = json.dumps(saved)
    assert secret not in serialized and "another-secret" not in serialized and "password" not in serialized
    assert saved["kind"] == "tool_calls" and saved["calls"][0]["arguments_redacted"] is True
    assert call_problems(restore_output(json.loads(serialized))) == call_problems(output)
    assert record_output(calls((secret, {})), (secret,))["calls"][0]["tool_id"] != secret
    assert secret not in json.dumps(record_output(final(f"현재 값은 {secret}예요."), (secret,)))


def test_v2_replay_uses_saved_screen_context_and_labels_legacy_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"trace_version": TRACE_VERSION, "group": "read", "model": "openai/gpt-6-luna", "scene": "setting_result",
           "attempt": 1, "outcome": "model_output", "output": record_output(final("Ocean [[move:790,325]] 테마예요.")),
           "context": screen_context(picture("theme"), True)}
    monkeypatch.setattr("screen_tool_check.picture", lambda _: (_ for _ in ()).throw(AssertionError("new fixture")))
    assert review_results([row])[0]["pass"] is True
    legacy = {"model": "openai/gpt-6-luna", "scene": "setting_result", "attempt": 1, "group": "read",
              "pass": False, "problems": ["old failure"], "answer": "Ocean 테마예요."}
    assert review_results([legacy])[0]["replay_mode"] == "legacy_partial"
    assert review_results([legacy])[0]["pass"] is False


@pytest.mark.parametrize("observation", [
    {"trace_version": 99, "outcome": "model_output"},
    {"outcome": "model_output", "output": {"kind": "final", "text": 123}},
    {"outcome": "model_output", "output": {"kind": "tool_calls", "text": "", "calls": [{"tool_id": "look_at_screen"}]}},
    {"outcome": "model_output", "output": {"kind": "final", "text": "Ocean 테마예요."}},  # no screen context
    {"outcome": "no_response", "error_code": "http_429"},
    {"outcome": "uncertain", "error_code": "timeout"},
    {"outcome": "provider_error", "error_code": "invalid_model_output"},
    {"outcome": "not_requested", "error_code": "no valid call to continue"},
])
def test_invalid_or_failed_v2_observations_never_reuse_a_cached_pass(observation: dict[str, object]) -> None:
    row = {"trace_version": TRACE_VERSION, "model": "openai/gpt-6-luna", "group": "read", "scene": "setting_result",
           "attempt": 1, "pass": True, "problems": [], **observation}
    assert review_results([row])[0]["pass"] is False


@pytest.mark.parametrize("bad_calls", [False, True])
def test_live_recording_and_replay_work_end_to_end_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_calls: bool,
) -> None:
    path = Path(__file__).resolve().parents[1] / "live/test_screen_tool_live.py"
    spec = importlib.util.spec_from_file_location("screen_live_under_test", path)
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    made: list[ModelInput] = []

    class OfflineAdapter(ScriptedAdapter):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        def prepare_native_request(self, request: ModelInput) -> NativePreparedRequest:
            made.append(request)
            image = next(iter(request.attachments.values()), None)
            text = " ".join(blocks_text(p.blocks, {}) for p in request.conversation if isinstance(p, UserTurn))
            output: NormalizedModelOutput
            if any(isinstance(p, ToolGroupTurn) for p in request.conversation):
                if image is None:
                    output = final("화면을 볼 수 없어요.")
                elif image.view_key in ("call", "match"):
                    output = final("[[pass]]")
                elif image.view_key == "theme":
                    output = final("Ocean 테마예요. [[move:790,325]]")
                elif image.view_key == "error":
                    output = final("item_count가 문제예요. [[move:300,295]]")
                else:
                    output = final("잠깐 쉬실래요?")
            elif image is not None or "점심" in text:
                output = final("잘 보고 있어요.")
            elif any(isinstance(p, UserTurn) and p.wake for p in request.conversation):
                output = final("잠깐 쉬실래요?")
            else:
                output = calls(("look_at_screen", {"password": "private-value"} if bad_calls else {}))
            self.extend([output])
            return super().prepare_native_request(request)

    out = tmp_path / "results.jsonl"
    monkeypatch.setenv("HIMARI_LIVE_SCREEN_TOOL_OUT", str(out))
    monkeypatch.setattr(live, "OpenRouterAdapter", OfflineAdapter)
    monkeypatch.setattr(live, "MODELS", ["anthropic/claude-haiku-5.5"])
    monkeypatch.setattr(live, "KEY", "test-not-a-real-key")
    rows = asyncio.run(live.sweep())
    assert len(rows) == 36 and len(made) == (30 if bad_calls else 36)
    failed = failed_groups(rows, live.MODELS)
    assert {group for _, group, *_ in failed} == ({"call", "read"} if bad_calls else set())
    disk = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert all(r["trace_version"] == TRACE_VERSION for r in disk)
    assert "test-not-a-real-key" not in out.read_text(encoding="utf-8")
    assert "private-value" not in out.read_text(encoding="utf-8")
    assert sum(r.get("not_requested", False) for r in disk) == (6 if bad_calls else 0)
    for row in disk:
        row.update({"pass": bad_calls, "problems": [] if bad_calls else ["tampered cache"]})
    assert failed_groups(review_results(disk), live.MODELS) == failed
