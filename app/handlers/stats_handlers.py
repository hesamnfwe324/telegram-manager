from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timezone

from app.services.stats_service import StatsService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="stats")
_stats_service = StatsService()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\ud83d\udd04 بروزرسانی", callback_data="stats")],
        [InlineKeyboardButton(text="\ud83d\udd19 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    s = await _stats_service.get_stats()

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    last_act = s.last_activity.strftime("%Y-%m-%d %H:%M UTC") if s.last_activity else "\u2014"
    health_icon = "\u2705" if s.client_healthy else "\u26a0\ufe0f"

    # In-memory queue vs DB pending — show both for clarity
    queue_in_memory = s.join_queue_size
    queue_in_db = s.pending_queue_size

    text = (
        "\ud83d\udcca <b>\u0622\u0645\u0627\u0631 \u0644\u062d\u0638\u0647\u200c\u0627\u06cc \u0633\u06cc\u0633\u062a\u0645</b>\n"
        f"\u23f1 \u0628\u0631\u0648\u0632\u0631\u0633\u0627\u0646\u06cc: <code>{now_str}</code>\n\n"
        "\ud83d\udc65 <b>\u06af\u0631\u0648\u0647\u200c\u0647\u0627</b>\n"
        f"  \u06a9\u0644: <code>{s.total_groups}</code>\n"
        f"  \ud83d\udfe2 \u0639\u0636\u0648 \u0634\u062f\u0647: <code>{s.joined_groups}</code>\n"
        f"  \u2705 \u0627\u0645\u0631\u0648\u0632: <code>{s.today_joins}</code> \u06af\u0631\u0648\u0647\n"
        f"  \u23f3 \u062f\u0631 \u0635\u0641: <code>{queue_in_db}</code> (\u062f\u06cc\u062a\u0627\u0628\u06cc\u0633) | <code>{queue_in_memory}</code> (\u062d\u0627\u0641\u0638\u0647)\n"
        f"  \ud83d\udd34 \u0646\u0627\u0645\u0648\u0641\u0642: <code>{s.failed_groups}</code>\n\n"
        "\ud83d\udd17 <b>\u0644\u06cc\u0646\u06a9\u200c\u0647\u0627</b>\n"
        f"  \u06a9\u0644: <code>{s.total_links}</code> | \u062f\u0631 \u0627\u0646\u062a\u0638\u0627\u0631: <code>{s.pending_links}</code>\n\n"
        "\ud83d\udc64 <b>\u06a9\u0627\u0631\u0628\u0631\u0627\u0646</b>\n"
        f"  \u2705 \u0641\u0639\u0627\u0644: <code>{s.total_contacted_users}</code>\n"
        f"  \ud83d\udce6 \u06a9\u0644: <code>{s.total_contacted_users_all}</code>\n\n"
        "\u2699\ufe0f <b>\u0633\u06cc\u0633\u062a\u0645</b>\n"
        f"  {health_icon} User Client: {'\u0633\u0627\u0644\u0645' if s.client_healthy else '\u0645\u0634\u06a9\u0644 \u062f\u0627\u0631\u062f'}\n"
        f"  \ud83d\udcdd \u0644\u0627\u06af\u200c\u0647\u0627: <code>{s.total_logs}</code>\n\n"
        f"\u23f1 \u0622\u062e\u0631\u06cc\u0646 \u0641\u0639\u0627\u0644\u06cc\u062a: <code>{last_act}</code>\n"
        f"\ud83d\udccc \u0622\u062e\u0631\u06cc\u0646 \u06af\u0631\u0648\u0647: <code>{s.last_group_title or '\u2014'}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_btn())
