"""HIMARI's life in the Application (character-life v0.1): told only with the companion and HIMARI_LIFE on, the
chosen persona's (none chosen: persona 0's), and the story writer as the ``life`` job with the memory passes'
model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from himari.bootstrap.application import Application, LifeChanged, life_settings
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import NO_PERSONA, LifeKind, UserMessage
from himari.life.api import LifeSettings, MindModel, WriteOutcome
from himari.prompt.api import LIFE_RULE, state_block
from himari.provider.api import NormalizedFinal, ProviderCapabilities
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


def test_the_story_writer_runs_only_with_the_companion_life_and_a_checked_model() -> None:
    checked, unchecked = ScriptedAdapter(memory_passes=True), ScriptedAdapter()
    values: dict[str, object] = {"life": True, "companion": True, "model_id": "m", "write_at": time(22, 0),
                                 "daily_tokens": 200_000, "time_zone": "Asia/Seoul"}
    assert life_settings(checked, **{**values, "companion": False}) == (None, "off (HIMARI_COMPANION=0)")  # type: ignore[arg-type]
    assert life_settings(checked, **{**values, "life": False}) == (None, "off (HIMARI_LIFE=0)")  # type: ignore[arg-type]
    assert life_settings(checked, **{**values, "model_id": None}) == (None, "off (no HIMARI_MEMORY_MODEL)")  # type: ignore[arg-type]
    assert life_settings(unchecked, **values) == (None, "off (m is not checked for memory passes)")  # type: ignore[arg-type]
    assert life_settings(checked, **values) == (  # type: ignore[arg-type]
        LifeSettings("m", None, time(22, 0), 200_000, "Asia/Seoul"), "on (m, at 22:00, 200,000 tokens a day each)")


@pytest.mark.parametrize("life", [True, False])
def test_a_turn_is_told_the_chosen_personas_life(tmp_path: Path, life: bool) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응."),))] * 2, memory_passes=True)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model", life=life)
        try:
            assert app.life_enabled is life
            assert (app.life_writer is not None) is life
            assert app.auto_life == ("on (scripted-model, at 22:00, 200,000 tokens a day each)" if life
                                     else "off (HIMARI_LIFE=0)")
            assert ("life" in app.ticker._jobs) is life  # noqa: SLF001
            lucy = await app.data.create_persona(ME, "Lucy", "이름: 루시.")
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            await app.data.add_life_fact(ME, lucy.persona_id, "고양이를 무서워한다.")
            await app.data.add_life_fact(ME, NO_PERSONA, "기본 페르소나의 사실.")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "요즘 뭐 해?")
            await app.runner.wait(scope)
            asked = adapter.prepared_inputs[-1]
            block = state_block(asked.current_state) or ""
            assert ("고양이를 무서워한다." in block) is life
            assert "기본 페르소나의 사실." not in block  # another persona's
            assert (LIFE_RULE in "\n".join(asked.stable_prefix)) is life
            if life:  # none chosen: persona 0's life
                await app.data.choose_persona(ME, None, 1)
                await app.send(scope, "그럼 지금은?")
                await app.runner.wait(scope)
                block = state_block(adapter.prepared_inputs[-1].current_state) or ""
                assert "기본 페르소나의 사실." in block and "고양이를 무서워한다." not in block
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_web_adds_a_fact_only_to_the_persona_it_showed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await Application.open(tmp_path / "data", ScriptedAdapter(), model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None)
        try:
            assert app.life_enabled and app.life_writer is None and app.auto_life == "off (no HIMARI_MEMORY_MODEL)"
            added = await app.add_life_fact(ME, None, "비 오는 날을 좋아한다.")
            chosen, entries = await app.life(ME)
            assert chosen is None and entries == (added,) and added.persona_id == NO_PERSONA
            lucy = await app.data.create_persona(ME, "Lucy", "")
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            with pytest.raises(LifeChanged):  # the page showed no persona's life
                await app.add_life_fact(ME, None, "lost")
            await app.add_life_fact(ME, lucy.persona_id, "루시의 사실.")
            chosen, entries = await app.life(ME)
            assert chosen == lucy and [(e.kind, e.text) for e in entries] == [(LifeKind.FACT, "루시의 사실.")]
        finally:
            await app.close()

    asyncio.run(scenario())


class _Models(ScriptedAdapter):
    """A standard model, a companion model checked for free wakes and a memory model."""

    def __init__(self) -> None:
        super().__init__()
        self._models = {
            "standard-model": ProviderCapabilities("standard-model", speaks_first=True, context_tokens=100_000),
            "companion-model": ProviderCapabilities("companion-model", image_input=True, screen_points=True,
                                                    screen_tool=True, speaks_first=True, free_wake=True,
                                                    companion=True, context_tokens=100_000),
            "memory-model": ProviderCapabilities("memory-model", memory_passes=True, context_tokens=100_000),
        }

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return self._models.get(model_id) or super().describe_capabilities(model_id)


def test_a_self_run_uses_the_day_sessions_model_else_the_default_model(tmp_path: Path) -> None:
    """character-day §10, §18.7: the model of the day session (where a wake goes) — no free-wake branch any
    more; with no day session open, the default model, with why."""
    async def scenario() -> None:
        app = await Application.open(tmp_path / "data", _Models(), model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)),
                                     memory_model="memory-model", sandbox=False, cleanup_seconds=None)
        try:
            assert app.life_writer is not None
            assert await app.mind_model(ME) == MindModel(None, reason="the default model (no day session)")
            assert (await app.life_writer.mind_model(ME)).model_id == "standard-model"
            other = TrustedScope(ME, await app.create_conversation(ME, initial_model="companion-model"))
            await app.data.append_event(other, UserMessage((TextBlock("안녕"),)))  # not the day session
            assert (await app.mind_model(ME)).model_id is None
            session = await app.day_session(ME)  # opened on the companion's default model
            assert session is not None
            assert await app.mind_model(ME) == MindModel("companion-model", None, "the day session's model")
            assert (await app.life_writer.mind_model(ME)).model_id == "companion-model"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_remarks_are_the_users_words_in_the_day_session_meanwhile(tmp_path: Path) -> None:
    """character-mind §4.4, character-day §18.9: what the user said in the day session while HIMARI worked, as
    text, oldest first."""
    async def scenario() -> None:
        app = await Application.open(tmp_path / "data", _Models(), model_id="standard-model",
                                     models=(("standard-model", None), ("companion-model", None)),
                                     memory_model="memory-model", sandbox=False, cleanup_seconds=None)
        try:
            assert await app.remarks(ME, datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC)) == ()
            session = await app.day_session(ME)
            assert session is not None
            scope = TrustedScope(ME, session.conversation_id)
            before = datetime.now(UTC)
            for said in ("방열판 세 겹은 과하지 않아?", "두 겹이 좋겠어"):
                await app.data.append_event(scope, UserMessage((TextBlock(said),)))
            assert await app.remarks(ME, before, datetime.now(UTC)) == ("방열판 세 겹은 과하지 않아?", "두 겹이 좋겠어")
            assert await app.remarks(ME, datetime.now(UTC) + timedelta(seconds=1),
                                     datetime.now(UTC) + timedelta(hours=1)) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_closed_day_session_is_read_for_the_diary_and_its_closing_wakes_the_amend(tmp_path: Path) -> None:
    """character-diary §5, §16.4: MindLink.session_input with a conversation id reads that day session even closed
    (another conversation, or none, is None); the closing's callback also starts the diary's amend."""
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응."),))], memory_passes=True, companion=True)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, memory_model="scripted-model", life=True, companion=True)
        try:
            session = await app.day_session(ME)
            assert session is not None and app.life_writer is not None
            scope = TrustedScope(ME, session.conversation_id)
            await app.send(scope, "오늘 재밌었어")
            await app.runner.wait(scope)
            assert await app.data.close_day_session(ME, session.conversation_id, datetime.now(UTC))
            closed = await app.session_input(ME, session.conversation_id)
            assert closed is not None and closed.conversation_id == session.conversation_id
            assert closed.day == session.day_session
            assert any(isinstance(e.payload, UserMessage) for e in closed.inputs.events)
            assert await app.session_input(ME) is None  # no open day session now
            other = await app.create_conversation(ME)
            assert await app.session_input(ME, other) is None  # not a day session
            assert await app.session_input(PrincipalId("someone"), session.conversation_id) is None  # not theirs
            amended: list[PrincipalId] = []

            async def amend(principal: PrincipalId) -> WriteOutcome:
                amended.append(principal)
                return WriteOutcome.NOTHING

            app.life_writer.amend = amend  # type: ignore[method-assign]
            await app._day_closed(ME, scope)  # noqa: SLF001
            await asyncio.sleep(0.05)
            assert amended == [ME]
        finally:
            await app.close()

    asyncio.run(scenario())
