"""``python -m himari.interfaces.web [--env-file .env]``

Configuration comes from the environment (see ``config.py``):
``HIMARI_DATA_ROOT`` (required), ``HIMARI_WEB_HOST``/``PORT``,
``HIMARI_MODEL``, ``HIMARI_REASONING_EFFORT``, ``HIMARI_SANDBOX``,
``HIMARI_CONTEXT_TOKENS`` (request token budget; compaction starts at 80%)
and ``OPENROUTER_API_KEY``. ``--env-file`` reads ``KEY=value`` lines first;
real environment variables win. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys

from himari.bootstrap import read_env_file
from himari.bootstrap.application import Application
from himari.interfaces.web.config import WebConfig, WebConfigError

API_KEY_VAR = "OPENROUTER_API_KEY"


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

    import uvicorn

    from himari.interfaces.web.app import create_app
    from himari.provider.api import ProviderAdapter

    def provider() -> ProviderAdapter:
        from himari.bootstrap.providers import openrouter

        return openrouter(api_key)

    async def opener() -> Application:
        application = await Application.open(config.data_root, provider(), model_id=config.model_id,
                                             reasoning_effort=config.reasoning_effort, sandbox=config.sandbox,
                                             context_tokens=config.context_tokens)
        print(f"HIMARI web: http://{config.host}:{config.port}/ — sign in with the token in "
              f"{application.data_root / 'control' / 'operator.secret'} (sandbox: {application.sandbox_state.value}, "
              f"context budget: {application.context_tokens} tokens)",
              file=sys.stderr, flush=True)
        return application

    uvicorn.run(create_app(config, opener), host=config.host, port=config.port, log_level="info",
                server_header=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
