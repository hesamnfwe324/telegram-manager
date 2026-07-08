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
      MAX_JOINS_PER_DAY: int = 30

      # ── Join queue anti-detection delays ───────────────────────────────────────
      JOIN_DELAY_MIN: int = 3600   # seconds — 1 hour
      JOIN_DELAY_MAX: int = 4500   # seconds — 1 hour 15 minutes

      # ── Auto-retry for failed joins ────────────────────────────────────────────
      RETRY_FAILED_JOINS: bool = True
      RETRY_INTERVAL_HOURS: int = 6
      RETRY_MAX_ATTEMPTS: int = 3

      # ── PeerFlood / DM broadcast protection ───────────────────────────────────
      PEER_FLOOD_PAUSE_SECONDS: int = 1800
      MAX_PEER_FLOOD_PAUSES: int = 3

      # ── TG DM delay (Telethon user-account DMs) ───────────────────────────────
      TG_DM_DELAY_SECONDS: float = 5.0

      # ── Grok AI (xAI) — conversational DM assistant ────────────────────────────
      # API key from https://console.x.ai/
      GROK_API_KEY: str = ""
      # Link to your Telegram channel — shared naturally during AI conversation
      CHANNEL_INVITE_LINK: str = "https://t.me/addlist/4xJXMUc98LZhNGM8"
      # Link to your Telegram bot — shared naturally during AI conversation
      BOT_LINK: str = "https://t.me/AmazonGiftCardBot?start=REF6538Q62Z"

      @field_validator("JOIN_DELAY_MAX", mode="after")
      @classmethod
      def validate_delay_range(cls, v: int, info: object) -> int:
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
    