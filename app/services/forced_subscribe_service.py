"""
Forced-Subscribe service — DISABLED.

این سرویس کاملاً غیرفعال شده است.
ربات دیگر برای عضویت در گروه‌ها نیازی به عضویت در کانال‌ها ندارد.
"""
from app.utils.logger import get_logger

logger = get_logger(__name__)

_ENABLED = False  # Forced-subscribe detection is disabled


class ForcedSubscribeService:
    _instance: "ForcedSubscribeService | None" = None

    @classmethod
    def get_instance(cls) -> "ForcedSubscribeService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: object) -> None:
        pass  # disabled

    async def start(self) -> None:
        logger.info("ForcedSubscribeService is disabled — not starting")

    async def stop(self) -> None:
        pass  # disabled

    async def check_after_join(self, group_id: int, group_title: str | None) -> list:
        return []  # disabled

    async def handle_write_forbidden(self, group_id: int) -> list:
        return []  # disabled

    async def process_message(self, event: object) -> None:
        return  # disabled
