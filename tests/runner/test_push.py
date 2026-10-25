"""git push (V3-P74 §12): the remote and branch checks, the runner's steps
against a local HTTPS git server, and the runner operation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gitserver import AVAILABLE, GitServer, LocalPusher, git
from runnerkit import RunnerKit, start_runner

from himari.environment.machine.client import RunnerClient
from himari.runner import api
from himari.runner.config import ConfigError, parse
from himari.runner import server as server_module
from himari.runner.push import Pusher, check_branch, push_repos, remote
from himari.sandbox.api import CommandResult
from himari.sandbox.backend import _spool

REPOS = push_repos(["github.com/owner/*"])
TOKEN = "github_pat_test"


@pytest.mark.parametrize(("url", "pushed"), [
    ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
    ("https://GitHub.com/Owner/repo\n", "https://github.com/Owner/repo.git"),
    ("https://github.com/owner/repo/", "https://github.com/owner/repo.git"),
])
def test_remotes_pushed_to(url: str, pushed: str) -> None:
    assert remote(url, REPOS) == pushed


@pytest.mark.parametrize(("url", "why"), [
    ("http://github.com/owner/repo.git", "https:// remotes only"),
    ("git@github.com:owner/repo.git", "https:// remotes only"),
    ("ssh://git@github.com/owner/repo.git", "https:// remotes only"),
    ("file:///tmp/repo.git", "https:// remotes only"),
    ("https://user@github.com/owner/repo.git", "user"),
    ("https://x:tok@github.com/owner/repo.git", "user"),
    ("https://github.com:8443/owner/repo.git", "port"),
    ("https://github.com/owner/repo.git?x=1", "query"),
    ("https://github.com/other/repo.git", "not one of the repositories"),
    ("https://github.com.evil.example/owner/repo.git", "not one of the repositories"),
    ("https://github.com/owner/group/repo.git", "https://host/owner/name"),
    ("https://github.com/owner/../other/repo.git", "https://host/owner/name"),
    ("https://127.0.0.1/owner/repo.git", "not a host name"),
    ("https://github.com/owner/.git", "https://host/owner/name"),
])
def test_remotes_refused(url: str, why: str) -> None:
    with pytest.raises(api.RunnerError, match=why) as refused:
        remote(url, REPOS)
    assert refused.value.category == api.ACCESS


def test_repository_patterns() -> None:
    assert push_repos(["GitHub.com/me/Repo.git", "github.com/*/*"]) == ("github.com/me/Repo", "github.com/*/*")
    assert remote("https://github.com/ME/repo", push_repos(["github.com/me/REPO"])) == "https://github.com/ME/repo.git"
    for bad in ("github.com/owner", "github.com/a/b/c", "10.0.0.1/a/b", "github.com/a b/c", "github.com/a*/b"):
        with pytest.raises(ValueError):
            push_repos([bad])
    base = {"client_uid": os.getuid()}
    assert parse(base).push is None
    config = parse({**base, "push": {"repos": ["github.com/owner/*"], "token_file": "/etc/t"}})
    assert config.push == ("github.com/owner/*",) and config.push_token == "/etc/t"
    refused: list[dict[str, Any]] = [{"push": []}, {"push": {"repos": ["x"]}}, {"push": {"repos": [], "token_file": "rel"}}]
    for raw in refused:
        with pytest.raises(ConfigError):
            parse({**base, **raw})


@pytest.mark.parametrize("branch", ["main", "feature/x", "fix-1.2", "a+b", "v1_2"])
def test_branch_names(branch: str) -> None:
    assert check_branch(branch) == branch


@pytest.mark.parametrize("branch", ["", "-f", "+main", ":main", "main:other", "a..b", "a//b", "/a", "a/", ".a",
                                    "a/.b", "a.lock", "a.", "HEAD", "a b", "a~1", "a^", "a@{1}", "a\\b", "x" * 201,
                                    None, 1])
def test_branch_names_refused(branch: object) -> None:
    with pytest.raises(api.RunnerError):
        check_branch(branch)


# --- the runner's steps, against a local HTTPS git server ---------------------------------------

needs_git = pytest.mark.skipif(not AVAILABLE, reason="needs git-http-backend and openssl")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[GitServer]:
    started = GitServer(tmp_path / "server", TOKEN)
    started.bare("repo")
    yield started
    started.close()


def token_file(folder: Path, token: str = TOKEN) -> str:
    path = folder / "token"
    path.write_text(token + "\n")
    path.chmod(0o600)
    return str(path)


def work(folder: Path) -> Path:
    """A repository with one commit on main."""
    folder.mkdir()
    git("init", "-q", "-b", "main", cwd=folder)
    commit(folder, "a")
    return folder


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git("add", name, cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).strip()


def bundle(repo: Path, *refs: str) -> str:
    path = repo.parent / f"{repo.name}.bundle"
    path.unlink(missing_ok=True)
    git("bundle", "create", "-q", str(path), *refs, cwd=repo)
    return str(path)


def push(pusher: Pusher, path: str, branch: str = "main") -> dict[str, object]:
    async def go() -> dict[str, object]:
        with pusher.workdir() as folder:
            pushed = await pusher.push(folder, path, branch, "https://github.com/owner/repo.git")
        return pushed.result()

    return asyncio.run(go())


@needs_git
def test_a_branch_is_created_pushed_kept_and_never_forced(server: GitServer, tmp_path: Path) -> None:
    pusher = LocalPusher(server, REPOS, token_file(tmp_path), str(tmp_path / "state"))
    repo = work(tmp_path / "work")
    first = git("rev-parse", "HEAD", cwd=repo).strip()
    assert push(pusher, bundle(repo, "refs/heads/main")) == {
        "url": "https://github.com/owner/repo.git", "branch": "main", "status": "created", "old": None, "new": first,
        "message": "[new branch]"}
    assert server.head("repo") == first and False in server.authorized  # asked, then the token
    second = commit(repo, "b")
    pushed = push(pusher, bundle(repo, "refs/heads/main"))
    assert pushed["status"] == "pushed" and pushed["new"] == second and server.head("repo") == second
    assert pushed["message"] == f"{first[:7]}..{second[:7]}" and pushed["old"] == first[:7]
    assert push(pusher, bundle(repo, "refs/heads/main"))["status"] == "up_to_date"
    other = work(tmp_path / "other")  # history the remote does not have under it
    rejected = push(pusher, bundle(other, "refs/heads/main"))
    assert rejected["status"] == "rejected" and str(rejected["message"]).startswith("[rejected] (fetch first)")
    assert server.head("repo") == second
    assert os.listdir(tmp_path / "state") == []  # every push's folder is removed


@needs_git
def test_the_bundle_holds_the_branch_only(server: GitServer, tmp_path: Path) -> None:
    pusher = LocalPusher(server, REPOS, token_file(tmp_path), str(tmp_path / "state"))
    repo = work(tmp_path / "work")
    git("branch", "other", cwd=repo)
    for refs in (("refs/heads/main", "refs/heads/other"), ("refs/heads/other",)):
        with pytest.raises(api.RunnerError, match="must hold refs/heads/main only"):
            push(pusher, bundle(repo, *refs))
    (tmp_path / "junk.bundle").write_text("not a bundle")
    with pytest.raises(api.RunnerError, match="bundle list-heads failed"):
        push(pusher, str(tmp_path / "junk.bundle"))
    assert server.head("repo") is None


@needs_git
def test_the_token(server: GitServer, tmp_path: Path) -> None:
    repo = work(tmp_path / "work")
    with pytest.raises(api.RunnerError, match="not set up") as refused:
        push(LocalPusher(server, REPOS, str(tmp_path / "missing"), str(tmp_path / "state")),
             bundle(repo, "refs/heads/main"))
    assert refused.value.category == api.ACCESS
    with pytest.raises(api.RunnerError, match="git push failed"):
        push(LocalPusher(server, REPOS, token_file(tmp_path, "wrong"), str(tmp_path / "state")),
             bundle(repo, "refs/heads/main"))
    assert server.head("repo") is None and not any(server.authorized)


# --- the runner operation (the sandbox's steps run unsandboxed here; the host gate runs them for real) ---

def call(kit: RunnerKit, args: dict[str, Any]) -> dict[str, Any]:
    result, _ = asyncio.run(RunnerClient(kit.socket).call(api.GIT_PUSH, args))
    return result


@pytest.fixture
def pushing(tmp_path: Path, server: GitServer) -> Iterator[tuple[RunnerKit, Path]]:
    kit = start_runner(tmp_path / "kit", push={"repos": ["github.com/owner/*"],
                                               "token_file": token_file(tmp_path)})
    assert kit.runner.pusher is not None
    kit.runner.pusher = LocalPusher(server, kit.runner.pusher.repos, kit.runner.pusher.token_file,
                                    kit.runner.config.state_dir)
    kit.sandbox.execute = True
    yield kit, kit.himari_home / "conversations/c1"
    kit.close()


@needs_git
def test_the_operation(pushing: tuple[RunnerKit, Path], server: GitServer) -> None:
    kit, folder = pushing
    folder.mkdir()
    repo = work(folder / "work")
    git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
    hooks = folder / "hooks"
    hooks.mkdir()
    marker = kit.home / "ran"
    for hook in ("pre-push", "post-update", "reference-transaction", "pre-receive"):
        (hooks / hook).write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
        (hooks / hook).chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=repo)
    git("config", "credential.helper", f"!echo helper >> {marker}; true", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo).strip()
    result = call(kit, {"repository": "work", "branch": "main", "cwd": str(folder), "write_roots": [str(folder)]})
    assert result["status"] == "created" and result["repository"] == str(repo) and server.head("repo") == head
    assert not marker.exists()  # the repository's hooks and helpers were not run outside
    argvs = [argv for argv, _, _ in kit.sandbox.calls]
    assert argvs == [["git", "remote", "get-url", "--push", "origin"], ["git", "bundle", "create", "-", "refs/heads/main"]]
    layout = kit.sandbox.calls[-1][1]
    assert layout.cwd == str(repo) and layout.writable == () and layout.proxy is None and layout.cache is None
    hello, _ = asyncio.run(RunnerClient(kit.socket).call(api.HELLO, {}))
    assert hello["push"] == ["github.com/owner/*"]


@needs_git
def test_the_operation_refuses(pushing: tuple[RunnerKit, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    kit, folder = pushing
    repo = work(folder)
    outside = work(kit.home / "projects/elsewhere")
    args = {"branch": "main", "cwd": str(folder), "write_roots": [str(folder)]}

    def refused(category: str, **more: Any) -> str:
        with pytest.raises(api.RunnerError) as error:
            call(kit, {**args, **more})
        assert error.value.category == category
        return str(error.value)

    assert "attach" in refused(api.OUTSIDE_WORKSPACES, repository=str(outside))
    assert "no 'origin' remote" in refused(api.INVALID, repository=str(repo))
    git("remote", "add", "origin", "git@github.com:owner/repo.git", cwd=repo)
    assert "https:// remotes only" in refused(api.ACCESS, repository=str(repo))
    git("remote", "set-url", "origin", "https://github.com/someone/repo.git", cwd=repo)
    assert "not one of the repositories" in refused(api.ACCESS, repository=str(repo))
    git("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=repo)
    assert "git bundle failed for nothere" in refused(api.INVALID, repository=str(repo), branch="nothere")
    assert "not a valid branch" in refused(api.INVALID, repository=str(repo), branch="a..b")
    with monkeypatch.context() as patched:
        patched.setattr(server_module, "MAX_BUNDLE_BYTES", 10)
        assert "over 2 GiB" in refused(api.TOO_LARGE, repository=str(repo))
    kit.sandbox.execute = False
    kit.sandbox.result = CommandResult(None, True, b"", b"", 0, 0, 1)
    assert "took too long" in refused(api.OTHER, repository=str(repo))
    kit.runner.pusher = None
    assert "not set up" in refused(api.ACCESS, repository=str(repo))


def test_a_spooled_stdout_keeps_its_first_bytes_and_counts_the_rest(tmp_path: Path) -> None:
    async def go() -> tuple[bytes, int]:
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100)
        stream.feed_eof()
        with open(tmp_path / "out", "wb") as sink:
            return await _spool(stream, sink.fileno(), 30)

    assert asyncio.run(go()) == (b"", 100) and (tmp_path / "out").read_bytes() == b"x" * 30
