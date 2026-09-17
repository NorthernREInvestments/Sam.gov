"""Entity-type and geographic coverage honesty for the national source network."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from national_discovery_constants import ENTITY_TYPES
from procurement_source_registry import ProcurementSourceRegistry

US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
]

PRODUCTIVE = {
    "HEALTHY_PRODUCTION",
    "PARTIALLY_PRODUCTIVE",
    "PUBLIC_METADATA_ONLY",
}
BLOCKED = {
    "AUTH_REQUIRED",
    "AUTH_GATED",
    "REGISTRATION_REQUIRED",
    "BOT_PROTECTED",
}


def _norm_entity(et: str | None) -> str:
    if not et:
        return "OTHER_PUBLIC_ENTITY"
    mapping = {
        "STATE": "STATE",
        "CITY": "CITY",
        "CITY_MUNICIPAL": "CITY",
        "MUNICIPAL": "CITY",
        "LOCAL": "CITY",
        "COUNTY": "COUNTY",
        "K12": "K12",
        "K12_SCHOOL_DISTRICT": "K12",
        "SCHOOL_DISTRICT": "K12",
        "HIGHER_ED": "HIGHER_ED",
        "HIGHER_EDUCATION": "HIGHER_ED",
        "UNIVERSITY": "HIGHER_ED",
        "AIRPORT": "AIRPORT",
        "TRANSIT": "TRANSIT",
        "UTILITY": "UTILITY",
        "AUTHORITY": "AUTHORITY",
        "COOPERATIVE": "COOPERATIVE",
        "FEDERAL": "OTHER_PUBLIC_ENTITY",
        "OTHER_PUBLIC": "OTHER_PUBLIC_ENTITY",
    }
    return mapping.get(str(et).upper(), "OTHER_PUBLIC_ENTITY")


def entity_type_coverage(
    registry: ProcurementSourceRegistry,
    *,
    live_records_by_source: dict[str, int] | None = None,
    survivors_by_source: dict[str, int] | None = None,
) -> dict[str, Any]:
    live_records_by_source = live_records_by_source or {}
    survivors_by_source = survivors_by_source or {}
    rows = []
    target_types = [
        "STATE", "CITY", "COUNTY", "K12", "HIGHER_ED", "AIRPORT", "TRANSIT",
        "UTILITY", "AUTHORITY", "COOPERATIVE", "OTHER_PUBLIC_ENTITY",
    ]
    buckets: dict[str, dict[str, Any]] = {
        t: {
            "entity_type": t,
            "registered": 0,
            "tested": 0,
            "healthy": 0,
            "partial": 0,
            "unknown_blocked": 0,
            "live_records": 0,
            "product_survivors": 0,
        }
        for t in target_types
    }
    for s in registry.all_sources():
        et = _norm_entity(s.get("entity_type"))
        if et not in buckets:
            et = "OTHER_PUBLIC_ENTITY"
        b = buckets[et]
        b["registered"] += 1
        h = s.get("health_state")
        if h in PRODUCTIVE or h in BLOCKED or h in {"DEGRADED", "BROKEN", "SOURCE_CHANGED", "UNSUPPORTED", "TEMPORARILY_UNAVAILABLE"}:
            b["tested"] += 1
        if h == "HEALTHY_PRODUCTION":
            b["healthy"] += 1
        elif h in {"PARTIALLY_PRODUCTIVE", "PUBLIC_METADATA_ONLY"}:
            b["partial"] += 1
        elif h in {"UNKNOWN", "DISCOVERED_UNVALIDATED", None} or h in BLOCKED:
            b["unknown_blocked"] += 1
        sid = s["source_id"]
        b["live_records"] += int(live_records_by_source.get(sid) or 0)
        b["product_survivors"] += int(survivors_by_source.get(sid) or 0)

    rows = list(buckets.values())
    weakest = sorted(rows, key=lambda r: (r["healthy"] + r["partial"], -r["registered"]))[:5]
    return {
        "kind": "EntityTypeCoverage",
        "by_entity_type": rows,
        "weakest_entity_types": [w["entity_type"] for w in weakest if w["healthy"] + w["partial"] == 0 or w["registered"] > 0],
        "note": "Counts are registry/live evidence — not a census of US public entities",
    }


def geographic_coverage(registry: ProcurementSourceRegistry) -> dict[str, Any]:
    """
    Distinguish STATEWIDE_SOURCE_PRESENT vs LOCAL_SOURCE_ONLY vs NO_VERIFIED_SOURCE.
    One city/university does NOT equal statewide coverage.
    """
    by_state: dict[str, dict[str, Any]] = {
        st: {
            "state": st,
            "registered_sources": 0,
            "productive_sources": 0,
            "statewide_productive": False,
            "local_productive": False,
            "classification": "NO_VERIFIED_SOURCE",
            "source_ids": [],
        }
        for st in US_STATES
    }
    for s in registry.all_sources():
        st = (s.get("jurisdiction") or s.get("geographic_scope") or "").upper()
        if len(st) != 2 or st not in by_state:
            # try parse from source_id
            for code in US_STATES:
                if s["source_id"].endswith(f"_{code.lower()}") or f"_{code.lower()}_" in s["source_id"]:
                    st = code
                    break
        if st not in by_state:
            continue
        slot = by_state[st]
        slot["registered_sources"] += 1
        slot["source_ids"].append(s["source_id"])
        productive = s.get("health_state") in PRODUCTIVE
        if productive:
            slot["productive_sources"] += 1
            et = _norm_entity(s.get("entity_type"))
            is_statewide = (
                et == "STATE"
                or str(s.get("government_level") or "").upper() in {"STATE", "STATEWIDE"}
                or s["source_id"].startswith("state_")
            )
            if is_statewide:
                slot["statewide_productive"] = True
            else:
                slot["local_productive"] = True

    for st, slot in by_state.items():
        if slot["statewide_productive"]:
            slot["classification"] = "STATEWIDE_SOURCE_PRESENT"
        elif slot["local_productive"]:
            slot["classification"] = "LOCAL_SOURCE_ONLY"
        elif slot["registered_sources"] > 0:
            slot["classification"] = "NO_VERIFIED_SOURCE"  # registered but not productive
            slot["note"] = "registered_but_nonproductive"
        else:
            slot["classification"] = "NO_VERIFIED_SOURCE"

    classes = Counter(s["classification"] for s in by_state.values())
    return {
        "kind": "GeographicCoverage",
        "states": list(by_state.values()),
        "summary": {
            "STATEWIDE_SOURCE_PRESENT": classes.get("STATEWIDE_SOURCE_PRESENT", 0),
            "LOCAL_SOURCE_ONLY": classes.get("LOCAL_SOURCE_ONLY", 0),
            "NO_VERIFIED_SOURCE": classes.get("NO_VERIFIED_SOURCE", 0),
        },
        "claim_national_market_coverage_percent": None,
        "fake_100_percent_coverage_claim": False,
        "honesty_note": "Local productive source ≠ statewide market coverage",
    }
