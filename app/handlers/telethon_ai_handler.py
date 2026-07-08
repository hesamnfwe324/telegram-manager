"""
Telethon AI handler — responds to incoming private DMs on the personal
user account using Groq AI with multi-step friendly conversation.
"""
from telethon import events

from app.config import settings
from app.services.grok_service import chat, clear_history
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in {
        int(x) for x in settings.ADMIN_IDS.split(",") if x.strip().isdigit()
    }


async def process_message(event: events.NewMessage.Event) -> None:
    """Handle incoming private messages on the Telethon user account."""
    # Only respond to incoming private (1-on-1) messages
    if not event.is_private:
        return
    # Skip outgoing messages (our own)
    if event.out:
        return

    sender_id = event.sender_id
    if sender_id is None:
        return

    # Skip admin users — they control the system
    if _is_admin(sender_id):
        return

    text = (event.message.text or "").strip()
    if not text:
        return

    try:
        sender = await event.get_sender()
        user_name = getattr(sender, "first_name", "") or ""

        # Show typing indicator while Groq thinks
        async with event.client.action(event.chat_id, "typing"):
            reply = await chat(sender_id, text, user_name=user_name)

        if reply:
            await event.reply(reply)
            logger.info("AI replied to Telethon user %d", sender_id)

    except Exception as exc:
        logger.error("Telethon AI handler error for user %d: %s", sender_id, exc)
