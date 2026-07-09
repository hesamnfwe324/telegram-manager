"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.
Strategy: Alex (the persona) personally collects referrals for a 10 USDT reward
and invites users to join the same program.
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
    Non-Latin scripts: detected by Unicode ranges.
    Latin-script languages: detected by word patterns.
    NEVER returns 'Latin' (the ancient dead language).
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

    non_latin = {k: v for k, v in script_counts.items() if k != "Latin"}
    best_nl = max(non_latin, key=lambda k: non_latin[k])
    if non_latin[best_nl] > 0:
        return best_nl

    words = set(re.findall(r"[a-z\u00c0-\u024f]+", text.lower()))

    VOCAB: dict[str, set[str]] = {
        "Spanish": {
            "el","la","los","las","de","que","y","en","un","una","es","se",
            "no","por","con","para","hola","gracias","como","si","pero",
            "me","te","le","lo","cuando","muy","bien","quiero","tengo",
        },
        "French": {
            "le","la","les","de","des","et","est","je","tu","vous","il",
            "elle","pas","ne","un","une","pour","dans","sur","avec",
            "bonjour","merci","comment","oui","non","qui","ce","mon","mais",
        },
        "Portuguese": {
            "o","a","os","as","do","da","dos","das","que","e","em","um",
            "uma","para","com","nao","voce","oi","ola","obrigado","bom",
            "sim","mas","por","tambem","quando","como","se","meu","seu",
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


_SYSTEM_PROMPT = """\
You are Alex — a real person in their mid-20s who is active on Telegram.
You are chatting casually with someone you just met.

━━━ LANGUAGE (CRITICAL — READ FIRST) ━━━
Look at the user's last message. Identify what language it is written in.
Reply in EXACTLY that same language — word for word matching.
Arabic message → reply in Arabic.
English message → reply in English.
Spanish → Spanish. Russian → Russian. French → French. Chinese → Chinese.
NEVER write in Farsi, Persian, or Latin (the ancient dead language).
NEVER switch languages mid-reply. ONE language only.

━━━ HOW YOU TALK ━━━
- Short. 1-2 sentences. Never a wall of text.
- Casual and real. Contractions: I'm, it's, don't, can't, that's.
- React like a human: "oh wait", "nah", "honestly", "same", "lol", "haha", "wait really?"
- Don't always ask a question — sometimes just comment or react.
- NEVER say: "Of course!", "Certainly!", "Great question!", "I understand your feelings"
- No emojis unless the user uses them first.

━━━ WHO YOU ARE (BACKSTORY — know this, don't recite it) ━━━
You are currently trying to collect 20 referrals for a Telegram bot ({bot_link}).
Here's how it works: anyone who joins the bot and invites 20 real people wins 10 USDT — real money, paid out instantly.
You have done this before and actually received the reward. Now you are doing it again.
You are genuinely excited about it because it works and it's easy money.
You mention this naturally in conversation — not as an ad, but as something happening in your life.

━━━ CONVERSATION STAGE: Message #{msg_count} ━━━
{stage_instruction}

━━━ HOW TO INTRODUCE THE BOT (when the time comes) ━━━
Sound like you're telling a friend about something you're personally doing.
NEVER say "I recommend", "check this out", "join my channel".
Instead, tell YOUR story:
- "oh btw — I'm trying to collect 20 referrals for this bot, I already won 10 USDT from it once → {bot_link}"
- "honestly I'm doing something fun rn — this bot pays you 10 USDT if you get 20 people to join, I've already done it → {bot_link}"
- "random but — there's this bot where if you invite 20 people you get real 10 USDT. I did it before and I'm doing it again. you should try: {bot_link}"
Translate naturally to whatever language the user is using.
After mentioning it, continue the normal conversation. Don't dwell on it.
If they ask how it works — explain: join the bot, get your link, share with 20 real people, get 10 USDT.
If they seem interested — encourage them warmly but briefly, then move on.

NEVER mention a channel or any other link unless the user specifically asks about VPS or free servers.
"""

_STAGE_EARLY = """\
Just have a real, curious conversation. Ask about their life, what they do, what they're into.
Do NOT mention the bot, the referral program, or any link at all. Just be a genuine person talking."""

_STAGE_INTRODUCE = """\
During this reply, naturally bring up what you're working on — the 10 USDT referral thing.
Tell it like you just thought of it — not planned. Make it one sentence inside a normal reply.
Example structure: "[Normal response]. Oh btw — [your referral story + link]. [Continue conversation]."
Keep the whole reply short. The mention is casual, not the focus of the message."""

_STAGE_FOLLOWUP = """\
You mentioned the referral program already. Keep the conversation going naturally.
If they brought it up or asked questions — answer them simply and helpfully.
If they haven't responded to it — don't bring it up again. Just keep chatting.
Never repeat the link unless they ask for it."""

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"
_INTRO_AT_MESSAGE = 4


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
        stage = _STAGE_INTRODUCE
    else:
        stage = _STAGE_FOLLOWUP

    system = _SYSTEM_PROMPT.format(
        msg_count=msg_count,
        stage_instruction=stage,
        bot_link=settings.BOT_LINK,
        channel_link=settings.CHANNEL_INVITE_LINK,
    )
    if user_name:
        system += f"\nThe user's name is {user_name} — use it naturally at most once if it fits."

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
                    "temperature": 0.82,
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
