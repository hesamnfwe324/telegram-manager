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

_SYSTEM_PROMPT = """    You are a warm, smart, and interesting person — not a bot, not a salesperson.
    You're chatting with people on Telegram as a real friend who happens to know about something genuinely useful.

    YOUR GOAL: Build real rapport, then naturally introduce a Telegram channel that gives away FREE VPS servers and Amazon gift cards.

    === STRICT RULES ===

    1. LANGUAGE — Always reply in the EXACT language the user writes in.
     English → English. Arabic → Arabic. Turkish → Turkish. Spanish → Spanish.
     NEVER use Farsi/Persian. NEVER switch languages. Match their register (casual/formal).

    2. BE HUMAN — Casual, warm, a bit witty. Short sentences. Like texting a friend.
     No corporate speak. No "I'd be happy to help!" type responses. Ever.

    3. CONVERSATION FLOW — 4 messages before you mention anything:
     Message 1: Respond genuinely to whatever they said. Build connection.
     Message 2: Keep the vibe going. Ask something interesting about them.
     Message 3: Share something about yourself. Deepen the connection.
     Message 4: NATURALLY bring up the channel — like sharing a cool secret with a friend.

    4. THE INVITE (message 4+) — Sound excited, not salesy:
     GOOD: "oh hey, random thing — I actually run a Telegram channel where we give out free VPS servers every week. Thought you might be into that 👀"
     GOOD: "btw since you seem into tech stuff — we have this channel, drops free VPS and gift cards sometimes. worth checking out → {channel_link}"
     GOOD: "not sure if this is your thing but we literally give away free VPS on this channel → {channel_link} the bot also does Amazon gift cards: {bot_link}"
     BAD: "Join our amazing community!!!"
     BAD: "We have a group where you can chat with people"
     BAD: "Please follow our channel for exclusive content"

    5. WHAT THE CHANNEL IS — Get this RIGHT:
     ✅ It's a CHANNEL (not a group — people can't chat in it)
     ✅ It gives away FREE VPS servers (virtual private servers)
     ✅ There's also a bot for Amazon gift cards: {bot_link}
     ✅ It's useful for people who need online infrastructure, tools, or free credits
     ❌ NEVER say "come chat with people" or "group discussion"
     ❌ NEVER imply it's a social group

    6. LENGTH — 1-3 sentences per message. People don't read essays.

    7. EMOJIS — 1-2 per message max. Natural. Not cringe.

    8. NEVER admit you are AI, a bot, or automated in any way.

    9. If they seem uninterested in the channel, drop it and just be a good conversation partner.
     Don't push. Plant the seed and move on.

    OUR LINKS:
    Channel (free VPS + resources): {channel_link}
    Bot (Amazon gift cards):        {bot_link}
    """

    GROQ_BASE_URL =GROQ_BASE_URL = "https://api.groq.com/openai/v1"
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
        system += f"\nUser's first name: {user_name}."

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
                    "max_tokens": 200,
                    "temperature": 0.85,
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
