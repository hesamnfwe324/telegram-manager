import asyncio
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.utils.logger import get_logger, setup_logging
from app.database.connection import engine, Base, AsyncSessionLocal
from app.handlers import get_main_router
from app.middlewares import AdminAuthMiddleware
from app.services import (
    TelegramUserService,
    DiscoveryService,
    JoinQueueService,
    NotificationService,
    HealthService,
    SchedulerService,
)

logger = get_logger(__name__)
_shutdown_event = asyncio.Event()


def _request_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received — initiating graceful shutdown")
    loop.call_soon_threadsafe(_shutdown_event.set)


async def _check_db() -> None:
    """Fail fast if the database is unreachable on startup."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as exc:
        logger.error("Database connection FAILED at startup: %s", exc)
        sys.exit(1)


async def _init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


def _build_storage():
    if settings.redis_enabled:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(settings.REDIS_URL)
            logger.info("Using Redis FSM storage: %s", settings.REDIS_URL.split("@")[-1])
            return storage
        except ImportError:
            logger.warning("aiogram-redis-provider not installed — falling back to MemoryStorage")
    from aiogram.fsm.storage.memory import MemoryStorage
    logger.warning("Using in-memory FSM storage — states lost on restart. Set REDIS_URL for production.")
    return MemoryStorage()


async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Group Manager v2")

    if not settings.get_admin_id_list():
        logger.warning(
            "ADMIN_IDS is not set or empty — no one will be able to use the bot! "
            "Set ADMIN_IDS to a comma-separated list of Telegram user IDs."
        )

    # Register signal handlers using the running event loop for thread-safety
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, loop)

    await _check_db()
    await _init_db()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = _build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())

    # Wire up singleton services
    ns = NotificationService.get_instance()
    ns.set_bot(bot)

    tg = TelegramUserService.get_instance()
    jq = JoinQueueService.get_instance()
    jq.set_tg_service(tg)

    health = HealthService.get_instance()
    health.set_tg_service(tg)

    scheduler = SchedulerService.get_instance()
    scheduler.set_bot(bot)

    # Start user client (non-fatal if session not yet configured)
    async def _start_client_safe() -> None:
        try:
            await tg.start()
            discovery = DiscoveryService(tg)
            tg.on_new_message(discovery.process_message)
            await jq.start()
            await health.start()
            logger.info("User client, join queue, and health monitor started")
        except RuntimeError as exc:
            logger.error("User client startup failed: %s", exc)
            await ns.notify_critical("User Client ناموفق", str(exc)[:300])
        except Exception as exc:
            logger.error("Unexpected startup error: %s", exc, exc_info=True)

    asyncio.create_task(_start_client_safe())
    scheduler.start()

    # Run bot polling until shutdown signal
    logger.info("Bot polling started")
    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    done, pending = await asyncio.wait(
        [polling_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated — cleaning up")
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    scheduler.stop()
    await health.stop()
    await jq.stop()
    await tg.stop()
    await bot.session.close()
    await engine.dispose()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
