"""Install the documents toolkit (V3-P70 §6.2), as root, at deploy time.

    python3 scripts/install_documents.py /opt/himari-v3/documents
    python3 scripts/install_documents.py /opt/himari-v3/documents --prune

The toolkit is the libraries and fonts the documents worker and
``run_command`` share: exact versions with wheel hashes
(``scripts/documents-requirements.txt``), installed for ``/usr/bin/python3``
— the interpreter both sandboxes run — and NanumGothic from a pinned
google/fonts commit, checked by SHA-256.

A new version goes into its own folder ``BASE/<version>`` and only then
does the ``current`` link move to it (one rename). An existing version
folder is never changed and installing never deletes one: a running server
keeps the folder it checked at startup until it restarts. ``--prune``
removes versions other than ``current`` that no process holds open.
Point the server at BASE with HIMARI_DOCUMENTS_DIR and restart it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PYTHON = "/usr/bin/python3"
REQUIREMENTS = Path(__file__).with_name("documents-requirements.txt")
FONTS_COMMIT = "133ccbee9a8b408eb71f31a36ccb9116f5c695ad"  # google/fonts, ofl/nanumgothic
FONTS = {
    "NanumGothic-Regular.ttf": "76f45ef4a6bcff344c837c95a7dcc26e017e38b5846d5ae0cdcb5b86be2e2d31",
    "NanumGothic-Bold.ttf": "f96298f9fb18e364d2370f4c3ce948ac67a2b61af992d7234bc15c42b033c674",
    "OFL.txt": "eeacf16032901d0ed0456876ec77b8f0fda6b3fecec7d972f8543eb602e6c30f",
}
MODULES = ("pypdfium2", "pypdf", "docx", "openpyxl", "pptx", "reportlab", "PIL")


class InstallError(Exception):
    pass


def run(argv: list[str]) -> None:
    completed = subprocess.run(argv, stdin=subprocess.DEVNULL, check=False)
    if completed.returncode != 0:
        raise InstallError(f"{' '.join(argv[:4])} … failed (exit {completed.returncode})")


def version_name() -> str:
    spec = REQUIREMENTS.read_bytes() + json.dumps(FONTS, sort_keys=True).encode() + FONTS_COMMIT.encode()
    return f"{datetime.date.today():%Y%m%d}-{hashlib.sha256(spec).hexdigest()[:8]}"


def fetch_fonts(folder: Path) -> None:
    folder.mkdir()
    for name, digest in FONTS.items():
        url = f"https://raw.githubusercontent.com/google/fonts/{FONTS_COMMIT}/ofl/nanumgothic/{name}"
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — a fixed https URL
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise InstallError(f"{name}: the download does not match its pinned SHA-256")
        (folder / name).write_bytes(data)


def seal(root: Path) -> None:
    """No links, nothing writable by group or others (§6.2)."""
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"{path} is a symbolic link; the server refuses links in the toolkit")
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)


def install(base: Path) -> str:
    if not os.access(PYTHON, os.X_OK):
        raise InstallError(f"{PYTHON} is missing")
    if subprocess.run([PYTHON, "-m", "pip", "--version"], capture_output=True, check=False).returncode != 0:
        raise InstallError(f"{PYTHON} has no pip: install python3-pip first")
    base.mkdir(mode=0o755, parents=True, exist_ok=True)
    version = version_name()
    target = base / version
    if (target / "MANIFEST.json").is_file():
        switch(base, version)
        return f"{version} was already installed; current points to it"
    new = base / f".new-{version}-{os.getpid()}"
    try:
        site = new / "site-packages"
        new.mkdir(mode=0o755)
        run([PYTHON, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--disable-pip-version-check",
             "--target", str(site), "--only-binary=:all:", "--require-hashes", "--no-deps",
             "-r", str(REQUIREMENTS)])
        shutil.rmtree(site / "bin", ignore_errors=True)
        fetch_fonts(new / "fonts")
        run([PYTHON, "-m", "compileall", "-q", str(site)])  # without .pyc each read pays ~1 s of imports
        check = f"import sys; sys.path.insert(0, {str(site)!r}); import " + ", ".join(MODULES)
        run([PYTHON, "-I", "-S", "-c", check])
        python = subprocess.run([PYTHON, "-c", "import sys; print(sys.version.split()[0])"], capture_output=True,
                                text=True, check=True).stdout.strip()
        manifest = {"version": version, "python": python, "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                    "fonts": sorted(n for n in FONTS if n.endswith(".ttf")), "fonts_commit": FONTS_COMMIT}
        (new / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        seal(new)
        if target.exists():
            shutil.rmtree(target)  # an earlier run that stopped before its manifest: never a finished version
        new.rename(target)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    switch(base, version)
    return f"installed {version}; current points to it — restart the server to use it"


def switch(base: Path, version: str) -> None:
    link = base / f".current-{os.getpid()}"
    link.unlink(missing_ok=True)
    link.symlink_to(version)
    os.replace(link, base / "current")


def in_use(folder: Path, procs: Path = Path("/proc")) -> bool:
    """Whether any process holds a descriptor or a mapping inside ``folder``."""
    prefix = str(folder) + "/"
    for proc in procs.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                target = os.readlink(fd)
                if target == str(folder) or target.startswith(prefix):
                    return True
            with open(proc / "maps", encoding="utf-8", errors="replace") as maps:
                if any(prefix in line for line in maps):
                    return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise InstallError("run --prune as root: other processes cannot be checked") from None
    return False


def prune(base: Path, procs: Path = Path("/proc")) -> list[str]:
    current = os.readlink(base / "current") if (base / "current").is_symlink() else None
    removed = []
    for folder in sorted(base.iterdir()):
        # An install that stopped: never a finished version. One under an hour old may still be running.
        leftover = folder.name.startswith(".new-") and time.time() - folder.stat().st_mtime > 3600
        if folder.name == current or (folder.name.startswith(".") and not leftover) or folder.is_symlink() \
                or not folder.is_dir():
            continue
        if not in_use(folder, procs):
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", help="the toolkit folder, e.g. /opt/himari-v3/documents (HIMARI_DOCUMENTS_DIR)")
    parser.add_argument("--prune", action="store_true", help="remove versions other than current no process uses")
    args = parser.parse_args(argv)
    if not Path(args.base).is_absolute():
        parser.error("the folder must be an absolute path")
    base = Path(args.base).resolve()  # /proc shows resolved paths: compare like with like (review N7)
    os.umask(0o022)  # readable by the service user whatever root's umask is
    if os.geteuid() != 0:
        print("note: not root — the server accepts only a toolkit owned by root", file=sys.stderr)
    try:
        if args.prune:
            removed = prune(base)
            print("removed: " + (", ".join(removed) if removed else "nothing"))
        else:
            print(install(base))
    except InstallError as error:
        print(f"install_documents: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
