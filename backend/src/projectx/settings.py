"""Configuration. Every value comes from the environment (prefix ``PX_``) or a ``.env`` file.

Defaults are safe for local development only. ``Settings.validate_for_production`` refuses to
start a staging/production process with development secrets or insecure cookies.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-secret-key-not-for-production-00000000"  # noqa: S105
# 32 bytes, base64. Development only: production must supply its own key (or a KMS key id).
_DEV_VAULT_KEY = base64.b64encode(b"projectx-dev-vault-master-key-32").decode()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PX_", env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    app_name: str = "Project X"
    base_url: str = "http://localhost:8010"
    web_url: str = "http://localhost:5173"
    log_level: str = "INFO"
    log_json: bool = False

    # --- database ---
    database_url: str = "postgresql+asyncpg://projectx:projectx@localhost:5432/projectx"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- security ---
    secret_key: SecretStr = SecretStr(_DEV_SECRET)
    vault_master_key: SecretStr = SecretStr(_DEV_VAULT_KEY)
    session_ttl_days: int = 30
    cookie_secure: bool = False
    cookie_domain: str | None = None
    trusted_origins: list[str] = Field(default_factory=list)
    signup_enabled: bool = True
    auth_signups_per_hour_per_ip: int = 10
    auth_logins_per_15m_per_ip: int = 30
    auth_logins_per_15m_per_email: int = 12
    # Plan for new businesses. Unset: "scale" in development (everything unlocked), "free" elsewhere.
    default_plan: str | None = None

    # --- model gateway: each rung is "provider:model"; providers: offline, anthropic, openai ---
    llm_small: str = "offline:draft-small"
    llm_mid: str = "offline:draft-mid"
    llm_frontier: str = "offline:draft-frontier"
    anthropic_api_key: SecretStr | None = None
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None
    llm_default_daily_cap_usd: float = 2.0
    llm_cache_ttl_hours: int = 72
    llm_log_prompts: bool = True

    # --- email ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_use_tls: bool = False
    email_from: str = "Project X <no-reply@projectx.local>"

    # --- files ---
    blob_dir: str = ".local/blobs"
    max_upload_mb: int = 200

    # --- worker ---
    worker_concurrency: int = 4
    worker_poll_interval_s: float = 1.0
    worker_lease_s: int = 120
    scheduler_enabled: bool = True
    daily_cycle_hour_local: int = 6

    # --- platform apps (empty until each platform approves our app) ---
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_webhook_verify_token: SecretStr | None = None
    meta_graph_version: str = "v24.0"
    threads_app_id: str | None = None
    threads_app_secret: SecretStr | None = None
    x_client_id: str | None = None
    x_client_secret: SecretStr | None = None
    linkedin_client_id: str | None = None
    linkedin_client_secret: SecretStr | None = None
    linkedin_api_version: str = "202510"
    tiktok_client_key: str | None = None
    tiktok_client_secret: SecretStr | None = None
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_ads_developer_token: SecretStr | None = None
    pinterest_app_id: str | None = None
    pinterest_app_secret: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    discord_application_id: str | None = None
    discord_public_key: str | None = None
    discord_bot_token: SecretStr | None = None
    ayrshare_api_key: SecretStr | None = None
    whatsapp_graph_version: str = "v24.0"

    # --- billing (Project X charging its own customers) ---
    dodo_api_key: SecretStr | None = None
    dodo_webhook_secret: SecretStr | None = None
    dodo_environment: Literal["test_mode", "live_mode"] = "test_mode"

    @property
    def is_dev(self) -> bool:
        return self.env in ("dev", "test")

    @property
    def new_business_plan(self) -> str:
        return self.default_plan or ("scale" if self.is_dev else "free")

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure or not self.is_dev

    @model_validator(mode="after")
    def validate_for_production(self) -> Settings:
        if self.is_dev:
            return self
        problems: list[str] = []
        if self.secret_key.get_secret_value() == _DEV_SECRET or len(self.secret_key.get_secret_value()) < 32:
            problems.append("PX_SECRET_KEY must be a unique value of at least 32 characters")
        if self.vault_master_key.get_secret_value() == _DEV_VAULT_KEY:
            problems.append("PX_VAULT_MASTER_KEY must be set to a production key")
        if not self.base_url.startswith("https://"):
            problems.append("PX_BASE_URL must use https in staging/production")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
