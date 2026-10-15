"""scripts/check_imports.py (V3-046): the rules hold on the real tree, and
the MCP connector may import only common and tool.api (V3-P57, PR #25
review B1), and the Sandbox only common (V3-P59 §3)."""

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


def _tree(tmp_path: Path, source: str, package_name: str = "mcp") -> Path:
    package = tmp_path / "himari" / package_name
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


@pytest.mark.parametrize("line", [
    "from himari.sandbox.api import SandboxState",  # V3-059: the connector gets its launcher injected
    "from himari.sandbox import backend",
])
def test_mcp_may_not_import_the_sandbox(tmp_path: Path, line: str) -> None:
    errors = check_imports.check(_tree(tmp_path, line + "\n"))
    assert len(errors) == 1 and "mcp may import only common, tool" in errors[0]


def test_the_sandbox_may_import_common_and_itself(tmp_path: Path) -> None:
    source = "from himari.common import JsonValue\nfrom himari.sandbox import landlock_helper\n"
    assert check_imports.check(_tree(tmp_path, source, "sandbox")) == []


@pytest.mark.parametrize("line", [
    "from himari.data.api import ConversationLog",  # same layer, and not common
    "from himari.tool.api import ToolHandler",
    "from himari.environment.api import EnvironmentPlugin",
])
def test_the_sandbox_may_import_only_common(tmp_path: Path, line: str) -> None:
    errors = check_imports.check(_tree(tmp_path, line + "\n", "sandbox"))
    assert len(errors) == 1 and "sandbox may import only common" in errors[0]


@pytest.mark.parametrize("line, allowed", [
    ("from himari.sandbox.api import WorkspaceSandbox", True),
    ("from himari.sandbox.backend import SandboxBackend", False),  # Bootstrap builds the backend
])
def test_an_environment_uses_the_sandbox_through_its_api(tmp_path: Path, line: str, allowed: bool) -> None:
    errors = check_imports.check(_tree(tmp_path, line + "\n", "environment"))
    assert errors == [] if allowed else (len(errors) == 1 and "private import across modules" in errors[0])

