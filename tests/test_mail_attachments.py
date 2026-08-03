from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from utmail_tool.attachments import Attachments
from utmail_tool.client import BinaryResponse
from utmail_tool.errors import UnsafeFileError
from utmail_tool.mail import Mailbox, normalize_message, parse_duration


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_json(self, url, *, params=None):
        self.calls.append(("json", url, params))
        return {"Id": "user", "DisplayName": "Example Student", "EmailAddress": "student@example.edu"}

    def collect(self, url, *, params=None, limit):
        self.calls.append(("collect", url, params, limit))
        return [{
            "Id": "message", "ConversationId": "thread", "Subject": "Subject",
            "From": {"EmailAddress": {"Name": "Registrar", "Address": "registrar@example.com"}},
            "ReceivedDateTime": "2026-08-01T12:00:00Z", "IsRead": False,
            "HasAttachments": True, "Body": {"ContentType": "Text", "Content": "Body"},
        }]

    def get_bytes(self, url, *, max_bytes):
        self.calls.append(("bytes", url, max_bytes))
        return BinaryResponse(b"pdf-data", "application/pdf", None)


class AttachmentMailbox:
    def attachment_rows(self, message_id, *, limit=100):
        return [{
            "@odata.type": "#Microsoft.OutlookServices.FileAttachment",
            "Id": "attachment", "Name": "../../receipt.pdf", "ContentType": "application/pdf",
            "Size": 8, "IsInline": False,
        }]


class MailAttachmentTests(unittest.TestCase):
    def test_mail_normalization_and_duration(self):
        row = normalize_message(FakeClient().collect("x", limit=1)[0], include_body=True)
        self.assertEqual(row["from"]["name"], "Registrar")
        self.assertEqual(row["body"], "Body")
        self.assertEqual(parse_duration("2d").days, 2)

    def test_inbox_uses_read_only_odata_collection(self):
        client = FakeClient()
        rows = Mailbox(client).inbox(limit=1, unread=True)
        self.assertEqual(rows[0]["id"], "message")
        call = client.calls[0]
        self.assertEqual(call[1], "/api/v2.0/me/mailfolders/inbox/messages")
        self.assertIn("IsRead eq false", call[2]["$filter"])

    def test_private_download_sanitizes_and_refuses_overwrite(self):
        client = FakeClient()
        attachments = Attachments(client, AttachmentMailbox())
        with tempfile.TemporaryDirectory() as directory:
            result = attachments.download("message", "attachment", output_directory=directory)
            path = Path(result["path"])
            self.assertEqual(path.name, "receipt.pdf")
            self.assertEqual(path.read_bytes(), b"pdf-data")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(UnsafeFileError):
                attachments.download("message", "attachment", output_directory=directory)
