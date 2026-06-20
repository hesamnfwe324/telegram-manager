from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.backup_service import BackupService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="backup")

_backup_service = BackupService()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _backup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 بکاپ دستی", callback_data="backup_create")],
        [InlineKeyboardButton(text="📋 لیست بکاپ‌ها", callback_data="backup_list")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "backup_menu")
async def cb_backup_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(  # type: ignore[union-attr]
        "💾 <b>مدیریت بکاپ</b>\n\nانتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_backup_menu_keyboard(),
    )


@router.callback_query(F.data == "backup_create")
async def cb_backup_create(callback: CallbackQuery) -> None:
    await callback.answer("⏳ در حال ساخت بکاپ...", show_alert=False)
    await callback.message.edit_text("⏳ در حال ساخت بکاپ دیتابیس...")  # type: ignore[union-attr]

    actor = str(callback.from_user.id) if callback.from_user else "admin"
    path = await _backup_service.create_backup(actor=actor)

    if path:
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"✅ بکاپ با موفقیت ساخته شد:\n<code>{path}</code>",
            parse_mode="HTML",
            reply_markup=_backup_menu_keyboard(),
        )
    else:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "❌ خطا در ساخت بکاپ. لاگ‌ها را بررسی کنید.",
            reply_markup=_backup_menu_keyboard(),
        )


@router.callback_query(F.data == "backup_list")
async def cb_backup_list(callback: CallbackQuery) -> None:
    await callback.answer()
    backups = _backup_service.list_backups()

    if not backups:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "📋 هیچ بکاپی وجود ندارد.",
            reply_markup=_backup_menu_keyboard(),
        )
        return

    lines = ["📋 <b>بکاپ‌های موجود:</b>\n"]
    for b in backups[:10]:
        lines.append(f"• <code>{b['filename']}</code> — {b['size_kb']} KB\n  <i>{b['created_at']}</i>")

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_backup_menu_keyboard(),
    )


@router.callback_query(F.data == "error_logs")
async def cb_error_logs(callback: CallbackQuery) -> None:
    await callback.answer()
    from app.database.connection import AsyncSessionLocal
    from app.repositories import LogRepository

    async with AsyncSessionLocal() as session:
        log_repo = LogRepository(session)
        logs = await log_repo.get_errors(limit=20)

    if not logs:
        await callback.message.edit_text("✅ هیچ خطایی ثبت نشده.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    lines = [f"🚨 <b>آخرین {len(logs)} خطا:</b>\n"]
    for log in logs:
        ts = log.timestamp.strftime("%m/%d %H:%M") if log.timestamp else "—"
        err = (log.error_message or "")[:60]
        lines.append(f"<code>{ts}</code> | {log.action} | {err}")

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_btn(),
    )
