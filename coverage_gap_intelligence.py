"""Coverage gap intelligence — what M3 does NOT yet cover."""

from __future__ import annotations

from typing import Any

from national_discovery_constants import ENTITY_TYPES
from procurement_source_registry import ProcurementSourceRegistry

# Lightweight expected coverage anchors — not a full US local-gov census
_EXPECTED_STATE_SOURCES = {f"state_{s}" for s in (
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks","ky","la",
    "me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok",
    "or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy",
)}

_MAJOR_CITIES = [
    ("agency_city_new_york_ny", "New York City", "NY"),
    ("agency_city_los_angeles_ca", "Los Angeles", "CA"),
    ("agency_city_chicago_il", "Chicago", "IL"),
    ("agency_city_houston_tx", "Houston", "TX"),
    ("agency_city_phoenix_az", "Phoenix", "AZ"),
    ("agency_city_philadelphia_pa", "Philadelphia", "PA"),
    ("agency_city_san_antonio_tx", "San Antonio", "TX"),
    ("agency_city_san_diego_ca", "San Diego", "CA"),
    ("agency_city_dallas_tx", "Dallas", "TX"),
    ("agency_city_denver_co", "Denver", "CO"),
]

_MAJOR_AIRPORTS = [
    ("agency_airport_atl_ga", "ATL"),
    ("agency_airport_ord_il", "ORD"),
    ("agency_airport_dfw_tx", "DFW"),
    ("agency_airport_lax_ca", "LAX"),
    ("agency_airport_den_co", "DEN"),
]


def build_coverage_gap_report(registry: ProcurementSourceRegistry) -> dict[str, Any]:
    known_ids = {s["source_id"] for s in registry.all_sources()}
    healthy = {s["source_id"] for s in registry.by_health("HEALTHY_PRODUCTION")}
    degraded = {s["source_id"] for s in registry.by_health("DEGRADED")}
    partial = {s["source_id"] for s in registry.by_health("PARTIALLY_PRODUCTIVE")}
    auth = {s["source_id"] for s in registry.by_health("AUTH_REQUIRED")}

    missing_states = sorted(_EXPECTED_STATE_SOURCES - known_ids)
    weak_states = sorted(
        sid for sid in _EXPECTED_STATE_SOURCES
        if sid in known_ids and sid not in healthy and sid not in degraded and sid not in partial
    )
    missing_cities = [c for c in _MAJOR_CITIES if c[0] not in known_ids]
    missing_airports = [a for a in _MAJOR_AIRPORTS if a[0] not in known_ids]

    by_entity = registry.coverage_summary()["KNOWN_SOURCE_COVERAGE"]["by_entity_type"]
    missing_entity_types = [e for e in ENTITY_TYPES if e not in by_entity]

    parser_failures = [
        s["source_id"]
        for s in registry.all_sources()
        if s.get("failure_class") == "PARSER_FAILURE" or s.get("health_state") == "QUARANTINED"
    ]

    # Platform adapter gaps — registered but not productive
    from collections import Counter

    fam_counts = Counter()
    fam_healthy = Counter()
    for s in registry.all_sources():
        fam = s.get("platform_family") or s.get("source_family") or "UNKNOWN"
        fam_counts[fam] += 1
        if s.get("health_state") in {"HEALTHY_PRODUCTION", "PARTIALLY_PRODUCTIVE"}:
            fam_healthy[fam] += 1
    platform_adapter_gaps = [
        {
            "platform_family": fam,
            "registered": fam_counts[fam],
            "productive": fam_healthy[fam],
            "gap": fam_counts[fam] - fam_healthy[fam],
            "gap_type": "PLATFORM_ADAPTER_GAP",
        }
        for fam in fam_counts
        if fam_counts[fam] - fam_healthy[fam] > 0
    ]
    platform_adapter_gaps.sort(key=lambda g: -g["gap"])

    return {
        "kind": "SourceGapReport",
        "UNRESOLVED_COVERAGE_GAPS": {
            "STATE_COVERAGE_GAP": {
                "states_missing_from_registry": missing_states,
                "states_present_but_not_healthy": weak_states,
            },
            "COUNTY_COVERAGE_GAP": {
                "note": "Major counties largely unregistered — engineering priority, not opportunity proof",
            },
            "HIGHER_ED_COVERAGE_GAP": {
                "entity_count": by_entity.get("HIGHER_EDUCATION", 0),
                "note": "Higher-ed coverage thin relative to national institutions",
            },
            "AIRPORT_TRANSIT_GAP": {
                "major_airports_missing": [{"id": a[0], "code": a[1]} for a in missing_airports],
                "transit_sources": by_entity.get("TRANSIT", 0),
            },
            "PLATFORM_ADAPTER_GAP": platform_adapter_gaps[:12],
            "major_cities_missing": [{"id": c[0], "name": c[1], "state": c[2]} for c in missing_cities],
            "entity_types_with_zero_sources": missing_entity_types,
            "parser_failure_or_quarantined": parser_failures[:40],
            "auth_gated_sources": sorted(auth)[:40],
            "notes": [
                "Not a complete census of US local governments",
                "Gaps feed weekly source discovery priorities",
                "Gaps are engineering priorities, not claims that opportunities definitely exist",
            ],
        },
        "KNOWN_SOURCE_COVERAGE": registry.coverage_summary()["KNOWN_SOURCE_COVERAGE"],
        "claim_100_percent_national_coverage": False,
    }


def weekly_discovery_priorities(gap_report: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = gap_report.get("UNRESOLVED_COVERAGE_GAPS") or {}
    state_gap = gaps.get("STATE_COVERAGE_GAP") or {}
    airport_gap = gaps.get("AIRPORT_TRANSIT_GAP") or {}
    priorities = []
    for city in (gaps.get("major_cities_missing") or [])[:10]:
        priorities.append({"type": "CITY", "target": city, "priority": 20})
    for ap in (airport_gap.get("major_airports_missing") or gaps.get("major_airports_missing") or [])[:5]:
        priorities.append({"type": "AIRPORT", "target": ap, "priority": 25})
    for st in (state_gap.get("states_present_but_not_healthy") or gaps.get("states_present_but_not_healthy") or [])[:10]:
        priorities.append({"type": "STATE_REPAIR", "target": st, "priority": 15})
    for et in (gaps.get("entity_types_with_zero_sources") or [])[:8]:
        priorities.append({"type": "ENTITY_TYPE", "target": et, "priority": 40})
    for fam in (gaps.get("PLATFORM_ADAPTER_GAP") or [])[:8]:
        priorities.append({"type": "PLATFORM_ADAPTER", "target": fam, "priority": 18})
    return priorities
