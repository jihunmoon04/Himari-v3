"""git push for the machine profile (V3-P74 §12): the runner's half.

The repository is the model's to change: its ``.git/config`` and hooks may
run anything git lets them. So nothing of it runs here. Inside the sandbox
(the server, ``_push``) git reads the push URL and writes a bundle of the
one branch to stdout, which lands in a file of the runner's own. Here the
URL is checked against the configured ``repos``, the bundle is fetched into
a new, empty bare repository and pushed from there: a fixed environment, no
system or user configuration, no hooks, no credential helpers, and only the
transport each step needs (``file`` to read the bundle, ``https`` to push).

The refspec is ``refs/heads/<branch>:refs/heads/<branch>``: never forced,
never a deletion. The token is the configured fine-grained one, handed to
the push alone through ``GIT_ASKPASS``; it is not in the runner's
environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from himari.runner import api
from himari.runner.proxy import Refused, normalize

MAX_BUNDLE_BYTES = 2 * 1024 ** 3
STEP_SECONDS = 120
PUSH_SECONDS = 300
MAX_MESSAGE_CHARS = 2000
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_BRANCH = re.compile(r"[A-Za-z0-9._/+-]+")
_ASKPASS = '#!/bin/sh\ncase "$1" in\n  Username*) echo x-access-token ;;\n  *) cat "$HIMARI_PUSH_TOKEN_FILE" ;;\nesac\n'
_SHARED = ("-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "core.fsmonitor=false",
           "-c", "protocol.allow=never")


def push_repos(entries: Iterable[str]) -> tuple[str, ...]:
    """The configured repositories: ``host/owner/name``, where ``owner`` and
    ``name`` may be ``*`` (ValueError if one is not like that)."""
    normal = []
    for entry in entries:
        host, _, path = entry.partition("/")
        segments = path.split("/")
        try:
            host = normalize(host)
        except Refused as refused:
            raise ValueError(f"{entry}: {refused}") from None
        if len(segments) != 2 or not all(s == "*" or _SEGMENT.fullmatch(s) for s in segments):
            raise ValueError(f"{entry}: host/owner/name, where owner and name may be *")
        normal.append(f"{host}/{segments[0]}/{segments[1].removesuffix('.git')}")
    return tuple(normal)


def remote(url: str, repos: Sequence[str]) -> str:
    """The push URL as pushed to (``https://host/owner/name.git``), or
    RunnerError: https only, no user, port, query or fragment, and its
    ``host/owner/name`` among ``repos`` (case is not significant)."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise api.RunnerError(api.ACCESS, "the remote is not a URL") from None
    if parts.scheme != "https":
        raise api.RunnerError(api.ACCESS, "git_push pushes to https:// remotes only")
    if parts.username is not None or parts.password is not None or port is not None or parts.query \
            or parts.fragment or not parts.hostname:
        raise api.RunnerError(api.ACCESS, "the remote URL may not carry a user, a port, a query or a fragment")
    try:
        host = normalize(parts.hostname)
    except Refused:
        raise api.RunnerError(api.ACCESS, "the remote's host is not a host name") from None
    segments = parts.path.strip("/").split("/")
    if len(segments) != 2 or not all(_SEGMENT.fullmatch(s) for s in segments):
        raise api.RunnerError(api.ACCESS, "the remote is not https://host/owner/name")
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not name:
        raise api.RunnerError(api.ACCESS, "the remote is not https://host/owner/name")
    seen = f"{host}/{owner}/{name}".lower().split("/")
    if not any(all(p in ("*", s) for s, p in zip(seen, pattern.lower().split("/"), strict=True))
               for pattern in repos):
        raise api.RunnerError(api.ACCESS, f"{host}/{owner}/{name} is not one of the repositories this server "
                                          f"pushes to ({', '.join(repos)})")
    return f"https://{host}/{owner}/{name}.git"


def check_branch(branch: object) -> str:
    """A branch name git accepts, from a conservative subset: letters,
    digits, ``._/+-``; no component starting with ``.`` or ending in
    ``.lock``; no ``..``, ``//``, leading ``-``, ``+`` or ``/``, or trailing ``/``
    or ``.``."""
    if not isinstance(branch, str) or not 0 < len(branch) <= 200 or not _BRANCH.fullmatch(branch):
        raise api.RunnerError(api.INVALID, "branch is a branch name (letters, digits, . _ / + -)")
    parts = branch.split("/")
    if (branch.startswith(("-", "+")) or ".." in branch or branch.endswith(".") or branch == "HEAD"
            or any(not p or p.startswith(".") or p.endswith(".lock") for p in parts)):
        raise api.RunnerError(api.INVALID, f"{branch} is not a valid branch name")
    return branch


@dataclass(frozen=True)
class Pushed:
    url: str
    branch: str
    status: str  # "pushed", "created", "up_to_date", "rejected"
    old: str | None
    new: str
    message: str  # git's summary, e.g. "[rejected] (non-fast-forward)"; or the remote's words

    def result(self) -> dict[str, object]:
        return {"url": self.url, "branch": self.branch, "status": self.status, "old": self.old, "new": self.new,
                "message": self.message}


class Pusher:
    """The runner's push steps, outside the sandbox."""

    def __init__(self, repos: Sequence[str], token_file: str, state_dir: str, *, git: str | None = None) -> None:
        self.repos = tuple(repos)
        self.token_file = token_file
        self.state_dir = state_dir
        self.git = git or shutil.which("git", path="/usr/bin:/bin") or "/usr/bin/git"

    @contextlib.contextmanager
    def workdir(self) -> Iterator[str]:
        """A new private folder for one push (the bundle, the bare repository, git's home)."""
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="push-", dir=self.state_dir)
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def push(self, folder: str, bundle: str, branch: str, url: str) -> Pushed:
        """Fetch ``bundle`` (exactly ``refs/heads/<branch>``) into a new bare
        repository in ``folder`` and push it to ``url`` (already checked)."""
        if not os.access(self.token_file, os.R_OK):
            raise api.RunnerError(api.ACCESS, "git push is not set up on this server (no token)")
        home, bare = os.path.join(folder, "home"), os.path.join(folder, "repo.git")
        os.mkdir(home, 0o700)
        ref = f"refs/heads/{branch}"
        await self._git("init", home, ["init", "-q", "--bare", bare])
        heads = await self._git("bundle list-heads", home, ["--git-dir", bare, "-c", "protocol.file.allow=always",
                                                            "bundle", "list-heads", bundle])
        listed = [line.split(" ", 1) for line in heads.splitlines() if line]
        if len(listed) != 1 or listed[0][1] != ref or not _SHA.fullmatch(listed[0][0]):
            raise api.RunnerError(api.INVALID, f"the bundle must hold {ref} only")
        new = listed[0][0]
        await self._git("fetch", home, ["--git-dir", bare, "-c", "protocol.file.allow=always",
                                        "fetch", "-q", "--no-tags", bundle, f"{ref}:{ref}"])
        askpass = os.path.join(folder, "askpass")
        with open(os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as handle:
            handle.write(_ASKPASS)
        target, settings = self._destination(url)
        code, out, err = await self._run(
            home, ["--git-dir", bare, "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
                   *settings, "push", "--porcelain", "--no-verify", target, f"{ref}:{ref}"],
            {"GIT_ASKPASS": askpass, "HIMARI_PUSH_TOKEN_FILE": self.token_file}, PUSH_SECONDS)
        return _outcome(url, branch, new, code, out, err)

    def _destination(self, url: str) -> tuple[str, list[str]]:
        """Where git pushes, and its extra ``-c`` settings: the checked URL
        (tests send it to a local server instead)."""
        return url, []

    async def _git(self, step: str, home: str, args: list[str]) -> str:
        code, out, err = await self._run(home, args, {}, STEP_SECONDS)
        if code != 0:
            raise api.RunnerError(api.OTHER, f"git {step} failed: {_clip(err)}")
        return out

    async def _run(self, home: str, args: list[str], extra: dict[str, str], seconds: float
                   ) -> tuple[int, str, str]:
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", **extra}
        process = await asyncio.create_subprocess_exec(
            self.git, *_SHARED, *args, cwd=home, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            async with asyncio.timeout(seconds):
                out, err = await process.communicate()
        except TimeoutError:
            await _kill(process)
            raise api.RunnerError(api.OTHER, "git took too long and was stopped") from None
        except BaseException:  # cancelled: git and its helpers go too
            await _kill(process)
            raise
        assert process.returncode is not None
        return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, 9)
    await asyncio.shield(process.wait())


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_MESSAGE_CHARS else "…" + text[-MAX_MESSAGE_CHARS:]


def _outcome(url: str, branch: str, new: str, code: int, out: str, err: str) -> Pushed:
    """git push --porcelain's line for our ref: ``<flag>\\t<from>:<to>\\t<summary>``."""
    ref = f"refs/heads/{branch}"
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[1] == f"{ref}:{ref}":
            flag, summary = fields[0], fields[2]
            if flag == "!":
                return Pushed(url, branch, "rejected", None, new, _clip(f"{summary}\n{err}"))
            if flag == "=":
                return Pushed(url, branch, "up_to_date", new, new, summary)
            if flag == "*":
                return Pushed(url, branch, "created", None, new, summary)
            if flag == " " and ".." in summary:
                return Pushed(url, branch, "pushed", summary.split("..", 1)[0], new, summary)
    raise api.RunnerError(api.OTHER, f"git push failed (exit {code}): {_clip(err) or 'no message'}")
