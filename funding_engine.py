"""Pre-bid funding viability + award funding confirmation — deterministic, no external calls.

Funding Path Intelligence (path types, source matching, call sheets, hybrid structures)
lives in funding_path_intelligence.py / funding_source_kb.py and extends this layer.
"""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

# Bridge: path-type catalog for callers that already import funding_engine
try:
    from funding_path_constants import FUNDING_PATH_TYPES as FUNDING_PATH_TYPE_CATALOG
except Exception:  # pragma: no cover
    FUNDING_PATH_TYPE_CATALOG = ()


# Pre-bid viability statuses
FUNDING_NOT_RESEARCHED = "NOT_RESEARCHED"
FUNDING_RESEARCHING = "RESEARCHING"
FUNDING_NEEDS_CONTACT = "NEEDS_CONTACT"
FUNDING_WAITING_ON_PROVIDER = "WAITING_ON_PROVIDER"
FUNDING_VIABLE_CONDITIONAL = "VIABLE_CONDITIONAL_ON_AWARD"
FUNDING_VIABLE = "VIABLE"
FUNDING_BLOCKED = "BLOCKED"
FUNDING_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

PHASE_PRE_BID = "PRE_BID_FUNDING_VIABILITY"
PHASE_AWARD = "AWARD_FUNDING_CONFIRMATION"

CASH_CYCLE_STEPS = (
    "supplier_payment_requirement",
    "production_order",
    "freight",
    "delivery",
    "government_acceptance",
    "invoice",
    "government_payment",
    "financier_repayment",
    "company_proceeds",
)

FUNDING_STRATEGY_CATALOG = (
    {
        "id": "supplier_government_po",
        "label": "Supplier accepts government PO",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "supplier_waits_gov_payment",
        "label": "Supplier waits for government payment",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "supplier_net_terms",
        "label": "Supplier net terms (if available without prohibited personal requirements)",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "manufacturer_public_sector_terms",
        "label": "Manufacturer/distributor public-sector terms",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "supplier_direct_fulfillment",
        "label": "Supplier direct fulfillment",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "po_financing",
        "label": "PO financing",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "contract_financing",
        "label": "Contract financing",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "financier_pays_supplier",
        "label": "Financier pays supplier directly",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "receivables_factoring",
        "label": "Receivables/factoring (where appropriate)",
        "status": "POSSIBLE_STRATEGY",
    },
    {
        "id": "milestone_payments",
        "label": "Progress/milestone payments (if solicitation allows)",
        "status": "POSSIBLE_STRATEGY",
    },
)


def _utc() -> str:
    return now_utc().isoformat()


def build_cash_cycle_model(*, known: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every capital touchpoint — UNKNOWN until verified."""
    known = known or {}
    steps = []
    for step in CASH_CYCLE_STEPS:
        entry = known.get(step) if isinstance(known.get(step), dict) else {}
        steps.append(
            {
                "step": step,
                "capital_required": entry.get("capital_required"),
                "capital_required_status": entry.get("status") or "UNKNOWN",
                "timing": entry.get("timing"),
                "notes": entry.get("notes"),
            }
        )
    return {
        "steps": steps,
        "rule": "Identify every point where capital may be required",
        "LIVE_API_REQUESTS": 0,
    }


def rank_funding_strategies(
    *,
    strategies: list[dict[str, Any]] | None = None,
    provider_evidence: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rank candidate structures — suggestions until evidence supports."""
    base = strategies or [dict(s) for s in FUNDING_STRATEGY_CATALOG]
    ranked = []
    for s in base:
        score = 50
        if s.get("pg_required") is False:
            score -= 15
        if s.get("personal_credit_required") is False:
            score -= 15
        if s.get("personal_cash_required") in (0, 0.0, False, "0"):
            score -= 10
        if s.get("verified_eligible") is True:
            score -= 25
        if s.get("blocked") is True:
            score += 100
        ranked.append(
            {
                **s,
                "rank_score": score,
                "is_fact": bool(s.get("verified_eligible")),
                "status": s.get("status") or "POSSIBLE_STRATEGY",
            }
        )
    ranked.sort(key=lambda x: (x.get("rank_score", 999), x.get("label") or ""))
    return ranked


def evaluate_pre_bid_funding_viability(
    *,
    pursuits: list[dict[str, Any]] | None = None,
    terms: list[dict[str, Any]] | None = None,
    supplier_upfront_required: bool | None = None,
    acquisition_total: float | None = None,
    hard_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    PRE_BID_FUNDING_VIABILITY — never confuse with award confirmation.
    Hard fail only on VERIFIED HIGH-confidence prohibited requirements.
    """
    hc = hard_constraints or {
        "personal_guarantee": False,
        "personal_credit": False,
        "personal_cash_upfront": 0,
    }
    blockers: list[str] = []
    unknowns: list[str] = []
    verified_pass = False

    for p in pursuits or []:
        if p.get("pg_required") is True:
            unknowns.append("PG required — OPERATOR_PG_REVIEW (not automatic block)")
        elif p.get("pg_required") is None:
            unknowns.append("PG requirement unknown")
        if p.get("personal_credit_required") is True and p.get("personal_credit_materially_disqualifies") is True:
            blockers.append("VERIFIED: personal credit materially disqualifies operator")
        elif p.get("personal_credit_required") is True:
            unknowns.append("personal credit checked — verify FICO role/floor vs operator")
        elif p.get("personal_credit_required") is None:
            unknowns.append("personal credit unknown")
        cash = p.get("borrower_cash_required")
        if cash is True:
            blockers.append("VERIFIED: borrower cash contribution required")
        elif cash is None:
            unknowns.append("cash contribution unknown")
        if (
            p.get("approval_status") == "FINANCING_PASS"
            and p.get("verification_status") == "VERIFIED"
            and not blockers
        ):
            verified_pass = True

    if supplier_upfront_required is True and not verified_pass:
        unknowns.append("supplier upfront payment with no verified financing path")

    if blockers:
        status = FUNDING_BLOCKED
    elif verified_pass:
        status = FUNDING_VIABLE
    elif unknowns:
        status = FUNDING_INSUFFICIENT_EVIDENCE if not pursuits else FUNDING_NEEDS_CONTACT
    else:
        status = FUNDING_NOT_RESEARCHED

    return {
        "phase": PHASE_PRE_BID,
        "question": (
            "If we win, is there a credible documented path to execute the cash cycle "
            "without personal cash, personal credit, or PG?"
        ),
        "status": status,
        "blockers": blockers,
        "unknowns": unknowns,
        "verified_viable": verified_pass,
        "acquisition_total_known": acquisition_total is not None,
        "hard_constraints": hc,
        "award_confirmation_separate": True,
        "LIVE_API_REQUESTS": 0,
    }


def evaluate_award_funding_confirmation(
    *,
    government_po_verified: bool = False,
    provider_approval_verified: bool = False,
    terms_verified: bool = False,
) -> dict[str, Any]:
    """AWARD_FUNDING_CONFIRMATION — only after award/PO exists."""
    if government_po_verified and provider_approval_verified and terms_verified:
        return {
            "phase": PHASE_AWARD,
            "status": "CONFIRMED",
            "LIVE_API_REQUESTS": 0,
        }
    missing = []
    if not government_po_verified:
        missing.append("government PO/award not verified")
    if not provider_approval_verified:
        missing.append("provider transaction approval not verified")
    if not terms_verified:
        missing.append("final terms not verified")
    return {
        "phase": PHASE_AWARD,
        "status": "PENDING",
        "missing": missing,
        "LIVE_API_REQUESTS": 0,
    }


def funding_economics_scenarios(
    *,
    base_amount: float | None,
    fee_pct: float | None = None,
    fixed_fee: float | None = None,
    verified_timing_days: int | None = None,
) -> dict[str, Any]:
    """
    30/60/90-day labeled scenarios unless timing/cost sufficiently verified.
    Never silently assume 30-day government payment.
    """
    if base_amount is None:
        return {
            "status": "INCOMPLETE",
            "scenarios": {},
            "notes": "Acquisition/financing base unknown — cannot calculate",
            "LIVE_API_REQUESTS": 0,
        }

    def _cost(days: int) -> float | None:
        total = float(base_amount)
        if fixed_fee is not None:
            total += float(fixed_fee)
        if fee_pct is not None:
            # Simple time-scaled scenario — labeled, not verified unless timing verified
            mult = days / 30.0
            total += float(base_amount) * float(fee_pct) * mult
        return round(total, 2)

    if verified_timing_days is not None and fee_pct is not None:
        return {
            "status": "CALCULATED",
            "verified_timing_days": verified_timing_days,
            "financing_cost": _cost(verified_timing_days),
            "scenarios": {},
            "LIVE_API_REQUESTS": 0,
        }

    return {
        "status": "SCENARIO",
        "notes": "Payment timing not verified — scenarios only, not actual profit inputs",
        "scenarios": {
            "30_day_financing_cost": _cost(30),
            "60_day_financing_cost": _cost(60),
            "90_day_financing_cost": _cost(90),
        },
        "LIVE_API_REQUESTS": 0,
    }


def build_funding_plan_snapshot(
    *,
    contract_id: int,
    pursuits: list[dict[str, Any]] | None = None,
    terms: list[dict[str, Any]] | None = None,
    economics: dict[str, Any] | None = None,
    quotes: list[dict[str, Any]] | None = None,
    supplier_upfront: bool | None = None,
    persisted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble full funding plan view for workspace / OS screens."""
    acq = None
    for q in quotes or []:
        v = (q.get("validation") or {}).get("status")
        if v == "QUOTE_VALID" and q.get("total") is not None:
            acq = q.get("total")
            break
        if v == "QUOTE_VALID" and q.get("extended_price") is not None:
            acq = q.get("extended_price")

    pre_bid = evaluate_pre_bid_funding_viability(
        pursuits=pursuits,
        terms=terms,
        supplier_upfront_required=supplier_upfront,
        acquisition_total=acq,
    )
    strategies = rank_funding_strategies(
        strategies=(persisted or {}).get("strategies"),
    )
    fee = None
    fee_pct = None
    for t in terms or []:
        if t.get("fee") is not None:
            try:
                fee = float(t["fee"])
            except (TypeError, ValueError):
                pass
        if t.get("rate") is not None:
            try:
                fee_pct = float(t["rate"]) / 100.0
            except (TypeError, ValueError):
                pass

    econ_scenarios = funding_economics_scenarios(
        base_amount=acq,
        fee_pct=fee_pct,
        fixed_fee=fee,
    )

    return {
        "contract_id": contract_id,
        "pre_bid_viability": pre_bid,
        "pre_bid_status": pre_bid["status"],
        "award_confirmation": evaluate_award_funding_confirmation(),
        "cash_cycle": build_cash_cycle_model(known=(persisted or {}).get("cash_cycle")),
        "strategies": strategies,
        "economics_scenarios": econ_scenarios,
        "what_we_know": (persisted or {}).get("known_facts") or [],
        "what_we_dont_know": pre_bid.get("unknowns") or [],
        "must_know_before_bid": [
            "No PG / no personal credit / $0 personal cash path documented",
            "Supplier payment timing compatible with execution",
            "Financing cost usable for actual profit if required",
        ],
        "may_wait_until_award": [
            "Final provider approval on specific government PO",
            "Assignment documentation if applicable",
        ],
        "updated_at": _utc(),
        "LIVE_API_REQUESTS": 0,
    }
