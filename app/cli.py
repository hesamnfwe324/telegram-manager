"""
Interactive CLI for first-time Telegram login.
Run locally: python -m app.cli login
This generates a TELEGRAM_SESSION_STRING you can set as an env var.
"""
import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings
from app.utils.logger import setup_logging, get_logger

logger = get_logger(__name__)


async def interactive_login() -> None:
    print("=== Telegram Interactive Login ===")
    print(f"Phone: {settings.TELEGRAM_PHONE}\n")

    session = StringSession()
    client = TelegramClient(session, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)

    await client.connect()

    if await client.is_user_authorized():
        print("Already authorized!")
        session_str = client.session.save()  # type: ignore[union-attr]
        print(f"\nTELEGRAM_SESSION_STRING={session_str}")
        await client.disconnect()
        return

    await client.send_code_request(settings.TELEGRAM_PHONE)
    code = input("Enter the verification code from Telegram: ").strip()

    try:
        await client.sign_in(settings.TELEGRAM_PHONE, code)
    except Exception as exc:
        if "SessionPasswordNeeded" in type(exc).__name__:
            password = input("Enter your 2FA password: ").strip()
            await client.sign_in(password=password)
        else:
            print(f"Login failed: {exc}")
            await client.disconnect()
            return

    session_str = client.session.save()  # type: ignore[union-attr]
    print("\n✅ Login successful!")
    print("\n" + "=" * 60)
    print("Set this environment variable:")
    print(f"TELEGRAM_SESSION_STRING={session_str}")
    print("=" * 60)

    await client.disconnect()


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2 or sys.argv[1] != "login":
        print("Usage: python -m app.cli login")
        sys.exit(1)
    asyncio.run(interactive_login())


if __name__ == "__main__":
    main()
