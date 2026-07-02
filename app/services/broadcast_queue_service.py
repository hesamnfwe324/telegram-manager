"""
Non-blocking background broadcast service.
Broadcasts run in a dedicated asyncio task — the bot stays responsive.
Progress and results are reported to the requesting admin.

ARCHITECTURE NOTE
-----------------
User DMs  → Telethon user client : personal account sends DMs to contacts directly.
                                    Requires shared-group history for entity resolution.
Group msgs → Telethon user client: the Telethon account joined those groups.
                                   The bot may not be a member of every group.
Bot API (aiogram) is used ONLY for admin notifications and progress messages.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramMigrateToChat,
)

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, ContactedUserRepository, LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)

DM_DELAY = 0.5        # seconds between DMs — 0.5s is safe under Bot API 30/s global limit
TG_DM_DELAY = 3.0    # seconds between Telethon user-account DMs (avoids FloodWait)
GROUP_DELAY = 2       # seconds between group sends
MAX_STORED_JOBS = 50
PER_CALL_TIMEOUT = 60 # max seconds for a single Telethon call (prevents FloodWait hang)
PROGRESS_EVERY = 25   # edit progress message every N users

_MAX_BROADCAST_SECONDS: int = 3600


@dataclass
class BroadcastJob:
    job_id: str
    target: str          # "groups" | "users"
    from_chat_id: int
    message_id: int
    actor_id: int
    bot: Any
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total: int = 0
    success: int = 0
    failed: int = 0
    blocked: int = 0
    deactivated: int = 0
    done: bool = False
    error: str | None = None
    first_user_error: str | None = None
    first_group_error: str | None = None
    message_text: str = ""
    media_file_id: str | None = None
    media_type: str | None = None
    is_forward: bool = False
    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None
    progress_message_id: int | None = None  # ID of the single live-edited progress message


class BroadcastQueueService:
    _instance: "BroadcastQueueService | None" = None

    def __init__(self) -> None:
        self._jobs: dict[str, BroadcastJob] = {}
        self._active: bool = False
        self._task: asyncio.Task | None = None

    @classmethod
    def get_instance(cls) -> "BroadcastQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_job(self, job_id: str) -> BroadcastJob | None:
        return self._jobs.get(job_id)

    def is_active(self) -> bool:
        return self._active

    def get_active_job(self) -> BroadcastJob | None:
        for job in self._jobs.values():
            if not job.done:
                return job
        return None

    def cancel_active(self) -> bool:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.warning("BroadcastQueueService: active task cancelled by admin")
            return True
        self._active = False
        return False

    async def start_broadcast(
        self,
        target: str,
        from_chat_id: int,
        message_id: int,
        actor_id: int,
        bot: Any,
        message_text: str = "",
        media_file_id: str | None = None,
        media_type: str | None = None,
        is_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_from_message_id: int | None = None,
    ) -> str:
        if self._active:
            raise RuntimeError("یک broadcast در حال اجرا است. لطفاً صبر کنید.")

        import uuid
        job_id = str(uuid.uuid4())[:8]
        job = BroadcastJob(
            job_id=job_id,
            target=target,
            from_chat_id=from_chat_id,
            message_id=message_id,
            actor_id=actor_id,
            bot=bot,
            message_text=message_text,
            media_file_id=media_file_id,
            media_type=media_type,
            is_forward=is_forward,
            forward_from_chat_id=forward_from_chat_id,
            forward_from_message_id=forward_from_message_id,
        )
        self._jobs[job_id] = job
        self._task = asyncio.create_task(self._run(job), name=f"broadcast-{job_id}")
        return job_id

    def _prune_old_jobs(self) -> None:
        done_jobs = [(jid, j) for jid, j in self._jobs.items() if j.done]
        if len(done_jobs) > MAX_STORED_JOBS:
            done_jobs.sort(key=lambda x: x[1].started_at)
            for jid, _ in done_jobs[:len(done_jobs) - MAX_STORED_JOBS]:
                del self._jobs[jid]

    async def _run(self, job: BroadcastJob) -> None:
        self._active = True
        try:
            coro = (
                self._send_to_groups(job)
                if job.target == "groups"
                else self._send_to_users(job)
            )
            await asyncio.wait_for(coro, timeout=_MAX_BROADCAST_SECONDS)
        except asyncio.CancelledError:
            job.error = "cancelled by admin"
            logger.warning("Broadcast job %s cancelled", job.job_id)
        except asyncio.TimeoutError:
            job.error = f"timed out after {_MAX_BROADCAST_SECONDS}s"
            logger.error("Broadcast job %s timed out", job.job_id)
        except Exception as exc:
            job.error = str(exc)
            logger.error("Broadcast job %s crashed: %s", job.job_id, exc, exc_info=True)
        finally:
            job.done = True
            self._active = False
            self._task = None
            self._prune_old_jobs()
            await self._report(job)

    # ══════════════════════════════════════════════════════════════════
    # USER BROADCAST — uses Telethon user client (personal account)
    # ══════════════════════════════════════════════════════════════════
    # Messages are sent from the personal Telethon account, not the bot.
    # Telethon resolves users via shared-group entity cache.
    # Users with no shared history → 'peer_not_found' (counted as deactivated).
    # ══════════════════════════════════════════════════════════════════

    async def _bot_send_to_user(self, job: BroadcastJob, user_id: int) -> tuple[bool, str | None]:
        """Send one message to a user via Bot API. Returns (ok, reason)."""
        bot = job.bot
        try:
            # ── Forward (preserves "Forwarded from …" header) ─────────────────────
            if job.is_forward and job.forward_from_chat_id and job.forward_from_message_id:
                await bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=job.forward_from_chat_id,
                    message_id=job.forward_from_message_id,
                )

            # ── Media via Bot API file_id (no download/re-upload needed) ──────────
            elif job.media_file_id and job.media_type:
                await self._bot_send_media(bot, user_id, job.media_file_id, job.media_type, job.message_text)

            # ── Plain text ────────────────────────────────────────────────────────
            elif job.message_text:
                await bot.send_message(chat_id=user_id, text=job.message_text)

            # ── Last resort: forward original message from admin's chat with bot ──
            else:
                await bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=job.from_chat_id,
                    message_id=job.message_id,
                )

            return True, None

        except TelegramRetryAfter as exc:
            # Bot API rate limit — respect it but cap at 120s
            wait = min(exc.retry_after, 120)
            logger.warning("Bot API RetryAfter for user %d: wait %ds", user_id, wait)
            await asyncio.sleep(wait)
            return False, f"retry_after_{exc.retry_after}s"

        except TelegramForbiddenError:
            # User blocked the bot
            return False, "blocked"

        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "chat not found" in msg or "user not found" in msg or "deactivated" in msg:
                return False, "deactivated"
            if "bot was blocked" in msg:
                return False, "blocked"
            logger.warning("TelegramBadRequest for user %d: %s", user_id, exc)
            return False, str(exc)[:120]

        except TelegramMigrateToChat:
            return False, "migrated"

        except Exception as exc:
            logger.error("Unexpected error sending to user %d: %s", user_id, exc)
            return False, str(exc)[:120]

    @staticmethod
    async def _bot_send_media(bot: Any, chat_id: int, file_id: str, media_type: str, caption: str) -> None:
        """Dispatch the right Bot API send method based on media_type."""
        kw: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            kw["caption"] = caption
        if media_type == "photo":
            await bot.send_photo(**kw, photo=file_id)
        elif media_type == "video":
            await bot.send_video(**kw, video=file_id)
        elif media_type == "document":
            await bot.send_document(**kw, document=file_id)
        elif media_type == "audio":
            await bot.send_audio(**kw, audio=file_id)
        elif media_type == "voice":
            await bot.send_voice(**kw, voice=file_id)
        elif media_type == "animation":
            await bot.send_animation(**kw, animation=file_id)
        elif media_type == "sticker":
            kw.pop("caption", None)  # stickers don't support captions
            await bot.send_sticker(**kw, sticker=file_id)
        elif media_type == "video_note":
            kw.pop("caption", None)
            await bot.send_video_note(**kw, video_note=file_id)
        else:
            # Fallback: treat as document
            await bot.send_document(**kw, document=file_id)

    async def _send_to_users(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        # Step 1: ALL private-chat users from the personal Telethon account dialogs
        # Ground truth: the account's own PV contacts, NOT bot starters.
        # Entities carry access_hash -> no spurious invalid_peer or deactivated errors.
        dialog_users = await tg.get_all_user_dialogs(limit=3000)
        dialog_user_ids = {u["user_id"] for u in dialog_users}

        # Step 2: DB active users not in live dialogs (fallback)
        async with AsyncSessionLocal() as session:
            repo = ContactedUserRepository(session)
            db_users = await repo.get_active(limit=10000)

        target_users: list[dict] = []
        for du in dialog_users:
            target_users.append({
                "user_id": du["user_id"],
                "username": du.get("username"),
                "first_name": du.get("first_name"),
            })
        for db_u in db_users:
            if db_u.user_id not in dialog_user_ids:
                target_users.append({
                    "user_id": db_u.user_id,
                    "username": db_u.username,
                    "first_name": db_u.first_name,
                })

        job.total = len(target_users)
        actor = str(job.actor_id)
        live_cnt = len(dialog_users)
        db_only_cnt = job.total - live_cnt
        logger.info(
            "Broadcast job %s: %d users (live_pvs=%d db_only=%d)",
            job.job_id, job.total, live_cnt, db_only_cnt,
        )

        if not target_users:
            logger.info("Broadcast job %s: no users to send to", job.job_id)
            return

        try:
            prog_msg = await job.bot.send_message(
                job.actor_id,
                f"⏳ <b>ارسال همگانی به مخاطبین شروع شد</b>\n\n"
                f"U0001f4e6 کاربران: <code>{job.total}</code> "
                f"(PV لایو: {live_cnt} | فقط DB: {db_only_cnt})\n"
                f"در حال ارسال از اکانت شخصی...",
                parse_mode="HTML",
            )
            job.progress_message_id = prog_msg.message_id
        except Exception as exc:
            logger.warning("Could not send initial progress message: %s", exc)

        for idx, user in enumerate(target_users):
            user_id = user["user_id"]
            ok, reason = await tg.send_dm_to_user(
                user_id=user_id,
                message_text=job.message_text,
                media_file_id=job.media_file_id,
                media_type=job.media_type,
                is_forward=job.is_forward,
                forward_from_chat_id=job.forward_from_chat_id,
                forward_from_message_id=job.forward_from_message_id,
                bot=job.bot,
            )

            if ok:
                job.success += 1
                await self._log("broadcast_user_sent", "success", actor, str(user_id))
            else:
                job.failed += 1
                if job.first_user_error is None:
                    job.first_user_error = f"uid={user_id}: {reason}"
                if reason == "blocked":
                    job.blocked += 1
                    await self._mark_user_blocked(user_id)
                elif reason in ("deactivated", "peer_not_found"):
                    job.deactivated += 1
                    await self._mark_user_blocked(user_id)
                elif reason == "peer_flood":
                    job.error = "پیر فلاد: اکانت موقتاً محدود شد. broadcast متوقف شد."
                    logger.error("Broadcast job %s stopped due to PeerFloodError", job.job_id)
                    await self._log("broadcast_user_failed", "error", actor, str(user_id), reason)
                    return
                elif reason and reason.startswith("flood_wait:"):
                    try:
                        wait_secs = int(reason.split(":")[1].rstrip("s"))
                    except Exception:
                        wait_secs = 60
                    logger.warning("FloodWait %ds -- sleeping before next user", wait_secs)
                    await asyncio.sleep(wait_secs)
                await self._log("broadcast_user_failed", "error", actor, str(user_id), reason)

            done_count = idx + 1
            if done_count % PROGRESS_EVERY == 0 and done_count < job.total:
                await self._send_progress(job, done_count)
            await asyncio.sleep(TG_DM_DELAY)

    async def _send_to_groups(self, job: BroadcastJob) -> None:
          from app.services.telegram_service import TelegramUserService
          tg = TelegramUserService.get_instance()

          # ── Step 1: Live Telethon dialogs — ALL groups the account is in ──────────
          # Ground truth. Entities carry access_hash → no invalid peer errors.
          dialog_groups = await tg.get_all_groups_from_dialogs(limit=3000)
          dialog_ids = {g["group_id"] for g in dialog_groups}

          # ── Step 2: DB-only joined groups not in live dialogs (edge cases) ────
          async with AsyncSessionLocal() as session:
              repo = GroupRepository(session)
              db_groups = await repo.get_joined()
          db_by_id = {g.group_id: g for g in db_groups}

          target_groups: list[dict] = []
          for dg in dialog_groups:
              db_g = db_by_id.get(dg["group_id"])
              target_groups.append({
                  "group_id": dg["group_id"],
                  "title": dg["title"],
                  "invite_link": db_g.invite_link if db_g else None,
                  "username": dg.get("username"),
              })
          for db_g in db_groups:
              if db_g.group_id not in dialog_ids:
                  target_groups.append({
                      "group_id": db_g.group_id,
                      "title": db_g.title,
                      "invite_link": db_g.invite_link,
                      "username": db_g.username,
                  })

          job.total = len(target_groups)
          actor = str(job.actor_id)
          live_cnt = len(dialog_groups)
          db_only_cnt = job.total - live_cnt
          logger.info(
              "Broadcast job %s: %d groups (live=%d db_only=%d)",
              job.job_id, job.total, live_cnt, db_only_cnt,
          )

          try:
              prog_msg = await job.bot.send_message(
                  job.actor_id,
                  f"⏳ <b>ارسال همگانی به گروه‌ها شروع شد</b>\n\n"
                  f"U0001f4e6 کل: <code>{job.total}</code> (لایو: {live_cnt} | فقط DB: {db_only_cnt})\nدر حال ارسال...",
                  parse_mode="HTML",
              )
              job.progress_message_id = prog_msg.message_id
          except Exception as exc:
              logger.warning("Could not send initial progress message: %s", exc)

          for idx, group in enumerate(target_groups):
              group_id = group["group_id"]
              group_link = group.get("invite_link") or (
                  f"@{group['username']}" if group.get("username") else None
              )
              ok, reason = await self._tg_call(
                  tg.forward_message_to_group(
                      group_id=group_id,
                      group_link=group_link,
                      message_text=job.message_text,
                      media_file_id=job.media_file_id,
                      media_type=job.media_type,
                      is_forward=job.is_forward,
                      forward_from_chat_id=job.forward_from_chat_id,
                      forward_from_message_id=job.forward_from_message_id,
                      bot=job.bot,
                  )
              )
              if ok:
                  job.success += 1
                  await self._log("broadcast_group_sent", "success", actor, str(group_id))
              else:
                  job.failed += 1
                  if job.first_group_error is None:
                      job.first_group_error = f"gid={group_id}: {reason}"
                  await self._log("broadcast_group_failed", "error", actor, str(group_id), reason)

              done_count = idx + 1
              if done_count % PROGRESS_EVERY == 0 and done_count < job.total:
                  await self._send_progress(job, done_count)
              await asyncio.sleep(GROUP_DELAY)

    # ══════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════

    async def _send_progress(self, job: BroadcastJob, done: int) -> None:
          """Edit the single live-progress message instead of flooding new ones."""
          try:
              pct = int(done / job.total * 100) if job.total else 0
              text = (
                  f"⏳ <b>ارسال همگانی در حال اجرا</b> ({pct}%)\n\n"
                  f"✅ موفق: <code>{job.success}</code>\n"
                  f"❌ ناموفق: <code>{job.failed}</code>\n"
                  f"📦 {done} از {job.total}"
              )
              if job.progress_message_id:
                  await job.bot.edit_message_text(
                      chat_id=job.actor_id,
                      message_id=job.progress_message_id,
                      text=text,
                      parse_mode="HTML",
                  )
              else:
                  msg = await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
                  job.progress_message_id = msg.message_id
          except Exception:
              pass
    async def _mark_user_blocked(self, user_id: int) -> None:
        try:
            async with AsyncSessionLocal() as s:
                from app.repositories import ContactedUserRepository as CUR
                r = CUR(s)
                u = await r.get_by_user_id(user_id)
                if u:
                    u.is_blocked = True
                await s.commit()
        except Exception as exc:
            logger.warning("Failed to mark user %d blocked: %s", user_id, exc)

    async def _log(
        self, action: str, result: str, actor: str,
        target: str, error: str | None = None
    ) -> None:
        try:
            async with AsyncSessionLocal() as session:
                log_repo = LogRepository(session)
                await log_repo.add(action=action, result=result, actor=actor, target=target, error_message=error)
                await session.commit()
        except Exception:
            pass

    async def _report(self, job: BroadcastJob) -> None:
        try:
            target_fa = "گروه‌ها" if job.target == "groups" else "کاربران"
            if job.error == "cancelled by admin":
                status = "🛑 لغو شد"
            elif job.error:
                status = "❌ با خطا متوقف شد"
            else:
                status = "✅ کامل شد"

            text = (
                f"📢 <b>ارسال همگانی {status}</b>\n\n"
                f"مقصد: {target_fa}\n"
                f"✅ موفق: <code>{job.success}</code>\n"
                f"❌ ناموفق: <code>{job.failed}</code>\n"
                f"🚫 بلاک: <code>{job.blocked}</code>\n"
                f"👻 غیرفعال: <code>{job.deactivated}</code>\n"
                f"📦 کل: <code>{job.total}</code>"
            )
            if job.error:
                text += f"\n\n⚠️ خطا: <code>{job.error[:200]}</code>"
            if job.first_user_error:
                text += f"\n\n🔍 اولین خطای کاربر: <code>{job.first_user_error[:200]}</code>"
            if job.first_group_error:
                text += f"\n\n🔍 اولین خطای گروه: <code>{job.first_group_error[:200]}</code>"

            # Edit the live-progress message with the final result
              if job.progress_message_id:
                  try:
                      await job.bot.edit_message_text(
                          chat_id=job.actor_id,
                          message_id=job.progress_message_id,
                          text=text,
                          parse_mode="HTML",
                      )
                  except Exception:
                      await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
              else:
                  await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
              for admin_id in settings.get_admin_id_list():
                if admin_id != job.actor_id:
                    try:
                        await job.bot.send_message(admin_id, text, parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Failed to send broadcast report: %s", exc)
