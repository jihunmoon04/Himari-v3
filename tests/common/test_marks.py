"""V3-P83 §7.2, V3-P84 §6: mood and move marks come out of an answer; code keeps them as text."""

from __future__ import annotations

import random

import pytest

from himari.common import INLINE_CODE_CHARS, MOODS, MarkScan, Moves, scan_marks, split_marks


def whole(text: str) -> tuple[str, tuple[tuple[int, str], ...]]:
    plain, moods, _, consumed = split_marks(text, complete=True)
    assert consumed == len(text)
    return plain, moods


def moved(text: str) -> tuple[str, Moves]:
    plain, _, moves, consumed = split_marks(text, complete=True)
    assert consumed == len(text)
    return plain, moves


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


@pytest.mark.parametrize(("text", "plain", "moves"), [
    ("음… [[move:1043,88]]여기 눌러 봐", "음… 여기 눌러 봐", ((3, 1043, 88),)),
    ("[[move:12, 34]]a[[move:0,0]]", "a", ((0, 12, 34), (1, 0, 0))),  # a space after the comma
    ("[[move:99999,99999]]", "", ((0, 99999, 99999),)),
    ("a[[move:1 2]]b", "ab", ()),  # the shape comes out; only X,Y moves
    ("a[[move:1,2,3]]b", "ab", ()),
    ("a[[move:123456,1]]b", "ab", ()),
    ("a[[move:1,  2]]b", "ab", ()),
    ("a[[move:,]]b", "ab", ()),
    ("a[[move:x,1]]b", "a[[move:x,1]]b", ()),  # not the shape: text
    ("a[[move:" + "1" * 14 + "]]", "a[[move:" + "1" * 14 + "]]", ()),
    ("a `[[move:1,2]]` b", "a `[[move:1,2]]` b", ()),  # code keeps it
    ("```\n[[move:1,2]]\n```\n[[move:3,4]]", "```\n[[move:1,2]]\n```\n", ((21, 3, 4),)),
    ("[[mood:happy]][[move:5,6]]가[[mood:sad]]", "가", ((0, 5, 6),)),
])
def test_moves(text: str, plain: str, moves: Moves) -> None:
    assert moved(text) == (plain, moves)


def test_moods_and_moves_keep_their_places() -> None:
    plain, moods, moves, _ = split_marks("[[mood:thinking]]음… [[move:7,8]][[mood:happy]]여기!", complete=True)
    assert (plain, moods, moves) == ("음… 여기!", ((0, "thinking"), (3, "happy")), ((3, 7, 8),))


def test_every_mood_name_is_kept() -> None:
    for mood in MOODS:
        assert whole(f"[[mood:{mood}]]") == ("", ((0, mood),))


@pytest.mark.parametrize(("text", "held"), [
    ("안녕 [", "["), ("안녕 [[mo", "[[mo"), ("안녕 [[mood:hap", "[[mood:hap"), ("안녕 [[mood:happy]", "[[mood:happy]"),
    ("x `abc", "`abc"), ("x ``", "``"), ("x `a`", "`a`"),  # the run may grow
    ("a\n``", "``"), ("a\n  ", "  "), ("a\n```py", "```py"),  # a line that may open a fence
    ("```\nx\n``", "``"),  # or close one
    ("가 [[mov", "[[mov"), ("가 [[move:", "[[move:"), ("가 [[move:12, 3", "[[move:12, 3"),
    ("가 [[move:1,2]", "[[move:1,2]"),
])
def test_a_growing_text_holds_back_an_end_that_may_change(text: str, held: str) -> None:
    plain, _, _, consumed = split_marks(text, complete=False)
    assert text[consumed:] == held
    assert plain == text[:consumed]


def test_an_end_that_cannot_change_is_confirmed() -> None:
    assert split_marks("a [x", complete=False)[3] == 4
    assert split_marks("a\n`` b", complete=False)[3] == 2  # `` b: no fence, but the inline code may still close
    assert split_marks("a\n`x", complete=False)[3] == 2
    assert split_marks("x `" + "a" * (INLINE_CODE_CHARS + 1), complete=False)[3] == INLINE_CODE_CHARS + 4
    assert split_marks("```\ncode [[mo", complete=False)[0] == "```\ncode [[mo"  # code is text at once
    assert split_marks("a [[move:1" + "2" * 13, complete=False)[3] == 23  # too long to be a move: text


ALPHABET = ["[[mood:happy]]", "[[mood:sad]]", "[[mood:nope]]", "[", "]", "[[mood:", "`", "``", "```", "~~~",
            "\n", " ", "a", "가", "😀", "x" * 30, "[[move:12,34]]", "[[move:", "5", ",", "[[move:1 2]]"]


def test_what_a_prefix_confirms_starts_the_whole_split() -> None:
    """The invariant (§7.2, re-review C4; moves too, V3-P84 §6.2), over random texts cut anywhere."""
    rng = random.Random(83)
    for _ in range(400):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 40)))
        plain, moods, moves, _ = split_marks(text, complete=True)
        for cut in range(len(text) + 1):
            part, part_moods, part_moves, consumed = split_marks(text[:cut], complete=False)
            assert plain.startswith(part), (text, cut)
            assert moods[:len(part_moods)] == part_moods, (text, cut)
            assert moves[:len(part_moves)] == part_moves, (text, cut)
            assert consumed <= cut


def test_going_on_from_a_state_is_the_same_as_starting_over() -> None:
    """``live`` goes on from its last state (§7.3, re-review N1)."""
    rng = random.Random(84)
    for _ in range(300):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 40)))
        state, plain = MarkScan(), ""
        moods: tuple[tuple[int, str], ...] = ()
        moves: Moves = ()
        cuts = sorted(rng.sample(range(len(text) + 1), min(4, len(text) + 1)))
        for cut in cuts:
            piece, piece_moods, piece_moves, state = scan_marks(text[state.raw:cut], state, complete=False)
            plain, moods, moves = plain + piece, moods + piece_moods, moves + piece_moves
        piece, piece_moods, piece_moves, state = scan_marks(text[state.raw:], state, complete=True)
        assert (plain + piece, moods + piece_moods, moves + piece_moves) == split_marks(text, complete=True)[:3], text
        assert state.raw == len(text)
