"""Diversified live source selection — priority orders execution; coverage is all eligible."""

from __future__ import annotations

from typing import Any

from discovery.agency_seeds import all_agencies_enriched, all_coops_enriched, FEDERAL_NON_SAM_LIVE
from discovery.bidnet_network import all_bidnet_networks_enriched
from discovery.constants import (
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_UNVERIFIED_LIVE,
    VALIDATABLE_STATUSES,
)
from discovery.state_matrix import all_states_enriched

# Deterministic easy-win validation priority (geographic diversity).
# Excludes known-hard AL/AK/AZ/CA from front of line; AR included if URL repaired.
STATE_VALIDATION_PRIORITY = [
    "state_ia",  # Jaggaer SciQuest public
    "state_mt",  # Jaggaer SciQuest public
    "state_tx",  # ESBD — prior productive
    "state_ne",  # NE purchasing — prior validation content
    "state_nh",  # NH bids — URL repaired to apps.das.nh.gov
    "state_id",  # Idaho purchasing root — URL repaired
    "state_nc",  # IPS — try before chronic parser failures
    # Chronic parser/empty/auth feeds demoted (source-health):
    "state_pa",
    "state_ga",
    "state_ar",
    "state_la",
    "state_ms",
    "state_ok",
    # WV demoted — auth/bulletin login only
]

LOCAL_VALIDATION_PRIORITY = [
    "agency_airport_dfw_tx",
    "agency_city_houston_tx",
    "agency_county_harris_tx",
    "agency_city_phoenix_az",
    "agency_city_denver_co",
    # agency_city_cheyenne_wy demoted — PublicPurchase home AUTH_REQUIRED / marketing
]

COOP_VALIDATION_PRIORITY = [
    "coop_sourcewell_live",
    "coop_hgac_live",
    "coop_naspo_live",
    "coop_buyboard_live",
    "coop_omnia_live",
    "coop_1gpa_live",
]

_BLOCKED_STATUSES = {"AUTH_REQUIRED", "BLOCKED", "BROKEN", "PLANNED", "UNSUPPORTED", "DISABLED"}


def _pool_map() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in all_states_enriched():
        if not s.get("list_url"):
            continue
        out[s["source_id"]] = {
            "source_id": s["source_id"],
            "name": s["name"],
            "list_url": s["list_url"],
            "adapter_family": s["adapter_family"],
            "platform_family": s.get("platform_family"),
            "kind": "STATE",
            "state_code": s["state"],
            "adapter_status": s.get("adapter_status"),
            "validation_candidate": s.get("validation_candidate", False),
        }
    for a in all_agencies_enriched():
        if not a.get("procurement_url"):
            continue
        out[a["source_id"]] = {
            "source_id": a["source_id"],
            "name": a["name"],
            "list_url": a["procurement_url"],
            "adapter_family": a["adapter_family"],
            "platform_family": a.get("platform_family"),
            "kind": "LOCAL",
            "state_code": a.get("state_code"),
            "adapter_status": a.get("adapter_status"),
            "validation_candidate": a.get("validation_candidate", True),
        }
    for c in all_coops_enriched():
        if not c.get("list_url"):
            continue
        out[c["source_id"]] = {
            "source_id": c["source_id"],
            "name": c["name"],
            "list_url": c["list_url"],
            "adapter_family": c["adapter_family"],
            "platform_family": "JSON",
            "kind": "COOPERATIVE",
            "adapter_status": c.get("adapter_status"),
            "validation_candidate": True,
        }
    for f in FEDERAL_NON_SAM_LIVE:
        if not f.get("list_url"):
            continue
        out[f["source_id"]] = {
            "source_id": f["source_id"],
            "name": f["name"],
            "list_url": f["list_url"],
            "adapter_family": f["adapter_family"],
            "kind": "FEDERAL",
            "adapter_status": ADAPTER_UNVERIFIED_LIVE if f.get("live_capable") else "PARTIAL",
            "validation_candidate": bool(f.get("list_url")),
            "platform_family": f.get("platform_family") or "FederalPublic",
        }

    # BidNet Direct statewide networks — one listing unlocks many local agencies
    for n in all_bidnet_networks_enriched():
        if not n.get("list_url"):
            continue
        out[n["source_id"]] = {
            "source_id": n["source_id"],
            "name": n["name"],
            "list_url": n["list_url"],
            "adapter_family": n["adapter_family"],
            "platform_family": n.get("platform_family"),
            "kind": "NETWORK",
            "state_code": n.get("state_code"),
            "coverage_class": n.get("coverage_class") or "LOCAL_NETWORK",
            "adapter_status": n.get("adapter_status"),
            "validation_candidate": n.get("validation_candidate", True),
        }

    # Apply verified alternate official routes (canonical may be stale/gated)
    try:
        from alternate_authoritative_routes import ALTERNATE_ROUTES

        for sid, route in ALTERNATE_ROUTES.items():
            row = out.get(sid)
            if not row:
                continue
            url = route.get("discovery_url")
            if not url:
                continue
            row["list_url"] = url
            row["alternate_route"] = route
            row["discovery_source"] = route.get("discovery_source")
            if route.get("adapter_family_override"):
                row["adapter_family"] = route["adapter_family_override"]
            if route.get("platform_family_override"):
                row["platform_family"] = route["platform_family_override"]
            expected = str(route.get("expected_access") or "").upper()
            if "REGISTRATION" in expected:
                row["expected_access"] = "REGISTRATION_REQUIRED"
            elif "AUTH" in expected:
                row["expected_access"] = "AUTH_REQUIRED"
    except Exception:
        pass

    # Normalize BidNet landing pages → open-bids wherever present
    try:
        from family_adapter_contract import normalize_bidnet_open_bids_url

        for row in out.values():
            if "bidnet" in str(row.get("adapter_family") or "").lower() or "bidnet" in str(
                row.get("platform_family") or ""
            ).lower():
                row["list_url"] = normalize_bidnet_open_bids_url(row.get("list_url")) or row.get("list_url")
            elif "bidnetdirect.com" in str(row.get("list_url") or "").lower():
                row["list_url"] = normalize_bidnet_open_bids_url(row.get("list_url")) or row.get("list_url")
    except Exception:
        pass
    return out


def _eligible(row: dict[str, Any], *, require_verified: bool = False) -> bool:
    st = (row.get("adapter_status") or "").upper()
    if st in _BLOCKED_STATUSES:
        return False
    if require_verified:
        return st == ADAPTER_LIVE_VERIFIED
    if st in {ADAPTER_LIVE_VERIFIED, ADAPTER_UNVERIFIED_LIVE, "DEGRADED", "PARTIAL"}:
        return True
    if st in VALIDATABLE_STATUSES:
        return True
    # Missing status but has URL + adapter → attempt (classify failure at runtime)
    if row.get("list_url") and row.get("adapter_family"):
        return True
    return False


def _priority_rank(source_id: str) -> int:
    """Lower = earlier. Priority controls ORDER only."""
    for i, sid in enumerate(STATE_VALIDATION_PRIORITY):
        if sid == source_id:
            return i
    for i, sid in enumerate(LOCAL_VALIDATION_PRIORITY):
        if sid == source_id:
            return 100 + i
    for i, sid in enumerate(COOP_VALIDATION_PRIORITY):
        if sid == source_id:
            return 200 + i
    fed_ids = [f["source_id"] for f in FEDERAL_NON_SAM_LIVE if f.get("list_url")]
    for i, sid in enumerate(fed_ids):
        if sid == source_id:
            return 50 + i
    # BidNet statewide networks — high yield; run before sparse agency duplicates
    if source_id.startswith("network_bidnet_"):
        return 40
    return 1000


def select_all_eligible_sources(
    *,
    require_verified: bool = False,
    status_overrides: dict[str, str] | None = None,
    include_blocked_accounted: bool = True,
) -> dict[str, Any]:
    """
    Full eligible coverage for BROAD/NATIONAL discovery.

    Priority determines execution ORDER, never silent exclusion.
    Blocked/auth sources are accounted for explicitly when include_blocked_accounted.
    """
    pool = _pool_map()
    if status_overrides:
        for sid, st in status_overrides.items():
            if sid in pool:
                pool[sid]["adapter_status"] = st

    eligible: list[dict[str, Any]] = []
    accounted: list[dict[str, Any]] = []
    for sid, row in pool.items():
        st = (row.get("adapter_status") or "").upper()
        if st in _BLOCKED_STATUSES:
            if include_blocked_accounted:
                accounted.append(
                    {
                        **row,
                        "selection_state": st,
                        "attempt": False,
                        "accounted_reason": st,
                    }
                )
            continue
        if not _eligible(row, require_verified=require_verified):
            if include_blocked_accounted:
                accounted.append(
                    {
                        **row,
                        "selection_state": "INELIGIBLE",
                        "attempt": False,
                        "accounted_reason": f"status={st or 'NONE'}",
                    }
                )
            continue
        eligible.append(row)

    # Order: verified first, then priority rank, then source_id for stability
    eligible.sort(
        key=lambda r: (
            0 if (r.get("adapter_status") or "").upper() == ADAPTER_LIVE_VERIFIED else 1,
            _priority_rank(r["source_id"]),
            r["source_id"],
        )
    )
    for r in eligible:
        r["selection_state"] = "ELIGIBLE"
        r["attempt"] = True

    return {
        "eligible": eligible,
        "accounted_non_attempt": accounted,
        "registered_in_pool": len(pool),
        "eligible_count": len(eligible),
        "accounted_non_attempt_count": len(accounted),
    }


def select_diversified_sources(
    *,
    max_sources: int | None = 5,
    require_verified: bool = False,
    status_overrides: dict[str, str] | None = None,
    all_eligible: bool = False,
) -> list[dict[str, Any]]:
    """
    TINY-style mix when max_sources is small / all_eligible=False.

    When all_eligible=True OR max_sources is None/large:
    return ALL eligible sources (priority = order only).
    """
    if all_eligible or max_sources is None or (isinstance(max_sources, int) and max_sources >= 999):
        return select_all_eligible_sources(
            require_verified=require_verified,
            status_overrides=status_overrides,
            include_blocked_accounted=False,
        )["eligible"]

    # Production caps ≥50 mean full eligible coverage slice, not priority-only
    if isinstance(max_sources, int) and max_sources >= 50:
        return select_all_eligible_sources(
            require_verified=require_verified,
            status_overrides=status_overrides,
            include_blocked_accounted=False,
        )["eligible"]

    pool = _pool_map()
    if status_overrides:
        for sid, st in status_overrides.items():
            if sid in pool:
                pool[sid]["adapter_status"] = st

    picked: list[dict[str, Any]] = []
    used: set[str] = set()
    cap = int(max_sources or 5)

    def take_from(priority: list[str], kind: str, n: int) -> None:
        nonlocal picked
        count = 0
        ordered = sorted(
            priority,
            key=lambda sid: 0 if (pool.get(sid) or {}).get("adapter_status") == ADAPTER_LIVE_VERIFIED else 1,
        )
        for sid in ordered:
            if count >= n or len(picked) >= cap:
                break
            row = pool.get(sid)
            if not row or sid in used:
                continue
            if row.get("kind") != kind and kind != "ANY":
                continue
            if not _eligible(row, require_verified=require_verified):
                continue
            if row.get("adapter_status") in _BLOCKED_STATUSES:
                continue
            picked.append(row)
            used.add(sid)
            count += 1

    take_from(STATE_VALIDATION_PRIORITY, "STATE", 2)
    take_from(LOCAL_VALIDATION_PRIORITY, "LOCAL", 1)
    take_from(COOP_VALIDATION_PRIORITY, "COOPERATIVE", 1)

    fed_ids = [f["source_id"] for f in FEDERAL_NON_SAM_LIVE if f.get("list_url")]
    take_from(fed_ids, "FEDERAL", 1)
    if len(picked) < cap:
        take_from(STATE_VALIDATION_PRIORITY, "STATE", cap - len(picked))
    if len(picked) < cap:
        take_from(LOCAL_VALIDATION_PRIORITY, "LOCAL", cap - len(picked))
    if len(picked) < cap:
        take_from(COOP_VALIDATION_PRIORITY, "COOPERATIVE", cap - len(picked))

    return picked[:cap]


def select_validation_candidates(*, max_sources: int = 15) -> list[dict[str, Any]]:
    """Ordered candidates for validate_discovery_sources.py — diverse, not alpha."""
    pool = _pool_map()
    out: list[dict[str, Any]] = []
    used: set[str] = set()

    for sid in STATE_VALIDATION_PRIORITY + LOCAL_VALIDATION_PRIORITY + COOP_VALIDATION_PRIORITY:
        row = pool.get(sid)
        if not row or sid in used:
            continue
        st = (row.get("adapter_status") or "").upper()
        if st in _BLOCKED_STATUSES:
            continue
        if not row.get("list_url"):
            continue
        out.append(row)
        used.add(sid)
        if len(out) >= max_sources:
            break
    return out


def asserts_not_alpha_first_five(selected: list[dict[str, Any]]) -> bool:
    """Guard: TINY must not be exactly AL/AK/AZ/AR/CA."""
    ids = [s["source_id"] for s in selected]
    bad = {"state_al", "state_ak", "state_az", "state_ar", "state_ca"}
    return set(ids) != bad
