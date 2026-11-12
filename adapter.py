"""MeetStream gateway platform: the meeting chat of each call Hermes is in becomes a conversation.

Outbound only. The adapter polls the MeetStream API; Hermes opens no port and needs no webhook.
- Discovery: bots this Hermes started (``meetstream_join`` hands them over at once) plus, every
  ``discovery_seconds``, the account's bots of the last day that are tagged ``hermes: true`` or carry
  the Hermes bot name, so a bot that MeetStream calendar auto-join sends under that name also gets Hermes.
- Chat: while a bot is in the call, its chat is read every ``chat_poll_seconds``. A message is
  handed to the agent only when it addresses the bot ("Hermes, ..." or "@Hermes ...").
- Identity: ``user_id`` is the sender's display name, lower-cased. Display names are chosen by
  participants, so ``MEETSTREAM_ALLOWED_USERS`` is a convenience list, not proof of identity.
  The gateway denies everyone until that list (or pairing) admits them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from . import meetstream_client as api

logger = logging.getLogger(__name__)

PLATFORM = "meetstream"
KEY_ENV = "MEETSTREAM_API_KEY"
CHAT_POLL_S = 5.0
DISCOVERY_S = 120.0
STATUS_REFRESH_S = 30.0
MIN_POLL_S = 3.0  # floor so a typo in config cannot hammer the API
BACKOFF_S = [5, 10, 30, 60]

PLATFORM_HINT = (
    "You are in a live video call through a MeetStream meeting bot, answering in the meeting chat. "
    "Everyone in the call reads your reply. Write short plain-text answers (no markdown, no tables), "
    "at most a few sentences; each chat message is capped at 2000 characters. You cannot hear the call "
    "live: you see only chat messages addressed to you. The transcript arrives after the call ends "
    "(meetstream_transcript). Do not share the user's private data with the call unless they asked."
)

# bot_id -> True for bots handed over by meetstream_join in this process. Drained by the adapter.
_PENDING: dict[str, bool] = {}


def watch(bot_id: str) -> None:
    """Called by ``meetstream_join``: start watching this bot's chat on the next poll."""
    _PENDING[str(bot_id)] = True


def _key() -> str:
    return str(_get_scoped_secret(KEY_ENV, "") or "").strip()


def check_requirements() -> bool:
    return bool(_key())


def validate_config(config) -> bool:
    return check_requirements()


def is_connected(config) -> bool:
    return check_requirements()


def normalize_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def addressed(text: str, names: list[str]) -> Optional[str]:
    """The question with the leading mention removed, or ``None`` when the message is not for the bot.

    "Hermes, what's next?" / "@Hermes what's next" / "hey hermes: what's next" all count; a message
    that only mentions the name mid-sentence ("ask Hermes later") does not, unless it is an @-mention.
    """
    text = (text or "").strip()
    for name in names:
        n = re.escape(name)
        m = re.match(rf"^(?:hey\s+|hi\s+|ok\s+)?@?{n}\b[\s,:;!.?-]*", text, re.I)
        if m:
            rest = text[m.end():].strip()
            return rest or None
        if re.search(rf"(?<!\w)@{n}\b", text, re.I):
            return re.sub(rf"(?<!\w)@{n}\b[\s,:;]*", "", text, flags=re.I).strip() or None
    return None


@dataclass
class _Watch:
    bot_id: str
    status: str = ""
    seeded: bool = False  # existing chat already marked seen
    fresh: bool = False  # started by this Hermes: answer from the first message
    checked_at: float = 0.0
    seen: set[str] = dc_field(default_factory=set)


class MeetStreamAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = api.MAX_CHAT

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = config.extra or {}
        self._chat_poll = max(MIN_POLL_S, float(extra.get("chat_poll_seconds") or CHAT_POLL_S))
        discovery = float(extra.get("discovery_seconds") if extra.get("discovery_seconds") is not None else DISCOVERY_S)
        self._discovery = 0.0 if discovery <= 0 else max(30.0, discovery)
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._bots: dict[str, _Watch] = {}
        self._sent: dict[str, list[str]] = {}
        self._discovered_at = 0.0
        self._dedup = MessageDeduplicator(max_size=5000, ttl_seconds=6 * 3600)

    def normalize_user_id(self, user_id: Any) -> str:
        return normalize_name(user_id)

    def _names(self) -> list[str]:
        from .tools import settings

        name = str(settings()["bot_name"]).strip()
        return list(dict.fromkeys(n for n in (name, "hermes") if n))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not _key():
            logger.warning("[%s] %s is not set", self.name, KEY_ENV)
            return False
        self._client = api.new_client()
        self._task = asyncio.create_task(self._run())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        logger.info("[%s] Connected; chat poll %.0fs, discovery %s", self.name, self._chat_poll,
                    f"{self._discovery:.0f}s" if self._discovery else "off")
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._bots.clear()

    # -- Poll loop ---------------------------------------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while self._running:
            started = time.monotonic()
            try:
                await self._tick()
                failures = 0
            except asyncio.CancelledError:
                return
            except api.MeetStreamError as e:
                if e.status in (401, 403):
                    self._set_fatal_error("meetstream_unauthorized",
                                          f"MeetStream rejected the API key ({e.status}). Check {KEY_ENV}.",
                                          retryable=False)
                    self._running = False
                    return
                failures += 1
                logger.warning("[%s] MeetStream error: %s", self.name, e)
            except Exception as e:
                failures += 1
                logger.warning("[%s] Poll failed: %s", self.name, e)
            if failures:
                await asyncio.sleep(BACKOFF_S[min(failures, len(BACKOFF_S)) - 1])
            await asyncio.sleep(max(0.0, self._chat_poll - (time.monotonic() - started)))

    async def _tick(self) -> None:
        while _PENDING:
            bot_id, _ = _PENDING.popitem()
            self._bots.setdefault(bot_id, _Watch(bot_id, fresh=True, seeded=True))
        now = time.monotonic()
        if self._discovery and now - self._discovered_at >= self._discovery:
            self._discovered_at = now
            await self._discover()
        for watch_ in list(self._bots.values()):
            await self._poll_bot(watch_)

    async def _discover(self) -> None:
        since = (datetime.now(tz=UTC) - timedelta(days=1)).date().isoformat()
        data = await api.list_bots(self._client, _key(), {"from": since})
        bot_name = self._names()[0]
        for record in data.get("bots") or []:
            bot_id = api.field(record, "BotID", "bot_id", "id")
            status = api.norm_status(api.field(record, "Status", "status"))
            if not bot_id or status in api.ENDED or not api.is_hermes_bot(record, bot_name):
                continue
            if bot_id not in self._bots:
                logger.info("[%s] Watching bot %s (%s)", self.name, bot_id, status or "unknown status")
                self._bots[bot_id] = _Watch(bot_id, status=status)

    async def _poll_bot(self, w: _Watch) -> None:
        now = time.monotonic()
        if not w.status or now - w.checked_at >= STATUS_REFRESH_S:
            w.checked_at = now
            w.status = api.norm_status((await api.bot_status(self._client, _key(), w.bot_id)).get("status"))
        if w.status in api.ENDED:
            logger.info("[%s] Bot %s ended (%s); no longer watching", self.name, w.bot_id, w.status)
            self._bots.pop(w.bot_id, None)
            self._sent.pop(w.bot_id, None)
            return
        if w.status not in api.CHAT_READY:
            return
        messages = await api.get_chats(self._client, _key(), w.bot_id)
        if not w.seeded:
            # Picked up by discovery mid-call (or after a gateway restart): old chat is history, not a question.
            w.seen.update(self._message_id(w.bot_id, m) for m in messages)
            w.seeded = True
            return
        for message in messages:
            msg_id = self._message_id(w.bot_id, message)
            if msg_id in w.seen:
                continue
            w.seen.add(msg_id)
            await self._on_chat(w, message, msg_id)

    @staticmethod
    def _message_id(bot_id: str, message: dict) -> str:
        ref = message.get("messageId") or f"{message.get('timestamp')}|{message.get('speakerName')}|{message.get('text')}"
        return f"{bot_id}:{ref}"

    async def _on_chat(self, w: _Watch, message: dict, msg_id: str) -> None:
        text = str(message.get("text") or "").strip()
        sender = str(api.field(message, "speakerDisplayName", "speakerName", default="") or "")
        names = self._names()
        if not text or normalize_name(sender) in {normalize_name(n) for n in names}:
            return  # the bot's own messages
        if text in self._sent.get(w.bot_id, []):
            return
        question = addressed(text, names)
        if not question or self._dedup.is_duplicate(msg_id):
            return
        user = normalize_name(sender) or "participant"
        source = self.build_source(chat_id=w.bot_id, chat_name=f"Meeting {w.bot_id[:8]}", chat_type="group",
                                   user_id=user, user_name=sender or "Participant", message_id=msg_id)
        timestamp = datetime.now(tz=UTC)
        try:
            timestamp = datetime.fromisoformat(str(message.get("timestamp")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
        await self.handle_message(MessageEvent(text=question, message_type=MessageType.TEXT, source=source,
                                               message_id=msg_id, raw_message=message, timestamp=timestamp))

    # -- Outbound ----------------------------------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="MeetStream adapter is not connected")
        sent = self._sent.setdefault(chat_id, [])
        try:
            for part in api.chunks(content):
                sent.append(part)
                await api.send_message(self._client, _key(), chat_id, part)
        except api.MeetStreamError as e:
            return SendResult(success=False, error=e.detail)
        del sent[:-50]
        return SendResult(success=True)

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        return None  # meeting chat has no typing indicator

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"Meeting {chat_id[:8]}", "type": "group", "chat_id": chat_id}
