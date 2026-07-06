from datetime import datetime
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupStatus
from .base import BaseRepository


class GroupRepository(BaseRepository[Group]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Group, session)

    async def get_by_group_id(self, group_id: int) -> Group | None:
        result = await self._session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Group | None:
        result = await self._session.execute(
            select(Group).where(Group.username == username.lower())
        )
        return result.scalar_one_or_none()

    async def get_by_invite_link(self, invite_link: str) -> Group | None:
        result = await self._session.execute(
            select(Group).where(Group.invite_link == invite_link)
        )
        return result.scalar_one_or_none()

    async def get_by_status(self, status: GroupStatus, limit: int = 50) -> list[Group]:
        result = await self._session.execute(
            select(Group)
            .where(Group.status == status)
            .order_by(Group.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_status(self, status: GroupStatus) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Group).where(Group.status == status)
        )
        return result.scalar_one()

    async def upsert(self, group_id: int, **kwargs: object) -> tuple[Group, bool]:
        existing = await self.get_by_group_id(group_id)
        created = False
        if existing is None:
            existing = Group(group_id=group_id, **kwargs)
            self._session.add(existing)
            created = True
        else:
            for key, value in kwargs.items():
                setattr(existing, key, value)
        await self._session.flush()
        await self._session.refresh(existing)
        return existing, created

    async def get_latest(self, limit: int = 10, offset: int = 0) -> list[Group]:
        result = await self._session.execute(
            select(Group)
            .order_by(Group.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_joined(self) -> list[Group]:
        return await self.get_by_status(GroupStatus.JOINED, limit=5000)

    async def get_all(self, limit: int = 100000, offset: int = 0) -> list[Group]:
        result = await self._session.execute(
            select(Group).order_by(Group.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def mark_left_not_in(self, active_group_ids: set[int]) -> int:
        """Mark as LEFT every group whose status is JOINED but whose group_id is
        NOT in active_group_ids (the live Telethon dialog list).

        Call this after every dialog sync so 'joined' always reflects exactly
        the groups the account is currently a member of — no stale/left
        groups inflate broadcast counts or stats. Returns rows updated.
        """
        if not active_group_ids:
            # Safety guard: never wipe everyone if the live list came back empty
            # (e.g. transient Telethon failure) — avoids mass-mislabeling groups.
            return 0

        result = await self._session.execute(
            update(Group)
            .where(Group.status == GroupStatus.JOINED)
            .where(Group.group_id.not_in(active_group_ids))
            .values(status=GroupStatus.LEFT)
        )
        return result.rowcount

    async def get_by_status_paged(
        self, status: "GroupStatus", limit: int = 15, offset: int = 0
    ) -> list[Group]:
        """DB-level paginated fetch for a given status (avoids loading all rows)."""
        result = await self._session.execute(
            select(Group)
            .where(Group.status == status)
            .order_by(Group.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
