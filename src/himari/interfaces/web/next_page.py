"""The new Web page while it grows beside the old one (V3-P102 §7.2, W1).

``web/`` (React, TypeScript, Vite) builds into this package's ``dist/``:
``index.html`` and hashed files under ``assets/``. The server serves it at
``/next/``:

- ``/next/assets/<name>``: only the names that were in ``dist/assets`` when
  the server started (never a path into the package), with a long cache
  (the names carry their content hash).
- any other ``GET /next/...``: ``index.html``, the page reads the address
  (SPA fallback). ``/api`` is not under ``/next``.
- each ``index.html`` carries a fresh style nonce, in its ``csp-nonce`` meta
  for the page and in ``request.state`` for the security middleware, which
  writes the one CSP header with it (``security.style_nonce_csp``).

Without a build (``dist/index.html`` missing) ``/next/`` is 404 and the
server logs it once; the old page is unaffected. KaTeX is still loaded from
the old page's ``/assets/katex/`` until W6 moves it into the build.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable

from himari.interfaces.web.security import new_style_nonce

log = logging.getLogger(__name__)

BASE = "/next/"
NONCE_PLACEHOLDER = "__HIMARI_CSP_NONCE__"
MEDIA_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff",
               ".png": "image/png", ".ico": "image/x-icon"}
ASSET_CACHE = "public, max-age=31536000, immutable"


@dataclass(frozen=True)
class Built:
    index: str  # with NONCE_PLACEHOLDER where the nonce goes
    assets: dict[str, tuple[Traversable, str]]  # name under assets/ → (file, media type)

    def page(self) -> tuple[str, str]:
        """The page's HTML with a fresh nonce, and that nonce."""
        nonce = new_style_nonce()
        return self.index.replace(NONCE_PLACEHOLDER, nonce), nonce


def _walk(folder: Traversable, prefix: str = "") -> dict[str, tuple[Traversable, str]]:
    found: dict[str, tuple[Traversable, str]] = {}
    for item in folder.iterdir():
        name = f"{prefix}{item.name}"
        if item.is_dir():
            found.update(_walk(item, f"{name}/"))
            continue
        media = MEDIA_TYPES.get("." + item.name.rsplit(".", 1)[-1].lower()) if "." in item.name else None
        if media is not None:  # an unknown kind of file is never served
            found[name] = (item, media)
    return found


def load(dist: Traversable | None = None) -> Built | None:
    """The build, read once at start; None (and a log line) without one."""
    root = dist if dist is not None else resources.files("himari.interfaces.web") / "dist"
    index = root / "index.html"
    if not index.is_file():
        log.info("the new Web page is not built (no %s): /next/ is off", "dist/index.html")
        return None
    html = index.read_text(encoding="utf-8")
    if NONCE_PLACEHOLDER not in html:
        log.warning("dist/index.html has no %s: /next/ is off", NONCE_PLACEHOLDER)
        return None
    assets = root / "assets"
    return Built(html, _walk(assets) if assets.is_dir() else {})
