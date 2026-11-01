"""User instructions in the Application (V3-P69 §4, §5): read once when a
turn starts, kept for that turn, each Conversation and principal its own."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from himari.prompt.system import BASE_INSTRUCTIONS
from himari.bootstrap.application import Application
from himari.common import CallId, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope
from himari.prompt.api import USER_INSTRUCTIONS_HEADER, ModelInput, ToolGroupTurn
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest, NativeReceived, NormalizedFinal, OutputSink, RawCapture
from himari.provider.api import NormalizedModelOutput, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter, encode_output

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")


class Reactive(ScriptedAdapter):
    """A file write after a user message, then an answer: the same for every
    Conversation, so turns running at once need no shared script. ``hold``
    runs before each answer is returned."""

    def __init__(self) -> None:
        super().__init__()
        self.outputs: dict[str, NormalizedModelOutput] = {}
        self.hold: Callable[[ModelInput], Awaitable[None]] | None = None
        self.inputs: dict[str, ModelInput] = {}

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        request = super().prepare_native_request(model_input)
        after_tools = isinstance(model_input.conversation[-1], ToolGroupTurn)
        count = len(self.prepared_inputs)
        self.outputs[request.digest] = NormalizedFinal((TextBlock("done"),)) if after_tools else NormalizedToolCalls(
            (ToolCall(CallId("c0"), 0, ToolId("inside/write_file"), {"path": f"{count}.txt", "content": "x"}),))
        self.inputs[request.digest] = model_input
        return request

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        self.sent.append(request)
        if self.hold is not None:
            await self.hold(self.inputs[request.digest])
        return NativeReceived(encode_output(self.outputs[request.digest]), "application/json")


def section_of(model_input: ModelInput) -> str | None:
    found = [part for part in model_input.stable_prefix if part.startswith(USER_INSTRUCTIONS_HEADER)]
    assert len(found) <= 1
    return found[0] if found else None


def text_in(model_input: ModelInput) -> str | None:
    section = section_of(model_input)
    return None if section is None else section.split("<user_instructions>\n", 1)[1].rsplit("\n</user", 1)[0]


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


def test_a_turn_keeps_the_text_it_started_with_and_the_next_turn_reads_the_new_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = Reactive()
        app = await opened(tmp_path, adapter)
        try:
            await app.set_instructions(ALICE, "Answer in Korean.", 0)
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            changed = False

            async def change_once(model_input: ModelInput) -> None:  # between the turn's two requests
                nonlocal changed
                if not changed:
                    changed = True
                    await app.set_instructions(ALICE, "Answer in English & be <brief>.", 1)

            adapter.hold = change_once
            await app.send(scope, "first")
            await app.runner.wait(scope)
            assert [text_in(i) for i in adapter.prepared_inputs] == ["Answer in Korean."] * 2  # the whole turn
            await app.send(scope, "second")
            await app.runner.wait(scope)
            assert text_in(adapter.prepared_inputs[-1]) == "Answer in English &amp; be &lt;brief&gt;."
            # After the operator's instructions and notes, before the server notes: last in the prefix here.
            prefix = adapter.prepared_inputs[-1].stable_prefix
            assert prefix[0] == BASE_INSTRUCTIONS[0] and prefix[-1] == section_of(
                adapter.prepared_inputs[-1])
            assert (await app.instructions(ALICE)).text == "Answer in English & be <brief>."  # stored as written
        finally:
            await app.close()

    asyncio.run(scenario())


def test_conversations_running_at_once_each_keep_their_turns_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = Reactive()
        app = await opened(tmp_path, adapter)
        try:
            await app.set_instructions(ALICE, "old", 0)
            first = TrustedScope(ALICE, await app.create_conversation(ALICE))
            second = TrustedScope(ALICE, await app.create_conversation(ALICE))
            started: set[str | None] = set()
            both = asyncio.Event()

            async def hold(model_input: ModelInput) -> None:
                # Both turns have started (each read the text) before it changes; then both go on.
                started.add(model_input.request_options.session_key)
                if len(started) == 2 and not both.is_set():
                    await app.set_instructions(ALICE, "new", 1)
                    both.set()
                await both.wait()

            adapter.hold = hold
            await app.send(first, "one")
            await app.send(second, "two")
            await asyncio.wait_for(asyncio.gather(app.runner.wait(first), app.runner.wait(second)), 10)
            assert len(adapter.prepared_inputs) == 4 and {text_in(i) for i in adapter.prepared_inputs} == {"old"}
            adapter.hold = None
            await app.send(first, "three")
            await app.runner.wait(first)
            assert text_in(adapter.prepared_inputs[-1]) == "new"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_each_principal_gets_their_own_and_none_adds_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = Reactive()
        app = await opened(tmp_path, adapter)
        try:
            await app.set_instructions(ALICE, "Alice's rules.", 0)
            bob = TrustedScope(BOB, await app.create_conversation(BOB))
            await app.send(bob, "hi")
            await app.runner.wait(bob)
            assert all(section_of(i) is None for i in adapter.prepared_inputs)  # Bob wrote none
            await app.set_instructions(BOB, "Bob's rules.", 0)
            await app.send(bob, "again")
            await app.runner.wait(bob)
            assert text_in(adapter.prepared_inputs[-1]) == "Bob's rules."
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_context_size_counts_todays_text_not_a_running_turns(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await opened(tmp_path, ScriptedAdapter([]), context_tokens=128_000)
        try:
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            before = await app.context_status(scope)
            await app.set_instructions(ALICE, "Keep every answer under three sentences. " * 100, 0)
            after = await app.context_status(scope)
            assert before is not None and after is not None
            assert after.estimated_tokens is not None and before.estimated_tokens is not None
            assert after.estimated_tokens > before.estimated_tokens + 500
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_request_records_what_was_used(tmp_path: Path) -> None:
    # V3-048: the stored request is the one sent, so it shows the text the turn used.
    async def scenario() -> None:
        adapter = Reactive()
        app = await opened(tmp_path, adapter)
        try:
            await app.set_instructions(ALICE, "Recorded.", 0)
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            await app.send(scope, "go")
            await app.runner.wait(scope)
            sent = adapter.sent[0]
            assert b"Recorded." in sent.body and sent.digest == hashlib.sha256(sent.body).hexdigest()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_two_principals_running_at_once_each_get_their_own(tmp_path: Path) -> None:
    # PR #67 review C2: a snapshot shared by every Conversation would hand one principal's text to the other.
    from himari.control.driver import session_key

    async def scenario() -> None:
        adapter = Reactive()
        app = await opened(tmp_path, adapter)
        try:
            loader = app.runtime.driver._loader  # the server's shared loader state never changes
            shared = loader._instructions  # type: ignore[attr-defined]
            await app.set_instructions(ALICE, "Alice's rules.", 0)
            await app.set_instructions(BOB, "Bob's rules.", 0)
            alice = TrustedScope(ALICE, await app.create_conversation(ALICE))
            bob = TrustedScope(BOB, await app.create_conversation(BOB))
            owner = {session_key(alice): "Alice's rules.", session_key(bob): "Bob's rules."}
            seen: list[tuple[str | None, str | None]] = []
            made: dict[str | None, int] = {key: 0 for key in owner}
            step = asyncio.Condition()

            async def hold(model_input: ModelInput) -> None:
                # Lockstep: no request is answered until the other turn has made as many, so each turn
                # is still running whenever the other one reads its text.
                key = model_input.request_options.session_key
                seen.append((key, text_in(model_input)))
                async with step:
                    made[key] += 1
                    step.notify_all()
                    await step.wait_for(lambda: min(made.values()) >= made[key])

            adapter.hold = hold
            await app.send(alice, "one")
            await app.send(bob, "two")
            await asyncio.wait_for(asyncio.gather(app.runner.wait(alice), app.runner.wait(bob)), 10)
            assert len(seen) == 4 and all(text == owner[key] for key, text in seen)  # type: ignore[index]
            assert loader._instructions is shared  # type: ignore[attr-defined]
        finally:
            await app.close(grace_seconds=0)  # a failure here ends at once, not after the shutdown grace

    asyncio.run(scenario())


def test_in_the_provider_request_the_server_notes_come_before_the_users_own_text() -> None:
    # V3-P89 review B1 (reversing PR #67 review C3's order): the Prompt module decides the final order —
    # HIMARI's text, the tool servers' notes, then the user's persona and instructions last; the
    # Provider joins it as it is and adds nothing.
    import json
    from dataclasses import replace

    from himari.data.api import PersistedStateBasis
    from himari.prompt.api import NOTES_HEADER, PERSONA_HEADER, PromptInputs, RequestOptions, SourceNote, SystemSpec
    from himari.prompt.composer import Composer
    from himari.provider.openrouter import OpenRouterAdapter

    inputs = PromptInputs(instructions=("You are HIMARI.",), tool_catalog=(), events=(),
                          tool_executions=(), snapshot=None, state_basis=PersistedStateBasis(0, 0, (), (), ()),
                          observations=(), request_options=RequestOptions("m"), trigger_refs=(),
                          source_notes=(SourceNote("calliope", "system", "Calliope's notes."),),
                          system=SystemSpec(companion=True, memory=True))
    model_input = Composer().compose(replace(inputs, user_instructions="Answer in Korean.",
                                             companion_persona="Call me 선생님."))
    body = json.loads(OpenRouterAdapter("sk-test").prepare_native_request(model_input).body)
    system = body["messages"][0]
    assert system["role"] == "system"
    content = system["content"]
    order = [content.index(part) for part in ("You are HIMARI.", "# What instructs you", "# Memory", "# Companion",
                                              NOTES_HEADER, "Calliope's notes.", PERSONA_HEADER, "Call me 선생님.",
                                              USER_INSTRUCTIONS_HEADER, "Answer in Korean.")]
    assert order == sorted(order)
    assert content.count(NOTES_HEADER) == 1 and content.endswith("</user_instructions>")


def test_a_compaction_request_does_not_carry_them(tmp_path: Path) -> None:
    # PR #67 review C3: the summary request has its own instructions; the user's are not in it.
    from himari.prompt.compaction import COMPACTION_INSTRUCTIONS

    async def scenario() -> None:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("SUMMARY"),))] * 12)
        app = await opened(tmp_path, adapter, context_tokens=3800,  # sized without the search Tools, with V3-P89's system text
                           conversation_search=False)
        try:
            await app.set_instructions(ALICE, "ZQX rule.", 0)
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            for text in ("가" * 550, "가" * 550, "가" * 550, "next"):
                await app.send(scope, text)
                await app.runner.wait(scope)
            pairs = list(zip(adapter.prepared_inputs, adapter.sent, strict=True))
            summaries = [sent for model_input, sent in pairs if model_input.stable_prefix == (COMPACTION_INSTRUCTIONS,)]
            turns = [sent for model_input, sent in pairs if model_input.stable_prefix != (COMPACTION_INSTRUCTIONS,)]
            assert summaries and turns  # it did compact
            assert all(b"ZQX rule." not in sent.body for sent in summaries)
            assert all(b"ZQX rule." in sent.body for sent in turns)
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_companion_persona_is_told_only_with_the_companion_on(tmp_path: Path) -> None:
    """V3-P89 §4 (owner A): read with the instructions at the turn's start, in every conversation
    while the companion is on; kept but not told while it is off."""
    from himari.data.api import InstructionScope
    from himari.prompt.api import PERSONA_HEADER

    async def scenario(companion: bool) -> str:
        adapter = ScriptedAdapter([NormalizedFinal((TextBlock("응"),))])
        root = tmp_path / str(companion)
        root.mkdir()
        app = await opened(root, adapter, companion=companion)
        try:
            saved = await app.set_instructions(ALICE, "Call me 선생님.", 0, InstructionScope.COMPANION_PERSONA)
            assert saved.revision == 1 and (await app.instructions(ALICE)).text == ""  # the instructions apart
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            await app.send(scope, "hi")
            await app.runner.wait(scope)
            return "\n".join(adapter.prepared_inputs[0].stable_prefix)
        finally:
            await app.close()

    told = asyncio.run(scenario(True))
    assert PERSONA_HEADER in told and "Call me 선생님." in told and "# Companion" in told
    off = asyncio.run(scenario(False))
    assert "Call me 선생님." not in off and "# Companion" not in off
