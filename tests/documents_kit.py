"""Helpers for the document worker's tests (V3-P70): running the worker as
the sandbox does (without the sandbox), making documents with the toolkit
(HIMARI_DOCUMENTS_TOOLKIT, opt-in), and HWPX packages.

A small HWPX package in Hancom's layout (from python-hwpx's Skeleton.hwpx,
Apache-2.0): ``mimetype`` first and stored, ``Contents/content.hpf`` with a
manifest and spine, sections of ``hp:p``/``hp:run``/``hp:t``, tables, a
picture in ``BinData`` and a footnote. Only the parts read_document reads."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import textwrap
import zipfile
from pathlib import Path
from typing import Any

from himari.documents import PYTHON as PYTHON, WORKER as WORKER


NS = ('xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
      'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"')
OPF = ('xmlns:opf="http://www.idpf.org/2007/opf/" xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf"')


def para(*runs: str) -> str:
    return '<hp:p id="0" paraPrIDRef="0" styleIDRef="0">' + "".join(runs) + "</hp:p>"


def text(value: str) -> str:
    return f'<hp:run charPrIDRef="0"><hp:t>{value}</hp:t></hp:run>'


def table(*rows: list[str]) -> str:
    cells = "".join("<hp:tr>" + "".join(f'<hp:tc><hp:subList>{para(text(c))}</hp:subList></hp:tc>' for c in row)
                    + "</hp:tr>" for row in rows)
    return f'<hp:run charPrIDRef="0"><hp:tbl rowCnt="{len(rows)}">{cells}</hp:tbl><hp:t/></hp:run>'


def picture(ref: str = "image1") -> str:
    return f'<hp:run charPrIDRef="0"><hp:pic id="1"><hc:img binaryItemIDRef="{ref}"/></hp:pic></hp:run>'


def footnote(value: str) -> str:
    return (f'<hp:run charPrIDRef="0"><hp:ctrl><hp:footNote number="1"><hp:subList>{para(text(value))}'
            "</hp:subList></hp:footNote></hp:ctrl></hp:run>")


def hwpx(sections: dict[str, str], spine: list[str], images: dict[str, bytes] | None = None) -> bytes:
    """``sections``: name (e.g. "section0") → body paragraphs; ``spine``: their order."""
    images = images or {}
    items = "".join(f'<opf:item id="{n}" href="Contents/{n}.xml" media-type="application/xml"/>' for n in sections)
    items += "".join(f'<opf:item id="{i}" href="BinData/{i}.png" media-type="image/png" isEmbeded="1"/>'
                     for i in images)
    refs = '<opf:itemref idref="header" linear="yes"/>' + "".join(f'<opf:itemref idref="{n}" linear="yes"/>'
                                                                 for n in spine)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip")  # first, stored
        archive.writestr("version.xml", '<?xml version="1.0" encoding="UTF-8"?><hv:HCFVersion/>')
        archive.writestr("Contents/header.xml", '<?xml version="1.0" encoding="UTF-8"?><hh:head/>')
        for name, body in sections.items():
            archive.writestr(f"Contents/{name}.xml",
                             f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><hs:sec {NS}>{body}</hs:sec>')
        for name, data in images.items():
            archive.writestr(f"BinData/{name}.png", data)
        archive.writestr("Contents/content.hpf", (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><opf:package {OPF}><opf:metadata/>'
            '<opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
            f'{items}</opf:manifest><opf:spine>{refs}</opf:spine></opf:package>'))
    return out.getvalue()


TOOLKIT = os.environ.get("HIMARI_DOCUMENTS_TOOLKIT", "")
SITE = os.path.join(TOOLKIT, "site-packages")


def make(tmp_path: Path, name: str, script: str) -> bytes:
    """A document made by ``script`` with the toolkit; ``OUT`` is its path."""
    out = tmp_path / name
    code = f"import sys; sys.path.insert(0, {SITE!r}); OUT = {str(out)!r}\n" + textwrap.dedent(script)
    subprocess.run([PYTHON, "-I", "-S", "-c", code], check=True)
    return out.read_bytes()


def worker(data: bytes, **request: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body = {"op": "read", "data": base64.b64encode(data).decode(), "pages": [{"first": 1, "last": None, "start": 0}],
            "pictures": None, "view": False, "text_budget": 60000, **request}
    done = subprocess.run([PYTHON, "-I", "-S", str(WORKER), SITE], input=json.dumps(body).encode() + b"\n",
                          capture_output=True, timeout=120, check=True)
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    return lines[0], lines[1:]
