import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)


class BackupService:
    def __init__(self) -> None:
        self._backup_dir = Path(settings.BACKUP_DIR)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    async def create_backup(self, actor: str = "system") -> str | None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{timestamp}.sql"
        filepath = self._backup_dir / filename

        db_url = settings.DATABASE_URL
        try:
            import urllib.parse as up
            parsed = up.urlparse(db_url)
            env = {**os.environ, "PGPASSWORD": parsed.password or ""}
            cmd = [
                "pg_dump",
                "-h", parsed.hostname or "localhost",
                "-p", str(parsed.port or 5432),
                "-U", parsed.username or "postgres",
                "-d", parsed.path.lstrip("/"),
                "-f", str(filepath),
                "--no-password",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error = stderr.decode()
                logger.error("Backup failed: %s", error)
                await self._log("backup_failed", "error", error_message=error, actor=actor)
                return None

            local_path = str(filepath)
            logger.info("Backup created: %s", local_path)
            await self._log("backup_created", "success", target=local_path, actor=actor)

            # Upload to S3/R2 if configured
            if settings.s3_enabled:
                s3_key = await self._upload_s3(filepath, filename, actor)
                if s3_key:
                    await self._log("backup_uploaded_s3", "success", target=s3_key, actor=actor)

            # Clean up old backups
            await self._cleanup_old_backups()

            return local_path

        except Exception as exc:
            logger.error("Backup exception: %s", exc, exc_info=True)
            await self._log("backup_exception", "error", error_message=str(exc), actor=actor)
            return None

    async def _upload_s3(self, filepath: Path, filename: str, actor: str) -> str | None:
        try:
            import boto3
            from botocore.config import Config

            s3 = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT or None,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name=settings.S3_REGION,
                config=Config(signature_version="s3v4"),
            )
            key = f"telegram-manager-backups/{filename}"

            # Use get_running_loop() — get_event_loop() is deprecated in Python 3.12+
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: s3.upload_file(str(filepath), settings.S3_BUCKET, key),
            )
            logger.info("Backup uploaded to S3: %s/%s", settings.S3_BUCKET, key)
            return f"s3://{settings.S3_BUCKET}/{key}"

        except ImportError:
            logger.warning("boto3 not installed — skipping S3 upload")
            return None
        except Exception as exc:
            logger.error("S3 upload failed: %s", exc)
            await self._log("backup_s3_upload_failed", "error", error_message=str(exc), actor=actor)
            return None

    async def _cleanup_old_backups(self) -> None:
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (settings.BACKUP_KEEP_DAYS * 86400)
            for f in self._backup_dir.glob("backup_*.sql"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info("Deleted old backup: %s", f.name)
        except Exception as exc:
            logger.warning("Cleanup failed: %s", exc)

    def list_backups(self) -> list[dict]:
        backups = []
        for f in sorted(self._backup_dir.glob("backup_*.sql"), reverse=True):
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "path": str(f),
                "size_kb": round(stat.st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        return backups

    async def _log(
        self, action: str, result: str,
        error_message: str | None = None,
        target: str | None = None,
        actor: str = "system",
    ) -> None:
        try:
            async with AsyncSessionLocal() as session:
                await LogRepository(session).add(
                    action=action, result=result,
                    error_message=error_message, actor=actor, target=target,
                )
                await session.commit()
        except Exception as exc:
            logger.error("Could not write backup log: %s", exc)
