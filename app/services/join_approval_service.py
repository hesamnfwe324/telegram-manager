"""
JoinApprovalWatcher — notifies admins when a pending join request gets approved.

When a group requires admin approval and the account sends a join request,
Telethon raises InviteRequestSentError (treated as success by JoinQueueService).
Later, when the group admin accepts the request, Telegram sends a ChatAction
event that the user was added. This service listens for those events and sends
a notification so the admin knows the account is now inside the group.
"""
import asyncio
from typing import Any

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)


class JoinApprovalWatcher:
    """Singleton that watches for approved join requests via Telethon ChatAction events."""

    _instance: "JoinApprovalWatcher | None" = None

    def __init__(self) -> None:
        self._tg: Any = None
        self._me_id: int | None = None

    @classmethod
    def get_instance(cls) -> "JoinApprovalWatcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def start(self) -> None:
        """Register the ChatAction handler on the Telethon client."""
        if self._tg is None:
            logger.warning("JoinApprovalWatcher: no TelegramUserService set — skipping")
            return
        try:
            me = await self._tg.client.get_me()
            self._me_id = me.id
            self._tg.on_chat_action(self._handle_chat_action)
            logger.info(
                "JoinApprovalWatcher started — watching for approved requests (my id=%d)",
                self._me_id,
            )
        except Exception as exc:
            logger.error("JoinApprovalWatcher.start failed: %s", exc)

    async def _handle_chat_action(self, event: Any) -> None:
        """Called by Telethon on every ChatAction in any chat the account can see."""
        try:
            # Only care about "user added" or "user joined" events
            user_joined = getattr(event, "user_joined", False)
            user_added  = getattr(event, "user_added",  False)
            if not (user_joined or user_added):
                return

            # Only care if it's OUR account being added
            event_user_id = getattr(event, "user_id", None)
            if event_user_id != self._me_id:
                return

            chat_id: int = event.chat_id
            logger.info(
                "JoinApprovalWatcher: account was approved/added to chat_id=%d", chat_id
            )

            async with AsyncSessionLocal() as session:
                group_repo = GroupRepository(session)
                log_repo   = LogRepository(session)

                group = await group_repo.get_by_group_id(chat_id)
                title = (group.title if group else None) or str(chat_id)

                await log_repo.add(
                    action="join_request_approved",
                    result="success",
                    target=str(chat_id),
                    details=f"group_id={chat_id} title={title!r} approved_by_admin",
                )
                await session.commit()

            await self._notify(chat_id, title)

        except Exception as exc:
            logger.error("JoinApprovalWatcher._handle_chat_action error: %s", exc, exc_info=True)

    async def _notify(self, chat_id: int, title: str) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify(
                f"🎉 <b>درخواست عضویت تأیید شد!</b>\n\n"
                f"ادمین گروه درخواست عضویت شما را تأیید کرد.\n"
                f"اکانت اکنون داخل گروه است.\n\n"
                f"📌 گروه: <code>{title}</code>\n"
                f"🆔 شناسه: <code>{chat_id}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("JoinApprovalWatcher: notification failed: %s", exc)
