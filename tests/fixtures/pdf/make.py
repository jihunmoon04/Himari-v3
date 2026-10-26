"""Makes the PDFs scripts/pdf_smoke.mjs reads (V3-P77 §11, stage 1); the output is committed.

Run with a Python that has pikepdf (not a HIMARI dependency):  python make.py

Both files hold the same three pages, one PDF with a cross-reference table, one with a
cross-reference stream and its objects in object streams:
1. text and a black bar (the highlighter check), an annotation of another app (a Square
   with its appearance) and a link;
2. turned a quarter (/Rotate 90);
3. a CropBox smaller than its MediaBox.
The document has a JavaScript open action: the viewer must never run it.

Two more, of one page: ``encrypted.pdf`` opens without a password but is encrypted (an owner
password only: viewed, never written to, §3.3), ``locked.pdf`` needs one (password "himari").
"""

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

HERE = Path(__file__).parent


def page(pdf: pikepdf.Pdf, text: str, extra: bytes = b"") -> pikepdf.Page:
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
    content = b"BT /F1 24 Tf 72 700 Td (" + text.encode() + b") Tj ET\n" + extra
    pdf.add_blank_page(page_size=(612, 792))
    added = pdf.pages[-1]
    added.obj.Resources = Dictionary(Font=Dictionary(F1=font))
    added.obj.Contents = pdf.make_stream(content)
    return added


def main() -> None:
    pdf = pikepdf.new()
    first = page(pdf, "HIMARI ink fixture, page 1", b"0 0 0 rg 72 500 300 40 re f\n")
    square_ap = pdf.make_stream(b"1 0 0 RG 2 w 1 1 98 48 re S\n", Type=Name.XObject, Subtype=Name.Form,
                                BBox=Array([0, 0, 100, 50]))
    square = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Square, Rect=Array([400, 400, 500, 450]), NM=String("other-app-1"),
        C=Array([1, 0, 0]), F=4, AP=Dictionary(N=square_ap)))
    link = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=Array([72, 690, 300, 730]), Border=Array([0, 0, 0]),
        A=Dictionary(S=Name.URI, URI=String("https://example.com/"))))
    first.obj.Annots = pdf.make_indirect(Array([square, link]))  # an indirect array, as some writers do
    turned = page(pdf, "HIMARI ink fixture, page 2 (rotated)")
    turned.obj.Rotate = 90
    cropped = page(pdf, "HIMARI ink fixture, page 3 (cropped)")
    cropped.obj.CropBox = Array([50, 50, 562, 742])
    pdf.Root.OpenAction = Dictionary(S=Name.JavaScript, JS=String("app.alert('scripts must not run');"))
    pdf.save(HERE / "table.pdf", object_stream_mode=pikepdf.ObjectStreamMode.disable, deterministic_id=True,
             compress_streams=False)
    pdf.save(HERE / "stream.pdf", object_stream_mode=pikepdf.ObjectStreamMode.generate, deterministic_id=True)

    small = pikepdf.new()
    page(small, "HIMARI encrypted fixture")
    small.save(HERE / "encrypted.pdf", encryption=pikepdf.Encryption(owner="owner", user="", R=6))
    small.save(HERE / "locked.pdf", encryption=pikepdf.Encryption(owner="owner", user="himari", R=6))


if __name__ == "__main__":
    main()
