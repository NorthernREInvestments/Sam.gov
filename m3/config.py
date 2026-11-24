"""Runtime configuration from environment. Secrets never hardcoded."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "M3 Supply"
    build_id: str = "20261122-m3-2-build-002m"
    secret_key: str = ""
    database_url: str = "postgresql://localhost/m3"
    m3_data_root: str = ""
    # Offline staged inventory (read-only inputs; output never written to production)
    m3_staged_inventory_root: str = ""
    m3_legacy_data_root: str = ""
    m3_legacy_backup_root: str = ""
    verified_open_max_age_hours: int = 72
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
    # NextBid Data API (paid; never log the key)
    nextbid_api_key: str = ""
    # Default OFF + emergency stop ON (Build 002J fail-closed)
    nextbid_enabled: bool = False
    nextbid_emergency_stop: bool = True
    nextbid_page_size: int = 25
    # Soft bucket planning (controller enforces hard daily/job/reserve)
    nextbid_budget_discovery: int = 50
    nextbid_budget_detail: int = 30
    nextbid_budget_documents: int = 20
    nextbid_budget_reserve: int = 1000
    nextbid_hard_stop_remaining: int = 1000
    nextbid_schedule_minutes: int = 180
    # Daily discovery slots (local wall-clock times); a third gate on top of the emergency stop
    discovery_schedule_enabled: bool = False
    discovery_schedule_slots: str = "06:00,14:00"
    discovery_schedule_timezone: str = "America/Chicago"
    # Hard enforceable ceilings (controller clamps daily ≤ 100)
    nextbid_daily_call_limit: int = 100
    nextbid_job_call_limit: int = 50
    nextbid_max_concurrency: int = 4

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

    def staged_inventory_root(self) -> Path:
        raw = (self.m3_staged_inventory_root or os.getenv("M3_STAGED_INVENTORY_ROOT") or "").strip()
        p = Path(raw) if raw else self.data_root() / "staged_inventory"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def legacy_paths(self) -> dict[str, Path | None]:
        """Retired legacy application data (read-only): canonical store + latest BidNet harvest artifacts."""
        data = (self.m3_legacy_data_root or os.getenv("M3_LEGACY_DATA_ROOT") or "").strip()
        backup = (self.m3_legacy_backup_root or os.getenv("M3_LEGACY_BACKUP_ROOT") or "").strip()
        d = Path(data) if data else None
        b = Path(backup) if backup else None
        out = "_og_commercial_research_pipeline_out"
        return {
            "store": d / "l23_canonical_population_store.json" if d else None,
            "harvest_checkpoint": d / out / "m3_bn_full_deadline_checkpoint_v1.json" if d else None,
            "harvest_report": d / out / "m3_bn_full_deadline_report_v1.json" if d else None,
            "prerun_store": b / "predeploy_20261012_084826" / "railway_data" / "l23_canonical_population_store.json" if b else None,
        }

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
