"""
Forced-Subscribe Auto-Detection & Auto-Join Service.

Some Telegram groups deploy "forced subscription" bots that prevent new
members from sending messages until they join a specific channel or group.
This service detects and resolves those restrictions automatically:

  1. After a successful group join, listens for bot messages for up to
     LISTEN_TIMEOUT seconds. If a forced-subscribe message is found, it
     extracts the required channel/group links and auto-joins them.

  2. When send_message_to_group_advanced() encounters ChatWriteForbiddenError,
     it calls handle_write_forbidden(), which scans recent group history for
     forced-subscribe instructions and auto-joins the required targets.

  3. In both cases the admin is notified of every auto-join action.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.services.telegram_service import TelegramUserService

logger = get_logger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# Private invite links: t.me/+ or t.me/joinchat/
_PRIVATE_INVITE_RE = re.compile(
    r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", re.I
)

# Public t.me/<username> links
_PUBLIC_TGME_RE = re.compile(
    r"t\.me/([a-zA-Z][a-zA-Z0-9_]{3,31})(?:\b|$)", re.I
)

# @username mentions
_AT_USERNAME_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})")

# ── Heuristic keywords ────────────────────────────────────────────────────────

# If a bot message contains a link AND one of these keywords, it is treated
# as a forced-subscribe notice.
_SUBSCRIBE_KEYWORDS = (
    "عضو", "subscribe", "join", "کانال", "channel", "گروه",
    "ابتدا", "اول", "پیوستن", "member", "عضویت", "ارسال",
    "مجاز", "دسترسی", "اجازه", "مشارکت", "باید", "must",
    "required", "الزامی", "ضروری",
)

# ── Tunables ─────────────────────────────────────────────────────────────────

# Seconds to listen for bot messages right after joining a group
_LISTEN_TIMEOUT: int = 20

# Recent messages to scan when send is blocked (ChatWriteForbiddenError)
_HISTORY_SCAN_LIMIT: int = 15

# Delay (seconds) between consecutive auto-join calls (anti-flood)
_AUTO_JOIN_DELAY: float = 3.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _looks_like_forced_subscribe(text: str) -> bool:
    """Return True if *text* looks like a forced-subscribe restriction notice."""
    text_lower = text.lower()
    has_keyword = any(kw in text_lower for kw in _SUBSCRIBE_KEYWORDS)
    has_link = bool(
        _PRIVATE_INVITE_RE.search(text)
        or _PUBLIC_TGME_RE.search(text)
        or _AT_USERNAME_RE.search(text)
    )
    return has_keyword and has_link


def _extract_targets(text: str) -> list[str]:
    """Extract joinable channel/group identifiers from *text*, deduplicated."""
    targets: list[str] = []
    seen_usernames: set[str] = set()

    # 1. Private invite links — highest priority
    for m in _PRIVATE_INVITE_RE.finditer(text):
        full = f"https://t.me/+{m.group(1)}"
        if full not in targets:
            targets.append(full)

    # 2. Public t.me/<username> links
    for m in _PUBLIC_TGME_RE.finditer(text):
        username = m.group(1).lower()
        # Skip fragments that look like URL paths, not usernames
        if username in ("joinchat", "share", "msg"):
            continue
        if username not in seen_usernames:
            seen_usernames.add(username)
            targets.append(f"@{m.group(1)}")

    # 3. @username mentions (only if not already captured via t.me link)
    for m in _AT_USERNAME_RE.finditer(text):
        username = m.group(1).lower()
        if username not in seen_usernames:
            seen_usernames.add(username)
            targets.append(f"@{m.group(1)}")

    return targets


# ── Service ───────────────────────────────────────────────────────────────────

class ForcedSubscribeService:
    """Singleton service that detects and auto-resolves forced-subscribe bans."""

    _instance: ForcedSubscribeService | None = None

    def __init__(self) -> None:
        self._tg: TelegramUserService | None = None

    @classmethod
    def get_instance(cls) -> ForcedSubscribeService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: TelegramUserService) -> None:
        self._tg = tg

    # ── Public API ────────────────────────────────────────────────────────────

    async def check_after_join(
        self, group_id: int, group_title: str | None = None
    ) -> list[str]:
        """
        Listen for forced-subscribe bot messages for up to LISTEN_TIMEOUT
        seconds right after the account joins *group_id*.

        Returns a list of channel/group targets that were successfully joined.
        """
        if self._tg is None:
            return []

        auto_joined: list[str] = []
        try:
            from telethon import events  # local import — Telethon may not be in scope

            detected_targets: list[str] = []
            found_event = asyncio.Event()

            @self._tg.client.on(events.NewMessage(chats=group_id))
            async def _handler(event) -> None:  # type: ignore[type-arg]
                text: str = event.raw_text or ""
                if not text:
                    return

                # Only trust messages from bots or service messages (no sender)
                sender = await event.get_sender()
                is_bot = getattr(sender, "bot", False) if sender else True  # no sender = service
                if not is_bot:
                    return  # ignore regular user messages to avoid false positives

                logger.debug(
                    "ForcedSubscribe[check_after_join]: bot msg in group=%d text=%r",
                    group_id, text[:100],
                )

                if _looks_like_forced_subscribe(text):
                    targets = _extract_targets(text)
                    if targets:
                        logger.info(
                            "ForcedSubscribe: restriction detected in group %d (%r) "
                            "— required targets: %s",
                            group_id, group_title, targets,
                        )
                        detected_targets.extend(targets)
                        found_event.set()

            try:
                await asyncio.wait_for(found_event.wait(), timeout=_LISTEN_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            finally:
                self._tg.client.remove_event_handler(_handler)

            if detected_targets:
                auto_joined = await self._join_targets(
                    detected_targets, group_id, group_title
                )

        except Exception as exc:
            logger.error(
                "ForcedSubscribe.check_after_join error for group %d: %s",
                group_id, exc, exc_info=True,
            )

        return auto_joined

    async def handle_write_forbidden(
        self, group_id: int, group_title: str | None = None
    ) -> list[str]:
        """
        Called when ChatWriteForbiddenError is raised while trying to send
        to *group_id*. Scans recent group messages for forced-subscribe
        instructions and auto-joins required channels/groups.

        Returns a list of channel/group targets that were successfully joined.
        """
        if self._tg is None:
            return []

        auto_joined: list[str] = []
        try:
            detected_targets: list[str] = []

            async for message in self._tg.client.iter_messages(
                group_id, limit=_HISTORY_SCAN_LIMIT
            ):
                text: str = (
                    getattr(message, "raw_text", None)
                    or getattr(message, "message", None)
                    or ""
                )
                if not text:
                    continue

                # Only trust service messages (no sender) or bot messages
                sender_id = getattr(message, "sender_id", None)
                if sender_id is not None:
                    try:
                        sender = await self._tg.client.get_entity(sender_id)
                        is_bot = getattr(sender, "bot", False)
                        if not is_bot:
                            continue  # skip regular user messages
                    except Exception:
                        continue  # can't resolve sender → skip to be safe

                if _looks_like_forced_subscribe(text):
                    targets = _extract_targets(text)
                    if targets:
                        logger.info(
                            "ForcedSubscribe[write-forbidden]: restriction in group %d (%r) "
                            "— targets: %s",
                            group_id, group_title, targets,
                        )
                        detected_targets.extend(targets)
                        break  # stop at first high-confidence match

            if detected_targets:
                # Deduplicate before joining
                seen: set[str] = set()
                unique = [t for t in detected_targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
                auto_joined = await self._join_targets(unique, group_id, group_title)

        except Exception as exc:
            logger.error(
                "ForcedSubscribe.handle_write_forbidden error for group %d: %s",
                group_id, exc, exc_info=True,
            )

        return auto_joined

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _join_targets(
        self,
        targets: list[str],
        source_group_id: int,
        source_group_title: str | None,
    ) -> list[str]:
        """
        Attempt to join each target. Returns the list of successfully joined
        channels/groups.
        """
        if self._tg is None:
            return []

        joined: list[str] = []

        for target in targets:
            try:
                # Validate target is a channel/group before joining
                if not await self._is_joinable_entity(target):
                    logger.warning(
                        "ForcedSubscribe: skipping %r — not a channel/group", target
                    )
                    continue

                success, _, error = await self._tg.join_group(target)
                if success:
                    logger.info(
                        "ForcedSubscribe: ✅ auto-joined %r (required by group %d %r)",
                        target, source_group_id, source_group_title,
                    )
                    joined.append(target)
                    await self._notify(
                        source_group_id, source_group_title,
                        target, joined=True, error=None,
                    )
                else:
                    logger.warning(
                        "ForcedSubscribe: ⚠️ failed to join %r required by group %d: %s",
                        target, source_group_id, error,
                    )
                    await self._notify(
                        source_group_id, source_group_title,
                        target, joined=False, error=error,
                    )

                await asyncio.sleep(_AUTO_JOIN_DELAY)

            except Exception as exc:
                logger.error(
                    "ForcedSubscribe._join_targets: unexpected error for %r: %s",
                    target, exc,
                )

        return joined

    async def _is_joinable_entity(self, target: str) -> bool:
        """Return True only if *target* resolves to a channel or group (not a user)."""
        if self._tg is None:
            return False
        try:
            from telethon.tl.types import Channel, Chat, ChatForbidden, ChannelForbidden
            entity = await self._tg.client.get_entity(target)
            return isinstance(entity, (Channel, Chat, ChatForbidden, ChannelForbidden))
        except Exception as exc:
            logger.debug("ForcedSubscribe._is_joinable_entity(%r): %s", target, exc)
            return False

    async def _notify(
        self,
        source_group_id: int,
        source_group_title: str | None,
        target: str,
        joined: bool,
        error: str | None,
    ) -> None:
        """Send an admin notification about a forced-subscribe auto-join action."""
        try:
            from app.services.notification_service import NotificationService

            ns = NotificationService.get_instance()
            group_label = (
                f"<b>{source_group_title}</b>"
                if source_group_title
                else f"<code>{source_group_id}</code>"
            )

            if joined:
                text = (
                    f"🔗 <b>Forced-Subscribe: عضویت خودکار</b>\n\n"
                    f"گروه: {group_label}\n"
                    f"کانال/گروه مورد نیاز: <code>{target}</code>\n"
                    f"وضعیت: ✅ عضو شدیم — محدودیت برداشته شد"
                )
            else:
                text = (
                    f"⚠️ <b>Forced-Subscribe: عضویت ناموفق</b>\n\n"
                    f"گروه: {group_label}\n"
                    f"کانال/گروه مورد نیاز: <code>{target}</code>\n"
                    f"خطا: <code>{error or 'نامشخص'}</code>"
                )

            await ns.notify(text, parse_mode="HTML")
        except Exception as exc:
            logger.error("ForcedSubscribe._notify error: %s", exc)
