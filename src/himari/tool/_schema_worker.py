"""Child process of ``validate_isolated``: one JSON document in on stdin
(``{"schema": ..., "value": ...}``), one JSON list of violations out on
stdout. It limits its own CPU time and memory first, so even if the parent's
wall-clock kill were late, a runaway validation ends by itself."""

from __future__ import annotations

import json
import resource
import sys

CPU_SECONDS = 10
MEMORY_BYTES = 1024 * 1024 * 1024


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    from himari.tool.schema import validate

    request = json.loads(sys.stdin.buffer.read())
    json.dump(validate(request["value"], request["schema"]), sys.stdout)


if __name__ == "__main__":
    main()
