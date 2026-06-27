"""Pull active federal contract opportunities from SAM.gov."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_FILE = CACHE_DIR / "opportunities.json"
STATE_FILE = CACHE_DIR / "rotation.json"

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
    place = raw.get("placeOfPerformance") or raw.get("officeAddress")
    if isinstance(place, dict):
        city = place.get("city")
        state = place.get("state")
        if isinstance(city, dict):
            city = city.get("name")
        if isinstance(state, dict):
            state = state.get("code") or state.get("name")
        parts = [city, state, place.get("zip")]
        return ", ".join(str(p) for p in parts if p) or None
    return str(place) if place else None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
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


def _naics_from_env() -> list[str]:
    raw = os.getenv("NAICS_CODES", "")
    if raw.strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    return DEFAULT_NAICS.copy()


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _fetch_one_naics(
    client: httpx.Client,
    api_key: str,
    naics: str,
    posted_from: date,
    posted_to: date,
) -> list[dict[str, Any]]:
    params = {
        "api_key": api_key,
        "postedFrom": posted_from.strftime("%m/%d/%Y"),
        "postedTo": posted_to.strftime("%m/%d/%Y"),
        "ncode": naics,
        "limit": 1000,
        "offset": 0,
        "active": "yes",
    }
    resp = client.get(SAM_SEARCH_URL, params=params)
    resp.raise_for_status()
    return resp.json().get("opportunitiesData") or []


def _refresh_due_fields(opp: dict[str, Any]) -> dict[str, Any]:
    due = _parse_date(opp.get("due_date"))
    opp["days_until_due"] = _days_until(due)
    return opp


def _passes_filters(opp: dict[str, Any], min_days: int) -> bool:
    opp = _refresh_due_fields(opp)
    days = opp.get("days_until_due")
    if days is not None and days < min_days:
        return False
    return True


def fetch_opportunities(
    naics_codes: list[str] | None = None,
    min_days_until_due: int | None = None,
    api_key: str | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    """
    One SAM.gov API call per run. Rotates through NAICS codes and merges into a
    local cache so a full refresh takes len(naics_codes) runs.

    Returns (results, api_calls_used, status_message).
    """
    api_key = (api_key or os.getenv("SAM_GOV_API_KEY", "")).strip()
    if not api_key:
        raise ValueError(
            "SAM_GOV_API_KEY is required. "
            "Get a free key at sam.gov → Account Details → Public API Key."
        )

    naics_codes = naics_codes or _naics_from_env()
    min_days = min_days_until_due
    if min_days is None:
        min_days = int(os.getenv("MIN_DAYS_UNTIL_DUE", "14"))

    posted_to = date.today()
    posted_from = posted_to - timedelta(days=30)

    state = _load_json(STATE_FILE, {"rotation_index": 0})
    index = int(state.get("rotation_index", 0)) % len(naics_codes)
    naics_today = naics_codes[index]

    cache = _load_json(CACHE_FILE, {"updated": {}, "records": {}})
    records: dict[str, Any] = cache.get("records", {})
    updated: dict[str, str] = cache.get("updated", {})

    api_calls = 0
    added = 0

    with httpx.Client(timeout=60.0) as client:
        batch = _fetch_one_naics(client, api_key, naics_today, posted_from, posted_to)
        api_calls = 1

        for raw in batch:
            if not _set_aside_matches(raw):
                continue
            opp = _normalize(raw)
            nid = str(opp.get("notice_id") or "")
            if not nid:
                continue
            if nid not in records:
                added += 1
            records[nid] = opp

        updated[naics_today] = date.today().isoformat()
        state["rotation_index"] = (index + 1) % len(naics_codes)
        _save_json(CACHE_FILE, {"updated": updated, "records": records})
        _save_json(STATE_FILE, state)

    naics_set = set(naics_codes)
    results = [
        opp
        for opp in records.values()
        if str(opp.get("naics_code", "")) in naics_set and _passes_filters(opp, min_days)
    ]
    results.sort(key=lambda o: (o.get("days_until_due") is None, o.get("days_until_due") or 9999))

    loaded = len(updated)
    status = (
        f"Searched NAICS {naics_today} ({index + 1} of {len(naics_codes)}). "
        f"Added {added} new contract(s). Cache covers {loaded}/{len(naics_codes)} NAICS codes."
    )
    if loaded < len(naics_codes):
        status += f" Run again to fetch the next code ({naics_codes[(index + 1) % len(naics_codes)]})."

    return results, api_calls, status
