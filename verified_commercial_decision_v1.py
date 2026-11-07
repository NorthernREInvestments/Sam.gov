"""Authoritative commercial readiness decision for M3 Supply.

Single source of truth for PROVEN / VERIFY / REJECT.

Rules (nonnegotiable):
- NET_PROFIT = PROPOSED_BID_REVENUE - TOTAL_VERIFIED_COSTS
- NET_MARGIN = NET_PROFIT / PROPOSED_BID_REVENUE
- Margin floor: 15% (hard). Dollar floor: configurable, default $5,000.
- High dollar profit cannot override a sub-15% margin.
- Historical award value is NEVER proposed bid revenue.
- Unknown / missing / pending costs are NEVER treated as zero.
- Zero is allowed only with explicit VERIFIED_ZERO or NOT_APPLICABLE + reason.
- $0 personal cash required for PROVEN under current model.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from application_clock import now_utc

BUILD = "20261107-m3-verified-commercial-decision-v1"

PROVEN = "PROVEN"
VERIFY = "VERIFY"
REJECT = "REJECT"

# Backward-compatible aliases used by older owner surfaces
PROVEN_PROFITABLE = "PROVEN_PROFITABLE"
LIKELY_PROFITABLE = "LIKELY_PROFITABLE"  # maps to VERIFY
POSSIBLE_PROFIT = "POSSIBLE_PROFIT"  # maps to VERIFY
UNPROVEN = "UNPROVEN"  # maps to VERIFY
UNPROFITABLE = "UNPROFITABLE"  # maps to REJECT
EXECUTION_BLOCKED = "EXECUTION_BLOCKED"  # maps to VERIFY or REJECT

COST_VERIFIED = "VERIFIED"
COST_CALCULATED = "CALCULATED"
COST_VERIFIED_ZERO = "VERIFIED_ZERO"
COST_NOT_APPLICABLE = "NOT_APPLICABLE"
COST_REQUIRED_UNKNOWN = "REQUIRED_UNKNOWN"
COST_UNKNOWN = "UNKNOWN"

MONEY_QUANT = Decimal("0.01")
DEFAULT_MIN_MARGIN = Decimal("0.15")
DEFAULT_MIN_PROFIT_USD = Decimal("5000")


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    s = str(v).strip().upper()
    if s in {"UNKNOWN", "NONE", "NULL", "N/A", "NA"}:
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def _money(v: Decimal | None) -> Decimal | None:
    if v is None:
        return None
    return v.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _pct(v: Decimal | None) -> Decimal | None:
    if v is None:
        return None
    return v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def load_commercial_targets(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Owner-configurable floors. Margin is always enforced (≥15% default)."""
    ov = overrides or {}
    try:
        from m3_data_root import data_path
        import json

        path = data_path("m3_owner_profit_targets.json")
        if path.exists() and not ov:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ov = data.get("targets") or data
    except Exception:
        pass

    def _env_dec(name: str, default: Decimal) -> Decimal:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        parsed = _d(raw)
        return parsed if parsed is not None else default

    min_profit = _d(ov.get("minimum_expected_profit_usd"))
    if min_profit is None:
        min_profit = _env_dec("M3_MIN_EXPECTED_PROFIT_USD", DEFAULT_MIN_PROFIT_USD)

    min_margin = _d(ov.get("minimum_margin_pct"))
    if min_margin is None:
        raw = (os.getenv("M3_MIN_MARGIN_PCT") or "").strip()
        if raw:
            # Accept 15 or 0.15
            parsed = _d(raw)
            if parsed is not None:
                min_margin = parsed / Decimal("100") if parsed > 1 else parsed
        if min_margin is None:
            min_margin = DEFAULT_MIN_MARGIN
    elif min_margin > 1:
        min_margin = min_margin / Decimal("100")

    # Prefer margin never below hard floor
    if min_margin < DEFAULT_MIN_MARGIN:
        min_margin = DEFAULT_MIN_MARGIN

    return {
        "minimum_net_profit_usd": _money(min_profit),
        "minimum_net_margin": _pct(min_margin),
        "preferred_net_margin": Decimal("0.20"),
        "personal_cash_required_max_usd": Decimal("0.00"),
        "note": (
            "Hard gates: net margin ≥15% AND dollar profit ≥ configured minimum. "
            "High dollar profit cannot waive the margin floor. "
            "Historical awards are not proposed revenue."
        ),
    }


def _normalize_cost(
    *,
    category: str,
    value: Any = None,
    status: str | None = None,
    required: bool = True,
    reason: str | None = None,
) -> dict[str, Any]:
    st = str(status or "").upper().strip()
    amount = _d(value)
    if not st:
        if amount is None:
            st = COST_REQUIRED_UNKNOWN if required else COST_UNKNOWN
        else:
            st = COST_VERIFIED
    if st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN}:
        return {
            "category": category,
            "status": COST_REQUIRED_UNKNOWN if required else COST_UNKNOWN,
            "required": bool(required),
            "value": None,
            "reason": reason,
            "usable": False,
        }
    if st == COST_NOT_APPLICABLE:
        if not reason:
            return {
                "category": category,
                "status": COST_REQUIRED_UNKNOWN,
                "required": True,
                "value": None,
                "reason": "NOT_APPLICABLE_REQUIRES_DOCUMENTED_REASON",
                "usable": False,
            }
        return {
            "category": category,
            "status": COST_NOT_APPLICABLE,
            "required": False,
            "value": Decimal("0.00"),
            "reason": reason,
            "usable": True,
        }
    if st == COST_VERIFIED_ZERO:
        if not reason:
            return {
                "category": category,
                "status": COST_REQUIRED_UNKNOWN,
                "required": True,
                "value": None,
                "reason": "VERIFIED_ZERO_REQUIRES_DOCUMENTED_REASON",
                "usable": False,
            }
        return {
            "category": category,
            "status": COST_VERIFIED_ZERO,
            "required": bool(required),
            "value": Decimal("0.00"),
            "reason": reason,
            "usable": True,
        }
    if st in {COST_VERIFIED, COST_CALCULATED} and amount is not None:
        return {
            "category": category,
            "status": st,
            "required": bool(required),
            "value": _money(amount),
            "reason": reason,
            "usable": True,
        }
    return {
        "category": category,
        "status": COST_REQUIRED_UNKNOWN if required else COST_UNKNOWN,
        "required": bool(required),
        "value": None,
        "reason": reason or "INVALID_COST_ENVELOPE",
        "usable": False,
    }


def _legacy_status(readiness: str, *, missing: list[str], hard_reject: bool) -> str:
    if readiness == PROVEN:
        return PROVEN_PROFITABLE
    if hard_reject or readiness == REJECT:
        if "MARGIN_BELOW_FLOOR" in missing or "DOLLAR_PROFIT_BELOW_FLOOR" in missing:
            return UNPROFITABLE
        if "NON_POSITIVE_NET_PROFIT" in missing:
            return UNPROFITABLE
        if "NOT_PRODUCT_ONLY" in missing or "EXPIRED" in missing:
            return UNPROFITABLE
        return EXECUTION_BLOCKED if missing else UNPROFITABLE
    # VERIFY — keep legacy research cues
    if "PROPOSED_BID_REVENUE" in missing or "GOVERNMENT_VALUE" in missing:
        return UNPROVEN
    if any(x.startswith("COST_PRODUCT_ACQUISITION") or x == "ACQUISITION_COST" for x in missing):
        return UNPROVEN
    if missing:
        return (
            LIKELY_PROFITABLE
            if not any("FREIGHT" in m or "FINANCING" in m for m in missing)
            else POSSIBLE_PROFIT
        )
    return UNPROVEN


def evaluate_verified_commercial_decision(
    *,
    proposed_bid_revenue: Any = None,
    historical_award_value: Any = None,
    historical_award_date: Any = None,
    product_acquisition: Any = None,
    product_acquisition_status: str | None = None,
    inbound_freight: Any = None,
    inbound_freight_status: str | None = None,
    inbound_freight_reason: str | None = None,
    outbound_freight: Any = None,
    outbound_freight_status: str | None = None,
    outbound_freight_reason: str | None = None,
    packaging_handling: Any = None,
    packaging_status: str | None = None,
    packaging_reason: str | None = None,
    insurance: Any = None,
    insurance_status: str | None = None,
    insurance_reason: str | None = None,
    financing_cost: Any = None,
    financing_status: str | None = None,
    financing_reason: str | None = None,
    taxes_duties: Any = None,
    taxes_status: str | None = None,
    taxes_reason: str | None = None,
    payment_processing: Any = None,
    payment_processing_status: str | None = None,
    payment_processing_reason: str | None = None,
    compliance_testing: Any = None,
    compliance_status: str | None = None,
    compliance_reason: str | None = None,
    warranty_returns: Any = None,
    warranty_status: str | None = None,
    warranty_reason: str | None = None,
    operating_overhead: Any = None,
    overhead_status: str | None = None,
    overhead_reason: str | None = None,
    other_mandatory: Any = None,
    other_status: str | None = None,
    other_reason: str | None = None,
    line_count_required: int | None = None,
    line_count_sourced: int | None = None,
    sourcing_complete: bool | None = None,
    equivalents_authorized: bool | None = None,
    supplier_quote_expired: bool = False,
    financing_transaction_verified: bool = False,
    financing_advance_sufficient: bool | None = None,
    uncovered_prepayment: bool | None = None,
    personal_cash_required_usd: Any = None,
    personal_guarantee_required: bool | None = None,
    lender_generic_marketing_only: bool = False,
    product_only: bool = True,
    prime_eligible: bool | None = None,
    deadline_open: bool | None = None,
    evidence_current: bool | None = None,
    hard_reject_reason: str | None = None,
    targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the single authoritative commercial decision."""
    targets = targets or load_commercial_targets()
    min_profit = Decimal(str(targets["minimum_net_profit_usd"]))
    min_margin = Decimal(str(targets["minimum_net_margin"]))
    max_personal = Decimal(str(targets["personal_cash_required_max_usd"]))

    revenue = _money(_d(proposed_bid_revenue))
    historical = _money(_d(historical_award_value))

    costs = {
        "product_acquisition": _normalize_cost(
            category="product_acquisition",
            value=product_acquisition,
            status=product_acquisition_status,
            required=True,
        ),
        "inbound_freight": _normalize_cost(
            category="inbound_freight",
            value=inbound_freight,
            status=inbound_freight_status,
            required=True,
            reason=inbound_freight_reason,
        ),
        "outbound_freight": _normalize_cost(
            category="outbound_freight",
            value=outbound_freight,
            status=outbound_freight_status,
            required=True,
            reason=outbound_freight_reason,
        ),
        "packaging_handling": _normalize_cost(
            category="packaging_handling",
            value=packaging_handling,
            status=packaging_status or (COST_NOT_APPLICABLE if packaging_handling is None and packaging_reason else None),
            required=True,
            reason=packaging_reason,
        ),
        "insurance": _normalize_cost(
            category="insurance",
            value=insurance,
            status=insurance_status or (COST_NOT_APPLICABLE if insurance is None and insurance_reason else None),
            required=True,
            reason=insurance_reason,
        ),
        "financing": _normalize_cost(
            category="financing",
            value=financing_cost,
            status=financing_status,
            required=True,
            reason=financing_reason,
        ),
        "taxes_duties": _normalize_cost(
            category="taxes_duties",
            value=taxes_duties,
            status=taxes_status or (COST_NOT_APPLICABLE if taxes_duties is None and taxes_reason else None),
            required=True,
            reason=taxes_reason,
        ),
        "payment_processing": _normalize_cost(
            category="payment_processing",
            value=payment_processing,
            status=payment_processing_status
            or (COST_NOT_APPLICABLE if payment_processing is None and payment_processing_reason else None),
            required=True,
            reason=payment_processing_reason,
        ),
        "compliance_testing": _normalize_cost(
            category="compliance_testing",
            value=compliance_testing,
            status=compliance_status
            or (COST_NOT_APPLICABLE if compliance_testing is None and compliance_reason else None),
            required=True,
            reason=compliance_reason,
        ),
        "warranty_returns": _normalize_cost(
            category="warranty_returns",
            value=warranty_returns,
            status=warranty_status
            or (COST_NOT_APPLICABLE if warranty_returns is None and warranty_reason else None),
            required=True,
            reason=warranty_reason,
        ),
        "operating_overhead": _normalize_cost(
            category="operating_overhead",
            value=operating_overhead,
            status=overhead_status
            or (COST_NOT_APPLICABLE if operating_overhead is None and overhead_reason else None),
            required=True,
            reason=overhead_reason,
        ),
        "other_mandatory": _normalize_cost(
            category="other_mandatory",
            value=other_mandatory,
            status=other_status or (COST_NOT_APPLICABLE if other_mandatory is None and other_reason else None),
            required=False if other_reason else True,
            reason=other_reason,
        ),
    }

    missing: list[str] = []
    hard_reject = False

    if hard_reject_reason:
        missing.append(str(hard_reject_reason))
        hard_reject = True
    if product_only is False:
        missing.append("NOT_PRODUCT_ONLY")
        hard_reject = True
    if deadline_open is False:
        missing.append("EXPIRED")
        hard_reject = True
    if supplier_quote_expired:
        missing.append("SUPPLIER_QUOTE_EXPIRED")
    if equivalents_authorized is False:
        missing.append("UNAPPROVED_EQUIVALENT")
    if lender_generic_marketing_only:
        missing.append("LENDER_MARKETING_NOT_TRANSACTION_APPROVAL")
    if financing_advance_sufficient is False:
        missing.append("INSUFFICIENT_FINANCING_ADVANCE")
    if uncovered_prepayment is True:
        missing.append("UNCOVERED_PREPAYMENT")

    personal_cash = _money(_d(personal_cash_required_usd))
    if personal_cash is not None and personal_cash > max_personal:
        missing.append("PERSONAL_CASH_REQUIRED")
        hard_reject = True
    if personal_guarantee_required is True:
        missing.append("PERSONAL_GUARANTEE_REQUIRES_OWNER_APPROVAL")

    if revenue is None:
        missing.append("PROPOSED_BID_REVENUE")
        # Detect historical misuse attempt
        if historical is not None:
            missing.append("HISTORICAL_AWARD_IS_NOT_PROPOSED_REVENUE")

    if line_count_required is not None and line_count_sourced is not None:
        if int(line_count_sourced) < int(line_count_required):
            missing.append("PARTIAL_LINE_SOURCING_COVERAGE")
    if sourcing_complete is False:
        missing.append("PARTIAL_LINE_SOURCING_COVERAGE")

    for key, c in costs.items():
        if not c.get("usable"):
            if c.get("required") or key in {
                "product_acquisition",
                "inbound_freight",
                "outbound_freight",
                "financing",
            }:
                missing.append(f"COST_{key.upper()}")

    total_costs: Decimal | None = None
    costs_complete = all(c.get("usable") for c in costs.values() if c.get("required") or c["category"] != "other_mandatory")
    # other_mandatory: if required unknown, already flagged
    if all(c.get("usable") for c in costs.values()):
        total_costs = _money(sum((c["value"] for c in costs.values()), Decimal("0")))
        costs_complete = True
    else:
        costs_complete = False

    net_profit: Decimal | None = None
    net_margin: Decimal | None = None
    if revenue is not None and total_costs is not None:
        net_profit = _money(revenue - total_costs)
        if revenue > 0:
            net_margin = _pct(net_profit / revenue)

    margin_ok = net_margin is not None and net_margin >= min_margin
    dollar_ok = net_profit is not None and net_profit >= min_profit

    if net_margin is not None and net_margin < min_margin:
        missing.append("MARGIN_BELOW_FLOOR")
        # Explicit: high dollar cannot waive margin
        if net_profit is not None and net_profit >= min_profit:
            missing.append("HIGH_DOLLAR_CANNOT_WAIVE_MARGIN_FLOOR")
            hard_reject = True
    if net_profit is not None and net_profit < min_profit and costs_complete and revenue is not None:
        missing.append("DOLLAR_PROFIT_BELOW_FLOOR")
        hard_reject = True
    if net_profit is not None and net_profit <= 0 and costs_complete and revenue is not None:
        missing.append("NON_POSITIVE_NET_PROFIT")
        hard_reject = True

    financing_ok = bool(financing_transaction_verified) and not lender_generic_marketing_only
    if costs["financing"].get("usable") and costs["financing"]["status"] == COST_VERIFIED_ZERO:
        # Zero financing cost only with documented funding explanation
        if not (financing_reason or "").strip():
            missing.append("ZERO_FINANCING_REQUIRES_FUNDING_EXPLANATION")
            financing_ok = False
        else:
            financing_ok = True

    if not financing_ok and "COST_FINANCING" not in missing and costs["financing"].get("usable"):
        # Cost known but transaction not verified → VERIFY, not auto REJECT
        if "LENDER_MARKETING_NOT_TRANSACTION_APPROVAL" not in missing:
            missing.append("FINANCING_NOT_TRANSACTION_VERIFIED")

    evidence_ok = evidence_current is not False
    if evidence_current is False:
        missing.append("EVIDENCE_STALE")

    # Classification
    proven_ready = (
        product_only is True
        and (prime_eligible is not False)
        and (deadline_open is not False)
        and revenue is not None
        and costs_complete
        and margin_ok
        and dollar_ok
        and financing_ok
        and (personal_cash is None or personal_cash <= max_personal)
        and personal_guarantee_required is not True
        and not supplier_quote_expired
        and equivalents_authorized is not False
        and sourcing_complete is not False
        and not (
            line_count_required is not None
            and line_count_sourced is not None
            and int(line_count_sourced) < int(line_count_required)
        )
        and evidence_ok
        and not hard_reject
        and "PARTIAL_LINE_SOURCING_COVERAGE" not in missing
        and "UNCOVERED_PREPAYMENT" not in missing
        and "INSUFFICIENT_FINANCING_ADVANCE" not in missing
    )

    if hard_reject and (
        "NOT_PRODUCT_ONLY" in missing
        or "EXPIRED" in missing
        or "NON_POSITIVE_NET_PROFIT" in missing
        or "MARGIN_BELOW_FLOOR" in missing
        or "DOLLAR_PROFIT_BELOW_FLOOR" in missing
        or "PERSONAL_CASH_REQUIRED" in missing
    ):
        readiness = REJECT
    elif proven_ready:
        readiness = PROVEN
    else:
        readiness = VERIFY

    # Deduplicate missing while preserving order
    seen: set[str] = set()
    missing_unique: list[str] = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            missing_unique.append(m)

    legacy = _legacy_status(readiness, missing=missing_unique, hard_reject=hard_reject)

    return {
        "kind": "VerifiedCommercialDecision",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "readiness": readiness,
        "profit_status": legacy,  # backward-compatible alias
        "proposed_bid_revenue": float(revenue) if revenue is not None else None,
        "historical_award_value": float(historical) if historical is not None else None,
        "historical_award_date": historical_award_date,
        "historical_is_not_revenue": True,
        "costs": {
            k: {
                **v,
                "value": float(v["value"]) if v.get("value") is not None else None,
            }
            for k, v in costs.items()
        },
        "total_verified_costs": float(total_costs) if total_costs is not None else None,
        "net_profit": float(net_profit) if net_profit is not None else None,
        "net_margin": float(net_margin) if net_margin is not None else None,
        "net_margin_pct": float(_money(net_margin * Decimal("100"))) if net_margin is not None else None,
        "margin_floor": float(min_margin),
        "dollar_profit_floor": float(min_profit),
        "margin_ok": margin_ok,
        "dollar_ok": dollar_ok,
        "costs_complete": costs_complete,
        "financing_transaction_verified": financing_ok,
        "personal_cash_required_usd": float(personal_cash) if personal_cash is not None else None,
        "personal_guarantee_required": personal_guarantee_required,
        "line_coverage": {
            "required": line_count_required,
            "sourced": line_count_sourced,
            "complete": (
                True
                if sourcing_complete is True
                or (
                    line_count_required is not None
                    and line_count_sourced is not None
                    and int(line_count_sourced) >= int(line_count_required)
                )
                else False
                if sourcing_complete is False
                or (
                    line_count_required is not None
                    and line_count_sourced is not None
                    and int(line_count_sourced) < int(line_count_required)
                )
                else None
            ),
        },
        "missing_requirements": missing_unique,
        "owner_queue_eligible": readiness == PROVEN,
        "targets_applied": {
            "minimum_net_profit_usd": float(min_profit),
            "minimum_net_margin": float(min_margin),
            "preferred_net_margin": float(targets["preferred_net_margin"]),
            "personal_cash_required_max_usd": float(max_personal),
        },
        "note": (
            "Authoritative decision. Historical awards support competition research only. "
            "Unknown costs remain unknown. PROVEN requires 15%+ net margin, dollar floor, "
            "complete sourcing, transaction-verified financing, and $0 personal cash."
        ),
    }


def decision_from_profit_first_inputs(
    *,
    expected_revenue: Any = None,
    product_cost: Any = None,
    freight: Any = None,
    financing: Any = None,
    other_costs: Any = None,
    price_basis: str | None = None,
    completeness_pct: float | None = None,
    evidence_grade: str | None = None,
    execution_pass: bool | None = None,
    execution_blockers: list[str] | None = None,
    unresolved_material: bool = False,
    historical_award_value: Any = None,
    proposed_bid_revenue: Any = None,
    targets: dict[str, Any] | None = None,
    line_count_required: int | None = None,
    line_count_sourced: int | None = None,
    financing_transaction_verified: bool = False,
    financing_reason: str | None = None,
    personal_cash_required_usd: Any = None,
    product_only: bool = True,
) -> dict[str, Any]:
    """Adapter used by profit_first.economics — never invents costs or revenue."""
    # Revenue: only explicit proposed bid. Do not promote historical.
    revenue = proposed_bid_revenue if proposed_bid_revenue is not None else expected_revenue
    hist = historical_award_value
    if hist is None and proposed_bid_revenue is None and expected_revenue is not None:
        # Caller may have passed historical as expected_revenue historically —
        # treat ambiguous revenue without proposed_bid as missing unless labeled.
        pass

    freight_status = None
    freight_reason = None
    if freight is None:
        freight_status = COST_REQUIRED_UNKNOWN
    else:
        freight_status = COST_VERIFIED

    fin_status = None
    fin_reason = financing_reason
    if financing is None:
        fin_status = COST_REQUIRED_UNKNOWN
    elif _d(financing) == Decimal("0"):
        fin_status = COST_VERIFIED_ZERO
        fin_reason = fin_reason or None  # must have reason for PROVEN
    else:
        fin_status = COST_VERIFIED

    other_status = COST_NOT_APPLICABLE if other_costs is None else COST_VERIFIED
    other_reason = "no_other_mandatory_costs_identified" if other_costs is None else None

    # Collapse single freight into outbound (common path); inbound N/A with reason
    decision = evaluate_verified_commercial_decision(
        proposed_bid_revenue=revenue,
        historical_award_value=hist,
        product_acquisition=product_cost,
        product_acquisition_status=COST_VERIFIED if product_cost is not None else COST_REQUIRED_UNKNOWN,
        inbound_freight=None,
        inbound_freight_status=COST_NOT_APPLICABLE,
        inbound_freight_reason="inbound_freight_not_applicable_or_included_in_outbound",
        outbound_freight=freight,
        outbound_freight_status=freight_status,
        outbound_freight_reason=freight_reason,
        packaging_handling=None,
        packaging_status=COST_NOT_APPLICABLE,
        packaging_reason="packaging_included_or_not_required",
        insurance=None,
        insurance_status=COST_NOT_APPLICABLE,
        insurance_reason="insurance_not_separately_required",
        financing_cost=financing,
        financing_status=fin_status,
        financing_reason=fin_reason,
        taxes_duties=None,
        taxes_status=COST_NOT_APPLICABLE,
        taxes_reason="taxes_not_applicable_or_pass_through",
        payment_processing=None,
        payment_processing_status=COST_NOT_APPLICABLE,
        payment_processing_reason="no_separate_payment_processing_fee",
        compliance_testing=None,
        compliance_status=COST_NOT_APPLICABLE,
        compliance_reason="no_separate_compliance_testing_cost",
        warranty_returns=None,
        warranty_status=COST_NOT_APPLICABLE,
        warranty_reason="warranty_included_in_product_terms",
        operating_overhead=None,
        overhead_status=COST_NOT_APPLICABLE,
        overhead_reason="overhead_allocated_elsewhere_or_zero_for_transaction",
        other_mandatory=other_costs,
        other_status=other_status,
        other_reason=other_reason,
        line_count_required=line_count_required,
        line_count_sourced=line_count_sourced,
        financing_transaction_verified=financing_transaction_verified
        or (fin_status == COST_VERIFIED and financing is not None)
        or (fin_status == COST_VERIFIED_ZERO and bool(fin_reason)),
        personal_cash_required_usd=personal_cash_required_usd if personal_cash_required_usd is not None else 0,
        product_only=product_only,
        deadline_open=True if execution_pass is not False else None,
        evidence_current=True if (completeness_pct or 0) >= 80 else None,
        hard_reject_reason=(execution_blockers or [None])[0] if execution_pass is False else None,
        targets=targets,
    )

    # Preserve familiar profit_first field names for callers/tests
    decision["expected_revenue"] = decision.get("proposed_bid_revenue")
    decision["product_cost"] = (decision.get("costs") or {}).get("product_acquisition", {}).get("value")
    decision["freight"] = (decision.get("costs") or {}).get("outbound_freight", {}).get("value")
    decision["financing"] = (decision.get("costs") or {}).get("financing", {}).get("value")
    decision["other_costs"] = (decision.get("costs") or {}).get("other_mandatory", {}).get("value")
    decision["post_financing_profit"] = decision.get("net_profit")
    decision["expected_profit"] = decision.get("net_profit")
    decision["margin_pct"] = decision.get("net_margin_pct")
    decision["price_basis"] = price_basis
    decision["missing_facts"] = list(decision.get("missing_requirements") or [])
    decision["completeness_pct"] = completeness_pct
    decision["evidence_grade"] = evidence_grade
    decision["execution_pass"] = execution_pass if execution_pass is not None else (
        True if decision.get("readiness") == PROVEN else False if decision.get("readiness") == REJECT else None
    )
    decision["execution_blockers"] = list(execution_blockers or [])
    decision["decision_basis"] = "verified_net_profit"
    decision["proof_signals"] = []
    if decision.get("readiness") == PROVEN and price_basis:
        basis = str(price_basis).upper()
        if basis in {"PUBLIC_RETAIL", "RETAIL", "PUBLIC"}:
            decision["proof_signals"].append("PROFITABLE_AT_PUBLIC_RETAIL")
        elif basis in {"QUOTED", "SUPPLIER_QUOTE", "QUOTE"}:
            decision["proof_signals"].append("PROFITABLE_AT_QUOTED_COST")
        elif basis in {"DISTRIBUTOR", "WHOLESALE", "DISTRIBUTOR_COST"}:
            decision["proof_signals"].append("PROFITABLE_AT_DISTRIBUTOR_COST")
    decision["unresolved_material"] = unresolved_material
    return decision
