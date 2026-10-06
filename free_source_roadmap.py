"""Free-national source roadmap + coverage categories for owner UI."""

from __future__ import annotations

from typing import Any

# Owner-facing coverage categories
FREE_ACTIVE = "FREE_ACTIVE"
FREE_PARTIAL = "FREE_PARTIAL"
FREE_PENDING_ACCOUNT = "FREE_PENDING_ACCOUNT"
PAID_OPTIONAL = "PAID_OPTIONAL"
BROKEN = "BROKEN"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

# Priority order for free national / multi-state coverage
FREE_SOURCE_PRIORITY: list[dict[str, Any]] = [
    {
        "source": "BidNet",
        "priority": 1,
        "category": FREE_ACTIVE,
        "notes": "~96.9% of reported open harvested via partitioned national+state fill-in",
        "next_action": "Continue incremental recovery + detail/docs",
    },
    {
        "source": "OpenGov",
        "priority": 2,
        "category": FREE_ACTIVE,
        "notes": "Public POST government/{code}/project/public — 322+ structured entities producing",
        "next_action": "Twice-daily cascade + detail recovery on product candidates",
    },
    {
        "source": "PublicPurchase",
        "priority": 3,
        "category": FREE_PENDING_ACCOUNT,
        "notes": "Vendor account pending approval; architecture prepared",
        "next_action": "Activate auth/session harvest after approval",
    },
    {
        "source": "PlanetBids",
        "priority": 4,
        "category": FREE_PARTIAL,
        "notes": "HTML adapter exists; L.17.3 mapped portals largely ADAPTER_FAILED / host DNS issues",
        "next_action": "Audit portal patterns; fix reachable agency portals",
    },
    {
        "source": "IonWave",
        "priority": 5,
        "category": FREE_PARTIAL,
        "notes": "Public portal fetcher present; agency-scoped",
        "next_action": "Expand known IonWave portals + pagination",
    },
    {
        "source": "DemandStar",
        "priority": 6,
        "category": FREE_PARTIAL,
        "notes": "Fetcher present if free-access model supports useful discovery",
        "next_action": "Validate free national search vs agency-gated",
    },
    {
        "source": "Periscope/BidSync",
        "priority": 7,
        "category": NOT_IMPLEMENTED,
        "notes": "Queued after higher-yield free families",
        "next_action": "Adapter design after Public Purchase + PlanetBids gains",
    },
    {
        "source": "State-hosted systems",
        "priority": 8,
        "category": FREE_PARTIAL,
        "notes": "Heterogeneous state portals; selective harvest",
        "next_action": "Prioritize high-volume state boards",
    },
    {
        "source": "Universities/schools/utilities/transit",
        "priority": 9,
        "category": FREE_PARTIAL,
        "notes": "Platform-family clusters (Bonfire agency hubs, etc.)",
        "next_action": "Harvest via free public agency portals only",
    },
    {
        "source": "Other free public networks",
        "priority": 10,
        "category": NOT_IMPLEMENTED,
        "notes": "Opportunistic expansion",
        "next_action": "Add when volume justifies",
    },
    {
        "source": "Euna/Bonfire",
        "priority": 99,
        "category": PAID_OPTIONAL,
        "notes": "Connector preserved; nationwide requires ~$100/state/year — not default national source",
        "next_action": "Enable via EUNA_ENABLED_STATES for targeted profitable states only",
    },
]


def source_coverage_categories() -> dict[str, Any]:
    """Build owner dashboard of free vs paid-optional sources."""
    rows = []
    for item in FREE_SOURCE_PRIORITY:
        rows.append(
            {
                "source": item["source"],
                "category": item["category"],
                "priority": item["priority"],
                "notes": item["notes"],
                "next_action": item["next_action"],
                "counts_toward_national_health": item["category"]
                in {FREE_ACTIVE, FREE_PARTIAL, FREE_PENDING_ACCOUNT},
            }
        )
    # Overlay live status from auth modules when available
    try:
        from bidnet_auth import owner_connection_status as bn

        st = bn()
        for r in rows:
            if r["source"] == "BidNet":
                r["live_status"] = st.get("status")
                r["harvested"] = st.get("harvested")
                r["reported_open"] = st.get("reported_open")
    except Exception:
        pass
    try:
        from opengov_auth import owner_connection_status as og

        st = og()
        for r in rows:
            if r["source"] == "OpenGov":
                r["live_status"] = st.get("status")
                r["category"] = FREE_PARTIAL
    except Exception:
        pass
    try:
        from euna_auth import owner_connection_status as eu

        st = eu()
        for r in rows:
            if r["source"] == "Euna/Bonfire":
                r["live_status"] = st.get("status")
                r["category"] = PAID_OPTIONAL
                r["national_discovery_enabled"] = st.get("national_discovery_enabled")
                r["enabled_states"] = st.get("enabled_states_display") or "NONE"
                r["counts_toward_national_health"] = False
    except Exception:
        pass
    try:
        from planetbids_audit import planetbids_audit_summary

        pb = planetbids_audit_summary()
        for r in rows:
            if r["source"] == "PlanetBids":
                r["adapter_state"] = pb.get("overall_state")
                r["category"] = pb.get("coverage_category") or r["category"]
                r["audit"] = {
                    "mapped": pb.get("mapped"),
                    "working": pb.get("working"),
                    "partial": pb.get("partial"),
                    "broken": pb.get("broken"),
                }
    except Exception:
        pass
    try:
        from public_purchase_prep import public_purchase_status

        pp = public_purchase_status()
        for r in rows:
            if r["source"] == "PublicPurchase":
                r["live_status"] = pp.get("status")
                r["category"] = pp.get("coverage_category") or FREE_PENDING_ACCOUNT
    except Exception:
        pass

    free_rows = [r for r in rows if r["category"] != PAID_OPTIONAL]
    return {
        "kind": "FreeSourceCoverageDashboard",
        "strategy": "FREE_NATIONAL_MULTI_STATE_FIRST",
        "target_canonical_live": 50000,
        "sources": rows,
        "free_sources": free_rows,
        "paid_optional": [r for r in rows if r["category"] == PAID_OPTIONAL],
        "priority_order": [r["source"] for r in rows if r["priority"] < 99],
        "primary_repair_target": "OpenGov",
        "next_free_source_after_opengov": "PublicPurchase",
    }
