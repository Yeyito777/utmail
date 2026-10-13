"""Minimal client for the Exocortex daemon's newline-delimited JSON socket.

Only the external-notification registry and supervised-daemon management
commands are used. This module never imports daemon code or writes into the
Exocortex data directory.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

# src/utmail_tool/exocortex.py -> utmail -> helper-tools -> exocortex checkout
EXOCORTEX_ROOT = Path(__file__).resolve().parents[4]


class ExocortexError(RuntimeError):
    """The daemon is unreachable or rejected a request."""


def socket_path() -> Path:
    override = os.environ.get("EXOCORTEX_SOCKET", "").strip()
    if override:
        return Path(override)
    config = os.environ.get("EXOCORTEX_CONFIG_DIR", "").strip()
    config_root = Path(config) if config else EXOCORTEX_ROOT / "config"
    return config_root / "runtime" / "exocortexd.sock"


def request(command_type: str, response_type: str, *, timeout_seconds: float = 10, **fields: Any) -> dict[str, Any]:
    """Send one command and return the response event matching its reqId and type."""
    path = socket_path()
    if not path.exists():
        raise ExocortexError("exocortexd is not running")

    req_id = f"utmail_{os.getpid()}_{uuid.uuid4().hex}"
    payload = {"type": command_type, "reqId": req_id}
    payload.update({key: value for key, value in fields.items() if value is not None})

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        try:
            sock.connect(str(path))
            sock.sendall((json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError as exc:
            raise ExocortexError(f"could not reach exocortexd: {exc.strerror or type(exc).__name__}") from None
        buffer = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                raise ExocortexError(f"exocortexd did not reply: {exc.strerror or type(exc).__name__}") from None
            if not chunk:
                raise ExocortexError("connection closed before exocortexd replied")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("reqId") != req_id:
                    continue
                if event.get("type") == "error":
                    raise ExocortexError(str(event.get("message") or "exocortexd returned an error"))
                if event.get("type") == response_type:
                    return event
    finally:
        sock.close()


def manage_external_tool_daemon(tool_name: str, action: str) -> dict[str, Any]:
    event = request("manage_external_tool_daemon", "external_tool_daemon_result", toolName=tool_name, action=action)
    return event.get("status") or {}


def register_external_notification_source(tool_name: str, source: dict[str, str]) -> dict[str, Any]:
    event = request("register_external_notification_source", "external_notification_source", toolName=tool_name, source=source)
    return event.get("source") or {}


def list_external_notification_subscriptions(*, tool_name: str, source_id: str | None = None) -> list[dict[str, Any]]:
    event = request(
        "list_external_notification_subscriptions",
        "external_notification_subscriptions",
        toolName=tool_name,
        sourceId=source_id,
    )
    return list(event.get("subscriptions") or [])


def subscribe_external_notification(
    tool_name: str,
    source: dict[str, str],
    conv_id: str,
    *,
    delivery: str,
) -> dict[str, Any]:
    event = request(
        "subscribe_external_notification",
        "external_notification_subscription",
        toolName=tool_name,
        sourceId=source["id"],
        sourceLabel=source["label"],
        sourceDescription=source.get("description"),
        convId=conv_id,
        delivery=delivery,
    )
    return event.get("subscription") or {}


def unsubscribe_external_notification(tool_name: str, source_id: str, conv_id: str) -> dict[str, Any]:
    return request(
        "unsubscribe_external_notification",
        "external_notification_subscriptions",
        toolName=tool_name,
        sourceId=source_id,
        convId=conv_id,
    )


def publish_external_notification(
    tool_name: str,
    source_id: str,
    event_id: str,
    text: str,
    *,
    data: Any = None,
    occurred_at: int | None = None,
) -> dict[str, Any]:
    return request(
        "publish_external_notification",
        "external_notification_publish_result",
        timeout_seconds=30,
        toolName=tool_name,
        sourceId=source_id,
        eventId=event_id,
        text=text,
        data=data,
        occurredAt=occurred_at,
    )
