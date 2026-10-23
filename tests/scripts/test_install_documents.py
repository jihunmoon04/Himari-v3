"""scripts/install_documents.py (V3-P70 §6.2): versions never change once
made, current moves in one rename, and --prune keeps what any process uses."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_documents", ROOT / "scripts" / "install_documents.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _installer()


def versions(base: Path, *names: str) -> None:
    for name in names:
        (base / name / "site-packages").mkdir(parents=True)
        (base / name / "MANIFEST.json").write_text("{}")


def fake_proc(tmp_path: Path, holds: dict[str, list[str]], maps: dict[str, str] | None = None) -> Path:
    """A /proc with one process per entry, holding descriptors to the given paths."""
    procs = tmp_path / "proc"
    for pid, targets in holds.items():
        (procs / pid / "fd").mkdir(parents=True)
        for number, target in enumerate(targets):
            (procs / pid / "fd" / str(number)).symlink_to(target)
        (procs / pid / "maps").write_text((maps or {}).get(pid, ""))
    (procs / "self").mkdir(parents=True, exist_ok=True)  # not a pid: skipped
    return procs


def test_switch_moves_current_in_one_rename(tmp_path: Path) -> None:
    versions(tmp_path, "a", "b")
    installer.switch(tmp_path, "a")
    assert os.readlink(tmp_path / "current") == "a"
    installer.switch(tmp_path, "b")
    assert os.readlink(tmp_path / "current") == "b"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b", "current"]  # no temporary link left


def test_prune_keeps_current_and_every_version_in_use(tmp_path: Path) -> None:
    base = tmp_path / "documents"
    versions(base, "a", "b", "c", "d")
    installer.switch(base, "d")
    # A server still on a (its descriptor), a worker mapping b's library; c is unused.
    procs = fake_proc(tmp_path, {"100": [str(base / "a")], "200": ["/dev/null"]},
                      {"200": f"7f00-7f01 r-xp 0 08:01 12 {base / 'b'}/site-packages/lib.so\n"})
    assert installer.prune(base, procs) == ["c"]
    assert sorted(p.name for p in base.iterdir()) == ["a", "b", "current", "d"]
    # The server restarted onto d: a and b go too.
    assert installer.prune(base, fake_proc(tmp_path / "later", {"100": [str(base / "d")]})) == ["a", "b"]


def test_a_version_folder_name_follows_what_it_holds() -> None:
    name = installer.version_name()
    assert len(name.split("-")) == 2 and len(name.split("-")[1]) == 8


def test_seal_refuses_links_and_clears_write_bits(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    (tmp_path / "f").chmod(0o666)
    installer.seal(tmp_path)
    assert (tmp_path / "f").stat().st_mode & 0o022 == 0
    (tmp_path / "link").symlink_to("f")
    with pytest.raises(installer.InstallError):
        installer.seal(tmp_path)


def test_the_requirements_pin_every_package_with_hashes() -> None:
    lines = (ROOT / "scripts" / "documents-requirements.txt").read_text().splitlines()
    pins = [line.split(" ")[0] for line in lines if line and not line.startswith(("#", " "))]
    assert all("==" in pin for pin in pins) and len(pins) == 12
    assert {pin.split("==")[0].lower() for pin in pins} >= {"pypdfium2", "pypdf", "python-docx", "openpyxl",
                                                           "python-pptx", "reportlab", "pillow", "lxml"}
    assert all(line.strip().startswith("--hash=sha256:") for line in lines if line.startswith(" "))


def test_prune_leaves_a_fresh_install_in_progress(tmp_path: Path) -> None:
    # Re-review N3: a .new-* folder under an hour old may be an install still running.
    base = tmp_path / "documents"
    versions(base, "a")
    installer.switch(base, "a")
    (base / ".new-b-1").mkdir()
    old = base / ".new-c-2"
    old.mkdir()
    os.utime(old, (0, 0))
    assert installer.prune(base, fake_proc(tmp_path, {})) == [".new-c-2"]
    assert (base / ".new-b-1").exists()
