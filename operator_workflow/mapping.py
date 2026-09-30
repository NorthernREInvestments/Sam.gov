"""Static maps: existing source-system states → operator_workflow_state.

Every known enum value should appear here. Unmapped values fall through to
DISCOVERED with an UNMAPPED_* missing_information flag (never invent execution).
"""

from __future__ import annotations

from operator_workflow.constants import (
    OW_AWARDED,
    OW_BID_PREPARATION,
    OW_DEEP_RESEARCH,
    OW_DELIVERED,
    OW_DISCOVERED,
    OW_FINANCE_REVIEW,
    OW_HISTORY,
    OW_ORDERING,
    OW_PAID,
    OW_QUALIFIED,
    OW_SUBMITTED,
    OW_SUPPLIER_VALIDATION,
)

# ---------------------------------------------------------------------------
# m3_lifecycle.py (canonical derived lifecycle)
# ---------------------------------------------------------------------------
M3_LIFECYCLE_MAP: dict[str, str] = {
    "DISCOVERED": OW_DISCOVERED,
    "NORMALIZED": OW_DISCOVERED,
    "CHEAP_SCREENED": OW_QUALIFIED,
    "REJECTED_CHEAP_SCREEN": OW_HISTORY,
    "RESEARCH_QUEUED": OW_DEEP_RESEARCH,
    "RESEARCH_IN_PROGRESS": OW_DEEP_RESEARCH,
    "PACKAGE_REQUIRED": OW_DEEP_RESEARCH,
    "PACKAGE_ACQUIRED": OW_DEEP_RESEARCH,
    "PACKAGE_ACCESS_GATED": OW_DEEP_RESEARCH,
    "REQUIREMENTS_PARSED": OW_DEEP_RESEARCH,
    "BOM_READY": OW_DEEP_RESEARCH,
    "ECONOMICS_IN_PROGRESS": OW_DEEP_RESEARCH,
    "ECONOMICS_PRELIMINARY": OW_DEEP_RESEARCH,
    "ECONOMICS_ATTRACTIVE": OW_SUPPLIER_VALIDATION,  # attractive ≠ supplier/funding done
    "ECONOMICS_UNATTRACTIVE": OW_HISTORY,
    "FUNDING_VERIFICATION_REQUIRED": OW_FINANCE_REVIEW,
    "COMPLIANCE_IN_PROGRESS": OW_BID_PREPARATION,
    "COMPLIANCE_BLOCKED": OW_BID_PREPARATION,
    "PRICING_IN_PROGRESS": OW_SUPPLIER_VALIDATION,
    "COMMERCIAL_VERIFICATION_REQUIRED": OW_SUPPLIER_VALIDATION,
    "DRAFT_BID_READY": OW_BID_PREPARATION,
    "EXECUTION_NOT_READY": OW_BID_PREPARATION,
    "READY_FOR_OPERATOR_ACTION": OW_BID_PREPARATION,
    "CLOSED": OW_HISTORY,
    "CANCELLED": OW_HISTORY,
    "AWARDED": OW_AWARDED,
    "LOST": OW_HISTORY,
    "ARCHIVED": OW_HISTORY,
    "REJECTED": OW_HISTORY,
}

# ---------------------------------------------------------------------------
# m3_portfolio_deal_analysis.py Deal_state / ST_*
# ---------------------------------------------------------------------------
PORTFOLIO_STATE_MAP: dict[str, str] = {
    "DISCOVERED": OW_DISCOVERED,
    "CHEAP_SCREEN_PASSED": OW_QUALIFIED,
    "PACKAGE_RESEARCH": OW_DEEP_RESEARCH,
    "PRODUCT_IDENTITY_RESEARCH": OW_DEEP_RESEARCH,
    "GOVERNMENT_PRICE_RESEARCH": OW_DEEP_RESEARCH,
    "COMMERCIAL_PRICE_RESEARCH": OW_SUPPLIER_VALIDATION,
    "MATERIAL_GAP_RESEARCH": OW_DEEP_RESEARCH,
    "PARTIAL_ECONOMICS": OW_DEEP_RESEARCH,
    "ECONOMICS_ESTABLISHED": OW_SUPPLIER_VALIDATION,
    "COMMERCIAL_VERIFICATION_WORTHY": OW_SUPPLIER_VALIDATION,  # NOT bid-ready
    "FUNDING_VERIFICATION_REQUIRED": OW_FINANCE_REVIEW,
    "OWNER_REVIEW": OW_BID_PREPARATION,
    "DEFERRED": OW_HISTORY,
    "VERIFIED_BLOCKED": OW_HISTORY,
    "FIRST_TRANSACTION_CANDIDATE": OW_SUPPLIER_VALIDATION,  # strategic signal, not approval
}

# ---------------------------------------------------------------------------
# m3_opportunity_operating_read.py OP_*
# ---------------------------------------------------------------------------
OPERATOR_LIFECYCLE_MAP: dict[str, str] = {
    "NEW": OW_DISCOVERED,
    "SCREENED": OW_QUALIFIED,
    "UNDERSTANDING": OW_DEEP_RESEARCH,
    "PRODUCT_RESEARCH": OW_DEEP_RESEARCH,
    "SUPPLY_RESEARCH": OW_SUPPLIER_VALIDATION,
    "COMMERCIAL_VALIDATION": OW_SUPPLIER_VALIDATION,
    "DECISION_READY": OW_BID_PREPARATION,  # capped by evidence gates
    "EXECUTION": OW_BID_PREPARATION,
    "COMPLETE": OW_HISTORY,  # OP_COMPLETE = closed — NOT µLab COMPLETE
    "LEARNED": OW_HISTORY,
}

# ---------------------------------------------------------------------------
# deal_lifecycle.py (CRM / Contract)
# ---------------------------------------------------------------------------
DEAL_LIFECYCLE_MAP: dict[str, str] = {
    "NEW": OW_DISCOVERED,
    "QUALIFYING": OW_QUALIFIED,
    "RESEARCHING": OW_DEEP_RESEARCH,
    "SOURCING": OW_SUPPLIER_VALIDATION,
    "FUNDING": OW_FINANCE_REVIEW,
    "PRICING": OW_SUPPLIER_VALIDATION,
    "DEAL_READY": OW_BID_PREPARATION,
    "BUILDING_BID": OW_BID_PREPARATION,
    "BID_READY": OW_BID_PREPARATION,
    "SUBMITTED": OW_SUBMITTED,
    "AWARDED": OW_AWARDED,
    "PERFORMING": OW_ORDERING,
    "INVOICED": OW_DELIVERED,
    "PAID": OW_PAID,
    "CLOSED": OW_HISTORY,
    "WATCH": OW_DISCOVERED,
    "REJECTED": OW_HISTORY,
    "LOST": OW_HISTORY,
}

# ---------------------------------------------------------------------------
# award_lifecycle.py
# ---------------------------------------------------------------------------
AWARD_LIFECYCLE_MAP: dict[str, str] = {
    "SUBMITTED": OW_SUBMITTED,
    "AWARDED": OW_AWARDED,
    "PERFORMING": OW_ORDERING,
    "INVOICED": OW_DELIVERED,
    "PAID": OW_PAID,
    "LOST": OW_HISTORY,
    "CLOSED": OW_HISTORY,
}

# ---------------------------------------------------------------------------
# national_discovery_constants.py INV_*
# ---------------------------------------------------------------------------
INVENTORY_STATE_MAP: dict[str, str] = {
    "NEW": OW_DISCOVERED,
    "CHEAP_SCREENING": OW_DISCOVERED,
    "RESEARCH_QUEUED": OW_DEEP_RESEARCH,
    "AUTONOMOUS_RESEARCH": OW_DEEP_RESEARCH,
    "PRELIMINARY_POTENTIAL": OW_QUALIFIED,
    "PURSUIT_WORTHY_WITH_UNCERTAINTY": OW_DEEP_RESEARCH,
    "PURSUIT_WORTHY": OW_QUALIFIED,
    "FUTURE_COMMERCIAL_VERIFICATION": OW_SUPPLIER_VALIDATION,
    "FUTURE_FUNDING_VERIFICATION": OW_FINANCE_REVIEW,
    "BID_PREP": OW_BID_PREPARATION,
    "SUBMITTED": OW_SUBMITTED,
    "AWAITING_RESULT": OW_SUBMITTED,
    "AWARDED": OW_AWARDED,
    "NOT_AWARDED": OW_HISTORY,
    "CANCELLED": OW_HISTORY,
    "REJECTED": OW_HISTORY,
    "CLOSED": OW_HISTORY,
    "POST_SUBMISSION_AMENDMENT": OW_SUBMITTED,
    "CLARIFICATION_ACTIVITY": OW_BID_PREPARATION,
    "UNKNOWN_RESULT": OW_SUBMITTED,
}

# ---------------------------------------------------------------------------
# micro_purchase_lab_pipeline.py research_state
# ---------------------------------------------------------------------------
MICRO_LAB_STATE_MAP: dict[str, str] = {
    "RAW_UNRESOLVED": OW_DISCOVERED,
    "RESEARCH_IN_PROGRESS": OW_DEEP_RESEARCH,
    "RESEARCHABLE_PRODUCT_CANDIDATE": OW_DEEP_RESEARCH,
    "COMPLETE": OW_SUPPLIER_VALIDATION,  # research pack complete ≠ bid submitted
    "REJECTED": OW_HISTORY,
    "RESEARCH_INCOMPLETE_NO_HISTORY": OW_DEEP_RESEARCH,
    "RESEARCH_INCOMPLETE_NO_MARKET_PRICE": OW_SUPPLIER_VALIDATION,
    "RESEARCH_INCOMPLETE_WEAK_IDENTITY": OW_DEEP_RESEARCH,
    "RESEARCH_INCOMPLETE_VALUE_UNKNOWN": OW_DEEP_RESEARCH,
}

# µLab economics status (when present)
MICRO_LAB_ECON_MAP: dict[str, str] = {
    "NOT_CLASSED": OW_DEEP_RESEARCH,
    "SUPPLIER_QUOTE_NEEDED": OW_SUPPLIER_VALIDATION,
    "ECONOMIC_FAIL": OW_HISTORY,
    "ECONOMIC_PASS": OW_FINANCE_REVIEW,
    "EXECUTION_FAIL": OW_HISTORY,
    "BID_CANDIDATE": OW_BID_PREPARATION,
}

# ---------------------------------------------------------------------------
# Ready / verify style labels that appear on cards (not full lifecycles)
# ---------------------------------------------------------------------------
READINESS_LABEL_MAP: dict[str, str] = {
    "READY": OW_BID_PREPARATION,
    "READY_FOR_PRICING": OW_SUPPLIER_VALIDATION,
    "READY_FOR_ECONOMICS": OW_DEEP_RESEARCH,
    "READY_FOR_SUPPLIER_SEARCH": OW_SUPPLIER_VALIDATION,
    "READY_TO_REQUEST": OW_SUPPLIER_VALIDATION,
    "READY_FOR_BID_PREPARATION": OW_BID_PREPARATION,
    "READY_FOR_OPERATOR_ACTION": OW_BID_PREPARATION,
    "EXECUTION_READY": OW_BID_PREPARATION,
    "VERIFY": OW_FINANCE_REVIEW,
    "FUNDING_VERIFICATION_REQUIRED": OW_FINANCE_REVIEW,
    "FUNDING_CALL_READY": OW_FINANCE_REVIEW,
    "QUOTE_REQUIRED": OW_SUPPLIER_VALIDATION,
    "LEVEL_4_UNKNOWN": OW_DEEP_RESEARCH,
    "UNKNOWN": OW_DISCOVERED,
}

# Source system ids for conflict reporting
SRC_M3_LIFECYCLE = "m3_lifecycle"
SRC_PORTFOLIO = "portfolio_deal_state"
SRC_OPERATOR = "operator_lifecycle"
SRC_DEAL_CRM = "deal_lifecycle"
SRC_AWARD = "award_lifecycle"
SRC_INVENTORY = "inventory_stage"
SRC_MICRO_LAB = "micro_lab_research_state"
SRC_MICRO_ECON = "micro_lab_economics"
SRC_READINESS = "readiness_label"
SRC_EVIDENCE_GATE = "evidence_gate"


def map_state(table: dict[str, str], raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    key = str(raw).strip().upper()
    # Exact
    if key in table:
        return table[key]
    # Case-preserving lookup
    for k, v in table.items():
        if k.upper() == key:
            return v
    return None


ALL_SOURCE_TABLES: dict[str, dict[str, str]] = {
    SRC_M3_LIFECYCLE: M3_LIFECYCLE_MAP,
    SRC_PORTFOLIO: PORTFOLIO_STATE_MAP,
    SRC_OPERATOR: OPERATOR_LIFECYCLE_MAP,
    SRC_DEAL_CRM: DEAL_LIFECYCLE_MAP,
    SRC_AWARD: AWARD_LIFECYCLE_MAP,
    SRC_INVENTORY: INVENTORY_STATE_MAP,
    SRC_MICRO_LAB: MICRO_LAB_STATE_MAP,
    SRC_MICRO_ECON: MICRO_LAB_ECON_MAP,
    SRC_READINESS: READINESS_LABEL_MAP,
}
