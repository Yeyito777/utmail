from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from utmail_tool.errors import UsageError
from utmail_tool.mail import Mailbox, compact_body, extract_links, parse_duration, prepare_message


class CollectionClient:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def collect(self, url, *, params=None, limit):
        self.calls.append((url, params, limit))
        return self.values[:limit]


class DetailClient(CollectionClient):
    def get_json(self, url, *, params=None):
        self.calls.append((url, params))
        return self.values[0]


def raw_message(identifier, received, *, unread, body="Body", conversation="thread"):
    return {
        "Id": identifier,
        "ConversationId": conversation,
        "Subject": f"Subject {identifier}",
        "ReceivedDateTime": received,
        "IsRead": not unread,
        "Body": {"ContentType": "Text", "Content": body},
    }


class SearchFilterTests(unittest.TestCase):
    def test_since_and_unread_are_composable_over_a_bounded_candidate_window(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        values = [
            raw_message("recent-unread", recent.isoformat(), unread=True),
            raw_message("recent-read", recent.isoformat(), unread=False),
            raw_message("old-unread", "2000-01-01T00:00:00Z", unread=True),
            raw_message("missing-date", None, unread=True),
        ]
        client = CollectionClient(values)
        rows = Mailbox(client).search("registrar", limit=2, since=timedelta(hours=1), unread=True)

        self.assertEqual([row["id"] for row in rows], ["recent-unread"])
        _, params, request_limit = client.calls[0]
        self.assertEqual(request_limit, 100)
        self.assertEqual(params["$top"], "100")
        self.assertNotIn("$filter", params)

    def test_unfiltered_search_preserves_existing_limit_and_search_escaping(self):
        client = CollectionClient([raw_message("one", "2026-08-01T00:00:00Z", unread=True)])
        Mailbox(client).search('say "hello"', limit=7)
        _, params, request_limit = client.calls[0]
        self.assertEqual(request_limit, 7)
        self.assertEqual(params["$top"], "7")
        self.assertEqual(params["$search"], '"say \\"hello\\""')

    def test_duration_rejects_invalid_and_overflowing_values(self):
        for value in ("", "today", "0d", "999999999999999999999999w"):
            with self.subTest(value=value), self.assertRaises(UsageError):
                parse_duration(value)


class LinkExtractionTests(unittest.TestCase):
    def test_safelinks_are_decoded_locally_and_destinations_are_deduplicated_in_order(self):
        destination = "https://example.edu/course?a=one+two&b=3"
        safe = (
            "https://nam10.safelinks.protection.outlook.com/?url="
            + quote(destination, safe="")
            + "&data=opaque"
        )
        body = (
            f'Before <a href="{safe.replace("&", "&amp;")}">Course page</a>. '
            "Then https://second.example/path). "
            f"Repeated {destination} after."
        )

        links = extract_links(body)

        self.assertEqual([link["url"] for link in links], [destination, "https://second.example/path"])
        self.assertTrue(links[0]["decodedSafeLink"])
        self.assertEqual(links[0]["text"], "Course page")
        self.assertIn("Before", links[0]["context"])
        self.assertFalse(links[1]["decodedSafeLink"])

    def test_only_exact_https_outlook_safelink_hosts_and_safe_destinations_are_decoded(self):
        lookalike = "https://safelinks.protection.outlook.com.evil.example/?url=https%3A%2F%2Fgood.example"
        unsafe_destination = (
            "https://nam01.safelinks.protection.outlook.com/?url=javascript%3Aalert%281%29&data=x"
        )
        links = extract_links(f"{lookalike}\n{unsafe_destination}")
        self.assertEqual([link["url"] for link in links], [lookalike, unsafe_destination])
        self.assertEqual([link["decodedSafeLink"] for link in links], [False, False])

    def test_link_projection_omits_body_without_changing_default_projection(self):
        message = {"id": "m", "body": "See https://example.edu", "bodyType": "Text"}
        self.assertEqual(prepare_message(message), message)
        view = prepare_message(message, links=True)
        self.assertNotIn("body", view)
        self.assertEqual(view["links"][0]["url"], "https://example.edu")
        self.assertEqual(message["body"], "See https://example.edu")


class CompactBodyTests(unittest.TestCase):
    def test_large_recognizable_disclaimer_is_removed_and_reported(self):
        ordinary = "Please review the attached agenda before Tuesday."
        disclaimer = (
            "CONFIDENTIALITY NOTICE\n"
            "This message is intended only for the named recipient.\n"
            + "Confidential institutional policy text.\n" * 20
        )
        compacted, details = compact_body(ordinary + "\n\n" + disclaimer)
        self.assertEqual(compacted, ordinary)
        self.assertTrue(details["truncated"])
        self.assertEqual(details["reason"], "institutional-disclaimer")
        self.assertGreater(details["removedCharacters"], 400)

    def test_large_institutional_signature_is_removed_but_ordinary_content_is_not(self):
        ordinary = "A short note.\nThis email is useful ordinary body text."
        unchanged, details = compact_body(ordinary)
        self.assertEqual(unchanged, ordinary)
        self.assertFalse(details["truncated"])

        signature = (
            "--\nExample Person\nDepartment of Examples\nUniversity of Toronto\n"
            "Email: person@utoronto.ca\nTel: 416-555-0100\n"
            "Office: 123 Example Street\nhttps://www.utoronto.ca\n"
            + "Institutional program and accessibility information.\n" * 12
        )
        compacted, details = compact_body("Important ordinary content.\n" + signature)
        self.assertEqual(compacted, "Important ordinary content.")
        self.assertEqual(details["reason"], "institutional-signature")

    def test_compact_link_view_excludes_footer_links_and_reports_truncation(self):
        disclaimer = (
            "CONFIDENTIALITY DISCLAIMER\n"
            + "Institutional disclaimer details.\n" * 15
            + "Policy: https://institution.example/policy\n"
        )
        message = {
            "id": "m",
            "body": "Use https://useful.example now.\n\n" + disclaimer,
            "bodyType": "Text",
        }
        view = prepare_message(message, compact=True, links=True)
        self.assertEqual([link["url"] for link in view["links"]], ["https://useful.example"])
        self.assertTrue(view["bodyCompaction"]["truncated"])
        self.assertNotIn("body", view)


class MessageViewIntegrationTests(unittest.TestCase):
    def test_show_and_thread_apply_views_after_one_read_only_bounded_fetch(self):
        raw = raw_message(
            "m/1",
            "2026-08-01T00:00:00Z",
            unread=True,
            body="Visit https://example.edu",
        )
        show_client = DetailClient([raw])
        shown = Mailbox(show_client).show("m/1", links=True)
        self.assertEqual(shown["links"][0]["url"], "https://example.edu")
        self.assertEqual(show_client.calls[0][0], "/api/v2.0/me/messages/m%2F1")

        thread_client = CollectionClient([raw])
        rows = Mailbox(thread_client).thread("thread", limit=3, links=True, compact=True)
        self.assertEqual(rows[0]["links"][0]["url"], "https://example.edu")
        _, params, request_limit = thread_client.calls[0]
        self.assertEqual(request_limit, 3)
        self.assertEqual(params["$top"], "3")
        self.assertEqual(params["$filter"], "ConversationId eq 'thread'")


if __name__ == "__main__":
    unittest.main()
