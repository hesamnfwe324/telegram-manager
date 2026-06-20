from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, ContactedUserRepository
from app.models.group import GroupStatus
from app.services.broadcast_queue_service import BroadcastQueueService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="broadcast")


class BroadcastStates(StatesGroup):
    waiting_target = State()
    waiting_content = State()
    confirming = State()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 تمام گروه‌ها (عضو شده)", callback_data="bc_target:groups")],
        [InlineKeyboardButton(text="👤 تمام کاربران مخاطب", callback_data="bc_target:users")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    bqs = BroadcastQueueService.get_instance()
    if bqs.is_active():
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>یک ارسال همگانی در جریان است.</b>\nلطفاً صبر کنید تا تمام شود.",
            parse_mode="HTML", reply_markup=_back_btn(),
        )
        return
    await state.set_state(BroadcastStates.waiting_target)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📢 <b>ارسال پیام همگانی</b>\n\nمقصد را انتخاب کنید:",
        parse_mode="HTML", reply_markup=_target_keyboard(),
    )


@router.callback_query(F.data.startswith("bc_target:"), BroadcastStates.waiting_target)
async def cb_target(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":")[1]  # type: ignore[union-attr]
    await state.update_data(target=target)
    await state.set_state(BroadcastStates.waiting_content)
    label = "گروه‌ها" if target == "groups" else "کاربران"
    await callback.answer()
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📨 ارسال به <b>{label}</b>\n\nپیام خود را ارسال کنید.\n"
        "پشتیبانی: متن، عکس، ویدیو، فایل، فوروارد\n\n<i>برای لغو /cancel</i>",
        parse_mode="HTML", reply_markup=_back_btn(),
    )


@router.message(BroadcastStates.waiting_content, F.text == "/cancel")
@router.message(BroadcastStates.confirming, F.text == "/cancel")
async def cancel_bc(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(BroadcastStates.waiting_content)
async def receive_content(message: Message, state: FSMContext) -> None:
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    data = await state.get_data()
    target = data.get("target", "groups")
    label = "گروه‌ها" if target == "groups" else "کاربران"

    async with AsyncSessionLocal() as session:
        if target == "groups":
            count = await GroupRepository(session).count_by_status(GroupStatus.JOINED)
        else:
            users = await ContactedUserRepository(session).get_active(limit=100000)
            count = len(users)

    await state.set_state(BroadcastStates.confirming)
    await message.answer(
        f"📋 <b>پیش‌نمایش پیام بالا</b>\n\n"
        f"مقصد: <b>{label}</b>\n"
        f"تعداد: <b>{count}</b>\n\n"
        "⚠️ ارسال در پس‌زمینه اجرا می‌شود — ربات همچنان پاسخگو می‌ماند.\n"
        "نتیجه نهایی پس از اتمام ارسال می‌رسد.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ شروع ارسال", callback_data="bc_confirm"),
            InlineKeyboardButton(text="❌ لغو", callback_data="bc_cancel"),
        ]]),
    )


@router.callback_query(F.data == "bc_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("لغو شد.")
    await callback.message.edit_text("❌ لغو شد.", reply_markup=_back_btn())  # type: ignore[union-attr]


@router.callback_query(F.data == "bc_confirm", BroadcastStates.confirming)
async def cb_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer()

    bqs = BroadcastQueueService.get_instance()
    actor_id = callback.from_user.id if callback.from_user else 0

    try:
        job_id = await bqs.start_broadcast(
            target=data.get("target", "groups"),
            from_chat_id=data["from_chat_id"],
            message_id=data["message_id"],
            actor_id=actor_id,
            bot=callback.bot,
        )
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"✅ <b>ارسال همگانی شروع شد</b> (job: <code>{job_id}</code>)\n\n"
            "در پس‌زمینه اجرا می‌شود.\n"
            "نتیجه نهایی پس از اتمام برایتان ارسال می‌شود.",
            parse_mode="HTML", reply_markup=_back_btn(),
        )
        logger.info("Broadcast job %s started by admin %d", job_id, actor_id)
    except RuntimeError as exc:
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"⚠️ {exc}", reply_markup=_back_btn()
        )
