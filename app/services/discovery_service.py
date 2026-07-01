import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, DiscoveredLinkRepository, LogRepository, ContactedUserRepository
from app.models.group import GroupStatus
from app.models.discovered_link import LinkStatus
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator

logger = get_logger(__name__)


def _placeholder_group_id(link: str) -> int:
    """Stable negative placeholder ID for private invite links before joining."""
    h = int(hashlib.md5(link.encode()).hexdigest()[:10], 16) % (10 ** 9)
    return -h


class DiscoveryService:
    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service

    async def process_message(self, event: Any) -> None:
        try:
            text = event.message.text or ""
            sender_id = event.sender_id

            if sender_id and sender_id > 0:
                await self._track_user(event)

            keywords = settings.get_discovery_keywords()
            if keywords:
                text_lower = text.lower()
                if not any(kw in text_lower for kw in keywords):
                    return

            links = LinkValidator.extract_links(text)

            if sender_id:
                try:
                    bio = await self._tg.get_user_bio(sender_id)
                    if bio:
                        if not keywords or any(kw in bio.lower() for kw in keywords):
                            links += LinkValidator.extract_links(bio)
                except Exception:
                    pass

            if not links:
                return

            source = f"message:{event.chat_id}:{event.message.id}"
            for raw_link in set(links):
                normalized = LinkValidator.normalize(raw_link)
                if normalized:
                    await self._register_link(normalized, source)

        except Exception as exc:
            logger.error("Error processing message: %s", exc, exc_info=True)

    async def _track_user(self, event: Any) -> None:
        try:
            sender = await event.get_sender()
            if sender is None:
                return
            from telethon.tl.types import User
            if not isinstance(sender, User) or sender.bot:
                return
            async with AsyncSessionLocal() as session:
                repo = ContactedUserRepository(session)
                _, created = await repo.register_or_update(
                    user_id=sender.id,
                    username=getattr(sender, "username", None),
                    first_name=getattr(sender, "first_name", None),
                    last_name=getattr(sender, "last_name", None),
                )
                await session.commit()
            if created:
                logger.debug("New contacted user: %d", sender.id)
        except Exception as exc:
            logger.debug("Could not track user: %s", exc)

    async def _register_link(self, link: str, source: str) -> None:
        async with AsyncSessionLocal() as session:
            link_repo = DiscoveredLinkRepository(session)
            log_repo = LogRepository(session)
            record, created = await link_repo.register(link, source)
            if not created:
                return
            logger.info("Discovered new link: %s from %s", link, source)
            await log_repo.add(action="link_discovered", result="success", target=link, details=f"source={source}")
            await session.commit()

        await self._validate_and_enqueue(link)

    async def _validate_and_enqueue(self, link: str) -> None:
        entity = await self._tg.resolve_entity(link)

        async with AsyncSessionLocal() as session:
            link_repo = DiscoveredLinkRepository(session)
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)

            record = await link_repo.get_by_link(link)
            if not record:
                return

            if entity is None:
                record.status = LinkStatus.REJECTED
                record.notes = "Could not resolve entity"
                await log_repo.add(
                    action="link_validation_failed", result="error",
                    target=link, details="entity not found",
                )
                await session.commit()
                return

            is_group = await self._tg.is_group(entity)
            if not is_group:
                record.status = LinkStatus.REJECTED
                record.notes = "Not a group (channel or other type)"
                await log_repo.add(action="link_classified_channel", result="skipped", target=link)
                await session.commit()
                logger.info("Link %s is a channel — skipping", link)
                return

            # get_entity_info handles ChatInvite / ChatInviteAlready / regular entities
            group_id, title, username, members_count = await self._tg.get_entity_info(entity)

            # For private invite links not yet joined, group_id is None —
            # use a placeholder so we can track the record.
            if group_id is None:
                group_id = _placeholder_group_id(link)

            # Check for duplicate by real group_id
            existing = await group_repo.get_by_group_id(group_id)
            if existing is not None:
                record.status = LinkStatus.REJECTED
                record.notes = f"Duplicate group_id={group_id}"
                await session.commit()
                logger.debug("Duplicate group %d — skipping", group_id)
                return

            # Also check by invite link to avoid re-queuing the same private link
            existing_by_link = await group_repo.get_by_invite_link(link)
            if existing_by_link is not None:
                record.status = LinkStatus.REJECTED
                record.notes = "Duplicate invite_link"
                await session.commit()
                logger.debug("Duplicate invite_link %s — skipping", link)
                return

            await group_repo.upsert(
                group_id=group_id,
                title=title,
                username=username.lower() if username else None,
                invite_link=link,
                members_count=members_count,
                status=GroupStatus.PENDING,
            )
            record.status = LinkStatus.APPROVED
            await log_repo.add(
                action="group_registered", result="success", target=link,
                details=f"group_id={group_id} title={title!r}",
            )
            await session.commit()
            logger.info("Registered group %d (%s) — enqueueing for join", group_id, title)

        from app.services.join_queue_service import JoinQueueService
        jq = JoinQueueService.get_instance()
        await jq.enqueue(group_id=group_id, link=link, title=title)
