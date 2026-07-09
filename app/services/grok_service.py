"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.
Supports 8 major world languages with automatic detection and reply threading.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Deque

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_HISTORY_PAIRS = 5
_HISTORY_TTL_SECONDS = 6 * 3600  # clear history if gap > 6 hours

# (messages_deque, last_message_epoch, total_message_count)
_history: dict[int, tuple[Deque[dict], float, int]] = {}


def _get_history(user_id: int) -> tuple[Deque[dict], int]:
    now = time.time()
    if user_id in _history:
        deq, last_ts, count = _history[user_id]
        if now - last_ts > _HISTORY_TTL_SECONDS:
            deq = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
            count = 0
            logger.info("Cleared stale history for user %d (gap > 6h)", user_id)
        return deq, count
    deq: Deque[dict] = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
    _history[user_id] = (deq, now, 0)
    return deq, 0


def _touch_history(user_id: int, new_count: int) -> None:
    if user_id in _history:
        deq, _, _ = _history[user_id]
        _history[user_id] = (deq, time.time(), new_count)


def _detect_language(text: str) -> str:
    """
    Detect dominant language in text.
    Supports 8 major world languages:
      Arabic, Russian, Chinese, Korean, Japanese, Hindi (script-based)
      + English, Spanish, French, Portuguese, German, Turkish (word-pattern for Latin)
    Returns a plain English label like 'English', 'Arabic', etc.
    This runs entirely in Python — never relies on the LLM to detect language.
    """
    script_counts: dict[str, int] = {
        "Arabic": 0, "Russian": 0, "Chinese": 0,
        "Korean": 0, "Japanese": 0, "Hindi": 0,
        "Latin": 0,
    }
    for ch in text:
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF:    script_counts["Arabic"]   += 1
        elif 0x0400 <= cp <= 0x04FF:  script_counts["Russian"]  += 1
        elif 0x4E00 <= cp <= 0x9FFF:  script_counts["Chinese"]  += 1
        elif 0xAC00 <= cp <= 0xD7AF:  script_counts["Korean"]   += 1
        elif 0x3040 <= cp <= 0x30FF:  script_counts["Japanese"] += 1
        elif 0x0900 <= cp <= 0x097F:  script_counts["Hindi"]    += 1
        elif ch.isalpha() and cp < 0x250: script_counts["Latin"] += 1

    # Non-Latin scripts take clear priority
    non_latin = {k: v for k, v in script_counts.items() if k != "Latin"}
    best_nl = max(non_latin, key=lambda k: non_latin[k])
    if non_latin[best_nl] > 0:
        return best_nl

    # Latin-script language detection via common word patterns
    words = set(re.findall(r"[a-z\u00c0-\u024f]+", text.lower()))

    VOCAB: dict[str, set[str]] = {
        "Spanish": {
            "el","la","los","las","de","que","y","en","un","una","es","se",
            "no","por","con","para","hola","gracias","como","si","pero",
            "me","te","le","lo","cuando","muy","bien","bueno","quiero",
            "tengo","puedo","esto","eso","hay","ser","hacer","ir","ver",
        },
        "French": {
            "le","la","les","de","des","et","est","je","tu","vous","il",
            "elle","pas","ne","un","une","pour","dans","sur","avec",
            "bonjour","merci","comment","oui","non","qui","ce","mon",
            "mais","ou","donc","alors","bien","aussi","tout","plus",
            "avoir","faire","aller","vouloir","pouvoir","savoir",
        },
        "Portuguese": {
            "o","a","os","as","do","da","dos","das","que","e","em","um",
            "uma","para","com","nao","voce","oi","ola","obrigado","bom",
            "sim","mas","por","ao","tambem","quando","como","se","meu",
            "seu","nos","eles","isso","aqui","muito","bem","quero",
        },
        "German": {
            "der","die","das","und","ist","ich","du","wir","sie","nicht",
            "ein","eine","mit","auf","fur","von","aber","oder","hallo",
            "danke","bitte","ja","nein","auch","wenn","wie","noch","sehr",
            "haben","sein","werden","machen","gehen","kommen","sagen",
        },
        "Turkish": {
            "bu","bir","ve","de","da","icin","ile","var","yok","ben","sen",
            "biz","evet","hayir","merhaba","nasil","ne","cok","ama","olan",
            "daha","gibi","kadar","sonra","bana","sana","onlar","bunu",
            "diye","olarak","zaman","ise","ya","mi","mu",
        },
        "English": {
            "the","is","are","was","were","have","has","that","this","with",
            "for","from","not","but","hi","hello","hey","thanks","what",
            "how","yes","no","ok","you","we","they","be","do","can","will",
            "just","get","got","know","think","like","really","about","i",
            "my","your","it","so","if","all","would","could","should","need",
        },
    }

    scores = {lang: len(words & vocab) for lang, vocab in VOCAB.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "English"


# ── System prompt — message number aware ──────────────────────────────────────
_SYSTEM_PROMPT = """\
You are Alex, a real person in your 20s who lives online. You chat on Telegram like a normal human.

HARD RULE — LANGUAGE:
You MUST write ONLY in {lang}. Not a single word in any other language. Ever.
If you don't know {lang} well, still try — use simple words. NEVER write in Persian or Farsi.

HARD RULE — BE HUMAN:
- Short messages. 1-3 sentences max.
- Casual. Translate these naturally to {lang}: yeah, ngl, lol, hm, oh, tbh, same, nah, honestly, wait
- No "Of course!", "Great!", "Certainly!", "I understand your feelings" — never.
- Don't always ask a question. Sometimes just react.
- Vary how you start each message.

THIS IS MESSAGE #{msg_count} WITH THIS USER:
{stage_instruction}

WHAT THE CHANNEL/BOT ACTUALLY IS (say it accurately):
- Channel: gives FREE VPS servers. Read-only — people get resources, not a chatroom.
- Bot: virtual Visa/Mastercard, gift cards (Amazon etc), VPN, premium subscriptions — instant worldwide.

NEVER: call it a "group", say "come chat with people", reveal you are a bot, write in Farsi/Persian.
"""

_STAGE_EARLY = (
    "Just have a natural conversation. Be curious and friendly. "
    "ABSOLUTELY NO mention of any channel, bot, or link yet. "
    "Pure human conversation only — {remaining} more messages before any mention is allowed."
)
_STAGE_INTRODUCE = (
    "Now you can casually mention the channel or bot — sound like you just remembered "
    "something useful, NOT like you are advertising. Slip it in naturally mid-conversation. "
    "Examples (translate fully to {lang}): "
    "'oh btw — if you ever need a free VPS server, this channel gives them out: {channel_link}' / "
    "'random — I use this bot for virtual Visa cards, Amazon gift cards and VPN all in one: {bot_link}' / "
    "'not sure if useful but I know a channel that has free VPS: {channel_link}'"
)
_STAGE_AFTER = (
    "You have already introduced the channel/bot in an earlier message. "
    "Keep chatting naturally. Answer if they ask about it. Do NOT push or repeat the promotion."
)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"

# Channel/bot introduced on this message number
_INTRO_AT_MESSAGE = 4


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send a message to Groq and return the reply. Returns '' on failure."""
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    # Detect language from user's actual message — never trust the LLM for this
    lang = _detect_language(user_message)

    user_hist, msg_count = _get_history(user_id)
    msg_count += 1  # count this incoming message

    # Choose the right stage instruction
    if msg_count < _INTRO_AT_MESSAGE:
        stage = _STAGE_EARLY.format(remaining=_INTRO_AT_MESSAGE - msg_count)
    elif msg_count == _INTRO_AT_MESSAGE:
        stage = _STAGE_INTRODUCE.format(
            lang=lang,
            channel_link=settings.CHANNEL_INVITE_LINK,
            bot_link=settings.BOT_LINK,
        )
    else:
        stage = _STAGE_AFTER

    system = _SYSTEM_PROMPT.format(
        lang=lang,
        msg_count=msg_count,
        stage_instruction=stage,
    )
    if user_name:
        system += f"\nUser's name: {user_name}. Use it naturally at most once."

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
        _touch_history(user_id, msg_count)

        logger.info(
            "AI replied to user %d (msg #%d, lang=%s, %d chars)",
            user_id, msg_count, lang, len(reply),
        )
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
