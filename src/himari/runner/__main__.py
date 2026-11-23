"""``python -m himari.runner --config /etc/himari-runner/runner.toml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from himari.runner.config import ConfigError, load
from himari.runner.hidden import HiddenRules
from himari.runner.hostfs import Host
from himari.runner.server import Runner, listener


async def main(path: str) -> int:
    try:
        config = load(path)
    except (OSError, ConfigError) as error:
        print(f"himari-runner: {error}", file=sys.stderr)
        return 2
    os.makedirs(os.path.join(config.himari_home, "conversations"), mode=0o755, exist_ok=True)
    runner = Runner(config, Host(HiddenRules(config.home, config.visible, config.hidden), config.himari_home))
    logging.getLogger("himari.runner").info("serving uid %s", config.client_uid)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    sock = listener(config)
    try:
        await runner.serve(sock, stop)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="himari-runner")
    parser.add_argument("--config", default="/etc/himari-runner/runner.toml")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(parser.parse_args().config)))
