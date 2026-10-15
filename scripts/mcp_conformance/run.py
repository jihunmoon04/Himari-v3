"""Run the official MCP conformance suite with HIMARI as the client.

    python -I scripts/mcp_conformance/run.py [--scenario NAME] [-o DIR]

Needs Node.js (npx) and network access to the npm registry for the pinned
suite version. The whole client suite runs by default against the baseline
of expected failures (``baseline.yml``); exit status 0 means no regression
and no stale baseline entry. Not part of pytest (docs/testing-policy.md
§2.3).
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SUITE = "@modelcontextprotocol/conformance@0.1.16"
HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", help="one scenario instead of the whole suite (no baseline)")
    parser.add_argument("-o", "--output-dir", help="where the suite saves its results (default: a temporary folder)")
    args = parser.parse_args(argv[1:])
    command = shlex.join([sys.executable, "-I", str(HERE / "client.py")])
    with tempfile.TemporaryDirectory(prefix="himari-conformance-results-") as tmp:
        cmd = ["npx", "--yes", SUITE, "client", "--command", command, "-o", args.output_dir or tmp]
        if args.scenario:
            cmd += ["--scenario", args.scenario]
        else:
            cmd += ["--suite", "all", "--expected-failures", str(HERE / "baseline.yml")]
        return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
