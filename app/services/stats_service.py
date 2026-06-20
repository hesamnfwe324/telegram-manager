from dataclasses import dataclass
from datetime import datetime, timezone

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, DiscoveredLinkRepository, LogRepository, ContactedUserRepository
from app.models.group import GroupStatus
from app.models.discovered_link import LinkStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SystemStats:
    total_groups: int
    pending_groups: int
    joined_groups: int
    failed_groups: int
    total_links: int
    pending_links: int
    total_logs: int
    total_contacted_users: int
    join_queue_size: int
    client_healthy: bool
    last_activity: datetime | None
    last_group_title: str | None
    generated_at: datetime


class StatsService:
    async def get_stats(self) -> SystemStats:
        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            link_repo = DiscoveredLinkRepository(session)
            log_repo = LogRepository(session)
            user_repo = ContactedUserRepository(session)

            total_groups = await group_repo.count()
            pending_groups = await group_repo.count_by_status(GroupStatus.PENDING)
            joined_groups = await group_repo.count_by_status(GroupStatus.JOINED)
            failed_groups = await group_repo.count_by_status(GroupStatus.FAILED)
            total_links = await link_repo.count()
            pending_links = await link_repo.count_by_status(LinkStatus.PENDING)
            total_logs = await log_repo.count()
            total_contacted_users = await user_repo.count()
            last_log = await log_repo.get_last_activity()
            last_activity = last_log.timestamp if last_log else None
            latest_groups = await group_repo.get_latest(1)
            last_group_title = latest_groups[0].title if latest_groups else None

        try:
            from app.services.join_queue_service import JoinQueueService
            from app.services.health_service import HealthService
            join_queue_size = JoinQueueService.get_instance().queue_size()
            client_healthy = HealthService.get_instance().is_healthy()
        except Exception:
            join_queue_size = 0
            client_healthy = True

        return SystemStats(
            total_groups=total_groups,
            pending_groups=pending_groups,
            joined_groups=joined_groups,
            failed_groups=failed_groups,
            total_links=total_links,
            pending_links=pending_links,
            total_logs=total_logs,
            total_contacted_users=total_contacted_users,
            join_queue_size=join_queue_size,
            client_healthy=client_healthy,
            last_activity=last_activity,
            last_group_title=last_group_title,
            generated_at=datetime.now(timezone.utc),
        )
