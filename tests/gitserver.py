"""A local HTTPS git server for git push tests (V3-P74 §12): ``git-http-backend``
behind Basic auth with one token, on 127.0.0.1 with a certificate of its own;
and a ``Pusher`` that sends what it would push to GitHub there instead."""

from __future__ import annotations

import base64
import http.server
import shutil
import ssl
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

from himari.runner.push import Pusher

BACKEND = Path("/usr/lib/git-core/git-http-backend")
AVAILABLE = BACKEND.exists() and shutil.which("openssl") is not None and shutil.which("git") is not None


def git(*args: str, cwd: Path | str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


class GitServer:
    def __init__(self, base: Path, token: str) -> None:
        self.base, self.token = base, token
        self.root = base / "remote"
        self.root.mkdir(parents=True)
        self.cert, key = base / "cert.pem", base / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(self.cert)], check=True, capture_output=True)
        self.authorized: list[bool] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                self._cgi()

            def do_POST(self) -> None:
                self._cgi()

            def _cgi(self) -> None:
                expected = "Basic " + base64.b64encode(f"x-access-token:{server.token}".encode()).decode()
                ok = self.headers.get("Authorization") == expected
                server.authorized.append(ok)
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self._body()
                parts = urlsplit(self.path)
                env = {"GIT_PROJECT_ROOT": str(server.root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": parts.path,
                       "QUERY_STRING": parts.query, "REQUEST_METHOD": self.command, "REMOTE_USER": "x-access-token",
                       "REMOTE_ADDR": "127.0.0.1", "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(body)), "PATH": "/usr/bin:/bin", "HOME": str(server.base),
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                out = subprocess.run([str(BACKEND)], input=body, env=env, capture_output=True).stdout
                head, _, payload = out.partition(b"\r\n\r\n")
                status, headers = 200, []
                for line in head.decode("latin-1").split("\r\n"):
                    name, _, value = line.partition(": ")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                    elif name:
                        headers.append((name, value))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = b""
                    while size := int(self.rfile.readline().strip(), 16):
                        data += self.rfile.read(size)
                        self.rfile.readline()
                    self.rfile.readline()
                    return data
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def bare(self, name: str) -> Path:
        path = self.root / f"{name}.git"
        git("init", "-q", "--bare", "-b", "main", str(path))
        git("config", "http.receivepack", "true", cwd=path)
        return path

    def head(self, name: str, branch: str = "main") -> str | None:
        found = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                               cwd=self.root / f"{name}.git", capture_output=True, text=True)
        return found.stdout.strip() or None

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class LocalPusher(Pusher):
    """Pushes https://<host>/<owner>/<name>.git to the local server's <name>."""

    def __init__(self, server: GitServer, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.server = server

    def _destination(self, url: str) -> tuple[str, list[str]]:
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        return f"https://127.0.0.1:{self.server.port}/{name}", ["-c", f"http.sslCAInfo={self.server.cert}"]
