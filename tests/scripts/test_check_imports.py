"""scripts/check_imports.py (V3-046): the rules hold on the real tree, and
the MCP connector may import only common and tool.api (V3-P57, PR #25
review B1)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_imports", ROOT / "scripts" / "check_imports.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_imports = _checker()


def _tree(tmp_path: Path, source: str) -> Path:
    package = tmp_path / "himari" / "mcp"
    package.mkdir(parents=True)
    (tmp_path / "himari" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text(source)
    return tmp_path


def test_the_source_tree_keeps_every_rule() -> None:
    assert check_imports.check(ROOT / "src") == []


def test_mcp_may_import_common_tool_api_and_itself(tmp_path: Path) -> None:
    source = ("from himari.common import JsonValue\n"
              "from himari.tool.api import ToolHandler\n"
              "from himari.mcp import client\n")
    assert check_imports.check(_tree(tmp_path, source)) == []


@pytest.mark.parametrize("line", [
    "from himari.data.api import ConversationLog",  # a lower layer's public api: still refused
    "from himari.data import api",
    "import himari.data.api",
    "from himari.environment.api import EnvironmentPlugin",
])
def test_mcp_may_not_import_other_public_apis(tmp_path: Path, line: str) -> None:
    errors = check_imports.check(_tree(tmp_path, line + "\n"))
    assert len(errors) == 1 and "mcp may import only common, tool" in errors[0]


def test_mcp_may_not_import_tool_internals(tmp_path: Path) -> None:
    errors = check_imports.check(_tree(tmp_path, "from himari.tool.schema import check_tool_input_schema\n"))
    assert len(errors) == 1 and "private import across modules" in errors[0]
