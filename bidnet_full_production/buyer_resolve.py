"""Buyer identity resolution + BIDNET_BUYER_DIRECTORY updates."""

from __future__ import annotations

import json
import re
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from bidnet_full_production.models import BUILD, BUYER_DIRECTORY, BUYER_HIGH, BUYER_LOW, BUYER_MEDIUM, BUYER_UNRESOLVED

_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
}


def _load_dir() -> dict[str, Any]:
    p = data_path(BUYER_DIRECTORY)
    if not p.exists():
        return {"kind": "BIDNET_BUYER_DIRECTORY", "build": BUILD, "by_buyer": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "BIDNET_BUYER_DIRECTORY", "build": BUILD, "by_buyer": {}}


def _save_dir(payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_utc().isoformat()
    payload["build"] = BUILD
    data_path(BUYER_DIRECTORY).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def resolve_buyer(row: dict[str, Any], *, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = parsed or {}
    agency = str(parsed.get("agency") or row.get("agency") or row.get("buyer") or "").strip()
    location = str(parsed.get("location") or row.get("location") or row.get("jurisdiction") or "").strip()
    title = str(row.get("title") or "")
    reasons: list[str] = []
    confidence = BUYER_UNRESOLVED
    normalized = agency or location or str(row.get("buyer") or "").strip()

    if agency and len(agency) > 10 and agency.lower() not in _STATE_NAMES:
        confidence = BUYER_HIGH
        reasons.append("authenticated_agency")
    elif agency and agency.lower() not in _STATE_NAMES:
        confidence = BUYER_MEDIUM
        reasons.append("agency_short")
    elif location and location.lower() not in _STATE_NAMES and len(location) > 8:
        confidence = BUYER_MEDIUM
        reasons.append("location_entity")
        normalized = location
    elif str(row.get("buyer") or "").strip() and str(row.get("buyer")).lower() not in _STATE_NAMES:
        confidence = BUYER_MEDIUM
        reasons.append("corpus_buyer")
    elif location or str(row.get("buyer") or ""):
        confidence = BUYER_LOW
        reasons.append("state_or_weak_buyer")
        normalized = normalized or str(row.get("buyer") or location)

    # Title entity hint
    m = re.search(
        r"\b((?:City|County|Town|School District|University|Authority|District)\s+of\s+[A-Z][A-Za-z .'-]{2,40})",
        title,
    )
    if m and confidence in {BUYER_UNRESOLVED, BUYER_LOW}:
        normalized = m.group(1)
        confidence = BUYER_MEDIUM
        reasons.append("title_entity")

    state = None
    for st in _STATE_NAMES:
        if st in f"{location} {agency} {row.get('buyer') or ''}".lower():
            state = st.upper()[:2] if len(st) <= 20 else None
            break
    if location and len(location) <= 3:
        state = location.upper()

    official_url = parsed.get("agency_source_url") or row.get("original_posting_url")
    portal = None
    if official_url:
        u = str(official_url).lower()
        if "opengov" in u:
            portal = "OpenGov"
        elif "ionwave" in u:
            portal = "IonWave"
        elif "planetbids" in u:
            portal = "PlanetBids"

    return {
        "raw_buyer_name": str(row.get("buyer") or agency or location),
        "normalized_buyer_name": normalized,
        "buyer_organization": agency or None,
        "department": row.get("department"),
        "state": state,
        "city": location if location and location.lower() not in _STATE_NAMES else None,
        "buyer_identity_confidence": confidence,
        "buyer_resolution_reasons": reasons,
        "official_procurement_url": official_url,
        "preferred_portal": portal,
    }


def remember_buyer_mapping(buyer_info: dict[str, Any], *, portal: str | None, success: bool) -> None:
    key = str(buyer_info.get("normalized_buyer_name") or buyer_info.get("raw_buyer_name") or "").strip().lower()[:120]
    if not key:
        return
    mem = _load_dir()
    row = mem.setdefault("by_buyer", {}).setdefault(
        key,
        {
            "buyer": buyer_info.get("normalized_buyer_name"),
            "state": buyer_info.get("state"),
            "portal_mappings": [],
            "success_count": 0,
        },
    )
    if portal:
        maps = row.setdefault("portal_mappings", [])
        if portal not in maps:
            maps.append(portal)
    if success:
        row["success_count"] = int(row.get("success_count") or 0) + 1
    _save_dir(mem)
