"""``python -m himari.runner --config /etc/himari-runner/runner.toml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from himari.runner.cgroups import CgroupError, Commands, delegate
from himari.runner.config import ConfigError, load
from himari.runner.hidden import HiddenRules
from himari.runner.hostfs import Host
from himari.runner.server import Runner, listener
from himari.sandbox.machine import MachineSandbox


async def main(path: str) -> int:
    try:
        config = load(path)
    except (OSError, ConfigError) as error:
        print(f"himari-runner: {error}", file=sys.stderr)
        return 2
    commands: Commands | None = None
    if config.cgroup == "delegated":
        try:
            commands = delegate(config.memory_max, config.pids_max, config.runner_memory_low)
        except CgroupError as error:
            print(f"himari-runner: {error}", file=sys.stderr)
            return 2
    host = Host(HiddenRules(config.home, config.visible, config.hidden), config.himari_home)
    runner = Runner(config, MachineSandbox.discover(), host, commands)
    state = await runner.start()
    logging.getLogger("himari.runner").info("machine sandbox %s; serving uid %s", state.value, config.client_uid)
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
