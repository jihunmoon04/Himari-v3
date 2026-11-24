"""Live check of the bond (character-bond v0.1 §3): opt-in, ``HIMARI_LIVE=1`` plus the key. One memory pass with
``self`` and ``bond`` on the memory passes' model (``HIMARI_LIVE_BOND_MODEL``, default the one the operator
configuration names) over a short conversation where the user settles a nickname and a promise, to see that the
JSON round-trips with a real model: ``bond`` comes back in shape, resting on the user's messages. With
``HIMARI_LIVE_BOND_OUT`` set to a file, the answer and its usage are appended as a JSON line. Machine checks are only
"is it the shape asked for" (character-architecture §10)."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from himari.bootstrap import read_env_file
from himari.common import ConversationId, EventId, TextBlock
from himari.data.api import (
    AssistantFinal,
    BondTag,
    ConversationEvent,
    EventKind,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PassKind,
    UserMessage,
)
from himari.memory import passes
from himari.prompt.api import RequestOptions
from himari.provider.api import NativeReceived, NormalizedFinal, ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("HIMARI_LIVE_BOND_MODEL", "deepseek/deepseek-v4.1-flash-1017")
OUT = os.environ.get("HIMARI_LIVE_BOND_OUT")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # Wednesday 22:30 in Seoul
CONV = ConversationId("conv_live")

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")

FACTS = (LifeEntry(1, 1, LifeKind.FACT, None, "고양이를 무서워한다.", LifeOrigin.USER, 1, NOW, NOW),)
BOND = (LifeEntry(2, 1, LifeKind.BOND, None, "감자 사건", LifeOrigin.CONVERSATION, 1, NOW, NOW, tag=BondTag.JOKE),)


def adapter() -> OpenRouterAdapter:
    known = KNOWN_CAPABILITIES.get(MODEL, ProviderCapabilities(MODEL))
    return OpenRouterAdapter(KEY, capabilities={MODEL: replace(known, memory_passes=True)})


def effort() -> str | None:
    known = KNOWN_CAPABILITIES.get(MODEL, ProviderCapabilities(MODEL))
    return "low" if "low" in known.reasoning_efforts else None


def call(asked: Any) -> tuple[str, dict[str, Any]]:
    """The answer's text and the reported usage."""
    provider = adapter()
    outcome = asyncio.run(provider.invoke_prepared(provider.prepare_native_request(asked)))
    assert isinstance(outcome, NativeReceived), outcome
    output = provider.normalize(outcome)
    assert isinstance(output, NormalizedFinal)
    usage = outcome.usage
    return (passes.answer_text(output.blocks),
            {} if usage is None else {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})


def test_a_memory_pass_keeps_the_nickname_and_the_promise_the_two_settled() -> None:
    turns = (("앞으로 나를 대장이라고 불러 줘. 그게 좋아.",
              "알았어, 대장. 그럼 나는 뭐라고 불러 줄 거야?"),
             ("너는 그냥 루시. 그리고 우리 주말마다 같이 본 영화 얘기 하자. 약속?",
              "약속. 주말마다 영화 얘기, 잊지 않을게."),
             ("좋아. 나 이번 주말엔 회사 워크숍 가.",
              "워크숍 잘 다녀와, 대장. 다녀와서 영화 얘기 해."))
    events: list[ConversationEvent] = []
    for index, (said, answered) in enumerate(turns):
        for seq, payload in ((2 * index + 1, UserMessage((TextBlock(said),))),
                             (2 * index + 2, AssistantFinal((TextBlock(answered),)))):
            kind = EventKind.USER_MESSAGE if isinstance(payload, UserMessage) else EventKind.ASSISTANT_FINAL
            events.append(ConversationEvent(EventId(f"e{seq}"), CONV, seq, kind, payload, 1, NOW))
    part = passes.next_part(events, 0)
    assert part is not None
    options = RequestOptions(MODEL, effort(), provider_key=adapter().provider_key,
                             max_output_tokens=passes.PASS_MAX_OUTPUT_TOKENS)
    shown = passes.shown_bond(BOND)
    data = passes.request_data(part, [], None, now=NOW, zone="Asia/Seoul", kind=PassKind.LIVE,
                               facts=passes.shown_facts(FACTS), bond=shown)
    text, usage = call(passes.model_input(data, options, life=True, bond=True))
    answer = passes.parse_answer(text)  # the notes, the episode and self as ever, bond beside them
    episode = passes.plan_episode(answer, part, None, kind=PassKind.LIVE)
    changes = passes.plan_bond(answer, part, shown, episode=episode is not None)
    users = {m.sequence for m in part.messages if m.role == "user"}
    line = json.dumps({"kind": "memory_pass_bond", "model": MODEL, "usage": usage,
                       "bond": [(c.op.value, None if c.tag is None else c.tag.value, c.text, list(c.sequences))
                                for c in changes],
                       "raw_bond": answer.get("bond"), "answer": text}, ensure_ascii=False)
    print(line)
    if OUT:
        with open(OUT, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    assert isinstance(answer.get("bond"), list) and answer["bond"]  # it came back, and in shape: plan_bond kept some
    assert changes and all(set(c.sequences) & users for c in changes)  # each rests on a user message (B2)
    tags = {c.tag for c in changes if c.op is LifeChangeOp.ADD}
    assert BondTag.NAME in tags and BondTag.PROMISE in tags  # the nickname and the promise
    assert not any(c.tag is BondTag.JOKE and c.text == "감자 사건" for c in changes)  # a saved one is not repeated
