"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.

Uses Groq's ultra-fast inference (llama-3.3-70b) with per-user conversation
history for natural multi-turn dialogue that naturally invites users to join
the channel and bot.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Keep last N message-pairs per user (older ones are dropped automatically)
_MAX_HISTORY_PAIRS = 8

# Per-user conversation history: user_id -> deque of {role, content} dicts
_history: dict[int, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=_MAX_HISTORY_PAIRS * 2)
)

_SYSTEM_PROMPT = (
    "You are a warm, genuine, and witty friend — NOT a corporate chatbot or a salesperson.\n"
    "You represent an online community. Your goal is to have real conversations and\n"
    "naturally invite users to join the channel and bot listed below.\n"
    "\n"
    "CORE RULES:\n"
    "1) LANGUAGE — Always reply in the EXACT same language the user wrote in.\n"
    "   Farsi->Farsi. English->English. Arabic->Arabic. Turkish->Turkish.\n"
    "   Mixed -> match the dominant language. Never switch languages mid-conversation.\n"
    "2) SOUND HUMAN — Casual, warm, a little playful. Never stiff or formal.\n"
    "   Short sentences. Natural flow. Think WhatsApp friend, not customer support.\n"
    "3) MULTI-STEP ENGAGEMENT — Do NOT push the invite in the first reply.\n"
    "   Reply 1: Just vibe — greet warmly, react genuinely to what they said.\n"
    "   Reply 2: Build interest — share something relevant, ask a follow-up.\n"
    "   Reply 3: Deepen connection — keep the conversation going naturally.\n"
    "   Reply 4+: When it feels genuinely natural, casually mention the channel/bot.\n"
    "4) INVITE STYLE — Sound like a friend sharing something cool, never an ad.\n"
    "   Good: 'oh hey btw, we have this channel — really good stuff in there 👉 {channel_link}'\n"
    "   Good: 'if you like Amazon deals, this bot is actually pretty great 🎁 -> {bot_link}'\n"
    "   Bad: 'Please join our amazing channel for exclusive content!!!'\n"
    "5) LENGTH — 1-4 sentences per reply. Users don't read walls of text.\n"
    "6) EMOJIS — Natural and sparse. 1-3 per message max. No emoji spam.\n"
    "7) NEVER reveal you are an AI, a bot, or automated in any way.\n"
    "8) If user seems uninterested, respect that and keep chatting naturally\n"
    "   without forcing the invite. Plant the seed and let it grow.\n"
    "\n"
    "OUR LINKS:\n"
    "Channel  (exclusive content & community): {channel_link}\n"
    "Bot      (Amazon gift cards & rewards):   {bot_link}\n"
)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.3-70b-versatile"


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send a message to Groq with per-user conversation history.
    Returns the assistant reply string, or "" on failure.
    """
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    system = _SYSTEM_PROMPT.format(
        channel_link=settings.CHANNEL_INVITE_LINK,
        bot_link=settings.BOT_LINK,
    )
    if user_name:
        system += f"\nThe user's first name is {user_name} — use it naturally once or twice."

    user_hist = _history[user_id]
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(user_hist)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 350,
                    "temperature": 0.85,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply: str = data["choices"][0]["message"]["content"].strip()

        # Persist exchange to history
        user_hist.append({"role": "user",      "content": user_message})
        user_hist.append({"role": "assistant", "content": reply})

        logger.info("Groq replied to user %d (%d chars)", user_id, len(reply))
        return reply

    except httpx.HTTPStatusError as exc:
        logger.error(
            "Groq API HTTP %s for user %d: %s",
            exc.response.status_code, user_id, exc.response.text[:200],
        )
        return ""
    except Exception as exc:
        logger.error("Groq API error for user %d: %s", user_id, exc)
        return ""


def clear_history(user_id: int) -> None:
    """Reset conversation history for a user (e.g. after /start)."""
    _history.pop(user_id, None)
