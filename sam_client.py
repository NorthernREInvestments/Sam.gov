"""Pull active federal contract opportunities from SAM.gov."""

from __future__ import annotations
from application_clock import now_utc, today_local

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
from naics_labels import ALL_NAICS_CODES

DEFAULT_NAICS = ALL_NAICS_CODES.copy()


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
    return (due - today_local()).days


def _set_aside_matches(opp: dict[str, Any]) -> bool:
    """Accept Total Small Business / SBA total set-asides; reject socio-economic exclusives.

    SAM.gov commonly returns descriptions like:
      \"Small Business Set Aside - Total\"
      \"Total Small Business\"
      \"Total Small Business Set-Aside\"
    """
    text = str(
        opp.get("typeOfSetAsideDescription")
        or opp.get("typeOfSetAside")
        or ""
    ).strip().lower()
    if not text:
        return False
    # Normalize punctuation so \"set-aside\" and \"set aside\" match equally
    normalized = (
        text.replace("–", "-")
        .replace("—", "-")
        .replace("_", " ")
    )
    compact = " ".join(normalized.replace("-", " ").split())

    excluded = ("veteran", "women", "hubzone", "8(a)", "8a", "disadvantaged", "indian", "wosb", "sdvosb", "edwosb")
    if any(tag in compact for tag in excluded):
        return False

    if "total small business" in compact:
        return True
    if compact == "small business":
        return True
    if compact.startswith("small business set aside"):
        # Includes \"Small Business Set Aside - Total\" from live SAM payloads
        return "total" in compact or compact == "small business set aside"
    if compact.startswith("sba") and "total" in compact:
        return True
    return False


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
            city = city.get("name") or city.get("code")
        if isinstance(state, dict):
            state = state.get("code") or state.get("name")
        parts = [city, state, place.get("zip")]
        formatted = ", ".join(str(p) for p in parts if p)
        if formatted:
            return formatted
        from usaspending_client import _parse_sam_location_block

        city, state_code, zip_code = _parse_sam_location_block(place)
        fallback_parts = [p for p in (city, state_code, zip_code) if p]
        if fallback_parts:
            return ", ".join(fallback_parts)
    if place and not isinstance(place, dict):
        return str(place)
    return None


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
    *,
    authorize_live: bool = False,
    authorize_broad_sam_discovery: bool = False,
    purpose: str | None = None,
) -> list[dict[str, Any]]:
    """One SAM.gov API call for a single NAICS code. Returns normalized opportunities.

    Blocked by default under SAM scarcity — requires authorize_live + broad discovery auth.
    """
    from api_budget import can_spend_sam, record_sam_usage
    from sam_scarcity import PURPOSE_NAICS_SCAN, gate_sam_api_call, mark_sam_audit_executed

    gate = gate_sam_api_call(
        purpose=purpose or PURPOSE_NAICS_SCAN,
        authorize_live=authorize_live,
        authorize_broad_sam_discovery=authorize_broad_sam_discovery,
        endpoint=SAM_SEARCH_URL,
        context={"requested_fact": f"naics_search:{naics_code}"},
    )
    if not gate["allowed"]:
        raise ValueError(
            "SAM NAICS scan blocked by scarcity gate: "
            + (gate.get("blocked_reason") or "not_eligible")
        )

    api_key = (api_key or os.getenv("SAM_GOV_API_KEY", "")).strip()
    if not api_key:
        raise ValueError(
            "SAM_GOV_API_KEY is required. "
            "Get a free key at sam.gov -> Account Details -> Public API Key."
        )
    if not can_spend_sam(1):
        raise ValueError(
            "SAM.gov daily API budget reached — try again tomorrow or raise SAM_API_CALL_LIMIT."
        )

    posted_to = today_local()
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
    mark_sam_audit_executed(gate.get("audit_id"), useful_new_evidence=bool(batch), result_status="EXECUTED")

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


def fetch_govspend_target_from_sam(target: Any) -> list[dict[str, Any]]:
    """One SAM.gov search tailored to a GovSpend gs_watchlist row."""
    from api_budget import can_spend_sam, record_sam_usage
    from sam_scarcity import PURPOSE_WATCHLIST_SEARCH, gate_sam_api_call, mark_sam_audit_executed

    gate = gate_sam_api_call(
        purpose=PURPOSE_WATCHLIST_SEARCH,
        authorize_live=False,
        authorize_broad_sam_discovery=False,
        endpoint=SAM_SEARCH_URL,
    )
    if not gate["allowed"]:
        raise ValueError(
            "SAM watchlist search blocked by scarcity gate: "
            + (gate.get("blocked_reason") or "broad_sam_discovery_blocked")
        )

    api_key = (os.getenv("SAM_GOV_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("SAM_GOV_API_KEY is required.")
    if not can_spend_sam(1):
        raise ValueError("SAM.gov daily API budget reached.")

    posted_to = today_local()
    posted_from = posted_to - timedelta(days=45)

    params: dict[str, Any] = {
        "api_key": api_key,
        "postedFrom": posted_from.strftime("%m/%d/%Y"),
        "postedTo": posted_to.strftime("%m/%d/%Y"),
        "limit": 200,
        "offset": 0,
        "active": "yes",
    }
    if getattr(target, "naics_code", None):
        params["ncode"] = str(target.naics_code).strip()
    if getattr(target, "location_state", None):
        params["state"] = str(target.location_state).strip()[:2].upper()
    if getattr(target, "agency", None) or getattr(target, "contracting_office", None):
        params["organizationName"] = str(
            getattr(target, "contracting_office", None) or target.agency
        ).strip()[:100]
    if getattr(target, "contract_name", None):
        params["title"] = str(target.contract_name).strip()[:100]
    elif getattr(target, "incumbent_name", None):
        params["title"] = str(target.incumbent_name).strip()[:100]

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(SAM_SEARCH_URL, params=params)
        resp.raise_for_status()
        batch = resp.json().get("opportunitiesData") or []

    record_sam_usage(1)
    mark_sam_audit_executed(gate.get("audit_id"), useful_new_evidence=bool(batch), result_status="EXECUTED")

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
