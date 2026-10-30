"""scripts/device_client.py (V3-P83 §9.4): the reference client's self test passes (CI runs it too)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_reference_device_client_runs_against_a_served_himari() -> None:
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "device_client.py"), "--self-test"],
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "device client: OK" in done.stdout
