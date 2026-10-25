"""Static module-boundary check for src/himari (V3-046).

Rules:
1. Across packages, only ``himari.common`` and ``himari.<pkg>.api`` may be imported.
2. Imports go downward only: a package may import packages with a lower layer.
3. An ``api`` module imports only stdlib, ``himari.common`` and other ``api``
   modules — never its own package's implementations or plugins.
4. ``himari.bootstrap`` (composition root) is exempt from rules 1 and 2.
5. ``himari.interfaces`` (Web/CLI) sits above the composition root: it may
   import ``himari.common``, ``api`` modules and ``himari.bootstrap``
   (the assembled Application) — nothing else of another package.
6. A package in ``ONLY_FROM`` may import only the listed packages, however
   low their layer: the MCP connector uses ``common`` and ``tool.api`` and
   nothing else, Data included (V3-P57, PR #25 review B1); the Sandbox uses
   ``common`` only (V3-P59 §3); the image copies use ``common``,
   ``sandbox`` and ``data`` (V3-P67).
7. ``himari.runner`` is the composition root of its own process (V3-P74):
   it may import the sandbox's modules, not only its api, and nothing but
   ``common`` and ``sandbox``. Others reach it through ``runner.api``.

Usage: python -I scripts/check_imports.py [src_root]
Exit status 1 on any violation. This is a static check, not part of pytest
selection (docs/testing-policy.md).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

LAYERS = {
    "common": 0,
    "sandbox": 1,  # the isolation mechanism: imports common only (rule 6), used by consumers via its api
    "runner": 2,  # the machine runner's process (V3-P74): the sandbox's profiles; others use runner.api
    "data": 1,
    "tool": 2,
    "imaging": 2,  # model copies of images (V3-P67): the sandbox and Data's file types, nothing else
    "documents": 2,  # reading documents (V3-P70): the sandbox and Data's file types, nothing else
    "environment": 3,
    "mcp": 3,  # a connector plugin: provides Tools through tool.api only, like an Environment
    "prompt": 4,
    "provider": 5,
    "control": 6,
    "bootstrap": 7,
    "interfaces": 8,
}
# Packages whose dependencies are narrower than their layer allows (rule 6).
ONLY_FROM = {
    "mcp": frozenset({"common", "tool"}),
    "sandbox": frozenset({"common"}),
    "imaging": frozenset({"common", "sandbox", "data"}),
    "documents": frozenset({"common", "sandbox", "data"}),
    "runner": frozenset({"common", "sandbox"}),
}
# Packages that may import another package's private modules (rule 7).
PRIVATE_FROM = {"runner": frozenset({"sandbox"})}


def module_name(src_root: Path, path: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported_modules(tree: ast.Module, current: str, is_package: bool) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    pkg_parts = current.split(".") if is_package else current.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target in ("himari",) or (target.startswith("himari.") and target.count(".") == 1):
                # "from himari.x import api" / "from himari import common": the
                # imported names are submodules, so judge each one.
                found.extend((node.lineno, f"{target}.{alias.name}") for alias in node.names)
            else:
                found.append((node.lineno, target))
    return found


def top_package(name: str) -> str | None:
    parts = name.split(".")
    if parts[0] != "himari" or len(parts) < 2:
        return None
    return parts[1]


def check(src_root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted((src_root / "himari").rglob("*.py")):
        current = module_name(src_root, path)
        pkg = top_package(current)
        if pkg is None:
            continue
        if pkg not in LAYERS:
            errors.append(f"{path}: package himari.{pkg} has no layer in LAYERS")
            continue
        is_api = current.endswith(".api") or pkg == "common"
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, target in imported_modules(tree, current, path.name == "__init__.py"):
            tpkg = top_package(target)
            if tpkg is None:
                continue
            where = f"{path}:{lineno}: {current} imports {target}"
            if tpkg not in LAYERS:
                errors.append(f"{where} (unknown package)")
                continue
            target_is_public = tpkg == "common" or target == f"himari.{tpkg}.api"
            if pkg == "interfaces" and tpkg == "bootstrap":
                target_is_public = True  # the assembled Application is the interfaces' entry point
            if is_api and not target_is_public:
                errors.append(f"{where} (api modules may import only common and other api modules)")
            if pkg == "bootstrap" or tpkg == pkg:
                continue
            if pkg in ONLY_FROM and tpkg not in ONLY_FROM[pkg]:
                allowed = ", ".join(sorted(ONLY_FROM[pkg]))
                errors.append(f"{where} ({pkg} may import only {allowed})")
                continue
            if not target_is_public and tpkg not in PRIVATE_FROM.get(pkg, frozenset()):
                errors.append(f"{where} (private import across modules)")
            elif LAYERS[tpkg] >= LAYERS[pkg]:
                errors.append(f"{where} (upward or sideways import)")
    return errors


def main(argv: list[str]) -> int:
    src_root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "src"
    errors = check(src_root)
    for e in errors:
        print(e)
    print(f"import boundaries: {'FAIL' if errors else 'OK'} ({len(errors)} violation(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
