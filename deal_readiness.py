"""DEAL_READINESS and BID_READINESS — separate deterministic gates."""

from __future__ import annotations

from typing import Any

from bom_gate import BOM_COMPLETE, evaluate_bom_completeness
from company_profile import (
    KEY_MIN_ACTUAL_PROFIT,
    KEY_PERSONAL_CASH_ALLOWED,
    KEY_PERSONAL_CREDIT_ALLOWED,
    KEY_PG_ALLOWED,
    evaluate_requirement_against_capability,
    get_capability,
    normalize_profile,
)
from economic_integrity import (
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    ECON_CALCULATED,
    GATE_FAIL,
    GATE_PASS,
)
from quote_validation import QUOTE_EXPIRED, QUOTE_MISMATCH, QUOTE_VALID, validate_supplier_quote
from requirement_register import REQ_SATISFIED, unresolved_mandatory
from solicitation_package import (
    PACKAGE_COMPLETE,
    evaluate_solicitation_package,
)

DEAL_READY = "DEAL_READY"
DEAL_NOT_READY = "DEAL_NOT_READY"
DEAL_BLOCKED = "DEAL_BLOCKED"
DEAL_REJECTED = "DEAL_REJECTED"

BID_READY = "BID_READY"
BID_NOT_READY = "BID_NOT_READY"


def _cost_known(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    st = str(item.get("status") or COST_UNKNOWN).upper()
    if st in {COST_VERIFIED, COST_VERIFIED_ZERO, COST_NOT_APPLICABLE, "CALCULATED"}:
        return True
    if st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN}:
        return False
    return False


def evaluate_deal_readiness(
    *,
    bom: list[dict[str, Any]] | None = None,
    bom_gate: dict[str, Any] | None = None,
    quotes: list[dict[str, Any]] | None = None,
    required_quantity: float | int | None = None,
    channel_status: str | None = None,
    delivery_status: str | None = None,
    freight_cost: dict[str, Any] | None = None,
    installation_cost: dict[str, Any] | None = None,
    subcontract_cost: dict[str, Any] | None = None,
    compliance: dict[str, Any] | None = None,
    financing: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    company_profile: dict[str, Any] | None = None,
    eligibility_requirements: list[dict[str, Any]] | None = None,
    availability_verified: bool | None = None,
    fatal_blocker: str | None = None,
) -> dict[str, Any]:
    """
    DEAL_READY only when all applicable commercial/execution gates pass.
    UNKNOWN costs never silently pass. Installation UNKNOWN ≠ N/A.
    """
    blockers: list[str] = []
    profile = normalize_profile(company_profile)

    if fatal_blocker:
        return {
            "status": DEAL_REJECTED,
            "blockers": [fatal_blocker],
            "deal_ready": False,
        }

    bg = bom_gate or evaluate_bom_completeness(bom)
    if bg.get("status") != BOM_COMPLETE:
        blockers.append("BOM incomplete")
        for b in bg.get("blockers") or []:
            blockers.append(str(b))

    # Valid supplier quote
    usable_quote = None
    quote_statuses = []
    for q in quotes or []:
        v = validate_supplier_quote(
            q, required_bom=bom, required_quantity=required_quantity
        )
        quote_statuses.append(v)
        if v.get("status") == QUOTE_VALID and v.get("usable_as_acquisition_cost"):
            usable_quote = v
            break
        if v.get("status") == QUOTE_EXPIRED:
            blockers.append("expired quote")
        if v.get("status") == QUOTE_MISMATCH:
            blockers.append("quote/BOM mismatch")
    if usable_quote is None:
        blockers.append("exact supplier acquisition price missing")
        if not any("expired" in b or "mismatch" in b for b in blockers):
            blockers.append("missing valid quote")

    if availability_verified is not True:
        blockers.append("availability unverified")

    ch = str(channel_status or "UNKNOWN").upper()
    if ch not in {"CHANNEL_PASS", "PASS", "SATISFIED", "VERIFIED"}:
        blockers.append("channel authorization unresolved")

    deliv = str(delivery_status or "UNKNOWN").upper()
    if deliv not in {"DELIVERY_PASS", "PASS", "VERIFIED"}:
        blockers.append("delivery unresolved")

    if not _cost_known(freight_cost):
        blockers.append("freight unresolved")

    inst = installation_cost or {}
    inst_st = str(inst.get("status") or COST_UNKNOWN).upper()
    if inst_st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN}:
        blockers.append("installation unresolved")
        # Explicit: UNKNOWN cannot become N/A here
    elif inst_st == COST_NOT_APPLICABLE:
        pass  # only if already proven N/A upstream
    elif not _cost_known(inst):
        blockers.append("installation cost unknown")

    # Subcontract only if required / not N/A
    sub = subcontract_cost or {}
    sub_st = str(sub.get("status") or "").upper()
    if sub_st in {COST_REQUIRED_UNKNOWN, COST_UNKNOWN}:
        blockers.append("subcontract cost unresolved")

    comp = compliance or {}
    for gate_name in ("baa", "taa", "coo", "nmr", "channel"):
        val = comp.get(gate_name)
        if val is None:
            continue
        vs = str(val).upper()
        if vs in {"UNKNOWN", "UNRESOLVED", "FAIL"}:
            blockers.append(f"compliance unresolved:{gate_name}")
    if not comp:
        blockers.append("compliance unresolved")

    fin = financing or {}
    fin_st = str(fin.get("status") or fin.get("overall") or "UNRESOLVED").upper()
    if fin_st not in {"FINANCING_PASS", "PASS", GATE_PASS}:
        blockers.append("financing unresolved")
    if fin.get("pg_required") is True and not fin.get("operator_pg_approved"):
        blockers.append("OPERATOR_PG_REVIEW_REQUIRED")
    if fin.get("personal_credit_required") is True and fin.get("personal_credit_materially_disqualifies") is True:
        blockers.append("personal credit materially disqualifies")
    elif fin.get("personal_credit_required") is True:
        blockers.append("personal credit role/floor needs verification")
    if fin.get("cash_upfront_required") is True or fin.get("borrower_cash_required") is True:
        blockers.append("cash upfront required")

    # Company policy hard gates
    for key, label in (
        (KEY_PG_ALLOWED, "PG policy violated"),
        (KEY_PERSONAL_CREDIT_ALLOWED, "personal credit policy violated"),
        (KEY_PERSONAL_CASH_ALLOWED, "personal cash policy violated"),
    ):
        cap = get_capability(profile, key)
        if cap.get("status") == "POLICY" and cap.get("value") is False:
            # Policy forbids — financing must verify compliance separately; do not auto-pass
            pass

    # Eligibility requirements (certs, bonds, etc.)
    for req in eligibility_requirements or []:
        if not req.get("required"):
            continue
        ev = evaluate_requirement_against_capability(
            requirement_type=str(req.get("type") or ""),
            required=True,
            profile=profile,
            capability_key=req.get("capability_key"),
        )
        if ev.get("blocker"):
            reason = ev.get("reason") or "eligibility_blocker"
            if reason == "capability_not_held":
                blockers.append(f"capability not held:{req.get('type')}")
            elif reason == "capability_unknown_not_assumed_held":
                blockers.append(f"capability unknown:{req.get('type')}")
            else:
                blockers.append(f"eligibility:{reason}")

    econ = economics or {}
    profit = econ.get("actual_profit_result") or econ.get("actual_profit") or {}
    if isinstance(profit, dict):
        pstatus = str(profit.get("status") or "")
        pval = profit.get("actual_profit")
        min_profit = float(get_capability(profile, KEY_MIN_ACTUAL_PROFIT).get("value") or 10000)
        if pstatus != ECON_CALCULATED or pval is None:
            blockers.append("actual profit not calculated")
        else:
            try:
                if float(pval) < min_profit:
                    blockers.append("actual profit under $10k")
            except (TypeError, ValueError):
                blockers.append("actual profit not calculated")
    else:
        blockers.append("actual profit not calculated")

    # Deduplicate blockers preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq.append(b)

    if any(
        "OPERATOR_PG_REVIEW" in b or "personal credit materially" in b or "cash upfront" in b for b in uniq
    ):
        status = DEAL_BLOCKED
    elif uniq:
        status = DEAL_NOT_READY
    else:
        status = DEAL_READY

    return {
        "status": status,
        "blockers": uniq,
        "deal_ready": status == DEAL_READY,
        "quote_validations": quote_statuses,
        "bom_gate": bg,
    }


def evaluate_bid_readiness(
    *,
    deal_readiness: dict[str, Any] | None,
    solicitation_package: dict[str, Any] | None = None,
    documents: list[dict[str, Any]] | None = None,
    amendments_accounted: bool | None = None,
    amendments_expected: bool | None = None,
    requirements: list[dict[str, Any]] | None = None,
    bid_package: dict[str, Any] | None = None,
    forms_complete: bool | None = None,
    signatures_complete: bool | None = None,
    pricing_complete: bool | None = None,
    technical_complete: bool | None = None,
    past_performance_complete: bool | None = None,
    oem_letters_attached: bool | None = None,
    oem_letter_required: bool | None = None,
    mandatory_attachments_present: bool | None = None,
    submission_method_verified: bool | None = None,
    deadline_verified: bool | None = None,
) -> dict[str, Any]:
    """BID_READY only when DEAL_READY and full bid checklist pass — never from AI narrative."""
    blockers: list[str] = []
    deal = deal_readiness or {}
    if not deal.get("deal_ready"):
        blockers.append("deal not ready")
        for b in deal.get("blockers") or []:
            blockers.append(f"deal:{b}")

    pkg = solicitation_package or evaluate_solicitation_package(
        documents,
        amendments_expected=amendments_expected,
        amendments_accounted=amendments_accounted,
    )
    if pkg.get("status") != PACKAGE_COMPLETE:
        blockers.append("solicitation package completeness unresolved")
        for b in pkg.get("blockers") or []:
            blockers.append(f"package:{b}")

    if amendments_accounted is not True:
        if amendments_expected is True or amendments_accounted is None:
            blockers.append("unresolved amendment")

    bp = bid_package or {}
    checklist = bp.get("checklist") if isinstance(bp.get("checklist"), dict) else bp

    def _need(flag: bool | None, label: str) -> None:
        if flag is not True:
            blockers.append(label)

    _need(forms_complete if forms_complete is not None else checklist.get("forms_complete"), "required bid documents/checklist incomplete")
    if forms_complete is False or checklist.get("forms_complete") is False:
        blockers.append("required forms incomplete")
    _need(
        signatures_complete if signatures_complete is not None else checklist.get("signatures_complete"),
        "signatures incomplete",
    )
    _need(
        pricing_complete if pricing_complete is not None else checklist.get("pricing_complete"),
        "pricing incomplete",
    )
    if technical_complete is False or checklist.get("technical_complete") is False:
        blockers.append("technical response incomplete")
    if past_performance_complete is False:
        blockers.append("past performance response incomplete")
    if oem_letter_required and oem_letters_attached is not True:
        blockers.append("OEM/channel letters missing")
    if mandatory_attachments_present is not True:
        blockers.append("mandatory attachments incomplete")
    if submission_method_verified is not True:
        blockers.append("submission method unverified")
    if deadline_verified is not True:
        blockers.append("deadline unverified")

    for r in unresolved_mandatory(requirements):
        blockers.append(f"mandatory requirement unresolved:{r.get('requirement_key') or r.get('type')}")

    # Missing mandatory bid item from package checklist
    for item in (bp.get("mandatory_items") or []):
        if isinstance(item, dict) and item.get("present") is not True:
            blockers.append(f"missing mandatory bid item:{item.get('key') or item.get('name')}")

    seen: set[str] = set()
    uniq: list[str] = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq.append(b)

    status = BID_READY if not uniq else BID_NOT_READY
    return {
        "status": status,
        "blockers": uniq,
        "bid_ready": status == BID_READY,
        "solicitation_package": pkg,
        "deal_status": deal.get("status"),
    }
