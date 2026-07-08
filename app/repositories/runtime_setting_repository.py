from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.runtime_setting import RuntimeSetting
from .base import BaseRepository


class RuntimeSettingRepository(BaseRepository[RuntimeSetting]):
    """Singleton-row repository (id=1) for admin-adjustable runtime settings."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(RuntimeSetting, session)

    async def get_or_create(self) -> RuntimeSetting:
        """Return the singleton row, creating it from env defaults if missing."""
        result = await self._session.execute(select(RuntimeSetting).limit(1))
        row = result.scalar_one_or_none()
        if row is None:
            row = RuntimeSetting(
                join_delay_min=settings.JOIN_DELAY_MIN,
                join_delay_max=settings.JOIN_DELAY_MAX,
            )
            await self.save(row)
            await self._session.commit()
        return row

    async def update_join_delay(self, delay_min: int, delay_max: int) -> RuntimeSetting:
        row = await self.get_or_create()
        row.join_delay_min = delay_min
        row.join_delay_max = delay_max
        await self._session.flush()
        await self._session.commit()
        await self._session.refresh(row)
        return row
