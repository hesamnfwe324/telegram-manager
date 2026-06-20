"""Push notifications to all admins via the management bot."""
from typing import Any

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class NotificationService:
    _instance: "NotificationService | None" = None

    def __init__(self) -> None:
        self._bot: Any = None

    @classmethod
    def get_instance(cls) -> "NotificationService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_bot(self, bot: Any) -> None:
        self._bot = bot

    async def notify(self, text: str, parse_mode: str = "HTML") -> None:
        if self._bot is None:
            logger.debug("Notification bot not set — skipping: %s", text[:80])
            return
        admin_ids = settings.get_admin_id_list()
        if not admin_ids:
            logger.warning("notify() called but ADMIN_IDS is empty — no one to notify")
            return
        for admin_id in admin_ids:
            try:
                await self._bot.send_message(admin_id, text, parse_mode=parse_mode)
            except Exception as exc:
                logger.warning("Failed to notify admin %d: %s", admin_id, exc)

    async def notify_critical(self, title: str, detail: str) -> None:
        await self.notify(f"🚨 <b>{title}</b>\n\n{detail}")

    async def notify_info(self, text: str) -> None:
        await self.notify(f"ℹ️ {text}")
