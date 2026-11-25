"""HIMARI's life runs (character-life v0.1 §3, character-mind v0.1 §4.1, §4.3, §5.3): the day's review is HIMARI's
own self run — a turn's system text and runtime state, no history, no Tools, on the latest conversation's model when
it is checked for free wakes, else the memory model — once a date after its time, for the persona chosen now, three
tries a date, within the life's own daily budget; then the memory model settles the facts its diary holds."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    NO_PERSONA,
    BondStage,
    InstructionScope,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifeWriteStart,
    LifeWriteStatus,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    UserMessage,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.life.api import ATTEMPTS_PER_DAY, LifeSettings, MindModel, WriteOutcome
from himari.life.review import REVIEW_MAX_OUTPUT_TOKENS, REVIEW_TASK
from himari.life.settle import SETTLE_INSTRUCTIONS
from himari.life.worker import START_DELAY, LifeWriter
from himari.prompt.api import LIFE_RULE, PERSONA_RULE, THINK_RULE, ModelInput, SystemSpec
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE
from himari.provider.api import NativeNoResponse, NormalizedFinal
from himari.provider.scripted import Measured, ScriptedAdapter
from himari.world.api import TopicSource, WeatherDay, WeatherSource

ME = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 22:30 in Seoul
SETTINGS = LifeSettings("m", time_zone="Asia/Seoul")
THOUGHT = "오늘은 그림이 잘 그려졌다. 기분이 좋다."


def final(text: str) -> NormalizedFinal:
    return NormalizedFinal((TextBlock(text),))


def day_answer(diary: str = "오늘은 그림을 그렸다. 저녁엔 차를 마셨다. 일찍 잤다.", *, thought: str = THOUGHT,
               **fields: object) -> NormalizedFinal:
    """§4.3: the review's answer — a thought, then one JSON object (``facts`` in it are not read, §5.3)."""
    body = json.dumps({"diary": diary, "plans": [{"date": "2026-11-27", "text": "전시를 보러 간다."}],
                       "facts": ["수채화를 배우기 시작했다."], **fields}, ensure_ascii=False)
    return final(f"[[think]]{thought}[[/think]]\n{body}")


def settled(*facts: str) -> NormalizedFinal:
    return final(json.dumps({"facts": list(facts)}, ensure_ascii=False))


class Link:
    """§4.1: a MindLink that names a model, or says why not."""

    def __init__(self, chosen: MindModel) -> None:
        self.chosen = chosen

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        return self.chosen


def setup(*script: object, settings: LifeSettings = SETTINGS, clock: object = None,
          weather: WeatherSource | None = None, topics: TopicSource | None = None,
          link: Link | None = None) -> tuple[InMemoryStore, InMemoryData, ScriptedAdapter, LifeWriter]:
    store = InMemoryStore()
    data = InMemoryData(store)
    provider = ScriptedAdapter(memory_passes=True)
    provider.extend(script)  # type: ignore[arg-type]
    writer = LifeWriter(data, provider, settings, clock=clock if callable(clock) else (lambda: NOW),
                        weather=weather, topics=topics, composer=Composer(), instructions=COMPANION_BASE,
                        system=SystemSpec(companion=True, memory=True, life=True), link=link)
    return store, data, provider, writer


class Weather:
    """character-world §2: a WeatherSource with fixed days."""

    def __init__(self, *days: WeatherDay) -> None:
        self.days = {d.day: d for d in days}

    def weather_on(self, day: date) -> WeatherDay | None:
        return self.days.get(day)


class Topics:
    """character-world §3: a TopicSource that counts the dates asked."""

    def __init__(self, *titles: str) -> None:
        self.titles = titles
        self.asked: list[date] = []

    async def topics(self, day: date) -> tuple[str, ...]:
        self.asked.append(day)
        return self.titles


def asked_of(provider: ScriptedAdapter, index: int = -1) -> ModelInput:
    return provider.prepared_inputs[index]


def told(provider: ScriptedAdapter, index: int = -1) -> dict[str, Any]:
    """A call's data (the last block of its one user turn), as JSON."""
    found: dict[str, Any] = json.loads(asked_of(provider, index).conversation[0].blocks[-1].text)  # type: ignore[union-attr]
    return found


def system(provider: ScriptedAdapter, index: int = -1) -> str:
    return "\n\n".join(asked_of(provider, index).stable_prefix)


def test_the_review_is_a_self_run_with_a_turns_system_text_and_state_and_the_days_data() -> None:
    """§4.1: the same system text (the persona, its rules, thinking first) and runtime state (the notes, the
    life) as a turn; no history and no Tools; the review's task and the day's data as the one user turn."""
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 9_000, 300))
        lucy = await data.create_persona(ME, "Lucy", "이름: 루시. 원래 나이트 시티에 살았다.")
        await data.choose_persona(ME, lucy.persona_id, 0)
        await data.add_life_fact(ME, lucy.persona_id, "고양이를 무서워한다.")
        await data.write_instructions(ME, InstructionScope.USER, "존댓말은 쓰지 마.", 0)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        asked = asked_of(provider)
        prefix = system(provider)
        for part in (PERSONA_RULE, THINK_RULE, LIFE_RULE, "나이트 시티", "존댓말은 쓰지 마."):
            assert part in prefix
        assert asked.tool_catalog == () and len(asked.conversation) == 1
        task, _ = asked.conversation[0].blocks  # type: ignore[union-attr]
        assert task.text == REVIEW_TASK  # type: ignore[union-attr]
        assert told(provider)["day"] == "2026-11-25" and "persona" not in told(provider)
        state = asked.current_state
        assert state.life is not None and [e.text for e in state.life] == ["고양이를 무서워한다."]
        assert state.memories == () and state.bond is not None
        options = asked.request_options
        assert (options.model_id, options.max_output_tokens) == ("m", REVIEW_MAX_OUTPUT_TOKENS)
        (record,) = await data.list_life_writes(ME)
        assert (record.job, record.status, record.model_id, record.model_reason, record.thought,
                record.charged_tokens) == (LifeJob.REVIEW, LifeWriteStatus.APPLIED, "m", "the memory model", THOUGHT,
                                           9_300)
        life = await data.list_life(ME, lucy.persona_id)
        assert [(e.kind, e.day, e.origin) for e in life] == [  # no fact from the review: settling is apart
            (LifeKind.FACT, None, LifeOrigin.USER), (LifeKind.PLAN, "2026-11-27", LifeOrigin.WRITER),
            (LifeKind.DIARY, "2026-11-25", LifeOrigin.WRITER)]
        assert await writer.write(ME) is WriteOutcome.NOTHING  # the date has its diary

    asyncio.run(scenario())


def test_the_review_runs_on_the_model_the_link_chooses_and_records_why() -> None:
    async def scenario() -> None:
        chosen = Link(MindModel("openai/gpt-6", "low", "the latest conversation's model"))
        _, data, provider, writer = setup(Measured(day_answer(), 900, 90), link=chosen)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        options = asked_of(provider).request_options
        assert (options.model_id, options.reasoning_effort) == ("openai/gpt-6", "low")
        (record,) = await data.list_life_writes(ME)
        assert (record.model_id, record.model_reason) == ("openai/gpt-6", "the latest conversation's model")
        refused = Link(MindModel(None, reason="the memory model (x is not checked for free wakes)"))
        _, data, provider, writer = setup(Measured(day_answer(), 900, 90), link=refused)
        assert (await writer.mind_model(ME)).model_id == "m"
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        (record,) = await data.list_life_writes(ME)
        assert (record.model_id, record.model_reason) == ("m", "the memory model (x is not checked for free wakes)")

    asyncio.run(scenario())


def test_the_review_is_told_the_weather_the_days_topics_and_the_persona_as_far_as_the_bond_has_come() -> None:
    """character-world §2, §3, §5: the weather of the date written and the topics (asked only when a call
    is made, once a date); the persona's hidden lines wait for the stage as stored."""
    async def scenario() -> None:
        moments = [NOW]
        rainy = WeatherDay(date(2026, 11, 25), "light rain", 3.2, 9.4, 2.1)
        topics = Topics("첫 눈 소식", "새 영화 개봉")
        _, data, provider, writer = setup(
            Measured(day_answer(arc={"move": "start", "title": "그림 연습", "goal": "풍경화 한 장"}), 900, 90),
            Measured(day_answer("다음 날."), 900, 90), clock=lambda: moments[0], weather=Weather(rainy),
            topics=topics)
        lucy = await data.create_persona(ME, "Lucy", "이름: 루시\n[familiar] 고양이를 키웠었다.\n- [close] 사실 해커였다.")
        await data.choose_persona(ME, lucy.persona_id, 0)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        first = told(provider)
        assert "고양이를 키웠었다" not in system(provider) and "해커" not in system(provider)  # new: both hidden
        assert (first["weather"], first["topics"]) == ("light rain, 3 to 9°C, rain 2.1 mm", ["첫 눈 소식", "새 영화 개봉"])
        assert first["arcs"] == []
        (arc,) = [e for e in await data.list_life(ME, lucy.persona_id) if e.kind is LifeKind.ARC]
        assert (arc.title, arc.text, arc.day) == ("그림 연습", "풍경화 한 장", "2026-11-25")
        assert await writer.write(ME) is WriteOutcome.NOTHING
        assert topics.asked == [date(2026, 11, 25)]  # no call, no search
        await data.set_bond_stage(ME, lucy.persona_id, BondStage.FAMILIAR, expected_revision=0)
        moments[0] = NOW + timedelta(days=1)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        second = told(provider)
        assert "\n고양이를 키웠었다." in system(provider)  # familiar now: its line, without the mark
        assert "해커" not in system(provider)
        assert "weather" not in second  # none read for that date
        assert second["arcs"] == [{"id": arc.entry_id, "title": "그림 연습", "goal": "풍경화 한 장",
                                   "started": "2026-11-25", "moved_on": ["2026-11-25"]}]
        assert topics.asked == [date(2026, 11, 25), date(2026, 11, 26)]
        assert "persona_makes" not in second  # written as one text: no field makes

    asyncio.run(scenario())


def test_the_review_is_told_the_personas_field_makes() -> None:
    """character-persona-fields §4: a persona written in fields gives its makes apart (persona_makes)."""
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 900, 90))
        lucy = await data.create_persona(ME, "Lucy", "", {"name": "루시",
                                                          "makes": "블랙홀 무기 설정집\n[close] 몰래 쓰는 일기"})
        await data.choose_persona(ME, lucy.persona_id, 0)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert "이름: 루시\n하는 일과 만드는 것:\n블랙홀 무기 설정집" in system(provider)
        assert told(provider)["persona_makes"] == "블랙홀 무기 설정집"  # its [close] line waits for the stage

    asyncio.run(scenario())


def test_a_bad_arc_move_is_left_out_and_the_day_is_written() -> None:
    """character-world §4: out of shape, or not one the store can make, the move alone is dropped."""
    async def scenario() -> None:
        _, data, _, writer = setup(Measured(day_answer(arc={"move": "advance", "id": 42}), 900, 90))
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        life = await data.list_life(ME, NO_PERSONA)
        assert [e.kind for e in life if e.kind is LifeKind.ARC] == []
        (diary,) = [e for e in life if e.kind is LifeKind.DIARY]
        assert diary.arc_id is None

    asyncio.run(scenario())


def test_a_day_is_written_once_for_the_persona_chosen_now_and_its_facts_settled_once() -> None:
    """§5.3: once the review applied, the memory model settles the facts its diary holds, once a date."""
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 1_500, 300),
                                          Measured(settled("수채화를 배우기 시작했다.", "고양이를 무서워한다."), 500, 50),
                                          Measured(day_answer("다른 날."), 900, 90))
        lucy = await data.create_persona(ME, "Lucy", "이름: 루시.")
        await data.choose_persona(ME, lucy.persona_id, 0)
        known = await data.add_life_fact(ME, lucy.persona_id, "고양이를 무서워한다.")
        assert await writer.settle(ME) is WriteOutcome.NOTHING  # no review yet
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert await writer.settle(ME) is WriteOutcome.WRITTEN
        asked = asked_of(provider)
        assert asked.stable_prefix == (SETTLE_INSTRUCTIONS,) and asked.request_options.model_id == "m"
        assert told(provider) == {"diary": {"date": "2026-11-25", "weekday": "Wednesday",
                                            "text": "오늘은 그림을 그렸다. 저녁엔 차를 마셨다. 일찍 잤다."},
                                  "facts": ["고양이를 무서워한다."]}
        settle_write, review_write = await data.list_life_writes(ME)
        assert (settle_write.job, settle_write.status, settle_write.day) == (LifeJob.SETTLE, LifeWriteStatus.APPLIED,
                                                                             "2026-11-25")
        facts = [e for e in await data.list_life(ME, lucy.persona_id) if e.kind is LifeKind.FACT]
        assert [(e.text, e.origin, e.source) for e in facts] == [  # the known one is not kept twice
            (known.text, LifeOrigin.USER, None), ("수채화를 배우기 시작했다.", LifeOrigin.WRITER, settle_write.write_id)]
        assert await writer.settle(ME) is WriteOutcome.NOTHING  # once a date
        assert review_write.job is LifeJob.REVIEW
        # another persona chosen: its own date to write; none chosen: persona 0's
        await data.choose_persona(ME, None, 1)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [e.text for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY] == ["다른 날."]

    asyncio.run(scenario())


def test_settling_is_tried_three_times_a_date() -> None:
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 900, 90), final("no json"),
                                          settled("a", "b", "c", "d"), NativeNoResponse("http_500"))
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert [await writer.settle(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * ATTEMPTS_PER_DAY
        assert await writer.settle(ME) is WriteOutcome.GAVE_UP
        assert len(provider.sent) == 1 + ATTEMPTS_PER_DAY

    asyncio.run(scenario())


def test_the_review_reads_the_thoughts_kept_today_and_the_expired_ones_retire_after_it() -> None:
    """§5.2: the review is told today's kept thoughts; those expired by now retire once it applied."""
    async def scenario() -> None:
        _, data, provider, writer = setup(Measured(day_answer(), 900, 90))
        for text, at in (("피곤해 보였다.", NOW - timedelta(hours=3)), ("오래된 생각.", NOW - timedelta(days=4))):
            await data.apply_thought_changes(ME, NO_PERSONA, [ThoughtChange(
                ThoughtOp.KEEP, text, ThoughtAbout.USER, sequences=(2,))], conversation=ConversationId("c"),
                source="mpass_1", now=at)
        assert await writer.write(ME) is WriteOutcome.WRITTEN
        assert told(provider)["kept_thoughts"] == [{"about": "user", "text": "피곤해 보였다.", "at": "19:30"}]
        kept = [e.text for e in await data.list_life(ME, NO_PERSONA, now=NOW - timedelta(days=5))
                if e.kind is LifeKind.THOUGHT]
        assert kept == ["피곤해 보였다."]  # the expired one retired, even as read before it expired

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
        _, data, provider, writer = setup(final("[[think]]생각만 하고 말았다.[[/think]] not json"),
                                          NativeNoResponse("http_500"),
                                          day_answer(plans=[{"date": "2026-12-25", "text": "too far"}]))
        assert [await writer.write(ME) for _ in range(ATTEMPTS_PER_DAY)] == [WriteOutcome.FAILED] * ATTEMPTS_PER_DAY
        assert await writer.write(ME) is WriteOutcome.GAVE_UP  # no fourth call
        assert len(provider.sent) == ATTEMPTS_PER_DAY
        records = await data.list_life_writes(ME)
        assert [r.status for r in records] == [LifeWriteStatus.FAILED] * 3
        assert records[0].error is not None and "plan" in records[0].error
        assert records[1].error == "http_500"
        assert records[2].error is not None and records[2].error.startswith("malformed answer")
        assert records[2].thought == "생각만 하고 말았다."  # kept even when the rest is thrown away
        assert await data.list_life(ME, NO_PERSONA) == ()

    asyncio.run(scenario())


def test_the_lifes_own_daily_budget_defers_it() -> None:
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

        store, data, provider, writer = setup(day_answer(plans=[]), settled(), clock=clock,
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
        assert len(provider.sent) == 2  # alice only (bob sent nothing): the review, then its facts settled
        assert [e.kind for e in await data.list_life(ME, NO_PERSONA) if e.kind is LifeKind.DIARY] == [LifeKind.DIARY]
        await writer.run()
        assert len(provider.sent) == 2  # written and settled

    asyncio.run(scenario())
