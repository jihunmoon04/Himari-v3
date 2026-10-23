"""The reader's checks (V3-P70 §5, §6.2): the toolkit on its descriptor, and
every worker answer judged again — nothing off passes."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from himari.documents import Toolkit, ToolkitRefused, _checked, open_toolkit, probe_docx, probe_pdf, worker_roots
from himari.documents.api import Piece

ME = frozenset({os.getuid()})


def jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(out, "JPEG")
    return out.getvalue()


def head(parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"ok": True, "format": "pdf", "units": 5, "unit_name": "page", "picture_total": 0, "parts": parts,
            "images": 0, "skipped": [], "examined": 0, "more": [], **extra}


def part(n: int, start: int, end: int, length: int | None, **extra: Any) -> dict[str, Any]:
    return {"n": n, "flags": [], "pictures": 0, "from": start, "to": end, "length": length,
            "text": "x" * (end - start), **extra}


def lines(first: dict[str, Any], *images: dict[str, Any]) -> list[bytes]:
    return [json.dumps(first).encode(), *(json.dumps(i).encode() for i in images)]


def image(unit: int, picture: int | None, data: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
    return {"unit": unit, "picture": picture, "media_type": media_type, "data": base64.b64encode(data).decode()}


ALL = (Piece(1, None),)


def test_a_good_answer() -> None:
    reading = _checked(lines(head([part(1, 0, 3, 3), part(2, 0, 0, 0, flags=["no_text"]), part(3, 0, 4, 9)])),
                       ALL, None, False, 1000)
    assert [(p.n, p.start, p.end, p.cut) for p in reading.parts] == [(1, 0, 3, False), (2, 0, 0, False),
                                                                     (3, 0, 4, True)]
    assert reading.parts[1].flags == ("no_text",)  # an empty unit (a scan) is fine (re-check B1)


@pytest.mark.parametrize("parts", [
    [part(2, 0, 3, 3)],  # skipped unit 1
    [part(1, 1, 3, 3)],  # not the requested start
    [part(1, 0, 3, 4), part(2, 0, 3, 3)],  # a cut part that is not last
    [part(1, 0, 4, 3)],  # past its length
    [part(1, 0, 0, 3)],  # no progress though text is left
    [part(1, 2, 2, None)],  # length unknown and no progress (re-check N1)
    [part(1, 0, 3, 3, flags=["secret"])],  # a mark outside the fixed set
    [part(1, 0, 3, 3, label="has space")],
    [part(1, 0, 3, 3, label="x" * 21)],
    [{**part(1, 0, 3, 3), "text": "xx"}],  # text length is not to − from
    [part(n, 0, 1, 1) for n in range(1, 7)],  # more units than the document has
])
def test_bad_parts_are_refused(parts: list[dict[str, Any]]) -> None:
    with pytest.raises((ValueError, KeyError, TypeError)):
        _checked(lines(head(parts)), ALL, None, False, 1000)


def test_resumed_positions_and_budget() -> None:
    pieces = (Piece(3, 3, 100), Piece(4, 5))
    reading = _checked(lines(head([part(3, 100, 150, 150), part(4, 0, 2, 2)])), pieces, None, False, 1000)
    assert reading.parts[0].start == 100
    with pytest.raises(ValueError):
        _checked(lines(head([part(3, 0, 150, 150)])), pieces, None, False, 1000)
    with pytest.raises(ValueError):
        _checked(lines(head([part(1, 0, 500, 500), part(2, 0, 501, 501)])), ALL, None, False, 1000)  # over budget


def test_view_images() -> None:
    good = jpeg(1568, 1000)
    reading = _checked(lines(head([part(1, 0, 1, 1)], images=1), image(1, None, good)), ALL, None, True, 1000)
    assert reading.images[0].unit == 1 and reading.images[0].data == good
    for bad in (image(1, None, jpeg(1569, 10)),  # past the long edge
                image(2, None, good),  # a unit not shown
                image(1, 3, good),  # a picture number on a PDF page
                image(1, None, good, "image/png"),  # declared as what it is not
                image(1, None, b"<svg/>", "image/png")):
        with pytest.raises((ValueError, KeyError, TypeError)):
            _checked(lines(head([part(1, 0, 1, 1)], images=1), bad), ALL, None, True, 1000)
    with pytest.raises(ValueError):  # nobody asked for images
        _checked(lines(head([part(1, 0, 1, 1)], images=1), image(1, None, good)), ALL, None, False, 1000)
    with pytest.raises(ValueError):  # view handles 4 units at most
        _checked(lines(head([part(n, 0, 1, 1) for n in range(1, 6)])), ALL, None, True, 1000)


def test_pictures_mode() -> None:
    docx = {"format": "docx", "unit_name": "part", "picture_total": 20}
    wanted = ((5, 8),)
    reading = _checked(lines(head([], **docx, images=2), image(1, 5, jpeg(10, 10)), image(2, 7, jpeg(10, 10))),
                       None, wanted, False, 1000)
    assert [i.number for i in reading.images] == [5, 7]
    for bad in ([image(1, 4, jpeg(10, 10))],  # not asked for
                [image(1, 7, jpeg(10, 10)), image(1, 5, jpeg(10, 10))]):  # not ascending
        with pytest.raises(ValueError):
            _checked(lines(head([], **docx, images=len(bad)), *bad), None, wanted, False, 1000)
    with pytest.raises(ValueError):  # no units in pictures mode
        _checked(lines(head([part(1, 0, 1, 1)], **docx)), None, wanted, False, 1000)


def test_unknown_formats_and_counts() -> None:
    with pytest.raises(ValueError):
        _checked(lines(head([], format="exe", unit_name="page")), ALL, None, False, 1000)
    with pytest.raises(ValueError):
        _checked(lines(head([], format="pdf", unit_name="sheet")), ALL, None, False, 1000)
    with pytest.raises(ValueError):
        _checked(lines(head([], units=-1)), ALL, None, False, 1000)


def test_probe_documents_are_well_formed() -> None:
    pdf = probe_pdf("HIMARI-PROBE")
    assert pdf.startswith(b"%PDF-1.4") and b"(HIMARI-PROBE) Tj" in pdf and pdf.endswith(b"%%EOF\n")
    xref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert pdf[xref:].startswith(b"xref\n")
    import zipfile

    with zipfile.ZipFile(io.BytesIO(probe_docx("HIMARI-PROBE"))) as archive:
        assert "HIMARI-PROBE" in archive.read("word/document.xml").decode()


# --- the toolkit folder --------------------------------------------------------------------


def make_toolkit(base: Path, version: str = "v1") -> Path:
    folder = base / version
    (folder / "site-packages" / "pkg").mkdir(parents=True)
    (folder / "site-packages" / "pkg" / "__init__.py").write_text("")
    (folder / "fonts").mkdir()
    (folder / "MANIFEST.json").write_text(json.dumps({"version": version, "fonts": ["A.ttf"]}))
    (base / "current").symlink_to(version)
    for path in (base, *base.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o022)
    return folder


def test_a_good_toolkit(tmp_path: Path) -> None:
    base = tmp_path / "documents"
    folder = make_toolkit(base)
    toolkit = open_toolkit(str(base), owners=ME)
    try:
        assert toolkit.path == str(base / "v1") and toolkit.version == "v1" and toolkit.manifest["fonts"] == ["A.ttf"]
        assert toolkit.same() and toolkit.site_packages == str(folder / "site-packages")
        assert worker_roots(toolkit)[0] == toolkit.path
        (base / "current").unlink()
        (base / "current").symlink_to("v2")  # a newer install: this server keeps its folder
        assert toolkit.same()
        os.rename(folder, base / "old")
        folder.mkdir()  # the folder was replaced: not the one checked
        assert not toolkit.same()
    finally:
        toolkit.close()


@pytest.mark.parametrize("spoil", ["missing", "relative", "no_link", "link_with_path", "deep_writable",
                                   "inner_link", "base_writable", "no_manifest", "not_root"])
def test_toolkits_that_are_refused(tmp_path: Path, spoil: str) -> None:
    base = tmp_path / "documents"
    folder = make_toolkit(base)
    owners = ME
    where = str(base)
    if spoil == "missing":
        where = str(tmp_path / "absent")
    elif spoil == "relative":
        where = "documents"
    elif spoil == "no_link":
        (base / "current").unlink()
        (base / "current").mkdir()
    elif spoil == "link_with_path":
        (base / "current").unlink()
        (base / "current").symlink_to(str(folder))
    elif spoil == "deep_writable":
        (folder / "site-packages" / "pkg").chmod(0o777)  # another user could plant a module (review C3)
    elif spoil == "inner_link":
        (folder / "site-packages" / "evil").symlink_to("/etc")
    elif spoil == "base_writable":
        base.chmod(0o777)
    elif spoil == "no_manifest":
        (folder / "MANIFEST.json").unlink()
    elif spoil == "not_root":
        owners = frozenset()  # production: root only, and these files are the test user's
    with pytest.raises(ToolkitRefused):
        open_toolkit(where, owners=owners)


def test_closing_twice_is_fine(tmp_path: Path) -> None:
    make_toolkit(tmp_path / "d")
    toolkit = open_toolkit(str(tmp_path / "d"), owners=ME)
    toolkit.close()
    toolkit.close()
    assert isinstance(toolkit, Toolkit) and toolkit.fd == -1


def test_more_parts_than_the_budget_pays_for() -> None:
    # Review B2: each part costs PART_OVERHEAD of the budget; an answer with more is bad.
    many = [part(n, 0, 0, 0, flags=["no_text"]) for n in range(1, 6)]
    _checked(lines(head(many)), ALL, None, False, 4 * 256)
    with pytest.raises(ValueError):
        _checked(lines(head(many)), ALL, None, False, 3 * 256)


def test_units_and_pictures_are_bounded() -> None:
    # Review C1: a claimed count drives loops: past MAX_UNITS it is a bad answer.
    with pytest.raises(ValueError):
        _checked(lines(head([], units=10**12)), ALL, None, False, 1000)
    with pytest.raises(ValueError):
        _checked(lines(head([], picture_total=10**12)), None, ((1, None),), False, 1000)


def test_no_progress_on_a_unit_of_unknown_length() -> None:
    # Re-check N1 (the earlier case failed on its start first): same start, no progress, length unknown.
    with pytest.raises(ValueError, match="no progress"):
        _checked(lines(head([part(1, 2, 2, None)])), (Piece(1, 1, 2),), None, False, 1000)
    _checked(lines(head([part(1, 2, 3, None)])), (Piece(1, 1, 2),), None, False, 1000)


def test_odd_entries_are_a_bad_answer_not_a_crash() -> None:
    with pytest.raises(AttributeError):  # read() turns this into "a bad answer" (review N2)
        _checked(lines(head([], skipped=["x"])), ALL, None, False, 1000)
