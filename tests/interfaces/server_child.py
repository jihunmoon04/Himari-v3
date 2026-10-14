"""Child process: a real Web server with a scripted provider whose steps are
given as JSON ([{"final": text} | {"ask": question}]). Not a test module."""

from __future__ import annotations

import json
import sys

import uvicorn
from testkit import calls, final

from himari.bootstrap.application import Application
from himari.common import ASK_USER_TOOL_ID
from himari.interfaces.web.app import create_app
from himari.interfaces.web.config import WebConfig
from himari.provider.scripted import ScriptedAdapter, ScriptStep

root, port, steps = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
script: list[ScriptStep] = [final(s["final"]) if "final" in s else calls((ASK_USER_TOOL_ID, {"question": s["ask"]}))
                            for s in steps]


async def opener() -> Application:
    return await Application.open(root, ScriptedAdapter(script), model_id="m", sandbox=False)


config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}), False,
                   root, "m", None, False)
uvicorn.run(create_app(config, opener), host="127.0.0.1", port=port, log_level="warning")
