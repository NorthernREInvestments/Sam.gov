"""Plain-English display helpers for dashboard cards."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from usaspending_client import extract_work_location, normalize_state


def format_agency_display(agency: str | None) -> str:
    if not agency:
        return "Federal agency"
    text = str(agency).strip()
    if not text or text.startswith("{") or text.startswith("["):
        return "Federal agency"

    upper = text.upper()
    known = (
        ("FISH AND WILDLIFE", "US Fish and Wildlife"),
        ("FOREST SERVICE", "USDA Forest Service"),
        ("DEPT OF THE ARMY", "US Army"),
        ("DEPARTMENT OF THE ARMY", "US Army"),
        ("DEPT OF THE NAVY", "US Navy"),
        ("DEPT OF THE AIR FORCE", "US Air Force"),
        ("CORPS OF ENGINEERS", "US Army Corps of Engineers"),
        ("GENERAL SERVICES ADMINISTRATION", "GSA"),
        ("VETERANS AFFAIRS", "VA"),
        ("HOMELAND SECURITY", "DHS"),
        ("NATIONAL PARK SERVICE", "National Park Service"),
        ("BUREAU OF LAND MANAGEMENT", "BLM"),
    )
    for needle, label in known:
        if needle in upper:
            return label

    segments = [part.strip() for part in text.split(".") if part.strip()]
    for needle, label in known:
        for segment in segments:
            if needle in segment.upper():
                return label

    for segment in segments:
        seg_upper = segment.upper()
        if _is_parent_department_segment(seg_upper):
            continue
        if len(segment) > 3 and not seg_upper.startswith("USDA-"):
            return _title_agency(segment)

    if segments:
        return _title_agency(segments[-1])

    first = text.split(",")[0].strip()
    if first.upper() in ("AGRICULTURE", "AGRICULTURE, DEPARTMENT OF"):
        return "USDA"
    return _title_agency(first) or "Federal agency"


def _is_parent_department_segment(seg_upper: str) -> bool:
    if "DEPARTMENT OF" in seg_upper:
        return True
    if seg_upper in ("AGRICULTURE", "DEFENSE", "INTERIOR", "COMMERCE", "JUSTICE", "TREASURY"):
        return True
    if seg_upper.startswith("AGRICULTURE,"):
        return True
    return False


def format_work_location_short(
    location: str | None,
    sam_raw: dict[str, Any] | None = None,
    work: dict[str, Any] | None = None,
) -> str:
    work = work or extract_work_location(location, sam_raw)
    city = work.get("city")
    state = work.get("state_code")
    if city and state:
        return f"{city}, {state}"
    if state:
        return state
    label = work.get("label")
    if label:
        return _label_to_city_state(label) or label

    parsed = _parse_location_blob(location)
    if parsed:
        city = parsed.get("city")
        state = parsed.get("state_code")
        if city and state:
            return f"{city}, {state}"
        if state:
            return state
    if location and not str(location).strip().startswith("{"):
        cleaned = str(location).strip()
        if len(cleaned) <= 80:
            return cleaned
    return "Location pending"


def pricing_card_label(
    pricing_intel: dict[str, Any] | None,
    *,
    has_work_state: bool = False,
) -> str:
    intel = pricing_intel if isinstance(pricing_intel, dict) else {}
    avg = intel.get("average_annual_award")
    count = int(intel.get("awards_count") or 0)
    if avg and count > 0:
        return f"Hist: {short_money(avg)} avg"
    if intel.get("error") or count == 0:
        if has_work_state:
            return "First contract at this location"
        return "No history found"
    return "No history found"


def short_money(value: float | int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M".replace(".0M", "M")
    if amount >= 1_000:
        return f"${round(amount / 1_000)}k"
    return f"${amount:,.0f}"


def _title_agency(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" .")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower.startswith("dept of "):
        cleaned = cleaned[8:]
    return cleaned.title()


def _label_to_city_state(label: str) -> str | None:
    parts = [part.strip() for part in label.split(",") if part.strip()]
    if len(parts) >= 2:
        city = parts[0]
        state = normalize_state(parts[-1])
        if city and state:
            return f"{city}, {state}"
    return None


def _parse_location_blob(location: Any) -> dict[str, Any] | None:
    if isinstance(location, dict):
        block = location
    elif isinstance(location, str):
        text = location.strip()
        if not text.startswith("{"):
            return None
        try:
            block = json.loads(text.replace("'", '"'))
        except (json.JSONDecodeError, TypeError):
            try:
                block = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                return None
        if not isinstance(block, dict):
            return None
    else:
        return None

    from usaspending_client import _parse_sam_location_block

    city, state_code, _zip = _parse_sam_location_block(block)
    return {"city": city, "state_code": state_code}
