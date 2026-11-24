"""Owner-facing procurement queues (canonical).

Unresolved records never appear under verified / bid-eligible queues.
Build 002Q: primary dispositions are mutually exclusive and explicit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

# Primary owner dispositions (Build 002Q repair)
PRIMARY_DISPOSITIONS = (
    "verified_bid_opportunities",
    "verified_product_information_needed",
    "potential_product_classification_needed",
    "easy_vendor_registration",
    "confirmed_disqualification",
    "closed_expired_non_solicitation",
)

# Legacy + recovery queues retained for UI compatibility
QUEUE_KEYS = PRIMARY_DISPOSITIONS + (
    "qualified_commercial_evaluation",
    "awaiting_supplier_quotations",
    "awaiting_financial_evaluation",
    "bids_ready_for_owner_approval",
    "promising_awaiting_verification",
    "missing_document",
    "restricted_document",
    "future_recurring_purchase",
    "estimated_quantity",
    "archived_or_closed",
    "verified_procurement_fits",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def queues_root() -> Path:
    p = get_settings().data_root() / "procurement_queues"
    p.mkdir(parents=True, exist_ok=True)
    return p


def empty_queues() -> dict[str, list]:
    return {k: [] for k in QUEUE_KEYS}


def write_queues(queues: dict[str, list], *, build: str | None = None) -> dict[str, int]:
    root = queues_root()
    summary: dict[str, int] = {}
    for qname in QUEUE_KEYS:
        rows = list(queues.get(qname) or [])
        # Safety: never allow unresolved into verified/bid queues
        if qname in {
            "verified_bid_opportunities",
            "verified_procurement_fits",
            "qualified_commercial_evaluation",
            "bids_ready_for_owner_approval",
        }:
            rows = [
                r
                for r in rows
                if not r.get("unresolved")
                and (
                    r.get("disposition")
                    in {
                        "verified_bid_opportunities",
                        "verified_procurement_fit",
                        "verified_procurement_fits",
                        "qualified_commercial_evaluation",
                        "bids_ready_for_owner_approval",
                        qname,
                        None,
                        "",
                    }
                    or r.get("queue") == qname
                )
            ]
        path = root / f"{qname}.json"
        path.write_text(
            json.dumps({"queue": qname, "count": len(rows), "rows": rows}, indent=2, default=str),
            encoding="utf-8",
        )
        summary[qname] = len(rows)
    index = {
        "updated_at": _utc(),
        "build": build,
        "queues": summary,
        "primary_dispositions": {k: summary.get(k, 0) for k in PRIMARY_DISPOSITIONS},
        "note": "Unresolved never listed as verified or bid-eligible",
    }
    (root / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return summary


def map_pipeline_to_queue(pipe: dict[str, Any], *, package_status: str | None = None) -> str:
    """Map procurement pipeline result to a recovery/legacy queue key."""
    stage = pipe.get("stage")
    excluded = bool(pipe.get("excluded"))
    unknown = set(pipe.get("unknown_flags") or [])
    qty_sem = (pipe.get("quantity_semantic") or "").upper()
    pkg = (package_status or "").upper()

    if excluded and any(
        str(r).startswith("class:services")
        or str(r).startswith("class:construction")
        or str(r).startswith("class:professional")
        or str(r).startswith("class:software")
        for r in (pipe.get("reasons") or [])
    ):
        return "confirmed_disqualification"
    if pkg in {"ACCESS_RESTRICTED", "RESTRICTED"}:
        return "restricted_document"
    if pkg in {"MISSING", "FAILED", "NOT_STARTED"} or "quantity_not_found_in_accessible_text" in (
        pipe.get("investigation_trail") or []
    ):
        if not pipe.get("quantity_defined"):
            return (
                "missing_document"
                if pkg in {"MISSING", "FAILED", "NOT_STARTED", ""}
                else "promising_awaiting_verification"
            )
    if qty_sem in {"ESTIMATED_QUANTITY", "MAXIMUM_AUTHORIZED_QUANTITY"} or "quantity_estimated_not_firm_order" in unknown:
        return "estimated_quantity"
    if "quantity_indefinite_not_fixed_purchase" in unknown:
        return "future_recurring_purchase"
    if stage == "VERIFIED_M3_PROCUREMENT_FIT" and pipe.get("quantity_defined"):
        return "qualified_commercial_evaluation"
    if pipe.get("product_opportunity"):
        return "promising_awaiting_verification"
    return "promising_awaiting_verification"


def map_to_primary_disposition(
    item: dict[str, Any],
    pipe: dict[str, Any],
    *,
    bid: dict[str, Any] | None = None,
    package_status: str | None = None,
) -> str:
    """Mutually exclusive primary disposition (Build 002Q)."""
    bid = bid or {}
    status = str(item.get("solicitation_status") or item.get("status") or "").lower()
    req = str(item.get("requirement_class") or pipe.get("enriched_fields", {}).get("requirement_class") or "")
    pkg = (package_status or item.get("package_status") or "").upper()
    reasons = [str(r) for r in (pipe.get("reasons") or [])]

    if status in {"closed", "cancelled", "canceled", "expired", "historical_award", "forecast"}:
        return "closed_expired_non_solicitation"
    if req == "not_applicable_non_solicitation" or str(item.get("record_type") or "") in {
        "HISTORICAL_AWARD",
        "PROCUREMENT_FORECAST",
        "NON_SOLICITATION",
        "PURCHASING_PLAN",
    }:
        return "closed_expired_non_solicitation"

    if pipe.get("excluded") and any(
        r.startswith("class:") or r.startswith("it_software") or "confirmed_service" in r
        for r in reasons
    ):
        return "confirmed_disqualification"

    # Verified bid: Stage C + bid_eligible (procurement gates only)
    if (
        pipe.get("stage") == "VERIFIED_M3_PROCUREMENT_FIT"
        and pipe.get("quantity_defined")
        and bid.get("bid_eligible")
    ):
        url = str(item.get("official_url") or item.get("source_url") or "").lower()
        needs_reg = bool(item.get("vendor_registration_required")) and not item.get(
            "vendor_registration_complete"
        )
        if needs_reg or "sam.gov" in url or "planetbids" in url or "bonfirehub" in url:
            return "easy_vendor_registration"
        return "verified_bid_opportunities"

    # Stage C with qty but not yet bid-eligible → info/eligibility needed (not DQ)
    if pipe.get("product_opportunity") and pipe.get("quantity_defined"):
        if any(
            "registration" in str(g.get("evidence") or "").lower()
            for g in (bid.get("gates") or {}).values()
            if isinstance(g, dict) and g.get("status") in {"fail", "unknown"}
        ):
            return "easy_vendor_registration"
        return "verified_product_information_needed"

    # Confirmed/potential product missing qty/specs/submission
    if pipe.get("product_opportunity"):
        unknowns = set(pipe.get("unknown_flags") or [])
        if "potential_product_insufficient_classification" in unknowns or "requirement_class_unknown" in unknowns:
            if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
                return "potential_product_classification_needed"
        return "verified_product_information_needed"

    if pkg in {"ACCESS_RESTRICTED"} and not pipe.get("excluded"):
        return "verified_product_information_needed"

    if pipe.get("excluded"):
        return "confirmed_disqualification"

    return "potential_product_classification_needed"
