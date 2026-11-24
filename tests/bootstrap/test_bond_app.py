"""The bond in the Application (character-bond v0.1): told with HIMARI's life (the companion and HIMARI_LIFE on),
the chosen persona's, its stage moving as it is read and set only for the persona the page showed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from himari.bootstrap.application import Application, LifeChanged
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import NO_PERSONA, BondStage, BondTag
from himari.prompt.api import BOND_RULE, BOND_STAGE_WORDS, state_data
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")


@pytest.mark.parametrize("life", [True, False])
def test_a_turn_is_told_the_chosen_personas_bond_with_the_life(tmp_path: Path, life: bool) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응."),))] * 2)
        app = await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None, life=life)
        try:
            lucy = await app.data.create_persona(ME, "Lucy", "이름: 루시.")
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            await app.data.add_bond(ME, lucy.persona_id, BondTag.NAME, "사용자를 대장이라고 부른다.")
            await app.data.add_bond(ME, NO_PERSONA, BondTag.JOKE, "기본 페르소나의 농담.")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕?")
            await app.runner.wait(scope)
            asked = adapter.prepared_inputs[-1]
            told = state_data(asked.current_state).get("bond")
            assert (BOND_RULE in "\n".join(asked.stable_prefix)) is life
            if not life:
                assert told is None
                return
            assert isinstance(told, dict)
            assert told["stage"] == BOND_STAGE_WORDS[BondStage.FAMILIAR]  # the name moved it as it was read
            assert told["together_days"] == 1
            assert told["entries"] == [{"tag": "name", "text": "사용자를 대장이라고 부른다."}]
            assert "기본 페르소나의 농담." not in json.dumps(told, ensure_ascii=False)  # another persona's
            assert (await app.data.bond_stage(ME, lucy.persona_id)).stage is BondStage.FAMILIAR
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_web_changes_the_bond_only_of_the_persona_it_showed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await Application.open(tmp_path / "data", ScriptedAdapter(), model_id="scripted-model", sandbox=False,
                                     cleanup_seconds=None)
        try:
            added = await app.add_bond(ME, None, BondTag.DATE, "사용자 생일", "03-02")
            chosen, bond, episodes = await app.bond(ME)
            assert chosen is None and bond.entries == (added,) and added.persona_id == NO_PERSONA and episodes == ()
            assert bond.together_days == 0 and bond.met_on is None  # no conversation yet
            close = await app.set_bond_stage(ME, None, BondStage.CLOSE, 0)
            assert (close.stage, close.reason, close.revision) == (BondStage.CLOSE, "user", 1)
            lucy = await app.data.create_persona(ME, "Lucy", "")
            await app.data.choose_persona(ME, lucy.persona_id, 0)
            with pytest.raises(LifeChanged):
                await app.add_bond(ME, None, BondTag.JOKE, "lost", None)
            with pytest.raises(LifeChanged):
                await app.set_bond_stage(ME, None, BondStage.NEW, 1)
            _, bond, _ = await app.bond(ME)
            assert bond.entries == () and bond.standing.stage is BondStage.NEW  # Lucy's own
        finally:
            await app.close()

    asyncio.run(scenario())
