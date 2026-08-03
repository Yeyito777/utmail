from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .client import MAX_ATTACHMENT_BYTES, OwaClient
from .errors import UnsafeFileError, UsageError
from .mail import Mailbox


def normalize_attachment(value: dict[str, Any]) -> dict[str, Any]:
    odata_type = str(value.get("@odata.type") or value.get("__type") or "")
    return {
        "id": value.get("Id"),
        "name": value.get("Name") or "attachment",
        "contentType": value.get("ContentType") or "application/octet-stream",
        "size": value.get("Size"),
        "isInline": bool(value.get("IsInline")),
        "isFile": not odata_type or odata_type.lower().endswith("fileattachment"),
        "type": odata_type or None,
    }


def safe_filename(value: str) -> str:
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "_", value).strip()
    if value in {"", ".", ".."}:
        value = "attachment"
    return value[:240]


def prepare_output_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeFileError("attachment output path is not a real directory")
    else:
        parent = path.parent
        if parent.exists() and parent.is_symlink():
            raise UnsafeFileError("attachment output parent may not be a symlink")
        try:
            path.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise UnsafeFileError(f"could not create attachment output directory: {exc.strerror}") from None
    path.chmod(0o700)
    return path.resolve()


def private_write(path: Path, content: bytes, *, force: bool) -> None:
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise UnsafeFileError("attachment exceeds the 100 MiB safety limit")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeFileError("refused to replace a non-regular attachment path")
        if not force:
            raise UnsafeFileError(f"attachment already exists: {path.name}; pass --force to replace it")
    if not force:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            raise UnsafeFileError(f"attachment already exists: {path.name}; pass --force to replace it") from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class Attachments:
    def __init__(self, client: OwaClient, mailbox: Mailbox | None = None):
        self.client = client
        self.mailbox = mailbox or Mailbox(client)

    def list(self, message_id: str) -> list[dict[str, Any]]:
        return [normalize_attachment(value) for value in self.mailbox.attachment_rows(message_id)]

    def download(
        self,
        message_id: str,
        attachment_id: str,
        *,
        output_directory: str | Path,
        force: bool = False,
    ) -> dict[str, Any]:
        matches = [row for row in self.list(message_id) if row.get("id") == attachment_id]
        if len(matches) != 1:
            raise UsageError("attachment ID was not found uniquely on that message")
        metadata = matches[0]
        if not metadata["isFile"]:
            raise UsageError("only file attachments can be downloaded")
        size = metadata.get("size")
        if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
            raise UnsafeFileError("attachment exceeds the 100 MiB safety limit")
        response = self.client.get_bytes(
            Mailbox.attachment_value_url(message_id, attachment_id),
            max_bytes=MAX_ATTACHMENT_BYTES,
        )
        directory = prepare_output_directory(output_directory)
        path = directory / safe_filename(str(metadata["name"]))
        private_write(path, response.content, force=force)
        return {
            **metadata,
            "path": str(path),
            "bytes": len(response.content),
        }
