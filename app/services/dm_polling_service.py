"""
DM Polling Service — actively polls Telegram for unread private messages
every few seconds instead of relying on Telethon's event queue.

Why polling instead of events?
  Accounts in many groups receive a constant flood of group updates.
  Even with incoming=True + is_private filters, Telethon still queues
  all raw updates before dispatching — group traffic delays private-
  message events by tens of seconds or minutes.
  Polling bypasses the queue entirely and checks directly.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.config import settings
from app.services.grok_service import chat
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = get_logger(__name__)

_POLL_INTERVAL = 4          # seconds between polls
_DIALOG_LIMIT  = 25         # check N most-recent dialogs per poll
_responded: dict[int, int] = {}   # user_id -> last message_id we replied to


def _admin_ids() -> set[int]:
    return {int(x) for x in settings.ADMIN_IDS.split(",") if x.strip().isdigit()}


async def _handle_message(client: "TelegramClient", peer_id: int,
                          msg_id: int, text: str, user_name: str) -> None:
    """Call Groq and send the reply. Runs as a background task."""
    try:
        reply = await chat(peer_id, text, user_name=user_name)
        msg = reply if reply else (f"سلام {user_name}! 👋" if user_name else "سلام! 👋")
        await client.send_message(peer_id, msg)
        _responded[peer_id] = msg_id
        logger.info("AI replied to user %d (msg %d)", peer_id, msg_id)
    except Exception as exc:
        logger.error("AI reply failed for user %d: %s", peer_id, exc)


async def polling_loop(client: "TelegramClient") -> None:
    """Infinite loop — polls recent dialogs for unread private messages."""
    admins = _admin_ids()
    logger.info("DM polling loop started (interval=%ds, dialogs=%d)",
                _POLL_INTERVAL, _DIALOG_LIMIT)

    while True:
        try:
            async for dialog in client.iter_dialogs(limit=_DIALOG_LIMIT):
                # Only private (user) chats with unread messages
                if not dialog.is_user:
                    continue
                if dialog.unread_count <= 0:
                    continue

                peer_id: int = dialog.entity.id
                if peer_id in admins:
                    continue

                # Get the latest incoming message
                msgs = await client.get_messages(dialog.entity, limit=1)
                if not msgs:
                    continue
                msg = msgs[0]

                # Skip outgoing, non-text, already-handled
                if msg.out or not msg.text:
                    continue
                if _responded.get(peer_id) == msg.id:
                    continue

                # Mark immediately to prevent double-reply on next poll
                _responded[peer_id] = msg.id
                text = msg.text.strip()
                user_name = getattr(dialog.entity, "first_name", "") or ""

                asyncio.create_task(
                    _handle_message(client, peer_id, msg.id, text, user_name)
                )

        except Exception as exc:
            logger.error("DM polling error: %s", exc)

        await asyncio.sleep(_POLL_INTERVAL)
