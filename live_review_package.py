"""Live opportunity review package for controlled first pursuits."""

from __future__ import annotations

from typing import Any

from first_pursuit_selection import score_first_pursuit_candidate
from m3_mobile_read_model import deal_room_summary
from m3_pipeline_store import M3PipelineStore
from operating_mode import is_controlled_verification, is_development_no_outreach, mode_snapshot


def live_opportunity_review_package(
    row: dict[str, Any] | None = None,
    *,
    canonical_id: str | None = None,
    store: M3PipelineStore | None = None,
) -> dict[str, Any]:
    """Complete operator review — reuses deal-room sections, never fabricates economics."""
    if row is None:
        store = store or M3PipelineStore()
        row = store.get(canonical_id or "")
    if not row:
        return {"error": "not_found", "canonical_id": canonical_id}

    deal = deal_room_summary(row)
    pursuit = score_first_pursuit_candidate(row)
    mode = mode_snapshot()

    next_actions = {
        "what_m3_can_do": deal.get("actions", {}).get("what_m3_can_do"),
        "what_operator_must_authorize": [
            "Enable CONTROLLED_REAL_WORLD_VERIFICATION (explicit acknowledgment)",
            "Authorize supplier verification per action",
            "Authorize financing verification per action",
            "Record evidence / outcomes in transaction learning store",
        ],
        "forbidden_without_future_production_mode": [
            "automatic outreach",
            "automatic bid submission",
            "automatic registration",
            "automatic signatures",
            "automatic commitments",
        ],
        "next_action": deal.get("actions", {}).get("next_action"),
        "why_not_ready": deal.get("actions", {}).get("why_not_ready"),
    }

    return {
        "kind": "M3LiveOpportunityReviewPackage",
        "canonical_id": row.get("canonical_id"),
        "overview": deal["overview"],
        "product_fit": deal["product_fit"],
        "requirements": {
            "bom_status": "PRESENT" if deal["requirements"].get("bom_lines") else "UNKNOWN",
            "bom_lines": deal["requirements"].get("bom_lines"),
            "specifications": [
                li.get("specification") for li in (deal["requirements"].get("bom_lines") or [])
            ],
            "missing_information": deal["requirements"].get("missing_information"),
            "package_access": deal["requirements"].get("package_access"),
        },
        "economics": deal["economics"],
        "funding": deal["funding"],
        "compliance": {
            "blockers": deal["compliance"].get("blockers"),
            "missing_requirements": deal["compliance"].get("missing_items"),
            "requirements": deal["compliance"].get("requirements"),
        },
        "next_actions": next_actions,
        "first_pursuit_score": pursuit,
        "operating_mode": mode,
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
        "controlled_verification_active": is_controlled_verification(),
        "safety": {
            "no_automatic_outreach": True,
            "no_automatic_bid_submission": True,
            "unknown_financing_is_not_rejection": True,
        },
    }
