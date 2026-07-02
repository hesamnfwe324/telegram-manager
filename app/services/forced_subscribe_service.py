"""
Forced-Subscribe Auto-Detection & Auto-Join Service.

Some Telegram groups deploy "forced subscription" bots that prevent new
members from sending messages until they join a specific channel or group.
This service detects and resolves those restrictions automatically:

  1. Permanent global listener (process_message) registered at startup.
     Monitors ALL incoming messages in all groups. When any message looks
     like a forced-subscribe notice it auto-joins the required targets.

  2. After a successful group join, listens for bot messages for up to
     LISTEN_TIMEOUT seconds. If a forced-subscribe message is found, it
     extracts the required channel/group links and auto-joins them.

  3. When send_message_to_group_advanced() encounters ChatWriteForbiddenError,
     it calls handle_write_forbidden(), which scans recent group history for
     forced-subscribe instructions and auto-joins the required targets.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

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

_SUBSCRIBE_KEYWORDS = (
    "عضو", "subscribe", "join", "کانال", "channel", "گروه",
    "ابتدا", "اول", "پیوستن", "member", "عضویت", "ارسال",
    "مجاز", "دسترسی", "اجازه", "مشارکت", "باید", "must",
    "required", "الزامی", "ضروری", "ممنوع", "محدود",
)

# ── Tunables ─────────────────────────────────────────────────────────────────

_LISTEN_TIMEOUT: int = 30
_HISTORY_SCAN_LIMIT: int = 25
_AUTO_JOIN_DELAY: float = 2.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_urls_from_buttons(message: Any) -> list[str]:
    """
    Extract all URLs from InlineKeyboard buttons attached to a message.
    Most forced-subscribe bots put the channel link in a button, not in text.
    """
    urls: list[str] = []
    try:
        # Try message.buttons (Telethon event)
        buttons = getattr(message, "buttons", None)
        if buttons:
            for row in buttons:
                if not isinstance(row, (list, tuple)):
                    row = [row]
                for btn in row:
                    url = getattr(btn, "url", None)
                    if url:
                        urls.append(url)
    except Exception:
        pass

    try:
        # Fallback: raw reply_markup
        markup = getattr(message, "reply_markup", None)
        if markup:
            for row in getattr(markup, "rows", []):
                for btn in getattr(row, "buttons", []):
                    url = getattr(btn, "url", None)
                    if url:
                        urls.append(url)
    except Exception:
        pass

    return urls


def _extract_targets_from_text(text: str) -> list[str]:
    """Extract joinable identifiers from a text string."""
    targets: list[str] = []
    seen: set[str] = set()

    for m in _PRIVATE_INVITE_RE.finditer(text):
        full = f"https://t.me/+{m.group(1)}"
        if full not in targets:
            targets.append(full)
            seen.add(m.group(1).lower())

    for m in _PUBLIC_TGME_RE.finditer(text):
        username = m.group(1).lower()
        if username in ("joinchat", "share", "msg", "addstickers", "start"):
            continue
        if username not in seen:
            seen.add(username)
            targets.append(f"@{m.group(1)}")

    for m in _AT_USERNAME_RE.finditer(text):
        username = m.group(1).lower()
        if username not in seen:
            seen.add(username)
            targets.append(f"@{m.group(1)}")

    return targets


def _extract_targets(message: Any, text: str) -> list[str]:
    """
    Extract joinable targets from BOTH the message text and any InlineKeyboard
    buttons. Buttons take priority because forced-subscribe bots almost always
    embed the link there.
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(t: str) -> None:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)

    # 1. Button URLs (highest priority)
    for url in _extract_urls_from_buttons(message):
        for t in _extract_targets_from_text(url):
            _add(t)

    # 2. Text links / @mentions
    for t in _extract_targets_from_text(text):
        _add(t)

    return result


def _looks_like_forced_subscribe(text: str, message: Any = None) -> bool:
    """
    Return True if the message looks like a forced-subscribe restriction notice.
    Positive if:
      - text contains a keyword AND (text has a link OR message has button URLs)
      - OR message has button URLs that look like channel/group links AND any keyword is present
    """
    text_lower = text.lower()
    has_keyword = any(kw in text_lower for kw in _SUBSCRIBE_KEYWORDS)

    if not has_keyword:
        return False

    # Has link in text?
    has_text_link = bool(
        _PRIVATE_INVITE_RE.search(text)
        or _PUBLIC_TGME_RE.search(text)
        or _AT_USERNAME_RE.search(text)
    )
    if has_text_link:
        return True

    # Has button with a Telegram link?
    if message is not None:
        button_urls = _extract_urls_from_buttons(message)
        if any("t.me" in u or "telegram.me" in u for u in button_urls):
            return True

    return False


# ── Service ───────────────────────────────────────────────────────────────────

class ForcedSubscribeService:
    """Singleton service that detects and auto-resolves forced-subscribe bans."""

    _instance: ForcedSubscribeService | None = None

    def __init__(self) -> None:
        self._tg: TelegramUserService | None = None
        # Tracks groups currently being processed to avoid duplicate auto-joins
        self._handling: set[int] = set()

    @classmethod
    def get_instance(cls) -> ForcedSubscribeService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: TelegramUserService) -> None:
        self._tg = tg

    # ── Permanent global listener ─────────────────────────────────────────────

    async def process_message(self, event: Any) -> None:
        """
        Permanent handler registered at startup (like DiscoveryService).
        Monitors ALL incoming messages across all groups and auto-resolves
        forced-subscribe restrictions as soon as they appear — regardless of
        whether we just joined or are in the middle of a broadcast.

        We do NOT filter by is_bot because:
        - Some forced-subscribe bots are anonymous admins (bot=False)
        - Some groups use service messages (no sender)
        - Missing a valid detection is worse than a false positive
        """
        try:
            # Only act in group/supergroup chats — never in DMs or channels
            if not event.is_group:
                return

            msg = event.message

            # Skip our own outgoing messages
            if getattr(msg, "out", False):
                return

            text: str = (
                getattr(msg, "text", "")
                or getattr(msg, "message", "")
                or ""
            )

            # Skip completely empty messages with no buttons
            button_urls = _extract_urls_from_buttons(msg)
            if not text and not button_urls:
                return

            if not _looks_like_forced_subscribe(text, msg):
                return

            targets = _extract_targets(msg, text)
            if not targets:
                logger.debug(
                    "ForcedSubscribe[global]: keyword+link match in group %s "
                    "but no joinable targets extracted. text=%r buttons=%r",
                    event.chat_id, text[:120], button_urls,
                )
                return

            group_id: int = event.chat_id
            if group_id in self._handling:
                return  # already processing this group

            sender_id = getattr(msg, "sender_id", None)
            logger.info(
                "ForcedSubscribe[global]: restriction detected in group %d "
                "(sender_id=%s) — targets: %s | text: %r",
                group_id, sender_id, targets, text[:100],
            )

            self._handling.add(group_id)
            try:
                group_title: str | None = None
                try:
                    chat = await event.get_chat()
                    group_title = getattr(chat, "title", None)
                except Exception:
                    pass

                await self._join_targets(targets, group_id, group_title)
            finally:
                self._handling.discard(group_id)

        except Exception as exc:
            logger.error("ForcedSubscribe.process_message error: %s", exc, exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    async def check_after_join(
        self, group_id: int, group_title: str | None = None
    ) -> list[str]:
        """
        Listen for forced-subscribe messages for up to LISTEN_TIMEOUT seconds
        right after joining *group_id*. Returns list of auto-joined targets.
        """
        if self._tg is None:
            return []

        auto_joined: list[str] = []

        try:
            found = asyncio.Event()

            async def _handler(event: Any) -> None:  # type: ignore[type-arg]
                nonlocal auto_joined
                if event.chat_id != group_id:
                    return

                msg = event.message
                text: str = (
                    getattr(msg, "text", "")
                    or getattr(msg, "message", "")
                    or ""
                )

                logger.debug(
                    "ForcedSubscribe[check_after_join]: msg in group %d: %r",
                    group_id, text[:80],
                )

                if not _looks_like_forced_subscribe(text, msg):
                    return

                targets = _extract_targets(msg, text)
                if not targets:
                    return

                sender_id = getattr(msg, "sender_id", None)
                logger.info(
                    "ForcedSubscribe[check_after_join]: restriction in group %d "
                    "(sender=%s) — targets: %s",
                    group_id, sender_id, targets,
                )

                if group_id not in self._handling:
                    self._handling.add(group_id)
                    try:
                        auto_joined = await self._join_targets(targets, group_id, group_title)
                    finally:
                        self._handling.discard(group_id)

                found.set()

            from telethon import events as tg_events
            self._tg.client.add_event_handler(_handler, tg_events.NewMessage())

            try:
                await asyncio.wait_for(found.wait(), timeout=_LISTEN_TIMEOUT)
            except asyncio.TimeoutError:
                logger.debug(
                    "ForcedSubscribe[check_after_join]: no restriction message in %ds for group %d",
                    _LISTEN_TIMEOUT, group_id,
                )
            finally:
                self._tg.client.remove_event_handler(_handler)

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

        Key fix: also parses InlineKeyboard button URLs (not just message text),
        and does NOT filter by sender (any message type is considered).

        Returns a list of channel/group targets that were successfully joined.
        """
        if self._tg is None:
            return []

        auto_joined: list[str] = []
        try:
            detected_targets: list[str] = []

            logger.info(
                "ForcedSubscribe[write-forbidden]: scanning last %d messages in group %d (%r)",
                _HISTORY_SCAN_LIMIT, group_id, group_title,
            )

            async for message in self._tg.client.iter_messages(
                group_id, limit=_HISTORY_SCAN_LIMIT
            ):
                text: str = (
                    getattr(message, "raw_text", None)
                    or getattr(message, "message", None)
                    or ""
                )
                button_urls = _extract_urls_from_buttons(message)

                # Log every message scanned for debugging
                sender_id = getattr(message, "sender_id", None)
                logger.debug(
                    "ForcedSubscribe[scan] sender=%s text=%r buttons=%r",
                    sender_id, text[:80], button_urls,
                )

                if not _looks_like_forced_subscribe(text, message):
                    continue

                targets = _extract_targets(message, text)
                if targets:
                    logger.info(
                        "ForcedSubscribe[write-forbidden]: match found in group %d — "
                        "sender=%s targets=%s text=%r",
                        group_id, sender_id, targets, text[:100],
                    )
                    detected_targets.extend(targets)
                    break  # stop at first high-confidence match

            if detected_targets:
                seen: set[str] = set()
                unique = [t for t in detected_targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
                auto_joined = await self._join_targets(unique, group_id, group_title)
            else:
                logger.warning(
                    "ForcedSubscribe[write-forbidden]: no forced-subscribe message found "
                    "in last %d messages of group %d. The group may use native Telegram "
                    "channel subscription enforcement — cannot auto-detect target channel.",
                    _HISTORY_SCAN_LIMIT, group_id,
                )

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

        NOTE: We do NOT validate entity type before joining. Calling get_entity()
        on a channel we're not yet a member of can raise errors and cause us to
        skip valid targets. Instead we try to join and let join_group() handle errors.
        """
        if self._tg is None:
            return []

        joined: list[str] = []

        for target in targets:
            try:
                logger.info(
                    "ForcedSubscribe: attempting to join %r (required by group %d %r)",
                    target, source_group_id, source_group_title,
                )
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
