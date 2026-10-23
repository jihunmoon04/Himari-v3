"""read_document's selection grammar, next and the lines it writes (V3-P70 §4, §4.2)."""

from __future__ import annotations

import pytest

from himari.documents.api import Part, Piece
from himari.environment.inside.documents import (
    SelectionError,
    _part_block,
    backstop,
    compress,
    fingerprint_of,
    next_value,
    past_end,
    spans_text,
    parse_selection,
)

F = "@3f9a1c0b7e2d"


@pytest.mark.parametrize(("raw", "pieces"), [
    ("1-5,8", (Piece(1, 5), Piece(8, 8))),
    ("8,1-5", (Piece(1, 5), Piece(8, 8))),  # a union, in order
    ("1-5,3", (Piece(1, 5),)),  # overlaps once
    ("12-", (Piece(12, None),)),
    ("1-3,4-6", (Piece(1, 6),)),  # adjacent ranges join
    ("5-,2,9", (Piece(2, 2), Piece(5, None))),
    (" 1 - 2 , 4 ", (Piece(1, 2), Piece(4, 4))),
    ("9:100,10-12" + F, (Piece(9, 9, 100), Piece(10, 12))),
    ("9:100,9" + F, (Piece(9, 9),)),  # the whole unit wins
    ("9:100,9:200" + F, (Piece(9, 9, 100),)),  # the earliest start
    ("9:0" + F, (Piece(9, 9),)),
    ("8-10,9:5" + F, (Piece(8, 10),)),
])
def test_selections_are_normalized(raw: str, pieces: tuple[Piece, ...]) -> None:
    assert parse_selection(raw, positions=True)[0] == pieces


@pytest.mark.parametrize("raw", ["", "0", "5-3", "a", "1,,2", "1-2-3", "-3", "1@xyz", "1@3F9A1C0B7E2D",
                                 "9:100", ",".join(["1"] * 21), "1," * 49 + "1"])
def test_bad_selections_are_refused(raw: str) -> None:
    with pytest.raises(SelectionError):
        parse_selection(raw, positions=True)


def test_limits_are_wider_for_a_value_this_tool_wrote() -> None:
    # The model's own: 20 pieces, 100 characters. With a fingerprint: 22 and 200 (re-review C2).
    twenty = ",".join(str(n * 2) for n in range(1, 21))
    parse_selection(twenty, positions=True)
    with pytest.raises(SelectionError):
        parse_selection(twenty + ",50", positions=True)
    parse_selection("1:1234567890," + twenty + ",50" + F, positions=True)
    with pytest.raises(SelectionError):
        parse_selection(",".join(str(n * 2) for n in range(1, 24)) + F, positions=True)


def test_pictures_take_numbers_only() -> None:
    assert parse_selection("5-8" + F, positions=False) == ((Piece(5, 8),), F[1:])
    with pytest.raises(SelectionError):
        parse_selection("5:3" + F, positions=False)


def test_a_full_selection_still_takes_its_next() -> None:
    # 20 pieces and up to 100 characters, cut inside a unit at a 10-digit position: next is accepted.
    raw = "1-99," + ",".join(str(n) for n in range(200, 236, 2)) + ",300-"
    assert len(raw.split(",")) == 20 and len(raw) <= 100
    pieces, _ = parse_selection(raw, positions=True)
    parts = (Part(1, (), 0, 0, 1, 1, "a"), Part(2, (), 0, 0, 1, 1, "b"),
             Part(3, (), 0, 0, 1234567890, 99999999999, ""))
    rest = next_value(pieces, 400, parts)
    assert rest is not None and rest.startswith("3:1234567890,4-99,200,") and rest.endswith(",300-400")
    again, _ = parse_selection(rest + F, positions=True)
    assert again[0] == Piece(3, 3, 1234567890) and again[-1] == Piece(300, 400)


def test_next_is_the_rest_of_the_selection() -> None:
    pieces = (Piece(1, 5), Piece(8, 8))
    whole = tuple(Part(n, (), 0, 0, 1, 1, "x") for n in (1, 2, 3))
    assert next_value(pieces, 42, whole) == "4-5,8"
    assert next_value(pieces, 42, whole[:2] + (Part(3, (), 0, 0, 7, 20, "x" * 7),)) == "3:7,4-5,8"
    assert next_value((Piece(40, None),), 42, tuple(Part(n, (), 0, 0, 1, 1, "x") for n in (40, 41))) == "42"
    assert next_value((Piece(1, 2),), 42, tuple(Part(n, (), 0, 0, 1, 1, "x") for n in (1, 2))) is None
    assert next_value((Piece(40, 60),), 42, ()) == "40-42"  # past the end left out
    assert next_value((Piece(9, 9, 100), Piece(10, 11)), 42, ()) == "9:100,10-11"
    assert next_value((Piece(9, 9, 100), Piece(10, 11)), 42, (Part(9, (), 0, 100, 120, 120, "x" * 20),)) == "10-11"


def test_huge_ranges_stay_ranges() -> None:
    # B1: "1-9999999999" on a 3-page document is ranges all the way, never one entry per number.
    pieces, _ = parse_selection("1-9999999999", positions=True)
    assert past_end(pieces, 3) == [(4, 9999999999, 0)]
    assert next_value(pieces, 3, (Part(1, (), 0, 0, 1, 1, "x"),)) == "2-3"
    assert spans_text(past_end(parse_selection("50-", positions=True)[0], 42)) == "50-"


def test_compress() -> None:
    assert compress([1, 2, 3, 5, 7, 8]) == "1-3,5,7-8"
    assert compress([]) == ""


def test_the_fingerprint_follows_bytes_and_toolkit() -> None:
    a = fingerprint_of(b"doc", b"v1\x001")
    assert len(a) == 12 and a == fingerprint_of(b"doc", b"v1\x001")
    assert a != fingerprint_of(b"doc2", b"v1\x001") and a != fingerprint_of(b"doc", b"v2\x001")
    assert a != fingerprint_of(b"doc", b"v1\x002")  # the split rule moves positions


def test_the_backstop_leaves_escaped_and_mid_line_text_alone() -> None:
    assert backstop("=== page 9 ===\n[shown: x]\n[view: y]\n[images: z]\nok\n") == (
        "\\=== page 9 ===\n\\[shown: x]\n\\[view: y]\n\\[images: z]\nok\n")
    assert backstop("\\=== page 9 ===\n[picture 2]\n[hidden: a]\n") == "\\=== page 9 ===\n[picture 2]\n[hidden: a]\n"
    assert backstop("…=== page 9 ===\nok") == "…=== page 9 ===\nok"  # shown text, with its prefix (re-check C1)


def test_unit_headers() -> None:
    page = Part(3, ("drawings",), 2, 0, 5, 5, "hello", label="iv")
    assert _part_block(page, "page") == '=== page 3 (label "iv") [2 pictures] [drawings] ===\nhello\n'
    scan = Part(4, ("no_text",), 1, 0, 0, 0, "")
    assert _part_block(scan, "page") == "=== page 4 [1 picture] [no text: a scan or only pictures? view it] ===\n"
    cut = Part(9, (), 0, 61234, 70000, 140002, "=== tail\nrest", mid_line=True)
    assert _part_block(cut, "page") == ("=== page 9 (characters 61,234-70,000 of 140,002) (continues mid-line) ===\n"
                                        "…=== tail\nrest\n")
    open_ended = Part(1, (), 0, 0, 10, None, "0123456789")
    assert "(characters 0-10 of ?)" in _part_block(open_ended, "sheet")


def test_the_worker_never_cuts_between_an_escape_and_its_character() -> None:
    # Review C6: the guard in cut_at, not only the refusal of such a position.
    from himari.documents import worker

    text = "ab\n\\[x" + "y" * 50
    for budget in range(1, 12):
        end = worker.cut_at(text, 3, budget)
        assert end != 4, budget  # 4 is right after the escaping backslash
    assert worker.cut_at("\\[q", 0, 1) == 0 and worker.cut_at("\\[q", 0, 2) == 2


def test_the_overhead_matches_the_worker() -> None:
    from himari.documents import worker
    from himari.documents.api import PART_OVERHEAD, VIEW_LIMIT

    assert worker.PART_OVERHEAD == PART_OVERHEAD and worker.VIEW_UNITS == VIEW_LIMIT
