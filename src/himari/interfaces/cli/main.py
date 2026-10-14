"""``himari`` — the terminal client of the local HIMARI Web server (V3-P47).

    himari chat [ID] [--title T]   interactive; answers questions; /continue /rename /id /exit
    himari list                    your conversations, most recent first
    himari show ID                 a conversation's transcript and state
    himari new [--title T]         create a conversation, print its id
    himari send ID TEXT|-          send one message (``-`` reads stdin) and follow it
    himari continue ID             continue an accepted but unanswered message

The server must be running (``python -m himari.interfaces.web``); the CLI
reads the operator secret from ``HIMARI_DATA_ROOT`` (or ``--data-root``)
and talks to ``HIMARI_WEB_URL`` or ``http://127.0.0.1:$HIMARI_WEB_PORT``.

Exit status: 0 done, 3 waiting for your answer, 1 the run did not
complete or the server refused, 2 usage/configuration, 4 outcome unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO
from urllib.parse import quote

from himari.bootstrap.operator import OperatorSecretError, derive_cli_bearer, read_operator_secret
from himari.interfaces.cli.client import (
    BadServerURL,
    ClientError,
    OutcomeUnknown,
    ServerClient,
    ServerRefused,
    ServerUnavailable,
)
from himari.interfaces.cli.terminal import safe_block, safe_line, stream_encoding

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_WAITING, EXIT_UNKNOWN = 0, 1, 2, 3, 4
POLL_SECONDS = 0.5
TOOL_RESULT_LINES = 8

STATE_TEXT = {
    "completed": "done",
    "waiting_for_user": "waiting for your answer",
    "recovery_required": "needs attention: an earlier action's outcome is unknown",
    "provider_failed": "the model provider failed; nothing was retried",
    "outcome_unknown": "the request may not have reached the model; it was not resent",
    "invalid_model_output": "the model's reply could not be used",
    "request_rejected": "the request was refused before sending",
    "run_limit_reached": "stopped after the maximum number of steps",
    "idle": "nothing to continue",
}
ERROR_TEXT = {
    "busy": "this conversation is still working",
    "cancelled": "the run was stopped when the server shut down",
    "internal_error": "something went wrong on the server",
}


def _path(conversation_id: str, suffix: str = "") -> str:
    return f"/api/conversations/{quote(conversation_id, safe='')}{suffix}"


@dataclass
class Printer:
    out: TextIO
    verbose: bool = False
    calls: dict[str, str] = field(default_factory=dict)  # call_id -> tool_id
    answered: set[str] = field(default_factory=set)  # ask_user calls whose answer was printed

    @property
    def encoding(self) -> str | None:
        return stream_encoding(self.out)

    def line(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def block(self, header: str, text: str, max_lines: int | None = None) -> None:
        self.line(header)
        self.line(safe_block(text, encoding=self.encoding, max_lines=max_lines))

    def event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        text = str(event.get("text") or "")
        if kind == "user_message":
            answers = event.get("answers_call")
            if answers:
                self.answered.add(str(answers))  # its tool_result repeats this text
            self.block("You:" if not answers else "You (answer):", text)
        elif kind == "assistant_final":
            self.block("HIMARI:", text)
        elif kind == "assistant_tool_calls":
            if text:
                self.block("HIMARI:", text)
            for call in event.get("calls", []):
                tool_id, call_id = str(call.get("tool_id")), str(call.get("call_id"))
                self.calls[call_id] = tool_id
                arguments = call.get("arguments") or {}
                if tool_id == "ask_user":
                    question = arguments.get("question") if isinstance(arguments, dict) else None
                    self.block("HIMARI asks:", question if isinstance(question, str) else "")
                    continue
                shown = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                if len(shown) > 200 and not self.verbose:
                    shown = shown[:199] + "…"
                self.line(f"  ⚙ {safe_line(tool_id, encoding=self.encoding)} {safe_line(shown, encoding=self.encoding)}")
        elif kind == "tool_result":
            call_id = str(event.get("call_id"))
            called = self.calls.get(call_id)
            if called == "ask_user" or call_id in self.answered:
                return  # the answer was already shown as the user's message
            mark = "✗ failed" if event.get("is_error") else "✓"
            self.line(f"  ↳ {safe_line(called or 'tool', encoding=self.encoding)} {mark}")
            self.line(safe_block(text, encoding=self.encoding, max_lines=None if self.verbose else TOOL_RESULT_LINES))


class Session:
    def __init__(self, client: ServerClient, printer: Printer, err: TextIO,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client, self.printer, self.err, self.sleep = client, printer, err, sleep

    def note(self, text: str) -> None:
        self.err.write(safe_line(text, encoding=stream_encoding(self.err)) + "\n")
        self.err.flush()

    def print_events(self, conversation_id: str, after: int) -> int:
        while True:
            page = self.client.get(_path(conversation_id, "/events"), {"after": after, "limit": 200})
            for event in page["items"]:
                self.printer.event(event)
            after = int(page["last_sequence"])
            if len(page["items"]) < 200:
                return after

    def follow(self, conversation_id: str, after: int) -> tuple[dict[str, Any], int]:
        """Print new events until the job is no longer running."""
        while True:
            detail = self.client.get(_path(conversation_id))  # read first: events after it are complete
            after = self.print_events(conversation_id, after)
            if not detail["job"]["running"]:
                return detail, after
            self.sleep(POLL_SECONDS)

    def report(self, detail: dict[str, Any]) -> int:
        job = detail["job"]
        if detail.get("waiting"):
            self.note("(waiting for your answer)")
            return EXIT_WAITING
        if job.get("error"):
            self.note(f"(the run failed: {ERROR_TEXT.get(job['error'], job['error'])})")
            return EXIT_FAILED
        state = job.get("state")
        if detail.get("continuable") and state in (None, "completed"):
            self.note("(not answered yet — use `continue`)")
            return EXIT_FAILED
        if state not in (None, "completed", "idle"):
            self.note(f"({STATE_TEXT.get(state, state)})")
            return EXIT_FAILED
        return EXIT_OK

    def submit_and_follow(self, conversation_id: str, after: int, text: str | None) -> tuple[int, int]:
        try:
            if text is None:
                self.client.post(_path(conversation_id, "/resume"))
            else:
                self.client.post(_path(conversation_id, "/messages"), {"text": text})
        except ServerRefused as error:
            self.note(f"refused: {error}")
            return EXIT_FAILED, after
        except OutcomeUnknown as error:
            self.note(f"{error}; check with `show` before sending again")
            return EXIT_UNKNOWN, after
        try:
            detail, after = self.follow(conversation_id, after)
        except KeyboardInterrupt:
            self.note("(stopped following; the server keeps working — `show` to see the result)")
            return EXIT_FAILED, after
        return self.report(detail), after


# --- commands ----------------------------------------------------------------------------------


def cmd_list(session: Session, args: argparse.Namespace) -> int:
    page = session.client.get("/api/conversations", {"limit": args.limit})
    enc = session.printer.encoding
    for item in page["items"]:
        name = item.get("title") or item.get("preview") or "(new conversation)"
        session.printer.line(f"{safe_line(item['id'], encoding=enc)}  {item['last_activity_at'][:19]}  "
                             f"{safe_line(name, encoding=enc)}")
    if not page["items"]:
        session.note("(no conversations yet)")
    return EXIT_OK


def cmd_new(session: Session, args: argparse.Namespace) -> int:
    created = session.client.post("/api/conversations", {"title": args.title} if args.title else {})
    session.printer.line(str(created["id"]))
    return EXIT_OK


def cmd_show(session: Session, args: argparse.Namespace) -> int:
    detail = session.client.get(_path(args.id))
    session.print_events(args.id, 0)
    if detail["job"]["running"]:
        session.note("(still working)")
        return EXIT_OK
    return session.report(detail)


def cmd_send(session: Session, args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.text == "-" else args.text
    detail = session.client.get(_path(args.id))
    code, _ = session.submit_and_follow(args.id, int(detail["conversation"]["last_sequence"]), text)
    return code


def cmd_continue(session: Session, args: argparse.Namespace) -> int:
    detail = session.client.get(_path(args.id))
    code, _ = session.submit_and_follow(args.id, int(detail["conversation"]["last_sequence"]), None)
    return code


CHAT_HELP = "/continue  continue an unanswered message\n/rename T  rename\n/id        show the id\n/exit      leave"


def cmd_chat(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    if args.id:
        conversation_id = args.id
        session.client.get(_path(conversation_id))
        after = session.print_events(conversation_id, 0)
    else:
        conversation_id = str(session.client.post("/api/conversations", {"title": args.title} if args.title else {})["id"])
        after = 0
    session.note(f"conversation {conversation_id} — /help for commands, Ctrl-D to leave")
    while True:
        detail = session.client.get(_path(conversation_id))
        if detail.get("continuable") and not detail["job"]["running"]:
            session.note("(your last message is not answered yet — /continue)")
        try:
            line = read("answer> " if detail.get("waiting") else "you> ")
        except (EOFError, KeyboardInterrupt):
            session.note("")
            return EXIT_OK
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return EXIT_OK
        if text == "/help":
            session.note(CHAT_HELP)
            continue
        if text == "/id":
            session.printer.line(conversation_id)
            continue
        if text.startswith("/rename"):
            try:
                session.client.patch(_path(conversation_id), {"title": text[len("/rename"):].strip() or None})
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/continue":
            _, after = session.submit_and_follow(conversation_id, after, None)
            continue
        if text.startswith("/") and not text.startswith("//"):
            session.note("unknown command — /help")
            continue
        _, after = session.submit_and_follow(conversation_id, after, text[1:] if text.startswith("//") else text)


# --- entry point ----------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="himari", description="Terminal client of the local HIMARI server.")
    p.add_argument("--url", help="server URL (default: $HIMARI_WEB_URL or http://127.0.0.1:$HIMARI_WEB_PORT)")
    p.add_argument("--data-root", help="HIMARI data directory holding control/operator.secret ($HIMARI_DATA_ROOT)")
    p.add_argument("--verbose", action="store_true", help="show full tool arguments and results")
    sub = p.add_subparsers(dest="command")
    chat = sub.add_parser("chat", help="interactive conversation")
    chat.add_argument("id", nargs="?")
    chat.add_argument("--title")
    lst = sub.add_parser("list", help="list conversations")
    lst.add_argument("--limit", type=int, default=50)
    show = sub.add_parser("show", help="show a conversation")
    show.add_argument("id")
    new = sub.add_parser("new", help="create a conversation")
    new.add_argument("--title")
    send = sub.add_parser("send", help="send one message and follow it")
    send.add_argument("id")
    send.add_argument("text", help="the message, or - to read it from stdin")
    cont = sub.add_parser("continue", help="continue an unanswered message")
    cont.add_argument("id")
    return p


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], int]] = {
    "chat": cmd_chat, "list": cmd_list, "show": cmd_show, "new": cmd_new, "send": cmd_send, "continue": cmd_continue,
}


def run(argv: list[str], *, environ: dict[str, str], out: TextIO, err: TextIO,
        client: ServerClient | None = None) -> int:
    args = parser().parse_args(argv)
    command = args.command or "chat"
    if args.command is None:
        args.id, args.title = None, None
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
    session = Session(client, Printer(out, verbose=args.verbose), err)
    try:
        return COMMANDS[command](session, args)
    except ServerRefused as error:
        session.note(f"refused ({error.code}): {error}")
        return EXIT_FAILED
    except OutcomeUnknown as error:
        session.note(str(error))
        return EXIT_UNKNOWN
    except ServerUnavailable as error:
        session.note(str(error))
        return EXIT_FAILED
    except ClientError as error:
        session.note(str(error))
        return EXIT_FAILED
    finally:
        if owned:
            client.close()


def main() -> int:
    return run(sys.argv[1:], environ=dict(os.environ), out=sys.stdout, err=sys.stderr)
