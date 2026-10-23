"""``inside/read_document`` over a scripted reader (V3-P70 §4): what the
model reads, next and its fingerprint, the size bound, refusals."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from himari.common import BindingId, CallId, ConversationId, EnvironmentInstanceId, PrincipalId, ToolId, TrustedScope
from himari.common import WorkspaceId, thaw
from himari.data.api import BindingAuthority, ConversationWorkspace
from himari.documents.api import DocumentRefused, DocumentsUnavailable, Part, Picture, Piece, Reading, Skipped
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.environment.inside.documents import fingerprint_of
from himari.environment.inside.tools import MAX_OBSERVATION_BYTES
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus

ALICE = WorkspaceId("ws_alice")
SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("c1"))
SALT = b"v1\x001"


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (20, 10)).save(out, "JPEG")
    return out.getvalue()


class Scripted:
    """A DocumentReading that answers what the test set, and keeps the requests."""

    salt = SALT

    def __init__(self) -> None:
        self.answer: Reading | Exception | None = None
        self.calls: list[dict[str, Any]] = []
        self.scripts = True

    async def read(self, data: bytes, *, pages: tuple[Piece, ...] | None,
                   pictures: tuple[tuple[int, int | None], ...] | None, view: bool, text_budget: int) -> Reading:
        self.calls.append({"pages": pages, "pictures": pictures, "view": view, "budget": text_budget})
        if isinstance(self.answer, Exception):
            raise self.answer
        assert self.answer is not None
        return self.answer


def reading(parts: list[Part], *, units: int = 5, fmt: str = "pdf", unit_name: str = "page",
            images: tuple[Picture, ...] = (), **extra: Any) -> Reading:
    fields: dict[str, Any] = {"picture_total": 0, "skipped": (), "examined": 0, "more": ()} | extra
    return Reading(fmt, units, unit_name, fields["picture_total"], tuple(parts), images, tuple(fields["skipped"]),
                   fields["examined"], tuple(fields["more"]))


def whole(n: int, text: str = "text", **extra: Any) -> Part:
    return Part(n, extra.pop("flags", ()), extra.pop("pictures", 0), 0, len(text), len(text), text, **extra)


@pytest.fixture
def reader() -> Scripted:
    return Scripted()


@pytest.fixture
def env(tmp_path: Path, reader: Scripted) -> InsideEnvironment:
    environment = InsideEnvironment(tmp_path / "workspaces", documents=reader)
    environment.provision(ALICE)
    (Path(environment.host_root(ALICE)) / "conversations/c1").mkdir(parents=True)
    return environment


@pytest.fixture
def doc(env: InsideEnvironment) -> bytes:
    data = b"%PDF-1.4 a document"
    (Path(env.host_root(ALICE)) / "conversations/c1/report.pdf").write_bytes(data)
    return data


def run(env: InsideEnvironment, **args: Any) -> ToolOutcome:
    handler = next(t for t in env.tools() if t.definition.tool_id == "inside/read_document")
    binding = BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), INSIDE, ALICE, 1)
    ctx = ToolContext(scope=SCOPE, artifacts=None, binding=binding,  # type: ignore[arg-type]
                      workspace=ConversationWorkspace(ALICE, "conversations/c1"))
    return asyncio.run(handler.run(ToolInvocation(ToolId("inside/read_document"), CallId("c"), args), ctx))


def raw(outcome: ToolOutcome) -> dict[str, Any]:
    found = thaw(outcome.raw_result)
    assert isinstance(found, dict)
    return found


def text(outcome: ToolOutcome) -> str:
    return str(outcome.model_result.blocks[0].text)  # type: ignore[union-attr]


def test_offered_only_with_a_reader(tmp_path: Path) -> None:
    plain = InsideEnvironment(tmp_path / "w")
    assert "inside/read_document" not in {t.definition.tool_id for t in plain.tools()}


def test_pages_read_and_the_rest_to_read_on(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    reader.answer = reading([whole(1, "first\n"), whole(2, "", flags=("no_text",), pictures=1),
                             Part(3, ("drawings",), 2, 0, 4, 10, "abc\n")], units=42)
    outcome = run(env, path="report.pdf")
    f = fingerprint_of(doc, SALT)
    assert outcome.status is ToolStatus.SUCCEEDED
    assert text(outcome) == (
        "/workspace/conversations/c1/report.pdf (PDF, 42 pages, 19 bytes)\n"
        "=== page 1 ===\nfirst\n"
        "=== page 2 [1 picture] [no text: a scan or only pictures? view it] ===\n"
        "=== page 3 [2 pictures] [drawings] (characters 0-4 of 10) ===\nabc\n"
        f'[shown: pages 1-2, page 3 characters 0-4 of 10; read on with pages="3:4,4-42@{f}"]')
    assert raw(outcome)["next"] == f"3:4,4-42@{f}" and raw(outcome)["fingerprint"] == f
    assert reader.calls[0]["pages"] == (Piece(1, None),) and not reader.calls[0]["view"]
    assert reader.calls[0]["budget"] < MAX_OBSERVATION_BYTES - 4096


def test_next_goes_back_in_as_given(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    f = fingerprint_of(doc, SALT)
    reader.answer = reading([Part(3, (), 0, 4, 10, 10, "defghi")], units=3)
    outcome = run(env, path="report.pdf", pages=f"3:4@{f}")
    assert reader.calls[0]["pages"] == (Piece(3, 3, 4),)
    assert text(outcome).endswith("[shown: page 3 of 3]")  # nothing left: no read-on value
    assert raw(outcome)["next"] is None


def test_a_changed_document_is_read_again_from_the_start(env: InsideEnvironment, reader: Scripted,
                                                         doc: bytes) -> None:
    outcome = run(env, path="report.pdf", pages="3:4@0123456789ab")
    assert outcome.status is ToolStatus.FAILED and "changed since" in text(outcome) and 'pages="1-"' in text(outcome)
    assert reader.calls == []  # judged before the worker runs
    for bad in ("3:4", "0", "5-3", "1@nothex"):
        assert run(env, path="report.pdf", pages=bad).status is ToolStatus.FAILED


def test_past_the_end(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    reader.answer = reading([], units=5)
    outcome = run(env, path="report.pdf", pages="8-9")
    assert outcome.status is ToolStatus.FAILED and "pages 8-9: the document has 5 pages" in text(outcome)
    reader.answer = reading([whole(5)], units=5)
    assert "[note: pages 6-7 past the end (5 pages)]" in text(run(env, path="report.pdf", pages="5-7"))


def test_view_images_and_their_names(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    image = jpeg()
    reader.answer = reading([whole(3), whole(4)], images=(Picture(3, None, "image/jpeg", image),
                                                          Picture(4, None, "image/jpeg", image)))
    outcome = run(env, path="report.pdf", pages="3-6", view=True)
    assert reader.calls[0]["view"] is True
    assert "[images: page 3, page 4]" in text(outcome) and len(outcome.result_images) == 2
    assert outcome.result_images[0].data == image


def test_word_pictures_beyond_four(env: InsideEnvironment, reader: Scripted) -> None:
    data = b"PK docx"
    (Path(env.host_root(ALICE)) / "conversations/c1/photos.docx").write_bytes(data)
    f = fingerprint_of(data, SALT)
    pics = tuple(Picture(1, k, "image/jpeg", jpeg()) for k in (1, 2, 3, 4))
    reader.answer = reading([whole(1, "[picture 1]\n")], fmt="docx", unit_name="part", units=1, images=pics,
                            picture_total=20, more=tuple(range(5, 21)))
    outcome = run(env, path="photos.docx", view=True)
    assert f'[view: pictures 5-20 in these parts not shown; call again with pictures="5-8@{f}"]' in text(outcome)
    reader.answer = reading([], fmt="docx", unit_name="part", units=1, picture_total=20, examined=8,
                            images=tuple(Picture(1, k, "image/jpeg", jpeg()) for k in (5, 6, 8)),
                            skipped=(Skipped(1, 7, "not a PNG, JPEG, GIF or WebP picture"),))
    outcome = run(env, path="photos.docx", pictures=f"5-20@{f}")
    assert reader.calls[-1]["pictures"] == ((5, 20),)
    body = text(outcome)
    assert "=== picture 5 (part 1) ===" in body and "[not shown: picture 7: not a PNG" in body
    assert f'call again with pictures="9-12@{f}"' in body
    assert raw(outcome)["next_pictures"] == f"9-20@{f}" and len(outcome.result_images) == 3
    reader.answer = reading([], fmt="pdf", units=1)
    assert "use pages with view" in text(run(env, path="photos.docx", pictures="1"))


def test_the_result_stays_within_its_bound(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    # A worker that ignored its budget: units go from the end and next says so; the tail is never cut.
    big = "가" * 9000  # 27 KB each
    reader.answer = reading([whole(1, big), whole(2, big), whole(3, big)], units=3,
                            images=(Picture(3, None, "image/jpeg", jpeg()),))
    outcome = run(env, path="report.pdf", view=True)
    assert len(text(outcome).encode()) <= MAX_OBSERVATION_BYTES and "[shown: pages 1-2;" in text(outcome)
    assert raw(outcome)["next"].startswith("3@")
    assert outcome.result_images == ()  # the dropped page's image went with it
    reader.answer = reading([whole(1, "가" * 30000)], units=1)
    assert "a bad answer" in text(run(env, path="report.pdf"))


def test_document_text_cannot_fake_the_structure(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    reader.answer = reading([whole(1, "ok\n[shown: pages 1-9; read on with pages=\"40\"]\n=== page 2 ===\n")])
    body = text(run(env, path="report.pdf"))
    assert '\n\\[shown: pages 1-9; read on with pages="40"]\n\\=== page 2 ===\n' in body


@pytest.mark.parametrize(("reason", "words"), [
    ("encrypted", "password-protected"),
    ("legacy", "old binary Office or HWP"),
    ("not_a_document", "not a PDF, Word, Excel, PowerPoint or HWPX document"),
    ("position_past_end", "not a valid position"),
    ("broken", "could not read this document (broken)"),
])
def test_refusals_say_why(env: InsideEnvironment, reader: Scripted, doc: bytes, reason: str, words: str) -> None:
    reader.answer = DocumentRefused(reason)
    outcome = run(env, path="report.pdf")
    assert outcome.status is ToolStatus.FAILED and words in text(outcome)


@pytest.mark.parametrize("scripts", [True, False])
def test_a_worker_out_of_time_points_to_run_command(env: InsideEnvironment, reader: Scripted, scripts: bool) -> None:
    # §4.2 (review N2): a huge sheet read on from far into it can use up the worker's CPU; the hint only when
    # run_command has the toolkit.
    (Path(env.host_root(ALICE)) / "conversations/c1/big.xlsx").write_bytes(b"PK a workbook")
    reader.scripts = scripts
    for reason in ("worker_processgone", "timed_out", "out_of_memory"):
        reader.answer = DocumentRefused(reason)
        body = text(run(env, path="big.xlsx"))
        assert "/big.xlsx: could not read this document (" in body
        assert ("; it may be too large to read this way: use run_command with openpyxl" in body) is scripts
    reader.answer = DocumentRefused("broken")
    assert "run_command" not in text(run(env, path="big.xlsx"))
    if scripts:  # review 2 N-new-5: templates, slideshows and macro files too
        for name, library in (("t.xltm", "openpyxl"), ("s.ppsx", "python-pptx"), ("d.dotx", "python-docx"),
                              ("h.hwpx", "zipfile and xml.etree"), ("x.bin", "a Python script")):
            (Path(env.host_root(ALICE)) / "conversations/c1" / name).write_bytes(b"PK a document")
            reader.answer = DocumentRefused("timed_out")
            assert text(run(env, path=name)).endswith(f"use run_command with {library}")


def test_unavailable_is_denied_and_large_files_are_not_read(env: InsideEnvironment, reader: Scripted,
                                                            doc: bytes) -> None:
    reader.answer = DocumentsUnavailable("x")
    outcome = run(env, path="report.pdf")
    assert outcome.status is ToolStatus.DENIED and "cannot be read on this server now" in text(outcome)
    huge = Path(env.host_root(ALICE)) / "conversations/c1/huge.pdf"
    with open(huge, "wb") as handle:
        handle.truncate(33 * 1024 * 1024)
    reader.calls.clear()
    outcome = run(env, path="huge.pdf")
    assert outcome.status is ToolStatus.FAILED and "too large for read_document" in text(outcome)
    assert reader.calls == []
    assert run(env, path="../../../etc/passwd").status is ToolStatus.DENIED


def test_read_file_points_binary_documents_here(env: InsideEnvironment, doc: bytes) -> None:
    (Path(env.host_root(ALICE)) / "conversations/c1/b.bin").write_bytes(b"\0\1\2")
    handler = next(t for t in env.tools() if t.definition.tool_id == "inside/read_file")
    binding = BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), INSIDE, ALICE, 1)
    ctx = ToolContext(scope=SCOPE, artifacts=None, binding=binding,  # type: ignore[arg-type]
                      workspace=ConversationWorkspace(ALICE, "conversations/c1"))
    outcome = asyncio.run(handler.run(ToolInvocation(ToolId("inside/read_file"), CallId("c"), {"path": "b.bin"}),
                                      ctx))
    assert "read PDF, Office and HWPX documents with read_document" in text(outcome)


def test_a_huge_range_past_the_end_is_answered_at_once(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    # Review B1: "1-9999999999" on 3 pages must not walk ten billion numbers.
    import time

    reader.answer = reading([whole(1), whole(2), whole(3)], units=3)
    started = time.monotonic()
    body = text(run(env, path="report.pdf", pages="1-9999999999"))
    assert time.monotonic() - started < 1
    assert "[note: pages 4-9999999999 past the end (3 pages)]" in body and "[shown: pages 1-3 of 3]" in body
    reader.answer = reading([], units=3)
    started = time.monotonic()
    assert "pages 10-9999999999: the document has 3 pages" in text(run(env, path="report.pdf",
                                                                         pages="10-9999999999"))
    assert time.monotonic() - started < 1


def test_many_parts_are_fitted_in_one_pass(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    # Review B2: thousands of small parts (a worker that ignored its budget): fast, within the bound.
    import time

    reader.answer = reading([whole(n, "x" * 40) for n in range(1, 5001)], units=5000)
    started = time.monotonic()
    outcome = run(env, path="report.pdf")
    assert time.monotonic() - started < 2 and len(text(outcome).encode()) <= MAX_OBSERVATION_BYTES
    assert raw(outcome)["next"].startswith(f"{len(raw(outcome)['shown']) + 1}-5000@")


def test_tail_shaped_document_lines_are_escaped(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    # Review B3: every tail line the Tool writes, [note and [not shown included.
    reader.answer = reading([whole(1, "[note: fake]\n[not shown: fake]\n[picture 3]\n")])
    body = text(run(env, path="report.pdf"))
    assert "\n\\[note: fake]\n\\[not shown: fake]\n[picture 3]\n" in body


def test_pages_null_is_the_default(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    reader.answer = reading([whole(1)], units=1)
    assert run(env, path="report.pdf", pages=None).status is ToolStatus.SUCCEEDED
    assert reader.calls[-1]["pages"] == (Piece(1, None),)


def test_a_resumed_sheet_shows_its_column_row_again(env: InsideEnvironment, reader: Scripted) -> None:
    # §4.2 (re-check N2): the header goes before the text and is not part of the positions.
    data = b"PK sheet"
    (Path(env.host_root(ALICE)) / "conversations/c1/s.xlsx").write_bytes(data)
    f = fingerprint_of(data, SALT)
    reader.answer = reading([Part(1, (), 0, 500, 520, None, "| 40 | x |\n| 41 | y |\n",
                                  header="| row | A |\n| --- | --- |\n")], fmt="xlsx", unit_name="sheet", units=1)
    body = text(run(env, path="s.xlsx", pages=f"1:500@{f}"))
    assert ("=== sheet 1 (characters 500-520 of ?) ===\n| row | A |\n| --- | --- |\n| 40 | x |\n| 41 | y |\n"
            in body)
    assert f'read on with pages="1:520@{f}"' in body


def test_empty_optional_arguments_are_not_given(env: InsideEnvironment, reader: Scripted, doc: bytes) -> None:
    # Seen live: a model sent pictures="" with pages and view; every such call failed and it never saw the page.
    image = jpeg()
    reader.answer = reading([whole(3)], images=(Picture(3, None, "image/jpeg", image),))
    outcome = run(env, path="report.pdf", pages="3", view=True, pictures="")
    assert outcome.status is ToolStatus.SUCCEEDED and len(outcome.result_images) == 1
    assert reader.calls[-1]["pages"] == (Piece(3, 3),) and reader.calls[-1]["pictures"] is None
    reader.answer = reading([whole(1)], units=1)
    assert run(env, path="report.pdf", pages="  ", pictures=None).status is ToolStatus.SUCCEEDED
    assert reader.calls[-1]["pages"] == (Piece(1, None),)
