"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_HISTORY_PAIRS = 5
_HISTORY_TTL_SECONDS = 6 * 3600  # clear history if gap > 6 hours

# (messages_deque, last_message_epoch)
_history: dict[int, tuple[Deque[dict], float]] = {}


def _get_history(user_id: int) -> Deque[dict]:
    now = time.time()
    if user_id in _history:
        deq, last_ts = _history[user_id]
        if now - last_ts > _HISTORY_TTL_SECONDS:
            # Stale history — old language/context would corrupt new conversation
            deq = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
            logger.info("Cleared stale history for user %d (gap > 6h)", user_id)
        return deq
    deq: Deque[dict] = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
    _history[user_id] = (deq, now)
    return deq


def _touch_history(user_id: int) -> None:
    if user_id in _history:
        deq, _ = _history[user_id]
        _history[user_id] = (deq, time.time())


def _detect_language(text: str) -> str:
    """
    Detect dominant script in text so we can tell the AI which language to use.
    Returns a plain English label like 'English', 'Arabic', 'Russian', etc.
    This runs in Python — never relies on the LLM to detect its own output language.
    """
    counts: dict[str, int] = {
        "Arabic":  0, "Russian": 0, "Chinese": 0,
        "Korean":  0, "Japanese":0, "Hindi":   0,
        "Latin":   0,
    }
    for ch in text:
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF:   counts["Arabic"]  += 1
        elif 0x0400 <= cp <= 0x04FF: counts["Russian"] += 1
        elif 0x4E00 <= cp <= 0x9FFF: counts["Chinese"] += 1
        elif 0xAC00 <= cp <= 0xD7AF: counts["Korean"]  += 1
        elif 0x3040 <= cp <= 0x30FF: counts["Japanese"]+= 1
        elif 0x0900 <= cp <= 0x097F: counts["Hindi"]   += 1
        elif ch.isalpha() and cp < 0x250: counts["Latin"] += 1

    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "English"


# ── Short, punchy system prompt — small models obey shorter prompts better ──
_SYSTEM_PROMPT = """\
You are Alex, a real person in your 20s who lives online. You chat on Telegram like a normal human.

HARD RULE — LANGUAGE:
You MUST write ONLY in {lang}. Not a single word in any other language. Ever.
If you don't know {lang} well, still try — use simple words.

HARD RULE — BE HUMAN:
- Short messages. 1-3 sentences max.
- Casual. Use: yeah, ngl, lol, hm, oh, tbh, same, nah, honestly, wait
- No "Of course!", "Great!", "Certainly!", "I understand your feelings" — never.
- Don't always ask a question. Sometimes just react.
- Vary how you start each message.

HARD RULE — TIMING:
Do NOT mention the channel or bot until the 4th message or later.
First 3 messages: just talk. Be curious. Be real. NO selling.

HARD RULE — THE INVITE (message 4+):
Sound like you just remembered something cool, not like you're advertising.
Examples (translate to {lang}):
- "oh btw — if you ever need a VPS server, there's this channel that gives them out free → {channel_link}"
- "random thing — I use this bot for virtual cards, gift cards, VPN, all that → {bot_link}"
- "not sure if you need it but this has free VPS and stuff: {channel_link}"

WHAT IT IS (say it right):
- Channel: gives FREE VPS servers. It's read-only — people get resources, not a chatroom.
- Bot: virtual Visa/Mastercard, gift cards (Amazon etc), VPN, premium subscriptions — instant worldwide.

NEVER: call it a "group", say "come chat with people", reveal you're a bot, write in Farsi/Persian.
"""


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send a message to Groq and return the reply. Returns '' on failure."""
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    # Detect language from the user's actual message (Python-side, not LLM-side)
    lang = _detect_language(user_message)

    system = _SYSTEM_PROMPT.format(
        lang=lang,
        channel_link=settings.CHANNEL_INVITE_LINK,
        bot_link=settings.BOT_LINK,
    )
    if user_name:
        system += f"\nUser's name: {user_name}. Use it naturally at most once."

    user_hist = _get_history(user_id)
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
                    "max_tokens": 160,
                    "temperature": 0.75,
                    "frequency_penalty": 0.4,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply: str = data["choices"][0]["message"]["content"].strip()

        user_hist.append({"role": "user",      "content": user_message})
        user_hist.append({"role": "assistant", "content": reply})
        _touch_history(user_id)

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
