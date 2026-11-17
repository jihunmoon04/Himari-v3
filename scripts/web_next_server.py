"""A real HIMARI Web server for the new page's browser checks (V3-P102 W1, ``web/e2e``).

Runs the built page (``src/himari/interfaces/web/dist``, from ``npm run build`` in ``web/``) on a temporary
private data root with a scripted model, and writes the operator token to ``--token-out`` so the browser checks
can sign in. The script, in order: a tool call (an unknown tool: its result is an error), a question
(``ask_user``), an answer with Markdown, hostile markup and a formula, then plain answers ("Answer 1." …). Every
step is streamed a few characters at a time after a moment of thinking (V3-P73). The hub is a scripted stand-in
(``scripts/web_next_hub.py``).

    python -I scripts/web_next_server.py --port 8798 --token-out web/.e2e-token

Not part of pytest. Stops on Ctrl+C; with ``--exit-with-stdin`` also when its stdin closes (started through
``wsl.exe`` from Windows, whose end does not reach the Linux process otherwise).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import (  # noqa: E402
    NativeInvocationOutcome, NativePreparedRequest, NormalizedFinal, NormalizedToolCalls, OutputSink, RawCapture,
)
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_next_hub import MCP_TOKEN, TOKEN, FakeHub  # noqa: E402

# V3-P102 W4: a message carrying one of these marks has HIMARI hand that work to the hub (its hub_delegate), then
# say so. Each mark is acted on once, whatever else the script holds.
HAND_OVERS = {
    "W4-HAND-OVER": {"project": "work", "prompt": "Write the release notes", "agent": "claude"},
    "W4-FAILS": {"project": "work", "prompt": "Make the build fail on purpose", "agent": "claude"},
    "W4-NOWHERE": {"project": "nowhere", "prompt": "Anything at all"},
}
MCP_TOKEN_VAR = "HIMARI_E2E_HUB_MCP_TOKEN"


class HandingOver(ScriptedAdapter):
    """The scripted model, which hands work to the hub when a message carries a mark of ``HAND_OVERS``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._handed: set[str] = set()

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        body = request.body.decode("utf-8", "replace")
        if "W6B-REMEMBER" in body and "W6B-REMEMBER" not in self._handed:
            self._handed.add("W6B-REMEMBER")
            self._script[0:0] = [
                NormalizedToolCalls((ToolCall(CallId("remember-1"), 0, ToolId("memory_save"),
                                              {"category": "preference", "text": "The user likes green tea."}),)),
                NormalizedFinal((TextBlock("I will remember that."),)),
            ]
        for mark, args in HAND_OVERS.items():
            if mark in body and mark not in self._handed:
                self._handed.add(mark)
                self._script[0:0] = [  # before the rest of the script: these two answer this message
                    NormalizedToolCalls((ToolCall(CallId(f"hand-{len(self._handed)}"), 0, ToolId("hub/hub_delegate"),
                                                  args),)),
                    NormalizedFinal((TextBlock(f"I handed it to the hub ({mark})."),)),
                ]
                break
        return await super().invoke_prepared(request, sink, capture)

ANSWER = ("# Plan\n\n**Day 1**: walk <script>window.__pwned = 1</script> [bad](javascript:window.__pwned=2) and "
          "[good](https://example.com)\n\n- palace\n- market\n\nArea $\\pi r^2$ and\n$$\n\\frac{a}{b}\n$$\n")


def scripted() -> ScriptedAdapter:
    return HandingOver([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("machine/read_file"), {"path": "plan.txt"}),)),
        NormalizedToolCalls((ToolCall(CallId("c1"), 0, ASK_USER_TOOL_ID, {"question": "Which **city**?"}),)),
        NormalizedFinal((TextBlock(ANSWER),)),
        *[NormalizedFinal((TextBlock(f"Answer {n}. " + "More words as they stream in. " * 12),)) for n in range(1, 20)],
    ], streaming=True, piece_delay=0.01, first_delay=2.5)  # streamed, after a moment of "thinking"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--token-out", required=True)
    parser.add_argument("--exit-with-stdin", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="himari-next-"))
    data = root / "data"
    origin = f"http://127.0.0.1:{args.port}"
    hub = FakeHub()  # V3-P102 W2: the hub's user path, scripted (scripts/web_next_hub.py)
    root.mkdir(parents=True, exist_ok=True)
    (root / "user.token").write_text(TOKEN, encoding="ascii")
    # W4: the hub's MCP side as HIMARI's mcp.json names it ("hub", the page's default hub source)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    mcp = data / "mcp.json"
    mcp.write_text(json.dumps({"servers": {"hub": {
        "url": f"{hub.url}/mcp", "token_env": MCP_TOKEN_VAR,
        "tools": {"hub_delegate": {"effect": "read"}, "hub_task": {"effect": "read"}}}}}), encoding="utf-8")
    mcp.chmod(0o600)
    os.environ[MCP_TOKEN_VAR] = MCP_TOKEN
    config = WebConfig("127.0.0.1", args.port, frozenset({"127.0.0.1"}), frozenset({origin}), False, str(data),
                       "scripted-model", None, False, hub_url=hub.url, hub_user_token_file=str(root / "user.token"))

    async def opener() -> Application:
        application = await Application.open(data, scripted(), model_id="scripted-model", sandbox=False,
                                             cleanup_seconds=None)
        out = Path(args.token_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(application.operator_secret, encoding="ascii")
        return application

    server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=args.port,
                                           log_level="warning"))

    def stop_on_stdin_close() -> None:
        try:
            sys.stdin.read()
        finally:
            server.should_exit = True

    if args.exit_with_stdin:
        threading.Thread(target=stop_on_stdin_close, daemon=True).start()
    try:
        asyncio.run(server.serve())
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
