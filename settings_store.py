"""App settings stored in PostgreSQL."""

from __future__ import annotations

import json
import os
from typing import Any

from claude_client import DEFAULT_SCREENING_PROMPT
from database import SessionLocal
from models import AppSetting
from naics_labels import ALL_NAICS_CODES, NAICS_LABELS, NAICS_TIER_GROUPS, TIER_SCHEDULE_SUMMARY

SCREENING_PROMPT_KEY = "screening_prompt"
NAICS_CODES_KEY = "naics_codes"
MIN_DAYS_KEY = "min_days_until_due"
MIN_SCORE_KEY = "min_score_threshold"
SCHEDULER_ENABLED_KEY = "scheduler_enabled"
SCHEDULER_HOUR_KEY = "scheduler_hour"
SCHEDULER_MINUTE_KEY = "scheduler_minute"
SCHEDULER_TIMEZONE_KEY = "scheduler_timezone"
SUB_SEARCH_RADIUS_KEY = "sub_search_radius_miles"
SUB_MIN_RATING_KEY = "sub_min_rating"
SUB_MIN_REVIEWS_KEY = "sub_min_review_count"
OWNER_SETTINGS_KEY = "proposal_owner_settings"


def _get_setting(session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row else None


def _set_setting(session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))


def _delete_setting(session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        session.delete(row)


def get_naics_codes() -> list[str]:
    """Active NAICS codes — only these are searched on SAM.gov and shown by default."""
    session = SessionLocal()
    try:
        raw = _get_setting(session, NAICS_CODES_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                cleaned = [str(c).strip() for c in data if str(c).strip() in NAICS_LABELS]
                if cleaned:
                    return cleaned
    finally:
        session.close()
    env_raw = os.getenv("NAICS_CODES", "")
    if env_raw.strip():
        return [c.strip() for c in env_raw.split(",") if c.strip() in NAICS_LABELS]
    return ALL_NAICS_CODES.copy()


def get_naics_codes_for_tiers(tier_numbers: list[int]) -> list[str]:
    """Enabled NAICS codes limited to the requested search tiers."""
    from naics_labels import codes_in_tiers

    enabled = set(get_naics_codes())
    return [code for code in codes_in_tiers(tier_numbers) if code in enabled]


def get_min_days_until_due() -> int:
    session = SessionLocal()
    try:
        raw = _get_setting(session, MIN_DAYS_KEY)
        if raw:
            return int(raw)
    finally:
        session.close()
    return int(os.getenv("MIN_DAYS_UNTIL_DUE", "10"))


def get_min_score_threshold() -> int:
    session = SessionLocal()
    try:
        raw = _get_setting(session, MIN_SCORE_KEY)
        if raw:
            return int(raw)
    finally:
        session.close()
    return int(os.getenv("MIN_SCORE_THRESHOLD", "1"))


def get_scheduler_settings() -> dict[str, Any]:
    session = SessionLocal()
    try:
        enabled_raw = _get_setting(session, SCHEDULER_ENABLED_KEY)
        hour_raw = _get_setting(session, SCHEDULER_HOUR_KEY)
        minute_raw = _get_setting(session, SCHEDULER_MINUTE_KEY)
        tz_raw = _get_setting(session, SCHEDULER_TIMEZONE_KEY)
    finally:
        session.close()

    env_enabled = os.getenv("SCHEDULER_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    enabled = env_enabled if enabled_raw is None else enabled_raw.strip().lower() in ("1", "true", "yes")
    hour = int(hour_raw) if hour_raw is not None else int(os.getenv("DAILY_REFRESH_HOUR", "6"))
    minute = int(minute_raw) if minute_raw is not None else int(os.getenv("DAILY_REFRESH_MINUTE", "0"))
    timezone = (tz_raw or os.getenv("SCHEDULER_TIMEZONE", "America/Denver")).strip()

    return {
        "enabled": enabled,
        "hour": max(0, min(23, hour)),
        "minute": max(0, min(59, minute)),
        "timezone": timezone or "America/Denver",
    }


def get_sub_search_settings() -> dict[str, Any]:
    session = SessionLocal()
    try:
        radius_raw = _get_setting(session, SUB_SEARCH_RADIUS_KEY)
        rating_raw = _get_setting(session, SUB_MIN_RATING_KEY)
        reviews_raw = _get_setting(session, SUB_MIN_REVIEWS_KEY)
    finally:
        session.close()

    radius = int(radius_raw) if radius_raw else int(os.getenv("SUB_SEARCH_RADIUS_MILES", "25"))
    min_rating = float(rating_raw) if rating_raw else float(os.getenv("SUB_MIN_RATING", "3.5"))
    min_reviews = int(reviews_raw) if reviews_raw else int(os.getenv("SUB_MIN_REVIEW_COUNT", "5"))
    if radius not in (10, 25, 50, 100):
        radius = 25
    return {
        "search_radius_miles": max(10, min(100, radius)),
        "min_rating": max(0.0, min(5.0, min_rating)),
        "min_review_count": max(0, min_reviews),
    }


def get_owner_settings() -> dict[str, Any]:
    from proposal_defaults import DEFAULT_OWNER_SETTINGS

    session = SessionLocal()
    try:
        raw = _get_setting(session, OWNER_SETTINGS_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                merged = {**DEFAULT_OWNER_SETTINGS, **data}
                if merged.get("business_email") == "NorthernREIncestments@outlook.com":
                    merged["business_email"] = "NorthernREInvestments@outlook.com"
                if merged.get("default_margin_pct") == 18:
                    merged["default_margin_pct"] = 20
                return merged
    finally:
        session.close()
    return dict(DEFAULT_OWNER_SETTINGS)


def save_owner_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = get_owner_settings()
    current.update({k: v for k, v in payload.items() if v is not None})
    session = SessionLocal()
    try:
        _set_setting(session, OWNER_SETTINGS_KEY, json.dumps(current, default=str))
        session.commit()
    finally:
        session.close()
    return get_owner_settings()


def get_screening_prompt() -> tuple[str, bool]:
    session = SessionLocal()
    try:
        custom = _get_setting(session, SCREENING_PROMPT_KEY)
        if custom:
            return custom, True
        return DEFAULT_SCREENING_PROMPT, False
    finally:
        session.close()


def resolve_screening_prompt() -> str:
    prompt, _ = get_screening_prompt()
    return prompt


def get_all_settings() -> dict[str, Any]:
    from api_budget import get_usage_snapshot

    def _owner_completion() -> dict[str, Any]:
        from workflow_status import _owner_gaps

        missing = _owner_gaps()
        return {"complete": len(missing) == 0, "missing": missing}

    prompt, prompt_custom = get_screening_prompt()
    scheduler = get_scheduler_settings()
    sub_search = get_sub_search_settings()
    return {
        "naics_codes": get_naics_codes(),
        "all_naics_codes": ALL_NAICS_CODES,
        "naics_tiers": NAICS_TIER_GROUPS,
        "naics_groups": NAICS_TIER_GROUPS,
        "naics_tier_schedule": TIER_SCHEDULE_SUMMARY,
        "naics_labels": NAICS_LABELS,
        "min_days_until_due": get_min_days_until_due(),
        "min_score_threshold": get_min_score_threshold(),
        "screening_prompt": prompt,
        "screening_prompt_custom": prompt_custom,
        "scheduler": scheduler,
        "sub_search": sub_search,
        "owner": get_owner_settings(),
        "owner_completion": _owner_completion(),
        "api_budget": get_usage_snapshot(),
        "api_keys": {
            "sam_gov": bool(os.getenv("SAM_GOV_API_KEY", "").strip()),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "google_places": bool(os.getenv("GOOGLE_PLACES_API_KEY", "").strip()),
            "database": bool(os.getenv("DATABASE_URL", "").strip()),
        },
        "performance": _performance_settings(),
    }


def _performance_settings() -> dict[str, Any]:
    from performance_settings import get_performance_settings

    return get_performance_settings()


def save_settings(
    naics_codes: list[str],
    min_days_until_due: int,
    min_score_threshold: int,
    screening_prompt: str | None = None,
    scheduler_enabled: bool | None = None,
    scheduler_hour: int | None = None,
    scheduler_minute: int | None = None,
    scheduler_timezone: str | None = None,
    sub_search_radius_miles: int | None = None,
    sub_min_rating: float | None = None,
    sub_min_review_count: int | None = None,
) -> dict[str, Any]:
    cleaned_naics = [c.strip() for c in naics_codes if c.strip()]
    unknown = [c for c in cleaned_naics if c not in NAICS_LABELS]
    if unknown:
        raise ValueError(f"Unknown NAICS code(s): {', '.join(unknown)}")
    if not cleaned_naics:
        raise ValueError("At least one NAICS code is required")

    session = SessionLocal()
    try:
        _set_setting(session, NAICS_CODES_KEY, json.dumps(cleaned_naics))
        _set_setting(session, MIN_DAYS_KEY, str(min_days_until_due))
        _set_setting(session, MIN_SCORE_KEY, str(min_score_threshold))
        if screening_prompt is not None:
            _set_setting(session, SCREENING_PROMPT_KEY, screening_prompt.strip())
        if scheduler_enabled is not None:
            _set_setting(session, SCHEDULER_ENABLED_KEY, "true" if scheduler_enabled else "false")
        if scheduler_hour is not None:
            _set_setting(session, SCHEDULER_HOUR_KEY, str(max(0, min(23, scheduler_hour))))
        if scheduler_minute is not None:
            _set_setting(session, SCHEDULER_MINUTE_KEY, str(max(0, min(59, scheduler_minute))))
        if scheduler_timezone is not None:
            _set_setting(session, SCHEDULER_TIMEZONE_KEY, scheduler_timezone.strip())
        if sub_search_radius_miles is not None:
            _set_setting(session, SUB_SEARCH_RADIUS_KEY, str(max(10, min(100, sub_search_radius_miles))))
        if sub_min_rating is not None:
            _set_setting(session, SUB_MIN_RATING_KEY, str(max(0.0, min(5.0, float(sub_min_rating)))))
        if sub_min_review_count is not None:
            _set_setting(session, SUB_MIN_REVIEWS_KEY, str(max(0, int(sub_min_review_count))))
        session.commit()
    finally:
        session.close()
    return get_all_settings()


def reset_screening_prompt() -> str:
    session = SessionLocal()
    try:
        _delete_setting(session, SCREENING_PROMPT_KEY)
        session.commit()
        return DEFAULT_SCREENING_PROMPT
    finally:
        session.close()
