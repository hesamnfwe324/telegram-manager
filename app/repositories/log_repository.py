from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.log import Log
from .base import BaseRepository


class LogRepository(BaseRepository[Log]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Log, session)

    async def add(
        self,
        action: str,
        result: str = "success",
        error_message: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        details: str | None = None,
    ) -> Log:
        record = Log(
            timestamp=datetime.now(timezone.utc),
            action=action,
            result=result,
            error_message=error_message,
            actor=actor,
            target=target,
            details=details,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_recent(self, limit: int = 50) -> list[Log]:
        result = await self._session.execute(
            select(Log).order_by(Log.timestamp.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def get_errors(self, limit: int = 50) -> list[Log]:
        result = await self._session.execute(
            select(Log)
            .where(Log.result == "error")
            .order_by(Log.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_last_activity(self) -> Log | None:
        result = await self._session.execute(
            select(Log).order_by(Log.timestamp.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Log)
        )
        return result.scalar_one()
