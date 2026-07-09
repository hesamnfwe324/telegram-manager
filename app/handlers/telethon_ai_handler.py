"""
Telethon AI handler — responds to incoming private DMs on the personal
user account using Groq AI with multi-step friendly conversation.
"""
from telethon import events

from app.config import settings
from app.services.grok_service import chat, clear_history
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in settings.ADMIN_IDS.split(",") if x.strip().isdigit()}


async def process_message(event: events.NewMessage.Event) -> None:
    """Handle incoming private messages on the Telethon user account."""
    try:
        sender_id = event.sender_id
        if sender_id is None:
            return

        # Skip admin users
        if sender_id in _admin_ids():
            return

        text = (event.message.text or "").strip()
        if not text:
            return

        logger.info("AI DM received from user %d: %r", sender_id, text[:60])

        sender = await event.get_sender()
        user_name = getattr(sender, "first_name", "") or ""

        # Show typing indicator
        async with event.client.action(event.chat_id, "typing"):
            reply = await chat(sender_id, text, user_name=user_name)

        if reply:
            await event.reply(reply)
            logger.info("AI replied to user %d (%d chars)", sender_id, len(reply))
        else:
            # Fallback — confirms handler is working even if Groq fails
            logger.warning("Groq returned empty reply for user %d — using fallback", sender_id)
            greeting = f"سلام {user_name}! 👋" if user_name else "سلام! 👋"
            await event.reply(greeting)

    except Exception as exc:
        logger.error("Telethon AI handler error: %s", exc, exc_info=True)


def register(tg_service) -> None:
    """Register this handler directly on the Telethon client with proper filters.

    Uses incoming=True + is_private filter so we ONLY react to messages
    sent TO this account (not our own outgoing messages, not group messages).
    """
    tg_service.client.add_event_handler(
        process_message,
        events.NewMessage(
            incoming=True,
            func=lambda e: e.is_private,
        ),
    )
    logger.info("Telethon AI private-DM handler registered")
