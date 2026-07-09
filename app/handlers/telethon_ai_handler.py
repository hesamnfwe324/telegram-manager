"""
Telethon AI handler — responds to incoming private DMs on the personal
user account using Groq AI.

Speed optimisations:
- Dispatches every DM to a background asyncio task immediately so the
  Telethon update loop is NEVER blocked waiting for Groq.
- Typing action is sent as a fire-and-forget coroutine; it must not
  delay the actual reply.
"""
from __future__ import annotations

import asyncio

from telethon import events

from app.config import settings
from app.services.grok_service import chat
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in settings.ADMIN_IDS.split(",") if x.strip().isdigit()}


async def process_message(event: events.NewMessage.Event) -> None:
    """Called by Telethon for every incoming private message.

    Returns immediately — all real work is done in a background task so
    the Telethon update loop can keep moving through the queue.
    """
    try:
        sender_id = event.sender_id
        if sender_id is None:
            return
        if sender_id in _admin_ids():
            return

        text = (event.message.text or "").strip()
        if not text:
            return

        # Fire-and-forget — do NOT await this
        asyncio.create_task(_reply(event, sender_id, text))

    except Exception as exc:
        logger.error("AI handler dispatch error: %s", exc)


async def _reply(event: events.NewMessage.Event, sender_id: int, text: str) -> None:
    """Background task: call Groq and send the reply threaded to the original message."""
    try:
        sender = await event.get_sender()
        user_name = getattr(sender, "first_name", "") or ""

        # Send typing action in background — don't wait for it
        asyncio.create_task(_send_typing(event))

        reply = await chat(sender_id, text, user_name=user_name)

        if reply:
            # reply() threads the response to the user's exact message
            await event.reply(reply)
            logger.info("AI replied to user %d (%d chars)", sender_id, len(reply))
        else:
            logger.warning("AI returned empty reply for user %d — skipping", sender_id)

    except Exception as exc:
        logger.error("AI _reply error for user %d: %s", sender_id, exc, exc_info=True)


async def _send_typing(event: events.NewMessage.Event) -> None:
    """Send typing indicator — best-effort, never blocks the reply."""
    try:
        await event.client.action(event.chat_id, "typing").__aenter__()
    except Exception:
        pass


def register(tg_service) -> None:
    """Register this handler on the Telethon client.

    Filter: incoming=True + is_private — only reacts to messages sent
    directly TO this personal account, never group or channel traffic.
    """
    tg_service.client.add_event_handler(
        process_message,
        events.NewMessage(
            incoming=True,
            func=lambda e: e.is_private,
        ),
    )
    logger.info("Telethon AI private-DM handler registered (background-task mode)")
