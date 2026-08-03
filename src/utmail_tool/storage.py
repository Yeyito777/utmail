from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import UnsafeFileError


def ensure_private_directory(path: Path) -> None:
    path = path.expanduser()
    if path.exists():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeFileError(f"private state path is not a real directory: {path}")
        path.chmod(0o700)
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UnsafeFileError(f"could not open the private session lock: {exc.strerror}") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        # fdopen owns fd on the normal path; close only if construction failed.
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_private_directory(path.parent)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def read_private_json(path: Path) -> Any:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeFileError("the saved UTmail session is not a regular private file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(0o600)
    if info.st_size > 256 * 1024:
        raise UnsafeFileError("the saved UTmail session is unexpectedly large")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise UnsafeFileError("the saved UTmail session is unreadable or invalid") from None
