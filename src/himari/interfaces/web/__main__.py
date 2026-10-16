"""``python -m himari.interfaces.web [--env-file .env]``

Configuration comes from the environment (see ``config.py``):
``HIMARI_DATA_ROOT`` (required), ``HIMARI_WEB_HOST``/``PORT``,
``HIMARI_MODEL``, ``HIMARI_REASONING_EFFORT``, ``HIMARI_SANDBOX``,
``HIMARI_CONTEXT_TOKENS`` (request token budget; compaction starts at 80%),
``HIMARI_WEB_SANDBOX_PORT`` (Tool views' origin; started only when some
MCP server's views are shown) and ``OPENROUTER_API_KEY``. ``--env-file`` reads ``KEY=value`` lines first;
real environment variables win; the token variables named in
``<data_root>/mcp.json`` are read from the same place. Secrets are never
printed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Iterator

from himari.bootstrap import read_env_file
from himari.bootstrap.application import Application, TurnSettings, check_tool_sources, tool_views_configured
from himari.interfaces.web.config import WebConfig, WebConfigError

API_KEY_VAR = "OPENROUTER_API_KEY"


def turn_text(turn: TurnSettings) -> str:
    """One turn's guards (V3-P62), for the startup line."""
    budget = f"turn budget: {turn.turn_budget} tokens" if turn.turn_budget else "no turn budget"
    limit = f"at most {turn.max_runs} steps" if turn.max_runs else "no step limit"
    pause = f"pauses after {turn.unattended_runs} steps unread" if turn.unattended_runs else "never pauses unread"
    return f"{budget}, {limit}, {pause}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m himari.interfaces.web", description="Run the HIMARI web server.")
    parser.add_argument("--env-file", help="read KEY=value settings from this file (environment variables win)")
    args = parser.parse_args(argv)
    environment = {**(read_env_file(args.env_file) if args.env_file else {}), **os.environ}
    try:
        config = WebConfig.from_env(environment)
    except WebConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    api_key = environment.get(API_KEY_VAR, "").strip()
    if not api_key:
        print(f"configuration error: {API_KEY_VAR} is not set", file=sys.stderr)
        return 2
    try:
        check_tool_sources(config.data_root, environment)
        views = tool_views_configured(config.data_root, environment)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    import uvicorn

    from himari.interfaces.web.app import create_app
    from himari.interfaces.web.sandbox import SandboxSigner, create_sandbox_app
    from himari.provider.api import ProviderAdapter

    def provider() -> ProviderAdapter:
        from himari.bootstrap.providers import openrouter

        return openrouter(api_key)

    async def opener() -> Application:
        application = await Application.open(config.data_root, provider(), model_id=config.model_id,
                                             reasoning_effort=config.reasoning_effort, sandbox=config.sandbox,
                                             context_tokens=config.context_tokens, environ=environment,
                                             turn_budget=config.turn_budget, unattended_runs=config.unattended_runs,
                                             max_runs=config.max_runs)
        print(f"HIMARI web: http://{config.host}:{config.port}/ — sign in with the token in "
              f"{application.data_root / 'control' / 'operator.secret'} (sandbox: {application.sandbox_state.value}, "
              f"context budget: {application.context_tokens} tokens; {turn_text(application.turn)})",
              file=sys.stderr, flush=True)
        for status in application.tool_source_statuses:
            count = len(status.tool_ids)
            state = f"{count} tool{'' if count == 1 else 's'}" if status.available else "not connected"
            print(f"HIMARI web: MCP server {status.source}: {state}"
                  f" (default {'on' if status.enabled_by_default else 'off'})"
                  + "".join(f"\n  - {p}" for p in status.problems), file=sys.stderr, flush=True)
        return application

    # One key per process: a restart (the only way the MCP policy changes)
    # voids every view URL signed before it (V3-P58 §3.4).
    signer = SandboxSigner()
    main_server = uvicorn.Server(uvicorn.Config(create_app(config, opener, signer), host=config.host,
                                                port=config.port, log_level="info", server_header=False,
                                                proxy_headers=False))
    if not views:
        main_server.run()
        return 0

    class SandboxServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield  # the main server handles signals and stops this one when it stops

    sandbox_server = SandboxServer(uvicorn.Config(create_sandbox_app(sorted(config.allowed_hosts), signer),
                                                  host=config.host, port=config.view_port, log_level="warning",
                                                  server_header=False, proxy_headers=False))

    async def serve() -> None:
        sandbox = asyncio.create_task(sandbox_server.serve())
        try:
            await main_server.serve()
        finally:
            sandbox_server.should_exit = True
            await sandbox

    print(f"HIMARI web: Tool views from http://{config.host}:{config.view_port}/", file=sys.stderr, flush=True)
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
