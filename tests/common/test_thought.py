"""character-mind §3: a leading thought never shows, live or whole, and is cut the same way."""

from __future__ import annotations

import random

import pytest

from himari.common import (
    THINK_CHARS,
    MarkScan,
    TextBlock,
    drop_thought,
    scan_marks,
    split_marks,
    split_thought,
    thought_end,
    thought_of,
)


def shown(text: str) -> str:
    plain, _, _, consumed = split_marks(text, complete=True)
    assert consumed == len(text)
    return plain


def live(text: str, cuts: list[int]) -> str:
    """The plain text sent as ``text`` grows at ``cuts``, then ends."""
    out: list[str] = []
    state = MarkScan()
    for end in [*cuts, len(text)]:
        rest = text[state.raw:end]
        piece, _, _, state = scan_marks(rest, state, complete=end == len(text))
        out.append(piece)
    return "".join(out)


@pytest.mark.parametrize(("text", "plain", "thought"), [
    ("[[think]]걱정되네. 가볍게 말하자.[[/think]]\n[[mood:worried]]또 밤샜어?", "또 밤샜어?", "걱정되네. 가볍게 말하자."),
    ("  \n[[think]]음[[/think]]  안녕", "안녕", "음"),
    ("[[think]]그냥 두자.[[/think]][[pass]]", "[[pass]]", "그냥 두자."),
    ("안녕 [[think]]이건 글[[/think]]", "안녕 [[think]]이건 글[[/think]]", None),  # not at the start: text
    ("안녕", "안녕", None),
    ("[[think]]닫히지 않음", "", "닫히지 않음"),  # never closed: all of it was thought
    ("[[think]][[/think]]답", "답", None),  # an empty thought
])
def test_a_leading_thought_is_left_out(text: str, plain: str, thought: str | None) -> None:
    assert shown(text) == plain
    assert split_thought(text)[0] == thought


def test_moods_count_from_what_is_shown() -> None:
    _, moods, _, _ = split_marks("[[think]]기쁘다[[/think]]와 [[mood:happy]]좋아!", complete=True)
    assert moods == ((2, "happy"),)


def test_a_thought_not_closed_in_time_is_cut() -> None:
    long = "가" * (THINK_CHARS + 40)
    text = f"[[think]]{long}[[/think]]답"
    cut = long[THINK_CHARS:] + "[[/think]]답"
    assert shown(text) == cut
    assert split_thought(text)[0] == long[:THINK_CHARS]
    assert live(text, [20, THINK_CHARS + 30]) == cut


def test_a_growing_answer_shows_nothing_while_it_thinks() -> None:
    text = "[[think]]뭐라고 할까.[[/think]] 응, 좋아!"
    for end in range(1, text.index("응")):
        assert scan_marks(text[:end], MarkScan(), complete=False)[0] == ""
    assert thought_end("[[thi", complete=False) is None
    assert thought_end("  ", complete=False) is None
    assert thought_end("[[mo", complete=False) == (0, 0, 0)


@pytest.mark.parametrize("text", [
    "[[think]]걱정되네.[[/think]]\n[[mood:worried]]또 밤샜어? `code` 끝",
    "[[think]]" + "가" * (THINK_CHARS + 5) + "[[/think]]답",
    "그냥 답 [[mood:happy]]",
    "[[think]]짧은 생각",
])
def test_live_and_whole_agree(text: str) -> None:
    rng = random.Random(7)
    for _ in range(30):
        cuts = sorted(rng.sample(range(1, len(text)), k=min(4, len(text) - 1)))
        assert live(text, cuts) == shown(text)


def test_blocks_lose_their_thought() -> None:
    blocks = (TextBlock("[[think]]반갑다[[/think]]안녕!"),)
    assert thought_of(blocks) == "반갑다"
    assert drop_thought(blocks) == (TextBlock("안녕!"),)
    plain = (TextBlock("안녕!"),)
    assert thought_of(plain) is None
    assert drop_thought(plain) is plain


@pytest.mark.parametrize("text", [
    "\u3000[[think]]몰래 걱정했다.[[/think]]\u3000안녕",
    "\u00a0\u2003\n[[think]]몰래 걱정했다.[[/think]]\u2028안녕",
    "\ufeff\u3000[[think]]몰래 걱정했다.[[/think]]안녕",
])
def test_unicode_white_space_around_a_thought_never_lets_it_show(text: str) -> None:
    """Review P0-2: white space is any Unicode white space (str.isspace), before the open and after the close;
    live and whole agree, and the thought reaches neither the text, nor the history, nor search."""
    if text.startswith("\ufeff"):  # not white space: the thought is not at the start, so it is text
        assert split_thought(text)[0] is None
        return
    assert shown(text) == "안녕"
    assert split_thought(text)[0] == "몰래 걱정했다."
    assert drop_thought((TextBlock(text),)) == (TextBlock("안녕"),)
    rng = random.Random(11)
    for _ in range(30):
        cuts = sorted(rng.sample(range(1, len(text)), k=4))
        assert live(text, cuts) == "안녕"
    assert thought_end("\u3000\u3000", complete=False) is None  # still may open one
