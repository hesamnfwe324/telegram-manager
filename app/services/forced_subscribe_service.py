"""
Forced-Subscribe Auto-Detection & Auto-Join Service.

Architecture:
  1. process_message() — permanent global Telethon handler registered at startup.
     Fires on every incoming group message. When a forced-subscribe restriction
     is detected it attempts to auto-join the required channels/groups.

  2. check_after_join() — short-lived listener started right after a group join,
     in case the group bot messages the account immediately on entry.

  3. handle_write_forbidden() — called when send_message_to_group_advanced()
     raises ChatWriteForbiddenError. Scans recent history for a restriction
     notice and auto-joins the required targets.

Anti-spam guarantees (stops the notification flood seen in production):
  - Group-level cooldown: after processing a group, don't process it again
    until at least _GROUP_COOLDOWN_MIN seconds have passed (or until the
    shortest flood-wait among its targets expires).
  - Target-level flood-wait tracking: when Telegram rate-limits a join attempt
    with FloodWaitError, the target is remembered until the wait expires —
    never retried early, never notified again during the wait.
  - Consolidated notifications: one summary per group run, not one per target.
  - Notification deduplication: the same (group, target) failure is never
    reported more than once per _NOTIFY_COOLDOWN seconds.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.services.telegram_service import TelegramUserService

logger = get_logger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

_PRIVATE_INVITE_RE = re.compile(
    r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", re.I
)
_PUBLIC_TGME_RE = re.compile(
    r"t\.me/([a-zA-Z][a-zA-Z0-9_]{3,31})(?:\b|$)", re.I
)
_AT_USERNAME_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})")

# ── Heuristic keywords ────────────────────────────────────────────────────────

_SUBSCRIBE_KEYWORDS = (
    "عضو", "subscribe", "join", "کانال", "channel", "گروه",
    "ابتدا", "اول", "پیوستن", "member", "عضویت", "ارسال",
    "مجاز", "دسترسی", "اجازه", "مشارکت", "باید", "must",
    "required", "الزامی", "ضروری", "ممنوع", "محدود",
)

# ── Tunables ─────────────────────────────────────────────────────────────────

# Seconds to listen for bot messages right after joining a group
_LISTEN_TIMEOUT: int = 30

# Recent messages to scan when ChatWriteForbiddenError is raised
_HISTORY_SCAN_LIMIT: int = 25

# Seconds to wait between consecutive join calls (anti-flood)
_AUTO_JOIN_DELAY: float = 2.0

# Minimum group cooldown (seconds) after any processing run.
# Prevents re-processing the same group faster than this, even on success.
_GROUP_COOLDOWN_MIN: float = 300.0  # 5 minutes

# How long (seconds) before we re-send the same failure notification.
# This is the hard floor — flood_wait durations will extend it further.
_NOTIFY_COOLDOWN: float = 3600.0  # 1 hour


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_urls_from_buttons(message: Any) -> list[str]:
    """Extract all URLs from InlineKeyboard buttons (message.buttons or raw reply_markup)."""
    urls: list[str] = []
    try:
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
    """Extract joinable identifiers (@username / t.me links) from plain text."""
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
    """Extract joinable targets from message text AND InlineKeyboard button URLs."""
    seen: set[str] = set()
    result: list[str] = []

    def _add(t: str) -> None:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)

    # Button URLs first (most reliable for forced-subscribe bots)
    for url in _extract_urls_from_buttons(message):
        for t in _extract_targets_from_text(url):
            _add(t)

    # Then text-embedded links / @mentions
    for t in _extract_targets_from_text(text):
        _add(t)

    return result


def _looks_like_forced_subscribe(text: str, message: Any = None) -> bool:
    """Return True if the message looks like a forced-subscribe restriction notice."""
    text_lower = text.lower()
    if not any(kw in text_lower for kw in _SUBSCRIBE_KEYWORDS):
        return False

    has_text_link = bool(
        _PRIVATE_INVITE_RE.search(text)
        or _PUBLIC_TGME_RE.search(text)
        or _AT_USERNAME_RE.search(text)
    )
    if has_text_link:
        return True

    if message is not None:
        button_urls = _extract_urls_from_buttons(message)
        if any("t.me" in u or "telegram.me" in u for u in button_urls):
            return True

    return False


def _parse_flood_wait_seconds(error: str | None) -> float | None:
    """Parse 'flood_wait:Ns' → N (float). Returns None if not a flood_wait error."""
    if not error or not error.startswith("flood_wait:"):
        return None
    try:
        return float(error.split(":", 1)[1].rstrip("s"))
    except (IndexError, ValueError):
        return None


# ── Service ───────────────────────────────────────────────────────────────────

class ForcedSubscribeService:
    """Singleton service that detects and auto-resolves forced-subscribe bans."""

    _instance: ForcedSubscribeService | None = None

    def __init__(self) -> None:
        self._tg: TelegramUserService | None = None

        # In-flight: group_ids currently being processed (prevent concurrent runs)
        self._handling: set[int] = set()

        # Group-level cooldown: group_id → monotonic expiry timestamp.
        # After a processing run, the group is silently skipped until expiry.
        # Expiry = max(GROUP_COOLDOWN_MIN, min remaining flood_wait of its targets).
        self._group_cooldown: dict[int, float] = {}

        # Target-level flood-wait: normalised_target → monotonic expiry timestamp.
        # Set when join_group() returns flood_wait:Ns; cleared when expiry passes.
        self._flood_until: dict[str, float] = {}

        # Notification dedup: (group_id, normalised_target) → last sent (monotonic).
        # Prevents sending the same failure notification repeatedly.
        self._last_notified: dict[tuple[int, str], float] = {}

    @classmethod
    def get_instance(cls) -> ForcedSubscribeService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: TelegramUserService) -> None:
        self._tg = tg

    # ── Cooldown / flood-wait helpers ─────────────────────────────────────────

    def _group_is_cooling(self, group_id: int) -> bool:
        return time.monotonic() < self._group_cooldown.get(group_id, 0.0)

    def _set_group_cooldown(self, group_id: int, seconds: float) -> None:
        self._group_cooldown[group_id] = time.monotonic() + max(seconds, _GROUP_COOLDOWN_MIN)

    def _target_flood_remaining(self, target: str) -> float:
        """Seconds remaining in flood_wait for *target*. 0 if not rate-limited."""
        expiry = self._flood_until.get(target.lower(), 0.0)
        return max(0.0, expiry - time.monotonic())

    def _set_target_flood(self, target: str, seconds: float) -> None:
        self._flood_until[target.lower()] = time.monotonic() + seconds

    def _should_notify(self, group_id: int, target: str) -> bool:
        key = (group_id, target.lower())
        last = self._last_notified.get(key, 0.0)
        return time.monotonic() - last >= _NOTIFY_COOLDOWN

    def _mark_notified(self, group_id: int, target: str) -> None:
        self._last_notified[(group_id, target.lower())] = time.monotonic()

    # ── Permanent global listener ─────────────────────────────────────────────

    async def process_message(self, event: Any) -> None:
        """
        Permanent Telethon handler registered at startup.
        Detects forced-subscribe restrictions in ANY group message and auto-joins.

        Anti-spam: group cooldown + target flood_wait tracking + notification dedup.
        """
        try:
            # Only act in group/supergroup chats
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

            button_urls = _extract_urls_from_buttons(msg)
            if not text and not button_urls:
                return

            if not _looks_like_forced_subscribe(text, msg):
                return

            targets = _extract_targets(msg, text)
            if not targets:
                return

            group_id: int = event.chat_id

            # ── Guard 1: in-flight (concurrent processing) ────────────────
            if group_id in self._handling:
                return

            # ── Guard 2: group cooldown (recent run) ──────────────────────
            if self._group_is_cooling(group_id):
                logger.debug(
                    "ForcedSubscribe[global]: group %d on cooldown for %.0fs — skipping",
                    group_id,
                    self._group_cooldown.get(group_id, 0.0) - time.monotonic(),
                )
                return

            sender_id = getattr(msg, "sender_id", None)
            logger.info(
                "ForcedSubscribe[global]: restriction in group %d (sender=%s) targets=%s",
                group_id, sender_id, targets,
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

            async def _handler(event: Any) -> None:
                nonlocal auto_joined
                if event.chat_id != group_id:
                    return
                if getattr(event.message, "out", False):
                    return

                msg = event.message
                text: str = (
                    getattr(msg, "text", "")
                    or getattr(msg, "message", "")
                    or ""
                )

                if not _looks_like_forced_subscribe(text, msg):
                    return

                targets = _extract_targets(msg, text)
                if not targets:
                    return

                logger.info(
                    "ForcedSubscribe[check_after_join]: restriction in group %d — targets=%s",
                    group_id, targets,
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
                    "ForcedSubscribe[check_after_join]: no restriction in %ds for group %d",
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
        Called when ChatWriteForbiddenError is raised. Scans recent group history
        for forced-subscribe messages and auto-joins required targets.
        """
        if self._tg is None:
            return []

        # Skip if group is cooling down or already in-flight
        if self._group_is_cooling(group_id) or group_id in self._handling:
            logger.debug(
                "ForcedSubscribe[write-forbidden]: group %d on cooldown/in-flight — skip",
                group_id,
            )
            return []

        auto_joined: list[str] = []
        try:
            detected_targets: list[str] = []

            logger.info(
                "ForcedSubscribe[write-forbidden]: scanning %d messages in group %d (%r)",
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
                logger.debug(
                    "ForcedSubscribe[scan] sender=%s text=%r buttons=%r",
                    getattr(message, "sender_id", None),
                    text[:80],
                    _extract_urls_from_buttons(message),
                )

                if not _looks_like_forced_subscribe(text, message):
                    continue

                targets = _extract_targets(message, text)
                if targets:
                    logger.info(
                        "ForcedSubscribe[write-forbidden]: match in group %d — targets=%s",
                        group_id, targets,
                    )
                    detected_targets.extend(targets)
                    break

            if detected_targets:
                seen: set[str] = set()
                unique = [t for t in detected_targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
                auto_joined = await self._join_targets(unique, group_id, group_title)
            else:
                logger.warning(
                    "ForcedSubscribe[write-forbidden]: no restriction message found in "
                    "last %d messages of group %d. Possibly native Telegram channel "
                    "enforcement (no bot message) — cannot auto-detect target.",
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
        Attempt to join each target channel/group.

        Key behaviours:
        - Targets still in flood_wait are silently skipped (no retry, no spam).
        - A single summary notification is sent per run (not per target).
        - Group cooldown is set after the run to prevent immediate re-trigger.
        """
        if self._tg is None:
            return []

        joined: list[str] = []
        failed: list[tuple[str, str]] = []   # (target, error)
        skipped_flood: list[tuple[str, float]] = []  # (target, remaining_secs)
        min_flood_wait: float = 0.0  # track shortest flood_wait to set group cooldown

        for target in targets:
            # ── Check target flood_wait ───────────────────────────────────
            remaining = self._target_flood_remaining(target)
            if remaining > 0:
                logger.debug(
                    "ForcedSubscribe: skipping %r — flood_wait %.0fs remaining",
                    target, remaining,
                )
                skipped_flood.append((target, remaining))
                if min_flood_wait == 0.0 or remaining < min_flood_wait:
                    min_flood_wait = remaining
                continue

            try:
                logger.info(
                    "ForcedSubscribe: joining %r (required by group %d %r)",
                    target, source_group_id, source_group_title,
                )
                success, _, error = await self._tg.join_group(target)

                if success:
                    logger.info(
                        "ForcedSubscribe: ✅ joined %r (required by group %d)",
                        target, source_group_id,
                    )
                    joined.append(target)
                else:
                    flood_secs = _parse_flood_wait_seconds(error)
                    if flood_secs is not None:
                        logger.warning(
                            "ForcedSubscribe: flood_wait %.0fs for %r — will retry after wait",
                            flood_secs, target,
                        )
                        self._set_target_flood(target, flood_secs)
                        skipped_flood.append((target, flood_secs))
                        if min_flood_wait == 0.0 or flood_secs < min_flood_wait:
                            min_flood_wait = flood_secs
                    else:
                        logger.warning(
                            "ForcedSubscribe: ⚠️ failed to join %r: %s",
                            target, error,
                        )
                        failed.append((target, error or "unknown"))

                await asyncio.sleep(_AUTO_JOIN_DELAY)

            except Exception as exc:
                logger.error(
                    "ForcedSubscribe._join_targets: error for %r: %s", target, exc,
                )
                failed.append((target, str(exc)))

        # ── Set group cooldown ────────────────────────────────────────────
        # If ALL targets are in flood_wait, cool down until the shortest one expires.
        # Otherwise use _GROUP_COOLDOWN_MIN (5 min) so we re-check soon.
        all_flood = len(skipped_flood) == len(targets) and len(joined) == 0 and len(failed) == 0
        if all_flood and min_flood_wait > 0:
            cooldown_secs = min_flood_wait
        else:
            cooldown_secs = _GROUP_COOLDOWN_MIN

        self._set_group_cooldown(source_group_id, cooldown_secs)
        logger.debug(
            "ForcedSubscribe: group %d cooldown set to %.0fs",
            source_group_id, cooldown_secs,
        )

        # ── Send ONE consolidated notification ────────────────────────────
        await self._notify_summary(
            source_group_id, source_group_title,
            joined=joined,
            failed=failed,
            skipped_flood=skipped_flood,
        )

        return joined

    async def _notify_summary(
        self,
        group_id: int,
        group_title: str | None,
        joined: list[str],
        failed: list[tuple[str, str]],
        skipped_flood: list[tuple[str, float]],
    ) -> None:
        """Send a single consolidated notification for one processing run."""
        try:
            # Check if there's anything worth notifying about
            # - Successes: always notify
            # - Failures: notify only if not already notified recently
            notify_failures = [
                (t, e) for t, e in failed
                if self._should_notify(group_id, t)
            ]
            notify_floods = [
                (t, r) for t, r in skipped_flood
                if self._should_notify(group_id, t)
            ]

            if not joined and not notify_failures and not notify_floods:
                logger.debug(
                    "ForcedSubscribe._notify_summary: all notifications suppressed "
                    "(dedup) for group %d", group_id,
                )
                return

            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()

            group_label = (
                f"<b>{group_title}</b>" if group_title
                else f"<code>{group_id}</code>"
            )

            lines: list[str] = [f"🔔 <b>Forced-Subscribe — {group_label}</b>\n"]

            if joined:
                lines.append("✅ <b>عضویت موفق:</b>")
                for t in joined:
                    lines.append(f"  • <code>{t}</code>")
                lines.append("")

            if notify_failures:
                lines.append("❌ <b>عضویت ناموفق:</b>")
                for t, e in notify_failures:
                    lines.append(f"  • <code>{t}</code> — <code>{e}</code>")
                    self._mark_notified(group_id, t)
                lines.append("")

            if notify_floods:
                lines.append("⏳ <b>محدودیت موقت (FloodWait):</b>")
                for t, r in notify_floods:
                    h = int(r // 3600)
                    m = int((r % 3600) // 60)
                    wait_str = f"{h}h {m}m" if h else f"{m}m"
                    lines.append(f"  • <code>{t}</code> — تلاش مجدد در {wait_str}")
                    self._mark_notified(group_id, t)

            await ns.notify("\n".join(lines), parse_mode="HTML")

        except Exception as exc:
            logger.error("ForcedSubscribe._notify_summary error: %s", exc)
