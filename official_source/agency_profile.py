"""Agency → procurement platform profiles (cached, reusable)."""

from __future__ import annotations

import json
import re
from typing import Any

from application_clock import now_utc

PROFILE_FILE = "m3_agency_procurement_profiles.json"

PLATFORM_OPENGOV = "OPENGOV"
PLATFORM_IONWAVE = "IONWAVE"
PLATFORM_PLANETBIDS = "PLANETBIDS_AGENCY"
PLATFORM_PUBLIC_PURCHASE = "PUBLIC_PURCHASE_AGENCY"
PLATFORM_STATE = "STATE_PORTAL"
PLATFORM_COUNTY = "COUNTY_PORTAL"
PLATFORM_CITY = "CITY_PORTAL"
PLATFORM_UNIVERSITY = "UNIVERSITY_PORTAL"
PLATFORM_SCHOOL = "SCHOOL_DISTRICT_PORTAL"
PLATFORM_UTILITY = "UTILITY_PORTAL"
PLATFORM_TRANSIT = "TRANSIT_PORTAL"
PLATFORM_AGENCY_NATIVE = "AGENCY_NATIVE"
PLATFORM_UNKNOWN = "UNKNOWN"


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(the|city|county|of|department|dept|purchasing|procurement)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _path():
    from m3_data_root import data_path

    return data_path(PROFILE_FILE)


def load_profiles() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"kind": "AgencyProcurementProfiles", "profiles": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "AgencyProcurementProfiles", "profiles": {}, "updated_at": None}


def save_profiles(payload: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def get_profile(agency_name: str) -> dict[str, Any] | None:
    key = _norm(agency_name)
    if not key:
        return None
    store = load_profiles()
    return (store.get("profiles") or {}).get(key)


def upsert_profile(
    *,
    agency_name: str,
    jurisdiction: str | None = None,
    platform_family: str,
    base_url: str | None = None,
    public_search_url: str | None = None,
    public_document_pattern: str | None = None,
    working_route: str | None = None,
    blocked_route: str | None = None,
    government_code: str | None = None,
    evidence: list[str] | None = None,
    confidence: float = 0.0,
) -> dict[str, Any]:
    store = load_profiles()
    profiles = store.setdefault("profiles", {})
    key = _norm(agency_name) or _norm(base_url or "") or "unknown"
    prior = dict(profiles.get(key) or {})
    profile = {
        "agency_name": agency_name,
        "normalized_agency_name": key,
        "jurisdiction": jurisdiction or prior.get("jurisdiction"),
        "platform_family": platform_family or prior.get("platform_family") or PLATFORM_UNKNOWN,
        "base_url": base_url or prior.get("base_url"),
        "public_search_url": public_search_url or prior.get("public_search_url"),
        "public_document_pattern": public_document_pattern or prior.get("public_document_pattern"),
        "working_route": working_route or prior.get("working_route"),
        "blocked_route": blocked_route if blocked_route is not None else prior.get("blocked_route"),
        "government_code": government_code or prior.get("government_code"),
        "last_verified": now_utc().isoformat(),
        "confidence": max(float(confidence or 0), float(prior.get("confidence") or 0)),
        "evidence": list(dict.fromkeys((prior.get("evidence") or []) + (evidence or [])))[:20],
    }
    profiles[key] = profile
    save_profiles(store)
    return profile


def detect_platform_from_url(url: str | None) -> str:
    u = (url or "").lower()
    if not u:
        return PLATFORM_UNKNOWN
    # Never treat BidNet itself as the official source platform
    if "bidnetdirect.com" in u or "bidnet.com" in u:
        return PLATFORM_UNKNOWN
    if "opengov.com" in u or "procurement.opengov" in u:
        return PLATFORM_OPENGOV
    if "ionwave.net" in u or "ionwave.com" in u:
        return PLATFORM_IONWAVE
    if "planetbids.com" in u or "pbsystem.planetbids" in u:
        return PLATFORM_PLANETBIDS
    if "publicpurchase.com" in u:
        return PLATFORM_PUBLIC_PURCHASE
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(u).netloc or "").lower()
    except Exception:
        host = ""
    if host.endswith(".edu") or any(x in host for x in ("university", "college")):
        return PLATFORM_UNIVERSITY
    # Require host-level school signals (avoid matching title/query text)
    if any(x in host for x in ("k12.", "school", "schooldistrict", "isd.", "usd.")) or host.endswith(
        (".k12.us", ".k12.ca.us")
    ):
        return PLATFORM_SCHOOL
    if any(x in host for x in ("transit", "metrobus", "mta.")):
        return PLATFORM_TRANSIT
    if any(x in host for x in ("utility", "water.", "electric", "power.")):
        return PLATFORM_UTILITY
    if host.endswith(".gov") or host.endswith(".us"):
        if "county" in host:
            return PLATFORM_COUNTY
        if any(x in host for x in ("city", "town", "village")):
            return PLATFORM_CITY
        if any(x in host for x in ("state", "purchasing", "procure")):
            return PLATFORM_STATE
        return PLATFORM_AGENCY_NATIVE
    return PLATFORM_UNKNOWN
