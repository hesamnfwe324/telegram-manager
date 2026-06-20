from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.stats_service import StatsService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="stats")
_stats_service = StatsService()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="stats")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    s = await _stats_service.get_stats()

    last_act = s.last_activity.strftime("%Y-%m-%d %H:%M UTC") if s.last_activity else "—"
    updated = s.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    health_icon = "✅" if s.client_healthy else "⚠️"

    text = (
        "📊 <b>آمار سیستم</b>\n\n"
        "👥 <b>گروه‌ها</b>\n"
        f"  کل: <code>{s.total_groups}</code> | ⏳ انتظار: <code>{s.pending_groups}</code>\n"
        f"  🟢 عضو: <code>{s.joined_groups}</code> | 🔴 ناموفق: <code>{s.failed_groups}</code>\n\n"
        "🔗 <b>لینک‌ها</b>\n"
        f"  کل: <code>{s.total_links}</code> | در انتظار: <code>{s.pending_links}</code>\n\n"
        "👤 <b>کاربران مخاطب</b>\n"
        f"  کل: <code>{s.total_contacted_users}</code>\n\n"
        "⚙️ <b>سیستم</b>\n"
        f"  {health_icon} User Client: {'سالم' if s.client_healthy else 'مشکل دارد'}\n"
        f"  📋 صف عضویت: <code>{s.join_queue_size}</code> مورد\n"
        f"  📝 لاگ‌ها: <code>{s.total_logs}</code>\n\n"
        f"⏱ آخرین فعالیت: <code>{last_act}</code>\n"
        f"📌 آخرین گروه: <code>{s.last_group_title or '—'}</code>\n"
        f"🔄 بروزرسانی: <code>{updated}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_btn())  # type: ignore[union-attr]
