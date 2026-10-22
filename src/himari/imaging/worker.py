"""The model-copy worker (V3-P67 §3.1): run alone, inside the image sandbox.

One request, one answer, each a line on stdin/stdout:

    in:  {"data": <base64 of the original>}
    out: {"ok": true, "media_type": "image/jpeg"|"image/png", "width": w, "height": h, "data": <base64>}
         {"ok": false, "reason": "..."}

It imports only the standard library and Pillow, never HIMARI: the sandbox
binds the Python runtime and this file, nothing else. Before anything is
decoded it limits itself (memory, CPU) and Pillow's pixel count, so a
decompression bomb or a codec bug ends this process, not the server.

The copy: EXIF orientation applied, the first frame of an animation, the
long edge at most LONG_EDGE (smaller images keep their size), re-encoded
without metadata — PNG when there is transparency, JPEG (quality 85)
otherwise; a PNG over MAX_OUTPUT_BYTES is flattened on white and made a JPEG.
"""

from __future__ import annotations

import base64
import io
import json
import resource
import sys

LONG_EDGE = 1568
MAX_PIXELS = 50_000_000
MAX_OUTPUT_BYTES = 3 * 1024 * 1024  # its base64 and the JSON around it fit one 4 MiB line
MEMORY_BYTES = 512 * 1024 * 1024
CPU_SECONDS = 10


def limit() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))


def answer(**fields: object) -> None:
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


def convert(original: bytes) -> dict[str, object]:
    from PIL import Image, ImageOps  # after the limits: decoding starts here

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow's own bomb check, besides the one below
    with Image.open(io.BytesIO(original)) as opened:
        if opened.width * opened.height > MAX_PIXELS:
            return {"ok": False, "reason": "too many pixels"}
        opened.seek(0)  # the first frame of an animation
        image = ImageOps.exif_transpose(opened.copy())
    alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if alpha else "RGB")
    if max(image.size) > LONG_EDGE:
        image.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if alpha:
        image.save(out, "PNG", optimize=True)
        if out.tell() > MAX_OUTPUT_BYTES:
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.getchannel("A"))
            image, alpha, out = flat, False, io.BytesIO()
    if not alpha:
        image.save(out, "JPEG", quality=85, optimize=True)
    if out.tell() > MAX_OUTPUT_BYTES:
        return {"ok": False, "reason": "the copy is too large"}
    return {"ok": True, "media_type": "image/png" if alpha else "image/jpeg", "width": image.width,
            "height": image.height, "data": base64.b64encode(out.getvalue()).decode("ascii")}


def main() -> None:
    limit()
    line = sys.stdin.buffer.readline()
    try:
        original = base64.b64decode(json.loads(line)["data"], validate=True)
    except (ValueError, KeyError, TypeError):
        answer(ok=False, reason="a bad request")
        return
    try:
        answer(**convert(original))
    except MemoryError:
        answer(ok=False, reason="out of memory")
    except Exception as error:  # noqa: BLE001 — any decode problem is "no copy", said in one word
        answer(ok=False, reason=type(error).__name__)


if __name__ == "__main__":
    main()
