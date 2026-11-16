"""``himari`` — the few things only a terminal does, on the local HIMARI Web server.

    himari proactive [status|try]        speaking first: the switch and today's count, or a wake now
    himari memory passes CONVERSATION    the memory passes applied in a conversation
    himari memory backfill CONVERSATION  run the memory passes a conversation has not had
    himari memory retry CONVERSATION PASS
                                         run a failed memory pass again

Everything else — conversations, files, notes, instructions, folders — is
the Web's (V3-P98). The server must be running (``python -m
himari.interfaces.web``); the CLI reads the operator secret from
``HIMARI_DATA_ROOT`` (or ``--data-root``) and talks to ``HIMARI_WEB_URL`` or
``http://127.0.0.1:$HIMARI_WEB_PORT``.

Exit status: 0 done, 1 the server refused or could not be reached,
2 usage/configuration, 4 outcome unknown.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import TextIO

from himari.bootstrap.operator import OperatorSecretError, derive_cli_bearer, read_operator_secret
from himari.interfaces.cli.client import (
    BadServerURL,
    ClientError,
    OutcomeUnknown,
    ServerClient,
    ServerRefused,
)
from himari.interfaces.cli.terminal import safe_line, stream_encoding

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNKNOWN = 0, 1, 2, 4
PASS_SECONDS = 600.0  # a memory pass is a model call or several


class Session:
    def __init__(self, client: ServerClient, out: TextIO, err: TextIO) -> None:
        self.client, self.out, self.err = client, out, err

    def line(self, text: str) -> None:
        """One line of the answer; what the server gave is made safe for the terminal."""
        self.out.write(safe_line(text, encoding=stream_encoding(self.out)) + "\n")
        self.out.flush()

    def note(self, text: str) -> None:
        self.err.write(safe_line(text, encoding=stream_encoding(self.err)) + "\n")
        self.err.flush()


def cmd_proactive(session: Session, args: argparse.Namespace) -> int:
    """V3-P87 §11: ``status`` (the default) or ``try`` — a TRY wake now, in the most
    recent conversation, spoken on the current device."""
    if args.action == "try":
        opened = session.client.post("/api/proactive/try", {})
        session.note(f"HIMARI speaks first in {opened['conversation']} (job {opened['job_id']})")
        return EXIT_OK
    status = session.client.get("/api/proactive")
    session.line(f"Reaching out: {status['told']}")
    session.line(f"Current device: {status.get('current_device') or 'none (away)'}")
    if "today" in status:
        session.line(f"Today: {status['today']} of {status['daily']}"
                     + (f" (last at {status['last_wake_at']})" if status.get("last_wake_at") else ""))
        session.line(f"Next loop check-in: {status.get('next_loop_at') or 'none'}")
        session.line(f"Next reminder: {status.get('next_reminder_at') or 'none'}")
        look = status.get("last_look")
        if isinstance(look, dict):
            held = ", ".join(look.get("held") or [])
            session.line(f"Last look: {look.get('outcome')} at {look.get('at')}" + (f" (held: {held})" if held else ""))
    return EXIT_OK


MEMORY_USAGE = "usage: himari memory passes CONVERSATION | memory backfill CONVERSATION | memory retry CONVERSATION PASS"
MEMORY_ACTIONS = ("passes", "backfill", "retry")


def cmd_memory(session: Session, args: argparse.Namespace) -> int:
    """V3-P85 §12: a conversation's memory passes, and running them by hand."""
    rest = list(args.rest)
    if args.action is None or len(rest) != (2 if args.action == "retry" else 1):
        session.note(MEMORY_USAGE)
        return EXIT_USAGE
    if args.action == "passes":
        lines = session.client.get(f"/api/conversations/{rest[0]}/memory-passes").get("passes") or []
        for line in lines:
            session.line(f"{line['pass_id']} (through event {line['through_sequence']}):")
            for item in line.get("items") or []:
                due = f" [due {item['due']}]" if item.get("due") else ""
                session.line(f"  {item['item']}  {item['action']}  {item.get('text') or ''}{due}"
                             + ("  (undo in the web)" if item.get("undo") else ""))
        if not lines:
            session.note("(no memory passes applied here)")
    elif args.action == "backfill":
        answer = session.client.post("/api/memory-passes/backfill", {"conversation_id": rest[0]}, timeout=PASS_SECONDS)
        session.note(f"(backfill: {', '.join(answer.get('outcomes') or []) or 'nothing'})")
    else:
        answer = session.client.post(f"/api/conversations/{rest[0]}/memory-passes/{rest[1]}/retry",
                                     timeout=PASS_SECONDS)
        session.note(f"(retry: {answer.get('outcome')})")
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="himari", description="What only a terminal does on the local HIMARI server.")
    p.add_argument("--url", help="server URL (default: $HIMARI_WEB_URL or http://127.0.0.1:$HIMARI_WEB_PORT)")
    p.add_argument("--data-root", help="HIMARI data directory holding control/operator.secret ($HIMARI_DATA_ROOT)")
    sub = p.add_subparsers(dest="command", required=True)
    proactive = sub.add_parser("proactive", help="speaking first: show the switch, or try it now")
    proactive.add_argument("action", nargs="?", choices=["status", "try"], default="status")
    memory = sub.add_parser("memory", help="a conversation's memory passes: list them, backfill, or retry one")
    memory.add_argument("action", nargs="?", choices=list(MEMORY_ACTIONS))
    memory.add_argument("rest", nargs="*", help="passes and backfill: CONVERSATION; retry: CONVERSATION PASS")
    return p


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], int]] = {"memory": cmd_memory, "proactive": cmd_proactive}


def run(argv: list[str], *, environ: dict[str, str], out: TextIO, err: TextIO,
        client: ServerClient | None = None) -> int:
    args = parser().parse_args(argv)
    owned = client is None
    if client is None:
        data_root = args.data_root or environ.get("HIMARI_DATA_ROOT", "")
        if not data_root:
            err.write("set HIMARI_DATA_ROOT (or --data-root) to the server's data directory\n")
            return EXIT_USAGE
        url = args.url or environ.get("HIMARI_WEB_URL") or f"http://127.0.0.1:{environ.get('HIMARI_WEB_PORT', '8765')}"
        try:
            secret = read_operator_secret(os.path.join(os.path.abspath(data_root), "control"))
            client = ServerClient.connect(url, derive_cli_bearer(secret))
        except (OperatorSecretError, OSError) as error:
            err.write(f"cannot read the operator secret: {safe_line(str(error))}\n")
            return EXIT_USAGE
        except BadServerURL as error:
            err.write(f"{error}\n")
            return EXIT_USAGE
    session = Session(client, out, err)
    try:
        return COMMANDS[args.command](session, args)
    except ServerRefused as error:
        session.note(f"refused ({error.code}): {error}")
        return EXIT_FAILED
    except OutcomeUnknown as error:
        session.note(str(error))
        return EXIT_UNKNOWN
    except ClientError as error:  # ServerUnavailable among them
        session.note(str(error))
        return EXIT_FAILED
    finally:
        if owned:
            client.close()


def main() -> int:
    return run(sys.argv[1:], environ=dict(os.environ), out=sys.stdout, err=sys.stderr)
