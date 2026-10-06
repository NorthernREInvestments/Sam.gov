"""Owner UI fields — never headline unproven profit as actionable."""

from __future__ import annotations

from typing import Any

from line_basket_completion_strict_economics.models import (
    ECONOMICS_NOT_READY,
    PROFIT_UNPROVEN,
)


def owner_view(result: dict[str, Any]) -> dict[str, Any]:
    econ = result.get("economics") or {}
    cov = result.get("coverage") or {}
    lines = result.get("lines") or {}
    material_unresolved = max(
        0,
        int(cov.get("material_lines") or 0) - int(cov.get("material_priced") or 0),
    )

    profit_display = econ.get("expected_profit")
    headline_profit = econ.get("headline") or "PROFIT NOT YET PROVEN"
    if econ.get("economics_status") == ECONOMICS_NOT_READY or econ.get("profit_confidence") == PROFIT_UNPROVEN:
        profit_display = None
        headline_profit = "PROFIT NOT YET PROVEN"

    next_action = "Continue material basket research"
    if econ.get("economics_status") == ECONOMICS_NOT_READY:
        if material_unresolved > 0:
            next_action = f"Price {material_unresolved} remaining material lines"
        else:
            next_action = "Obtain bounded quotes for material remainder"
    elif econ.get("profit_confidence") in {"PROFIT_PROVEN", "PROFIT_LIKELY"}:
        if result.get("lender_ready_object"):
            next_action = "Prepare lender package (do not contact lender yet)"
        else:
            next_action = "Review delivery / execution risks"
    elif int(lines.get("QUOTE_REQUIRED_LINES") or 0) > 0 or any(
        l.get("terminal_state") == "QUOTE_REQUIRED" for l in (result.get("line_rows") or [])
    ):
        next_action = "Hidden quote reserve — material quotes only"

    return {
        "basket_coverage": cov.get("LINE_COUNT_COVERAGE"),
        "material_coverage": cov.get("MATERIAL_VALUE_COVERAGE"),
        "lines_priced": cov.get("priced_executable"),
        "material_lines_unresolved": material_unresolved,
        "expected_profit": profit_display,
        "profit_confidence": econ.get("profit_confidence"),
        "unresolved_cost_exposure": econ.get("unresolved_cost_exposure"),
        "headline": headline_profit,
        "next_action": next_action,
        "economics_status": econ.get("economics_status"),
    }
