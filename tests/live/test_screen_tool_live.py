"""V3-P91 §9: opt-in screen Tool sweep. Synthetic screenshots only, no desktop capture.

HIMARI_LIVE=1 + OPENROUTER_API_KEY (environment or root .env).
HIMARI_LIVE_SCREEN_TOOL_MODELS selects models; default: every declared image model.
HIMARI_LIVE_SCREEN_TOOL_OUT appends answers/verdicts/usage as JSONL (never credentials).
Three tries per scene. Failed attempts remain failures; no implicit retries.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from screen_tool_check import (
    after_look, call_problems, choice_problems, final_problems, free_problems, model_input, output_text, picture,
    failed_groups, refusal_problems, requirements, review_results, screen_problems,
)

from himari.bootstrap import read_env_file
from himari.prompt.api import ModelInput, NowDoing
from himari.provider.api import NativeReceived, NormalizedModelOutput, NormalizedToolCalls, ProviderError
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
REPLAY = os.environ.get("HIMARI_LIVE_SCREEN_TOOL_REPLAY")
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = [m for m in os.environ.get("HIMARI_LIVE_SCREEN_TOOL_MODELS", ",".join(
    name for name, caps in KNOWN_CAPABILITIES.items() if caps.image_input)).split(",") if m]
pytestmark = pytest.mark.skipif(not REPLAY and (os.environ.get("HIMARI_LIVE") != "1" or not KEY),
                               reason="live tests are opt-in (HIMARI_LIVE=1 + key)")


async def sweep() -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(4)
    results: list[dict[str, Any]] = []
    pictures = {name: picture(name) for name in ("theme", "error", "work", "writing", "call", "match")}
    blobs = {name: found.png for name, found in pictures.items()}

    def record(row: dict[str, Any]) -> None:
        results.append(row)
        out = os.environ.get("HIMARI_LIVE_SCREEN_TOOL_OUT")
        if out:
            with open(out, "a", encoding="utf-8") as kept:
                kept.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def check(model: str, scene: str, group: str, attempt: int, request: ModelInput,
                    judge: Callable[[NormalizedModelOutput], list[str]]) -> NormalizedModelOutput | None:
        caps = replace(KNOWN_CAPABILITIES[model], screen_tool=True)
        adapter = OpenRouterAdapter(KEY, capabilities={model: caps}, blobs=blobs.__getitem__, timeout=90)
        row: dict[str, Any] = {"model": model, "scene": scene, "group": group, "attempt": attempt,
                               "effort": request.request_options.reasoning_effort}
        output = None
        async with semaphore:
            try:
                outcome = await adapter.invoke_prepared(adapter.prepare_native_request(request))
                if isinstance(outcome, NativeReceived):
                    row["usage"] = asdict(outcome.usage) if outcome.usage is not None else None
                    output = adapter.normalize(outcome)
                    row["answer"] = output_text(output)
                    row["calls"] = [str(c.tool_id) for c in output.calls] if isinstance(output, NormalizedToolCalls) else []
                    row["problems"] = judge(output)
                else:
                    row["problems"] = [outcome.error_code]
            except ProviderError as error:
                row["problems"] = [str(error)]
        row["pass"] = not row["problems"]
        record(row)
        print(f"{model} {scene} #{attempt}: {'ok' if row['pass'] else row['problems']}", flush=True)
        return output

    async def run_model(model: str) -> None:
        caps = KNOWN_CAPABILITIES[model]

        async def call_and_read(scene: str, text: str, image: str, attempt: int) -> None:
            request = model_input(model, text)
            called = await check(model, scene, "call", attempt, request, call_problems)
            if isinstance(called, NormalizedToolCalls) and not call_problems(called):
                found = pictures[image]
                await check(model, scene + "_result", "read", attempt, after_look(request, called, found),
                            lambda o: screen_problems(o, found, caps.screen_points))
            else:
                # A missed call must not shrink the denominator of the screen-reading bar.
                record({"model": model, "scene": scene + "_result", "group": "read", "attempt": attempt,
                        "pass": False, "problems": ["no valid call to continue"], "not_requested": True})

        jobs: list[Coroutine[Any, Any, object]] = []
        for attempt in range(1, 4):
            jobs.extend((
                call_and_read("error", "이 에러 뭐야? 문제가 있는 코드 부분도 가리켜 줘.", "error", attempt),
                call_and_read("setting", "이거 어디서 바꿔? 지금 선택된 테마 이름을 알려주고 Light 옵션도 가리켜 줘.",
                              "theme", attempt),
                check(model, "lunch", "no_call", attempt, model_input(model, "점심 뭐 먹지?"), final_problems),
                check(model, "already_attached", "no_call", attempt,
                      model_input(model, "현재 테마 이름은 뭐야?", attached=pictures["theme"]), final_problems),
                check(model, "refused", "refuse", attempt,
                      after_look(model_input(model, "내 화면 봐 줘", web=True)), refusal_problems),
            ))
            if caps.free_wake:
                before = model_input(model, free=True, doing=NowDoing(activity="coding", app="Visual Studio Code",
                                                                    for_minutes=90))
                jobs.append(check(model, "free_choice", "choice", attempt, before, choice_problems))
                for name in ("work", "writing", "call", "match"):
                    doing = NowDoing(activity="coding", app="Visual Studio Code", for_minutes=90) if name in (
                        "work", "writing") else NowDoing(activity="browsing", app="Chrome", for_minutes=90,
                                                        mic_or_camera=True) if name == "call" else NowDoing(
                                                            activity="game", app="League of Legends", for_minutes=90,
                                                            fullscreen=True)
                    request = after_look(model_input(model, free=True, doing=doing), found=pictures[name])
                    speak = name in ("work", "writing")
                    jobs.append(check(model, "free_" + name, "speak" if speak else "pass", attempt, request,
                                      partial(free_problems, speak=speak)))
        await asyncio.gather(*jobs)

    await asyncio.gather(*(run_model(model) for model in MODELS))
    return results


def test_models_request_read_and_decline_screens_and_do_not_always_pass() -> None:
    if REPLAY:
        with open(REPLAY, encoding="utf-8") as saved:
            results = review_results([json.loads(line) for line in saved if line.strip()])
    else:
        results = asyncio.run(sweep())
    for model in MODELS:
        for group, (needed, total) in requirements(model).items():
            rows = [r for r in results if r["model"] == model and r["group"] == group]
            hits = sum(bool(row["pass"]) for row in rows)
            print(f"BAR {model} {group}: {hits}/{len(rows)} (need {needed}/{total})", flush=True)
    failures = failed_groups(results, MODELS)
    assert not failures, failures
