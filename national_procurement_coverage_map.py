"""NATIONAL_PROCUREMENT_COVERAGE_MAP + discovery yield/gap analytics."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from discovery.selection import _pool_map, select_all_eligible_sources

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY",
]

LEVELS = [
    "FEDERAL", "STATE", "COUNTY", "CITY", "UNIVERSITY", "K12", "AIRPORT", "TRANSIT",
    "UTILITY", "AUTHORITY", "PUBLIC_HOSPITAL", "SPECIAL_DISTRICT", "NETWORK", "OTHER",
]

ACCESS_STATES = {
    "PUBLIC_PRODUCTIVE",
    "PUBLIC_PARTIAL",
    "PUBLIC_METADATA_ONLY",
    "AUTH_REQUIRED",
    "REGISTRATION_REQUIRED",
    "BOT_BLOCKED",
    "BROKEN_ROUTE",
    "STALE_ROUTE",
    "NO_CURRENT_RECORDS",
    "UNKNOWN",
}

GAP_TYPES = [
    "STATE_WITHOUT_STATEWIDE_SOURCE",
    "HIGH_VOLUME_PORTAL_BLOCKED",
    "PAGINATION_INCOMPLETE",
    "SOURCE_STALE",
    "PARSER_BROKEN",
    "NETWORK_NOT_EXPANDED",
    "FEDERAL_COVERAGE_GAP",
    "LOCAL_NETWORK_GAP",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _level_for(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "").upper()
    buyer = str(row.get("buyer_type") or row.get("jurisdiction") or "").upper()
    sid = str(row.get("source_id") or "").lower()
    if kind == "FEDERAL" or sid.startswith("fed_"):
        return "FEDERAL"
    if kind == "NETWORK" or sid.startswith("network_"):
        return "NETWORK"
    if kind == "STATE" or sid.startswith("state_"):
        return "STATE"
    if "COUNTY" in buyer or "county_" in sid:
        return "COUNTY"
    if "CITY" in buyer or "city_" in sid:
        return "CITY"
    if "UNIVERSITY" in buyer or "univ_" in sid or "HIGHER" in buyer:
        return "UNIVERSITY"
    if "SCHOOL" in buyer or "isd_" in sid or "usd_" in sid or "K12" in buyer:
        return "K12"
    if "AIRPORT" in buyer or "airport_" in sid:
        return "AIRPORT"
    if "TRANSIT" in buyer or "transit_" in sid:
        return "TRANSIT"
    if "UTIL" in buyer or "utility_" in sid:
        return "UTILITY"
    if "HOSPITAL" in buyer:
        return "PUBLIC_HOSPITAL"
    if "AUTHORITY" in buyer:
        return "AUTHORITY"
    if kind == "COOPERATIVE":
        return "OTHER"
    return "OTHER"


def classify_access_state(metrics: dict[str, Any] | None, row: dict[str, Any] | None = None) -> str:
    m = metrics or {}
    row = row or {}
    expected = str(row.get("expected_access") or "").upper()
    stop = str(m.get("source_stop_reason") or m.get("pagination_stop_reason") or m.get("failure_type") or "").upper()
    root = str(m.get("root_cause") or m.get("explicit_state") or "").upper()
    raw = int(m.get("raw") or m.get("records_fetched") or m.get("raw_records") or 0)
    ok = bool(m.get("ok"))
    if "REGISTRATION" in expected or "REGISTRATION" in stop or "REGISTRATION" in root:
        return "REGISTRATION_REQUIRED"
    if "AUTH" in expected or "AUTH" in stop or "401" in stop or "403" in stop:
        return "AUTH_REQUIRED"
    if "BOT" in stop or "CLOUDFLARE" in stop or "CAPTCHA" in stop:
        return "BOT_BLOCKED"
    if "STALE" in stop or "404" in stop:
        return "STALE_ROUTE"
    if "BROKEN" in stop or "PARSER" in stop or "TECHNICAL" in root:
        return "BROKEN_ROUTE"
    if ok and raw > 0:
        if m.get("pagination_complete") is False or stop == "PAGINATION_INCOMPLETE":
            return "PUBLIC_PARTIAL"
        if m.get("public_metadata_only") or (row.get("platform_family") or "").upper() == "BIDNET":
            return "PUBLIC_METADATA_ONLY" if raw > 0 else "PUBLIC_PRODUCTIVE"
        return "PUBLIC_PRODUCTIVE"
    if ok and raw == 0:
        return "NO_CURRENT_RECORDS"
    if stop or root:
        return "BROKEN_ROUTE"
    return "UNKNOWN"


def build_source_yield_analytics(
    *,
    per_source: dict[str, Any] | None = None,
    opportunities: list[dict[str, Any]] | None = None,
    product_survivors_by_source: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Per-source yield rows for prioritization."""
    pool = _pool_map()
    per_source = per_source or {}
    survivors = product_survivors_by_source or {}
    contrib: Counter[str] = Counter()
    for o in opportunities or []:
        sid = str(o.get("source_id") or "")
        if sid:
            contrib[sid] += 1
    rows = []
    for sid, cand in pool.items():
        m = per_source.get(sid) or {}
        raw = int(m.get("raw") or m.get("records_fetched") or contrib.get(sid) or 0)
        unique = int(m.get("unique") or m.get("unique_records") or contrib.get(sid) or 0)
        dup_rate = 0.0
        if raw > 0 and unique >= 0:
            dup_rate = max(0.0, 1.0 - (unique / raw)) if unique <= raw else 0.0
        rows.append(
            {
                "source_id": sid,
                "portal_family": cand.get("platform_family") or cand.get("adapter_family") or "UNKNOWN",
                "kind": cand.get("kind"),
                "state_code": cand.get("state_code") or cand.get("state"),
                "current_records": raw,
                "unique_records_contributed": unique,
                "product_survivors_contributed": int(survivors.get(sid) or 0),
                "duplicate_rate": round(dup_rate, 4),
                "failure_rate": 0.0 if m.get("ok") else (1.0 if m else None),
                "average_runtime_seconds": m.get("runtime_seconds"),
                "last_success": m.get("last_success"),
                "access_state": classify_access_state(m, cand),
                "ok": bool(m.get("ok")) if m else None,
            }
        )
    rows.sort(key=lambda r: (-int(r["current_records"] or 0), r["source_id"]))
    return rows


def build_national_procurement_coverage_map(
    *,
    per_source: dict[str, Any] | None = None,
    registry_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Durable coverage map — measurable facts only (no fake national %)."""
    pool = _pool_map()
    per_source = per_source or {}
    eligible = select_all_eligible_sources()
    by_level: dict[str, list[str]] = defaultdict(list)
    by_family: Counter[str] = Counter()
    productive_by_family: Counter[str] = Counter()
    state_status: dict[str, str] = {s: "NO_VERIFIED_SOURCE" for s in US_STATES}

    productive_ids: set[str] = set()
    for sid, cand in pool.items():
        level = _level_for(cand)
        by_level[level].append(sid)
        fam = str(cand.get("platform_family") or cand.get("adapter_family") or "UNKNOWN")
        by_family[fam] += 1
        m = per_source.get(sid) or {}
        raw = int(m.get("raw") or m.get("records_fetched") or 0)
        if m.get("ok") and raw > 0:
            productive_ids.add(sid)
            productive_by_family[fam] += 1

        sc = str(cand.get("state_code") or cand.get("state") or "").upper()
        if sc in state_status:
            kind = str(cand.get("kind") or "").upper()
            coverage_class = str(cand.get("coverage_class") or "").upper()
            is_state_portal = kind == "STATE" and sid.startswith("state_")
            is_network = kind == "NETWORK" or coverage_class == "LOCAL_NETWORK"
            if sid in productive_ids:
                if is_state_portal and coverage_class != "LOCAL_NETWORK":
                    # Productive official state portal → statewide verified
                    state_status[sc] = "STATEWIDE_SOURCE_VERIFIED"
                elif state_status[sc] == "NO_VERIFIED_SOURCE":
                    state_status[sc] = "LOCAL_ONLY" if (is_network or kind == "LOCAL") else "PARTIAL_COVERAGE"
                elif state_status[sc] == "LOCAL_ONLY" and is_state_portal:
                    state_status[sc] = "STATEWIDE_SOURCE_VERIFIED"
                elif state_status[sc] == "LOCAL_ONLY":
                    state_status[sc] = "PARTIAL_COVERAGE"

    # Registry overlay when available
    if registry_sources:
        for s in registry_sources:
            sid = s.get("source_id")
            sc = str(s.get("state_code") or s.get("state") or "").upper()
            health = str(s.get("health_state") or "").upper()
            if sc in state_status and health in {"HEALTHY_PRODUCTION", "PARTIALLY_PRODUCTIVE"}:
                if str(s.get("source_id") or "").startswith("state_"):
                    if state_status[sc] != "STATEWIDE_SOURCE_VERIFIED":
                        # Only upgrade if productive metrics also confirm, else PARTIAL
                        if sid in productive_ids:
                            state_status[sc] = "STATEWIDE_SOURCE_VERIFIED"
                        elif state_status[sc] == "NO_VERIFIED_SOURCE":
                            state_status[sc] = "PARTIAL_COVERAGE"

    statewide = sorted(s for s, st in state_status.items() if st == "STATEWIDE_SOURCE_VERIFIED")
    local_only = sorted(s for s, st in state_status.items() if st == "LOCAL_ONLY")
    partial = sorted(s for s, st in state_status.items() if st == "PARTIAL_COVERAGE")
    missing = sorted(s for s, st in state_status.items() if st == "NO_VERIFIED_SOURCE")

    return {
        "kind": "NATIONAL_PROCUREMENT_COVERAGE_MAP",
        "generated_at": _now(),
        "registered_in_pool": len(pool),
        "eligible_sources": eligible.get("eligible_count"),
        "productive_sources": len(productive_ids),
        "by_government_level": {k: sorted(v) for k, v in sorted(by_level.items())},
        "level_counts": {k: len(v) for k, v in sorted(by_level.items())},
        "portal_family_distribution": dict(by_family.most_common()),
        "portal_family_productive": dict(productive_by_family.most_common()),
        "geographic": {
            "federal_nationwide": sorted(by_level.get("FEDERAL") or []),
            "states": {
                "STATEWIDE_SOURCE_VERIFIED": statewide,
                "LOCAL_ONLY": local_only,
                "PARTIAL_COVERAGE": partial,
                "NO_VERIFIED_SOURCE": missing,
                "by_state": state_status,
            },
        },
        "claim_percent_of_all_solicitations": None,
        "notes": [
            "STATEWIDE_SOURCE_VERIFIED requires a productive official state portal, not only a city/network hit",
            "BidNet/network productivity is LOCAL_NETWORK / PARTIAL unless a state-owned portal is also productive",
            "No fake national solicitation percentage — denominator unknown",
        ],
    }


def build_discovery_gap_queue(
    *,
    coverage_map: dict[str, Any] | None = None,
    yield_rows: list[dict[str, Any]] | None = None,
    per_source: dict[str, Any] | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Rank discovery gaps by expected opportunity yield."""
    coverage_map = coverage_map or build_national_procurement_coverage_map(per_source=per_source)
    yield_rows = yield_rows or build_source_yield_analytics(per_source=per_source)
    gaps: list[dict[str, Any]] = []

    geo = (coverage_map.get("geographic") or {}).get("states") or {}
    for st in geo.get("NO_VERIFIED_SOURCE") or []:
        gaps.append(
            {
                "gap_type": "STATE_WITHOUT_STATEWIDE_SOURCE",
                "target": st,
                "expected_yield": 90,
                "detail": f"No productive verified source for {st}",
            }
        )
    for st in geo.get("LOCAL_ONLY") or []:
        gaps.append(
            {
                "gap_type": "STATE_WITHOUT_STATEWIDE_SOURCE",
                "target": st,
                "expected_yield": 70,
                "detail": f"{st} has local/network only — no statewide portal verified productive",
            }
        )

    fed = (coverage_map.get("by_government_level") or {}).get("FEDERAL") or []
    fed_prod = [
        r for r in yield_rows
        if str(r.get("source_id") or "").startswith("fed_") and int(r.get("current_records") or 0) > 0
    ]
    if len(fed_prod) < 2:
        gaps.append(
            {
                "gap_type": "FEDERAL_COVERAGE_GAP",
                "target": "FEDERAL",
                "expected_yield": 95,
                "detail": f"Only {len(fed_prod)} productive federal sources of {len(fed)} registered",
            }
        )

    for r in yield_rows:
        access = r.get("access_state")
        fam = str(r.get("portal_family") or "UNKNOWN").upper()
        if access in {"BOT_BLOCKED", "AUTH_REQUIRED", "REGISTRATION_REQUIRED", "BROKEN_ROUTE"}:
            base = 60
            if fam in {"BIDNET", "BONFIRE", "JAGGAER", "OPENGOV", "PUBLIC_PURCHASE", "DIBBS", "SAM"}:
                base = 85
            gaps.append(
                {
                    "gap_type": "HIGH_VOLUME_PORTAL_BLOCKED",
                    "target": r["source_id"],
                    "expected_yield": base,
                    "detail": f"{r['source_id']} access={access} family={fam}",
                    "access_state": access,
                    "portal_family": fam,
                }
            )
        stop = str((per_source or {}).get(r["source_id"], {}).get("pagination_stop_reason") or "")
        if stop == "PAGINATION_INCOMPLETE" or access == "PUBLIC_PARTIAL":
            gaps.append(
                {
                    "gap_type": "PAGINATION_INCOMPLETE",
                    "target": r["source_id"],
                    "expected_yield": 75,
                    "detail": f"{r['source_id']} pagination incomplete — more current records likely",
                }
            )

    # Network not expanded: few BidNet networks productive
    bidnet_prod = [r for r in yield_rows if "BIDNET" in str(r.get("portal_family") or "").upper() and int(r.get("current_records") or 0) > 0]
    bidnet_all = [r for r in yield_rows if "BIDNET" in str(r.get("portal_family") or "").upper()]
    if bidnet_all and len(bidnet_prod) < max(5, len(bidnet_all) // 4):
        gaps.append(
            {
                "gap_type": "NETWORK_NOT_EXPANDED",
                "target": "BidNet",
                "expected_yield": 92,
                "detail": f"Only {len(bidnet_prod)}/{len(bidnet_all)} BidNet sources productive",
            }
        )

    gaps.sort(key=lambda g: (-int(g.get("expected_yield") or 0), g.get("gap_type") or "", g.get("target") or ""))
    # Dedupe by (gap_type, target)
    seen: set[tuple[str, str]] = set()
    out = []
    for g in gaps:
        key = (str(g.get("gap_type")), str(g.get("target")))
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
        if len(out) >= limit:
            break
    return out


def build_discovery_coverage_health(
    *,
    coverage_map: dict[str, Any] | None = None,
    yield_rows: list[dict[str, Any]] | None = None,
    funnel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage_map = coverage_map or build_national_procurement_coverage_map()
    yield_rows = yield_rows or []
    geo = (coverage_map.get("geographic") or {}).get("states") or {}
    productive = [r for r in yield_rows if int(r.get("current_records") or 0) > 0]
    records_per = [int(r.get("current_records") or 0) for r in productive]
    avg = (sum(records_per) / len(records_per)) if records_per else 0.0
    funnel = funnel or {}
    return {
        "kind": "DISCOVERY_COVERAGE_HEALTH",
        "generated_at": _now(),
        "total_sources": coverage_map.get("registered_in_pool"),
        "eligible_sources": coverage_map.get("eligible_sources"),
        "productive_sources": coverage_map.get("productive_sources") or len(productive),
        "records_per_productive_source_avg": round(avg, 2),
        "states_statewide_covered": len(geo.get("STATEWIDE_SOURCE_VERIFIED") or []),
        "states_partial": len(geo.get("PARTIAL_COVERAGE") or []) + len(geo.get("LOCAL_ONLY") or []),
        "states_missing": len(geo.get("NO_VERIFIED_SOURCE") or []),
        "federal_sources_registered": len((coverage_map.get("by_government_level") or {}).get("FEDERAL") or []),
        "local_network_sources": len((coverage_map.get("by_government_level") or {}).get("NETWORK") or []),
        "portal_family_coverage": coverage_map.get("portal_family_productive"),
        "funnel": {
            "TOTAL_CURRENT_UNIQUE_DISCOVERED": funnel.get("TOTAL_CURRENT_UNIQUE_DISCOVERED"),
            "TOTAL_CHEAP_SCREENED": funnel.get("TOTAL_CHEAP_SCREENED"),
            "LIKELY_PRODUCT_RESALE": funnel.get("LIKELY_PRODUCT_RESALE"),
            "PRODUCT_PLUS_MINOR_SERVICE": funnel.get("PRODUCT_PLUS_MINOR_SERVICE"),
            "MIXED": funnel.get("MIXED"),
            "UNKNOWN": funnel.get("UNKNOWN"),
            "SERVICE_DEFERRED": funnel.get("SERVICE_DEFERRED"),
            "CONSTRUCTION_DEFERRED": funnel.get("CONSTRUCTION_DEFERRED"),
        },
        "national_percent_claim": None,
    }


def build_product_survivor_universe(opportunities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Cheap-screen class totals for the discovered universe."""
    rows = opportunities or []
    counts: Counter[str] = Counter()
    for o in rows:
        cls = str(
            o.get("cheap_screen_class")
            or (o.get("cheap_screen") or {}).get("class")
            or o.get("product_class")
            or o.get("product_classification")
            or o.get("classification")
            or "UNKNOWN"
        ).upper()
        # Map discovery classifier labels → survivor funnel labels
        if cls in {"CORE_PRODUCT", "PRODUCT_RESALE", "LIKELY_PRODUCT_RESALE"}:
            cls = "LIKELY_PRODUCT_RESALE"
        elif cls in {"PRODUCT_PLUS_SERVICE", "PRODUCT_PLUS_MINOR_SERVICE"}:
            cls = "PRODUCT_PLUS_MINOR_SERVICE"
        elif cls in {"SERVICE", "LIKELY_SERVICE", "PROFESSIONAL_SERVICE"}:
            cls = "SERVICE_DEFERRED"
        elif cls in {"CONSTRUCTION", "CONSTRUCTION_DEFERRED"}:
            cls = "CONSTRUCTION_DEFERRED"
        elif cls in {"CLEARLY_IRRELEVANT"}:
            cls = "SERVICE_DEFERRED"
        counts[cls] += 1
    productish = (
        counts.get("LIKELY_PRODUCT_RESALE", 0)
        + counts.get("PRODUCT_PLUS_MINOR_SERVICE", 0)
    )
    return {
        "kind": "PRODUCT_SURVIVOR_UNIVERSE",
        "TOTAL_CURRENT_UNIQUE_DISCOVERED": len(rows),
        "TOTAL_CHEAP_SCREENED": sum(counts.values()),
        "LIKELY_PRODUCT_RESALE": counts.get("LIKELY_PRODUCT_RESALE", 0),
        "PRODUCT_PLUS_MINOR_SERVICE": counts.get("PRODUCT_PLUS_MINOR_SERVICE", 0),
        "MIXED": counts.get("MIXED", 0),
        "UNKNOWN": counts.get("UNKNOWN", 0),
        "SERVICE_DEFERRED": counts.get("SERVICE_DEFERRED", 0) + counts.get("LIKELY_SERVICE", 0) + counts.get("PROFESSIONAL_SERVICE", 0),
        "CONSTRUCTION_DEFERRED": counts.get("CONSTRUCTION_DEFERRED", 0) + counts.get("CONSTRUCTION", 0),
        "plausible_product_transaction_survivors": productish + counts.get("MIXED", 0) + counts.get("UNKNOWN", 0),
        "by_class": dict(counts),
    }
