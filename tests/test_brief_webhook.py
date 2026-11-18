"""Exercise the signed webhook over loopback without exposing the gateway."""

import asyncio
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from test_plugin import plugin


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_event_wakes_one_queued_bot_once_after_restart(self):
        try:
            from aiohttp.test_utils import TestClient, TestServer
        except ImportError:
            self.skipTest("Hermes messaging dependency aiohttp is unavailable")
        from importlib import import_module

        webhook = import_module(f"{plugin.__name__}.brief_webhook")
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ledger = webhook.Briefs(home)
        ledger.prepare()
        ledger.queue("queued", "session", "In meeting", start_check=4_000_000_000)
        ledger.queue("other", "session", "Scheduled", start_check=4_000_000_000)
        wake = asyncio.Event()
        secret = "test-secret"

        async def post(client, data, sign=True):
            signature = "sha256=" + hmac.digest(secret.encode(), data, "sha256").hex() if sign else ""
            response = await client.post("/meetstream/briefs", data=data, headers={"X-MeetStream-Signature": signature})
            return response.status

        body = json.dumps({"bot_id": "queued", "bot_event": "transcription.processed"}).encode()
        async with TestClient(TestServer(webhook.create_app(home, wake, secret))) as client:
            self.assertEqual(await post(client, body, sign=False), 401)
            self.assertEqual(await post(client, b"{}"), 204)
            self.assertEqual(await post(client, b"x" * 65537), 413)
            self.assertFalse(wake.is_set())
            self.assertEqual(ledger.due(), [])
            self.assertEqual(await post(client, body), 204)
            self.assertTrue(wake.is_set())
            self.assertEqual([row["bot_id"] for row in ledger.due()], ["queued"])

        ledger.defer("queued", "Processing transcript")
        wake.clear()
        async with TestClient(TestServer(webhook.create_app(home, wake, secret))) as client:
            self.assertEqual(await post(client, body), 204)
            self.assertFalse(wake.is_set())
            self.assertEqual(ledger.due(), [])
