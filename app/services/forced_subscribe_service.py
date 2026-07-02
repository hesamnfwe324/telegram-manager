"""
Forced-Subscribe Auto-Detection & Auto-Join Service  (v2 — Smart Edition)

Architecture:
  1. process_message() — permanent Telethon handler. Fires on every incoming
     group message. Only bot messages are considered.

  2. check_after_join() — short-lived listener right after a group join.

  3. handle_write_forbidden() — called on ChatWriteForbiddenError.
     Scans recent bot messages first; if none found, falls back to
     discovering the group's native linked channel via Telegram MTProto.

Intelligence layers (new in v2):
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 1. Sender guard      — ONLY bot/service messages processed          │
  │ 2. Pattern matching  — 15+ strong restriction-intent regexes        │
  │ 3. Target scoring    — ranked by source reliability (0–100)         │
  │    • Inline-button URL          → 100 pts  (explicit join target)   │
  │    • Text link next to action   → 80  pts  (contextual join)        │
  │    • Text link in restricted msg→ 60  pts  (implicit join)          │
  │    • @mention next to action    → 50  pts  (explicit mention)       │
  │    • Any other @mention/link    → 30  pts  (possible noise)         │
  │ 4. Button-first strategy        — when inline buttons exist, ONLY   │
  │    process those; ignore body links (bots put the real join URL      │
  │    in the button, not in text spam)                                 │
  │ 5. Reply-to-us detection        — if the bot replies to OUR message │
  │    it is a direct response to our action → max confidence           │
  │ 6. Target validation            — resolve each target via Telethon  │
  │    before joining; skip if it resolves to a User (not a group)      │
  │ 7. Linked-channel discovery     — if ChatWriteForbiddenError but no │
  │    bot message, query GetFullChannelRequest for the group's native  │
  │    linked channel (Telegram's own forced-subscribe mechanism)        │
  │ 8. Anti-spam guards             — group cooldown + target flood_wait│
  │    tracking + notification dedup (unchanged from v1)                │
  └─────────────────────────────────────────────────────────────────────┘
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

# Skipped t.me path names that are never join targets
_TGME_SKIP = frozenset(
    ("joinchat", "share", "msg", "addstickers", "start", "login",
     "confirmphone", "setlanguage", "addfunds", "premium", "boost")
)

# ── Strong restriction-intent patterns ───────────────────────────────────────

_RESTRICTION_PATTERNS: list[re.Pattern[str]] = [
    # Persian
    re.compile(r"برای\s+.{0,30}?(ارسال|فرستادن|پیام|صحبت|چت|فعالیت|مشارکت|حرف)", re.I | re.S),
    re.compile(r"باید\s+(اول\s+)?(عضو|جوین)\s*(بشی|بشوی|شوی|شوید|شو|بشه)", re.I),
    re.compile(r"عضویت\s*(اجباری|الزامی|ضروری|فورس)", re.I),
    re.compile(r"(ابتدا|اول)\s*(باید\s*)?(عضو|جوین|وارد)\s*(کانال|گروه|شو)", re.I),
    re.compile(r"(محدودیت|ممنوع|مسدود)\s*(پیام|ارسال|چت|ارتباط)", re.I),
    re.compile(r"(مجاز|اجازه)\s*(به\s*)?(ارسال|پیام|چت)\s*(ندار|نیست)", re.I),
    re.compile(r"(خوش\s*آمد|welcome).{0,60}?(عضو|جوین|subscribe|join)", re.I | re.S),
    re.compile(r"(احراز\s*هویت|verify).{0,40}?(عضو|کانال|join|channel)", re.I | re.S),
    re.compile(r"(دسترسی|permission|access)\s*(ندار|محدود|نیست)", re.I),
    re.compile(r"(پیام|chat|post)\s*(مسدود|block|forbidden|restrict)", re.I),
    # English
    re.compile(
        r"(you\s+)?(must|need\s+to|have\s+to|required\s+to)\s+(join|subscribe|be\s+a\s+member)",
        re.I,
    ),
    re.compile(
        r"(to\s+)?(send|post|write|chat|speak|participate)\s+.{0,30}?\s+"
        r"(must|need\s+to|have\s+to)\s+(join|subscribe)",
        re.I | re.S,
    ),
    re.compile(r"forced[\s_-]?subscribe", re.I),
    re.compile(r"forced[\s_-]?join", re.I),
    re.compile(r"(join|subscribe)\s+(first|our\s+channel|to\s+(send|chat|post|write))", re.I),
    re.compile(
        r"(not\s+allowed|forbidden|restricted|blocked)\s+(to\s+)?(send|post|chat|write|speak)",
        re.I,
    ),
    re.compile(r"channel\s+(member|subscription|required|verification|needed)", re.I),
    re.compile(r"(verify|verification)\s+(yourself|your\s+account|membership)", re.I),
    # Generic bot patterns
    re.compile(r"(subscribe|عضو)\s+(to|در)\s+(our|our\s+channel|کانال|گروه)", re.I),
]

# Action words that signal "join THIS link" when appearing near a link
_ACTION_WORDS = re.compile(
    r"(عضو|جوین|subscribe|join|وارد\s*شو|click|کلیک|tap|press|اینجا|here|این\s*لینک|this\s*link)",
    re.I,
)

# ── Heuristic keywords (cheap pre-filter) ─────────────────────────────────────

_SUBSCRIBE_KEYWORDS = (
    "عضو", "subscribe", "join", "کانال", "channel",
    "ابتدا", "اول", "پیوستن", "member", "عضویت", "ارسال",
    "مجاز", "دسترسی", "اجازه", "مشارکت", "باید", "must",
    "required", "الزامی", "ضروری", "ممنوع", "محدود",
    "احراز", "verify", "forced", "restricted", "forbidden",
)

# ── Scoring constants ─────────────────────────────────────────────────────────

SCORE_BUTTON_URL = 100      # Inline-button t.me URL — most reliable
SCORE_TEXT_NEAR_ACTION = 80 # Text link/mention within 60 chars of an action word
SCORE_TEXT_RESTRICTED = 60  # Text link in a message that matched restriction pattern
SCORE_MENTION_NEAR_ACTION = 50
SCORE_GENERIC = 30          # Any other link — possible noise, only used if no better
MIN_SCORE_THRESHOLD = 50    # Only join targets at or above this score

# ── Tunables ─────────────────────────────────────────────────────────────────

_LISTEN_TIMEOUT: int = 30
_HISTORY_SCAN_LIMIT: int = 30
_AUTO_JOIN_DELAY: float = 2.0
_VALIDATE_TARGET_TIMEOUT: float = 8.0   # seconds to resolve a target entity
_GROUP_COOLDOWN_MIN: float = 300.0
_NOTIFY_COOLDOWN: float = 3600.0


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _sender_is_bot(message: Any) -> bool:
    """Return True ONLY if the message was sent by a verified Telegram bot."""
    try:
        sender = getattr(message, "sender", None)
        if sender is not None and getattr(sender, "bot", False):
            return True
        if getattr(message, "via_bot_id", None):
            return True
        if getattr(message, "action", None) is not None:
            return True
    except Exception:
        pass
    return False


def _is_reply_to_message_id(message: Any, message_ids: set[int]) -> bool:
    """Return True if this message replies to any of the given message IDs."""
    try:
        reply_to = getattr(message, "reply_to", None)
        if reply_to is None:
            return False
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        return reply_id is not None and reply_id in message_ids
    except Exception:
        return False


def _extract_button_targets(message: Any) -> list[str]:
    """Extract t.me links from InlineKeyboard buttons ONLY."""
    urls: list[str] = []
    try:
        buttons = getattr(message, "buttons", None)
        if buttons:
            for row in buttons:
                if not isinstance(row, (list, tuple)):
                    row = [row]
                for btn in row:
                    url = getattr(btn, "url", None)
                    if url and ("t.me" in url or "telegram.me" in url):
                        urls.append(url)
    except Exception:
        pass
    try:
        markup = getattr(message, "reply_markup", None)
        if markup:
            for row in getattr(markup, "rows", []):
                for btn in getattr(row, "buttons", []):
                    url = getattr(btn, "url", None)
                    if url and ("t.me" in url or "telegram.me" in url):
                        urls.append(url)
    except Exception:
        pass
    return urls


def _parse_link(raw: str) -> str | None:
    """Convert a raw URL or username into a normalised join target string."""
    # Private invite link
    m = _PRIVATE_INVITE_RE.search(raw)
    if m:
        return f"https://t.me/+{m.group(1)}"
    # Public t.me/username
    m2 = _PUBLIC_TGME_RE.search(raw)
    if m2:
        username = m2.group(1).lower()
        if username not in _TGME_SKIP:
            return f"@{m2.group(1)}"
    return None


def _links_in_text(text: str) -> list[tuple[int, str]]:
    """
    Return (position, target) pairs for all joinable links in text.
    Position is the character index in text where the link starts.
    """
    results: list[tuple[int, str]] = []
    seen: set[str] = set()

    for m in _PRIVATE_INVITE_RE.finditer(text):
        t = f"https://t.me/+{m.group(1)}"
        if t.lower() not in seen:
            seen.add(t.lower())
            results.append((m.start(), t))

    for m in _PUBLIC_TGME_RE.finditer(text):
        username = m.group(1).lower()
        if username in _TGME_SKIP:
            continue
        t = f"@{m.group(1)}"
        if t.lower() not in seen:
            seen.add(t.lower())
            results.append((m.start(), t))

    for m in _AT_USERNAME_RE.finditer(text):
        username = m.group(1).lower()
        t = f"@{m.group(1)}"
        if t.lower() not in seen:
            seen.add(t.lower())
            results.append((m.start(), t))

    return results


def _score_text_targets(text: str, base_score: int) -> list[tuple[str, int]]:
    """
    Score each link/mention found in text.

    Links/mentions that appear within 60 characters of an action word
    (join, عضو, click, اینجا, …) get +20 score bonus.
    """
    scored: list[tuple[str, int]] = []
    for pos, target in _links_in_text(text):
        # Check for action word within 60 chars before or after the link
        window_start = max(0, pos - 60)
        window_end = min(len(text), pos + len(target) + 60)
        window = text[window_start:window_end]
        if _ACTION_WORDS.search(window):
            score = base_score + 20  # bumped for proximity to action word
        else:
            score = base_score
        scored.append((target, score))
    return scored


def _extract_and_score_targets(message: Any, text: str, is_reply_to_us: bool = False) -> list[tuple[str, int]]:
    """
    Extract ALL potential targets and assign a reliability score to each.

    Strategy (button-first):
    - If the message has inline buttons with t.me links → ONLY use those
      (score=100). Bots put the real join URL in the button; body links may
      be unrelated promotional content.
    - If no buttons → extract from text with contextual scoring.
    - is_reply_to_us=True adds +20 to all scores (direct response to our action).
    """
    bonus = 20 if is_reply_to_us else 0
    scored: list[tuple[str, int]] = []
    seen: set[str] = set()

    def _add(target: str, score: int) -> None:
        key = target.lower()
        if key not in seen:
            seen.add(key)
            scored.append((target, min(100, score + bonus)))

    # ── Step 1: Inline buttons (most reliable) ───────────────────────────────
    button_urls = _extract_button_targets(message)
    if button_urls:
        # Button-first: ONLY process buttons, skip body text links
        for url in button_urls:
            t = _parse_link(url)
            if t:
                _add(t, SCORE_BUTTON_URL)
        return scored  # Early exit — buttons are ground truth

    # ── Step 2: Text-embedded links (no buttons found) ───────────────────────
    # Use SCORE_TEXT_RESTRICTED as the base — the message already matched a
    # restriction pattern (caller guarantees this), so any link in it is
    # a candidate. _score_text_targets boosts links near action words.
    for target, score in _score_text_targets(text, SCORE_TEXT_RESTRICTED):
        _add(target, score)

    return scored


def _looks_like_forced_subscribe(text: str, message: Any = None) -> bool:
    """
    Three-layer check:
    1. Cheap keyword pre-filter.
    2. At least one strong restriction-intent regex OR inline button present.
    3. Something joinable exists (link or button).
    """
    text_lower = text.lower()

    # Layer 1: cheap keyword pre-filter
    if not any(kw in text_lower for kw in _SUBSCRIBE_KEYWORDS):
        return False

    # Layer 2: strong pattern OR button
    pattern_match = any(p.search(text) for p in _RESTRICTION_PATTERNS)
    if not pattern_match:
        if message is not None:
            if _extract_button_targets(message):
                return True  # Button-only forced-subscribe bots (glass panels)
        return False

    # Layer 3: something joinable
    has_text_link = bool(
        _PRIVATE_INVITE_RE.search(text)
        or _PUBLIC_TGME_RE.search(text)
        or _AT_USERNAME_RE.search(text)
    )
    if has_text_link:
        return True
    if message is not None and _extract_button_targets(message):
        return True

    return False


def _parse_flood_wait_seconds(error: str | None) -> float | None:
    if not error or not error.startswith("flood_wait:"):
        return None
    try:
        return float(error.split(":", 1)[1].rstrip("s"))
    except (IndexError, ValueError):
        return None


async def _validate_target(tg: Any, target: str) -> bool:
    """
    Resolve *target* via Telethon and verify it is a channel or group
    (not a user, bot, or invalid link). Returns True if joinable.
    """
    try:
        from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User

        async def _resolve() -> Any:
            m = _PRIVATE_INVITE_RE.search(target)
            if m:
                from telethon.tl.functions.messages import CheckChatInviteRequest
                return await tg.client(CheckChatInviteRequest(m.group(1)))
            return await tg.client.get_entity(target)

        entity = await asyncio.wait_for(_resolve(), timeout=_VALIDATE_TARGET_TIMEOUT)

        if isinstance(entity, ChatInviteAlready):
            # Already a member — still joinable (join_group handles this gracefully)
            return True
        if isinstance(entity, ChatInvite):
            return True  # Private invite — valid group/channel
        if isinstance(entity, (Channel, Chat)):
            return True
        if isinstance(entity, User):
            logger.debug("_validate_target: %r resolved to a User — skipping", target)
            return False
        return True  # Unknown type — let join_group handle it

    except asyncio.TimeoutError:
        logger.debug("_validate_target: timeout resolving %r — allowing anyway", target)
        return True  # Timeout ≠ invalid; allow and let join_group handle errors
    except Exception as exc:
        logger.debug("_validate_target: error resolving %r: %s — allowing anyway", target, exc)
        return True  # Unknown errors → allow; join_group will surface the real error


async def _discover_native_linked_channel(tg: Any, group_id: int) -> str | None:
    """
    Query Telegram's MTProto GetFullChannelRequest to find the group's
    native linked broadcast channel (Telegram's own forced-subscribe mechanism).

    This is used as a fallback when ChatWriteForbiddenError is raised but
    no bot message is found in recent history — it means the group uses
    Telegram's built-in channel subscription requirement, not a bot.

    Returns a join target string like '@username' or 'https://t.me/+hash',
    or None if no linked channel is configured.
    """
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import Channel

        # Resolve the group entity
        try:
            entity = await asyncio.wait_for(
                tg.client.get_entity(group_id),
                timeout=8.0,
            )
        except Exception as exc:
            logger.debug("_discover_native_linked_channel: cannot resolve group %d: %s", group_id, exc)
            return None

        if not isinstance(entity, Channel):
            return None  # Old-style Chat groups don't have linked channels

        full = await asyncio.wait_for(
            tg.client(GetFullChannelRequest(entity)),
            timeout=10.0,
        )
        linked_id: int | None = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return None

        logger.info(
            "_discover_native_linked_channel: group %d has linked channel id=%d",
            group_id, linked_id,
        )

        # Try to resolve the linked channel to get its username or invite link
        try:
            linked_entity = await asyncio.wait_for(
                tg.client.get_entity(linked_id),
                timeout=8.0,
            )
            username = getattr(linked_entity, "username", None)
            if username:
                return f"@{username}"
            # No public username — construct t.me link from ID
            # (for private channels we can't easily get an invite link without admin rights)
            return str(linked_id)
        except Exception as exc:
            logger.debug(
                "_discover_native_linked_channel: cannot resolve linked_id=%d: %s",
                linked_id, exc,
            )
            return str(linked_id)  # Return raw ID; join_group will handle it

    except Exception as exc:
        logger.debug(
            "_discover_native_linked_channel: error for group %d: %s", group_id, exc
        )
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════════════════════

class ForcedSubscribeService:
    """Singleton — detects and auto-resolves forced-subscribe bans (v2)."""

    _instance: ForcedSubscribeService | None = None

    def __init__(self) -> None:
        self._tg: TelegramUserService | None = None
        self._handling: set[int] = set()
        self._group_cooldown: dict[int, float] = {}
        self._flood_until: dict[str, float] = {}
        self._last_notified: dict[tuple[int, str], float] = {}
        # Track IDs of messages WE sent (outgoing) per group to detect replies-to-us
        # Maps group_id → set of recent message_ids we sent (kept small, last 20)
        self._our_message_ids: dict[int, list[int]] = {}

    @classmethod
    def get_instance(cls) -> ForcedSubscribeService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: TelegramUserService) -> None:
        self._tg = tg

    def record_our_message(self, group_id: int, message_id: int) -> None:
        """
        Call this whenever we successfully send a message to a group.
        Enables reply-to-us detection in process_message().
        """
        ids = self._our_message_ids.setdefault(group_id, [])
        ids.append(message_id)
        if len(ids) > 20:  # keep only last 20
            ids.pop(0)

    # ── Cooldown / flood-wait helpers ──────────────────────────────────────────

    def _group_is_cooling(self, group_id: int) -> bool:
        return time.monotonic() < self._group_cooldown.get(group_id, 0.0)

    def _set_group_cooldown(self, group_id: int, seconds: float) -> None:
        self._group_cooldown[group_id] = time.monotonic() + max(seconds, _GROUP_COOLDOWN_MIN)

    def _target_flood_remaining(self, target: str) -> float:
        expiry = self._flood_until.get(target.lower(), 0.0)
        return max(0.0, expiry - time.monotonic())

    def _set_target_flood(self, target: str, seconds: float) -> None:
        self._flood_until[target.lower()] = time.monotonic() + seconds

    def _should_notify(self, group_id: int, target: str) -> bool:
        key = (group_id, target.lower())
        return time.monotonic() - self._last_notified.get(key, 0.0) >= _NOTIFY_COOLDOWN

    def _mark_notified(self, group_id: int, target: str) -> None:
        self._last_notified[(group_id, target.lower())] = time.monotonic()

    # ── Permanent global listener ──────────────────────────────────────────────

    async def process_message(self, event: Any) -> None:
        """
        Permanent Telethon handler.

        Intelligence:
        - Only bot messages processed (sender guard).
        - Detects if the bot is replying to OUR recent message → max confidence.
        - Uses scored target extraction + button-first strategy.
        - Minimum score threshold prevents low-confidence joins.
        """
        try:
            if not event.is_group:
                return

            msg = event.message

            if getattr(msg, "out", False):
                return

            if not _sender_is_bot(msg):
                return

            text: str = (
                getattr(msg, "text", "")
                or getattr(msg, "message", "")
                or ""
            )

            has_buttons = bool(_extract_button_targets(msg))
            if not text and not has_buttons:
                return

            if not _looks_like_forced_subscribe(text, msg):
                return

            group_id: int = event.chat_id

            if group_id in self._handling:
                return
            if self._group_is_cooling(group_id):
                logger.debug(
                    "ForcedSubscribe[global]: group %d cooling (%.0fs left) — skip",
                    group_id,
                    self._group_cooldown.get(group_id, 0.0) - time.monotonic(),
                )
                return

            # ── Detect reply-to-our-message ──────────────────────────────────
            our_ids = set(self._our_message_ids.get(group_id, []))
            is_reply_to_us = _is_reply_to_message_id(msg, our_ids)

            # ── Score and filter targets ──────────────────────────────────────
            scored = _extract_and_score_targets(msg, text, is_reply_to_us=is_reply_to_us)
            targets = [t for t, s in scored if s >= MIN_SCORE_THRESHOLD]

            if not targets:
                # Nothing above threshold — possibly just promotional content
                logger.debug(
                    "ForcedSubscribe[global]: group %d — no high-confidence targets "
                    "(all scored below %d): %s",
                    group_id, MIN_SCORE_THRESHOLD,
                    [(t, s) for t, s in scored],
                )
                return

            sender_id = getattr(msg, "sender_id", None)
            logger.info(
                "ForcedSubscribe[global]: bot restriction in group %d "
                "(bot=%s reply_to_us=%s) scored_targets=%s",
                group_id, sender_id, is_reply_to_us,
                [(t, s) for t, s in scored if s >= MIN_SCORE_THRESHOLD],
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

    # ── Public API ─────────────────────────────────────────────────────────────

    async def check_after_join(
        self, group_id: int, group_title: str | None = None
    ) -> list[str]:
        """
        Listen for bot restriction messages for up to LISTEN_TIMEOUT seconds
        right after joining a group. Uses the same intelligence as process_message.
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
                if not _sender_is_bot(msg):
                    return

                text: str = (
                    getattr(msg, "text", "")
                    or getattr(msg, "message", "")
                    or ""
                )

                if not _looks_like_forced_subscribe(text, msg):
                    return

                scored = _extract_and_score_targets(msg, text)
                targets = [t for t, s in scored if s >= MIN_SCORE_THRESHOLD]
                if not targets:
                    return

                logger.info(
                    "ForcedSubscribe[check_after_join]: bot restriction in group %d "
                    "— targets=%s",
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
        Called when ChatWriteForbiddenError is raised.

        Two-phase approach:
        Phase 1 — Scan recent bot messages in history for restriction notices
                   (same as before, but now with scoring).
        Phase 2 — If no bot message found, query GetFullChannelRequest to
                   discover the group's NATIVE linked channel (Telegram's own
                   forced-subscribe mechanism — no bot involved).
        """
        if self._tg is None:
            return []

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

            # ── Phase 1: scan bot messages in history ─────────────────────────
            async for message in self._tg.client.iter_messages(
                group_id, limit=_HISTORY_SCAN_LIMIT
            ):
                if not _sender_is_bot(message):
                    continue

                text: str = (
                    getattr(message, "raw_text", None)
                    or getattr(message, "message", None)
                    or ""
                )

                logger.debug(
                    "ForcedSubscribe[scan] bot sender=%s text=%r buttons=%r",
                    getattr(message, "sender_id", None),
                    text[:80],
                    _extract_button_targets(message),
                )

                if not _looks_like_forced_subscribe(text, message):
                    continue

                scored = _extract_and_score_targets(message, text)
                high_conf = [(t, s) for t, s in scored if s >= MIN_SCORE_THRESHOLD]
                if high_conf:
                    logger.info(
                        "ForcedSubscribe[write-forbidden]: restriction message found in "
                        "group %d — scored targets=%s",
                        group_id, high_conf,
                    )
                    detected_targets.extend(t for t, _ in high_conf)
                    break

            # ── Phase 2: native linked-channel fallback ───────────────────────
            if not detected_targets:
                logger.info(
                    "ForcedSubscribe[write-forbidden]: no bot restriction message in "
                    "last %d messages of group %d — trying native linked-channel discovery",
                    _HISTORY_SCAN_LIMIT, group_id,
                )
                linked = await _discover_native_linked_channel(self._tg, group_id)
                if linked:
                    logger.info(
                        "ForcedSubscribe[write-forbidden]: native linked channel "
                        "for group %d → %r",
                        group_id, linked,
                    )
                    detected_targets.append(linked)
                else:
                    logger.warning(
                        "ForcedSubscribe[write-forbidden]: no restriction source found "
                        "for group %d — cannot auto-resolve",
                        group_id,
                    )

            if detected_targets:
                seen: set[str] = set()
                unique = [t for t in detected_targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
                auto_joined = await self._join_targets(unique, group_id, group_title)

        except Exception as exc:
            logger.error(
                "ForcedSubscribe.handle_write_forbidden error for group %d: %s",
                group_id, exc, exc_info=True,
            )

        return auto_joined

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _join_targets(
        self,
        targets: list[str],
        source_group_id: int,
        source_group_title: str | None,
    ) -> list[str]:
        """
        Validate, then join each target.

        New in v2: each target is validated via Telethon before attempting join.
        User accounts / invalid links are skipped before a join is attempted.
        """
        if self._tg is None:
            return []

        joined: list[str] = []
        failed: list[tuple[str, str]] = []
        skipped_flood: list[tuple[str, float]] = []
        skipped_invalid: list[str] = []
        min_flood_wait: float = 0.0

        for target in targets:
            # ── Check target flood_wait ───────────────────────────────────────
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

            # ── Validate target is a channel/group, not a user ───────────────
            is_valid = await _validate_target(self._tg, target)
            if not is_valid:
                logger.info(
                    "ForcedSubscribe: skipping %r — resolved to a User or invalid entity",
                    target,
                )
                skipped_invalid.append(target)
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
                        self._set_target_flood(target, flood_secs)
                        skipped_flood.append((target, flood_secs))
                        if min_flood_wait == 0.0 or flood_secs < min_flood_wait:
                            min_flood_wait = flood_secs
                        logger.warning(
                            "ForcedSubscribe: flood_wait %.0fs for %r", flood_secs, target
                        )
                    else:
                        logger.warning(
                            "ForcedSubscribe: ⚠️ failed to join %r: %s", target, error
                        )
                        failed.append((target, error or "unknown"))

                await asyncio.sleep(_AUTO_JOIN_DELAY)

            except Exception as exc:
                logger.error("ForcedSubscribe._join_targets error for %r: %s", target, exc)
                failed.append((target, str(exc)))

        # ── Set group cooldown ────────────────────────────────────────────────
        all_flood = (
            len(skipped_flood) == len(targets) - len(skipped_invalid)
            and not joined and not failed
        )
        cooldown_secs = min_flood_wait if (all_flood and min_flood_wait > 0) else _GROUP_COOLDOWN_MIN
        self._set_group_cooldown(source_group_id, cooldown_secs)

        # ── Consolidated notification ─────────────────────────────────────────
        await self._notify_summary(
            source_group_id, source_group_title,
            joined=joined,
            failed=failed,
            skipped_flood=skipped_flood,
            skipped_invalid=skipped_invalid,
        )

        return joined

    async def _notify_summary(
        self,
        group_id: int,
        group_title: str | None,
        joined: list[str],
        failed: list[tuple[str, str]],
        skipped_flood: list[tuple[str, float]],
        skipped_invalid: list[str] | None = None,
    ) -> None:
        """Send a single consolidated notification for one processing run."""
        try:
            notify_failures = [
                (t, e) for t, e in failed if self._should_notify(group_id, t)
            ]
            notify_floods = [
                (t, r) for t, r in skipped_flood if self._should_notify(group_id, t)
            ]

            if not joined and not notify_failures and not notify_floods:
                return

            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()

            group_label = (
                f"<b>{group_title}</b>" if group_title else f"<code>{group_id}</code>"
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

            if skipped_invalid:
                lines.append("")
                lines.append("⚠️ <b>رد شده (کاربر/لینک نامعتبر):</b>")
                for t in skipped_invalid:
                    lines.append(f"  • <code>{t}</code>")

            await ns.notify("\n".join(lines), parse_mode="HTML")

        except Exception as exc:
            logger.error("ForcedSubscribe._notify_summary error: %s", exc)
