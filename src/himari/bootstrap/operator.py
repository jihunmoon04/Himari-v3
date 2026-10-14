"""The local operator's secret: the root of Web/CLI caller authentication.

``<data_root>/control/operator.secret`` holds exactly 32 random bytes as
unpadded URL-safe base64 (43 characters) plus one newline. It grants the
single P0 operator principal; it is never shown to the model, never inside
a Workspace and never logged.

Creation never exposes a partial file: a private ``O_EXCL|O_NOFOLLOW`` temp
file is written and fsynced, then published with ``link`` (which refuses to
replace an existing secret), and the directory is fsynced. Every read
re-checks the file: regular, owned by this user, mode 600, one name, exact
format — anything else is refused, never repaired.
"""

from __future__ import annotations

import base64
import errno
import hmac
import os
import re
import secrets
import stat

from himari.common import PrincipalId

OPERATOR = PrincipalId("operator")
SECRET_NAME = "operator.secret"
_FORMAT = re.compile(rb"\A[A-Za-z0-9_-]{43}\n\Z")


class OperatorSecretError(RuntimeError):
    pass


def check_private_dir(path: str) -> None:
    """``path`` must be a real directory (not a symlink) of this user that
    no other user can read, enter or change."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise OperatorSecretError(f"{path} does not exist") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OperatorSecretError(f"{path} is not a directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OperatorSecretError(f"{path} must be owned by this user with mode 700")


def ensure_private_dir(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    check_private_dir(path)


def _read(path: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise OperatorSecretError(f"{path} is a symbolic link") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise OperatorSecretError(f"{path} must be a private regular file (mode 600, one name)")
        data = os.read(fd, 64)
    finally:
        os.close(fd)
    if not _FORMAT.match(data) or len(base64.urlsafe_b64decode(data[:-1] + b"=")) != 32:
        raise OperatorSecretError(f"{path} is not a valid operator secret")
    return data


def read_operator_secret(control_dir: str) -> str:
    """For clients: read and verify, never create."""
    check_private_dir(control_dir)
    return _read(os.path.join(control_dir, SECRET_NAME))[:-1].decode()


def ensure_operator_secret(control_dir: str) -> str:
    ensure_private_dir(control_dir)
    final = os.path.join(control_dir, SECRET_NAME)
    if os.path.lexists(final):
        return read_operator_secret(control_dir)
    value = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=") + b"\n"
    temp = os.path.join(control_dir, f".{SECRET_NAME}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temp, final)  # no-clobber publication
    except FileExistsError:
        pass  # another process published first: use its secret
    finally:
        os.unlink(temp)
        dir_fd = os.open(control_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return read_operator_secret(control_dir)


def verify_operator_token(presented: str | None, secret: str) -> bool:
    """Constant-time comparison; an absent token never matches."""
    if not presented:
        return False
    return hmac.compare_digest(presented.encode(), secret.encode())
