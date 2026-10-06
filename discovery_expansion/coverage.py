"""Owner-facing discovery coverage dashboard — raw universe vs product candidates."""

from __future__ import annotations

from collections import Counter
from typing import Any

from application_clock import now_utc
from discovery_expansion.platform_status import platform_family_status_matrix, state_coverage_matrix


def _is_product_candidate(rec: dict[str, Any]) -> bool:
    """Tangible-product potential only — never shrink RAW LIVE by this gate."""
    cls = str(rec.get("universe_class") or rec.get("product_service_classification") or "").upper()
    if cls in {"TANGIBLE_PRODUCT", "MIXED_PRODUCT_SERVICE", "PRODUCT", "CORE_PRODUCT", "PRODUCT_RESALE", "PRODUCT_PLUS_SERVICE"}:
        return True
    if cls in {"SERVICE", "PURE_SERVICE", "CLEARLY_IRRELEVANT", "CONSTRUCTION"}:
        return False
    if rec.get("product_screen_survive") or rec.get("cheap_screen_survive") or rec.get("is_product"):
        return True
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    if rr.get("is_product") or rr.get("product_screen_survive"):
        return True
    if rec.get("eligible_for_profit_research"):
        return True
    return False


def _bucket_jurisdiction(rec: dict[str, Any]) -> str:
    if rec.get("is_federal") or str(rec.get("jurisdiction") or "").upper() in {"FEDERAL", "FED"}:
        return "Federal"
    j = str(rec.get("jurisdiction") or "").upper()
    plat = str(rec.get("platform") or "").lower()
    title = str(rec.get("title") or "").lower()
    if "coop" in j or "sourcewell" in plat or "naspo" in plat:
        return "Cooperative"
    if any(x in title for x in ("school", "isd", "university", "college", "usd ")):
        return "Education"
    if any(x in title for x in ("utility", "water authority", "electric", "transit", "airport")):
        return "Utilities"
    if j in {"STATE", "STATE_NETWORK"} or plat.startswith("state_"):
        return "State"
    if j in {"LOCAL", "CITY", "COUNTY", "MULTI_AGENCY_NETWORK"}:
        return "Local"
    return "Other"


def _last_harvest_telemetry() -> dict[str, Any]:
    try:
        from m3_data_root import data_path
        import json

        path = data_path("m3_discovery_expansion_last_harvest.json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def discovery_coverage_dashboard() -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import DEAD_FUNNEL_STATES, _is_available_rec

    store = load_store()
    raw_live = 0
    product = 0
    by_bucket: Counter = Counter()
    by_platform: Counter = Counter()
    truncated = []
    producing_sources: set[str] = set()

    for rec in store.values():
        if not isinstance(rec, dict):
            continue
        st = str(rec.get("current_funnel_state") or "")
        if st in DEAD_FUNNEL_STATES:
            continue
        fresh = str(rec.get("freshness") or "").upper()
        if fresh in {"EXPIRED", "CANCELED", "CANCELLED"}:
            continue
        if _is_available_rec(rec) or st in {
            "RAW",
            "ACCESSIBLE_PRODUCT",
            "FAST_RESEARCH_PENDING",
            "FAST_RESEARCH_COMPLETE",
            "DEEP_RESEARCH_PRIORITY",
            "DEEP_RESEARCH_IN_PROGRESS",
            "DEEP_RESEARCH_COMPLETE",
            "WATCH",
            "WATCH_FEDERAL_ACCESS",
            "WATCH_OTHER",
            "NEEDS_SOURCE_DATA",
            "LOW_PRIORITY_RESEARCH",
        }:
            raw_live += 1
            by_bucket[_bucket_jurisdiction(rec)] += 1
            plat = str(rec.get("platform") or "")
            if plat:
                by_platform[plat] += 1
                producing_sources.add(plat)
            if _is_product_candidate(rec):
                product += 1
        if rec.get("discovery_truncated"):
            truncated.append(rec.get("canonical_opportunity_id"))

    platforms = platform_family_status_matrix()
    states = state_coverage_matrix()
    harvest = _last_harvest_telemetry()

    # Known entities vs producing
    try:
        from pathlib import Path
        import json

        jpath = Path(__file__).resolve().parents[1] / "data" / "jurisdiction_procurement_registry.json"
        known_entities = 0
        producing = 0
        if jpath.exists():
            jdata = json.loads(jpath.read_text(encoding="utf-8"))
            ents = jdata.get("entities") or jdata.get("jurisdictions") or []
            if isinstance(ents, dict):
                ents = list(ents.values())
            known_entities = len(ents)
            producing = sum(
                1
                for e in ents
                if isinstance(e, dict) and (e.get("procurement_page") or e.get("bid_portal"))
            )
    except Exception:
        known_entities = 0
        producing = 0

    gaps = [
        {
            "platform": p["platform"],
            "known_entities": p["entities"],
            "live_entities": p.get("sources_ok") or 0,
            "current_opportunities": p["live_opps"],
            "status": p["status"],
            "blocker": p.get("blocker"),
            "expected_coverage_gain": p.get("expected_coverage_gain"),
        }
        for p in platforms["platforms"]
        if p["status"] != "WORKING" or (p["live_opps"] or 0) < 50
    ][:20]

    target = 16000
    auth_gaps = [
        g
        for g in gaps
        if g["status"] == "AUTH_REQUIRED"
        or (g.get("blocker") and "Login" in str(g.get("blocker")))
    ]
    return {
        "kind": "DiscoveryCoverageDashboard",
        "generated_at": now_utc().isoformat(),
        "RAW_LIVE": raw_live,
        "CANONICAL_LIVE": raw_live,
        "PRODUCT_CANDIDATES": product,
        "TOTAL_LIVE_DISCOVERY_UNIVERSE": raw_live,
        "target_raw_live": target,
        "gap_to_target": max(0, target - raw_live),
        "progress_pct": round(min(100.0, raw_live / target * 100.0), 2),
        "by_segment": {
            "Federal": by_bucket.get("Federal", 0),
            "State": by_bucket.get("State", 0),
            "Local": by_bucket.get("Local", 0),
            "Education": by_bucket.get("Education", 0),
            "Utilities": by_bucket.get("Utilities", 0),
            "Cooperative": by_bucket.get("Cooperative", 0),
            "Other": by_bucket.get("Other", 0),
        },
        "by_platform_in_store": dict(by_platform.most_common(40)),
        "platforms": platforms["platforms"],
        "priority_build_order": platforms.get("priority_build_order"),
        "state_coverage": states["summary"],
        "states": states["states"],
        "known_entities": known_entities,
        "entities_with_portals": producing,
        "entities_producing_records": len(producing_sources),
        "entities_not_producing": max(0, known_entities - producing),
        "largest_coverage_gaps": gaps,
        "authentication_blockers": auth_gaps[:15],
        "pagination_truncated_samples": truncated[:50],
        "last_harvest": {
            "run_id": harvest.get("run_id"),
            "raw_discovered": harvest.get("raw_discovered") or harvest.get("raw_opportunities"),
            "duplicates_removed": harvest.get("duplicates_removed"),
            "unique_records": harvest.get("unique_records"),
            "platform_family_counts": harvest.get("platform_family_counts"),
            "TOTAL_LIVE_DISCOVERY_UNIVERSE": harvest.get("TOTAL_LIVE_DISCOVERY_UNIVERSE"),
            "completed_at": harvest.get("completed_at"),
        }
        if harvest
        else None,
        "note": (
            "RAW LIVE = open opportunities in canonical store (post-dedupe). "
            "PRODUCT CANDIDATES = subset with tangible-product potential. "
            "Profit-first filtering is downstream and must not shrink RAW. "
            "Free account required portals: connect/login to expand coverage."
        ),
    }
