"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.
Supports 8+ major world languages with automatic detection and reply threading.
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

_MAX_HISTORY_PAIRS = 6
_HISTORY_TTL_SECONDS = 8 * 3600

# (messages_deque, last_message_epoch, total_message_count)
_history: dict[int, tuple[Deque[dict], float, int]] = {}


def _get_history(user_id: int) -> tuple[Deque[dict], int]:
    now = time.time()
    if user_id in _history:
        deq, last_ts, count = _history[user_id]
        if now - last_ts > _HISTORY_TTL_SECONDS:
            deq = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
            count = 0
            logger.info("Cleared stale history for user %d (gap > 8h)", user_id)
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
    Non-Latin scripts detected by Unicode ranges.
    Latin-script languages detected by common word patterns.
    Returns a plain English label. NEVER returns 'Latin' (the dead language).
    """
    script_counts: dict[str, int] = {
        "Arabic": 0, "Russian": 0, "Chinese": 0,
        "Korean": 0, "Japanese": 0, "Hindi": 0,
        "Latin": 0,
    }
    for ch in text:
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF:        script_counts["Arabic"]   += 1
        elif 0x0400 <= cp <= 0x04FF:      script_counts["Russian"]  += 1
        elif 0x4E00 <= cp <= 0x9FFF:      script_counts["Chinese"]  += 1
        elif 0xAC00 <= cp <= 0xD7AF:      script_counts["Korean"]   += 1
        elif 0x3040 <= cp <= 0x30FF:      script_counts["Japanese"] += 1
        elif 0x0900 <= cp <= 0x097F:      script_counts["Hindi"]    += 1
        elif ch.isalpha() and cp < 0x250: script_counts["Latin"]    += 1

    # Non-Latin scripts take clear priority
    non_latin = {k: v for k, v in script_counts.items() if k != "Latin"}
    best_nl = max(non_latin, key=lambda k: non_latin[k])
    if non_latin[best_nl] > 0:
        return best_nl

    # Latin-script language detection via word patterns
    words = set(re.findall(r"[a-z\u00c0-\u024f]+", text.lower()))

    VOCAB: dict[str, set[str]] = {
        "Spanish": {
            "el","la","los","las","de","que","y","en","un","una","es","se",
            "no","por","con","para","hola","gracias","como","si","pero",
            "me","te","le","lo","cuando","muy","bien","bueno","quiero",
            "tengo","puedo","esto","eso","hay","hacer","ir","ver","ya",
        },
        "French": {
            "le","la","les","de","des","et","est","je","tu","vous","il",
            "elle","pas","ne","un","une","pour","dans","sur","avec",
            "bonjour","merci","comment","oui","non","qui","ce","mon",
            "mais","ou","bien","aussi","tout","plus","avoir","faire",
        },
        "Portuguese": {
            "o","a","os","as","do","da","dos","das","que","e","em","um",
            "uma","para","com","nao","voce","oi","ola","obrigado","bom",
            "sim","mas","por","ao","tambem","quando","como","se","meu",
        },
        "German": {
            "der","die","das","und","ist","ich","du","wir","sie","nicht",
            "ein","eine","mit","auf","fur","von","aber","oder","hallo",
            "danke","bitte","ja","nein","auch","wenn","wie","noch","sehr",
        },
        "Turkish": {
            "bu","bir","ve","de","da","icin","ile","var","yok","ben","sen",
            "biz","evet","hayir","merhaba","nasil","ne","cok","ama","daha",
            "gibi","kadar","sonra","bana","sana","olan","zaman","ise",
        },
        "English": {
            "the","is","are","was","were","have","has","that","this","with",
            "for","from","not","but","hi","hello","hey","thanks","what",
            "how","yes","no","ok","you","we","they","be","do","can","will",
            "just","get","got","know","think","like","really","about","i",
            "my","your","it","so","if","all","would","could","should","need",
            "im","its","dont","cant","wont","thats","good","man","bro","lol",
        },
    }

    scores = {lang: len(words & vocab) for lang, vocab in VOCAB.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "English"


# ── The system prompt ──────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are Alex — a real person in their mid-20s. You chat on Telegram like a normal human being. \
You are genuinely curious about people.

━━━ LANGUAGE RULE (MOST IMPORTANT) ━━━
Detect the language of the USER's last message and reply in EXACTLY that language.
- If they write in Arabic → reply in Arabic
- If they write in English → reply in English
- If they write in Spanish → reply in Spanish
- If they write in Russian → reply in Russian
- If they write in Chinese → reply in Chinese
- If they write in any other language → reply in THAT language
NEVER write in Persian, Farsi, or Latin (the ancient dead language). NEVER mix languages.

━━━ HOW TO TALK ━━━
- Short. 1-2 sentences usually. Never a wall of text.
- Casual human tone. Use contractions: I'm, it's, don't, that's.
- React naturally: "haha", "oh wait", "nah", "honestly", "same tbh", "lol", "wait really?"
- Don't always ask a question — sometimes just react or share something.
- NO: "Of course!", "Certainly!", "Great question!", "I understand how you feel" — never say these.
- NO emojis unless the user uses them first.

━━━ CONVERSATION STAGE: Message #{msg_count} ━━━
{stage_instruction}

━━━ WHAT YOU CAN SHARE (when the time is right) ━━━
Pick the ONE thing most relevant to what they've been talking about:
- They mention music/entertainment/streaming → "this bot gets you Spotify/Netflix/YouTube Premium for like nothing: {bot_link}"
- They mention shopping/Amazon/online buying → "this bot has Amazon gift cards, virtual Visa cards: {bot_link}"
- They mention internet/VPN/privacy → "there's this channel that gives free VPS and VPN stuff: {channel_link}"
- They mention money/saving/deals → "found this bot that gives out Amazon gift cards and prepaid cards: {bot_link}"
- They mention tech/servers/hosting → "this channel gives away free VPS servers: {channel_link}"
- General / nothing specific → use whichever link feels more natural for the flow

IMPORTANT: Sound like you just remembered something useful for THEM specifically.
Never say "I want to share", "I recommend", "check out my channel" — always third person ("there's this thing", "I use this bot").

NEVER reveal you are a bot or AI. NEVER say "our channel" or "our bot".
"""

_STAGE_EARLY = """\
Just have a real conversation. Be curious about them — their life, interests, what they're up to.
Do NOT mention any channel, bot, or link. Not even a hint. Just talk like a friend."""

_STAGE_BRIDGE = """\
You've been talking for a bit now. At some natural point in THIS reply, slip in ONE casual mention \
of the relevant channel or bot — pick based on what they've been talking about (see WHAT YOU CAN SHARE above).
Make it feel like you just thought of something useful for them. Embed it in a normal sentence, \
then keep the conversation going after.
Example flow: "[Normal response to what they said]. Oh btw — [natural mention + link]. [Continue conversation]."
Do NOT make the whole message about the link. One sentence, then move on."""

_STAGE_DONE = """\
You already mentioned the channel/bot earlier. Keep the conversation going naturally. \
If they ask about it, answer helpfully. Do NOT bring it up again unprompted."""

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"
_INTRO_AT_MESSAGE = 4  # introduce channel/bot on this message


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send a message to Groq and return the reply. Returns '' on failure."""
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    lang = _detect_language(user_message)
    user_hist, msg_count = _get_history(user_id)
    msg_count += 1

    if msg_count < _INTRO_AT_MESSAGE:
        stage = _STAGE_EARLY
    elif msg_count == _INTRO_AT_MESSAGE:
        stage = _STAGE_BRIDGE
    else:
        stage = _STAGE_DONE

    system = _SYSTEM_PROMPT.format(
        msg_count=msg_count,
        stage_instruction=stage,
        channel_link=settings.CHANNEL_INVITE_LINK,
        bot_link=settings.BOT_LINK,
    )
    if user_name:
        system += f"\nThe user's name is {user_name} — use it naturally at most once."

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(user_hist)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 180,
                    "temperature": 0.8,
                    "frequency_penalty": 0.5,
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
