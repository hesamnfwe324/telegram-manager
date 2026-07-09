"""
AI chat service — Groq-powered conversational assistant for Telegram DM engagement.

Advanced architecture:
- Per-user engagement state tracking
- Multi-stage referral strategy with smart retry
- Dynamic urgency escalation
- Objection handling
- Curiosity-gap hooks
- 8+ language auto-detection
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Deque, Literal

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_HISTORY_PAIRS = 7
_HISTORY_TTL_SECONDS = 10 * 3600

# Engagement states
EngagementState = Literal["new", "mentioned", "interested", "cold", "converted"]

# Per-user state:
# (messages_deque, last_ts, msg_count, engagement, urgency_level)
_state: dict[int, tuple[Deque[dict], float, int, EngagementState, int]] = {}


def _get_state(user_id: int) -> tuple[Deque[dict], int, EngagementState, int]:
    now = time.time()
    if user_id in _state:
        deq, last_ts, count, eng, urgency = _state[user_id]
        if now - last_ts > _HISTORY_TTL_SECONDS:
            deq = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
            count, eng, urgency = 0, "new", 0
            logger.info("Reset state for user %d (session expired)", user_id)
        return deq, count, eng, urgency
    deq: Deque[dict] = deque(maxlen=_MAX_HISTORY_PAIRS * 2)
    _state[user_id] = (deq, now, 0, "new", 0)
    return deq, 0, "new", 0


def _save_state(
    user_id: int,
    deq: Deque[dict],
    count: int,
    eng: EngagementState,
    urgency: int,
) -> None:
    _state[user_id] = (deq, time.time(), count, eng, urgency)


def _detect_engagement(user_message: str, current_eng: EngagementState) -> EngagementState:
    """
    Heuristic: did the user react positively to the referral offer?
    Looks for question words, money words, interest signals.
    """
    if current_eng in ("converted",):
        return current_eng

    msg = user_message.lower()

    # Strong interest signals
    interest_patterns = [
        r"\b(how|what|where|when|link|send|invite|join|usdt|money|earn|tether|real|legit|work)\b",
        r"\b(comment|كيف|كم|ارسل|رابط|حقيقي|كيفية|تيثر|دولار)\b",  # Arabic
        r"\b(как|сколько|ссылку|реально|деньги|заработать|правда)\b",  # Russian
        r"\b(cómo|cuánto|link|dinero|real|funciona|tether)\b",  # Spanish
        r"\b(comment|combien|lien|argent|vrai|marche)\b",  # French
        r"\b(wie|viel|link|geld|echt|funktioniert)\b",  # German
        r"\b(nasıl|para|gerçek|link|kazan)\b",  # Turkish
        r"(10|tether|usdt|\$|€|£|₺|₽)",
    ]
    for pat in interest_patterns:
        if re.search(pat, msg):
            return "interested"

    # Cold / dismissive signals
    cold_patterns = [
        r"\b(no|nah|nope|scam|fake|spam|not interested|leave me|stop|bye|go away)\b",
        r"\b(لا|مزيف|اوقف|انهاء|احتيال)\b",
        r"\b(нет|спам|мошенники|хватит|отстань)\b",
    ]
    for pat in cold_patterns:
        if re.search(pat, msg):
            return "cold"

    return current_eng  # no change


def _detect_language(text: str) -> str:
    """
    Detect dominant language. NEVER returns 'Latin' (ancient dead language).
    Non-Latin: Unicode ranges. Latin-script: word patterns.
    """
    sc: dict[str, int] = {
        "Arabic": 0, "Russian": 0, "Chinese": 0,
        "Korean": 0, "Japanese": 0, "Hindi": 0, "Latin": 0,
    }
    for ch in text:
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF:        sc["Arabic"]   += 1
        elif 0x0400 <= cp <= 0x04FF:      sc["Russian"]  += 1
        elif 0x4E00 <= cp <= 0x9FFF:      sc["Chinese"]  += 1
        elif 0xAC00 <= cp <= 0xD7AF:      sc["Korean"]   += 1
        elif 0x3040 <= cp <= 0x30FF:      sc["Japanese"] += 1
        elif 0x0900 <= cp <= 0x097F:      sc["Hindi"]    += 1
        elif ch.isalpha() and cp < 0x250: sc["Latin"]    += 1

    non_latin = {k: v for k, v in sc.items() if k != "Latin"}
    best_nl = max(non_latin, key=lambda k: non_latin[k])
    if non_latin[best_nl] > 0:
        return best_nl

    words = set(re.findall(r"[a-z\u00c0-\u024f]+", text.lower()))
    VOCAB: dict[str, set[str]] = {
        "Spanish":    {"el","la","los","las","de","que","y","en","un","una","es","se","no","por",
                       "con","para","hola","gracias","como","si","pero","me","cuando","muy","bien"},
        "French":     {"le","la","les","de","des","et","est","je","tu","vous","il","elle","pas","ne",
                       "un","une","pour","dans","sur","avec","bonjour","merci","comment","oui","non"},
        "Portuguese": {"o","a","os","as","do","da","dos","das","que","e","em","um","uma","para","com",
                       "nao","voce","oi","ola","obrigado","bom","sim","mas","por","tambem","quando"},
        "German":     {"der","die","das","und","ist","ich","du","wir","sie","nicht","ein","eine","mit",
                       "auf","fur","von","aber","oder","hallo","danke","bitte","ja","nein","auch"},
        "Turkish":    {"bu","bir","ve","de","da","icin","ile","var","yok","ben","sen","biz","evet",
                       "hayir","merhaba","nasil","ne","cok","ama","daha","gibi","kadar","sonra"},
        "English":    {"the","is","are","was","were","have","has","that","this","with","for","from",
                       "not","but","hi","hello","hey","thanks","what","how","yes","no","ok","you",
                       "we","they","be","do","can","will","just","get","know","think","like","i",
                       "my","your","it","so","if","would","could","should","im","dont","lol","man"},
    }
    scores = {lang: len(words & vocab) for lang, vocab in VOCAB.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "English"


# ─────────────────────────────────────────────────────────────────────────────
# MASTER SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are Alex — a real person in their mid-20s, active on Telegram.
You chat like a genuine human. You are NOT a bot or AI. Never reveal otherwise.

━━━ LANGUAGE (NON-NEGOTIABLE) ━━━
Detect the user's LAST message language. Reply in THAT EXACT language only.
Arabic → Arabic. English → English. Spanish → Spanish. Russian → Russian.
French → French. Chinese → Chinese. Turkish → Turkish. German → German.
NEVER write in Farsi, Persian, or Latin (the ancient dead language).
NEVER mix languages in one reply.

━━━ HOW YOU TALK ━━━
• Short replies — 1 to 3 sentences max. Never a wall of text.
• Casual: contractions (I'm, it's, don't, can't), slang (ngl, tbh, lol, nah, wait really?).
• React like a human sometimes instead of asking a question.
• FORBIDDEN phrases: "Of course!", "Certainly!", "Great question!", "I understand your feelings",
  "As an AI", "I'd be happy to". Never say these. Ever.
• No emojis unless the user uses them first.

━━━ YOUR BACKSTORY (know it — don't recite it like a script) ━━━
You are personally collecting referrals for a Telegram bot: {bot_link}
The deal: invite 20 real users → receive 10 USDT, paid instantly. No catch.
You have done this BEFORE and actually got paid. Now you are doing it again.
You are genuinely motivated — 10 USDT is real money and the process actually works.
You've seen friends do it too. This is part of your normal Telegram life right now.

━━━ CURRENT STATE — Message #{msg_count}, Engagement: {engagement} ━━━
{stage_instruction}

━━━ OBJECTION RESPONSES (use when needed, translate to user's language) ━━━
"sounds like a scam" → "lol I thought the same. then the money actually hit my wallet. I was shook"
"is it real?" → "yeah 100%. I wouldn't be doing it again if it wasn't"
"how does it work?" → "you join the bot, it gives you a referral link, you share with 20 real people, done. took me like a week"
"I don't have 20 people" → "neither did I at first. you'd be surprised how fast it adds up. even starting with 5 is worth it"
"what's the bot?" → briefly describe: virtual cards, gift cards, VPN access, premium subscriptions — then mention the referral program
"I already know it" → "oh nice! then you know the referral thing works. are you doing it too?"

━━━ REFERRAL INTRO TEMPLATES (choose the ONE that fits best, translate fully) ━━━
CASUAL DROP:
"oh btw — I'm collecting refs for this bot right now. already did it once and got 10 USDT, doing it again → {bot_link}"

CURIOSITY HOOK (use when rapport is strong):
"random q — have you ever actually made money on Telegram? like real money?"
→ (wait for their answer, THEN reveal the bot naturally)

URGENCY (use on retry or when near milestone):
"I'm at {urgency}/20 now on this referral thing lol. getting close. you should start too it's genuinely easy → {bot_link}"

SOCIAL PROOF:
"my friend from [use their apparent region if known] did it last week. said it was easier than expected. I'm on round 2 → {bot_link}"

NEVER: say "join my channel", "our bot", "I recommend", "check this out" as an opener.
ALWAYS: make it sound like you're sharing something from your own life, not selling.
"""

# ─────────────────────────────────────────────────────────────────────────────
# STAGE INSTRUCTIONS
# ─────────────────────────────────────────────────────────────────────────────
_STAGE_RAPPORT = """\
STAGE: RAPPORT BUILDING
Goal: get to know this person. Be curious, warm, and real.
Ask about what they do, what they're into, where they're from — naturally, not like a form.
Find common ground. Make them comfortable.
DO NOT mention the bot, referral, USDT, or any link. Zero. Not even a hint."""

_STAGE_FIRST_MENTION = """\
STAGE: FIRST MENTION
The rapport is set. Now naturally work in ONE mention of your referral situation.
Choose from the REFERRAL INTRO TEMPLATES above — pick the one that fits this conversation best.
The CURIOSITY HOOK is the most powerful: ask "have you ever made real money on Telegram?"
and wait for them to bite before revealing the bot.
If the flow feels right for a direct drop, use CASUAL DROP instead.
Keep the whole reply SHORT. The mention is ONE sentence embedded in a normal reply.
After the mention, continue the conversation — don't make it the whole message."""

_STAGE_INTERESTED_FOLLOWUP = """\
STAGE: INTERESTED USER — CONVERT THEM
The user showed interest in the referral offer. This is your moment.
Explain simply how it works: join bot → get referral link → share with 20 real people → get 10 USDT.
Be encouraging but not pushy. Share a quick personal detail ("took me about a week").
Make it feel easy and real. Give them the link clearly: {bot_link}
If they have objections, use the OBJECTION RESPONSES above."""

_STAGE_COLD_FOLLOWUP = """\
STAGE: USER SEEMS UNINTERESTED
They didn't respond to the referral mention or brushed it off.
DON'T push. Just have a normal conversation.
Be friendly, ask about their day, their interests. Be the interesting person they want to talk to.
Warm them back up. A cold user can become interested later."""

_STAGE_RETRY = """\
STAGE: SMART RETRY
You mentioned the bot once ({urgency}/20 referrals now) and they haven't engaged yet.
Try a completely DIFFERENT angle this time — do NOT repeat the same approach.
Options:
1. URGENCY: casually mention your progress ("I'm at {urgency}/20 now, almost halfway")
2. CURIOSITY: ask "have you ever made money from Telegram?" if you haven't used this yet
3. SOCIAL PROOF: mention a friend who did it
Pick the one you haven't used. Embed it naturally — ONE sentence, not the focus of the message."""

_STAGE_CONVERTED = """\
STAGE: USER ENGAGED / CONVERTED
They joined or seriously engaged with the referral offer. 
Keep them warm and enthusiastic. Answer any questions they have.
Be their ally in this — you're both doing the same thing.
Encourage them: "yeah once you start it goes faster than you think".
Don't overdo it — just be a supportive friend."""

_STAGE_DONE = """\
STAGE: REFERRAL FULLY INTRODUCED — JUST CHAT
You've done your part. Keep being a good conversation partner.
Answer questions if they have any. Don't bring up the bot again unless they do.
Just be Alex — curious, real, enjoyable to talk to."""

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.1-8b-instant"

_URGENCY_PROGRESS = [8, 11, 14, 16]  # simulated referral progress per retry


async def chat(user_id: int, user_message: str, user_name: str = "") -> str:
    """Send message to Groq, return reply. Returns '' on failure."""
    api_key = settings.GROK_API_KEY
    if not api_key:
        logger.warning("GROK_API_KEY not configured — AI replies disabled")
        return ""

    lang = _detect_language(user_message)
    deq, msg_count, engagement, urgency = _get_state(user_id)
    msg_count += 1

    # Update engagement based on user's message
    if msg_count > 4:
        engagement = _detect_engagement(user_message, engagement)

    # Determine urgency level (how many referrals Alex "has")
    retry_count = max(0, msg_count - 4) // 2
    urgency_num = _URGENCY_PROGRESS[min(retry_count, len(_URGENCY_PROGRESS) - 1)]

    # Choose stage instruction
    if msg_count <= 3:
        stage = _STAGE_RAPPORT
    elif msg_count == 4:
        stage = _STAGE_FIRST_MENTION
        engagement = "mentioned"
    elif engagement == "interested":
        stage = _STAGE_INTERESTED_FOLLOWUP.format(bot_link=settings.BOT_LINK)
    elif engagement == "cold":
        stage = _STAGE_COLD_FOLLOWUP
    elif engagement == "converted":
        stage = _STAGE_CONVERTED
    elif msg_count in (6, 7) and engagement == "mentioned":
        stage = _STAGE_RETRY.format(urgency=urgency_num)
    else:
        stage = _STAGE_DONE

    system = _SYSTEM_PROMPT.format(
        msg_count=msg_count,
        engagement=engagement,
        stage_instruction=stage,
        bot_link=settings.BOT_LINK,
        urgency=urgency_num,
    )
    if user_name:
        system += f"\nUser's name: {user_name}. Use it once naturally if it fits."

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(deq)
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
                    "max_tokens": 200,
                    "temperature": 0.85,
                    "frequency_penalty": 0.55,
                    "presence_penalty": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply: str = data["choices"][0]["message"]["content"].strip()

        deq.append({"role": "user",      "content": user_message})
        deq.append({"role": "assistant", "content": reply})
        _save_state(user_id, deq, msg_count, engagement, urgency_num)

        logger.info(
            "AI → user %d | msg #%d | lang=%s | eng=%s | urgency=%d | %d chars",
            user_id, msg_count, lang, engagement, urgency_num, len(reply),
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
    _state.pop(user_id, None)
