"""Capture and resolve a brief's private messaging destination."""

from __future__ import annotations

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import async_delivery_supported, bound_session_key, get_session_env

PLATFORMS = frozenset({"telegram", "whatsapp"})


def capture_origin(session_id: str, stored_source: str | None) -> dict | None:
    platform = get_session_env("HERMES_SESSION_PLATFORM")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
    user_id = get_session_env("HERMES_SESSION_USER_ID")
    if (platform not in PLATFORMS or platform != stored_source or not bound_session_key()
            or session_id != get_session_env("HERMES_SESSION_ID")
            or get_session_env("HERMES_SESSION_CHAT_TYPE") != "dm" or not async_delivery_supported()
            or not chat_id or not user_id):
        return None
    if platform == "telegram" and (not chat_id.isascii() or not chat_id.isdecimal() or int(chat_id) <= 0):
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "chat_type": "dm",
        "user_id": user_id,
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or None,
        "user_id_alt": get_session_env("HERMES_SESSION_USER_ID_ALT") or None,
        "scope_id": get_session_env("HERMES_SESSION_SCOPE_ID") or None,
        "parent_chat_id": get_session_env("HERMES_SESSION_PARENT_CHAT_ID") or None,
        "profile": get_session_env("HERMES_SESSION_PROFILE") or None,
    }


def resolve_sender(runner, origin: dict, profile: str | None):
    """Resolve the same profile-owned native adapter and verify current DM authorization."""
    if runner is None or origin.get("platform") not in PLATFORMS or origin.get("chat_type") != "dm":
        return None
    source = SessionSource.from_dict({**origin, "profile": profile or origin.get("profile")})
    adapter = runner._delivery_adapter_for(source)
    if adapter is None or not adapter.is_connected or adapter.platform != Platform(origin["platform"]):
        return None
    if not runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
        raise PermissionError("Messaging permission was revoked")
    metadata = {key: value for key in ("thread_id", "scope_id", "parent_chat_id")
                if (value := getattr(source, key, None))}

    if source.platform == Platform.TELEGRAM and source.thread_id:
        # Use Hermes's strict topic send behavior so a deleted topic never falls back to the root DM.
        metadata["telegram_dm_topic_created_for_send"] = True

    async def send(text: str):
        return await adapter.send(source.chat_id, text, metadata=metadata or None)

    return send


def one_message(text: str) -> str:
    """Keep a phone notification in one platform message. The saved history keeps the full brief."""
    if len(text.encode("utf-16-le")) // 2 <= 1800:
        return text
    clipped = text.encode("utf-16-le")[:3400].decode("utf-16-le", errors="ignore")
    return (clipped.rsplit("\n", 1)[0] or clipped).rstrip() + "\n\nThe full brief is saved in Hermes history."
