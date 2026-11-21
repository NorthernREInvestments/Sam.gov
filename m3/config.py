"""Runtime configuration from environment. Secrets never hardcoded."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "M3 Supply"
    build_id: str = "20261121-m3-2-build-002g"
    secret_key: str = ""
    database_url: str = "postgresql://localhost/m3"
    m3_data_root: str = ""
    session_cookie: str = "m3_session"
    session_max_age_seconds: int = 60 * 60 * 12
    # Owner bootstrap (env only)
    m3_owner_email: str = ""
    m3_owner_password: str = ""
    m3_owner_name: str = "Owner"
    # Legacy APP_EMAIL/APP_PASSWORD accepted as fallback for owner bootstrap
    app_email: str = ""
    app_password: str = ""
    # Feature kills — SAM/BidNet/Euna stay parked; OpenAI authorized when enabled
    sam_live_enabled: bool = False
    bidnet_enabled: bool = False
    euna_enabled: bool = False
    paid_research_enabled: bool = False
    openai_enabled: bool = False
    openai_api_key: str = ""
    openai_default_model: str = "gpt-4o-mini"
    openai_capable_model: str = "gpt-5-mini"
    openai_search_context_size: str = "low"
    # Research budget ceilings (owner-overridable in DB policy; not spending targets)
    research_daily_budget_usd: float = 1.0
    research_monthly_budget_usd: float = 30.0
    research_per_job_budget_usd: float = 0.50
    research_per_user_daily_budget_usd: float = 1.0
    research_max_depth: int = 2
    # Discovery — no artificial commercial ceiling; page size is display/slice only
    research_display_page_size: int = 100
    research_socrata_page_size: int = 500
    # Build 002A: remove prior 3-page acceptance harness / 8-page soft cap.
    # 200 × 500 = up to 100k rows/source per job; incomplete sources report more_available.
    research_max_pages_per_source: int = 200
    # HTTP research
    research_timeout_seconds: float = 25.0
    research_max_retries: int = 2
    upload_max_bytes: int = 15 * 1024 * 1024

    def resolved_secret(self) -> str:
        key = (self.secret_key or os.getenv("SECRET_KEY") or "").strip()
        if not key:
            raise RuntimeError("SECRET_KEY must be set in the environment")
        return key

    def resolved_database_url(self) -> str:
        return (self.database_url or os.getenv("DATABASE_URL") or "").strip()

    def data_root(self) -> Path:
        raw = (self.m3_data_root or os.getenv("M3_DATA_ROOT") or "").strip()
        if raw:
            p = Path(raw)
        else:
            p = Path(__file__).resolve().parents[1] / "data"
        p.mkdir(parents=True, exist_ok=True)
        (p / "uploads").mkdir(parents=True, exist_ok=True)
        (p / "cache").mkdir(parents=True, exist_ok=True)
        return p

    def owner_bootstrap(self) -> tuple[str, str, str] | None:
        email = (self.m3_owner_email or self.app_email or "").strip().lower()
        password = (self.m3_owner_password or self.app_password or "").strip()
        name = (self.m3_owner_name or "Owner").strip()
        if email and password:
            return email, password, name
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
