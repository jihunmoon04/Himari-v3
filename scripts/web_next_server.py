"""A real HIMARI Web server for the new page's browser checks (V3-P102 W1, ``web/e2e``).

Runs the built page (``src/himari/interfaces/web/dist``, from ``npm run build`` in ``web/``) on a temporary
private data root with a scripted model, and writes the operator token to ``--token-out`` so the browser checks
can sign in. The script, in order: a tool call (an unknown tool: its result is an error), an answer with Markdown,
hostile markup and a formula, then plain answers ("Answer 1." …). Every step is streamed a few characters at a time
after a moment of thinking (V3-P73). A small MCP server ("notes", in this process) gives the conversations a Tool
server with a prompt.

    python -I scripts/web_next_server.py --port 8798 --token-out web/.e2e-token

Not part of pytest. Stops on Ctrl+C; with ``--exit-with-stdin`` also when its stdin closes (started through
``wsl.exe`` from Windows, whose end does not reach the Linux process otherwise).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from himari.bootstrap.application import Application  # noqa: E402
from himari.common import CallId, TextBlock, ToolCall, ToolId  # noqa: E402
from himari.interfaces.web.app import create_app  # noqa: E402
from himari.interfaces.web.config import WebConfig  # noqa: E402
from himari.provider.api import (  # noqa: E402
    NativeInvocationOutcome, NativePreparedRequest, NormalizedFinal, NormalizedToolCalls, OutputSink, RawCapture,
)
from himari.provider.scripted import ScriptedAdapter  # noqa: E402

# A message carrying one of these marks gets these steps as its answer, before the rest of the script. Each mark is
# acted on once.
MARKED = {
    "W6B-REMEMBER": [
        NormalizedToolCalls((ToolCall(CallId("remember-1"), 0, ToolId("memory_save"),
                                      {"category": "preference", "text": "The user likes green tea."}),)),
        NormalizedFinal((TextBlock("I will remember that."),)),
    ],
    "W6A-COPY": [NormalizedFinal((TextBlock("A **bold** answer (W6A-COPY)."),))],
}


class Marked(ScriptedAdapter):
    """The scripted model, which answers a message carrying a mark of ``MARKED`` with that mark's steps."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._done: set[str] = set()

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        body = request.body.decode("utf-8", "replace")
        for mark, steps in MARKED.items():
            if mark in body and mark not in self._done:
                self._done.add(mark)
                self._script[0:0] = steps
                break
        return await super().invoke_prepared(request, sink, capture)


class NotesServer:
    """A small MCP server over HTTP (``/mcp``): one prompt, ``review``, with an argument; no tools."""

    def __init__(self) -> None:
        notes = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                answer = notes.answer(message)
                data = b"" if answer is None else json.dumps(answer).encode()
                self.send_response(202 if answer is None else 200)  # 202: a notification
                if answer is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @staticmethod
    def answer(message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message of HIMARI's MCP client."""
        method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}, "prompts": {}}, "serverInfo": {"name": "notes", "version": "0"}}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "review", "title": "Review something",
                                   "description": "Ask for a careful review.",
                                   "arguments": [{"name": "topic", "description": "What to review", "required": True}]}]}
        elif method == "prompts/get":
            topic = (params.get("arguments") or {}).get("topic", "")
            result = {"messages": [{"role": "user", "content": {"type": "text", "text": f"Review {topic} carefully."}}]}
        elif method == "tools/list":
            result = {"tools": []}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


ANSWER = ("# Plan\n\n**Day 1**: walk <script>window.__pwned = 1</script> [bad](javascript:window.__pwned=2) and "
          "[good](https://example.com)\n\n- palace\n- market\n\nArea $\\pi r^2$ and\n$$\n\\frac{a}{b}\n$$\n")


def scripted() -> ScriptedAdapter:
    return Marked([
        NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("machine/read_file"), {"path": "plan.txt"}),)),
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
    notes = NotesServer()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    mcp = data / "mcp.json"
    mcp.write_text(json.dumps({"servers": {"notes": {"url": notes.url}}}), encoding="utf-8")
    mcp.chmod(0o600)
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
