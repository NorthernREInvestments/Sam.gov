"""Owner approval gate — tri-state safe. UNKNOWN never passes."""

from __future__ import annotations

from typing import Any

from execution_requirements.constants import (
    EXECUTION_CRITICAL_CATEGORIES,
    ST_BLOCKED,
    ST_CONFIRMED,
    ST_NOT_APPLICABLE,
    ST_UNKNOWN,
)
from execution_requirements.supplier_state import resolve_supplier_execution_state


def is_explicitly_true(value: Any) -> bool:
    """Positive confirmation only — True is the sole pass. UNKNOWN/None/False all fail required checks."""
    return value is True


def _unresolved(req: dict[str, Any]) -> bool:
    st = req.get("status")
    if st in {ST_CONFIRMED, ST_NOT_APPLICABLE}:
        return False
    if not req.get("mandatory") and not req.get("blocking"):
        return False
    return True


def section_status(requirements: list[dict[str, Any]], categories: set[str]) -> dict[str, Any]:
    subset = [r for r in requirements if r.get("category") in categories]
    if not subset:
        return {"status": "Missing", "code": "MISSING", "count": 0, "actions": [], "blocking": False}
    if any(r.get("status") == ST_BLOCKED for r in subset):
        actions = [r.get("plain_english_action") for r in subset if r.get("status") == ST_BLOCKED]
        return {"status": "Blocked", "code": "BLOCKED", "count": len(subset), "actions": [a for a in actions if a], "blocking": True}
    open_reqs = [r for r in subset if _unresolved(r)]
    if not open_reqs:
        return {"status": "Complete", "code": "COMPLETE", "count": len(subset), "actions": [], "blocking": False}
    actions = [r.get("plain_english_action") for r in open_reqs if r.get("plain_english_action")]
    return {
        "status": "Action Required",
        "code": "ACTION_REQUIRED",
        "count": len(subset),
        "open_count": len(open_reqs),
        "actions": actions[:8],
        "blocking": any(r.get("blocking") or r.get("mandatory") for r in open_reqs),
    }


def build_deep_dive_sections(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "Product": section_status(requirements, {"PRODUCT", "QUANTITY_UOM", "TECHNICAL_DATA"}),
        "Supplier": section_status(requirements, {"SUPPLIER_CONFIRMATION"}),
        "Pricing": {
            "status": "Deferred",
            "code": "DEFERRED_TO_ECONOMICS",
            "count": 0,
            "actions": ["Pricing remains owned by economics / supplier quote engines."],
            "blocking": False,
        },
        "Compliance": section_status(
            requirements, {"COUNTRY_OF_ORIGIN", "CERTIFICATION", "PACKAGING", "MARKING", "INSPECTION", "ACCEPTANCE"}
        ),
        "Financing": {
            "status": "Deferred",
            "code": "DEFERRED_TO_CASH_CYCLE",
            "count": 0,
            "actions": ["See cash_cycle / financing_compatible for owner-gate financing truth."],
            "blocking": False,
        },
        "Bid": section_status(requirements, {"SUBMISSION"}),
    }


def resolve_economics_complete(row: dict[str, Any]) -> bool | None:
    if row.get("economics_complete") is True:
        return True
    if row.get("economics_complete") is False or row.get("economics_valid") is False:
        return False
    profit = row.get("supported_profit") or row.get("Known_gross_spread") or row.get("expected_profit")
    cost = row.get("acquisition_cost") or row.get("Observed_acquisition") or row.get("supplier_unit_cost")
    revenue = row.get("government_revenue") or row.get("Government_benchmark") or row.get("supported_revenue")
    if profit in (None, "", "UNKNOWN") or cost in (None, "", "UNKNOWN"):
        return None
    try:
        if float(str(profit).replace(",", "").replace("$", "")) <= 0:
            return False
        float(str(cost).replace(",", "").replace("$", ""))
        if revenue not in (None, "", "UNKNOWN"):
            float(str(revenue).replace(",", "").replace("$", ""))
        return True
    except (TypeError, ValueError):
        return None


def resolve_financing_compatible(row: dict[str, Any]) -> bool | None:
    """
    Business constraints:
    - owner cash required before government payment must be 0
    - personal guarantee prohibited unless explicit override
    - UNKNOWN financing blocks (returns None → gate treats as not True)
    """
    if row.get("financing_compatible") is True:
        # Still enforce PG / cash hard blocks
        if row.get("personal_guarantee_required") is True and not row.get("pg_operator_approved"):
            return False
        cash = row.get("owner_cash_required_before_payment")
        if cash not in (None, "", "UNKNOWN", 0, "0"):
            try:
                if float(cash) > 0:
                    return False
            except (TypeError, ValueError):
                return None
        return True
    if row.get("financing_compatible") is False:
        return False
    if row.get("personal_guarantee_required") is True and not row.get("pg_operator_approved"):
        return False
    cash = row.get("owner_cash_required_before_payment")
    if cash not in (None, "", "UNKNOWN", 0, "0"):
        try:
            if float(cash) > 0:
                return False
        except (TypeError, ValueError):
            return None
    fs = str(row.get("funding_state") or "UNKNOWN").upper()
    if fs in ("", "UNKNOWN", "NONE", "NULL"):
        return None
    if "UNRESOLVED" in fs:
        return False
    if fs in {"VERIFIED_PRE_BID_PATH", "COMPATIBLE", "RESOLVED", "APPROVED", "FUNDED"}:
        return True
    return None


def resolve_submission_complete(
    submission_checklist: dict[str, Any] | None,
    row: dict[str, Any],
) -> bool | None:
    if row.get("submission_complete") is True:
        return True
    if row.get("submission_complete") is False:
        return False
    if not submission_checklist:
        return None
    if submission_checklist.get("blocks_owner_approval"):
        return False
    if submission_checklist.get("complete") is True:
        return True
    # No submission items extracted — UNKNOWN (do not treat as pass)
    if not (submission_checklist.get("items") or []):
        return None
    return False


def resolve_packaging_resolved(requirements: list[dict[str, Any]], row: dict[str, Any]) -> bool | None:
    if row.get("packaging_resolved") is True:
        return True
    if row.get("packaging_resolved") is False:
        return False
    packs = [r for r in requirements if r.get("category") == "PACKAGING" and (r.get("mandatory") or r.get("blocking"))]
    if not packs:
        return True  # not applicable
    if any(_unresolved(r) for r in packs):
        return False
    return True


def resolve_delivery_feasible(requirements: list[dict[str, Any]], row: dict[str, Any]) -> bool | None:
    if row.get("delivery_feasible") is True:
        return True
    if row.get("delivery_feasible") is False:
        return False
    dels = [r for r in requirements if r.get("category") in {"DELIVERY", "FOB", "SHIPPING"} and (r.get("mandatory") or r.get("blocking"))]
    if not dels:
        return None
    if any(r.get("status") == ST_BLOCKED for r in dels):
        return False
    if any(_unresolved(r) for r in dels):
        return False
    return True


def resolve_amendments_resolved(conflicts: list[dict[str, Any]], row: dict[str, Any], amendment: dict[str, Any] | None) -> bool | None:
    if row.get("amendments_resolved") is True:
        return True
    if row.get("amendments_resolved") is False or row.get("amendment_unresolved") is True:
        return False
    material = [c for c in (conflicts or []) if c.get("material")]
    if material:
        return False
    # Explicit operator denial of acknowledgment blocks; mere overlay application does not.
    if row.get("amendment_acknowledged") is False:
        return False
    return True


def resolve_product_and_qty(requirements: list[dict[str, Any]], row: dict[str, Any]) -> tuple[bool | None, bool | None]:
    if row.get("product_identity_confirmed") is True:
        prod: bool | None = True
    elif row.get("product_identity_confirmed") is False:
        prod = False
    elif row.get("owner_readiness_fixture_confirm_all") is True:
        prod = True
    else:
        prods = [r for r in requirements if r.get("category") == "PRODUCT"]
        if not prods:
            prod = None
        elif any(r.get("status") == ST_CONFIRMED for r in prods):
            prod = True
        elif any(r.get("captured_value") or r.get("raw_text") for r in prods):
            prod = False  # extracted but not confirmed
        else:
            prod = None

    if row.get("quantity_uom_confirmed") is True:
        qty: bool | None = True
    elif row.get("quantity_uom_confirmed") is False:
        qty = False
    elif row.get("owner_readiness_fixture_confirm_all") is True:
        qty = True
    else:
        qtys = [r for r in requirements if r.get("category") == "QUANTITY_UOM"]
        if not qtys:
            qty = None
        elif any(r.get("status") == ST_CONFIRMED for r in qtys):
            qty = True
        elif any(r.get("captured_value") is not None for r in qtys):
            qty = False
        else:
            qty = None
    return prod, qty


def evaluate_owner_approval_gate(
    *,
    requirements: list[dict[str, Any]],
    conflicts: list[dict[str, Any]] | None = None,
    submission_checklist: dict[str, Any] | None = None,
    supplier_checklist: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
    amendment: dict[str, Any] | None = None,
    # legacy kwargs kept for callers — interpreted with UNKNOWN-safe rules
    economics_valid: bool | None = None,
    financing_acceptable: bool | None = None,
    supplier_quote_present: bool | None = None,
) -> dict[str, Any]:
    """
    Sole authority for READY FOR OWNER APPROVAL.

    Required satisfaction fields pass ONLY when explicitly True.
    UNKNOWN / None / False → blocked.
    """
    row = row if isinstance(row, dict) else {}
    blockers: list[str] = []
    reasons: list[str] = []
    provenance: dict[str, Any] = {}

    supplier_res = resolve_supplier_execution_state(row)
    economics_complete = resolve_economics_complete(row)
    if economics_valid is False:
        economics_complete = False
    elif economics_valid is True and economics_complete is None:
        economics_complete = True

    financing_compatible = resolve_financing_compatible(row)
    if financing_acceptable is False:
        financing_compatible = False
    elif financing_acceptable is True and financing_compatible is None:
        financing_compatible = True

    supplier_validated = supplier_res.get("supplier_validated")
    quote_executable = supplier_res.get("quote_executable")
    if supplier_quote_present is False:
        quote_executable = False

    submission_complete = resolve_submission_complete(submission_checklist, row)
    packaging_resolved = resolve_packaging_resolved(requirements, row)
    delivery_feasible = resolve_delivery_feasible(requirements, row)
    amendments_resolved = resolve_amendments_resolved(conflicts or [], row, amendment)
    product_ok, qty_ok = resolve_product_and_qty(requirements, row)

    # Fixture confirm-all (positive golden cases only)
    if row.get("owner_readiness_fixture_confirm_all") is True:
        product_ok = True if product_ok is not False else product_ok
        qty_ok = True if qty_ok is not False else qty_ok
        # still require explicit supplier/econ/finance flags on row for positive cases

    required = {
        "economics_complete": economics_complete,
        "supplier_validated": supplier_validated,
        "quote_executable": quote_executable,
        "financing_compatible": financing_compatible,
        "submission_complete": submission_complete,
        "packaging_resolved": packaging_resolved,
        "delivery_feasible": delivery_feasible,
        "amendments_resolved": amendments_resolved,
        "product_identity_confirmed": product_ok,
        "quantity_uom_confirmed": qty_ok,
    }

    labels = {
        "economics_complete": "ECONOMICS_INCOMPLETE_OR_UNKNOWN",
        "supplier_validated": "SUPPLIER_NOT_VALIDATED",
        "quote_executable": "QUOTE_NOT_EXECUTABLE",
        "financing_compatible": "FINANCING_INCOMPATIBLE_OR_UNKNOWN",
        "submission_complete": "SUBMISSION_INCOMPLETE_OR_UNKNOWN",
        "packaging_resolved": "PACKAGING_UNRESOLVED",
        "delivery_feasible": "DELIVERY_INFEASIBLE_OR_UNKNOWN",
        "amendments_resolved": "AMENDMENT_UNRESOLVED",
        "product_identity_confirmed": "PRODUCT_IDENTITY_UNCONFIRMED",
        "quantity_uom_confirmed": "QUANTITY_UOM_UNCONFIRMED",
    }
    reason_text = {
        "economics_complete": "Economics must be explicitly complete (UNKNOWN blocks).",
        "supplier_validated": "Supplier path must be validated (public price is not enough).",
        "quote_executable": "Executable supplier quote / acquisition cost required.",
        "financing_compatible": "Financing must be explicitly compatible (UNKNOWN/PG/cash blocks).",
        "submission_complete": "Submission checklist must be complete.",
        "packaging_resolved": "Mandatory packaging must be resolved.",
        "delivery_feasible": "Delivery/FOB must be feasible and resolved.",
        "amendments_resolved": "Material amendment conflicts must be resolved.",
        "product_identity_confirmed": "Exact product identity must be confirmed.",
        "quantity_uom_confirmed": "Quantity/UOM must be confirmed.",
    }

    for key, val in required.items():
        if not is_explicitly_true(val):
            blockers.append(labels[key])
            reasons.append(reason_text[key] + f" (value={val!r})")
        else:
            provenance[key] = {"satisfied": True, "source": "row_or_resolution"}

    # Material conflicts always block
    material_conflicts = [c for c in (conflicts or []) if c.get("material")]
    if material_conflicts:
        if "AMENDMENT_UNRESOLVED" not in blockers and "REQUIREMENT_CONFLICT" not in blockers:
            blockers.append("REQUIREMENT_CONFLICT")
            reasons.append(material_conflicts[0].get("message") or "Material requirement conflict")

    # Unresolved mandatory execution-critical requirements (when not fixture-confirmed)
    if row.get("owner_readiness_fixture_confirm_all") is not True:
        for r in requirements:
            if r.get("category") not in EXECUTION_CRITICAL_CATEGORIES:
                continue
            if not (r.get("mandatory") or r.get("blocking")):
                continue
            if r.get("category") == "SUPPLIER_CONFIRMATION" and is_explicitly_true(supplier_validated) and is_explicitly_true(quote_executable):
                continue
            if r.get("category") == "PACKAGING" and is_explicitly_true(packaging_resolved):
                continue
            if r.get("category") in {"DELIVERY", "FOB", "SHIPPING"} and is_explicitly_true(delivery_feasible):
                continue
            if r.get("category") == "SUBMISSION" and is_explicitly_true(submission_complete):
                continue
            if _unresolved(r):
                rid = str(r.get("requirement_id"))
                if rid not in blockers:
                    blockers.append(rid)
                    reasons.append(str(r.get("plain_english_action") or r.get("normalized_requirement")))

    if supplier_checklist and supplier_checklist.get("blocks_owner_approval") and not (
        is_explicitly_true(supplier_validated) and is_explicitly_true(quote_executable)
    ):
        if "SUPPLIER_NOT_VALIDATED" not in blockers:
            blockers.append("SUPPLIER_CONFIRMATION_INCOMPLETE")
            reasons.append("Mandatory supplier confirmations remain unresolved")

    ready = len(blockers) == 0
    return {
        "kind": "OwnerApprovalGate",
        "authority": "execution_requirements.readiness.evaluate_owner_approval_gate",
        "ready_for_owner_approval": ready,
        "blockers": blockers,
        "reasons": reasons[:24],
        "va_next_actions": [r for r in reasons if r][:12],
        "satisfaction": {k: (v is True) for k, v in required.items()},
        "satisfaction_raw": required,
        "supplier_execution_state": supplier_res,
        "provenance": provenance,
        "note": "UNKNOWN/None/False never pass required satisfaction fields. Sole UI authority for READY FOR OWNER APPROVAL.",
    }


def list_execution_critical_blockers(requirements: list[dict[str, Any]], conflicts: list[dict[str, Any]] | None = None) -> list[str]:
    gate = evaluate_owner_approval_gate(requirements=requirements, conflicts=conflicts or [], row={})
    if gate["ready_for_owner_approval"]:
        return []
    return list(gate["blockers"])
