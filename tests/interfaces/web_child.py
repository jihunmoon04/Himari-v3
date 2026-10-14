"""Child process for test_web_durability.py: a real Web server (uvicorn,
SQLite) whose background jobs wait 30 s before doing anything, so the
test can kill it right after an HTTP 202. Not a test module."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import uvicorn
from testkit import final

from himari.bootstrap.application import Application
from himari.interfaces.web.app import create_app
from himari.interfaces.web.config import WebConfig
from himari.provider.scripted import ScriptedAdapter

root, port = sys.argv[1], int(sys.argv[2])


async def opener() -> Application:
    application = await Application.open(root, ScriptedAdapter([final("never")]), model_id="m", sandbox=False)
    driver: Any = application.runtime.driver
    for name in ("drive", "resume"):  # only the background work is slowed; accept is untouched
        original = getattr(driver, name)

        async def slow(*args: Any, _original: Any = original) -> Any:
            await asyncio.sleep(30)
            return await _original(*args)

        setattr(driver, name, slow)
    return application


config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}), False,
                   root, "m", None, False)
uvicorn.run(create_app(config, opener), host="127.0.0.1", port=port, log_level="warning")
