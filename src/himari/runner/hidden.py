"""The hidden list (V3-P74 §7): what the file Tools do not see.

- In the user's home, every entry whose name starts with "." is hidden,
  except the visible ones (``.gitconfig``, ``.local/bin``, ``.local/lib``
  by default). A folder that only *holds* visible entries (``.local``)
  stays, and its other children are hidden one by one.
- The operator's extra paths (HIMARI's data and settings, the runner's own).

Masks are computed from the filesystem as it is now, for every file
operation. A hidden entry that is a symbolic link is masked at its target:
a path reached through another link to it stays hidden. Other users'
homes are left to the OS permissions: hiding them wholesale would also
hide the cloud folders mounted under them (V3-P74 §14).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass


def _parts(relative: str) -> tuple[str, ...]:
    return tuple(p for p in relative.strip("/").split("/") if p)


def under(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or below it, by whole components."""
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/") or root == "/"


@dataclass(frozen=True)
class HiddenRules:
    home: str
    visible: tuple[str, ...] = (".gitconfig", ".local/bin", ".local/lib")  # relative to home
    extra: tuple[str, ...] = ()  # absolute paths

    def _visible_parts(self) -> tuple[tuple[str, ...], ...]:
        return tuple(_parts(v) for v in self.visible)

    def by_rule(self, real: str) -> bool:
        """Hidden by the rules alone (no filesystem access)."""
        if any(under(real, os.path.realpath(p)) or under(real, p) for p in self.extra):
            return True
        if not under(real, self.home) or real == self.home:
            return False
        rel = _parts(real[len(self.home):])
        if not rel or not rel[0].startswith("."):
            return False
        for parts in self._visible_parts():
            if rel[: len(parts)] == parts or parts[: len(rel)] == rel:
                return False  # inside a visible entry, or a folder that holds one
        return True

    def is_hidden(self, real: str, masks: tuple[str, ...] | None = None) -> bool:
        """``real`` is a real path (no link in it)."""
        if self.by_rule(real):
            return True
        return any(under(real, m) for m in (self.masks() if masks is None else masks))

    def masks(self) -> tuple[str, ...]:
        """The real paths of every hidden entry that exists, a link's at its target."""
        found: set[str] = set()

        def mask(path: str) -> None:
            try:
                info = os.lstat(path)
            except OSError:
                return
            if stat.S_ISLNK(info.st_mode):
                target = os.path.realpath(path)
                if not os.path.exists(target):
                    return  # a dangling link shows nothing
                path = target
            if path not in found and not any(under(path, m) for m in found):
                found.difference_update([inner for inner in found if under(inner, path)])  # a folder covers them
                found.add(path)

        visible = self._visible_parts()

        def walk(rel: tuple[str, ...]) -> None:
            path = os.path.join(self.home, *rel)
            if any(rel[: len(parts)] == parts for parts in visible):
                return
            if any(parts[: len(rel)] == rel for parts in visible):
                try:
                    if os.path.islink(path) or not os.path.isdir(path):
                        mask(path)
                        return
                    children = os.listdir(path)
                except OSError:
                    return
                for child in children:
                    walk((*rel, child))
                return
            mask(path)

        try:
            entries = os.listdir(self.home)
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith("."):
                walk((entry,))
        for path in self.extra:
            mask(path)
        return tuple(sorted(found))
