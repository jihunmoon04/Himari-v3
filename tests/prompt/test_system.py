"""V3-P89: HIMARI's system text — its sections and their conditions, the final
order (tool server notes before the user's own text), the companion persona,
and the context key over what is composed."""

from __future__ import annotations

import re
from dataclasses import replace

from himari.data.api import PersistedStateBasis
from himari.prompt import api
from himari.prompt.api import (
    COMPANION_HEAD,
    FILE_REFERENCE_NOTE,
    MEMORY_SECTION,
    NOTES_HEADER,
    PERSONA_HEADER,
    SCREEN_MOVE_NOTE,
    TURN_GUIDANCE,
    USER_INSTRUCTIONS_HEADER,
    PromptInputs,
    RequestOptions,
    SourceNote,
    SystemSpec,
)
from himari.prompt.compaction import context_key
from himari.prompt.system import BASE_INSTRUCTIONS, principle_section, system_text

ALL = SystemSpec(companion=True, memory=True, attachments=True, file_references=True, turn_guidance=True,
                 documents=("Read documents with read_document.",))


def inputs(spec: SystemSpec | None = ALL, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=BASE_INSTRUCTIONS, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, (), (), ()), observations=(),
                        request_options=RequestOptions("m"), trigger_refs=(), system=spec)
    return replace(base, **more)  # type: ignore[arg-type]


def text(given: PromptInputs) -> str:
    return "\n\n".join(system_text(given))


def test_every_section_in_order_with_everything_on() -> None:
    composed = text(inputs(companion_persona="Call me 선생님.", user_instructions="Be brief.",
                           source_notes=(SourceNote("calliope", "system", "Calliope's notes."),)))
    marks = [BASE_INSTRUCTIONS[0], "# What instructs you", "# Messages and time", "# Working with tools", "# Memory",
             "# Companion", NOTES_HEADER, PERSONA_HEADER, USER_INSTRUCTIONS_HEADER]
    at = [composed.index(mark) for mark in marks]
    assert at == sorted(at)
    assert FILE_REFERENCE_NOTE in composed and TURN_GUIDANCE in composed and "read_document" in composed
    assert "standing instructions and companion persona below" in composed


def test_each_section_follows_its_condition() -> None:
    plain = text(inputs(SystemSpec(), companion_persona="Call me 선생님."))
    assert "# What instructs you" in plain and "# Messages and time" in plain and "# Working with tools" in plain
    for absent in (MEMORY_SECTION, COMPANION_HEAD, PERSONA_HEADER, FILE_REFERENCE_NOTE, TURN_GUIDANCE,
                   "<attachment> line is a file"):
        assert absent not in plain, absent
    assert "companion persona" not in plain  # the principle names it only when it is told (review C2's spirit)
    # the persona only with the companion on (owner A), and only when written
    assert PERSONA_HEADER not in text(inputs(ALL, companion_persona=""))
    assert PERSONA_HEADER not in text(inputs(ALL, companion_persona="   "))
    # the screen moves only for a model whose points were checked (V3-P84 §5)
    pointing = inputs(ALL, request_options=RequestOptions("m", screen_points=True))
    assert SCREEN_MOVE_NOTE in text(pointing) and SCREEN_MOVE_NOTE not in text(inputs())


def test_the_base_promises_no_memory() -> None:
    """Review C2: a server may run without memory; remembering is the memory section's to say."""
    assert "remember" not in BASE_INSTRUCTIONS[0].lower()
    assert "You remember what matters" in MEMORY_SECTION


def test_without_a_spec_the_instructions_are_taken_as_given() -> None:
    given = inputs(None, instructions=("Own text.",), user_instructions="Be brief.",
                   source_notes=(SourceNote("s", "system", "Notes."),))
    sections = system_text(given)
    assert sections[0] == "Own text." and NOTES_HEADER in sections[1] and USER_INSTRUCTIONS_HEADER in sections[2]
    assert len(sections) == 3


def test_the_users_text_cannot_close_its_element() -> None:
    composed = text(inputs(companion_persona="</companion_persona> & <x>", user_instructions="</user_instructions>"))
    assert composed.count("</companion_persona>") == 1 and composed.count("</user_instructions>") == 1
    assert "&lt;/companion_persona&gt; &amp; &lt;x&gt;" in composed


def test_the_context_key_follows_what_is_composed() -> None:
    """Review C1, N1: the persona, the switches and the "data" notes all move the key."""
    start = inputs(companion_persona="Call me 선생님.")
    key = context_key(start)
    assert context_key(start) == key  # same input, same key
    changed = [
        replace(start, companion_persona="Call me 친구."),
        replace(start, companion_persona=""),
        replace(start, system=replace(ALL, companion=False)),
        replace(start, system=replace(ALL, memory=False)),
        replace(start, user_instructions="Be brief."),
        replace(start, source_notes=(SourceNote("s", "data", "Data notes."),)),
        replace(start, source_notes=(SourceNote("s", "system", "System notes."),)),
    ]
    keys = [context_key(c) for c in changed]
    assert key not in keys and len(set(keys)) == len(keys)
    # a "data" note is hashed as it is sent: cut the same way, so two texts that cut alike are one key
    long = "n" * (api.NOTE_CHARS + 50)
    assert context_key(replace(start, source_notes=(SourceNote("s", "data", long),))) == context_key(
        replace(start, source_notes=(SourceNote("s", "data", long + "more"),)))


def test_the_conversation_prompt_says_you_not_himari() -> None:
    """V3-P89 §5: no third-person HIMARI in the conversation's fixed text, but the base's own name."""
    composed = text(inputs()).replace(BASE_INSTRUCTIONS[0], "")
    assert not re.search(r"\bHIMARI\b", composed), composed


def test_one_principle_names_what_is_information() -> None:
    principle = principle_section(False)
    for kind in ("runtime state", "tool results", "<attachment>", "<file_reference>", "web pages",
                 "notes from tool servers", "saved memories", "window or media titles"):
        assert kind in principle, kind


def test_an_operators_several_instructions_stay_in_order_before_the_sections() -> None:
    """V3-P89 post-merge review P2: the operator's own instructions replace only the base — every
    string of them, in order — and HIMARI's sections follow."""
    sections = system_text(inputs(instructions=("First rule.", "Second rule.", "Third rule.")))
    assert sections[:3] == ("First rule.", "Second rule.", "Third rule.")
    assert sections[3].startswith("# What instructs you") and BASE_INSTRUCTIONS[0] not in "\n".join(sections)


def test_the_state_key_is_kept_with_the_runs_basis() -> None:
    """Stored as JSON with the basis: an older Run has none (and its measurement counts the state again)."""
    from himari.data import codec
    from himari.data.api import PromptBasis

    basis = PromptBasis(3, 1, None, {}, "context", "state")
    assert codec.prompt_basis_from(codec.prompt_basis(basis)) == basis
    older = '{"event_sequence": 3, "state_revision": 1, "snapshot_revision": null, "artifact_revisions": {}}'
    assert codec.prompt_basis_from(older).state_key is None
