"""App settings stored in PostgreSQL."""

from __future__ import annotations

import json
import os
from typing import Any

from claude_client import DEFAULT_SCREENING_PROMPT
from database import SessionLocal
from models import AppSetting
from sam_client import DEFAULT_NAICS

SCREENING_PROMPT_KEY = "screening_prompt"
NAICS_CODES_KEY = "naics_codes"
MIN_DAYS_KEY = "min_days_until_due"
MIN_SCORE_KEY = "min_score_threshold"


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
    session = SessionLocal()
    try:
        raw = _get_setting(session, NAICS_CODES_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return [str(c).strip() for c in data if str(c).strip()]
    finally:
        session.close()
    env_raw = os.getenv("NAICS_CODES", "")
    if env_raw.strip():
        return [c.strip() for c in env_raw.split(",") if c.strip()]
    return DEFAULT_NAICS.copy()


def get_min_days_until_due() -> int:
    session = SessionLocal()
    try:
        raw = _get_setting(session, MIN_DAYS_KEY)
        if raw:
            return int(raw)
    finally:
        session.close()
    return int(os.getenv("MIN_DAYS_UNTIL_DUE", "30"))


def get_min_score_threshold() -> int:
    session = SessionLocal()
    try:
        raw = _get_setting(session, MIN_SCORE_KEY)
        if raw:
            return int(raw)
    finally:
        session.close()
    return int(os.getenv("MIN_SCORE_THRESHOLD", "1"))


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
    prompt, prompt_custom = get_screening_prompt()
    return {
        "naics_codes": get_naics_codes(),
        "min_days_until_due": get_min_days_until_due(),
        "min_score_threshold": get_min_score_threshold(),
        "screening_prompt": prompt,
        "screening_prompt_custom": prompt_custom,
        "api_keys": {
            "sam_gov": bool(os.getenv("SAM_GOV_API_KEY", "").strip()),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "database": bool(os.getenv("DATABASE_URL", "").strip()),
        },
    }


def save_settings(
    naics_codes: list[str],
    min_days_until_due: int,
    min_score_threshold: int,
    screening_prompt: str | None = None,
) -> dict[str, Any]:
    cleaned_naics = [c.strip() for c in naics_codes if c.strip()]
    if not cleaned_naics:
        raise ValueError("At least one NAICS code is required")

    session = SessionLocal()
    try:
        _set_setting(session, NAICS_CODES_KEY, json.dumps(cleaned_naics))
        _set_setting(session, MIN_DAYS_KEY, str(min_days_until_due))
        _set_setting(session, MIN_SCORE_KEY, str(min_score_threshold))
        if screening_prompt is not None:
            _set_setting(session, SCREENING_PROMPT_KEY, screening_prompt.strip())
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
