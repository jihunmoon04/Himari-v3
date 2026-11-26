"""``python -m himari.interfaces.web [--env-file .env]``

Configuration comes from the environment (see ``config.py``):
``HIMARI_DATA_ROOT`` (required), ``HIMARI_WEB_HOST``/``PORT``,
``HIMARI_MODEL`` (the default model), ``HIMARI_MODELS`` (the offered models, ``id[=effort]``),
``HIMARI_REASONING_EFFORT``, ``HIMARI_SANDBOX``,
``HIMARI_CONTEXT_TOKENS`` (request token budget; compaction starts at 80%),
``HIMARI_TRASH_DAYS`` (days a deleted Conversation stays in the trash, default 30),
``HIMARI_FILE_MAX_BYTES``, ``HIMARI_FILE_STORE_MAX_BYTES``, ``HIMARI_FILE_KEEP_DAYS`` (files, V3-P66),
``HIMARI_MEMORY`` (0 turns memory off, V3-P71),
``HIMARI_LIFE``, ``HIMARI_LIFE_WRITE_AT``, ``HIMARI_LIFE_DAILY_TOKENS`` (HIMARI's life, character-life v0.1),
``HIMARI_LOCATION``, ``HIMARI_LIFE_TOPICS`` (the weather and the day's topics, character-world v0.1),
``HIMARI_CONVERSATION_SEARCH`` (0 turns conversation search off, V3-P72),
and ``OPENROUTER_API_KEY``. ``--env-file`` reads ``KEY=value`` lines first;
real environment variables win; the token variables named in
``<data_root>/mcp.json`` are read from the same place. Secrets are never
printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from types import FrameType

from himari.bootstrap import read_env_file
from himari.bootstrap.application import Application, TurnSettings, check_tool_sources
from himari.interfaces.web.config import WebConfig, WebConfigError

API_KEY_VAR = "OPENROUTER_API_KEY"


def turn_text(turn: TurnSettings) -> str:
    """One turn's guard (V3-P106), for the startup line."""
    return f"at most {turn.max_runs} steps a turn"


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
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    import uvicorn

    from himari.interfaces.web.app import begin_shutdown, create_app
    from himari.provider.api import ProviderAdapter

    def provider() -> ProviderAdapter:
        from himari.bootstrap.providers import openrouter

        return openrouter(api_key)

    async def opener() -> Application:
        application = await Application.open(config.data_root, provider(), model_id=config.model_id,
                                             reasoning_effort=config.reasoning_effort, sandbox=config.sandbox,
                                             context_tokens=config.context_tokens, environ=environment,
                                             max_runs=config.max_runs, models=config.models,
                                             trash_days=config.trash_days, file_max_bytes=config.file_max_bytes,
                                             file_store_max_bytes=config.file_store_max_bytes,
                                             file_keep_days=config.file_keep_days,
                                             documents_dir=config.documents_dir, memory=config.memory,
                                             memory_auto=config.memory_auto, memory_model=config.memory_model,
                                             memory_idle_minutes=config.memory_idle_minutes,
                                             memory_daily_tokens=config.memory_daily_tokens,
                                             life=config.life, life_write_at=config.life_write_at,
                                             life_daily_tokens=config.life_daily_tokens,
                                             location=config.location, life_topics=config.life_topics,
                                             conversation_search=config.conversation_search,
                                             streaming=config.streaming, companion=config.companion,
                                             companion_default_model=config.companion_default_model,
                                             companion_models_only=config.companion_models_only,
                                             backup_model=config.backup_model,
                                             time_zone=config.time_zone,
                                             machine_principals=config.machine_principals,
                                             proactive=config.proactive)
        print(f"HIMARI web: http://{config.host}:{config.port}/ — sign in with the token in "
              f"{application.data_root / 'control' / 'operator.secret'} ("
              f"context budget: {application.context_tokens} tokens; {turn_text(application.turn)})",
              file=sys.stderr, flush=True)
        for model in application.models:
            effort = f", reasoning {model.default_effort}" if model.default_effort else ""
            print(f"HIMARI web: model {model.model_id}{' (default)' if model.is_default else ''}{effort}",
                  file=sys.stderr, flush=True)
        if application.backup_model is not None:  # character-day §10
            print(f"HIMARI web: backup model {application.backup_model} (when a call gets no answer at all)",
                  file=sys.stderr, flush=True)
        if not application.memory_enabled:  # V3-P71 §9
            print("HIMARI web: memory is off (HIMARI_MEMORY=0): models neither save nor see notes; "
                  "you can still manage them", file=sys.stderr, flush=True)
        print(f"HIMARI web: automatic memory {application.auto_memory}", file=sys.stderr, flush=True)  # V3-P85 §12
        print(f"HIMARI web: HIMARI's life {'on' if application.life_enabled else 'off'}, story writer "
              f"{application.auto_life}", file=sys.stderr, flush=True)  # character-life v0.1
        print(f"HIMARI web: weather {application.world_weather}, the day's topics {application.world_topics}",
              file=sys.stderr, flush=True)  # character-world v0.1
        print(f"HIMARI web: reaching out {application.proactive.describe()}", file=sys.stderr, flush=True)  # V3-P87
        if not application.conversation_search_enabled:  # V3-P72 §9
            print("HIMARI web: conversation search is off (HIMARI_CONVERSATION_SEARCH=0)", file=sys.stderr, flush=True)
        if not application.streaming_enabled:  # V3-P73 §9
            print("HIMARI web: streaming is off (HIMARI_STREAMING=0): answers arrive whole", file=sys.stderr,
                  flush=True)
        if application.machine is not None:  # V3-P74 §4.4, V3-P109
            print(f"HIMARI web: conversation folders in {application.machine.root} for "
                  f"{', '.join(sorted(application.machine_principals)) or 'nobody'}'s new conversations",
                  file=sys.stderr, flush=True)
        if config.machine_socket_ignored:  # V3-P109 §3 D9
            print("HIMARI web: HIMARI_MACHINE_SOCKET is no longer used (himari-runner was removed, V3-P109); "
                  "remove it", file=sys.stderr, flush=True)
        if not application.images_available:  # V3-P67 §3.1: fail-closed, said once at startup
            print("HIMARI web: images are not shown to models on this server (the image sandbox is unavailable): "
                  "an image is attached as a file line only", file=sys.stderr, flush=True)
        for unverified in application.unverified_models:  # V3-P64 §3, review B1
            print(f"HIMARI web: warning: model {unverified} is not declared by the provider: its window, reasoning "
                  "efforts and Tool calling are unverified (a model without Tool calling fails every request)",
                  file=sys.stderr, flush=True)
        for status in application.tool_source_statuses:
            count = len(status.tool_ids)
            state = f"{count} tool{'' if count == 1 else 's'}" if status.available else "not connected"
            print(f"HIMARI web: MCP server {status.source}: {state}"
                  f" (default {'on' if status.enabled_by_default else 'off'})"
                  + "".join(f"\n  - {p}" for p in status.problems), file=sys.stderr, flush=True)
        if application.store.backup_path is not None:  # V3-P65 §8.1
            print(f"HIMARI web: the database was upgraded; its previous version is kept in "
                  f"{application.store.backup_path} (delete it once a rollback is no longer needed: it keeps every "
                  "Conversation as it was, including ones deleted since)", file=sys.stderr, flush=True)
        return application

    web_app = create_app(config, opener)

    class MainServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            begin_shutdown(web_app)  # V3-P73: open live streams would hold the shutdown
            super().handle_exit(sig, frame)

    main_server = MainServer(uvicorn.Config(web_app, host=config.host, port=config.port, log_level="info",
                                            server_header=False, proxy_headers=False))
    main_server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
