"""V3-P83 §7.2: mood marks come out of an answer; code keeps them as text."""

from __future__ import annotations

import random

import pytest

from himari.common import INLINE_CODE_CHARS, MOODS, MoodScan, scan_moods, split_moods


def whole(text: str) -> tuple[str, tuple[tuple[int, str], ...]]:
    plain, moods, consumed = split_moods(text, complete=True)
    assert consumed == len(text)
    return plain, moods


@pytest.mark.parametrize(("text", "plain", "moods"), [
    ("[[mood:happy]]잘 됐다!", "잘 됐다!", ((0, "happy"),)),
    ("좋아. [[mood:thinking]]음… [[mood:excited]]됐다!", "좋아. 음… 됐다!", ((4, "thinking"), (7, "excited"))),
    ("[[mood:grumpy]]흠", "흠", ((0, "neutral"),)),  # outside the list: neutral
    ("[[mood:Happy]]", "[[mood:Happy]]", ()),  # not the grammar: text
    ("[[mood:" + "a" * 25 + "]]", "[[mood:" + "a" * 25 + "]]", ()),
    ("[[[mood:sad]]", "[", ((1, "sad"),)),
    ("[[mood:sad]", "[[mood:sad]", ()),  # broken
    ("a `[[mood:happy]]` b", "a `[[mood:happy]]` b", ()),  # inline code
    ("a ``x ` [[mood:sad]]`` b", "a ``x ` [[mood:sad]]`` b", ()),
    ("a `x\n[[mood:sad]]` b", "a `x\n` b", ((5, "sad"),)),  # inline code closes on its line only
    ("a `" + "x" * (INLINE_CODE_CHARS + 1) + "` [[mood:sad]]", "a `" + "x" * (INLINE_CODE_CHARS + 1) + "` ",
     ((INLINE_CODE_CHARS + 6, "sad"),)),  # too far to close: the backticks are text
    ("```\n[[mood:sad]]\n```\n[[mood:sad]]끝", "```\n[[mood:sad]]\n```\n끝", ((21, "sad"),)),
    ("~~~~ py\n[[mood:sad]]\n~~~\n[[mood:sad]]", "~~~~ py\n[[mood:sad]]\n~~~\n[[mood:sad]]", ()),  # too short to close
    ("```py\n[[mood:sad]]", "```py\n[[mood:sad]]", ()),  # never closed: code to the end
    ("   ```\n[[mood:sad]]", "   ```\n[[mood:sad]]", ()),
    ("    ```\n[[mood:sad]]", "    ```\n", ((8, "sad"),)),  # four spaces: no fence
    ("```a`b\n[[mood:sad]]", "```a`b\n", ((7, "sad"),)),  # a backtick in the info: no fence
    ("😀[[mood:happy]]a", "😀a", ((1, "happy"),)),  # code points, not UTF-16 units
])
def test_split(text: str, plain: str, moods: tuple[tuple[int, str], ...]) -> None:
    assert whole(text) == (plain, moods)


def test_every_mood_name_is_kept() -> None:
    for mood in MOODS:
        assert whole(f"[[mood:{mood}]]") == ("", ((0, mood),))


@pytest.mark.parametrize(("text", "held"), [
    ("안녕 [", "["), ("안녕 [[mo", "[[mo"), ("안녕 [[mood:hap", "[[mood:hap"), ("안녕 [[mood:happy]", "[[mood:happy]"),
    ("x `abc", "`abc"), ("x ``", "``"), ("x `a`", "`a`"),  # the run may grow
    ("a\n``", "``"), ("a\n  ", "  "), ("a\n```py", "```py"),  # a line that may open a fence
    ("```\nx\n``", "``"),  # or close one
])
def test_a_growing_text_holds_back_an_end_that_may_change(text: str, held: str) -> None:
    plain, _, consumed = split_moods(text, complete=False)
    assert text[consumed:] == held
    assert plain == text[:consumed]


def test_an_end_that_cannot_change_is_confirmed() -> None:
    assert split_moods("a [x", complete=False)[2] == 4
    assert split_moods("a\n`` b", complete=False)[2] == 2  # `` b: no fence, but the inline code may still close
    assert split_moods("a\n`x", complete=False)[2] == 2
    assert split_moods("x `" + "a" * (INLINE_CODE_CHARS + 1), complete=False)[2] == INLINE_CODE_CHARS + 4
    assert split_moods("```\ncode [[mo", complete=False)[0] == "```\ncode [[mo"  # code is text at once


ALPHABET = ["[[mood:happy]]", "[[mood:sad]]", "[[mood:nope]]", "[", "]", "[[mood:", "`", "``", "```", "~~~",
            "\n", " ", "a", "가", "😀", "x" * 30]


def test_what_a_prefix_confirms_starts_the_whole_split() -> None:
    """The invariant (§7.2, re-review C4), over random texts cut anywhere."""
    rng = random.Random(83)
    for _ in range(400):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 40)))
        plain, moods = whole(text)
        for cut in range(len(text) + 1):
            part, part_moods, consumed = split_moods(text[:cut], complete=False)
            assert plain.startswith(part), (text, cut)
            assert moods[:len(part_moods)] == part_moods, (text, cut)
            assert consumed <= cut


def test_going_on_from_a_state_is_the_same_as_starting_over() -> None:
    """``live`` goes on from its last state (§7.3, re-review N1)."""
    rng = random.Random(84)
    for _ in range(300):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 40)))
        state, plain = MoodScan(), ""
        moods: tuple[tuple[int, str], ...] = ()
        cuts = sorted(rng.sample(range(len(text) + 1), min(4, len(text) + 1)))
        for cut in cuts:
            piece, piece_moods, state = scan_moods(text[state.raw:cut], state, complete=False)
            plain, moods = plain + piece, moods + piece_moods
        piece, piece_moods, state = scan_moods(text[state.raw:], state, complete=True)
        assert (plain + piece, moods + piece_moods) == whole(text), text
        assert state.raw == len(text)
