    """
    AI DM handler — intercepts private messages from non-admin users and
    replies with a Grok-powered conversational assistant.
    """
    from aiogram import Router, F
    from aiogram.types import Message
    from aiogram.filters import CommandStart

    from app.config import settings
    from app.services.grok_service import chat, clear_history
    from app.utils.logger import get_logger

    logger = get_logger(__name__)
    router = Router(name="ai")


    def _is_admin(user_id: int) -> bool:
      return user_id in {
          int(x) for x in settings.ADMIN_IDS.split(",") if x.strip().isdigit()
      }


    @router.message(CommandStart(), F.chat.type == "private")
    async def cmd_start_ai(message: Message) -> None:
      """Handle /start from non-admin users — reset context and greet warmly."""
      if not message.from_user or _is_admin(message.from_user.id):
          return

      user_id   = message.from_user.id
      user_name = message.from_user.first_name or ""
      clear_history(user_id)

      # Grok opens the conversation naturally
      reply = await chat(user_id, "/start", user_name=user_name)
      if reply:
          await message.answer(reply)
      else:
          greeting = f"سلام {user_name}! 👋" if user_name else "سلام! 👋"
          await message.answer(f"{greeting} خوشحالم که اومدی 😊")


    @router.message(F.chat.type == "private")
    async def handle_dm(message: Message) -> None:
      """Main private DM handler — relay to Grok and reply."""
      if not message.from_user:
          return
      if _is_admin(message.from_user.id):
          return  # admins use the admin panel

      text = (message.text or message.caption or "").strip()
      if not text:
          return

      user_id   = message.from_user.id
      user_name = message.from_user.first_name or ""

      try:
          await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")  # type: ignore[union-attr]
          reply = await chat(user_id, text, user_name=user_name)
          if reply:
              await message.answer(reply)
      except Exception as exc:
          logger.error("AI handler error for user %d: %s", user_id, exc)
    