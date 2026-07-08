from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram User Client (Telethon) ────────────────────────────────────────
    # IMPORTANT: DO NOT use shared credentials such as API_ID=2040 (Telegram Desktop).
    # Using shared credentials causes account bans. Register your own app at:
    # https://my.telegram.org/apps and supply the values via environment variables.
    #
    # Defaults are 0/"" so the bot can start without crashing even when these are
    # not yet set in the deployment environment. The Telethon user-client will
    # refuse to connect and log a clear warning until real values are provided.
    TELEGRAM_API_ID: int = 0      # Set via TELEGRAM_API_ID env var
    TELEGRAM_API_HASH: str = ""   # Set via TELEGRAM_API_HASH env var
    TELEGRAM_PHONE: str = ""
    TELEGRAM_SESSION_NAME: str = "tg_session"
    TELEGRAM_SESSION_STRING: str = ""

    # ── Telegram Bot (Aiogram) ─────────────────────────────────────────────────
    BOT_TOKEN: str

    # Admin whitelist (comma-separated Telegram user IDs)
    ADMIN_IDS: str = ""

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Redis (FSM + task queue — optional but strongly recommended for prod) ──
    REDIS_URL: str = ""

    # ── App ────────────────────────────────────────────────────────────────────
    APP_ENV: str = "production"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # ── Rate limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_MESSAGES: int = 30
    RATE_LIMIT_WINDOW: int = 60

    # ── Backup ─────────────────────────────────────────────────────────────────
    BACKUP_DIR: str = "/tmp/backups"
    BACKUP_KEEP_DAYS: int = 7
    BACKUP_DAILY_HOUR: int = 3

    # ── S3-compatible backup upload (optional) ─────────────────────────────────
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "auto"

    # ── Discovery ──────────────────────────────────────────────────────────────
    DISCOVERY_KEYWORDS: str = ""

    # ── Daily join limit ───────────────────────────────────────────────────────
    # Maximum number of group joins allowed per day (resets at UTC midnight).
    # Telegram's unofficial safe threshold is ~30-50 joins/day per account.
    # Exceeding this risks a temporary restriction or permanent ban.
    MAX_JOINS_PER_DAY: int = 30

    # ── Join queue anti-detection delays ───────────────────────────────────────
    # Random jitter in [MIN, MAX] seconds is chosen per join so Telegram's
    # anti-spam system cannot detect a fixed pattern. The range MUST have
    # MIN < MAX; setting them equal eliminates the randomness and makes the
    # account look like a bot.
    # Configured to ~9 minutes average: jitter between 8 min and 10 min.
    # To change, set JOIN_DELAY_MIN / JOIN_DELAY_MAX env vars (in seconds).
    JOIN_DELAY_MIN: int = 480   # seconds — 8 minutes
    JOIN_DELAY_MAX: int = 600   # seconds — 10 minutes  (average ≈ 9 min)

    # ── Auto-retry for failed joins ────────────────────────────────────────────
    RETRY_FAILED_JOINS: bool = True
    RETRY_INTERVAL_HOURS: int = 6
    RETRY_MAX_ATTEMPTS: int = 3

    # ── PeerFlood / DM broadcast protection ───────────────────────────────────
    # When a PeerFlood error hits during user broadcast, pause this many seconds
    # before continuing. Telegram imposes PeerFlood when too many DMs are sent
    # to strangers in a short window. 1800s (30 min) is a safe recovery window.
    # If MAX_PEER_FLOOD_PAUSES is hit in a single broadcast, the job is aborted.
    PEER_FLOOD_PAUSE_SECONDS: int = 1800  # 30 minutes
    MAX_PEER_FLOOD_PAUSES: int = 3        # abort after this many pauses

    # ── TG DM delay (Telethon user-account DMs) ───────────────────────────────
    # Seconds to wait between each DM sent via the Telethon user account.
    # Increase if you see frequent PeerFlood errors.
    TG_DM_DELAY_SECONDS: float = 5.0

    @field_validator("JOIN_DELAY_MAX", mode="after")
    @classmethod
    def validate_delay_range(cls, v: int, info: object) -> int:
        # Access MIN via the info object if available; otherwise just return v.
        try:
            min_val = getattr(info, "data", {}).get("JOIN_DELAY_MIN", 0)
            if min_val and v < min_val:
                raise ValueError(
                    f"JOIN_DELAY_MAX ({v}) must be >= JOIN_DELAY_MIN ({min_val}). "
                    "Setting them equal removes randomisation — use a range."
                )
        except (AttributeError, TypeError):
            pass
        return v

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: str) -> str:
        v = v.strip()
        if v:
            for part in v.split(","):
                part = part.strip()
                if part:
                    try:
                        int(part)
                    except ValueError:
                        raise ValueError(
                            f"ADMIN_IDS contains invalid integer: {part!r}. "
                            "Expected comma-separated Telegram user IDs (integers)."
                        )
        return v

    def get_admin_id_list(self) -> List[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    def get_discovery_keywords(self) -> List[str]:
        if not self.DISCOVERY_KEYWORDS:
            return []
        return [k.strip().lower() for k in self.DISCOVERY_KEYWORDS.split(",") if k.strip()]

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def s3_enabled(self) -> bool:
        return bool(self.S3_BUCKET and self.S3_ACCESS_KEY and self.S3_SECRET_KEY)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.REDIS_URL)


settings = Settings()
