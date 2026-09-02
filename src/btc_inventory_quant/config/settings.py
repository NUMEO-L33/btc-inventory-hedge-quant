"""Environment-backed secrets and hard switches (never stored in YAML or code)."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIQ_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    binance_demo_api_key: SecretStr | None = None
    binance_demo_api_secret: SecretStr | None = None

    enable_live_trading: bool = False
    live_acknowledgement: str = ""

    database_url: str = "sqlite:///state/biq.sqlite3"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_to: str | None = None

    log_level: str = Field("INFO")
    log_json: bool = True

    def credentials_for(self, environment: str) -> tuple[SecretStr | None, SecretStr | None]:
        if environment == "production":
            return self.binance_api_key, self.binance_api_secret
        return self.binance_demo_api_key, self.binance_demo_api_secret


def load_settings() -> Settings:
    return Settings()
