import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SendResult:
    group_id: int
    group_title: str | None
    success: bool
    error: str | None = None


@dataclass
class BroadcastResult:
    total: int
    succeeded: int
    failed: int
    results: list[SendResult] = field(default_factory=list)


class MessageService:
    DELAY_BETWEEN_SENDS = 2.0

    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service

    async def send_to_groups(
        self,
        group_ids: list[int],
        content: Any,
        actor: str = "admin",
    ) -> BroadcastResult:
        """Send a message to multiple groups.

        The DB session is intentionally kept short — we resolve group titles
        upfront, then close the session before doing Telegram API calls.
        Holding a session open during asyncio.sleep() wastes connection pool
        slots and risks timeouts on busy deployments.
        """
        # Resolve group titles in one short-lived session
        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            groups = {gid: await group_repo.get_by_group_id(gid) for gid in group_ids}

        titles: dict[int, str | None] = {
            gid: (g.title if g else str(gid)) for gid, g in groups.items()
        }

        results: list[SendResult] = []
        for group_id in group_ids:
            title = titles.get(group_id)
            success = await self._tg.send_message_to_group(group_id, content)
            results.append(SendResult(group_id=group_id, group_title=title, success=success))
            await asyncio.sleep(self.DELAY_BETWEEN_SENDS)

        # Write all log entries in a single session after sending is complete
        async with AsyncSessionLocal() as session:
            log_repo = LogRepository(session)
            for r in results:
                await log_repo.add(
                    action="message_sent",
                    result="success" if r.success else "error",
                    actor=actor,
                    target=str(r.group_id),
                    details=f"title={r.group_title!r}",
                )
            await session.commit()

        succeeded = sum(1 for r in results if r.success)
        return BroadcastResult(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
        )

    async def get_joined_groups(self) -> list[Any]:
        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            return await group_repo.get_joined()
