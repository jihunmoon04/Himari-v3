"""A real HIMARI Web server for the new page's browser checks (V3-P102 W1, ``web/e2e``).

Runs the built page (``src/himari/interfaces/web/dist``, from ``npm run build`` in ``web/``) on a temporary
private data root with a scripted model, and writes the operator token to ``--token-out`` so the browser checks
can sign in. The script, in order: a tool call (an unknown tool: its result is an error), a question
(``ask_user``), an answer with Markdown, hostile markup and a formula, then plain answers ("Answer 1." …). Every
step is streamed a few characters at a time after a moment of thinking (V3-P73).

    python -I scripts/web_next_server.py --port 8798 --token-out web/.e2e-token

Not part of pytest. Stops on Ctrl+C; with ``--exit-with-stdin`` also when its stdin closes (started through
``wsl.exe`` from Windows, whose end does not reach the Linux process otherwise).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import ASK_USER_TOOL_ID, CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import NormalizedFinal, NormalizedToolCalls  # noqa: E402
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

ANSWER = ("# Plan\n\n**Day 1**: walk <script>window.__pwned = 1</script> [bad](javascript:window.__pwned=2) and "
          "[good](https://example.com)\n\n- palace\n- market\n\nArea $\\pi r^2$ and\n$$\n\\frac{a}{b}\n$$\n")


def scripted() -> ScriptedAdapter:
    return ScriptedAdapter([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("machine/read_file"), {"path": "plan.txt"}),)),
        NormalizedToolCalls((ToolCall(CallId("c1"), 0, ASK_USER_TOOL_ID, {"question": "Which **city**?"}),)),
        NormalizedFinal((TextBlock(ANSWER),)),
        *[NormalizedFinal((TextBlock(f"Answer {n}. " + "More words as they stream in. " * 12),)) for n in range(1, 20)],
    ], streaming=True, piece_delay=0.01, first_delay=0.8)  # streamed, after a moment of "thinking"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--token-out", required=True)
    parser.add_argument("--exit-with-stdin", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="himari-next-"))
    data = root / "data"
    origin = f"http://127.0.0.1:{args.port}"
    config = WebConfig("127.0.0.1", args.port, frozenset({"127.0.0.1"}), frozenset({origin}), False, str(data),
                       "scripted-model", None, False)

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
