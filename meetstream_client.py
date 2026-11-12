"""Small async client for the MeetStream REST API (https://docs.meetstream.ai).

Every call authenticates with the user's own key as ``Authorization: Token <key>``. Nothing here
logs request or response bodies above DEBUG: meeting chats and transcripts are user data.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.meetstream.ai/api/v1"
TIMEOUT_S = 20.0
MAX_CHAT = 2000  # send_message rejects longer bodies (SendMessageRequest.maxLength)
HERMES_ATTR = "hermes"  # custom attribute that marks a bot as one Hermes speaks through

# Meeting hosts MeetStream can join. Anything else is refused before a bot is billed.
MEETING_HOSTS = ("zoom.us", "zoom.com", "meet.google.com", "teams.microsoft.com", "teams.live.com")

# Bot lifecycle. Chat can be read and written only while the bot is in the call.
CHAT_READY = frozenset({"inmeeting", "recording"})
ENDED = frozenset({"stopped", "notallowed", "denied", "error", "done", "failed", "cancelled", "removed"})

TRANSCRIPTION_PROVIDERS = {
    "meetstream": {"meetstream": {"language": "auto", "translate": False}},
    "meeting_captions": {"meeting_captions": {}},
    "deepgram": {"deepgram": {"model": "nova-3", "language": "en", "diarize": True}},
    "assemblyai": {"assemblyai": {"speech_models": ["best"], "language_code": "en_us", "speaker_labels": True}},
    "none": None,
}

# Same safety timeouts as the MeetStream CLI and MCP server, so a bot never sits in an empty call.
AUTOMATIC_LEAVE = {
    "waiting_room_timeout": 300,
    "everyone_left_timeout": 60,
    "in_call_recording_timeout": 14400,
    "recording_permission_denied_timeout": 60,
}


class MeetStreamError(Exception):
    """A non-2xx answer from MeetStream (``status`` 0 means the request never got an answer)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"MeetStream HTTP {status}: {detail}" if status else detail)
        self.status = status
        self.detail = detail


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_S)


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, dict):
        for key in ("detail", "error", "message"):
            if data.get(key):
                return str(data[key])[:200]
    return str(data)[:200]


async def request(client: httpx.AsyncClient, key: str, method: str, path: str, *,
                  params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
    if not key:
        raise MeetStreamError(0, "MEETSTREAM_API_KEY is not set (create one at https://app.meetstream.ai/api-key)")
    headers = {"Authorization": f"Token {key}", "Accept": "application/json"}
    try:
        resp = await client.request(method, path, params=params, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise MeetStreamError(0, f"MeetStream unreachable: {type(e).__name__}") from e
    logger.debug("MeetStream %s %s -> %s", method, path, resp.status_code)
    if resp.status_code >= 400:
        raise MeetStreamError(resp.status_code, _detail(resp))
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


def field(record: dict, *names: str, default: Any = None) -> Any:
    """First present value among ``names``. List items come back in DynamoDB casing
    (``BotID``, ``Status``) while single-bot endpoints use snake_case (``bot_id``, ``status``)."""
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return default


def norm_status(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def validate_meeting_url(url: str) -> str:
    """Return the stripped URL, or raise ``ValueError`` for anything that is not an https meeting link."""
    url = (url or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("meeting_url must be an https:// Zoom, Google Meet or Microsoft Teams link")
    if not any(host == h or host.endswith("." + h) for h in MEETING_HOSTS):
        raise ValueError(f"{host} is not a Zoom, Google Meet or Microsoft Teams host")
    return url


def build_join_payload(meeting_url: str, *, bot_name: str, message: str = "", join_at: str = "",
                       transcription: str = "meetstream", image_url: str = "") -> dict:
    payload: dict[str, Any] = {
        "meeting_link": validate_meeting_url(meeting_url),
        "bot_name": bot_name,
        "video_required": False,
        "custom_attributes": {HERMES_ATTR: True},
        "automatic_leave": dict(AUTOMATIC_LEAVE),
    }
    if message:
        payload["bot_message"] = message[:MAX_CHAT]
    if join_at:
        payload["join_at"] = join_at
    if image_url:
        payload["bot_image_url"] = image_url
    if transcription not in TRANSCRIPTION_PROVIDERS:
        raise ValueError(f"transcription must be one of {', '.join(TRANSCRIPTION_PROVIDERS)}")
    provider = TRANSCRIPTION_PROVIDERS[transcription]
    if provider:
        payload["recording_config"] = {"transcript": {"provider": provider}}
    return payload


def chunks(text: str, size: int = MAX_CHAT) -> list[str]:
    """Split on paragraph, then line, then word boundaries so each part fits one chat message."""
    text = (text or "").strip()
    parts: list[str] = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def is_hermes_bot(record: dict, bot_name: str) -> bool:
    """A bot Hermes speaks through: tagged ``hermes: true`` by ``meetstream_join``, or named like the
    Hermes bot. The name rule covers calendar auto-join, which replaces ``custom_attributes`` with
    its own scheduling metadata, so a tag set in the default bot config does not survive."""
    attrs = field(record, "custom_attributes", "CustomAttributes", default={}) or {}
    value = attrs.get(HERMES_ATTR) if isinstance(attrs, dict) else None
    if value is True or str(value).lower() == "true":
        return True
    name = _norm_name(field(record, "BotUsername", "bot_name", "BotName"))
    return bool(name) and name == _norm_name(bot_name)


# -- Endpoints -------------------------------------------------------------------------------------

async def create_bot(client, key, payload: dict) -> dict:
    return await request(client, key, "POST", "/bots/create_bot", body=payload)


async def bot_status(client, key, bot_id: str) -> dict:
    return await request(client, key, "GET", f"/bots/{bot_id}/status")


async def bot_detail(client, key, bot_id: str) -> dict:
    return await request(client, key, "GET", f"/bots/{bot_id}/detail")


async def list_bots(client, key, params: Optional[dict] = None) -> dict:
    return await request(client, key, "GET", "/bots", params=params)


async def get_chats(client, key, bot_id: str) -> list[dict]:
    data = await request(client, key, "GET", f"/bots/{bot_id}/get_chats")
    messages = data.get("chatMessages") if isinstance(data, dict) else None
    return [m for m in (messages or []) if isinstance(m, dict)]


async def send_message(client, key, bot_id: str, text: str) -> dict:
    return await request(client, key, "POST", f"/bots/{bot_id}/send_message", body={"message": text[:MAX_CHAT]})


async def remove_bot(client, key, bot_id: str) -> dict:
    return await request(client, key, "GET", f"/bots/{bot_id}/remove_bot")


async def transcript(client, key, bot_id: str) -> tuple[Optional[str], Any]:
    """``(transcript_id, segments)``; segments is ``None`` while the transcript is not ready."""
    detail = await bot_detail(client, key, bot_id)
    tid = field(detail.get("bot_details") or {}, "transcript_id") or field(detail, "transcript_id")
    if not tid:
        runs = (await request(client, key, "GET", f"/bots/{bot_id}/transcriptions")).get("transcriptions") or []
        done = [r for r in runs if re.search(r"success|completed", str(r.get("status", "")), re.I)]
        tid = (done or runs or [{}])[0].get("transcript_id")
    if not tid:
        return None, None
    try:
        data = await request(client, key, "GET", f"/transcript/{tid}/get_transcript")
    except MeetStreamError as e:
        if e.status in (400, 404):
            return tid, None
        raise
    segments = data.get("message") if isinstance(data, dict) and isinstance(data.get("message"), list) else data
    return tid, segments or None
