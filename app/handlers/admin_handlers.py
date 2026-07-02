from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from datetime import datetime, timezone
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="admin")


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 آمار", callback_data="stats"),
            InlineKeyboardButton(text="📋 لیست گروه‌ها", callback_data="groups_list"),
        ],
        [
            InlineKeyboardButton(text="⏳ در انتظار بررسی", callback_data="groups_pending"),
            InlineKeyboardButton(text="🔴 ناموفق‌ها", callback_data="groups_failed"),
        ],
        [
            InlineKeyboardButton(text="📨 ارسال به گروه", callback_data="send_message"),
            InlineKeyboardButton(text="📢 ارسال همگانی", callback_data="broadcast"),
        ],
        [
            InlineKeyboardButton(text="📥 خروجی داده", callback_data="export_menu"),
            InlineKeyboardButton(text="🚨 گزارش خطاها", callback_data="error_logs"),
        ],
        [
            InlineKeyboardButton(text="💾 بکاپ دیتابیس", callback_data="backup_menu"),
            InlineKeyboardButton(text="❤️ وضعیت سیستم", callback_data="system_health"),
        ],
        [
            InlineKeyboardButton(text="▶️ شروع سیستم", callback_data="system_start"),
            InlineKeyboardButton(text="⏹ توقف سیستم", callback_data="system_stop"),
        ],
        [InlineKeyboardButton(text="U0001f504 همگام‌سازی گروه‌ها", callback_data="sync_dialogs"),
            InlineKeyboardButton(text="👥 همگام‌سازی مخاطبین", callback_data="sync_users")],
    ])


async def _safe_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    """Edit message text, silently ignoring 'message is not modified' errors."""
    try:
        await callback.message.edit_text(text, **kwargs)  # type: ignore[union-attr]
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🤖 <b>ربات مدیریت گروه‌های تلگرام</b>\n\nانتخاب کنید:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())


@router.message(Command("cancel_broadcast"))
async def cmd_cancel_broadcast(message: Message) -> None:
    """Cancel the currently running broadcast immediately."""
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    if not bqs.is_active():
        await message.answer("⚠️ هیچ broadcast فعالی در حال اجرا نیست.")
        return
    cancelled = bqs.cancel_active()
    if cancelled:
        await message.answer(
            "🛑 <b>Broadcast لغو شد.</b>\n\n"
            "گزارش نهایی با آمار تا الان برایتان ارسال می‌شود.",
            parse_mode="HTML",
        )
    else:
        await message.answer("⚠️ تسک اجرایی پیدا نشد — broadcast flag reset شد.")


@router.message(Command("broadcast_status"))
async def cmd_broadcast_status(message: Message) -> None:
    """Show progress of the currently running broadcast."""
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    job = bqs.get_active_job()
    if not job:
        await message.answer("⚠️ هیچ broadcast فعالی در حال اجرا نیست.")
        return
    done = job.success + job.failed
    pct = int(done / job.total * 100) if job.total else 0
    target_fa = "گروه‌ها" if job.target == "groups" else "کاربران"
    elapsed = (datetime.now(timezone.utc) - job.started_at).seconds // 60
    await message.answer(
        f"📢 <b>وضعیت broadcast</b>\n\n"
        f"مقصد: {target_fa}\n"
        f"پیشرفت: <code>{done}</code> از <code>{job.total}</code> ({pct}%)\n"
        f"✅ موفق: <code>{job.success}</code>\n"
        f"❌ ناموفق: <code>{job.failed}</code>\n"
        f"⏱ مدت: <code>{elapsed}</code> دقیقه\n\n"
        f"برای لغو: /cancel_broadcast",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery) -> None:
    await _safe_edit(callback, "منوی اصلی:", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "system_health")
async def cb_system_health(callback: CallbackQuery) -> None:
    await callback.answer()
    from app.services import TelegramUserService, HealthService, JoinQueueService
    from app.services.broadcast_queue_service import BroadcastQueueService

    tg = TelegramUserService.get_instance()
    health = HealthService.get_instance()
    jq = JoinQueueService.get_instance()
    bqs = BroadcastQueueService.get_instance()

    client_status = "🟢 متصل" if tg.is_running() else "🔴 قطع"
    health_status = "✅ سالم" if health.is_healthy() else "⚠️ مشکل دارد"
    last_ok = health.last_ok_at()
    last_ok_str = last_ok.strftime("%H:%M:%S UTC") if last_ok else "—"

    # Broadcast status
    bc_job = bqs.get_active_job()
    if bc_job:
        done = bc_job.success + bc_job.failed
        pct = int(done / bc_job.total * 100) if bc_job.total else 0
        bc_status = f"📢 در حال ارسال ({done}/{bc_job.total} — {pct}%)"
    else:
        bc_status = "💤 بیکار"

    buttons = [
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="system_health")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ]
    if bc_job:
        buttons.insert(1, [
            InlineKeyboardButton(text="🛑 لغو broadcast", callback_data="cancel_broadcast_cb")
        ])

    await _safe_edit(
        callback,
        "❤️ <b>وضعیت سیستم</b>\n\n"
        f"📡 User Client: {client_status}\n"
        f"🏥 Health Monitor: {health_status}\n"
        f"⏱ آخرین بررسی OK: <code>{last_ok_str}</code>\n"
        f"📋 صف عضویت: <code>{jq.queue_size()}</code> مورد در انتظار\n"
        f"📢 Broadcast: {bc_status}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "cancel_broadcast_cb")
async def cb_cancel_broadcast(callback: CallbackQuery) -> None:
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    cancelled = bqs.cancel_active()
    await callback.answer(
        "🛑 Broadcast لغو شد." if cancelled else "⚠️ تسک پیدا نشد.",
        show_alert=True,
    )
    await cb_system_health(callback)


@router.callback_query(F.data == "system_start")
async def cb_system_start(callback: CallbackQuery) -> None:
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if tg.is_running():
        await callback.answer("سیستم در حال اجرا است.", show_alert=True)
        return
    try:
        await tg.start()
        await JoinQueueService.get_instance().start()
        await HealthService.get_instance().start()
        actor = callback.from_user.id if callback.from_user else "?"
        await callback.answer("✅ سیستم شروع شد.", show_alert=True)
        logger.info("System started by admin %s", actor)
    except Exception as exc:
        await callback.answer(f"❌ خطا: {exc}", show_alert=True)


@router.callback_query(F.data == "system_stop")
async def cb_system_stop(callback: CallbackQuery) -> None:
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if not tg.is_running():
        await callback.answer("سیستم متوقف است.", show_alert=True)
        return
    await HealthService.get_instance().stop()
    await JoinQueueService.get_instance().stop()
    await tg.stop()
    actor = callback.from_user.id if callback.from_user else "?"
    await callback.answer("⏹ سیستم متوقف شد.", show_alert=True)
    logger.info("System stopped by admin %s", actor)


@router.callback_query(F.data == "error_logs")
async def cb_error_logs(callback: CallbackQuery) -> None:
    """Show recent error entries from the logs table."""
    await callback.answer()
    from app.database.connection import AsyncSessionLocal
    from app.repositories import LogRepository

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="error_logs")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])

    async with AsyncSessionLocal() as session:
        repo = LogRepository(session)
        logs = await repo.get_errors(limit=15)

    if not logs:
        await _safe_edit(
            callback,
            "✅ هیچ خطایی در لاگ‌ها ثبت نشده.",
            reply_markup=back_kb,
        )
        return

    lines = [f"🚨 <b>آخرین خطاها ({len(logs)}):</b>\n"]
    for log in logs:
        ts = log.timestamp.strftime("%m/%d %H:%M") if log.timestamp else "?"
        detail = (log.error_message or log.details or "")[:60]
        lines.append(
            f"⏱ <code>{ts}</code>  <b>{log.action}</b>"
            + (f"\n<i>{detail}</i>" if detail else "")
        )

    await _safe_edit(
        callback,
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_kb,
    )


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "🤖 <b>ربات مدیریت گروه‌های تلگرام</b>\n\nانتخاب کنید:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sync_dialogs")
async def cb_sync_dialogs(callback: CallbackQuery) -> None:
    """همگام‌سازی گروه‌های تلگرام با دیتابیس."""
    await callback.answer()
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>در حال همگام‌سازی گروه‌ها...</b>\n\nلطفاً چند ثانیه صبر کنید.",
            parse_mode="HTML",
            reply_markup=_back_btn(),
        )
    except Exception:
        pass
    try:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        new_count, total = await tg.sync_dialogs_to_db()
        text = (
            f"✅ <b>همگام‌سازی گروه‌ها کامل شد</b>\n\n"
            f"📦 کل گروه‌ها: <code>{total}</code>\n"
            f"🆕 جدید: <code>{new_count}</code>\n"
            f"♻️ موجود: <code>{total - new_count}</code>"
        )
    except Exception as exc:
        text = f"❌ <b>خطا:</b>\n<code>{str(exc)[:300]}</code>"
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML", reply_markup=_back_btn(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sync_users")
async def cb_sync_users(callback: CallbackQuery) -> None:
    """همگام‌سازی PVهای شخصی اکانت با دیتابیس."""
    await callback.answer()
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>در حال همگام‌سازی مخاطبین...</b>\n\nلطفاً چند ثانیه صبر کنید.",
            parse_mode="HTML",
            reply_markup=_back_btn(),
        )
    except Exception:
        pass
    try:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        new_count, total = await tg.sync_user_dialogs_to_db()
        text = (
            f"✅ <b>همگام‌سازی مخاطبین کامل شد</b>\n\n"
            f"📦 کل PVهای اکانت: <code>{total}</code>\n"
            f"🆕 جدید اضافه شده: <code>{new_count}</code>\n"
            f"♻️ موجود در دیتابیس: <code>{total - new_count}</code>"
        )
    except Exception as exc:
        text = f"❌ <b>خطا:</b>\n<code>{str(exc)[:300]}</code>"
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML", reply_markup=_back_btn(),
        )
    except Exception:
        pass