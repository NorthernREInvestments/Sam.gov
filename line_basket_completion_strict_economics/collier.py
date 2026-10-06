"""Collier County hard validation — independent profit bridge audit."""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from line_basket_completion_strict_economics.models import (
    BUILD,
    COLLIER_OID,
    COLLIER_VALIDATION,
    ECONOMICS_NOT_READY,
    PRIOR_REV,
    PROFIT_UNPROVEN,
)
from line_basket_completion_strict_economics.provenance import classify_price_record
from m3_data_root import data_path

_BOND_THRESHOLD = re.compile(
    r"(?:of\s+\$?[\d,]+(?:\.\d+)?\s+may\s+require|bond(?:ing)?\s+(?:threshold|requirement)|"
    r"certified\s+or\s+qualified|performance\s+bond)",
    re.I,
)


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _money(v: Any) -> float:
    try:
        return float(Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def audit_collier_revenue() -> dict[str, Any]:
    rev = _load(PRIOR_REV)
    row = (rev.get("by_opportunity") or {}).get(COLLIER_OID) or {}
    revenue_block = row.get("revenue") or {}
    best = revenue_block.get("best") or {}
    if not best and isinstance(row.get("full_evidence"), list) and row["full_evidence"]:
        best = row["full_evidence"][0]

    value = best.get("reference_value") or best.get("value")
    snippet = str(best.get("snippet") or "")
    source = best.get("source")
    evidence_type = best.get("revenue_evidence_type") or best.get("type")

    # Detect bonding-threshold misread
    is_bond_threshold = bool(_BOND_THRESHOLD.search(snippet)) or (
        "250,000" in snippet and "certified or qualified" in snippet.lower()
    )
    if is_bond_threshold:
        return {
            "government_expected_revenue": None,
            "reported_prior_value": value,
            "source": source,
            "evidence_type": "BONDING_THRESHOLD_LANGUAGE_NOT_CONTRACT_VALUE",
            "value_class": "NOT_REVENUE",
            "label": "Bonding/qualification threshold — not guaranteed contract revenue",
            "snippet": snippet[:240],
            "usable_as_revenue": False,
            "note": "Prior $250,000 came from bid-instructions bonding language, not an explicit package budget.",
        }

    value_class = "estimate"
    if evidence_type == "CURRENT_VALUE_EXPLICIT":
        value_class = "current_explicit_budget"
    elif "CEILING" in str(evidence_type or "").upper():
        value_class = "ceiling"
    elif "COMPARABLE" in str(evidence_type or "").upper() or "PRIOR" in str(evidence_type or "").upper():
        value_class = "prior_comparable_only"

    return {
        "government_expected_revenue": value if best else None,
        "source": source,
        "evidence_type": evidence_type,
        "value_class": value_class,
        "label": value_class,
        "snippet": snippet[:240],
        "usable_as_revenue": bool(value) and value_class in {"current_explicit_budget", "current_bid_qty_x_reference"},
        "exact_or_estimated": best.get("exact_or_estimated"),
    }


def validate_collier(result: dict[str, Any]) -> dict[str, Any]:
    """Independent line-by-line + waterfall validation for Collier."""
    lines = (result.get("lines") or {}).get("lines") or result.get("line_rows") or []
    econ = result.get("economics") or {}
    coverage = result.get("coverage") or {}
    rev_audit = audit_collier_revenue()

    line_reports = []
    valid_prices = 0
    invalid_removed = 0
    for ln in lines:
        audit = classify_price_record(
            {
                "unit_cost": ln.get("unit_cost"),
                "source_url": ln.get("source_url"),
                "seller": ln.get("seller"),
                "price_origin": ln.get("price_origin"),
                "research_route": ln.get("research_route"),
            }
        ) if ln.get("unit_cost") is not None else {"verdict": "NO_PRICE", "is_valid_production": False}
        if ln.get("terminal_state") == "PRICED_EXECUTABLE" and not audit.get("is_valid_production"):
            invalid_removed += 1
        if audit.get("is_valid_production"):
            valid_prices += 1
        line_reports.append(
            {
                "clin": ln.get("clin"),
                "description": (ln.get("description") or "")[:160],
                "qty": ln.get("quantity"),
                "uom": ln.get("uom"),
                "identity": ln.get("mpn") or ln.get("manufacturer"),
                "seller": ln.get("seller"),
                "unit_acquisition_cost": ln.get("unit_cost") if audit.get("is_valid_production") else None,
                "extended_acquisition_cost": ln.get("extended_line_cost") if audit.get("is_valid_production") else None,
                "government_revenue_value": ln.get("government_line_value"),
                "freight_treatment": "not_allocated_per_line",
                "price_provenance": ln.get("price_origin") if audit.get("is_valid_production") else audit.get("verdict"),
                "terminal_state": ln.get("terminal_state"),
                "provenance_verdict": audit.get("verdict"),
            }
        )

    # Revenue: only if usable
    gov_rev = rev_audit.get("government_expected_revenue") if rev_audit.get("usable_as_revenue") else None
    acq = float(econ.get("product_acquisition_cost") or 0)
    freight = float(econ.get("freight") or 0) if acq > 0 else 0.0
    financing = float(econ.get("financing_cost") or 0) if acq > 0 else 0.0
    other = float(econ.get("other_fees") or 0)
    total_cost = acq + freight + financing + other
    unresolved = float(econ.get("unresolved_cost_exposure") or 0)

    if gov_rev is None:
        expected_profit = None
        margin = None
        profit_confidence = PROFIT_UNPROVEN
        pass_fail = "FAIL"
        high_value = False
        fail_reasons = [
            "revenue_not_contract_value",
            "bonding_threshold_misread" if not rev_audit.get("usable_as_revenue") else "no_revenue",
        ]
    elif econ.get("economics_status") == ECONOMICS_NOT_READY:
        expected_profit = None
        margin = None
        profit_confidence = PROFIT_UNPROVEN
        pass_fail = "FAIL"
        high_value = False
        fail_reasons = ["economics_not_ready", "insufficient_material_basket"]
    else:
        expected_profit = _money(Decimal(str(gov_rev)) - Decimal(str(total_cost)))
        margin = round(expected_profit / float(gov_rev) * 100, 1) if gov_rev else None
        profit_confidence = econ.get("profit_confidence") or PROFIT_UNPROVEN
        # ~$250K claim: only if profit still ~250k after audit
        high_value = bool(expected_profit and abs(expected_profit - 250000) < 25000 and profit_confidence in {"PROFIT_PROVEN", "PROFIT_LIKELY"})
        pass_fail = "PASS" if high_value and valid_prices > 0 and invalid_removed == 0 else "FAIL"
        fail_reasons = []
        if not high_value:
            fail_reasons.append("profit_not_approx_250k_after_audit")
        if invalid_removed:
            fail_reasons.append("sentinel_or_invalid_prices")
        if valid_prices == 0:
            fail_reasons.append("no_valid_production_prices")

    owner_cash = float((econ.get("financing_detail") or {}).get("owner_cash_required") or econ.get("owner_cash_required") or 0)
    financing_status = econ.get("financing_status")
    if owner_cash > 0:
        financing_status = "FINANCING_BLOCKED"

    lender_ready = bool(
        pass_fail == "PASS"
        and owner_cash == 0
        and profit_confidence in {"PROFIT_PROVEN", "PROFIT_LIKELY"}
        and expected_profit
        and expected_profit >= 5000
    )

    waterfall = {
        "Government Revenue": gov_rev,
        "minus Acquisition": acq,
        "minus Freight": freight,
        "minus Financing": financing,
        "minus Other": other,
        "Expected Profit": expected_profit,
    }

    payload = {
        "build": BUILD,
        "opportunity": COLLIER_OID,
        "lines": len(lines),
        "material_coverage": coverage.get("MATERIAL_VALUE_COVERAGE") or econ.get("material_coverage"),
        "valid_production_prices": valid_prices,
        "invalid_sentinel_prices_removed": invalid_removed,
        "government_revenue": gov_rev,
        "revenue_evidence": rev_audit,
        "acquisition_cost": acq,
        "freight": freight,
        "financing": financing,
        "other_cost": other,
        "total_cost": total_cost,
        "unresolved_exposure": unresolved,
        "expected_profit": expected_profit,
        "margin": margin,
        "profit_confidence": profit_confidence,
        "delivery_risk": (result.get("execution") or {}).get("delivery_risk") or "UNVERIFIED_INSTALL_HEAVY",
        "financing_status": financing_status,
        "owner_cash_required": owner_cash,
        "lender_ready": lender_ready,
        "PASS_FAIL": pass_fail,
        "fail_reasons": fail_reasons,
        "HIGH_VALUE_VALIDATED_CANDIDATE": high_value,
        "waterfall": waterfall,
        "line_reports": line_reports,
        "prior_claim": {
            "coverage": "~97.4%",
            "basket_ready": True,
            "likely_profitable": True,
            "expected_profit": 250000,
            "disposition": "REJECTED_AS_NON_RECONCILABLE",
        },
    }
    _save(COLLIER_VALIDATION, payload)
    return payload
