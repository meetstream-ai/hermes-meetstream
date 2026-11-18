"""Behaviour tests against a fake MeetStream API (httpx.MockTransport). No network, no real key."""

import asyncio
import importlib.util
import json
import sys
import time
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
tools = sys.modules[f"{PKG}.meetstream_tools"]
guard = sys.modules[f"{PKG}.guard"]

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
        self.page_size = 50
        self.broken: dict[str, int] = {}  # bot_id -> HTTP status its per-bot endpoints return
        self.bot_display = "Hermes"  # how the meeting shows the bot's own chat messages
        self.create_error: httpx.Response | None = None  # answer create_bot with this instead

    def add_bot(self, bot_id, status="InMeeting", hermes=True, chats=(), name="Hermes", attrs=None, age_s=3600):
        if attrs is None:
            attrs = {"hermes": True} if hermes else {}
        self.bots[bot_id] = {"status": status, "attrs": attrs, "chats": list(chats),
                             "name": name if hermes or attrs else "MeetStream Bot",
                             "created_at": time.time() - age_s}

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
        if path == "/bots/create_bot" and self.create_error is not None:
            return self.create_error
        if path == "/bots/create_bot":
            body = json.loads(request.content)
            self.created.append(body)
            self.add_bot("bot-new", status="Joining")
            return httpx.Response(201, json={"bot_id": "bot-new", "status": "Active",
                                             "meeting_url": body["meeting_link"], "transcript_id": None})
        if path == "/bots":
            rows = [{"bot_id": b, "status": v["status"], "bot_username": v["name"], "custom_attributes": v["attrs"],
                     "created_at": v["created_at"]} for b, v in self.bots.items()]
            start = int(request.url.params.get("cursor") or 0)
            page = rows[start:start + self.page_size]
            more = start + self.page_size < len(rows)
            return httpx.Response(200, json={"bots": page, "hasNextPage": more,
                                             "nextCursor": str(start + self.page_size) if more else None})
        bot = self.bots.get(parts[1]) if len(parts) > 2 else None
        if parts[1:2] and parts[1] in self.broken:
            return httpx.Response(self.broken[parts[1]], json={"detail": "broken"})
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
            bot["chats"].append({"messageId": f"m{len(bot['chats'])}", "speakerDisplayName": self.bot_display,
                                 "text": text})
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
        a = adapter_mod.addressed
        self.assertEqual(a("Hermes, what did we decide?", "Hermes"), "what did we decide?")
        self.assertEqual(a("@hermes summarize so far", "Hermes"), "summarize so far")
        self.assertEqual(a("hey Hermes: next steps?", "Hermes"), "next steps?")
        self.assertEqual(a("can you check this @Hermes", "Hermes"), "can you check this")
        self.assertEqual(a("Acme  AI, hello there", "acme ai"), "hello there")
        self.assertEqual(a("Notes Bot! what is next", "Notes Bot"), "what is next")

    def test_messages_that_do_not(self):
        a = adapter_mod.addressed
        for text in ("let's ask Hermes later", "Hermesville is a town", "Hermes", "Hermes-ville is a town",
                     "Hermes's notes are good", "mail me at me@hermes.com", "write to @hermes.com today"):
            self.assertIsNone(a(text, "Hermes"), text)
        self.assertIsNone(a("Hermes, hi", ""))


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

    def test_link_that_parsers_read_differently_is_refused(self):
        for url in ("https://evil.example\\@meet.google.com/x", "https://user@meet.google.com/x",
                    "https://meet.google.com/x y", "https://meet.google.com\t.evil.example/x"):
            with self.assertRaises(ValueError, msg=url):
                api.validate_meeting_url(url)
        self.assertEqual(api.validate_meeting_url(" https://MEET.google.com/abc-defg-hij?authuser=1 "),
                         "https://meet.google.com/abc-defg-hij?authuser=1")

    def test_bot_id_cannot_change_the_api_path(self):
        for bad in ("../users", "a/b", "x?y=1", "", "a" * 200):
            with self.assertRaises(ValueError, msg=bad):
                api.bot_path(bad, "status")
        self.assertEqual(api.bot_path("604c0a0b-973d-4eb1-a5bf-2c7513f6bbe0", "status"),
                         "/bots/604c0a0b-973d-4eb1-a5bf-2c7513f6bbe0/status")

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

    def test_low_credit_gives_top_up_link_not_key_hint(self):
        self.fake.create_error = httpx.Response(403, json={"message": "Low credits please recharge"})
        out = json.loads(self.run_async(tools.handle_join({"meeting_url": "https://meet.google.com/abc-defg-hij"})))
        self.assertEqual(out["status_code"], 403)
        self.assertIn("app.meetstream.ai/billing", out["hint"])
        self.assertNotIn("MEETSTREAM_API_KEY", out["hint"])

    def test_error_reads_message_first_and_keeps_retry_after(self):
        self.fake.create_error = httpx.Response(
            429, headers={"Retry-After": "37"},
            json={"error": "rate_limit_exceeded", "message": "Too many bots created in the last minute"})
        out = json.loads(self.run_async(tools.handle_join({"meeting_url": "https://meet.google.com/abc-defg-hij"})))
        self.assertEqual(out["status_code"], 429)
        self.assertEqual(out["error"], "Too many bots created in the last minute (retry after 37 s)")

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

    def test_one_broken_bot_does_not_starve_the_others(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            for b in ("gone", "ok"):
                adapter_mod.watch(b)
                self.fake.add_bot(b)
            self.fake.broken["gone"] = 404
            self.fake.say("ok", "Sid", "Hermes, are you there?")
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertNotIn("gone", a._bots)
        a.handle_message.assert_awaited_once()
        self.assertFalse(a.has_fatal_error)

    def test_forbidden_bot_is_dropped_without_stopping_the_platform(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("other-account")
            self.fake.add_bot("other-account")
            self.fake.broken["other-account"] = 403
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertNotIn("other-account", a._bots)
        self.assertFalse(a.has_fatal_error)

    def test_discovery_reads_every_page(self):
        async def go():
            a = self.make_adapter()
            self.fake.page_size = 2
            for i in range(5):
                self.fake.add_bot(f"plain-{i}", hermes=False)
            self.fake.add_bot("late-hermes")
            await a._discover()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        self.assertIn("late-hermes", a._bots)

    def test_fresh_bot_found_by_discovery_answers_early_questions(self):
        async def go():
            a = self.make_adapter()
            self.fake.add_bot("cli-bot", age_s=30)  # started from a CLI session a moment ago
            self.fake.say("cli-bot", "Sid", "Hermes, first question")
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        a.handle_message.assert_awaited_once()

    def test_reply_is_not_read_back_when_meeting_renames_the_bot(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("b1")
            self.fake.add_bot("b1")
            self.fake.bot_display = "Meeting Notetaker"
            await a._tick()
            await a.send("b1", "Hermes, here is the answer")
            await a._tick()
            await a._client.aclose()
            return a
        a = self.run_async(go())
        a.handle_message.assert_not_awaited()

    def test_send_refuses_a_bot_hermes_is_not_in(self):
        async def go():
            a = self.make_adapter()
            result = await a.send("someone-elses-bot", "hello")
            await a._client.aclose()
            return result
        self.fake.add_bot("someone-elses-bot")
        result = self.run_async(go())
        self.assertFalse(result.success)
        self.assertEqual(self.fake.sent, [])

    def test_first_tick_discovers_even_right_after_boot(self):
        async def go():
            a = self.make_adapter()
            self.fake.add_bot("cal-3")
            with mock.patch.object(adapter_mod.time, "monotonic", return_value=5.0):
                await a._tick()
            await a._client.aclose()
            return a
        self.assertIn("cal-3", self.run_async(go())._bots)

    def test_bad_poll_setting_falls_back_to_default(self):
        a = adapter_mod.MeetStreamAdapter(PlatformConfig(enabled=True, extra={"chat_poll_seconds": "fast"}))
        self.assertEqual(a._chat_poll, adapter_mod.CHAT_POLL_S)

    def test_own_replies_are_not_read_back_as_questions(self):
        async def go():
            a = self.make_adapter(discovery_seconds=0)
            adapter_mod.watch("b1")
            self.fake.add_bot("b1")
            await a._tick()
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
            a._bots["b1"] = adapter_mod._Watch("b1")
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


class TestMeetingGuard(unittest.TestCase):
    """Meeting chat senders are display names anyone can type, so meeting turns get web search only."""

    def setUp(self):
        guard._MEETING_IDS.clear()

    def test_meeting_turn_blocks_everything_but_web_search(self):
        guard.on_pre_llm_call(session_id="s-meet", task_id="t-meet", platform="meetstream")
        for tool in ("terminal", "write_file", "patch", "execute_code", "delegate_task", "memory",
                     "mcp__fs__read_file", "meetstream_join", "meetstream_transcript", "send_message"):
            verdict = guard.on_pre_tool_call(tool_name=tool, session_id="s-meet", task_id="t-meet")
            self.assertEqual((verdict or {}).get("action"), "block", tool)
        for tool in ("web_search", "web_extract"):
            self.assertIsNone(guard.on_pre_tool_call(tool_name=tool, session_id="s-meet"))

    def test_platform_object_and_enum_spellings_count(self):
        from gateway.config import Platform

        guard.on_pre_llm_call(session_id="s-obj", platform=Platform("meetstream"))
        guard.on_pre_llm_call(session_id="s-enum", platform="Platform.MEETSTREAM")
        guard.on_pre_llm_call(session_id="s-pad", platform=" MeetStream ")
        for sid in ("s-obj", "s-enum", "s-pad"):
            self.assertEqual(guard.on_pre_tool_call(tool_name="terminal", session_id=sid)["action"], "block", sid)
        guard.on_pre_llm_call(session_id="s-near", platform="meetstream-like")
        self.assertIsNone(guard.on_pre_tool_call(tool_name="terminal", session_id="s-near"))

    def test_active_meeting_session_is_not_evicted(self):
        guard.on_pre_llm_call(session_id="s-live", platform="meetstream")
        for i in range(guard._MAX_TRACKED - 1):
            guard._remember(f"filler-{i}")
        guard.on_pre_tool_call(tool_name="terminal", session_id="s-live")  # refreshes s-live
        guard._remember("one-more")
        self.assertTrue(guard.is_meeting_turn(session_id="s-live"))

    def test_task_id_alone_identifies_the_meeting_turn(self):
        guard.on_pre_llm_call(session_id="", task_id="t-only", platform="meetstream")
        self.assertEqual(guard.on_pre_tool_call(tool_name="terminal", task_id="t-only")["action"], "block")

    def test_other_platforms_are_untouched(self):
        guard.on_pre_llm_call(session_id="s-cli", platform="cli")
        guard.on_pre_llm_call(session_id="s-tg", platform="telegram")
        for sid in ("s-cli", "s-tg", "", "unknown"):
            self.assertIsNone(guard.on_pre_tool_call(tool_name="terminal", session_id=sid))

    def test_tracking_is_bounded(self):
        for i in range(guard._MAX_TRACKED + 50):
            guard.on_pre_llm_call(session_id=f"s{i}", platform="meetstream")
        self.assertLessEqual(len(guard._MEETING_IDS), guard._MAX_TRACKED)
        self.assertTrue(guard.is_meeting_turn(session_id=f"s{guard._MAX_TRACKED + 49}"))

    def test_adapter_pins_meeting_turns_to_web_without_mcp(self):
        a = adapter_mod.MeetStreamAdapter(PlatformConfig(enabled=True, extra={}))
        self.assertEqual(a.toolsets_for_source(None), ["web", "no_mcp"])

    def test_hermes_resolves_the_override_without_shell_files_or_mcp(self):
        # The gateway feeds toolsets_for_source() through this same resolver (gateway/run_turn.py).
        from hermes_cli.tools_config import _get_platform_tools

        cfg = {"platform_toolsets": {"meetstream": ["web", "no_mcp"]},
               "mcp_servers": {"localfs": {"command": "npx", "args": ["fs-server"]}}}
        enabled = _get_platform_tools(cfg, "meetstream")
        self.assertIn("web", enabled)
        self.assertFalse({"terminal", "file", "code_execution", "delegation", "localfs"} & enabled, enabled)


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
        hooks = {c.args[0] for c in ctx.register_hook.call_args_list}
        self.assertEqual(hooks, {"pre_llm_call", "pre_tool_call"})
        self.assertEqual(ctx.register_skill.call_args.args[0], "hermes-in-meetings")


if __name__ == "__main__":
    unittest.main()
