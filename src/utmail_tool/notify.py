"""Publish newly arrived Inbox messages as Exocortex external notifications.

One supervised process polls the Inbox through the existing read-only
``OwaClient`` (HTTPS GET only) using a ``ReceivedDateTime`` cursor plus a
seen-ID set, and publishes one event per new message to the Exocortex
external-notification registry. Routing and filtering belong to Exocortex
subscriptions; this module only emits structured, stable event data.

Mail content is untrusted data: it is copied into event fields and never
interpreted. Nothing here marks, moves, flags, or deletes mail.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import exocortex
from .client import OwaClient
from .errors import BrowserImportError, SessionRejectedError, SessionRequiredError, UtmailError
from .mail import _received_at_or_none
from .paths import session_path, state_dir
from .renewal import load_or_refresh_session
from .session import OwaSession
from .storage import atomic_write_json, ensure_private_directory, read_private_json


TOOL_NAME = "utmail"
SOURCE = {
    "id": "inbox",
    "label": "UTmail · U of T Inbox",
    "description": "One event per newly arrived message in the University of Toronto Outlook Inbox, "
    "plus a single auth_needed event when the session needs human sign-in.",
}
SCHEMA_VERSION = 1
STATE_VERSION = 1

INBOX_MESSAGES = "/api/v2.0/me/mailfolders/inbox/messages"
LIST_FIELDS = (
    "Id,ConversationId,InternetMessageId,Subject,From,Sender,ReplyTo,ToRecipients,CcRecipients,"
    "ReceivedDateTime,SentDateTime,IsRead,IsDraft,HasAttachments,Importance,BodyPreview,"
    "InferenceClassification,Categories,Flag"
)
PAGE_SIZE = 50
BATCH_LIMIT = 100
# Messages can become visible slightly after their ReceivedDateTime; re-scan
# this window behind the cursor and let the seen set dedupe.
OVERLAP = timedelta(minutes=15)
SEEN_RETENTION = OVERLAP + timedelta(hours=1)
MAX_SEEN = 2000
MAX_PENDING = 200

DEFAULT_POLL_SECONDS = 60
MIN_POLL_SECONDS = 20
MAX_ERROR_DELAY = 15 * 60
AUTH_CONFIRMATIONS = 2
AUTH_RETRY_DELAYS = (5 * 60, 10 * 60, 20 * 60, 40 * 60, 60 * 60)
REGISTER_INTERVAL = 10 * 60

MAX_SUBJECT = 500
MAX_PREVIEW = 300
MAX_TEXT_PREVIEW = 200
MAX_RECIPIENTS = 50
MAX_ATTACHMENTS = 25
MAX_HEADER_VALUE = 300

AUTH_ERRORS = (SessionRequiredError, SessionRejectedError)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")


# ── State ────────────────────────────────────────────────────────────────


def state_path() -> Path:
    override = os.environ.get("UTMAIL_NOTIFY_STATE_FILE")
    return Path(override).expanduser() if override else state_dir() / "notify-state.json"


def lock_file() -> Path:
    override = os.environ.get("UTMAIL_NOTIFY_LOCK_FILE")
    return Path(override).expanduser() if override else state_dir() / "notify.lock"


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "mailbox": None,
        "initialized": False,
        "cursor": None,
        "catchUp": False,
        "seen": {},
        "pending": {},
        "auth": None,
        "lastPollAt": None,
        "lastSuccessAt": None,
        "lastError": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    state = empty_state()
    try:
        raw = read_private_json(path)
    except FileNotFoundError:
        return state
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return state
    for key in state:
        if key in raw:
            state[key] = raw[key]
    if not isinstance(state["seen"], dict):
        state["seen"] = {}
    if not isinstance(state["pending"], dict):
        state["pending"] = {}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    atomic_write_json(path, state)


# ── Event construction ───────────────────────────────────────────────────


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean(value: Any, limit: int) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    return text[:limit]


def _one_line(value: Any, limit: int) -> str:
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", str(value or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _domain(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


def _person(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    email = value.get("EmailAddress", value)
    if not isinstance(email, dict):
        return None
    address = email.get("Address")
    name = email.get("Name")
    if not address and not name:
        return None
    address = _clean(address, 320) if address else None
    return {
        "name": _clean(name, 200) if name else None,
        "address": address,
        "domain": _domain(address),
    }


def _people(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    people = [person for item in value if (person := _person(item)) is not None]
    return people[:MAX_RECIPIENTS]


def mailbox_aliases(mailbox: str) -> set[str]:
    address = mailbox.strip().lower()
    aliases = {address}
    local, _, domain = address.partition("@")
    if domain == "mail.utoronto.ca":
        aliases.add(f"{local}@utoronto.ca")
    return aliases


def recipient_role(mailbox: str, to: list[dict], cc: list[dict]) -> str:
    aliases = mailbox_aliases(mailbox)
    if any((person.get("address") or "").lower() in aliases for person in to):
        return "to"
    if any((person.get("address") or "").lower() in aliases for person in cc):
        return "cc"
    return "none"


def _lower_camel(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[0].lower() + value[1:]


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def header_classification(headers: Any) -> dict[str, Any] | None:
    """Extract cheap, server-stamped routing signals from Internet headers."""
    if not isinstance(headers, list):
        return None
    first: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = str(header.get("Name") or "").strip().lower()
        if name and name not in first:
            first[name] = _clean(header.get("Value"), 4096).strip()

    def value(name: str) -> str | None:
        found = first.get(name)
        return found[:MAX_HEADER_VALUE] if found else None

    antispam = first.get("x-microsoft-antispam", "")
    bcl = re.search(r"(?i)\bBCL:(\d+)", antispam)
    auth_results = first.get("authentication-results", "")
    results = {}
    for mechanism in ("spf", "dkim", "dmarc"):
        match = re.search(rf"(?i)\b{mechanism}=([a-z]+)", auth_results)
        results[mechanism] = match.group(1).lower() if match else None
    return_path = (first.get("return-path") or "").strip("<> ")
    return {
        "listId": value("list-id"),
        "listUnsubscribe": "list-unsubscribe" in first,
        "precedence": (value("precedence") or "").lower() or None,
        "autoSubmitted": (value("auto-submitted") or "").lower() or None,
        "spamConfidenceLevel": _int_or_none(first.get("x-ms-exchange-organization-scl")),
        "bulkComplaintLevel": int(bcl.group(1)) if bcl else None,
        "directionality": _lower_camel(value("x-ms-exchange-organization-messagedirectionality")),
        "authAs": _lower_camel(value("x-ms-exchange-organization-authas")),
        "authResults": results if auth_results else None,
        "returnPathDomain": _domain(return_path),
    }


def _attachment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    size = value.get("Size")
    return {
        "name": _clean(value.get("Name") or "attachment", 255),
        "contentType": _clean(value.get("ContentType"), 200) or None,
        "size": size if isinstance(size, int) else None,
        "isInline": bool(value.get("IsInline")),
    }


def event_id_for(message_id: str) -> str:
    return f"utmail:message:{message_id}"


def build_message_event(
    row: dict[str, Any],
    *,
    mailbox: str,
    headers: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    detected_at: datetime,
) -> dict[str, Any]:
    message_id = str(row["Id"])
    received = _received_at_or_none(row.get("ReceivedDateTime"))
    sender = _person(row.get("From")) or _person(row.get("Sender"))
    to = _people(row.get("ToRecipients"))
    cc = _people(row.get("CcRecipients"))
    flag = row.get("Flag")
    flag_status = flag.get("FlagStatus") if isinstance(flag, dict) else None
    categories = row.get("Categories")
    if not isinstance(categories, list):
        categories = []
    attachment_rows = [a for item in (attachments or []) if (a := _attachment(item)) is not None][:MAX_ATTACHMENTS]
    preview = _one_line(row.get("BodyPreview"), MAX_PREVIEW)
    subject = _clean(row.get("Subject"), MAX_SUBJECT)
    from_address = ((sender or {}).get("address") or "").lower()
    data = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "message",
        "mailbox": mailbox,
        "folder": "inbox",
        "messageId": message_id,
        "conversationId": row.get("ConversationId"),
        "internetMessageId": _clean(row.get("InternetMessageId"), 500) or None,
        "subject": subject,
        "from": sender,
        "sender": _person(row.get("Sender")),
        "replyTo": _people(row.get("ReplyTo")),
        "to": to,
        "cc": cc,
        "recipientRole": recipient_role(mailbox, to, cc),
        "fromSelf": bool(from_address) and from_address in mailbox_aliases(mailbox),
        "receivedAt": iso(received) if received else row.get("ReceivedDateTime"),
        "sentAt": row.get("SentDateTime"),
        "detectedAt": iso(detected_at),
        "preview": preview,
        "hasAttachments": bool(row.get("HasAttachments")),
        "attachments": attachment_rows,
        "isRead": bool(row.get("IsRead")),
        "importance": (str(row.get("Importance") or "normal")).lower(),
        "inferenceClassification": (str(row.get("InferenceClassification") or "")).lower() or None,
        "categories": [_clean(item, 100) for item in categories if isinstance(item, str)][:20],
        "flagStatus": _lower_camel(flag_status) or "notFlagged",
        "headers": header_classification(headers),
    }
    return {
        "eventId": event_id_for(message_id),
        "occurredAt": int(received.timestamp() * 1000) if received else None,
        "text": format_message_text(data),
        "data": data,
    }


def _display(person: dict[str, Any] | None) -> str:
    if not person:
        return "(unknown sender)"
    name, address = person.get("name"), person.get("address")
    if name and address and name.lower() != address.lower():
        return f"{_one_line(name, 100)} <{address}>"
    return str(address or name)


def format_message_text(data: dict[str, Any]) -> str:
    location = "Inbox"
    if data.get("inferenceClassification"):
        location += f" · {data['inferenceClassification'].capitalize()}"
    if not data.get("isRead"):
        location += " · unread"
    if data.get("importance") == "high":
        location += " · high importance"
    lines = [f"{location} [thread:{data.get('conversationId') or '-'}]"]
    lines.append(f"→ {_display(data.get('from'))}: {_one_line(data.get('subject'), 200) or '(no subject)'}")
    if data.get("preview"):
        lines.append(_one_line(data["preview"], MAX_TEXT_PREVIEW))
    if data.get("attachments"):
        names = ", ".join(_one_line(item["name"], 80) for item in data["attachments"][:5])
        more = len(data["attachments"]) - 5
        lines.append(f"Attachments: {names}" + (f" (+{more} more)" if more > 0 else ""))
    elif data.get("hasAttachments"):
        lines.append("Attachments: (names unavailable)")
    lines.append(f"[msg:{data['messageId']}]")
    return "\n".join(lines)


def build_auth_event(*, mailbox: str | None, error: UtmailError, since: datetime) -> dict[str, Any]:
    reason = "session_rejected" if isinstance(error, SessionRejectedError) else "session_required"
    started_ms = int(since.timestamp() * 1000)
    data = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "auth_needed",
        "mailbox": mailbox,
        "reason": reason,
        "error": _one_line(error.message, 300),
        "since": iso(since),
        "action": "utmail login --persistent",
    }
    text = (
        "UTmail cannot renew the Outlook session; human sign-in (Microsoft/U of T Duo) is needed.\n"
        f"Reason: {data['error']}\n"
        "Fix: run `utmail login --persistent` and complete sign-in. New-mail notifications resume automatically."
    )
    return {
        "eventId": f"utmail:auth-needed:{started_ms}",
        "occurredAt": started_ms,
        "text": text,
        "data": data,
    }


# ── Mailbox reads (GET-only through OwaClient) ───────────────────────────


class InboxReader:
    def __init__(self, client: OwaClient):
        self.client = client

    def newest(self, limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
        return self.client.collect(
            INBOX_MESSAGES,
            params={"$top": str(limit), "$select": LIST_FIELDS, "$orderby": "ReceivedDateTime desc"},
            limit=limit,
        )

    def since(self, moment: datetime, limit: int = BATCH_LIMIT) -> list[dict[str, Any]]:
        return self.client.collect(
            INBOX_MESSAGES,
            params={
                "$top": str(PAGE_SIZE),
                "$select": LIST_FIELDS,
                "$filter": "ReceivedDateTime ge " + iso(moment),
                "$orderby": "ReceivedDateTime asc",
            },
            limit=limit,
        )

    def headers(self, message_id: str) -> Any:
        payload = self.client.get_json(
            f"/api/v2.0/me/messages/{quote(message_id, safe='')}",
            params={"$select": "InternetMessageHeaders"},
        )
        return payload.get("InternetMessageHeaders")

    def attachments(self, message_id: str) -> list[dict[str, Any]]:
        # Select metadata only so file contents are never downloaded.
        return self.client.collect(
            f"/api/v2.0/me/messages/{quote(message_id, safe='')}/attachments",
            params={"$top": str(MAX_ATTACHMENTS), "$select": "Name,ContentType,Size,IsInline"},
            limit=MAX_ATTACHMENTS,
        )


def enrich(reader: InboxReader, row: dict[str, Any], warn: Callable[[str, str], None]) -> tuple[Any, list[dict[str, Any]] | None]:
    """Fetch headers and attachment metadata; degrade to None on non-auth failures."""
    message_id = str(row["Id"])
    headers = None
    attachments = None
    try:
        headers = reader.headers(message_id)
    except AUTH_ERRORS:
        raise
    except UtmailError as exc:
        warn("headers", f"Internet headers unavailable; continuing without them: {exc.message}")
    if row.get("HasAttachments"):
        try:
            attachments = reader.attachments(message_id)
        except AUTH_ERRORS:
            raise
        except UtmailError as exc:
            warn("attachments", f"Attachment names unavailable; continuing without them: {exc.message}")
    return headers, attachments


def _publish_accepted(result: dict[str, Any]) -> bool:
    return not any(d.get("status") == "failed" for d in (result.get("deliveries") or []))


def _delivery_summary(result: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for delivery in result.get("deliveries") or []:
        status = str(delivery.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "no subscribers"


def _session_mtime() -> float | None:
    try:
        return session_path().stat().st_mtime
    except OSError:
        return None


def _saved_mailbox() -> str | None:
    try:
        raw = read_private_json(session_path())
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("mailbox"), str):
        return raw["mailbox"]
    return os.environ.get("UTMAIL_MAILBOX", "").strip() or None


# ── Poller ───────────────────────────────────────────────────────────────


class InboxNotifier:
    """Poll the Inbox, publish new messages, and persist a restart-safe cursor."""

    def __init__(
        self,
        *,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        state_file: Path | None = None,
        session_loader: Callable[[], OwaSession] = load_or_refresh_session,
        reader_factory: Callable[[OwaSession], InboxReader] = lambda s: InboxReader(OwaClient(s)),
        publisher: Callable[..., dict[str, Any]] = exocortex.publish_external_notification,
        registrar: Callable[[], Any] = lambda: exocortex.register_external_notification_source(TOOL_NAME, SOURCE),
        clock: Callable[[], float] = time.time,
        session_mtime: Callable[[], float | None] = _session_mtime,
        log: Callable[[str], None] = lambda message: None,
    ):
        self.poll_seconds = max(MIN_POLL_SECONDS, int(poll_seconds))
        self.state_file = state_file or state_path()
        self.session_loader = session_loader
        self.reader_factory = reader_factory
        self.publisher = publisher
        self.registrar = registrar
        self.clock = clock
        self.session_mtime = session_mtime
        self.log = log
        self.state = load_state(self.state_file)
        self.error_delay = self.poll_seconds
        self.next_attempt_at = 0.0
        self.registered_at: float | None = None
        self.auth_streak = 0
        self.warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self.warned:
            self.warned.add(key)
            self.log(message)

    # Publishing ----------------------------------------------------------

    def _ensure_registered(self) -> None:
        now = self.clock()
        if self.registered_at is not None and now - self.registered_at < REGISTER_INTERVAL:
            return
        try:
            self.registrar()
        except Exception as exc:  # exocortexd may be restarting; retry next step
            self.log(f"Could not register notification source: {exc}")
            self.registered_at = None
            return
        self.registered_at = now

    def _publish(self, event: dict[str, Any]) -> bool:
        try:
            result = self.publisher(
                TOOL_NAME,
                SOURCE["id"],
                event["eventId"],
                event["text"],
                data=event["data"],
                occurred_at=event.get("occurredAt"),
            )
        except Exception as exc:
            self.log(f"Publish failed for {event['data'].get('kind')} event; will retry: {exc}")
            self.registered_at = None
            return False
        if not _publish_accepted(result):
            self.log(f"Publish had failed deliveries ({_delivery_summary(result)}); will retry")
            return False
        self.log(f"Published {event['data'].get('kind')} event: {_delivery_summary(result)}")
        return True

    # Polling -------------------------------------------------------------

    def _prime(self, reader: InboxReader, mailbox: str) -> dict[str, Any]:
        rows = reader.newest()
        moments = [m for row in rows if (m := _received_at_or_none(row.get("ReceivedDateTime")))]
        cursor = max(moments) if moments else datetime.fromtimestamp(self.clock(), timezone.utc)
        self.state.update(
            mailbox=mailbox,
            initialized=True,
            cursor=iso(cursor),
            catchUp=False,
            seen={str(row["Id"]): row.get("ReceivedDateTime") for row in rows if row.get("Id")},
            pending={},
        )
        self.log(f"Primed cursor at {iso(cursor)} from {len(rows)} existing Inbox messages; nothing published")
        return {"primed": len(rows), "new": 0, "published": 0, "pending": 0, "catchUp": False}

    def _prune(self) -> None:
        cursor = _received_at_or_none(self.state["cursor"])
        seen = self.state["seen"]
        if cursor is not None:
            horizon = cursor - SEEN_RETENTION
            seen = {
                key: value for key, value in seen.items()
                if (moment := _received_at_or_none(value)) is None or moment >= horizon
            }
        if len(seen) > MAX_SEEN:
            seen = dict(sorted(seen.items(), key=lambda item: str(item[1]))[-MAX_SEEN:])
        self.state["seen"] = seen
        pending = self.state["pending"]
        if len(pending) > MAX_PENDING:
            dropped = len(pending) - MAX_PENDING
            self.log(f"Dropping {dropped} oldest undeliverable events beyond the pending limit")
            self.state["pending"] = dict(list(pending.items())[-MAX_PENDING:])

    def poll_once(self) -> dict[str, Any]:
        session = self.session_loader()
        reader = self.reader_factory(session)
        mailbox = session.mailbox
        if not self.state.get("initialized") or self.state.get("mailbox") != mailbox:
            return self._prime(reader, mailbox)

        published = 0
        for message_id, event in list(self.state["pending"].items()):
            if self._publish(event):
                self.state["pending"].pop(message_id, None)
                self.state["seen"][message_id] = event["data"].get("receivedAt")
                published += 1

        cursor = _received_at_or_none(self.state["cursor"])
        if cursor is None:
            return self._prime(reader, mailbox)
        since = cursor if self.state.get("catchUp") else cursor - OVERLAP
        rows = reader.since(since)
        new = 0
        for row in rows:
            message_id = row.get("Id")
            received = _received_at_or_none(row.get("ReceivedDateTime"))
            if not message_id or received is None:
                continue
            message_id = str(message_id)
            if received > cursor:
                cursor = received
            if message_id in self.state["seen"] or message_id in self.state["pending"]:
                continue
            if row.get("IsDraft"):
                self.state["seen"][message_id] = row.get("ReceivedDateTime")
                continue
            new += 1
            headers, attachments = enrich(reader, row, self._warn_once)
            event = build_message_event(
                row,
                mailbox=mailbox,
                headers=headers,
                attachments=attachments,
                detected_at=datetime.fromtimestamp(self.clock(), timezone.utc),
            )
            if self._publish(event):
                self.state["seen"][message_id] = row.get("ReceivedDateTime")
                published += 1
            else:
                self.state["pending"][message_id] = event
        # A full batch may hide more mail; continue without overlap right away.
        self.state["catchUp"] = len(rows) >= BATCH_LIMIT
        self.state["cursor"] = iso(cursor)
        self._prune()
        return {
            "primed": 0,
            "new": new,
            "published": published,
            "pending": len(self.state["pending"]),
            "catchUp": self.state["catchUp"],
        }

    # Scheduling ----------------------------------------------------------

    def due(self) -> bool:
        if self.clock() >= self.next_attempt_at:
            return True
        auth = self.state.get("auth")
        # A fresh `utmail login` rewrites the session file: retry immediately.
        return bool(auth) and self.session_mtime() != auth.get("sessionMtime")

    def _enter_auth_episode(self, error: UtmailError) -> float:
        now = self.clock()
        auth = self.state.get("auth")
        if not auth:
            since = datetime.fromtimestamp(now, timezone.utc)
            event = build_auth_event(mailbox=self.state.get("mailbox") or _saved_mailbox(), error=error, since=since)
            auth = {"event": event, "published": False, "attempts": 0}
            self.state["auth"] = auth
            self.log(f"Authentication needed: {error.message}")
        auth["attempts"] = int(auth.get("attempts", 0)) + 1
        auth["sessionMtime"] = self.session_mtime()
        if not auth.get("published"):
            self._ensure_registered()
            auth["published"] = self._publish(auth["event"])
        delay = AUTH_RETRY_DELAYS[min(auth["attempts"] - 1, len(AUTH_RETRY_DELAYS) - 1)]
        # Keep retrying an unpublished auth event at the normal error cadence.
        return delay if auth["published"] else min(delay, self.error_delay)

    def step(self) -> float:
        """Run one poll attempt, persist state, and return the delay until the next."""
        self._ensure_registered()
        now = self.clock()
        self.state["lastPollAt"] = iso(datetime.fromtimestamp(now, timezone.utc))
        try:
            stats = self.poll_once()
        except AUTH_ERRORS as error:
            self.auth_streak += 1
            self.state["lastError"] = _one_line(error.message, 300)
            if self.auth_streak >= AUTH_CONFIRMATIONS or self.state.get("auth"):
                delay = self._enter_auth_episode(error)
            else:
                # One rejection can be transient; confirm before alerting.
                self.log(f"Authentication failed; confirming in {self.error_delay}s: {self.state['lastError']}")
                delay = self.error_delay
        except Exception as error:
            message = error.message if isinstance(error, UtmailError) else f"{type(error).__name__}: {error}"
            if isinstance(error, BrowserImportError):
                message = f"browser renewal failed: {message}"
            self.log(f"Poll failed; retrying in {self.error_delay}s: {_one_line(message, 300)}")
            self.state["lastError"] = _one_line(message, 300)
            delay = self.error_delay
            self.error_delay = min(self.error_delay * 2, MAX_ERROR_DELAY)
        else:
            self.auth_streak = 0
            if self.state.get("auth"):
                self.log("Authentication recovered; resuming new-mail notifications")
                self.state["auth"] = None
            self.state["lastSuccessAt"] = self.state["lastPollAt"]
            self.state["lastError"] = None
            self.error_delay = self.poll_seconds
            if stats["new"] or stats["published"] or stats["pending"]:
                self.log(
                    f"Poll: {stats['new']} new, {stats['published']} published, {stats['pending']} pending"
                )
            delay = 1.0 if stats["catchUp"] else float(self.poll_seconds)
        save_state(self.state_file, self.state)
        self.next_attempt_at = self.clock() + delay
        return delay


# ── Daemon process ───────────────────────────────────────────────────────


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def _lock_owner() -> int | None:
    """Return the PID holding the poller lock, or None when no poller runs."""
    path = lock_file()
    if not path.exists():
        return None
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _int_or_none(handle.read()) or -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return None
    return None


def run(poll_seconds: int) -> int:
    path = lock_file()
    ensure_private_directory(path.parent)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        _log(f"Another UTmail notify poller already runs (pid {handle.read().strip() or '?'}); exiting")
        handle.close()
        return 0
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()

    stopping = threading.Event()

    def shutdown(signum: int, _frame: Any) -> None:
        _log(f"Received {signal.Signals(signum).name}; stopping")
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    notifier = InboxNotifier(poll_seconds=poll_seconds, log=_log)
    _log(f"UTmail notify poller started (poll {notifier.poll_seconds}s, source {TOOL_NAME}/{SOURCE['id']})")
    try:
        while not stopping.is_set():
            if notifier.due():
                notifier.step()
            # Wake at least every poll interval so a fresh login is noticed.
            wait = min(notifier.poll_seconds, max(0.5, notifier.next_attempt_at - time.time()))
            stopping.wait(wait)
    finally:
        handle.seek(0)
        handle.truncate()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        _log("UTmail notify poller stopped")
    return 0


def configured_poll_seconds(value: int | None = None) -> int:
    if value is None:
        value = _int_or_none(os.environ.get("UTMAIL_NOTIFY_POLL_SECONDS")) or DEFAULT_POLL_SECONDS
    return max(MIN_POLL_SECONDS, value)


# ── CLI ──────────────────────────────────────────────────────────────────

USAGE = """\
usage: utmail notify <command> [args]

Publish one Exocortex external notification per newly arrived Inbox message
(source utmail/inbox). Exocortex owns routing and subscriber filtering.

commands:
  start                     Start the exocortexd-supervised poller
  stop                      Stop the poller
  status                    Show poller, cursor, auth, and subscription state
  subscribe CONV [--delivery wake|inbox]
                            Route events to an Exocortex conversation
  unsubscribe CONV          Remove that conversation's route
  list                      List subscriptions for the source
  preview [--limit N]       Print the latest Inbox events as JSON; never publishes
  run [--poll-seconds N]    Run the poller in the foreground (used by the daemon)
"""


def _cmd_start(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="utmail notify start").parse_args(argv)
    try:
        status = exocortex.manage_external_tool_daemon(TOOL_NAME, "start")
    except exocortex.ExocortexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if "not loaded" in str(exc):
            print(
                "  exocortexd supervises daemons only for tools under external-tools/.\n"
                "  Install UTmail there (see NOTIFY.md), or run `utmail notify run` in the foreground.",
                file=sys.stderr,
            )
        return 1
    print(status.get("message") or "Requested start.")
    return 0


def _cmd_stop(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="utmail notify stop").parse_args(argv)
    try:
        status = exocortex.manage_external_tool_daemon(TOOL_NAME, "stop")
        print(status.get("message") or "Requested stop.")
    except exocortex.ExocortexError as exc:
        print(f"Supervised daemon: {exc}")
    owner = _lock_owner()
    if owner and owner > 0:
        try:
            cmdline = Path(f"/proc/{owner}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            cmdline = ""
        if "notify" in cmdline and "run" in cmdline:
            os.kill(owner, signal.SIGTERM)
            print(f"Stopped foreground poller (pid {owner}).")
    return 0


def _cmd_status(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="utmail notify status").parse_args(argv)
    try:
        daemon = exocortex.manage_external_tool_daemon(TOOL_NAME, "status").get("message")
    except exocortex.ExocortexError as exc:
        daemon = f"unavailable ({exc})"
    owner = _lock_owner()
    state = load_state(state_path())
    auth = state.get("auth") or {}
    print(f"  Supervised daemon: {daemon}")
    print(f"  Poller process:    {f'running (pid {owner})' if owner else 'not running'}")
    print(f"  Source:            {TOOL_NAME}/{SOURCE['id']}")
    print(f"  Mailbox:           {state.get('mailbox') or '-'}")
    print(f"  Cursor:            {state.get('cursor') or '(not primed)'}")
    print(f"  Last poll:         {state.get('lastPollAt') or '-'}")
    print(f"  Last success:      {state.get('lastSuccessAt') or '-'}")
    print(f"  Last error:        {state.get('lastError') or '-'}")
    print(f"  Pending events:    {len(state.get('pending') or {})}")
    if auth:
        published = "published" if auth.get("published") else "not yet published"
        print(f"  Auth needed since: {auth.get('event', {}).get('data', {}).get('since')} ({published})")
    return 0


def _cmd_subscribe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="utmail notify subscribe")
    parser.add_argument("conv_id")
    parser.add_argument("--delivery", choices=("wake", "inbox"), default="wake")
    args = parser.parse_args(argv)
    subscription = exocortex.subscribe_external_notification(TOOL_NAME, SOURCE, args.conv_id, delivery=args.delivery)
    print(f"Subscribed. Subscription ID: {subscription.get('id', '?')} ({subscription.get('delivery', args.delivery)})")
    return 0


def _cmd_unsubscribe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="utmail notify unsubscribe")
    parser.add_argument("conv_id")
    args = parser.parse_args(argv)
    result = exocortex.unsubscribe_external_notification(TOOL_NAME, SOURCE["id"], args.conv_id)
    print(f"Unsubscribed {result.get('removed', 0)} route(s).")
    return 0


def _cmd_list(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="utmail notify list").parse_args(argv)
    subscriptions = exocortex.list_external_notification_subscriptions(tool_name=TOOL_NAME, source_id=SOURCE["id"])
    if not subscriptions:
        print("  No subscriptions.")
    for item in subscriptions:
        enabled = "enabled" if item.get("enabled", True) else "disabled"
        print(f"  {item.get('convId')}  {item.get('delivery')}  {enabled}  [{item.get('id')}]")
    return 0


def _cmd_preview(argv: list[str]) -> int:
    import json

    parser = argparse.ArgumentParser(
        prog="utmail notify preview",
        description="Build events for the latest Inbox messages exactly as the poller would; never publishes.",
    )
    parser.add_argument("--limit", "-n", type=int, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 20:
        print("Error: --limit must be between 1 and 20", file=sys.stderr)
        return 2
    try:
        session = load_or_refresh_session()
        reader = InboxReader(OwaClient(session))
        events = []
        for row in reader.newest(args.limit):
            headers, attachments = enrich(reader, row, lambda _key, message: _log(message))
            events.append(build_message_event(
                row,
                mailbox=session.mailbox,
                headers=headers,
                attachments=attachments,
                detected_at=datetime.now(timezone.utc),
            ))
    except UtmailError as error:
        print(f"utmail: {error.message}", file=sys.stderr)
        return error.exit_code
    print(json.dumps(events, indent=2, ensure_ascii=False))
    return 0


def _cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="utmail notify run")
    parser.add_argument("--poll-seconds", type=int)
    args = parser.parse_args(argv)
    return run(configured_poll_seconds(args.poll_seconds))


COMMANDS = {
    "start": _cmd_start,
    "stop": _cmd_stop,
    "status": _cmd_status,
    "subscribe": _cmd_subscribe,
    "unsubscribe": _cmd_unsubscribe,
    "list": _cmd_list,
    "preview": _cmd_preview,
    "run": _cmd_run,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        print(f"utmail notify: unknown command '{argv[0]}'\n", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    try:
        return command(argv[1:])
    except exocortex.ExocortexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
