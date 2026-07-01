"""
Sequential join queue with random jitter and daily rate-limit.
All discovered group links pass through this queue — never joined in parallel.

Key guarantees:
  - PENDING groups from DB are reloaded into the queue on every startup,
    so a bot restart never loses groups that were waiting to be joined.
  - The worker loop is self-healing: if an inner error kills the consume
    loop, the outer wrapper restarts it after a short delay.
  - JoinQueueService is a singleton; HealthService can watch its worker task.
"""
import asyncio
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.repositories.join_attempt_repository import JoinAttemptRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class JoinTask:
    group_id: int
    link: str
    title: str | None
    attempt_number: int = 1


class JoinQueueService:
    """Single-consumer FIFO queue for joining Telegram groups."""

    _instance: "JoinQueueService | None" = None

    def __init__(self) -> None:
        self._queue: asyncio.Queue[JoinTask] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._tg: Any = None
        # Track IDs already in queue to avoid duplicate requeue on reload
        self._queued_ids: set[int] = set()

    @classmethod
    def get_instance(cls) -> "JoinQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def enqueue(self, group_id: int, link: str, title: str | None, attempt: int = 1) -> None:
        if group_id in self._queued_ids:
            logger.debug("Group %d already in queue — skipping duplicate enqueue", group_id)
            return
        task = JoinTask(group_id=group_id, link=link, title=title, attempt_number=attempt)
        self._queued_ids.add(group_id)
        await self._queue.put(task)
        logger.info("Queued join task: group_id=%d title=%r queue_size=%d", group_id, title, self._queue.qsize())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # ── Critical fix: reload any PENDING groups from DB on every startup ──
        # The asyncio.Queue is in-memory. When the bot restarts (deploy, crash,
        # Render restart), groups with status=PENDING in the DB would otherwise
        # sit idle forever. This reload restores them to the queue automatically.
        await self._reload_pending_from_db()

        self._worker_task = asyncio.create_task(self._worker(), name="join-queue-worker")
        logger.info("Join queue worker started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Join queue worker stopped")

    def queue_size(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Startup reload
    # ------------------------------------------------------------------

    async def _reload_pending_from_db(self) -> None:
        """Load all PENDING groups from DB into the in-memory queue.

        Safe to call repeatedly — uses _queued_ids to skip duplicates.
        Groups with no invite_link are skipped (cannot join without a link).
        """
        try:
            async with AsyncSessionLocal() as session:
                repo = GroupRepository(session)
                pending = await repo.get_by_status(GroupStatus.PENDING, limit=1000)

            loaded = 0
            for group in pending:
                if not group.invite_link:
                    logger.debug(
                        "PENDING group %d has no invite_link — cannot auto-join, skipping",
                        group.group_id,
                    )
                    continue
                if group.group_id in self._queued_ids:
                    continue
                task = JoinTask(
                    group_id=group.group_id,
                    link=group.invite_link,
                    title=group.title,
                    attempt_number=1,
                )
                self._queued_ids.add(group.group_id)
                await self._queue.put(task)
                loaded += 1

            if loaded:
                logger.info(
                    "Reloaded %d PENDING group(s) from DB into join queue on startup",
                    loaded,
                )
            else:
                logger.info("No PENDING groups to reload — queue starts empty")
        except Exception as exc:
            logger.error("Failed to reload PENDING groups from DB: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Self-healing worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """Self-healing outer loop. If _consume_loop crashes, restart after delay."""
        while self._running:
            try:
                await self._consume_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Join queue consume loop crashed — restarting in 5s: %s",
                    exc, exc_info=True,
                )
                await asyncio.sleep(5)

    async def _consume_loop(self) -> None:
        """Inner FIFO consumer — processes one task at a time."""
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise  # propagate so _worker breaks cleanly on shutdown

            try:
                await self._process(task)
            except Exception as exc:
                logger.error("Join worker unhandled error for group %d: %s", task.group_id, exc, exc_info=True)
            finally:
                self._queued_ids.discard(task.group_id)
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Core join logic
    # ------------------------------------------------------------------

    async def _process(self, task: JoinTask) -> None:
        if self._tg is None:
            logger.error(
                "TelegramUserService not set on JoinQueueService — dropping task for group_id=%d",
                task.group_id,
            )
            return

        # Check daily rate limit
        async with AsyncSessionLocal() as session:
            attempt_repo = JoinAttemptRepository(session)
            today_count = await attempt_repo.count_today()

        if today_count >= settings.MAX_JOINS_PER_DAY:
            logger.warning(
                "Daily join limit (%d) reached — requeueing group_id=%d for later",
                settings.MAX_JOINS_PER_DAY,
                task.group_id,
            )
            await asyncio.sleep(300)
            # Re-add to queue (re-register in _queued_ids too)
            self._queued_ids.add(task.group_id)
            await self._queue.put(task)
            return

        # Exact 7-minute delay between joins (anti-detection, configurable via env)
        delay = random.uniform(settings.JOIN_DELAY_MIN, settings.JOIN_DELAY_MAX)
        logger.info(
            "Waiting %.0fs (%.1f min) before joining group_id=%d (%r)",
            delay,
            delay / 60,
            task.group_id,
            task.title,
        )
        await asyncio.sleep(delay)

        success, real_group_id, join_error = await self._tg.join_group(task.link)

        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)
            attempt_repo = JoinAttemptRepository(session)

            group = await group_repo.get_by_group_id(task.group_id)

            if success and real_group_id and real_group_id != task.group_id:
                existing_real = await group_repo.get_by_group_id(real_group_id)
                if existing_real is None and group is not None:
                    try:
                        group.group_id = real_group_id
                        await session.flush()
                        logger.info(
                            "Updated placeholder group_id %d → real group_id %d",
                            task.group_id,
                            real_group_id,
                        )
                    except Exception as exc:
                        logger.warning("Could not update placeholder group_id: %s", exc)

            await attempt_repo.add(
                group_id=real_group_id or task.group_id,
                invite_link=task.link,
                attempt_number=task.attempt_number,
                success=success,
                error=join_error,
            )

            if group:
                if success:
                    group.status = GroupStatus.JOINED
                    group.join_date = datetime.now(timezone.utc)
                    await log_repo.add(
                        action="group_joined",
                        result="success",
                        target=task.link,
                        details=(
                            f"group_id={real_group_id or task.group_id} "
                            f"title={task.title!r} attempt={task.attempt_number}"
                        ),
                    )
                    logger.info(
                        "✅ Joined group_id=%d (%r)", real_group_id or task.group_id, task.title
                    )
                else:
                    group.status = GroupStatus.FAILED
                    await log_repo.add(
                        action="group_join_failed",
                        result="error",
                        target=task.link,
                        details=(
                            f"group_id={task.group_id} "
                            f"title={task.title!r} attempt={task.attempt_number}"
                        ),
                    )
                    logger.warning(
                        "❌ Failed to join group_id=%d (%r)", task.group_id, task.title
                    )

            await session.commit()

        if settings.get_admin_id_list():
            await self._notify_admins(task, success)

    async def _notify_admins(self, task: JoinTask, success: bool) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            emoji = "✅" if success else "❌"
            title = task.title or str(task.group_id)
            await ns.notify(
                f"{emoji} <b>عضویت {'موفق' if success else 'ناموفق'}</b>\n"
                f"گروه: <code>{title}</code>\n"
                f"شناسه: <code>{task.group_id}</code>\n"
                f"تلاش: <code>{task.attempt_number}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
