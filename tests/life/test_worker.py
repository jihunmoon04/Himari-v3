"""The story writer runs (character-life v0.1 §3): once a date, after its time, for the persona chosen now,
three tries a date, within its own daily budget."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time, timedelta

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import NO_PERSONA, LifeKind, LifeOrigin, LifeWriteStart, LifeWriteStatus, UserMessage
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life.api import ATTEMPTS_PER_DAY, LifeSettings, WriteOutcome
from himari.life.worker import START_DELAY, LifeWriter
from himari.life.writer import WRITER_INSTRUCTIONS, WRITER_MAX_OUTPUT_TOKENS
from himari.provider.api import NativeNoResponse, NormalizedFinal
from himari.provider.scripted import Measured, ScriptedAdapter

ME = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
SETTINGS = LifeSettings("m", time_zone="Asia/Seoul")


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def day_answer(diary: str = "오늘은 그림을 그렸다. 저녁엔 차를 마셨다. 일찍 잤다.", **fields: object) -> NormalizedFinal:
    return final(json.dumps({"diary": diary, "plans": [{"date": "2026-11-27", "text": "전시를 보러 간다."}],
                             "facts": ["수채화를 배우기 시작했다."], **fields}, ensure_ascii=False))


def setup(*script: object, settings: LifeSettings = SETTINGS,
          clock: object = None) -> tuple[InMemoryStore, InMemoryData, ScriptedAdapter, LifeWriter]:
    store = InMemoryStore()
    data = InMemoryData(store)
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    writer = LifeWriter(data, provider, settings, clock=clock if callable(clock) else (lambda: NOW))
    return store, data, provider, writer


def test_a_day_is_written_once_for_the_persona_chosen_now() -> None:
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 1_500, 300), Measured(day_answer("다른 날."), 900, 90))
        lucy = await data.create_persona(ME, "Lucy", "이름: 루시. 원래 나이트 시티에 살았다.")
        await data.choose_persona(ME, lucy.persona_id, 0)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        life = await data.list_life(ME, lucy.persona_id)
        assert [(e.kind, e.day, e.origin) for e in life] == [
            (LifeKind.FACT, None, LifeOrigin.WRITER), (LifeKind.PLAN, "2026-11-27", LifeOrigin.WRITER),
            (LifeKind.DIARY, "2026-11-25", LifeOrigin.WRITER)]
        assert await writer.write(ME) is WriteOutcome.NOTHING  # the date has its diary
        (asked,) = provider.prepared_inputs
        assert asked.stable_prefix == (WRITER_INSTRUCTIONS,)
        assert asked.request_options.max_output_tokens == WRITER_MAX_OUTPUT_TOKENS
        assert "나이트 시티" in asked.conversation[0].blocks[0].text  # type: ignore[union-attr]
        (record,) = await data.list_life_writes(ME)
        assert (record.status, record.persona_id, record.day, record.charged_tokens) == (
            LifeWriteStatus.APPLIED, lucy.persona_id, "2026-11-25", 1_800)
        # another persona chosen: its own date to write; none chosen: persona 0's
        await data.choose_persona(ME, None, 1)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [e.text for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY] == ["다른 날."]

    asyncio.run(scenario())


def test_the_days_mood_is_kept_at_the_days_end_or_now_if_that_is_earlier() -> None:
    """character-state §2: written tonight, the mood is as of now; a day written late is as of its end."""
    async def scenario() -> None:
        def answered() -> NormalizedFinal:
            return day_answer(mood={"name": "happy", "reason": "전시가 좋았다"},
                              plans=[{"date": "2026-11-27", "time": "14:00", "text": "전시를 보러 간다."}])

        _, data, _, writer = setup(Measured(answered(), 900, 90))
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        kept = await data.read_mood(ME, NO_PERSONA)
        assert kept is not None and (kept.mood, kept.reason, kept.set_at) == ("happy", "전시가 좋았다", NOW)
        assert [e.time_of_day for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.PLAN] == ["14:00"]
        morning = NOW + timedelta(hours=10)  # 08:30 the next day, before the write time: the 25th is written
        _, data, _, writer = setup(Measured(answered(), 900, 90), clock=lambda: morning)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        kept = await data.read_mood(ME, NO_PERSONA)
        assert kept is not None and kept.set_at == datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # midnight in Seoul

    asyncio.run(scenario())


def test_before_the_write_time_the_day_before_is_written_and_no_earlier_one() -> None:
    async def scenario() -> None:
        morning = datetime(2026, 11, 25, 1, 0, tzinfo=UTC)  # 10:00 in Seoul
        _, data, _, writer = setup(Measured(day_answer(plans=[{"date": "2026-11-25", "text": "오늘 할 일."}]), 10, 10),
                                   clock=lambda: morning)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [(e.kind, e.day) for e in await data.list_life(ME, NO_PERSONA) if e.kind is not LifeKind.FACT] == [
            (LifeKind.PLAN, "2026-11-25"), (LifeKind.DIARY, "2026-11-24")]
        (record,) = await data.list_life_writes(ME)
        assert record.day == "2026-11-24"

    asyncio.run(scenario())


def test_a_bad_answer_is_thrown_away_and_a_date_is_tried_three_times() -> None:
    async def scenario() -> None:
        _, data, provider, writer = setup(final("not json"), NativeNoResponse("http_500"),
                                          day_answer(plans=[{"date": "2026-12-25", "text": "too far"}]))
        assert [await writer.write(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * ATTEMPTS_PER_DAY
        assert await writer.write(ME) is WriteOutcome.GAVE_UP  # no fourth call
        assert len(provider.sent) == ATTEMPTS_PER_DAY
        records = await data.list_life_writes(ME)
        assert [r.status for r in records] == [LifeWriteStatus.FAILED] * 3
        assert records[0].error is not None and "plan" in records[0].error
        assert records[1].error == "http_500"
        assert records[2].error is not None and records[2].error.startswith("malformed answer")
        assert await data.list_life(ME, NO_PERSONA) == ()

    asyncio.run(scenario())


def test_the_writers_own_daily_budget_defers_it() -> None:
    async def scenario() -> None:
        _, data, provider, writer = setup(day_answer(), settings=LifeSettings("m", daily_tokens=100,
                                                                              time_zone="Asia/Seoul"))
        assert await writer.write(ME) is WriteOutcome.DEFERRED
        assert provider.sent == [] and await data.list_life_writes(ME) == ()

    asyncio.run(scenario())


def test_rounds_start_after_a_delay_and_cover_only_principals_who_talked_lately() -> None:
    async def scenario() -> None:
        offset = timedelta(0)

        def clock() -> datetime:
            return datetime.now(UTC) + offset

        store, data, provider, writer = setup(day_answer(plans=[]), clock=clock,
                                              settings=LifeSettings("m", write_at=time(0, 0), time_zone="Asia/Seoul"))
        for scope in (TrustedScope(ME, ConversationId("c1")), TrustedScope(PrincipalId("bob"), ConversationId("c2"))):
            store.create_conversation(scope)
        await data.append_event(TrustedScope(ME, ConversationId("c1")), UserMessage((TextBlock("안녕"),)))
        left = await data.begin_life_write(ME, LifeWriteStart(NO_PERSONA, "2026-11-01", "scripted", "m", b"{}",
                                                              "application/json", 10))
        await writer.start()
        (cut,) = [w for w in await data.list_life_writes(ME) if w.write_id == left]
        assert cut.status is LifeWriteStatus.INTERRUPTED
        await writer.run()
        assert provider.sent == []  # not before START_DELAY
        offset = START_DELAY + timedelta(seconds=1)
        await writer.run()
        assert len(provider.sent) == 1  # alice only: bob sent nothing
        assert [e.kind for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY] == [LifeKind.DIARY]
        await writer.run()
        assert len(provider.sent) == 1  # written

    asyncio.run(scenario())
