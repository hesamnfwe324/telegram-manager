import re
from typing import Optional


TELEGRAM_LINK_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?t\.me/joinchat/([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?t\.me/\+([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?t\.me/([a-zA-Z][a-zA-Z0-9_]{4,})", re.I),
    re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{4,})", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?telegram\.me/([a-zA-Z0-9_]+)", re.I),
]

PRIVATE_HASH_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?t\.me/joinchat/([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?t\.me/\+([a-zA-Z0-9_-]+)", re.I),
]


class LinkValidator:
    @staticmethod
    def extract_links(text: str) -> list[str]:
        found: list[str] = []
        for pattern in TELEGRAM_LINK_PATTERNS:
            for match in pattern.finditer(text):
                full = match.group(0).strip()
                if full.startswith("@"):
                    full = f"https://t.me/{match.group(1)}"
                elif not full.startswith("http"):
                    full = f"https://{full}"
                found.append(full)
        return list(dict.fromkeys(found))

    @staticmethod
    def normalize(link: str) -> Optional[str]:
        link = link.strip()
        if not link:
            return None
        if link.startswith("@"):
            username = link[1:]
            if re.match(r"^[a-zA-Z][a-zA-Z0-9_]{4,}$", username):
                return f"https://t.me/{username}"
            return None
        if not link.startswith("http"):
            link = f"https://{link}"
        for pattern in TELEGRAM_LINK_PATTERNS:
            if pattern.search(link):
                return link
        return None

    @staticmethod
    def is_private_invite(link: str) -> bool:
        for pattern in PRIVATE_HASH_PATTERNS:
            if pattern.search(link):
                return True
        return False

    @staticmethod
    def extract_username(link: str) -> Optional[str]:
        m = re.search(r"t\.me/([a-zA-Z][a-zA-Z0-9_]{4,})$", link, re.I)
        if m:
            return m.group(1).lower()
        return None
