from datetime import datetime
from sqlalchemy import BigInteger, String, DateTime, Integer, func, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class JoinAttempt(Base):
    """Tracks every join attempt for rate-limiting and retry logic."""
    __tablename__ = "join_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    invite_link: Mapped[str] = mapped_column(String(1024), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    success: Mapped[bool | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<JoinAttempt group_id={self.group_id} attempt={self.attempt_number} success={self.success}>"
