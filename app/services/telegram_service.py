import asyncio
import re
from typing import Any
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User, ChatInvite, ChatInviteAlready
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UserIsBlockedError,
    InputUserDeactivatedError,
    PeerFloodError,
)

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PRIVATE_INVITE_RE = re.compile(
    r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", re.I
)


class TelegramUserService:
    _instance: "TelegramUserService | None" = None

    def __init__(self) -> None:
        session = (
            StringSession(settings.TELEGRAM_SESSION_STRING)
            if settings.TELEGRAM_SESSION_STRING
            else StringSession()
        )
        self.client = TelegramClient(
            session,
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            connection_retries=10,
            retry_delay=5,
            auto_reconnect=True,
        )
        self._running = False

    @classmethod
    def get_instance(cls) -> "TelegramUserService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.warning("Telegram session not authorized — interactive login required")
            raise RuntimeError(
                "Telegram session is not authorized. "
                "Run `python -m app.cli login` locally to generate a session string "
                "and set TELEGRAM_SESSION_STRING in your environment."
            )
        me = await self.client.get_me()
        logger.info("Telegram user client connected", extra={"user": str(me)})
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self.client.is_connected():
            await self.client.disconnect()
        logger.info("Telegram user client disconnected")

    def is_running(self) -> bool:
        return self._running and self.client.is_connected()

    async def resolve_entity(self, link: str) -> Any | None:
        """Resolve a Telegram link to an entity.
        Supports both public username links and private invite links.
        """
        try:
            m = _PRIVATE_INVITE_RE.search(link)
            if m:
                invite_hash = m.group(1)
                try:
                    from telethon.tl.functions.messages import CheckChatInviteRequest
                    result = await self.client(CheckChatInviteRequest(invite_hash))
                    return result  # ChatInvite or ChatInviteAlready
                except Exception as exc:
                    logger.debug("CheckChatInviteRequest failed for %s: %s", link, exc)
                    return None
            return await self.client.get_entity(link)
        except Exception as exc:
            logger.debug("Cannot resolve entity %s: %s", link, exc)
            return None

    async def is_group(self, entity: Any) -> bool:
        """Return True if the entity is a group (not a broadcast channel)."""
        if isinstance(entity, ChatInviteAlready):
            chat = getattr(entity, "chat", None)
            if chat:
                return isinstance(chat, (Chat, Channel)) and not getattr(chat, "broadcast", False)
            return False
        if isinstance(entity, ChatInvite):
            # Valid group/supergroup invite — broadcast=False means it's a group
            return not getattr(entity, "broadcast", False)
        return isinstance(entity, (Chat, Channel)) and not getattr(entity, "broadcast", False)

    async def get_entity_info(
        self, entity: Any
    ) -> tuple[int | None, str | None, str | None, int | None]:
        """Extract (group_id, title, username, members_count) from any resolved entity type."""
        if isinstance(entity, ChatInviteAlready):
            chat = getattr(entity, "chat", None)
            if chat:
                return (
                    chat.id,
                    getattr(chat, "title", None),
                    getattr(chat, "username", None),
                    getattr(chat, "participants_count", None),
                )
            return None, None, None, None
        if isinstance(entity, ChatInvite):
            # Private invite not yet joined — group_id unknown until we actually join
            return (
                None,
                getattr(entity, "title", None),
                None,
                getattr(entity, "participants_count", None),
            )
        return (
            getattr(entity, "id", None),
            getattr(entity, "title", None),
            getattr(entity, "username", None),
            getattr(entity, "participants_count", None),
        )

    async def get_user_bio(self, user_id: int) -> str:
        try:
            full = await self.client.get_entity(user_id)
            if isinstance(full, User):
                from telethon.tl.functions.users import GetFullUserRequest
                info = await self.client(GetFullUserRequest(full))
                return getattr(info.full_user, "about", "") or ""
        except Exception:
            pass
        return ""

    async def join_group(self, link: str) -> tuple[bool, int | None]:
        """
        Join a group by invite link or public username.

        Returns (success, real_group_id).
        - For private invite links the real group_id is extracted from the join
          response so callers can update any placeholder record.
        - For public links the entity is resolved first and its id returned.
        """
        try:
            m = _PRIVATE_INVITE_RE.search(link)
            if m:
                invite_hash = m.group(1)
                from telethon.tl.functions.messages import ImportChatInviteRequest
                updates = await self.client(ImportChatInviteRequest(invite_hash))
                real_id: int | None = None
                if hasattr(updates, "chats") and updates.chats:
                    real_id = updates.chats[0].id
                logger.info("Joined private group via invite: %s (group_id=%s)", link, real_id)
                return True, real_id
            else:
                from telethon.tl.functions.channels import JoinChannelRequest
                entity = await self.client.get_entity(link)
                await self.client(JoinChannelRequest(entity))
                real_id = getattr(entity, "id", None)
                logger.info("Joined public group: %s (group_id=%s)", link, real_id)
                return True, real_id
        except UserAlreadyParticipantError:
            logger.info("Already in group: %s", link)
            return True, None
        except FloodWaitError as exc:
            logger.warning("FloodWait joining %s: wait %d seconds", link, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, None
        except Exception as exc:
            logger.error("Failed to join %s: %s", link, exc)
            return False, None

    async def send_message_to_group(self, group_id: int, message: Any) -> bool:
        try:
            await self.client.send_message(group_id, message)
            return True
        except FloodWaitError as exc:
            logger.warning("FloodWait sending to %d: wait %d seconds", group_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False
        except Exception as exc:
            logger.error("Failed to send to %d: %s", group_id, exc)
            return False

    async def send_message_to_user(self, user_id: int, message: Any) -> tuple[bool, str | None]:
        """Send a DM to a user. Returns (success, error_reason)."""
        try:
            await self.client.send_message(user_id, message)
            return True, None
        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except PeerFloodError:
            logger.warning("PeerFlood — too many DMs sent, slowing down")
            await asyncio.sleep(60)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning("FloodWait sending DM to %d: wait %d seconds", user_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, "flood_wait"
        except Exception as exc:
            logger.error("Failed to send DM to %d: %s", user_id, exc)
            return False, str(exc)

    async def forward_message_to_user(
        self, user_id: int, from_chat_id: int, message_id: int
    ) -> tuple[bool, str | None]:
        """Forward a message to a user. Returns (success, error_reason)."""
        try:
            await self.client.forward_messages(user_id, message_id, from_chat_id)
            return True, None
        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except PeerFloodError:
            logger.warning("PeerFlood — too many DMs sent, slowing down")
            await asyncio.sleep(60)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning("FloodWait forwarding to %d: wait %d seconds", user_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, "flood_wait"
        except Exception as exc:
            logger.error("Failed to forward to %d: %s", user_id, exc)
            return False, str(exc)
    async def forward_message_to_group(
        self, group_id: int, from_chat_id: int, message_id: int
    ) -> tuple[bool, str | None]:
        """Forward a message to a group using the user client. Returns (success, error_reason)."""
        try:
            await self.client.forward_messages(group_id, message_id, from_chat_id)
            return True, None
        except FloodWaitError as exc:
            logger.warning("FloodWait forwarding to group %d: wait %d seconds", group_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, "flood_wait"
        except Exception as exc:
            logger.error("Failed to forward to group %d: %s", group_id, exc)
            return False, str(exc)

    def on_new_message(self, handler: Any) -> None:
        self.client.add_event_handler(handler, events.NewMessage())

    async def get_session_string(self) -> str:
        if isinstance(self.client.session, StringSession):
            return self.client.session.save()
        return ""
