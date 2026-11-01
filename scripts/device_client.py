"""The reference device client (V3-P83 §9.4): what a companion app does, as a running example.

It pairs (and sends the same pairing again, as an app that lost the answer
would), opens the event stream and reads ``hello``, sends its activity,
uploads its screen and sends a message with it (V3-P84 §4), follows
``live`` of that job (by its ``job_id``, §6.3) to ``done`` — the text
without marks, each mood and each move at its place — reads the recorded
answer's ``display``, and sends the same screen again as an app that lost
the answer would (409 ``screen_already_sent``, §7.8).

    python scripts/device_client.py --server https://himari.example --code ABCD-EFGH "안녕"
    python scripts/device_client.py --self-test

``--self-test`` serves HIMARI on a loopback port with a scripted model, makes
a pairing code there and checks every step (CI runs it). The token is made
here and kept in memory only; an app keeps it in the OS store (§9.1).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import secrets
import struct
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

TIME_ZONE = "Asia/Seoul"


def new_token() -> str:
    """``hd_`` and 32 random bytes from the OS, URL-safe base64 without padding (§4.1, §9.1)."""
    return "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


async def frames(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """Server-Sent Events as (event, data); comment lines are skipped."""
    name, data = "", ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            name = line[len("event: "):]
        elif line.startswith("data: "):
            data = line[len("data: "):]
        elif not line and name:
            yield name, json.loads(data)
            name, data = "", ""


class Refused(Exception):
    def __init__(self, response: httpx.Response) -> None:
        error = response.json().get("error", {}) if response.content else {}
        super().__init__(f"{response.status_code} {error.get('code')}: {error.get('message')}")
        self.status, self.code = response.status_code, error.get("code")


@dataclass
class Report:
    device: dict[str, Any] = field(default_factory=dict)
    resent: dict[str, Any] = field(default_factory=dict)
    hello: dict[str, Any] = field(default_factory=dict)
    current_device: str | None = None
    conversation: str = ""
    text: str = ""  # what live sent for the answer's Run
    moods: list[tuple[int, str]] = field(default_factory=list)
    moves: list[tuple[int, int, int]] = field(default_factory=list)
    screen: dict[str, Any] = field(default_factory=dict)  # the uploaded screen's file view
    job_id: str | None = None  # from the message's 202
    started_job_id: str | None = None  # from the event stream's "job started"
    live_job_ids: set[str | None] = field(default_factory=set)
    screen_again: str | None = None  # the error code of sending the same screen again
    display: dict[str, Any] = field(default_factory=dict)  # the recorded answer's
    events: list[str] = field(default_factory=list)  # the event stream's, in order
    # V3-P88: whether the server takes the signals, and what a lock, an unlock and a quiet told
    signals: bool = False
    locked_current: str | None = "unset"
    unlocked_current: str | None = "unset"
    quiet: dict[str, Any] | None = None


def screen_png(width: int = 1280, height: int = 720) -> bytes:
    """A blank screen, as an app's capture would be sized (V3-P84 §3.3)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


async def pair(http: httpx.AsyncClient, code: str, token: str) -> tuple[dict[str, Any], str]:
    """Appendix A.1 and §9.1 rule 11: a token another device has is replaced
    once; too many pairings waiting is tried again a little later."""
    for attempt in range(5):
        response = await http.post("/api/devices/pair", json={"token": token},
                                   headers={"authorization": f"Pairing {code}"})
        if response.status_code == 200:
            return response.json(), token
        refused = Refused(response)
        if refused.code == "token_in_use" and attempt == 0:
            token = new_token()
        elif refused.code == "pairing_busy":
            await asyncio.sleep(2)
        else:
            raise refused
    raise RuntimeError("pairing kept failing")


async def run(server: str, code: str, text: str, *, verify: bool = True) -> Report:
    report = Report()
    async with httpx.AsyncClient(base_url=server, timeout=60, verify=verify) as http:
        report.device, token = await pair(http, code, new_token())
        report.resent, _ = await pair(http, code, token)  # the lost answer: the same answer again
        http.headers["authorization"] = f"Bearer {token}"
        events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def read_events() -> None:
            while True:  # one stream, opened again when it ends (§9.1 rule 6)
                async with http.stream("GET", "/api/events") as response:
                    if response.status_code != 200:
                        raise Refused(response)
                    async for event in frames(response):
                        report.events.append(event[0])
                        await events.put(event)

        reader = asyncio.create_task(read_events())
        try:
            name, report.hello = await asyncio.wait_for(events.get(), 30)
            assert name == "hello", name
            response = await http.post("/api/device/presence", json={"idle_seconds": 0, "time_zone": TIME_ZONE})
            if response.status_code != 204:
                raise Refused(response)
            report.current_device = (await http.get("/api/device")).json()["current_device"]
            if report.hello.get("features", {}).get("presence_signals") == 1:  # V3-P88 §14: only then
                await signals(http, events, report)
            recent = report.hello["recent_conversation"]
            if recent is None:
                response = await http.post("/api/conversations", json={})
                if response.status_code != 201:
                    raise Refused(response)
                recent = response.json()
            report.conversation = recent["id"]
            response = await http.post(f"/api/conversations/{report.conversation}/uploads", content=screen_png(),
                                       headers={"content-type": "image/png"})
            if response.status_code != 201:
                raise Refused(response)
            report.screen = response.json()
            message = {"text": text, "screen": report.screen["file_id"]}
            response = await http.post(f"/api/conversations/{report.conversation}/messages", json=message)
            if response.status_code != 202:
                raise Refused(response)
            report.job_id = response.json()["job_id"]
            me = {"kind": "device", "device_id": report.device["device_id"]}
            while True:  # live opens at this device's own job's start (§8.4, §9.1 rule 7)
                name, data = await asyncio.wait_for(events.get(), 60)
                if name == "job" and data.get("state") == "started" and data.get("origin") == me:
                    report.started_job_id = data.get("job_id")
                    break
            await follow(http, report)
            items = (await http.get(f"/api/conversations/{report.conversation}/events")).json()["items"]
            report.display = [i for i in items if i["kind"] == "assistant_final"][-1]["display"]
            response = await http.post(f"/api/conversations/{report.conversation}/messages", json=message)
            report.screen_again = Refused(response).code if response.status_code != 202 else None
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    return report


SIGNAL = {"idle_seconds": 0, "time_zone": TIME_ZONE, "session": "active",
          "activity": {"category": "coding", "app": "Visual Studio Code", "for_seconds": 120, "fullscreen": False},
          "media": None, "mic_or_camera": False}


async def next_named(events: asyncio.Queue[tuple[str, Any]], name: str,
                     wanted: Callable[[Any], bool] = lambda data: True) -> Any:
    """The next ``name`` event that is ``wanted`` (earlier ones, e.g. the first signal's ``current``, are passed)."""
    while True:
        got, data = await asyncio.wait_for(events.get(), 30)
        if got == name and wanted(data):
            return data


async def signals(http: httpx.AsyncClient, events: asyncio.Queue[tuple[str, Any]], report: Report) -> None:
    """V3-P88: the whole snapshot; a lock leaves the device away at once, the first signal after the
    unlock (marked ``after``) makes it current again; the user's quiet reaches every stream."""
    async def send(body: dict[str, Any]) -> None:
        response = await http.post("/api/device/presence", json=body)
        if response.status_code != 204:
            raise Refused(response)

    report.signals = True
    await send(SIGNAL)
    await send({"idle_seconds": 0, "time_zone": TIME_ZONE, "session": "locked"})
    report.locked_current = (await next_named(events, "current", lambda d: d["device_id"] is None))["device_id"]
    await send({**SIGNAL, "after": "unlock"})
    report.unlocked_current = (await next_named(events, "current", lambda d: d["device_id"] is not None))["device_id"]
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    response = await http.put("/api/presence/quiet", json={"until": until})
    if response.status_code != 200:
        raise Refused(response)
    report.quiet = await next_named(events, "quiet")
    response = await http.put("/api/presence/quiet", json={"until": None})
    if response.status_code != 200:
        raise Refused(response)
    await next_named(events, "quiet")


async def follow(http: httpx.AsyncClient, report: Report) -> None:
    """The answer as it is written: a snapshot starts a Run's text over, a delta adds to it (appendix A.4).
    Only frames of this device's job count (V3-P84 §6.3); a snapshot of a Run seen before plays no move again."""
    played: dict[str, int] = {}  # run_id → moves taken (§7.4)
    async with http.stream("GET", f"/api/conversations/{report.conversation}/live") as response:
        if response.status_code != 200:
            raise Refused(response)
        async for name, data in frames(response):
            if name in ("snapshot", "delta", "end"):
                report.live_job_ids.add(data.get("job_id"))
                if data.get("job_id") != report.job_id:
                    continue
            if name == "snapshot":
                report.text, report.moods = data["text"], [(m["at"], m["mood"]) for m in data["moods"]]
                fresh = data["moves"][played.get(data["run_id"], 0):]
            elif name == "delta":
                report.text += data["text"]
                report.moods += [(m["at"], m["mood"]) for m in data["moods"]]
                fresh = data["moves"]
            elif name == "done":
                return
            else:
                continue
            played[data["run_id"]] = played.get(data["run_id"], 0) + len(fresh)
            report.moves += [(m["at"], m["x"], m["y"]) for m in fresh]


def show(report: Report) -> None:
    print(f"paired: {report.device['name']} ({report.device['device_id']}); the same pairing again: {report.resent}")
    print(f"hello: current device {report.hello['current_device']}, moods {', '.join(report.hello['moods'])}")
    print(f"current device after the signal: {report.current_device}")
    print(f"answer: {report.text!r}")
    print(f"moods: {report.moods}")
    print(f"screen: {report.screen.get('name')}; moves: {report.moves}; job: {report.job_id}; "
          f"the same screen again: {report.screen_again}")
    print(f"signals: {report.signals}; current when locked: {report.locked_current}, after the unlock: "
          f"{report.unlocked_current}; quiet: {report.quiet}")
    print(f"events: {', '.join(report.events)}")


# --- self test ---------------------------------------------------------------------------------

ANSWER = "[[mood:happy]]안녕! 오늘은 `[[mood:sad]]` 대신 [[mood:thinking]]뭘 할까? [[move:1043,88]]여기 봐!"


def self_test() -> int:
    import uvicorn

    from himari.bootstrap.application import Application
    from himari.common import PrincipalId, TextBlock
    from himari.interfaces.web.app import create_app
    from himari.interfaces.web.config import WebConfig
    from himari.provider.api import NormalizedFinal
    from himari.provider.scripted import ScriptedAdapter

    with tempfile.TemporaryDirectory() as tmp:
        root = str(Path(tmp, "data"))
        with __import__("socket").socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        opened: dict[str, Any] = {}

        async def opener() -> Application:
            provider = ScriptedAdapter([NormalizedFinal((TextBlock(ANSWER),))], streaming=True, piece_delay=0.01,
                                       first_delay=0.5)
            application = await Application.open(root, provider, model_id="scripted-model", sandbox=False)
            opened["app"], opened["loop"] = application, asyncio.get_running_loop()
            return application

        config = WebConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), frozenset({f"http://127.0.0.1:{port}"}),
                           False, root, "scripted-model", None, False)
        server = uvicorn.Server(uvicorn.Config(create_app(config, opener), host="127.0.0.1", port=port,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started and "app" in opened:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("the server did not start")
        try:
            made = asyncio.run_coroutine_threadsafe(opened["app"].devices.create(PrincipalId("operator"), "데스크톱"),
                                                    opened["loop"]).result(10)
            report = asyncio.run(run(f"http://127.0.0.1:{port}", made.code, "안녕"))
        finally:
            server.should_exit = True
    show(report)
    problems = check(report)
    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr)
    print("device client: OK" if not problems else "device client: FAILED")
    return 1 if problems else 0


def check(report: Report) -> list[str]:
    plain = "안녕! 오늘은 `[[mood:sad]]` 대신 뭘 할까? 여기 봐!"
    expected = [(0, "happy"), (len("안녕! 오늘은 `[[mood:sad]]` 대신 "), "thinking")]
    moves = [(len("안녕! 오늘은 `[[mood:sad]]` 대신 뭘 할까? "), 1043, 88)]
    wanted = {
        "paired as d1": report.device == {"device_id": "d1", "name": "데스크톱"},
        "the same pairing again gets the same answer": report.resent == report.device,
        "hello names this device": report.hello.get("device") == report.device,
        "the signal makes it current": report.current_device == "d1",
        "live sends the text without marks": report.text == plain,
        "each mood at its place": report.moods == expected,
        "the recorded answer's display is the same": report.display == {
            "text": plain, "moods": [{"at": at, "mood": mood} for at, mood in expected],
            "moves": [{"at": at, "x": x, "y": y} for at, x, y in moves]},
        "the screen was named by the server": str(report.screen.get("name", "")).startswith("screen-"),
        "each move once at its place": report.moves == moves,
        "the 202, the job event and live name one job": report.job_id is not None
        and report.started_job_id == report.job_id and report.live_job_ids == {report.job_id},
        "the same screen again is refused as sent": report.screen_again == "screen_already_sent",
        "the stream told the job's start and end": "job" in report.events and "conversation" in report.events,
        "the server takes the activity signals": report.signals,
        "a lock leaves the device away at once": report.locked_current is None,
        "the unlock makes it current again": report.unlocked_current == "d1",
        "the quiet reaches the device's stream": report.quiet is not None and report.quiet.get("until") is not None,
        "a device gets no signals notices": "devices" not in report.events,
    }
    return [name for name, ok in wanted.items() if not ok]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("text", nargs="?", default="안녕")
    parser.add_argument("--server", help="HIMARI's address, https:// (http:// only on this computer)")
    parser.add_argument("--code", help="the pairing code from Settings → Devices")
    parser.add_argument("--self-test", action="store_true", help="serve HIMARI here with a scripted model and check")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.server or not args.code:
        parser.error("--server and --code (or --self-test)")
    address = urlsplit(args.server)
    if address.scheme != "https" and address.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("use https:// (§9.1 rule 4)")
    show(asyncio.run(run(args.server.rstrip("/"), args.code, args.text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
