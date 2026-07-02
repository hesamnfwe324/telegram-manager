import asyncio
import os
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import text

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
    JoinApprovalWatcher,
    ForcedSubscribeService,
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
            await session.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as exc:
        logger.error("Database connection FAILED at startup: %s", exc)
        sys.exit(1)


async def _init_db() -> None:
    """Create all tables, migrating stale schema when necessary.

    Uses separate connections so a failed schema-check SELECT does not
    abort the DDL transaction that follows (asyncpg marks a connection as
    failed after any error inside a transaction block).
    """
    # ── Step 1: probe schema in its own connection ───────────────────────────
    needs_reset = False
    async with engine.connect() as conn:
        try:
            await conn.execute(text("SELECT group_id FROM groups LIMIT 0"))
            logger.info("DB schema is current — no migration needed")
        except Exception:
            logger.warning(
                "Stale DB schema detected — will wipe all public tables "
                "and enum types, then recreate with current models"
            )
            needs_reset = True
        # connection rolls back / closes automatically

    # ── Step 2: nuclear wipe (fresh connection, raw SQL + CASCADE) ───────────
    if needs_reset:
        async with engine.begin() as conn:
            # Drop ALL tables in public schema (CASCADE handles FK chains)
            await conn.execute(text("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN (
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                    ) LOOP
                        EXECUTE 'DROP TABLE IF EXISTS '
                            || quote_ident(r.tablename)
                            || ' CASCADE';
                    END LOOP;
                END $$
            """))
            logger.info("All public tables dropped (CASCADE)")

            # Drop all enum types in public schema
            await conn.execute(text("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN (
                        SELECT t.typname
                        FROM pg_type t
                        JOIN pg_namespace n ON n.oid = t.typnamespace
                        WHERE t.typtype = 'e'
                          AND n.nspname = 'public'
                    ) LOOP
                        EXECUTE 'DROP TYPE IF EXISTS '
                            || quote_ident(r.typname)
                            || ' CASCADE';
                    END LOOP;
                END $$
            """))
            logger.info("All public enum types dropped")

    # ── Step 3: create fresh tables ──────────────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


async def _build_storage():
    """Build FSM storage — Redis if available, MemoryStorage as fallback."""
    if settings.redis_enabled:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(settings.REDIS_URL)
            await storage.redis.ping()
            logger.info("Redis FSM storage connected: %s", settings.REDIS_URL.split("@")[-1])
            return storage
        except ImportError:
            logger.warning("Redis storage package not available — falling back to MemoryStorage")
        except Exception as exc:
            logger.warning(
                "Redis not reachable (%s) — falling back to MemoryStorage. "
                "FSM states will be lost on restart.",
                exc,
            )
    from aiogram.fsm.storage.memory import MemoryStorage
    logger.warning(
        "Using in-memory FSM storage — states lost on restart. "
        "Set a reachable REDIS_URL for production."
    )
    return MemoryStorage()


async def _http_health_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Respond to any HTTP request with 200 OK — used by Render health checks."""
    try:
        await asyncio.wait_for(reader.read(4096), timeout=5.0)
    except Exception:
        pass
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"OK"
    )
    try:
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def _start_health_server() -> asyncio.Server:
    """Start a minimal HTTP server so Render web service health checks pass."""
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(
        _http_health_handler, "0.0.0.0", port
    )
    logger.info("Health server listening on port %d", port)
    return server


async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Group Manager v2")

    if not settings.get_admin_id_list():
        logger.warning(
            "ADMIN_IDS is not set or empty — no one will be able to use the bot! "
            "Set ADMIN_IDS to a comma-separated list of Telegram user IDs."
        )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, loop)

    # Start HTTP health server first so Render marks the service healthy immediately
    health_http_server = await _start_health_server()

    await _check_db()
    await _init_db()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = await _build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())

    ns = NotificationService.get_instance()
    ns.set_bot(bot)

    tg = TelegramUserService.get_instance()
    jq = JoinQueueService.get_instance()
    jq.set_tg_service(tg)

    health = HealthService.get_instance()
    health.set_tg_service(tg)
    health.set_join_queue(jq)   # enables worker-crash watchdog

    approval_watcher = JoinApprovalWatcher.get_instance()
    approval_watcher.set_tg_service(tg)

    forced_subscribe = ForcedSubscribeService.get_instance()
    forced_subscribe.set_tg_service(tg)

    scheduler = SchedulerService.get_instance()
    scheduler.set_bot(bot)

    async def _start_client_safe() -> None:
        try:
            await tg.start()
            discovery = DiscoveryService(tg)
            tg.on_new_message(discovery.process_message)
            tg.on_new_message(forced_subscribe.process_message)
            await approval_watcher.start()   # watch for approved join requests
            await jq.start()
            await health.start()
            logger.info("User client, join queue, approval watcher, and health monitor started")
        except RuntimeError as exc:
            logger.error("User client startup failed: %s", exc)
            await ns.notify_critical("User Client ناموفق", str(exc)[:300])
        except Exception as exc:
            logger.error("Unexpected startup error: %s", exc, exc_info=True)

    asyncio.create_task(_start_client_safe())
    scheduler.start()

    # Delete any stale webhook so polling does not conflict with a previous
    # Render deployment that set a webhook or left a getUpdates session open.
    # This must run BEFORE start_polling or Telegram returns ConflictError.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Webhook cleared — ready for polling")
    except Exception as _wh_exc:
        logger.warning("Could not clear webhook (non-fatal): %s", _wh_exc)

    logger.info("Bot polling started")
    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
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

    health_http_server.close()
    await health_http_server.wait_closed()

    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
