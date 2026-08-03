from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .client import OwaClient
from .errors import UsageError


SUMMARY_FIELDS = (
    "Id,ConversationId,Subject,From,Sender,ReceivedDateTime,SentDateTime,"
    "IsRead,HasAttachments,Importance,BodyPreview,InternetMessageId"
)
DETAIL_FIELDS = SUMMARY_FIELDS + ",ToRecipients,CcRecipients,BccRecipients,ReplyTo,Body"


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", value, re.IGNORECASE)
    if not match:
        raise UsageError("duration must look like 30m, 12h, 2d, or 3w")
    amount = int(match.group(1))
    if amount <= 0:
        raise UsageError("duration must be positive")
    unit = match.group(2).lower()
    return {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }[unit]


def _address(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    email = value.get("EmailAddress", value)
    if not isinstance(email, dict):
        return None
    address = email.get("Address") or email.get("address")
    name = email.get("Name") or email.get("name")
    if not address and not name:
        return None
    return {"name": str(name) if name else None, "address": str(address) if address else None}


def _addresses(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    return [parsed for item in value if (parsed := _address(item)) is not None]


def normalize_message(value: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    body = value.get("Body") if isinstance(value.get("Body"), dict) else {}
    result = {
        "id": value.get("Id"),
        "conversationId": value.get("ConversationId"),
        "internetMessageId": value.get("InternetMessageId"),
        "subject": value.get("Subject") or "",
        "from": _address(value.get("From")),
        "sender": _address(value.get("Sender")),
        "receivedAt": value.get("ReceivedDateTime"),
        "sentAt": value.get("SentDateTime"),
        "isRead": value.get("IsRead"),
        "hasAttachments": value.get("HasAttachments"),
        "importance": value.get("Importance"),
        "bodyPreview": value.get("BodyPreview") or "",
    }
    if include_body:
        result.update(
            {
                "to": _addresses(value.get("ToRecipients")),
                "cc": _addresses(value.get("CcRecipients")),
                "bcc": _addresses(value.get("BccRecipients")),
                "replyTo": _addresses(value.get("ReplyTo")),
                "body": body.get("Content") or "",
                "bodyType": body.get("ContentType") or "Text",
            }
        )
    return result


def normalize_account(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("Id"),
        "displayName": value.get("DisplayName"),
        "emailAddress": value.get("EmailAddress"),
    }


class Mailbox:
    def __init__(self, client: OwaClient):
        self.client = client

    def whoami(self) -> dict[str, Any]:
        payload = self.client.get_json(
            "/api/v2.0/me",
            params={"$select": "Id,DisplayName,EmailAddress"},
        )
        return normalize_account(payload)

    def inbox(
        self,
        *,
        limit: int = 20,
        since: timedelta | None = None,
        unread: bool = False,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        if unread:
            filters.append("IsRead eq false")
        if since is not None:
            moment = datetime.now(timezone.utc) - since
            filters.append("ReceivedDateTime ge " + moment.isoformat().replace("+00:00", "Z"))
        params: dict[str, Any] = {
            "$top": str(min(limit, 100)),
            "$select": SUMMARY_FIELDS,
            "$orderby": "ReceivedDateTime desc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)
        values = self.client.collect(
            "/api/v2.0/me/mailfolders/inbox/messages",
            params=params,
            limit=limit,
        )
        return [normalize_message(value) for value in values]

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise UsageError("search query cannot be empty")
        if len(query) > 500:
            raise UsageError("search query is too long")
        escaped = query.replace('"', '\\"')
        values = self.client.collect(
            "/api/v2.0/me/messages",
            params={
                "$top": str(min(limit, 100)),
                "$select": SUMMARY_FIELDS,
                "$search": f'"{escaped}"',
            },
            limit=limit,
        )
        return [normalize_message(value) for value in values]

    def show(self, message_id: str) -> dict[str, Any]:
        encoded = quote(message_id, safe="")
        payload = self.client.get_json(
            f"/api/v2.0/me/messages/{encoded}",
            params={"$select": DETAIL_FIELDS},
        )
        return normalize_message(payload, include_body=True)

    def thread(self, conversation_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        escaped = conversation_id.replace("'", "''")
        values = self.client.collect(
            "/api/v2.0/me/messages",
            params={
                "$top": str(min(limit, 100)),
                "$select": DETAIL_FIELDS,
                "$filter": f"ConversationId eq '{escaped}'",
            },
            limit=limit,
        )
        rows = [normalize_message(value, include_body=True) for value in values]
        # Outlook rejects the ConversationId restriction combined with an
        # order-by clause as InefficientFilter. Sort the bounded result locally.
        rows.sort(key=lambda row: str(row.get("receivedAt") or row.get("sentAt") or ""))
        return rows

    def attachment_rows(self, message_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        encoded = quote(message_id, safe="")
        return self.client.collect(
            f"/api/v2.0/me/messages/{encoded}/attachments",
            params={"$top": str(min(limit, 100))},
            limit=limit,
        )

    @staticmethod
    def attachment_value_url(message_id: str, attachment_id: str) -> str:
        return (
            f"/api/v2.0/me/messages/{quote(message_id, safe='')}/attachments/"
            f"{quote(attachment_id, safe='')}/$value"
        )
