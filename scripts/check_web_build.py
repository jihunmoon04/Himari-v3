"""Release check for the Web page (V3-P102 §7.2): the wheel carries the built page.

Run after ``npm ci && npm run build`` in ``web/``. Builds HIMARI's wheel into a temporary folder (``pip wheel
--no-deps``) and fails unless it holds ``himari/interfaces/web/dist/index.html`` with the nonce placeholder, every
``/assets/...`` file that page references, and KaTeX as the renderer loads it (``assets/katex/``). ``package-data``
alone does not build anything: a release made without the build would serve no page, and this makes that a failure
instead.

    python -I scripts/check_web_build.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = "himari/interfaces/web/dist/"
# what the renderer (web/src/lib/rich_text.js) loads on the first formula, and one of KaTeX's fonts
KATEX = ("assets/katex/katex.min.js", "assets/katex/katex.min.css", "assets/katex/fonts/KaTeX_Main-Regular.woff2")


def main() -> int:
    if not (ROOT / "src" / DIST / "index.html").is_file():
        print("no src/himari/interfaces/web/dist/index.html: run `npm ci && npm run build` in web/ first")
        return 1
    with tempfile.TemporaryDirectory() as out:
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", out,
                        str(ROOT)], check=True)
        wheels = list(Path(out).glob("himari-*.whl"))
        if len(wheels) != 1:
            print(f"expected one wheel, got {wheels}")
            return 1
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            index = DIST + "index.html"
            if index not in names:
                print(f"the wheel has no {index}")
                return 1
            html = wheel.read(index).decode("utf-8")
    problems = []
    if "__HIMARI_CSP_NONCE__" not in html:
        problems.append("index.html has no __HIMARI_CSP_NONCE__ placeholder")
    referenced = re.findall(r'(?:src|href)="/(assets/[^"]+)"', html)
    if not referenced:
        problems.append("index.html references no /assets/ file")
    problems += [f"the wheel lacks {DIST}{name}" for name in (*referenced, *KATEX) if DIST + name not in names]
    if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html):
        problems.append("index.html has an inline script (the CSP allows none)")
    for problem in problems:
        print(problem)
    if not problems:
        print(f"ok: {wheels[0].name} carries the page, its {len(referenced)} referenced files and KaTeX")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
