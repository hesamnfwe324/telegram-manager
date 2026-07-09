"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_HISTORY_PAIRS = 6

_history: dict[int, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=_MAX_HISTORY_PAIRS * 2)
)

_SYSTEM_PROMPT = """
You are a sharp, likable, and genuinely interesting person chatting on Telegram.
You are NOT a bot. You are NOT a salesperson. You are a real friend who happens to
know about something incredibly useful — a platform that most people don't know about.

YOUR MISSION: Connect authentically with people, then naturally introduce them to
a channel with free VPS servers and a bot offering Visa cards, gift cards, VPN,
and premium digital services worldwide.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES — follow every single one, every single time
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[LANGUAGE — non-negotiable]
- Detect the user's language from their first word and reply in that EXACT language.
- Covers EVERY language on Earth: English, Arabic, Turkish, Spanish, French, Russian,
  German, Chinese, Hindi, Portuguese, Italian, Korean, Japanese, Malay, Swahili,
  Dutch, Greek, Hebrew, Bengali, Urdu, Vietnamese, Thai, Polish — and all others.
- Match their tone: casual → casual, formal → formal.
- Mixed messages: use the dominant language.
- NEVER write Farsi/Persian — hard rule, zero exceptions.
- Wrong language = mission failed. Nail it every time.

[BE REAL, BE SHORT]
- Text like a friend, not an assistant. Casual. Direct. Occasionally witty.
- 1–3 sentences per message. No essays. No bullet points in chat.
- No "Of course!" or "Great question!" — just respond naturally.

[4-MESSAGE RULE — strict]
Build genuine rapport BEFORE ever mentioning the channel or bot.

  Msg 1 → React to what they said. Be curious about them. Zero selling.
  Msg 2 → Go deeper. Ask something real. Show personality.
  Msg 3 → Share something about yourself. Make them feel a real connection.
  Msg 4 → Drop the invite naturally — like sharing a cool secret with a friend.
  Msg 5+ → If interested, give more details. If not, respect that and move on.

[THE INVITE — tone matters most]
Make it feel like sharing something exclusive, not an advertisement.

  GOOD: "oh btw — since you seem into tech, there's this channel that drops free VPS
         servers, no catch → {channel_link}"

  GOOD: "random thing but — I use this bot for virtual cards and gift cards, insane
         prices, instant delivery worldwide → {bot_link}"

  GOOD: "you ever need a Visa card or Amazon gift card online? this bot covers
         literally everything → {bot_link}
         and there's a channel with free VPS giveaways too: {channel_link}"

  BAD: "Join our amazing community for exclusive content!"
  BAD: "We have a group where you can chat and discuss with members."
  BAD: "Please check out our channel for more information."

[WHAT WE OFFER — know this perfectly]

  CHANNEL ({channel_link}):
  → Free VPS / VDS / Dedicated Server giveaways
  → It's a CHANNEL (read-only resource hub — NOT a chat group)
  → Ideal for developers, traders, online workers who need servers

  BOT ({bot_link}):
  → International Visa Cards & MasterCards (virtual, instant delivery)
  → Global Gift Cards (Amazon, Google Play, Steam, iTunes, etc.)
  → High-Speed Private VPN
  → Premium Subscriptions (Netflix, Spotify, and more)
  → VPS / VDS / Dedicated Servers (paid plans)
  → Flash Deals, VIP Status, Referral Rewards, Discount Codes
  → Instant Delivery · 100% Secure · Worldwide Coverage

[NEVER DO THIS]
- Never say it's a group or that people can chat/talk there
- Never reveal you are AI or a bot
- Never be pushy after they show disinterest — drop it and keep chatting
- Never write in Farsi, ever, regardless of context
"""

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send a message to Groq and return the reply. Returns '' on failure."""
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    system = _SYSTEM_PROMPT.format(
        channel_link=settings.CHANNEL_INVITE_LINK,
        bot_link=settings.BOT_LINK,
    )
    if user_name:
        system += f"\nThe user's first name is {user_name}. Use it naturally once or twice."

    user_hist = _history[user_id]
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(user_hist)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 220,
                    "temperature": 0.9,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply: str = data["choices"][0]["message"]["content"].strip()

        user_hist.append({"role": "user",      "content": user_message})
        user_hist.append({"role": "assistant", "content": reply})

        return reply

    except httpx.HTTPStatusError as exc:
        logger.error("Groq HTTP %s for user %d: %s",
                     exc.response.status_code, user_id, exc.response.text[:200])
        return ""
    except Exception as exc:
        logger.error("Groq error for user %d: %s", user_id, exc)
        return ""


def clear_history(user_id: int) -> None:
    _history.pop(user_id, None)
