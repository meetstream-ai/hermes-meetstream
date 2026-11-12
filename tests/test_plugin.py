"""Behaviour tests against a fake MeetStream API (httpx.MockTransport). No network, no real key."""

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
PKG = "meetstream_plugin_under_test"


def _load_plugin():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
api = sys.modules[f"{PKG}.meetstream_client"]
adapter_mod = sys.modules[f"{PKG}.adapter"]
tools = sys.modules[f"{PKG}.tools"]

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

if not platform_registry.is_registered("meetstream"):
    platform_registry.register(PlatformEntry(name="meetstream", label="MeetStream", adapter_factory=lambda c: None,
                                             check_fn=lambda: True))

KEY = "ms_test_key"
SETTINGS = {"bot_name": "Hermes", "transcription": "meetstream", "join_message": "Hi, I'm {name}."}


class FakeMeetStream:
    """In-memory MeetStream: bots, their status and chat."""

    def __init__(self):
        self.bots: dict[str, dict] = {}
        self.sent: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.reject_key = False

    def add_bot(self, bot_id, status="InMeeting", hermes=True, chats=(), name="Hermes", attrs=None):
        if attrs is None:
            attrs = {"hermes": True} if hermes else {}
        self.bots[bot_id] = {"status": status, "attrs": attrs, "chats": list(chats),
                             "name": name if hermes or attrs else "MeetStream Bot"}

    def say(self, bot_id, who, text):
        chats = self.bots[bot_id]["chats"]
        chats.append({"messageId": f"m{len(chats)}", "speakerDisplayName": who, "text": text,
                      "timestamp": "2026-11-12T10:00:00Z"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.reject_key or request.headers.get("Authorization") != f"Token {KEY}":
            return httpx.Response(401, json={"detail": "Invalid token."})
        path = request.url.path.removeprefix("/api/v1")
        parts = path.strip("/").split("/")
        if path == "/bots/create_bot":
            body = json.loads(request.content)
            self.created.append(body)
            self.add_bot("bot-new", status="Joining")
            return httpx.Response(201, json={"bot_id": "bot-new", "status": "Active",
                                             "meeting_url": body["meeting_link"], "transcript_id": None})
        if path == "/bots":
            rows = [{"BotID": b, "Status": v["status"], "BotUsername": v["name"], "CustomAttributes": v["attrs"]}
                    for b, v in self.bots.items()]
            return httpx.Response(200, json={"bots": rows, "hasNextPage": False})
        bot = self.bots.get(parts[1]) if len(parts) > 2 else None
        if bot is None:
            return httpx.Response(404, json={"detail": "Bot not found."})
        action = parts[2]
        if action == "status":
            return httpx.Response(200, json={"bot_id": parts[1], "status": bot["status"]})
        if action == "get_chats":
            return httpx.Response(200, json={"chatMessages": bot["chats"]})
        if action == "send_message":
            text = json.loads(request.content)["message"]
            self.sent.append((parts[1], text))
            bot["chats"].append({"messageId": f"m{len(bot['chats'])}", "speakerDisplayName": "Hermes", "text": text})
            return httpx.Response(200, json={"status": "sent", "bot_id": parts[1]})
        if action == "remove_bot":
            bot["status"] = "Stopped"
            return httpx.Response(200, json={"bot_id": parts[1], "status": "stop_requested"})
        return httpx.Response(404, json={"detail": "unknown"})


class PluginTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMeetStream()
        transport = httpx.MockTransport(self.fake.handler)
        patches = [
            mock.patch.object(api, "new_client",
                              lambda: httpx.AsyncClient(base_url=api.BASE_URL, transport=transport)),
            mock.patch.object(tools, "api_key", lambda: KEY),
            mock.patch.object(tools, "settings", lambda: dict(SETTINGS)),
            mock.patch.object(adapter_mod, "_key", lambda: KEY),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        adapter_mod._PENDING.clear()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_adapter(self, **extra):
        a = adapter_mod.MeetStreamAdapter(PlatformConfig(enabled=True, extra=extra))
        a._client = api.new_client()
        a._running = True
        a.handle_message = mock.AsyncMock()
        return a


class TestAddressing(unittest.TestCase):
    def test_messages_that_address_the_bot(self):
        names = ["Hermes"]
        self.assertEqual(adapter_mod.addressed("Hermes, what did we decide?", names), "what did we decide?")
        self.assertEqual(adapter_mod.addressed("@hermes summarize so far", names), "summarize so far")
        self.assertEqual(adapter_mod.addressed("hey Hermes: next steps?", names), "next steps?")
        self.assertEqual(adapter_mod.addressed("can you check this @Hermes", names), "can you check this")

    def test_messages_that_do_not(self):
        names = ["Hermes"]
        self.assertIsNone(adapter_mod.addressed("let's ask Hermes later", names))
        self.assertIsNone(adapter_mod.addressed("Hermesville is a town", names))
        self.assertIsNone(adapter_mod.addressed("Hermes", names))


class TestPayload(unittest.TestCase):
    def test_join_payload_tags_bot_and_sets_safe_leave_timeouts(self):
        p = api.build_join_payload("https://meet.google.com/abc-defg-hij", bot_name="Hermes", message="hi")
        self.assertEqual(p["custom_attributes"], {"hermes": True})
        self.assertEqual(p["automatic_leave"]["everyone_left_timeout"], 60)
        self.assertEqual(p["recording_config"]["transcript"]["provider"], {"meetstream": {"language": "auto", "translate": False}})
        self.assertFalse(p["video_required"])

    def test_rejects_non_meeting_links(self):
        for url in ("http://meet.google.com/x", "https://evil.example/zoom.us", "https://zoom.us.evil.com/j/1", "file:///etc"):
            with self.assertRaises(ValueError, msg=url):
                api.build_join_payload(url, bot_name="Hermes")
        api.build_join_payload("https://us02web.zoom.us/j/123", bot_name="Hermes")
        api.build_join_payload("https://teams.microsoft.com/l/meetup-join/x", bot_name="Hermes")

    def test_chunks_fit_meeting_chat(self):
        parts = api.chunks(("word " * 1000).strip())
        self.assertTrue(all(len(p) <= api.MAX_CHAT for p in parts))
        self.assertEqual(" ".join(parts), ("word " * 1000).strip())


class TestTools(PluginTestCase):
    def test_join_creates_tagged_bot_and_hands_it_to_the_adapter(self):
        out = json.loads(self.run_async(tools.handle_join({"meeting_url": "https://meet.google.com/abc-defg-hij"})))
        self.assertEqual(out["bot_id"], "bot-new")
        body = self.fake.created[0]
        self.assertEqual(body["bot_name"], "Hermes")
        self.assertEqual(body["bot_message"], "Hi, I'm Hermes.")
        self.assertEqual(body["custom_attributes"], {"hermes": True})
        self.assertIn("bot-new", adapter_mod._PENDING)

    def test_join_refuses_bad_link_without_calling_meetstream(self):
        out = json.loads(self.run_async(tools.handle_join({"meeting_url": "https://example.com/meeting"})))
        self.assertIn("error", out)
        self.assertEqual(self.fake.requests, [])

    def test_rejected_key_gives_actionable_error(self):
        self.fake.reject_key = True
        out = json.loads(self.run_async(tools.handle_leave({"bot_id": "x"})))
        self.assertEqual(out["status_code"], 401)
        self.assertIn("MEETSTREAM_API_KEY", out["hint"])

    def test_status_lists_only_hermes_bots(self):
        self.fake.add_bot("b1", hermes=True)
        self.fake.add_bot("b2", hermes=False)
        out = json.loads(self.run_async(tools.handle_status({})))
        self.assertEqual([b["bot_id"] for b in out["bots"]], ["b1"])

    def test_chat_and_leave(self):
        self.fake.add_bot("b1")
        out = json.loads(self.run_async(tools.handle_chat({"bot_id": "b1", "message": "Agenda: pricing"})))
        self.assertEqual(out["sent"], 1)
        self.assertEqual(self.fake.sent, [("b1", "Agenda: pricing")])
        self.run_async(tools.handle_leave({"bot_id": "b1"}))
        self.assertEqual(self.fake.bots["b1"]["status"], "Stopped")


class TestAdapter(PluginTestCase):
    def test_bot_started_by_hermes_answers_from_the_first_question(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("bot-new")
            self.fake.add_bot("bot-new", status="InMeeting")
            self.fake.say("bot-new", "Sid", "Hermes, what is on the agenda?")
            self.fake.say("bot-new", "Nav", "unrelated chatter")
            await a._tick()
            await a._tick()  # nothing new: no second dispatch
            await a._client.aclose()
            return a
        a = self.run_async(go())
        a.handle_message.assert_awaited_once()
        event = a.handle_message.await_args.args[0]
        self.assertEqual(event.text, "what is on the agenda?")
        self.assertEqual(event.source.chat_id, "bot-new")
        self.assertEqual(event.source.user_id, "sid")

    def test_discovered_mid_call_does_not_answer_old_chat(self):
        async def go():
            a = self.make_adapter()
            self.fake.add_bot("cal-1", status="InMeeting")
            self.fake.add_bot("other", status="InMeeting", hermes=False)
            self.fake.say("cal-1", "Sid", "Hermes, old question")
            await a._tick()
            self.assertNotIn("other", a._bots)
            self.fake.say("cal-1", "Sid", "Hermes, new question")
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        a.handle_message.assert_awaited_once()
        self.assertEqual(a.handle_message.await_args.args[0].text, "new question")

    def test_calendar_auto_join_bot_is_found_by_name(self):
        # MeetStream auto-schedule replaces custom_attributes, so the name is the only marker left.
        async def go():
            a = self.make_adapter()
            self.fake.add_bot("cal-2", name="Hermes", attrs={"source": "auto_schedule_cron"})
            self.fake.add_bot("plain", name="Notetaker", attrs={"source": "auto_schedule_cron"})
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertIn("cal-2", a._bots)
        self.assertNotIn("plain", a._bots)

    def test_own_replies_are_not_read_back_as_questions(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("b1")
            self.fake.add_bot("b1")
            result = await a.send("b1", "Hermes, here is the answer")
            await a._tick()
            await a._client.aclose()
            return a, result
        a, result = self.run_async(go())
        self.assertTrue(result.success)
        a.handle_message.assert_not_awaited()

    def test_long_reply_is_split_into_chat_sized_messages(self):
        async def go():
            a = self.make_adapter()
            await a.send("b1", "x " * 1500)
            await a._client.aclose()
        self.fake.add_bot("b1")
        self.run_async(go())
        self.assertEqual(len(self.fake.sent), 2)
        self.assertTrue(all(len(t) <= api.MAX_CHAT for _, t in self.fake.sent))

    def test_ended_bot_is_dropped(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("b1")
            self.fake.add_bot("b1", status="Done")
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertNotIn("b1", a._bots)

    def test_rejected_key_stops_the_loop_with_a_fatal_error(self):
        async def go():
            a = self.make_adapter()
            self.fake.reject_key = True
            await a._run()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertFalse(a._running)
        self.assertTrue(a.has_fatal_error)

    def test_poll_interval_has_a_floor(self):
        a = adapter_mod.MeetStreamAdapter(PlatformConfig(enabled=True, extra={"chat_poll_seconds": 0.1}))
        self.assertGreaterEqual(a._chat_poll, adapter_mod.MIN_POLL_S)


class TestRegistration(unittest.TestCase):
    def test_registers_declared_tools_platform_and_skill(self):
        ctx = mock.MagicMock()
        plugin.register(ctx)
        names = [c.kwargs["name"] for c in ctx.register_tool.call_args_list]
        manifest = (ROOT / "plugin.yaml").read_text().split("provides_tools:\n", 1)[1]
        declared = []
        for line in manifest.splitlines():
            if not line.startswith("  - "):
                break
            declared.append(line[4:].strip())
        self.assertEqual(sorted(names), sorted(declared))
        self.assertEqual(ctx.register_platform.call_args.kwargs["name"], "meetstream")
        self.assertEqual(ctx.register_skill.call_args.args[0], "hermes-in-meetings")


if __name__ == "__main__":
    unittest.main()
