"""
Periodic health monitoring for the Telegram user client.
Detects soft-bans, disconnects, and session issues — alerts admins immediately.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL = 60          # seconds between checks
SOFT_BAN_THRESHOLD = 3       # consecutive failures before alert
MESSAGE_TIMEOUT = 15         # seconds to wait for Telegram API response


class HealthService:
    _instance: "HealthService | None" = None

    def __init__(self) -> None:
        self._tg: Any = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_ok: datetime | None = None
        self._alerted = False

    @classmethod
    def get_instance(cls) -> "HealthService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def start(self) -> None:
        if self._running:
            return
        # Reset alert state on (re)start so a newly-restarted service
        # will re-alert if it continues to fail.
        self._alerted = False
        self._consecutive_failures = 0
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="health-monitor")
        logger.info("Health monitor started (interval=%ds)", CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def is_healthy(self) -> bool:
        return self._consecutive_failures < SOFT_BAN_THRESHOLD

    def last_ok_at(self) -> datetime | None:
        return self._last_ok

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(CHECK_INTERVAL)
            await self._check()

    async def _check(self) -> None:
        if self._tg is None:
            return

        try:
            if not self._tg.is_running():
                raise RuntimeError("User client is not running")

            # Ping Telegram — get_me() is the lightest possible API call
            me = await asyncio.wait_for(
                self._tg.client.get_me(),
                timeout=MESSAGE_TIMEOUT,
            )
            if me is None:
                raise RuntimeError("get_me() returned None")

            self._consecutive_failures = 0
            self._last_ok = datetime.now(timezone.utc)
            self._alerted = False

        except Exception as exc:
            self._consecutive_failures += 1
            logger.warning(
                "Health check failed (consecutive=%d): %s",
                self._consecutive_failures, exc,
            )

            await self._log_failure(str(exc))

            if self._consecutive_failures >= SOFT_BAN_THRESHOLD and not self._alerted:
                self._alerted = True
                await self._alert(str(exc))

    async def _log_failure(self, error: str) -> None:
        try:
            async with AsyncSessionLocal() as session:
                log_repo = LogRepository(session)
                await log_repo.add(
                    action="health_check_failed",
                    result="error",
                    error_message=error,
                    details=f"consecutive_failures={self._consecutive_failures}",
                )
                await session.commit()
        except Exception:
            pass

    async def _alert(self, error: str) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify_critical(
                "⚠️ User Client ناسالم",
                f"تعداد خطاهای متوالی: <code>{self._consecutive_failures}</code>\n"
                f"خطا: <code>{error[:200]}</code>\n\n"
                "ربات ممکن است soft-ban شده باشد یا session منقضی شده.",
            )
        except Exception:
            pass
