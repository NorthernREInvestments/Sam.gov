"""Pull active federal contract opportunities from SAM.gov."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
DEFAULT_NAICS = ["561720", "561210", "561730", "561710", "561790", "561612"]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned[:10], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _days_until(due: date | None) -> int | None:
    if due is None:
        return None
    return (due - date.today()).days


def _set_aside_matches(opp: dict[str, Any]) -> bool:
    text = str(
        opp.get("typeOfSetAsideDescription")
        or opp.get("typeOfSetAside")
        or ""
    ).strip().lower()
    if not text:
        return False
    if "total small business" in text:
        return True
    excluded = ("veteran", "women", "hubzone", "8(a)", "8a", "disadvantaged", "indian")
    if any(tag in text for tag in excluded):
        return False
    return text == "small business" or text.startswith("small business set-aside")


def _format_location(raw: dict[str, Any]) -> str | None:
    from sam_enrich import extract_states_from_text, _place_of_performance_text

    pop_text = _place_of_performance_text(raw)
    work_states = extract_states_from_text(raw.get("title"), pop_text)
    if work_states and len(work_states) > 1:
        return f"Multiple locations ({', '.join(work_states)})"
    if pop_text:
        if len(pop_text) > 180:
            return pop_text[:177] + "..."
        return pop_text

    place = raw.get("placeOfPerformance") or raw.get("placeOfPerformanceLocation")
    if not isinstance(place, dict):
        place = raw.get("officeAddress")
    if isinstance(place, dict):
        city = place.get("city")
        state = place.get("state")
        if isinstance(city, dict):
            city = city.get("name")
        if isinstance(state, dict):
            state = state.get("code") or state.get("name")
        parts = [city, state, place.get("zip")]
        formatted = ", ".join(str(p) for p in parts if p)
        if formatted:
            return formatted
    return str(place) if place else None


def normalize_opportunity(raw: dict[str, Any]) -> dict[str, Any]:
    due_raw = raw.get("responseDeadLine") or raw.get("reponseDeadLine")
    due = _parse_date(due_raw)
    return {
        "notice_id": raw.get("noticeId") or raw.get("solicitationNumber"),
        "title": raw.get("title") or "Untitled",
        "agency": raw.get("fullParentPathName") or raw.get("department"),
        "location": _format_location(raw),
        "naics_code": raw.get("naicsCode") or raw.get("naics"),
        "set_aside": raw.get("typeOfSetAsideDescription") or raw.get("typeOfSetAside"),
        "due_date": due.isoformat() if due else None,
        "days_until_due": _days_until(due),
        "link": raw.get("uiLink"),
    }


def naics_from_env() -> list[str]:
    from settings_store import get_naics_codes

    return get_naics_codes()


def min_days_from_env() -> int:
    from settings_store import get_min_days_until_due

    return get_min_days_until_due()


def fetch_naics_from_sam(
    naics_code: str,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """One SAM.gov API call for a single NAICS code. Returns normalized opportunities."""
    from api_budget import can_spend_sam, record_sam_usage

    api_key = (api_key or os.getenv("SAM_GOV_API_KEY", "")).strip()
    if not api_key:
        raise ValueError(
            "SAM_GOV_API_KEY is required. "
            "Get a free key at sam.gov -> Account Details -> Public API Key."
        )
    if not can_spend_sam(1):
        raise ValueError("SAM.gov daily API budget reached — try again tomorrow or raise SAM_DAILY_API_BUDGET.")

    posted_to = date.today()
    posted_from = posted_to - timedelta(days=30)

    params = {
        "api_key": api_key,
        "postedFrom": posted_from.strftime("%m/%d/%Y"),
        "postedTo": posted_to.strftime("%m/%d/%Y"),
        "ncode": naics_code,
        "limit": 1000,
        "offset": 0,
        "active": "yes",
    }

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(SAM_SEARCH_URL, params=params)
        resp.raise_for_status()
        batch = resp.json().get("opportunitiesData") or []

    record_sam_usage(1)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in batch:
        if not _set_aside_matches(raw):
            continue
        opp = normalize_opportunity(raw)
        description = raw.get("description")
        if isinstance(description, str) and description.strip() and not description.startswith("http"):
            opp["description"] = description[:8000]
        nid = str(opp.get("notice_id") or "")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        results.append({**opp, "sam_raw": dict(raw)})

    return results
