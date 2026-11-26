"""HIMARI's state in the Application (character-state v0.1 §2, §3): a final answer's last mood other than neutral
becomes the chosen persona's mood, told to the next turns with the life (the companion and HIMARI_LIFE on), and
what HIMARI is doing now comes from its timed plans and the quiet hours."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from himari.bootstrap.application import Application
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import NO_PERSONA, LifePlan, LifeWriteStart, MoodSource, RawReceived
from himari.proactive import ProactiveSettings
from himari.prompt.api import MOOD_RULE, state_data
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


def said(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


@pytest.mark.parametrize("life", [True, False])
def test_an_answers_last_mood_is_kept_for_the_persona_and_told_to_the_next_turn(tmp_path: Path, life: bool) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([said("[[mood:happy]]좋아! [[mood:surprised]]정말? [[mood:neutral]]그래."),
                                   said("[[mood:neutral]]응."), said("응.")])
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, life=life)
        try:
            lucy = await app.data.create_persona(ME, "Lucy", "이름: 루시.")
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "합격했어!")
            await app.runner.wait(scope)
            assert "himari_mood" not in state_data(adapter.prepared_inputs[-1].current_state)  # none yet
            kept = await app.data.read_mood(ME, lucy.persona_id)
            if not life:
                assert kept is None and await app.current_mood(ME) is None
                assert MOOD_RULE not in "\n".join(adapter.prepared_inputs[-1].stable_prefix)
                return
            assert kept is not None and (kept.mood, kept.source) == ("surprised", MoodSource.REPLY)
            assert await app.data.read_mood(ME, NO_PERSONA) is None  # the persona's own
            assert await app.current_mood(ME) == "surprised"
            await app.send(scope, "응")
            await app.runner.wait(scope)
            asked = adapter.prepared_inputs[-1]
            assert MOOD_RULE in "\n".join(asked.stable_prefix)
            told = state_data(asked.current_state)["himari_mood"]
            assert isinstance(told, dict) and told["mood"] == "surprised" and told["hours_ago"] == 0
            assert (await app.data.read_mood(ME, lucy.persona_id)) == kept  # neutral only: left as it was
            mood, _ = await app.himari_state(ME)
            assert mood == kept
            await app.data.choose_persona(ME, None, 1)
            assert await app.current_mood(ME) == "neutral"  # another persona: no mood of its own
        finally:
            await app.close()

    asyncio.run(scenario())


def test_what_himari_is_doing_comes_from_a_timed_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.life.keeper import DayKeeper

    async def no_diary_slot(*args: object) -> None:
        return None  # the keeper's clock is held at noon, the plan is now: its diary slot could hold the hour

    monkeypatch.setattr(DayKeeper, "_place_diary", no_diary_slot)

    async def scenario() -> None:
        zone = ZoneInfo("Asia/Seoul")
        local = datetime.now(UTC).astimezone(zone)
        start = (local - timedelta(minutes=10)).replace(second=0, microsecond=0)
        adapter = ScriptedAdapter([said("응.")])  # character-diary §9: no quiet hours, never asleep by the clock
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, time_zone="Asia/Seoul", proactive=ProactiveSettings())
        try:
            write_id = await app.data.begin_life_write(ME, LifeWriteStart(
                NO_PERSONA, (start.date() - timedelta(days=1)).isoformat(), "openrouter", "m", b"{}",
                "application/json", 10))
            await app.data.mark_life_write_dispatched(write_id)
            await app.data.record_life_write(write_id, RawReceived(b"{}", "application/json", None))
            await app.data.apply_life_write(write_id, LifePlan("어제.", ((start.date().isoformat(), "빵집 들르기"),),
                                                               (), (f"{start:%H:%M}",)), now=datetime.now(UTC))
            _, now = await app.himari_state(ME)
            assert now is not None and (now.doing, now.since) == ("빵집 들르기", f"{start:%H:%M}")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "뭐 해?")
            await app.runner.wait(scope)
            told = state_data(adapter.prepared_inputs[-1].current_state)["himari_now"]
            assert isinstance(told, dict) and told["doing"] == "빵집 들르기"
        finally:
            await app.close()

    asyncio.run(scenario())
