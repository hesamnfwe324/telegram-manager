from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class AdminAuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        admin_ids = settings.get_admin_id_list()

        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user is None or user.id not in admin_ids:
            uid = user.id if user else "unknown"
            logger.warning("Unauthorized access attempt by user_id=%s", uid)
            if isinstance(event, Message):
                await event.answer("⛔ دسترسی غیرمجاز.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            return

        data["is_admin"] = True
        return await handler(event, data)
