"""``himari`` — the terminal client of the local HIMARI Web server (V3-P47).

    himari chat [ID] [--title T]   interactive; /continue /context /compact /tools /model /rename /delete /id /exit
    himari list [--offset N]       your conversations, most recent first
    himari delete ID [--now] [--yes]
                                   move a conversation to the trash (--now: delete it for good)
    himari trash [--offset N]      the conversations in the trash
    himari restore ID              bring a conversation back from the trash
    himari show ID                 a conversation's transcript and state
    himari new [--title T]         create a conversation, print its id
    himari send ID TEXT|-          send one message (``-`` reads stdin) and follow it
    himari continue ID             continue an accepted but unanswered message
    himari compact ID              summarize the earlier messages now (no model answer)
    himari tools ID [on|off|default SOURCE]
                                   the MCP servers this conversation uses; turn one on or off
    himari model ID [MODEL [EFFORT]|default]
                                   the model this conversation uses; choose another (next turn)
    himari workspace [add PATH | rm ID|PATH]
                                   folders on this server registered as workspaces
    himari attach ID [FOLDER]      a conversation's folders; attach one (the working directory)
    himari detach ID FOLDER        detach one (the folder stays)
    himari workdir ID FOLDER       make an attached folder the working directory

The server must be running (``python -m himari.interfaces.web``); the CLI
reads the operator secret from ``HIMARI_DATA_ROOT`` (or ``--data-root``)
and talks to ``HIMARI_WEB_URL`` or ``http://127.0.0.1:$HIMARI_WEB_PORT``.

Exit status: 0 done, 3 waiting for your answer, 1 the run did not
complete or the server refused, 2 usage/configuration, 4 outcome unknown.
"""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import re
import shlex
import shutil
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
    ResponseTooLarge,
    ServerClient,
    ServerRefused,
    ServerUnavailable,
)
from himari.interfaces.cli.terminal import Symbols, safe_block, safe_line, stream_encoding, symbols_for

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_WAITING, EXIT_UNKNOWN = 0, 1, 2, 3, 4
POLL_SECONDS = 0.5
EVENT_PAGE = 32  # 32 x a 64 KiB Tool result stays well under the 8 MiB response bound
TRASH_SECONDS = 90.0  # the server waits up to 30 s for a running job to stop (V3-P65 §7.1)
TOOL_RESULT_LINES = 8
SEARCH_COPIES = "Text that other conversations found with search stays in them."
MEMORY_CHANGES = frozenset({"memory_save", "memory_forget"})  # V3-P71: shown as one line each
MEMORY_RESULT = re.compile(r"(Saved|Updated|Forgot|Already saved as) (m[0-9]+)")
PROGRESS_CHARS = 200  # a progress line, like a call's arguments (V3-P68 §6)

STATE_TEXT = {
    "completed": "done",
    "waiting_for_user": "waiting for your answer",
    "recovery_required": "needs attention: an earlier action's outcome is unknown",
    "provider_failed": "the model provider failed; nothing was retried",
    "outcome_unknown": "the request may not have reached the model; it was not resent",
    "invalid_model_output": "the model's reply could not be used",
    "request_rejected": "the request was refused before sending",
    "run_limit_reached": "stopped after the maximum number of steps — `continue` to go on",
    "budget_reached": "stopped: this turn's token budget is used — `continue` to go on with a new one",
    "no_progress": "stopped: the last steps brought nothing new — `continue` to go on",
    "idle": "nothing to continue",
    "compaction_failed": "compaction failed; nothing was changed",
    "paused": "paused — `continue` to go on, or send a new message",
    "cancelled": "stopped",
}
DONE_TEXT = {  # finished states that are not failures
    "compacted": "compacted: earlier messages now reach HIMARI as a summary",
    "nothing_to_compact": "nothing to compact yet",
}
ERROR_TEXT = {
    "busy": "this conversation is still working",
    "cancelled": "the run was stopped when the server shut down",
    "internal_error": "something went wrong on the server",
}


def format_tokens(n: int) -> str:
    """As the Web client shows them: 950, 2.9k, 132k, 1M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
    if n >= 10_000:
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def usage_line(usage: dict[str, Any] | None) -> str | None:
    """What a turn used (V3-P62 §7): ``used 132k in + 2.9k out of 1M tokens in 7 requests``."""
    if not usage or not usage.get("runs"):
        return None
    runs = int(usage["runs"])
    budget = usage.get("budget")
    of = f" of {format_tokens(int(budget))}" if budget else ""
    return (f"used {format_tokens(int(usage['input_tokens']))} in + {format_tokens(int(usage['output_tokens']))} out"
            f"{of} tokens in {runs} request{'s' if runs != 1 else ''}")


def _path(conversation_id: str, suffix: str = "") -> str:
    return f"/api/conversations/{quote(conversation_id, safe='')}{suffix}"


@dataclass
class Printer:
    out: TextIO
    verbose: bool = False
    # A call is identified by (run_id, call_id): call ids repeat across Runs.
    calls: dict[tuple[str, str], str] = field(default_factory=dict)  # -> tool_id
    notes: dict[tuple[str, str], str] = field(default_factory=dict)  # memory_save calls -> the note's text
    answered: set[tuple[str, str]] = field(default_factory=set)  # ask_user calls whose answer was printed
    # The latest compaction (from /context): a divider is printed before the
    # first event it does not cover, with the summary when verbose.
    covered: int | None = None
    summary: str | None = None
    compactions: int = 0
    conversation_id: str | None = None  # named in the download hint of a file (V3-P66)
    divided: bool = False
    last_model: str | None = None  # of the latest answer printed: a change is named (V3-P64 §7)

    @property
    def encoding(self) -> str | None:
        return stream_encoding(self.out)

    def line(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    @property
    def symbols(self) -> Symbols:
        return symbols_for(self.encoding)

    def block(self, header: str, text: str, max_lines: int | None = None) -> None:
        self.line(header)
        self.line(safe_block(text, encoding=self.encoding, max_lines=max_lines))

    def divider(self) -> None:
        self.divided = True
        hint = "below" if self.verbose else "use --verbose or /context to read it"
        rule = self.symbols.rule  # ASCII on a stream that cannot carry "─" (like every CLI mark)
        self.line(f"{rule} earlier messages reach HIMARI only as a summary ({hint}) {rule}")
        if self.verbose and self.summary:
            self.line(safe_block(self.summary, encoding=self.encoding))

    def changed_model(self, event: dict[str, Any]) -> str | None:
        """The step's model when it changed since the step before, else None.
        Called once per step, which then shows it (PR #53 review B1)."""
        model = event.get("model")
        if not isinstance(model, str) or not model:
            return None
        changed = model if self.last_model is not None and model != self.last_model else None
        self.last_model = model
        return changed

    def speaker(self, model: str | None, verb: str = "") -> str:
        """``HIMARI:``, or ``HIMARI (model):`` for a changed model."""
        named = f" ({safe_line(model, encoding=self.encoding)})" if model else ""
        return f"HIMARI{named}{verb}:"

    def event(self, event: dict[str, Any]) -> None:
        sequence = event.get("sequence")
        if (self.covered is not None and not self.divided and isinstance(sequence, int)
                and sequence > self.covered):
            self.divider()
        kind = event.get("kind")
        text = str(event.get("text") or "")
        if kind == "user_message":
            answers = event.get("answers_call")
            if answers:
                self.answered.add((str(event.get("answers_run")), str(answers)))  # its tool_result repeats this text
            if text or not event.get("attachments"):
                self.block("You:" if not answers else "You (answer):", text)
            else:  # files only: no empty box (PR #60 review C7)
                self.line("You:" if not answers else "You (answer):")
            self.files(event.get("attachments") or [])
        elif kind == "assistant_final":
            self.block(self.speaker(self.changed_model(event)), text)
        elif kind == "assistant_tool_calls":
            model = self.changed_model(event)  # named on the first thing this step prints
            asks = any(call.get("tool_id") == "ask_user" for call in event.get("calls", []))
            if text and asks:  # the question's lead-in, not progress (V3-P68 §5.1)
                self.block(self.speaker(model), text)
                model = None
            elif text.strip():
                if model:
                    self.line(self.speaker(model))
                    model = None
                self.progress(text)
            for call in event.get("calls", []):
                tool_id, call_id = str(call.get("tool_id")), str(call.get("call_id"))
                self.calls[(str(event.get("run_id")), call_id)] = tool_id
                arguments = call.get("arguments") or {}
                if tool_id == "ask_user":
                    question = arguments.get("question") if isinstance(arguments, dict) else None
                    self.block(self.speaker(model, " asks"), question if isinstance(question, str) else "")
                    model = None
                    continue
                if model:  # a step of calls only: its model above them
                    self.line(self.speaker(model))
                    model = None
                if tool_id in MEMORY_CHANGES:  # one line with its result (V3-P71 §8)
                    note = arguments.get("text") if isinstance(arguments, dict) else None
                    self.notes[(str(event.get("run_id")), call_id)] = note if isinstance(note, str) else ""
                    continue
                shown = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                if len(shown) > 200 and not self.verbose:
                    shown = shown[:199] + self.symbols.more
                self.line(f"  {self.symbols.call} {safe_line(tool_id, encoding=self.encoding)} "
                          f"{safe_line(shown, encoding=self.encoding)}")
        elif kind == "tool_result":
            key = (str(event.get("run_id")), str(event.get("call_id")))
            called = self.calls.get(key)
            if key in self.answered:
                return  # the answer was already shown as the user's message
            if called == "ask_user":
                if event.get("is_error"):  # closed without an answer (dismissed, or its turn cancelled)
                    self.line("  (question closed without an answer)")
                return
            if called in MEMORY_CHANGES:
                self.memory_line(called, text, self.notes.get(key, ""), bool(event.get("is_error")))
                return
            mark = self.symbols.failed if event.get("is_error") else self.symbols.ok
            self.line(f"  {self.symbols.result} {safe_line(called or 'tool', encoding=self.encoding)} {mark}")
            self.line(safe_block(text, encoding=self.encoding, max_lines=None if self.verbose else TOOL_RESULT_LINES))
            self.files(event.get("presented_files") or [])
            self.files(event.get("images") or [])  # V3-P67 §8: the images the call returned

    def memory_line(self, tool_id: str, result: str, note: str, failed: bool) -> None:
        """``  ◆ remembered m15: Uses Go.`` (V3-P71 §8): what the user would
        want to know was kept or dropped, apart from the progress lines."""
        mark = self.symbols.memory
        if failed:
            self.line(f"  {mark} {tool_id} {self.symbols.failed}: {safe_line(result, encoding=self.encoding)}")
            return
        said = MEMORY_RESULT.match(result)
        verb, note_id = (said.group(1), said.group(2)) if said else ("", "")
        shown = {"Saved": f"remembered {note_id}: {note}", "Updated": f"updated {note_id}: {note}",
                 "Forgot": f"forgot {note_id}", "Already saved as": f"already remembered as {note_id}"}.get(
                     verb, result)
        self.line(f"  {mark} {safe_line(' '.join(shown.split()), encoding=self.encoding)}")

    def progress(self, text: str) -> None:
        """Commentary as one progress line, ``  » Looking through the code`` (V3-P68 §6):
        its first line (at most ``PROGRESS_CHARS``) and a marker when there is more;
        all of it when verbose."""
        first, _, rest = text.strip().partition("\n")
        first = first.strip()
        cut = len(first) > PROGRESS_CHARS and not self.verbose  # a long paragraph from before V3-P68
        if cut:
            first = first[:PROGRESS_CHARS - 1]
        more = self.symbols.more if cut or (rest.strip() and not self.verbose) else ""
        self.line(f"  {self.symbols.progress} {safe_line(first, encoding=self.encoding)}{more}")
        if rest.strip() and self.verbose:
            self.line(safe_block(rest.strip("\n"), encoding=self.encoding))

    def files(self, files: list[dict[str, Any]]) -> None:
        """One line a file: ``[file] report.pdf (1.2 MB) — himari download c_12 file_40`` (V3-P66 §9);
        an image says its size: ``[file] photo.jpg [image 800×600] (1.2 MB) — …`` (V3-P67 §8)."""
        for file in files:
            name = safe_line(str(file.get("name") or "(unknown)"), encoding=self.encoding)
            version = int(file.get("version") or 1)
            shown = f"{name} v{version}" if version > 1 else name
            image = file.get("image")
            if isinstance(image, dict) and isinstance(image.get("width"), int) and isinstance(image.get("height"), int):
                shown += f" [image {image['width']}{self.symbols.times}{image['height']}]"
            if file.get("expired"):
                self.line(f"  [file] {shown} (expired)")
                continue
            how = f"himari download {self.conversation_id} {file.get('file_id')}" if self.conversation_id else str(
                file.get("file_id"))
            if version > 1:
                how += f" --version {version}"
            self.line(safe_line(f"  [file] {shown} ({human_size(int(file.get('size') or 0))}) — {how}",
                                encoding=self.encoding))


def context_line(context: dict[str, Any]) -> str:
    estimated = context.get("estimated_tokens")
    size = ("size unknown while working" if estimated is None
            else f"about {estimated:,} of {context['budget_tokens']:,} tokens")
    line = f"context: {size}; summarizes at {context['compact_at_tokens']:,}"
    if context.get("hard_limit_tokens") != context.get("budget_tokens"):
        line += f"; input limit {context['hard_limit_tokens']:,}"  # the budget is a target, not the refusal point
    compactions = int(context.get("compactions") or 0)
    if compactions:
        line += f"; compacted {compactions} time{'s' if compactions > 1 else ''}"
    return line


class Session:
    def __init__(self, client: ServerClient, printer: Printer, err: TextIO,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client, self.printer, self.err, self.sleep = client, printer, err, sleep
        # The last post_and_follow's request itself: None if it was taken (or
        # may have been: its answer was lost), else the server's refusal.
        self.refused: ServerRefused | None = None

    def note(self, text: str) -> None:
        self.err.write(safe_line(text, encoding=stream_encoding(self.err)) + "\n")
        self.err.flush()

    def context(self, conversation_id: str) -> dict[str, Any] | None:
        """The /context answer, also arming the printer's divider. Display
        only: a failure to read it never stops a command."""
        try:
            context = self.client.get(_path(conversation_id, "/context"))
        except ClientError:
            return None
        if not isinstance(context, dict) or not context.get("enabled"):
            return None
        covered = context.get("covered_through_sequence")
        self.printer.covered = covered if isinstance(covered, int) else None
        summary = context.get("summary")
        self.printer.summary = summary if isinstance(summary, str) else None
        compactions = context.get("compactions")
        self.printer.compactions = compactions if isinstance(compactions, int) else 0
        return context

    def print_events(self, conversation_id: str, after: int) -> int:
        """Pages of at most EVENT_PAGE events; a page over the response bound
        is read again at half the size (a read, so safe to repeat)."""
        self.printer.conversation_id = conversation_id
        limit = EVENT_PAGE
        while True:
            try:
                page = self.client.get(_path(conversation_id, "/events"), {"after": after, "limit": limit})
            except ResponseTooLarge:
                if limit == 1:
                    raise ClientError(f"event {after + 1} is larger than the CLI can read") from None
                limit = max(1, limit // 2)
                continue
            for event in page["items"]:
                self.printer.event(event)
            after = int(page["last_sequence"])
            if len(page["items"]) < limit:
                return after

    def seed_model(self, conversation_id: str, after: int) -> None:
        """The model of the latest answer among the last events before
        ``after``, so a one-shot command can name a change (display only)."""
        try:
            page = self.client.get(_path(conversation_id, "/events"),
                                   {"after": max(0, after - EVENT_PAGE), "limit": EVENT_PAGE})
        except ClientError:
            return
        for event in page.get("items") or []:
            model = event.get("model")
            if isinstance(model, str) and model and int(event.get("sequence") or 0) <= after:
                self.printer.last_model = model

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
        why = job.get("detail")  # the job's own reason, e.g. the size that was refused
        used = usage_line(job.get("turn_usage"))
        if used:
            self.note(f"({used})")
        if detail.get("waiting"):
            asked = f": {why}" if why and job.get("state") == "waiting_for_user" else ""
            self.note(f"(waiting for your answer{asked})")
            return EXIT_WAITING
        if job.get("error"):
            self.note(f"(the run failed: {ERROR_TEXT.get(job['error'], job['error'])})")
            return EXIT_FAILED
        state = job.get("state")
        if state in DONE_TEXT:  # the job's own result; the conversation's state follows
            self.note(f"({DONE_TEXT[state]}{f': {why}' if why and state == 'nothing_to_compact' else ''})")
        if detail.get("continuable") and state in (None, "completed", *DONE_TEXT):
            # Compacting (or finding nothing to compact) answers nothing.
            self.note("(not answered yet — use `continue`)")
            return EXIT_FAILED
        if state not in (None, "completed", "idle", *DONE_TEXT):
            self.note(f"({STATE_TEXT.get(state, state)}{f': {why}' if why else ''})")
            return EXIT_FAILED
        return EXIT_OK

    def submit_and_follow(self, conversation_id: str, after: int, text: str | None,
                          attachments: list[str] | None = None) -> tuple[int, int]:
        if text is None and not attachments:
            return self.post_and_follow(conversation_id, after, "/resume", None)
        return self.post_and_follow(conversation_id, after, "/messages",
                                    {"text": text or "", "attachments": list(attachments or [])})

    def upload(self, conversation_id: str, paths: list[str]) -> list[dict[str, Any]]:
        """Upload each file; the ones the server took. A refusal is told and
        the rest still go (V3-P66 §9)."""
        taken: list[dict[str, Any]] = []
        for path in paths:
            name = os.path.basename(path)
            try:
                with open(path, "rb") as source:
                    kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    taken.append(self.client.upload(_path(conversation_id, "/uploads"), name, source, kind))
            except OSError as error:
                self.note(f"cannot read {path}: {error.strerror or error}")
                continue
            except ServerRefused as error:
                self.note(f"{path}: refused ({error.code}): {error}")
                continue
            except (OutcomeUnknown, ServerUnavailable) as error:  # the chat goes on (PR #60 review C8)
                self.note(f"{path}: {error}; it is not attached")
                continue
            if taken[-1].get("commands") is False:
                self.note("(commands cannot run on this server: the model reads text files only)")
        return taken

    def post_and_follow(self, conversation_id: str, after: int, suffix: str,
                        body: dict[str, Any] | None) -> tuple[int, int]:
        """Start a job (sent once, never resent) and follow it to the end."""
        self.refused = None
        try:
            self.client.post(_path(conversation_id, suffix), body)
        except ServerRefused as error:
            self.refused = error
            self.note(f"refused: {error}")
            return EXIT_FAILED, after
        except OutcomeUnknown as error:
            self.note(f"{error}; check with `show` before sending again")
            return EXIT_UNKNOWN, after
        try:
            detail, after = self.follow(conversation_id, after)
        except KeyboardInterrupt:
            # First Ctrl-C: stop the job now (a stop request only; it never
            # closes what a finished job left waiting). Second: stop following.
            try:
                self.client.post(_path(conversation_id, "/cancel"), None)
                self.note("(stopping — Ctrl-C again to stop following)")
                detail, after = self.follow(conversation_id, after)
            except KeyboardInterrupt:
                self.note("(stopped following; the server keeps working — `show` to see the result)")
                return EXIT_FAILED, after
        return self.report(detail), after

    def stop(self, conversation_id: str, action: str) -> int:
        """``pause`` or ``cancel`` the running job and follow it to its end;
        ``cancel`` with nothing running closes what was left waiting (an
        open question, or the steps a paused turn left). Stopping as asked
        is success."""
        detail = self.client.get(_path(conversation_id))
        after = int(detail["conversation"]["last_sequence"])
        job = self.client.post(_path(conversation_id, f"/{action}"), None)
        if job.get("running"):
            self.note("(pausing: the current step finishes first)" if action == "pause" else "(stopping)")
        elif action == "pause":
            self.note("(nothing is running)")
            return EXIT_OK
        else:
            self.client.post(_path(conversation_id, "/dismiss"), None)
        try:
            detail, _ = self.follow(conversation_id, after)
        except KeyboardInterrupt:
            self.note("(stopped following; the server keeps working — `show` to see the result)")
            return EXIT_FAILED
        job = detail["job"]
        if not job.get("error") and job.get("state") in ("paused", "cancelled", "idle"):
            self.note("(nothing was running or waiting)" if job["state"] == "idle" else f"({STATE_TEXT[job['state']]})")
            return EXIT_OK
        return self.report(detail)


TOOLS_USAGE = "usage: /tools [on|off|default SOURCE]"
_CHOICES = {"on": True, "off": False, "default": None}


def tool_source_line(source: dict[str, Any]) -> str:
    state = "on" if source.get("enabled") else "off"
    why = "default" if source.get("choice") is None else "this conversation"
    line = f"{source.get('source')}: {state} ({why}; default {'on' if source.get('default') else 'off'})"
    if not source.get("available"):
        line += " — not connected (restart the server after fixing it)"
    else:
        tools = source.get("tools") or []
        line += f" — {len(tools)} tool{'s' if len(tools) != 1 else ''}"
    return line


def tools_command(session: Session, conversation_id: str, words: list[str]) -> int:
    """No words: list. ``on|off|default SOURCE``: change, then list."""
    if words:
        if len(words) != 2 or words[0] not in _CHOICES:
            session.note(TOOLS_USAGE)
            return EXIT_USAGE
        answer = session.client.put(_path(conversation_id, f"/tools/{quote(words[1], safe='')}"),
                                    {"enabled": _CHOICES[words[0]]})
    else:
        answer = session.client.get(_path(conversation_id, "/tools"))
    sources = answer.get("sources") if isinstance(answer, dict) else None
    if not sources:
        session.note("(no MCP servers are configured)")
        return EXIT_OK
    enc = session.printer.encoding
    for source in sources:
        session.printer.line(safe_line(tool_source_line(source), encoding=enc))
        for problem in source.get("problems") or []:
            session.printer.line(f"  - {safe_line(str(problem), encoding=enc)}")
    return EXIT_OK


MODEL_USAGE = "usage: /model [MODEL [EFFORT] | default]"


def model_line(choice: dict[str, Any]) -> str:
    effort = choice.get("effort")
    chosen = choice.get("chosen")
    line = f"model: {choice.get('model')}{f' (reasoning {effort})' if effort else ''}"
    return line + (" — the server's default" if chosen is None else " — chosen here")


def model_command(session: Session, conversation_id: str, words: list[str]) -> int:
    """No words: this conversation's model and the offered ones. ``default``:
    follow the server's default. ``MODEL [EFFORT]``: choose (next turn)."""
    if len(words) > 2 or (words and words[0] == "default" and len(words) > 1):
        session.note(MODEL_USAGE)
        return EXIT_USAGE
    if words:
        model = None if words[0] == "default" else words[0]
        choice = session.client.put(_path(conversation_id, "/model"),
                                    {"model": model, "effort": words[1] if len(words) == 2 else None})
    else:
        choice = session.client.get(_path(conversation_id, "/model"))
    enc = session.printer.encoding
    session.printer.line(safe_line(model_line(choice), encoding=enc))
    if choice.get("fallback"):
        session.note(f"({choice['fallback']})")
    if words:
        detail = session.client.get(_path(conversation_id))
        if detail["job"]["running"]:
            session.note("(applies from the next message)")
        return EXIT_OK
    for model in session.client.get("/api/server").get("models") or []:
        efforts = model.get("efforts") or []
        about = [part for part in ("default" if model.get("default") else None,
                                   f"reasoning: {', '.join(efforts)} (default {model.get('default_effort') or 'none'})"
                                   if efforts else None,
                                   None if model.get("window_known") else "unverified") if part]
        session.printer.line(safe_line(f"  {model.get('id')}{f' ({'; '.join(about)})' if about else ''}",
                                       encoding=enc))
    return EXIT_OK


PROMPT_USAGE = "usage: /prompt SERVER NAME [ARGUMENT=VALUE ...] (quote a value with spaces)"


def prompt_line(prompt: dict[str, Any]) -> str:
    about = " — ".join(part for part in (prompt.get("title"), prompt.get("description")) if part)
    names = ", ".join(f"{a.get('name')}{'*' if a.get('required') else ''}" for a in prompt.get("arguments") or [])
    line = f"{prompt.get('source')} {prompt.get('name')}"
    return line + (f" ({names})" if names else "") + (f": {about}" if about else "")


def prompts_command(session: Session, conversation_id: str) -> int:
    """The prompts of the servers on in this conversation (V3-P60 §4)."""
    answer = session.client.get(_path(conversation_id, "/prompts"))
    prompts = answer.get("prompts") if isinstance(answer, dict) else None
    if not prompts:
        session.note("(no prompts: no server on in this conversation offers one)")
        return EXIT_OK
    enc = session.printer.encoding
    for prompt in prompts:
        session.printer.line(safe_line(prompt_line(prompt), encoding=enc))
    session.note("(* required) use: /prompt SERVER NAME ARGUMENT=VALUE ...")
    return EXIT_OK


def expand_prompt(session: Session, conversation_id: str, words: list[str]) -> str | None:
    """``SERVER NAME ARGUMENT=VALUE ...`` → the expanded text, shown; None
    when it cannot be (said why)."""
    if len(words) < 2 or any("=" not in w or w.startswith("=") for w in words[2:]):
        session.note(PROMPT_USAGE)
        return None
    arguments = dict(w.split("=", 1) for w in words[2:])
    try:
        answer = session.client.post(_path(conversation_id, "/prompts/expand"),
                                     {"source": words[0], "name": words[1], "arguments": arguments})
    except ServerRefused as error:
        session.note(f"refused: {error}")
        return None
    text = answer.get("text") if isinstance(answer, dict) else None
    if not isinstance(text, str) or not text:
        session.note("the server's answer was not readable")
        return None
    session.printer.block("This prompt would be sent as your message:", text)
    return text


def delete_question(name: str, notice: dict[str, Any], now: bool) -> list[str]:
    """The delete confirmation (V3-P65 §9), with what it does not delete
    (§6.1); one line each."""
    days = int(notice.get("trash_days") or 0)
    lines = [f"Delete \"{name}\" for good? This cannot be undone." if now
             else f"Move \"{name}\" to the trash? It is deleted for good after {days} days."]
    if notice.get("running"):
        lines.append("The work under way is stopped first.")
    unconfigured = set(notice.get("unconfigured") or [])
    servers = [f"{s} (no longer configured)" if s in unconfigured else str(s) for s in notice.get("servers") or []]
    lines.append(f"Data stored by the external servers this conversation used ({', '.join(servers)}) is not deleted."
                 if servers else "Data stored by external services is not deleted.")
    count = int(notice.get("memory_count") or 0)
    if count:  # V3-P71 D5
        lines.append(f"{count} note{'s' if count > 1 else ''} HIMARI saved in it {'are' if count > 1 else 'is'} "
                     "deleted with it for good.")
    if now:  # V3-P72 §8: what search already copied into other conversations stays there
        lines.append(SEARCH_COPIES)
    return lines


def delete_conversation(session: Session, conversation_id: str, *, now: bool, yes: bool,
                        read: Callable[[str], str]) -> bool:
    """Asks first (unless ``yes``), then moves it to the trash; ``now``:
    deletes it for good right after. Whether it was moved."""
    detail = session.client.get(_path(conversation_id))
    notice = session.client.get(_path(conversation_id, "/delete-notice"))
    name = detail["conversation"].get("title") or detail["conversation"].get("preview") or "(new conversation)"
    for line in delete_question(name, notice, now):
        session.note(line)
    if not yes:
        try:
            answer = read("Delete? [y/N] " if now else "Move to the trash? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            session.note("(nothing was deleted)")
            return False
    moved = session.client.post(_path(conversation_id, "/trash"), None, timeout=TRASH_SECONDS)
    if not now:
        until = str(moved.get("purge_after") or "")[:10]
        session.note(f"(moved to the trash; deleted for good on {until} — "
                     f"`himari restore {conversation_id}` brings it back)")
        return True
    try:
        session.client.delete(_path(conversation_id))
    except ClientError:
        session.note("(moved to the trash, but not deleted for good: `himari trash` shows it)")
        raise
    session.note("(deleted for good)")
    return True


# --- commands ----------------------------------------------------------------------------------


def _more(session: Session, page: dict[str, Any]) -> None:
    if page.get("next_offset") is not None:
        session.note(f"(more: --offset {page['next_offset']})")


def cmd_list(session: Session, args: argparse.Namespace) -> int:
    page = session.client.get("/api/conversations", {"limit": args.limit, "offset": args.offset})
    enc = session.printer.encoding
    for item in page["items"]:
        name = item.get("title") or item.get("preview") or "(new conversation)"
        session.printer.line(f"{safe_line(item['id'], encoding=enc)}  {item['last_activity_at'][:19]}  "
                             f"{safe_line(name, encoding=enc)}")
    if not page["items"]:
        session.note("(no conversations yet)" if not args.offset else "(no more conversations)")
    _more(session, page)
    return EXIT_OK


def cmd_trash(session: Session, args: argparse.Namespace) -> int:
    page = session.client.get("/api/conversations", {"limit": args.limit, "offset": args.offset, "trashed": "true"})
    enc = session.printer.encoding
    for item in page["items"]:
        name = item.get("title") or item.get("preview") or "(new conversation)"
        session.printer.line(f"{safe_line(item['id'], encoding=enc)}  trashed {item['trashed_at'][:19]}  "
                             f"deleted {item['purge_after'][:10]}  {safe_line(name, encoding=enc)}")
    if not page["items"]:
        session.note("(the trash is empty)" if not args.offset else "(nothing more in the trash)")
    _more(session, page)
    return EXIT_OK


def cmd_delete(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    delete_conversation(session, args.id, now=args.now, yes=args.yes, read=read)
    return EXIT_OK


def cmd_restore(session: Session, args: argparse.Namespace) -> int:
    session.client.post(_path(args.id, "/restore"))
    session.note("(restored)")
    return EXIT_OK


def cmd_new(session: Session, args: argparse.Namespace) -> int:
    created = session.client.post("/api/conversations", {"title": args.title} if args.title else {})
    session.printer.line(str(created["id"]))
    return EXIT_OK


def cmd_show(session: Session, args: argparse.Namespace) -> int:
    detail = session.client.get(_path(args.id))
    context = session.context(args.id)
    session.print_events(args.id, 0)
    if context is not None:
        session.note(context_line(context))
    if detail["job"]["running"]:
        session.note("(still working)")
        return EXIT_OK
    return session.report(detail)


def cmd_send(session: Session, args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.text == "-" else args.text
    if not text and not args.attach:
        session.note("send needs a message or --attach")
        return EXIT_USAGE
    detail = session.client.get(_path(args.id))
    after = int(detail["conversation"]["last_sequence"])
    session.seed_model(args.id, after)
    attachments: list[str] = []
    if args.attach:
        taken = session.upload(args.id, args.attach)
        if len(taken) < len(args.attach):
            session.note("(not sent: not every file was taken)")
            return EXIT_FAILED
        attachments = [str(f["file_id"]) for f in taken]
    # With files, an empty text still sends a message (never /resume).
    code, _ = session.submit_and_follow(args.id, after, text or ("" if attachments else None), attachments)
    return code


def human_size(n: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def cmd_files(session: Session, args: argparse.Namespace) -> int:
    page = session.client.get(_path(args.id, "/files"), {"limit": args.limit, "offset": args.offset})
    for file in page["items"]:
        version = int(file["version"])
        name = safe_line(str(file["name"]), encoding=session.printer.encoding)
        state = " expired" if file["expired"] else ""
        image = file.get("image")
        if isinstance(image, dict) and isinstance(image.get("width"), int) and isinstance(image.get("height"), int):
            # Before the name, which is the user's text (PR #63 review N2).
            state += f"  [image {image['width']}{session.printer.symbols.times}{image['height']}]"
        session.printer.line(f"{file['file_id']}  v{version}  {file['origin']:<9}  {human_size(int(file['size'])):>9}"
                             f"{state}  {name}")
    _more(session, page)
    return EXIT_OK


def copy_new(source_path: str, path: str) -> None:
    """Copy into a new file at ``path`` (never replacing one); if the copy
    fails part way, the new file goes too (owner review B1): a failed
    download never leaves half a file."""
    with open(source_path, "rb") as source:
        sink = open(path, "xb")
        try:
            with sink:
                shutil.copyfileobj(source, sink)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise


def cmd_download(session: Session, args: argparse.Namespace) -> int:
    """Into a temporary file next to the target, which takes its name only
    when complete: a failure never leaves half a file, nor costs an existing
    one under --force (PR #60 review C3)."""
    chosen: list[str] = []

    def target(name: str | None) -> tuple[str, Any]:
        if args.output:
            path = args.output
        else:
            if not name or name.startswith(".") or "/" in name or "\0" in name:
                # V3-P66 review C9: never a dot-file (.npmrc, .gitconfig) chosen by the model.
                raise ClientError(f"the file's name {name!r} is not used as is; give one with -o")
            path = name
        if not args.force and os.path.lexists(path):
            raise ClientError(f"{path} exists; use --force to overwrite it, or -o")
        chosen.append(path)
        temp = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.{os.getpid()}.part")
        try:
            return temp, open(temp, "xb")
        except OSError as error:
            raise ClientError(f"cannot write {path}: {error.strerror or error}") from None

    temp = session.client.download(_path(args.id, f"/files/{quote(args.file_id, safe='')}/{args.version}"), target)
    path = chosen[0]
    try:
        if args.force:
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)  # never replaces: something may have appeared meanwhile
            except OSError as error:
                if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EMLINK):
                    raise
                copy_new(temp, path)  # no hard links here (re-review N2)
            os.unlink(temp)
    except FileExistsError:
        os.unlink(temp)
        raise ClientError(f"{path} exists; use --force to overwrite it, or -o") from None
    except OSError as error:
        os.unlink(temp)
        raise ClientError(f"cannot write {path}: {error.strerror or error}") from None
    session.note(f"saved {path}")
    return EXIT_OK


def cmd_continue(session: Session, args: argparse.Namespace) -> int:
    detail = session.client.get(_path(args.id))
    after = int(detail["conversation"]["last_sequence"])
    session.seed_model(args.id, after)
    code, _ = session.submit_and_follow(args.id, after, None)
    return code


def cmd_compact(session: Session, args: argparse.Namespace) -> int:
    detail = session.client.get(_path(args.id))
    code, _ = session.post_and_follow(args.id, int(detail["conversation"]["last_sequence"]), "/compact", None)
    context = session.context(args.id)
    if context is not None:
        session.note(context_line(context))
    return code


def cmd_tools(session: Session, args: argparse.Namespace) -> int:
    return tools_command(session, args.id, [w for w in (args.choice, args.source) if w is not None])


def cmd_model(session: Session, args: argparse.Namespace) -> int:
    return model_command(session, args.id, [w for w in (args.model, args.effort) if w is not None])


SANDBOX_TEXT = {
    "enforced": "ready",
    "capable_inactive": "off (sandbox disabled)",
    "unavailable": "unavailable on this host",
    "setup_failed": "unavailable (sandbox setup failed)",
}


def instructions_text(text: str, encoding: str | None) -> str:
    """For a terminal: lines and tabs kept, anything a terminal could act on
    or hide (e.g. bidi controls) escaped — for reading, not for setting again."""
    return "\n".join("\t".join(safe_line(part, encoding=encoding) for part in line.split("\t"))
                     for line in text.split("\n"))


def _is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def write_exactly(out: TextIO, text: str) -> None:
    """The text as stored, as UTF-8 bytes where the stream has them: no
    newline translation, and any locale's encoding (Data refuses control
    characters, so nothing in it acts on a terminal it is piped to)."""
    raw = getattr(out, "buffer", None)
    if raw is None:
        out.write(text)
    else:
        out.flush()
        raw.write(text.encode("utf-8"))
        raw.flush()
    out.flush()


INSTRUCTIONS_USAGE = "usage: himari instructions [set FILE|- | clear [--yes]]"


def cmd_instructions(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    """V3-P69 §8: show, ``set FILE|-``, or ``clear`` the user's instructions.
    Shown into a file or a pipe, the text is written exactly as stored, so
    ``himari instructions > f; himari instructions set f`` changes nothing."""
    if ((args.action is None and args.file is not None) or (args.action == "set" and args.file is None)
            or (args.action == "clear" and args.file is not None) or (args.yes and args.action != "clear")):
        session.note(INSTRUCTIONS_USAGE)
        return EXIT_USAGE
    text = ""
    if args.action == "set":
        try:
            if args.file == "-":
                if _is_terminal(sys.stdin):
                    session.note("(reading the instructions from standard input; end with Ctrl-D)")
                raw = getattr(sys.stdin, "buffer", None)  # bytes: invalid UTF-8 is "cannot read", not a crash
                text = raw.read().decode("utf-8-sig") if raw is not None else sys.stdin.read().removeprefix("\ufeff")
            else:
                with open(args.file, encoding="utf-8-sig") as handle:  # a BOM (e.g. from Notepad) is not text
                    text = handle.read()
        except (OSError, UnicodeDecodeError) as error:
            session.note(f"cannot read {safe_line(args.file)}: {safe_line(str(error))}")
            return EXIT_USAGE
    current = session.client.get("/api/instructions")
    if args.action is None:
        if not current["text"]:
            session.note("(no instructions)")
        elif _is_terminal(session.printer.out):
            session.printer.line(instructions_text(current["text"], session.printer.encoding))
        else:
            write_exactly(session.printer.out, current["text"])
        return EXIT_OK
    if args.action == "clear":
        if not current["text"]:
            session.note("(no instructions)")
            return EXIT_OK
        if not args.yes:
            try:
                answer = read("Clear your instructions for every conversation? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                session.note("(nothing was changed)")
                return EXIT_OK
    # Saved from the revision just read: a change made elsewhere since is refused, never overwritten.
    try:
        session.client.put("/api/instructions", {"text": text, "revision": current["revision"]})
    except ServerRefused as error:
        if error.code != "revision_conflict":
            raise
        session.note(f"refused ({error.code}): {error} Run the command again to replace them.")
        return EXIT_FAILED
    session.note("(saved; applies from the next message)" if text.strip() else "(cleared; applies from the next message)")
    return EXIT_OK


MEMORY_USAGE = ("usage: himari memory | memory add CATEGORY TEXT|- | memory edit ID TEXT|- [--category C]"
                " | memory rm ID [--yes]")
MEMORY_CATEGORIES = ("user", "preference", "project", "reference")


def memory_id(raw: str) -> int | None:
    """``m12`` or ``12`` → 12."""
    match = re.fullmatch(r"m?([1-9][0-9]{0,9})", raw.strip())
    return None if match is None else int(match.group(1))


def memory_row(note: dict[str, Any]) -> str:
    """``m12  Uses Go.  (from "Trip" · HIMARI · 2026-10-24)`` (V3-P71 §8)."""
    source = note.get("source")
    if isinstance(source, dict):
        where = f"from \"{source.get('title')}\"" if source.get("title") else f"from {source.get('conversation')}"
        if source.get("in_trash"):
            where += " (in trash)"
    else:
        where = "added by you"
    who = "HIMARI" if note.get("origin") == "model" else "you"
    when = str(note.get("updated_at") or "")[:10]
    text = " ".join(str(note.get("text") or "").split())
    return f"m{note.get('id')}  {text}  ({where} · {who} · {when})" if isinstance(source, dict) else \
        f"m{note.get('id')}  {text}  ({where} · {when})"


def _memory_text(session: Session, raw: str) -> str | None:
    if raw != "-":
        return raw
    try:
        if _is_terminal(sys.stdin):
            session.note("(reading the note from standard input; end with Ctrl-D)")
        stream = getattr(sys.stdin, "buffer", None)
        return stream.read().decode("utf-8-sig") if stream is not None else sys.stdin.read().removeprefix("\ufeff")
    except (OSError, UnicodeDecodeError) as error:
        session.note(f"cannot read the note: {safe_line(str(error))}")
        return None


def cmd_memory(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    """V3-P71 §8: list the notes, ``add``, ``edit`` or ``rm`` one."""
    rest = list(args.rest)
    action = args.action
    wrong = ((action is None and (rest or args.category or args.yes))
             or (action == "add" and (len(rest) != 2 or rest[0] not in MEMORY_CATEGORIES or args.category or args.yes))
             or (action == "edit" and (len(rest) != 2 or args.yes))
             or (action == "rm" and (len(rest) != 1 or args.category)))
    if wrong:
        session.note(MEMORY_USAGE)
        return EXIT_USAGE
    if action is None:
        listing = session.client.get("/api/memories")
        notes = listing.get("memories") or []
        if not listing.get("enabled"):
            session.note("(memory is off on this server: HIMARI neither saves nor uses notes; you can still manage them)")
        if not notes:
            session.note("(no notes)")
            return EXIT_OK
        for category in MEMORY_CATEGORIES:
            mine = [n for n in notes if n.get("category") == category]
            if mine:
                session.printer.line(f"{category}:")
                for note in mine:
                    session.printer.line("  " + safe_line(memory_row(note), encoding=session.printer.encoding))
        session.note(f"({len(notes)} of {listing.get('max_count')} notes)")
        return EXIT_OK
    if action == "add":
        text = _memory_text(session, rest[1])
        if text is None:
            return EXIT_USAGE
        try:
            saved = session.client.post("/api/memories", {"category": rest[0], "text": text})
        except ServerRefused as error:
            session.note(f"refused ({error.code}): {error}")
            return EXIT_FAILED
        session.note(f"(saved as m{saved['id']}; used from the next message)")
        return EXIT_OK
    target = memory_id(rest[0])
    if target is None:
        session.note(f"not a note id: {safe_line(rest[0])} (as m12)")
        return EXIT_USAGE
    notes = session.client.get("/api/memories").get("memories") or []
    current = next((n for n in notes if n.get("id") == target), None)
    if current is None:
        session.note(f"no note m{target}")
        return EXIT_FAILED
    if action == "edit":
        if args.category is not None and args.category not in MEMORY_CATEGORIES:
            session.note(f"a category is one of {', '.join(MEMORY_CATEGORIES)}")
            return EXIT_USAGE
        text = _memory_text(session, rest[1])
        if text is None:
            return EXIT_USAGE
        try:  # from the revision just read: a change made elsewhere since is refused, never overwritten
            session.client.put(f"/api/memories/{target}", {"category": args.category or current["category"],
                                                           "text": text, "revision": current["revision"]})
        except ServerRefused as error:
            session.note(f"refused ({error.code}): {error}")
            return EXIT_FAILED
        session.note("(saved; used from the next message)")
        return EXIT_OK
    session.printer.line(safe_line(memory_row(current), encoding=session.printer.encoding))
    if not args.yes:
        try:
            answer = read("Delete this note? Conversations that already used it still show it. [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            session.note("(nothing was deleted)")
            return EXIT_OK
    try:
        session.client.delete(f"/api/memories/{target}", {"revision": current["revision"]})
    except ServerRefused as error:
        session.note(f"refused ({error.code}): {error}")
        return EXIT_FAILED
    session.note("(deleted)")
    return EXIT_OK


WORKSPACE_USAGE = "usage: himari workspace | workspace add PATH [--label L] [--note N] | workspace rm ID|PATH [--yes]"


def _workspace_row(workspace: dict[str, Any], encoding: str | None) -> str:
    used = int(workspace.get("attached") or 0)
    attached = f"  (attached to {used} conversation{'s' if used != 1 else ''})" if used else ""
    return safe_line(f"{workspace.get('id')}  {workspace.get('label')}  {workspace.get('path')}{attached}",
                     encoding=encoding)


def cmd_workspace(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    """V3-P74 §10.3: the folders on this server registered as Workspaces."""
    rest, action = list(args.rest), args.action
    if ((action is None and (rest or args.label or args.note or args.yes))
            or (action == "add" and (len(rest) != 1 or args.yes))
            or (action == "rm" and (len(rest) != 1 or args.label or args.note))):
        session.note(WORKSPACE_USAGE)
        return EXIT_USAGE
    listing = session.client.get("/api/workspaces")
    if not listing.get("available"):
        session.note("(folders on this server are not offered to you)")
        return EXIT_FAILED
    items: list[dict[str, Any]] = listing.get("items") or []
    if action is None:
        for workspace in items:
            session.printer.line(_workspace_row(workspace, session.printer.encoding))
            if workspace.get("note"):
                session.printer.line("    " + safe_line(" ".join(str(workspace["note"]).split()),
                                                        encoding=session.printer.encoding))
            if workspace.get("cloud_synced"):
                session.printer.line("    (cloud-synced: the model is told files have no execute bit and sync can lag)")
        if not items:
            session.note("(no folders registered; add one with `himari workspace add PATH`)")
        return EXIT_OK
    if action == "add":
        added = session.client.post("/api/workspaces", {"path": rest[0], "label": args.label, "note": args.note})
        session.printer.line(_workspace_row(added, session.printer.encoding))
        return EXIT_OK
    target = next((w for w in items if rest[0] in (w.get("id"), w.get("path"))), None)
    if target is None:
        session.note(f"no registered folder {safe_line(rest[0])}")
        return EXIT_FAILED
    if not args.yes:
        try:
            answer = read(f"Unregister {target.get('path')}? The folder itself stays. [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            session.note("(nothing was changed)")
            return EXIT_OK
    session.client.delete(f"/api/workspaces/{quote(str(target['id']), safe='')}")
    session.note("(unregistered; the folder itself is untouched)")
    return EXIT_OK


def print_folders(session: Session, folders: list[dict[str, Any]]) -> None:
    """A Conversation's folders: ``*`` marks the working directory."""
    if not folders:
        session.note("(this conversation does not work on this server's folders)")
        return
    for folder in folders:
        mark = "*" if folder.get("working") else " "
        own = "  (this conversation's own)" if folder.get("own") else ""
        session.printer.line(safe_line(f"{mark} {folder.get('path') or '(unknown: the runner does not answer)'}  "
                                       f"{folder.get('label')}{own}", encoding=session.printer.encoding))


def _folder(folders: list[dict[str, Any]], given: str) -> dict[str, Any] | None:
    return next((f for f in folders if given in (f.get("binding_id"), f.get("path"), f.get("workspace_id"))), None)


def cmd_attach(session: Session, args: argparse.Namespace) -> int:
    """No folder: the conversation's folders. Else attach a registered one
    (id or path) or a new path, which becomes the working directory."""
    if args.folder is None:
        print_folders(session, session.client.get(_path(args.id, "/workspaces"))["items"])
        return EXIT_OK
    listing = session.client.get("/api/workspaces")
    known = next((w for w in listing.get("items") or [] if args.folder in (w.get("id"), w.get("path"))), None)
    body: dict[str, Any] = ({"workspace_id": known["id"]} if known is not None
                            else {"path": args.folder, "label": args.label})
    folders = session.client.post(_path(args.id, "/workspaces"), body)["items"]
    print_folders(session, folders)
    session.note("(the model can write there from its next step)")
    return EXIT_OK


def cmd_detach(session: Session, args: argparse.Namespace) -> int:
    folders = session.client.get(_path(args.id, "/workspaces"))["items"]
    found = _folder(folders, args.folder)
    if found is None:
        session.note(f"not attached to this conversation: {safe_line(args.folder)}")
        return EXIT_FAILED
    print_folders(session, session.client.delete(
        _path(args.id, f"/workspaces/{quote(str(found['binding_id']), safe='')}"))["items"])
    return EXIT_OK


def cmd_workdir(session: Session, args: argparse.Namespace) -> int:
    folders = session.client.get(_path(args.id, "/workspaces"))["items"]
    found = _folder(folders, args.folder)
    if found is None:
        session.note(f"not attached to this conversation: {safe_line(args.folder)}")
        return EXIT_FAILED
    print_folders(session, session.client.put(_path(args.id, "/workspaces/working"),
                                              {"binding_id": found["binding_id"]})["items"])
    return EXIT_OK


def documents_line(documents: object) -> str:
    """V3-P70 §9: whether documents are read here, whether scripts can make them, with which libraries."""
    if not isinstance(documents, dict) or not documents.get("read"):
        return "documents: off"
    libraries = documents.get("libraries") if isinstance(documents.get("libraries"), dict) else {}
    named = ", ".join(f"{name} {version}" for name, version in (libraries or {}).items())
    return (f"documents: read on, scripts {'on' if documents.get('scripts') else 'off'}"
            + (f" ({named})" if named else ""))


def cmd_status(session: Session, args: argparse.Namespace) -> int:
    server = session.client.get("/api/server")
    sandbox = str(server.get("sandbox"))
    offered = [str(m.get("id")) for m in server.get("models") or [] if not m.get("default")]
    for line in (f"model: {server.get('model')}" + (f" (also offered: {', '.join(offered)})" if offered else ""),
                 f"commands: {SANDBOX_TEXT.get(sandbox, sandbox)}",
                 f"context budget: {server.get('context_budget'):,} tokens", documents_line(server.get("documents")),
                 f"memory: {'on' if server.get('memory') else 'off'}",
                 f"conversation search: {'on' if server.get('conversation_search') else 'off'}",
                 f"streaming: {'on' if server.get('streaming') else 'off'}"):
        session.printer.line(safe_line(line, encoding=session.printer.encoding))
    return EXIT_OK


def cmd_rename(session: Session, args: argparse.Namespace) -> int:
    session.client.patch(_path(args.id), {"title": args.title or None})  # no title: back to the default name
    return EXIT_OK


def cmd_pause(session: Session, args: argparse.Namespace) -> int:
    return session.stop(args.id, "pause")


def cmd_cancel(session: Session, args: argparse.Namespace) -> int:
    return session.stop(args.id, "cancel")


CHAT_HELP = ("/continue  continue an unanswered message (or a paused turn)\n"
             "/dismiss   close an open question, or the steps a paused turn left\n"
             "Ctrl-C     while HIMARI works: stop now (again: stop following)\n"
             "/context   context size and the latest summary\n"
             "/compact   summarize the earlier messages now\n"
             "/tools     MCP servers in use; /tools on|off|default SOURCE to change\n"
             "/model     the model; /model MODEL [EFFORT] or /model default to change (next message)\n"
             "/prompts   prompts the servers in use offer\n"
             "/instructions  your instructions for every conversation (change: himari instructions set FILE)\n"
             "/prompt S N A=V ...  fill in server S's prompt N, show it, send it if you confirm\n"
             "/attach F...  upload files for the next message; /attach alone lists them, /detach drops them\n"
             "/send      send the attached files without text\n"
             "/files     the conversation's files (download with: himari download ID FILE_ID)\n"
             "/rename T  rename\n/delete    move this conversation to the trash, then leave\n"
             "/id        show the id\n/exit      leave")


FILE_REFUSAL_CODES = frozenset({"attachment_refused", "too_many_attachments", "file_missing", "file_expired",
                                "files_unavailable", "uploads_folder_unusable"})


def settle_pending(session: Session, pending: list[dict[str, Any]]) -> None:
    """After a message with ``pending`` files (PR #60 review B1, C1): taken
    (whatever became of its turn), or maybe taken — they are the message's
    now, so none is attached again. Refused — they stay for the next one,
    except a file the server refused, which is dropped with a word."""
    refused = session.refused
    if refused is None:
        pending.clear()
        return
    if refused.code == "attachment_refused" and refused.file_id:
        for file in [f for f in pending if f.get("file_id") == refused.file_id]:
            pending.remove(file)
            session.note(f"dropped {file.get('name')}: upload it again (/attach)")
    elif pending and refused.code in FILE_REFUSAL_CODES:  # not one file: say how to go on (re-review N3)
        session.note("(the attached files stay attached; /detach drops them)")


def cmd_chat(session: Session, args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    if args.id:
        conversation_id = args.id
        session.client.get(_path(conversation_id))
        session.context(conversation_id)
        after = session.print_events(conversation_id, 0)
    else:
        conversation_id = str(session.client.post("/api/conversations", {"title": args.title} if args.title else {})["id"])
        after = 0
    session.note(f"conversation {conversation_id} — /help for commands, Ctrl-D to leave")
    session.printer.conversation_id = conversation_id
    pending: list[dict[str, Any]] = []  # uploaded for the next message (V3-P66 §9)
    try:
        choice = session.client.get(_path(conversation_id, "/model"))
        session.note(model_line(choice) + (f" ({choice['fallback']})" if choice.get("fallback") else ""))
    except ClientError:
        pass  # display only
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
        if text == "/compact":
            _, after = session.post_and_follow(conversation_id, after, "/compact", None)
            context = session.context(conversation_id)
            if context is not None:
                session.note(context_line(context))
            continue
        if text == "/instructions":  # V3-P69 §8: shown here; changed with `himari instructions set`
            try:
                current = session.client.get("/api/instructions")
            except ClientError as error:
                session.note(f"refused: {error}" if isinstance(error, ServerRefused) else str(error))
                continue
            if not current["text"]:
                session.note("(no instructions; set them with `himari instructions set FILE`)")
            else:
                session.printer.block("Your instructions (every conversation):", current["text"])
            continue
        if text == "/context":
            seen = session.printer.compactions
            context = session.context(conversation_id)
            if context is None:
                session.note("(no context information)")
                continue
            session.note(context_line(context))
            if session.printer.compactions > seen and session.printer.covered is not None:
                # The history above was printed before this compaction: say where the boundary is now.
                session.note(f"(compacted since the history above was printed: the summary now covers "
                             f"events up to #{session.printer.covered})")
            if session.printer.summary:
                session.printer.block("Summary the model sees for earlier messages:", session.printer.summary)
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
        if text == "/attach" or text.startswith("/attach "):
            try:
                paths = shlex.split(text[len("/attach"):])
            except ValueError:
                session.note("usage: /attach FILE...")
                continue
            if paths:
                pending.extend(session.upload(conversation_id, paths))
            for file in pending:
                session.note(f"attached: {file['name']} ({human_size(int(file['size']))})")
            if not pending:
                session.note("(nothing attached)")
            continue
        if text == "/detach":
            pending.clear()
            session.note("(nothing attached)")
            continue
        if text == "/files":
            try:
                cmd_files(session, argparse.Namespace(id=conversation_id, limit=50, offset=0))
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/send":
            if not pending:
                session.note("(nothing attached)")
                continue
            _, after = session.submit_and_follow(conversation_id, after, "", [str(f["file_id"]) for f in pending])
            settle_pending(session, pending)
            continue
        if text == "/delete":
            try:
                if delete_conversation(session, conversation_id, now=False, yes=False, read=read):
                    return EXIT_OK  # in the trash: nothing more to say in it
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/dismiss":
            _, after = session.post_and_follow(conversation_id, after, "/dismiss", None)
            continue
        if text == "/tools" or text.startswith("/tools "):
            try:
                tools_command(session, conversation_id, text.split()[1:])
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/model" or text.startswith("/model "):
            try:
                model_command(session, conversation_id, text.split()[1:])
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/prompts":
            try:
                prompts_command(session, conversation_id)
            except ServerRefused as error:
                session.note(f"refused: {error}")
            continue
        if text == "/prompt" or text.startswith("/prompt "):
            try:
                words = shlex.split(text[len("/prompt"):])
            except ValueError:
                session.note(PROMPT_USAGE)
                continue
            expanded = expand_prompt(session, conversation_id, words)
            if expanded is None:
                continue
            try:
                confirm = read("Send it? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm not in ("", "y", "yes"):
                session.note("(not sent)")
                continue
            # The user's own message now, sent as any other (V3-P60 §4).
            _, after = session.submit_and_follow(conversation_id, after, expanded)
            continue
        if text.startswith("/") and not text.startswith("//"):
            session.note("unknown command — /help")
            continue
        _, after = session.submit_and_follow(conversation_id, after, text[1:] if text.startswith("//") else text,
                                             [str(f["file_id"]) for f in pending])
        settle_pending(session, pending)


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
    lst.add_argument("--offset", type=int, default=0)
    show = sub.add_parser("show", help="show a conversation")
    show.add_argument("id")
    delete = sub.add_parser("delete", help="move a conversation to the trash; deleted for good after the server's days")
    delete.add_argument("id")
    delete.add_argument("--now", action="store_true", help="delete it for good right away")
    delete.add_argument("--yes", action="store_true", help="do not ask first")
    trash = sub.add_parser("trash", help="the conversations in the trash")
    trash.add_argument("--limit", type=int, default=50)
    trash.add_argument("--offset", type=int, default=0)
    restore = sub.add_parser("restore", help="bring a conversation back from the trash")
    restore.add_argument("id")
    new = sub.add_parser("new", help="create a conversation")
    new.add_argument("--title")
    send = sub.add_parser("send", help="send one message and follow it")
    send.add_argument("id")
    send.add_argument("text", nargs="?", help="the message, or - to read it from stdin")
    send.add_argument("--attach", action="append", metavar="FILE", help="upload and attach a file (repeatable)")
    files = sub.add_parser("files", help="a conversation's files")
    files.add_argument("id")
    files.add_argument("--limit", type=int, default=50)
    files.add_argument("--offset", type=int, default=0)
    download = sub.add_parser("download", help="download a file of a conversation")
    download.add_argument("id")
    download.add_argument("file_id")
    download.add_argument("--version", type=int, default=1)
    download.add_argument("-o", "--output", help="where to write it (default: its name, here)")
    download.add_argument("--force", action="store_true", help="overwrite an existing file")
    compact = sub.add_parser("compact", help="summarize the earlier messages now")
    compact.add_argument("id")
    cont = sub.add_parser("continue", help="continue an unanswered message")
    cont.add_argument("id")
    tools = sub.add_parser("tools", help="the MCP servers a conversation uses")
    tools.add_argument("id")
    tools.add_argument("choice", nargs="?", choices=sorted(_CHOICES))
    tools.add_argument("source", nargs="?")
    model = sub.add_parser("model", help="the model a conversation uses; choose another from its next turn")
    model.add_argument("id")
    model.add_argument("model", nargs="?", help="a model the server offers, or 'default'")
    model.add_argument("effort", nargs="?", help="a reasoning effort the model offers")
    pause = sub.add_parser("pause", help="finish the current step, then stop")
    pause.add_argument("id")
    cancel = sub.add_parser("cancel", help="stop now; when idle, close an open question or a paused turn's steps")
    cancel.add_argument("id")
    rename = sub.add_parser("rename", help="rename a conversation (no title: the default name)")
    rename.add_argument("id")
    rename.add_argument("title", nargs="?")
    sub.add_parser("status", help="the server's models, whether commands can run, and the context budget")
    instructions = sub.add_parser("instructions", help="your instructions for every conversation: show, set or clear")
    instructions.add_argument("action", nargs="?", choices=["set", "clear"])
    instructions.add_argument("file", nargs="?", help="set: a UTF-8 text file, or - for standard input")
    instructions.add_argument("--yes", action="store_true", help="clear without asking")
    memory = sub.add_parser("memory", help="the notes HIMARI keeps about you: list, add, edit or rm")
    memory.add_argument("action", nargs="?", choices=["add", "edit", "rm"])
    memory.add_argument("rest", nargs="*", help="add: CATEGORY TEXT|-; edit: ID TEXT|-; rm: ID")
    memory.add_argument("--category", help="edit: move the note to this category")
    memory.add_argument("--yes", action="store_true", help="rm without asking")
    workspace = sub.add_parser("workspace", help="folders on this server registered as workspaces: list, add or rm")
    workspace.add_argument("action", nargs="?", choices=["add", "rm"])
    workspace.add_argument("rest", nargs="*", help="add: PATH; rm: ID or PATH")
    workspace.add_argument("--label", help="add: the name shown (default: the folder's name)")
    workspace.add_argument("--note", help="add: a short note the model sees")
    workspace.add_argument("--yes", action="store_true", help="rm without asking")
    attach = sub.add_parser("attach", help="a conversation's folders; attach one (it becomes the working directory)")
    attach.add_argument("id")
    attach.add_argument("folder", nargs="?", help="a registered folder (id or path), or a new path")
    attach.add_argument("--label", help="a new path: the name shown")
    detach = sub.add_parser("detach", help="detach a folder from a conversation (the folder stays)")
    detach.add_argument("id")
    detach.add_argument("folder", help="the folder's path (or binding id)")
    workdir = sub.add_parser("workdir", help="make an attached folder the conversation's working directory")
    workdir.add_argument("id")
    workdir.add_argument("folder", help="the folder's path (or binding id)")
    return p


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], int]] = {
    "chat": cmd_chat, "list": cmd_list, "show": cmd_show, "new": cmd_new, "send": cmd_send, "continue": cmd_continue,
    "compact": cmd_compact, "tools": cmd_tools, "model": cmd_model, "pause": cmd_pause, "cancel": cmd_cancel,
    "status": cmd_status, "rename": cmd_rename, "delete": cmd_delete, "trash": cmd_trash, "restore": cmd_restore,
    "files": cmd_files, "download": cmd_download, "instructions": cmd_instructions, "memory": cmd_memory,
    "workspace": cmd_workspace, "attach": cmd_attach, "detach": cmd_detach, "workdir": cmd_workdir,
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
