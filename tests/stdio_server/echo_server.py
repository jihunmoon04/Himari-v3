"""A minimal MCP stdio server (standard library only), for end-to-end tests.

Offers one Tool, ``echo``; ``echo`` with ``text`` "where" answers with the
working directory and the environment it sees. Ends at stdin EOF.
"""

import json
import os
import sys

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
          "additionalProperties": False}


def answer(request_id: object, result: dict[str, object]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        answer(request_id, {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                            "serverInfo": {"name": "echo-server", "version": "1"}})
    elif method == "tools/list":
        answer(request_id, {"tools": [{"name": "echo", "description": "Echo the text.", "inputSchema": SCHEMA}]})
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        if text == "where":
            text = json.dumps({"cwd": os.getcwd(), "env": dict(os.environ)})
        answer(request_id, {"content": [{"type": "text", "text": f"echo: {text}"}]})
    elif method == "ping":
        answer(request_id, {})
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                     "error": {"code": -32601, "message": "unknown"}}) + "\n")
        sys.stdout.flush()
