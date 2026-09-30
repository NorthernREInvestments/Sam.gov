"""Operator workflow projection — maps existing M3 states to one operator-facing ladder.

Read-only. Does not replace m3_lifecycle, portfolio, CRM, award, µLab, or inventory enums.
"""

from __future__ import annotations

from operator_workflow.constants import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNKNOWN,
    OPERATOR_STATES,
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
    STATE_RANK,
)
from operator_workflow.resolver import project_operator_workflow, map_single_source_state
from operator_workflow.summary import (
    attach_operator_workflow,
    build_operator_dashboard_payload,
    build_operator_deal_summary,
    operator_fields_dict,
)

__all__ = [
    "project_operator_workflow",
    "map_single_source_state",
    "attach_operator_workflow",
    "build_operator_deal_summary",
    "build_operator_dashboard_payload",
    "operator_fields_dict",
    "OPERATOR_STATES",
    "STATE_RANK",
    "OW_DISCOVERED",
    "OW_QUALIFIED",
    "OW_DEEP_RESEARCH",
    "OW_SUPPLIER_VALIDATION",
    "OW_FINANCE_REVIEW",
    "OW_BID_PREPARATION",
    "OW_SUBMITTED",
    "OW_AWARDED",
    "OW_ORDERING",
    "OW_DELIVERED",
    "OW_PAID",
    "OW_HISTORY",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_LOW",
    "CONFIDENCE_UNKNOWN",
]
