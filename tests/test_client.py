from __future__ import annotations

import json
import unittest

import requests

from helpers import session
from utmail_tool.client import OwaClient
from utmail_tool.errors import NetworkError, SessionRejectedError


class FakeResponse:
    def __init__(self, status: int, payload, *, headers=None):
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {"Content-Type": "application/json"})
        self._content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def iter_content(self, chunk_size=65536):
        yield self._content

    def close(self):
        pass


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def test_get_is_bearer_authenticated_and_redirects_are_disabled(self):
        transport = FakeTransport([FakeResponse(200, {"value": []})])
        client = OwaClient(session(), transport=transport)
        client.get_json("/api/v2.0/me/messages")
        url, kwargs = transport.calls[0]
        self.assertEqual(url, "https://outlook.cloud.microsoft/api/v2.0/me/messages")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))
        self.assertFalse(kwargs["allow_redirects"])

    def test_outside_origin_and_pagination_are_rejected(self):
        client = OwaClient(session(), transport=FakeTransport([]))
        with self.assertRaises(NetworkError):
            client.validate_url("https://evil.example/api/v2.0/me/messages")
        transport = FakeTransport([FakeResponse(200, {"value": [], "@odata.nextLink": "https://evil.example/api/v2.0/x"})])
        with self.assertRaises(NetworkError):
            OwaClient(session(), transport=transport).collect("/api/v2.0/me/messages", limit=2)

    def test_rejected_session_has_actionable_error(self):
        transport = FakeTransport([FakeResponse(401, {})])
        with self.assertRaises(SessionRejectedError):
            OwaClient(session(), transport=transport).get_json("/api/v2.0/me")

    def test_bounded_retry(self):
        transport = FakeTransport([
            FakeResponse(503, {}, headers={"Retry-After": "100"}),
            FakeResponse(200, {"value": []}),
        ])
        delays = []
        result = OwaClient(session(), transport=transport, sleeper=delays.append).get_json("/api/v2.0/me/messages")
        self.assertEqual(result, {"value": []})
        self.assertEqual(delays, [5.0])
