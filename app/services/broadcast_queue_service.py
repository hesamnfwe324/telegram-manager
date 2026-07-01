"""
Non-blocking background broadcast service.
Broadcasts run in a dedicated asyncio task — the bot stays responsive.
Progress and results are reported to the requesting admin.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, ContactedUserRepository, LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)

DM_DELAY = 5          # seconds between DMs
GROUP_DELAY = 2       # seconds between group sends
MAX_STORED_JOBS = 50  # keep only last N completed jobs
PER_CALL_TIMEOUT = 90 # max seconds for a single Telethon send call (prevents FloodWait hang)
PROGRESS_EVERY = 50   # send a progress update to admin every N users

# Hard ceiling — 1 hour gives room for 700+ users even with retries.
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
    first_group_error: str | None = None
    message_text: str = ""
    media_file_id: str | None = None
    media_type: str | None = None
    is_forward: bool = False
    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None


class BroadcastQueueService:
    _instance: "BroadcastQueueService | None" = None

    def __init__(self) -> None:
        self._jobs: dict[str, BroadcastJob] = {}
        self._active: bool = False
        self._task: asyncio.Task | None = None  # reference for real cancellation

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
        """Return the currently running job, or None."""
        for job in self._jobs.values():
            if not job.done:
                return job
        return None

    def cancel_active(self) -> bool:
        """Cancel the running broadcast task immediately.

        Returns True if a task was found and cancelled, False if nothing was running.
        """
        if self._task and not self._task.done():
            self._task.cancel()
            logger.warning("BroadcastQueueService: active task cancelled by admin")
            return True
        # No running task — just reset the flag
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
            logger.warning("Broadcast job %s was cancelled", job.job_id)
        except asyncio.TimeoutError:
            job.error = f"broadcast timed out after {_MAX_BROADCAST_SECONDS}s"
            logger.error("Broadcast job %s timed out", job.job_id)
        except Exception as exc:
            job.error = str(exc)
            logger.error("Broadcast job %s failed: %s", job.job_id, exc, exc_info=True)
        finally:
            job.done = True
            self._active = False
            self._task = None
            self._prune_old_jobs()
            await self._report(job)

    async def _safe_call(self, coro) -> tuple[bool, str | None]:
        """Wrap a Telethon call with a per-call timeout.

        Prevents a single FloodWait (potentially hours long) from freezing the
        entire broadcast loop.  If the call takes longer than PER_CALL_TIMEOUT
        seconds, we cancel it and mark that user/group as 'timeout' failure.
        """
        try:
            return await asyncio.wait_for(coro, timeout=PER_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Telethon call timed out after %ds", PER_CALL_TIMEOUT)
            return False, "per_call_timeout"
        except asyncio.CancelledError:
            raise  # propagate cancellation so _run() can handle it

    async def _send_to_groups(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        await tg.refresh_dialogs(limit=500)

        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            groups = await repo.get_joined()

        job.total = len(groups)
        actor = str(job.actor_id)

        for group in groups:
            group_link = group.invite_link or (
                f"@{group.username}" if group.username else None
            )
            ok, reason = await self._safe_call(
                tg.forward_message_to_group(
                    group_id=group.group_id,
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
                await self._log("broadcast_group_sent", "success", actor, str(group.group_id))
            else:
                job.failed += 1
                if job.first_group_error is None:
                    job.first_group_error = f"{group.group_id}: {reason}"
                await self._log("broadcast_group_failed", "error", actor, str(group.group_id), reason)
            await asyncio.sleep(GROUP_DELAY)

    async def _send_to_users(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        async with AsyncSessionLocal() as session:
            repo = ContactedUserRepository(session)
            users = await repo.get_active(limit=10000)

        job.total = len(users)
        actor = str(job.actor_id)

        for idx, user in enumerate(users):
            # ── Priority 1: True forward — original chat + message_id ────────────────
            if job.is_forward and job.forward_from_chat_id and job.forward_from_message_id:
                ok, reason = await self._safe_call(
                    tg.forward_message_to_user(
                        user_id=user.user_id,
                        from_chat_id=job.forward_from_chat_id,
                        message_id=job.forward_from_message_id,
                    )
                )

            # ── Priority 2: Media ─────────────────────────────────────────────────────
            elif job.media_file_id and job.media_type:
                ok, reason = await self._safe_call(
                    tg.send_media_to_user(
                        user_id=user.user_id,
                        media_file_id=job.media_file_id,
                        media_type=job.media_type,
                        caption=job.message_text,
                        bot=job.bot,
                    )
                )

            # ── Priority 3: Plain text ────────────────────────────────────────────────
            # CRITICAL: Do NOT use forward_message_to_user here — Telethon user client
            # cannot access the bot's private chat to forward from it.
            elif job.message_text:
                ok, reason = await self._safe_call(
                    tg.send_message_to_user(
                        user_id=user.user_id,
                        message=job.message_text,
                    )
                )

            # ── Priority 4: Last resort forward ──────────────────────────────────────
            else:
                ok, reason = await self._safe_call(
                    tg.forward_message_to_user(
                        user_id=user.user_id,
                        from_chat_id=job.from_chat_id,
                        message_id=job.message_id,
                    )
                )

            if ok:
                job.success += 1
                await self._log("broadcast_user_sent", "success", actor, str(user.user_id))
            else:
                job.failed += 1
                if reason == "blocked":
                    job.blocked += 1
                    await self._mark_user_blocked(user.user_id)
                elif reason == "deactivated":
                    job.deactivated += 1
                    await self._mark_user_blocked(user.user_id)
                await self._log("broadcast_user_failed", "error", actor, str(user.user_id), reason)

            # Periodic progress report every PROGRESS_EVERY users
            done_count = idx + 1
            if done_count % PROGRESS_EVERY == 0 and done_count < job.total:
                await self._send_progress(job, done_count)

            await asyncio.sleep(DM_DELAY)

    async def _send_progress(self, job: BroadcastJob, done: int) -> None:
        try:
            pct = int(done / job.total * 100)
            text = (
                f"⏳ <b>ارسال همگانی در حال اجرا</b> ({pct}%)\n\n"
                f"✅ {job.success} / ❌ {job.failed} / 📦 {done} از {job.total}"
            )
            await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
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
            if job.first_group_error:
                text += f"\n\n🔍 خطای گروه: <code>{job.first_group_error[:300]}</code>"

            await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
            for admin_id in settings.get_admin_id_list():
                if admin_id != job.actor_id:
                    try:
                        await job.bot.send_message(admin_id, text, parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Failed to send broadcast report: %s", exc)
