"""DLA procurement source map + access investigation (no anti-bot bypass)."""

from __future__ import annotations

from typing import Any

from federal_dla_constants import (
    DIBBS_AUTH_REQUIRED,
    DIBBS_BOT_BLOCKED_AUTOMATION,
    DIBBS_DIRECT_PARTIAL,
    DIBBS_DIRECT_PUBLIC_MACHINE_ACCESSIBLE,
    DIBBS_REGISTRATION_REQUIRED,
    DIBBS_SAM_RECONCILED,
    DIBBS_UNKNOWN,
)

# Official / historically documented public DLA procurement endpoints
DLA_PROCUREMENT_SOURCE_MAP: list[dict[str, Any]] = [
    {
        "source_id": "dla_dibbs_rfq_recents",
        "name": "DIBBS Recent RFQs",
        "url": "https://www.dibbs.bsm.dla.mil/RFQ/RfqRecents.aspx",
        "buying_activity": "DLA_ENTERPRISE",
        "solicitation_families": ["SPE*", "SPR*"],
        "machine_interface": "HTML_LISTING",
        "expected_access": "PUBLIC_OR_BOT_PROTECTED",
        "overlap_with_sam": "PARTIAL",
        "notes": "Primary DIBBS RFQ recent list — historically bot/403 for automated clients",
    },
    {
        "source_id": "dla_dibbs_rfq_by_fsc",
        "name": "DIBBS RFQs by FSC",
        "url": "https://www.dibbs.bsm.dla.mil/RFQ/RfqByFsc.aspx",
        "buying_activity": "DLA_ENTERPRISE",
        "solicitation_families": ["SPE*", "SPR*"],
        "machine_interface": "HTML_FORM",
        "expected_access": "PUBLIC_OR_BOT_PROTECTED",
        "overlap_with_sam": "PARTIAL",
        "notes": "FSC browse entry — same DIBBS host protections",
    },
    {
        "source_id": "dla_sam_contract_opportunities",
        "name": "DLA opportunities via SAM.gov",
        "url": "https://api.sam.gov/opportunities/v2/search",
        "buying_activity": "DLA_ALL",
        "solicitation_families": ["SPE*", "SPR*", "OTHER"],
        "machine_interface": "OFFICIAL_API",
        "expected_access": "API_KEY",
        "overlap_with_sam": "AUTHORITATIVE_FOR_SAM_POSTED",
        "notes": "Identify DLA via organization path; prefixes supplementary only",
    },
    {
        "source_id": "dla_land_and_maritime",
        "name": "DLA Land and Maritime",
        "url": "https://www.dla.mil/Land-and-Maritime/",
        "buying_activity": "DLA_LAND_AND_MARITIME",
        "solicitation_families": ["SPE7*", "SPR*"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
        "notes": "Buying activity landing — solicitations via DIBBS/SAM",
    },
    {
        "source_id": "dla_aviation",
        "name": "DLA Aviation",
        "url": "https://www.dla.mil/Aviation/",
        "buying_activity": "DLA_AVIATION",
        "solicitation_families": ["SPE4*", "SPR*"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
    },
    {
        "source_id": "dla_troop_support",
        "name": "DLA Troop Support",
        "url": "https://www.dla.mil/TroopSupport/",
        "buying_activity": "DLA_TROOP_SUPPORT",
        "solicitation_families": ["SPE1*", "SPE2*", "SPE3*", "SPE5*", "SPE6*", "SPE8*"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
    },
    {
        "source_id": "dla_distribution",
        "name": "DLA Distribution",
        "url": "https://www.dla.mil/Distribution/",
        "buying_activity": "DLA_DISTRIBUTION",
        "solicitation_families": ["SPE*"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
    },
    {
        "source_id": "dla_disposition",
        "name": "DLA Disposition Services",
        "url": "https://www.dla.mil/DispositionServices/",
        "buying_activity": "DLA_DISPOSITION",
        "solicitation_families": ["SPE*"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
    },
    {
        "source_id": "dla_energy",
        "name": "DLA Energy",
        "url": "https://www.dla.mil/Energy/",
        "buying_activity": "DLA_ENERGY",
        "solicitation_families": ["SPE6*", "OTHER"],
        "machine_interface": "AGENCY_LANDING",
        "expected_access": "NAVIGATION_ONLY",
        "overlap_with_sam": "INDIRECT",
        "notes": "Energy procurement scope may differ from NSN RFQs",
    },
    {
        "source_id": "piee_public_index",
        "name": "PIEE Public Solicitation Index",
        "url": "https://piee.eb.mil/sol/xhtml/unauth/index.xhtml",
        "buying_activity": "DOD_MULTI",
        "solicitation_families": ["W91*", "N00*", "SPE*", "FA*"],
        "machine_interface": "HTML_INDEX",
        "expected_access": "PUBLIC_OR_AUTH",
        "overlap_with_sam": "PARTIAL",
    },
]


def classify_dibbs_access_from_metrics(metrics: dict[str, Any] | None) -> str:
    m = metrics or {}
    stop = str(m.get("source_stop_reason") or m.get("pagination_stop_reason") or m.get("failure_type") or "").upper()
    raw = int(m.get("raw") or m.get("records_fetched") or 0)
    if "BOT" in stop or "CLOUDFLARE" in stop or "CAPTCHA" in stop:
        return DIBBS_BOT_BLOCKED_AUTOMATION
    if "AUTH" in stop or "401" in stop or "403" in stop:
        return DIBBS_AUTH_REQUIRED
    if "REGISTRATION" in stop:
        return DIBBS_REGISTRATION_REQUIRED
    if stop in {"EMPTY_PAGE", "PARSER_FAILURE", "VALIDATION_FAILURE"} and raw == 0:
        # Public listing returns no parseable RFQs — treat as automation-blocked / non-machine
        return DIBBS_BOT_BLOCKED_AUTOMATION
    if m.get("ok") and raw > 0:
        return DIBBS_DIRECT_PUBLIC_MACHINE_ACCESSIBLE if raw >= 10 else DIBBS_DIRECT_PARTIAL
    if m.get("ok") and raw == 0:
        return DIBBS_DIRECT_PARTIAL
    return DIBBS_UNKNOWN


def probe_dibbs_access(*, authorize_live: bool = False, timeout: float = 20.0) -> dict[str, Any]:
    """ONE bounded probe of DIBBS recent RFQs — no bypass."""
    if not authorize_live:
        return {"executed": False, "error": "authorize_live_required", "access_state": DIBBS_UNKNOWN}
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.live_fetchers import DibbsLiveFetcher

    url = "https://www.dibbs.bsm.dla.mil/RFQ/RfqRecents.aspx"
    budget = RequestBudget(
        max_total_requests=2,
        max_requests_per_source=2,
        max_pages_per_source=1,
        max_records_per_source=50,
        max_runtime_seconds=60,
        min_interval_seconds=0.5,
        timeout_seconds=timeout,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
    try:
        result = DibbsLiveFetcher().fetch_listing(
            client,
            list_url=url,
            source_id="probe_dibbs",
            max_pages=1,
            pagination_exhaust=False,
        )
        validation = result.get("validation") or {}
        records = int(result.get("records_fetched") or 0)
        fail = str(validation.get("failure_type") or result.get("pagination_stop_reason") or "")
        state = classify_dibbs_access_from_metrics(
            {
                "ok": bool(validation.get("valid")) and records >= 0,
                "raw": records,
                "source_stop_reason": fail,
                "pagination_stop_reason": result.get("pagination_stop_reason"),
            }
        )
        if fail.upper() in {"BOT_PROTECTED", "AUTH_REQUIRED"} or result.get("pagination_stop_reason") in {
            "BOT_PROTECTED",
            "AUTH_REQUIRED",
        }:
            state = (
                DIBBS_BOT_BLOCKED_AUTOMATION
                if "BOT" in fail.upper() or result.get("pagination_stop_reason") == "BOT_PROTECTED"
                else DIBBS_AUTH_REQUIRED
            )
        return {
            "executed": True,
            "url": url,
            "access_state": state,
            "records_fetched": records,
            "validation": validation.get("failure_type") or validation.get("health_status"),
            "pagination_stop_reason": result.get("pagination_stop_reason"),
            "bypass_attempted": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "executed": True,
            "url": url,
            "access_state": DIBBS_UNKNOWN,
            "error": str(exc)[:300],
            "bypass_attempted": False,
        }


def build_dla_source_map_report(*, dibbs_probe: dict[str, Any] | None = None, sam_dla_count: int = 0) -> dict[str, Any]:
    dibbs_state = (dibbs_probe or {}).get("access_state") or DIBBS_UNKNOWN
    coverage_mode = DIBBS_SAM_RECONCILED if sam_dla_count > 0 else dibbs_state
    return {
        "kind": "DLA_PROCUREMENT_SOURCE_MAP",
        "sources": DLA_PROCUREMENT_SOURCE_MAP,
        "dibbs_probe": dibbs_probe,
        "dibbs_access_state": dibbs_state,
        "coverage_mode": coverage_mode,
        "sam_dla_count": sam_dla_count,
        "anti_bot_bypass": 0,
    }
