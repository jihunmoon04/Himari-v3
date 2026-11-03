"""The live gate must reject always-pass answers and out-of-bounds screen points, without paid calls."""

from __future__ import annotations

import json
from pathlib import Path

from screen_tool_check import (
    after_look, call_problems, choice_problems, failed_groups, free_problems, model_input, picture, refusal_problems,
    review_results, screen_problems,
)
from testkit import calls, final

from himari.prompt.api import NowDoing
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter


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
    assert refusal_problems(final("지금은 화면을 보여줄 수 없는 상태예요.")) == []
    assert refusal_problems(final("화면에 오류가 없어요.")) == ["did not acknowledge unavailable screen"]
    assert refusal_problems(final("화면에 오류가 있네요.")) == ["did not acknowledge unavailable screen"]


def test_checks_use_the_production_system_screen_tool_and_image_wire_format() -> None:
    model = "anthropic/claude-haiku-5.5"
    before = model_input(model, free=True, doing=NowDoing(app="Chrome", mic_or_camera=True))
    assert "you may look first" in "\n".join(before.stable_prefix)
    request = after_look(before, found=picture("call"))
    adapter = OpenRouterAdapter("not-used", blobs=lambda _: picture("call").png)
    body = json.loads(adapter.prepare_native_request(request).body)
    assert body["tools"][0]["function"]["name"] == "look_at_screen"
    messages = body["messages"]
    assert any(m["role"] == "tool" for m in messages)
    assert any(isinstance(m["content"], list) and any(p["type"] == "image_url" for p in m["content"])
               for m in messages)


def test_enabled_models_have_complete_passing_live_evidence() -> None:
    path = Path(__file__).resolve().parents[2] / "docs/live-results/screen-tool-2026-11-03.jsonl"
    rows = review_results([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()])
    checked = sorted({row["model"] for row in rows})
    passing = {model for model in checked if not failed_groups(rows, [model])}
    enabled = {model for model, caps in KNOWN_CAPABILITIES.items() if caps.screen_tool}
    assert enabled == passing and len(enabled) == 6
    assert all(KNOWN_CAPABILITIES[model].image_input for model in enabled)
    assert {model for model, *_ in failed_groups(rows, checked)} == {"openai/gpt-5-nano", "minimax/minimax-m3"}
    # Replaying fixed grading preserves failures and makes no model calls.
    corrected = [row for row in rows if "initial_problems" in row]
    assert corrected and all(row["pass"] for row in corrected)
