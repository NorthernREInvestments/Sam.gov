"""BUILD 31 — Cash / Capital Requirement Gate.

Answers: based on what we actually know, how much capital may be required
before government payment, what causes that requirement, and what remains unknown?

NOT a lender approval system, financing recommendation engine, or approval predictor.
Evidence-based execution gate only.

Reuses transaction_economics, supplier_commercial_terms, funding_requirement,
and financing path intelligence. Does NOT invent terms, treat UNKNOWN as zero,
assume personal cash/credit/guarantee, auto-contact lenders/suppliers, or spend AI.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_capital_requirement_gate")

BUILD_TAG = "20260919-m3-capital-requirement-gate-1"

# Visibility / gate states (align with Pursuit Readiness + funding evidence)
ST_KNOWN = "KNOWN"
ST_PARTIAL = "PARTIAL"
ST_UNKNOWN = "UNKNOWN"
ST_CONDITIONAL = "CONDITIONAL"
ST_VERIFIED = "VERIFIED"
ST_STALE = "STALE"
ST_PROPOSED_AI = "PROPOSED_AI_UNCONFIRMED"

# Business constraints — never assumed true
ASSUMPTIONS_FORBIDDEN = (
    "personal_cash",
    "personal_credit",
    "personal_guarantee",
    "business_credit",
    "supplier_terms",
    "financing_availability",
)


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _unknown_or(v: Any) -> Any:
    if v is None or v == "":
        return "UNKNOWN"
    if isinstance(v, str) and v.strip().upper() in {"UNKNOWN", "N/A", "NA", "NONE"}:
        # "NONE" as deposit text is handled separately; bare NONE → UNKNOWN for generic fields
        return "UNKNOWN"
    return v


def _parse_number(v: Any) -> Any:
    """Return float if parseable, else UNKNOWN. Blank never becomes 0."""
    if v is None or v == "":
        return "UNKNOWN"
    if isinstance(v, dict):
        return _parse_number(v.get("value") if "value" in v else v.get("amount"))
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        s = str(v).strip().replace(",", "").replace("$", "")
        if not s or s.upper() in {"UNKNOWN", "N/A", "NA"}:
            return "UNKNOWN"
        return float(s)
    except (TypeError, ValueError):
        return "UNKNOWN"


def _fact_value(v: Any) -> Any:
    """Unwrap fact() envelopes and nested dicts to a scalar-or-UNKNOWN."""
    if isinstance(v, dict):
        if "value" in v:
            return _unknown_or(v.get("value"))
        return "UNKNOWN"
    return _unknown_or(v)


def _parse_deposit(raw: Any, *, acquisition: Any) -> dict[str, Any]:
    """Parse deposit requirement. Never invent; explicit none/0 is evidenced zero."""
    if raw is None or raw == "" or (isinstance(raw, str) and raw.strip().upper() in {"UNKNOWN", "N/A", "NA"}):
        return {"raw": "UNKNOWN", "amount": "UNKNOWN", "state": ST_UNKNOWN, "note": "Deposit requirement unknown"}

    if isinstance(raw, dict):
        raw = raw.get("value") if "value" in raw else raw.get("amount") or raw

    s = str(raw).strip()
    su = s.upper()

    # Explicit none / zero — evidenced, not assumed
    if su in {"NONE", "NO", "N/A DEPOSIT", "NO DEPOSIT", "0", "0%", "$0"}:
        return {
            "raw": s,
            "amount": 0.0,
            "state": ST_KNOWN,
            "note": "Supplier evidence states no deposit",
        }

    # Percentage of acquisition
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if m:
        pct = float(m.group(1)) / 100.0
        if _known(acquisition) and isinstance(acquisition, (int, float)):
            amt = round(float(acquisition) * pct, 2)
            return {
                "raw": s,
                "amount": amt,
                "percent": pct * 100.0,
                "state": ST_KNOWN,
                "note": f"{pct * 100:.0f}% of known acquisition",
            }
        return {
            "raw": s,
            "amount": "UNKNOWN",
            "percent": pct * 100.0,
            "state": ST_PARTIAL,
            "note": "Deposit percent known; acquisition unknown — cannot compute amount",
        }

    # Absolute amount
    num = _parse_number(s)
    if num != "UNKNOWN":
        return {"raw": s, "amount": num, "state": ST_KNOWN, "note": "Deposit amount from evidence"}

    return {"raw": s, "amount": "UNKNOWN", "state": ST_PARTIAL, "note": "Deposit text recorded; amount not parseable"}


def _preserve_deposit_raw(v: Any) -> Any:
    """Keep explicit none/zero deposit text — do not collapse to UNKNOWN."""
    if v is None or v == "":
        return "UNKNOWN"
    if isinstance(v, dict):
        return _preserve_deposit_raw(v.get("value") if "value" in v else v.get("amount"))
    s = str(v).strip()
    if not s:
        return "UNKNOWN"
    if s.upper() in {"UNKNOWN", "N/A", "NA"}:
        return "UNKNOWN"
    return s


def _extract_supplier_terms(row: dict[str, Any]) -> dict[str, Any]:
    """Pull payment terms / deposit / lead from row commercial terms (first evidenced supplier)."""
    terms_blob = _as_dict(row.get("supplier_commercial_terms"))
    by_sup = _as_dict(terms_blob.get("by_supplier"))
    econ = _as_dict(row.get("transaction_economics") or row.get("economics"))

    payment = _unknown_or(econ.get("payment_terms"))
    deposit_raw = _preserve_deposit_raw(econ.get("deposit_requirement"))
    lead = "UNKNOWN"
    supplier_name = "UNKNOWN"
    payment_timing = "UNKNOWN"

    for name, entry in by_sup.items():
        e = _as_dict(entry)
        t = _as_dict(e.get("terms"))
        q = _as_dict(e.get("quote"))
        lt = e.get("lead_time")
        supplier_name = str(name)
        if not _known(payment):
            payment = _unknown_or(t.get("net_terms") or e.get("payment_terms"))
        if deposit_raw == "UNKNOWN":
            deposit_raw = _preserve_deposit_raw(t.get("deposit_requirement") or e.get("deposit"))
        payment_timing = _unknown_or(t.get("payment_timing") or payment_timing)
        if isinstance(lt, dict):
            lead = _unknown_or(lt.get("stated") or lt.get("value"))
        elif _known(lt):
            lead = lt
        # Prefer first supplier with any commercial signal
        if _known(payment) or deposit_raw != "UNKNOWN" or _known(q.get("price")) or _known(q.get("total")):
            break

    # Try structured SCO read for richer unwrap
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms

        built = build_supplier_commercial_terms(row)
        for s in built.get("suppliers") or []:
            if not isinstance(s, dict):
                continue
            t = _as_dict(s.get("terms"))
            if not _known(payment):
                payment = _fact_value(t.get("net_terms"))
            if deposit_raw == "UNKNOWN":
                deposit_raw = _preserve_deposit_raw(_fact_value(t.get("deposit_requirement")))
            if not _known(payment_timing):
                payment_timing = _fact_value(t.get("payment_timing"))
            if supplier_name == "UNKNOWN":
                supplier_name = str(s.get("supplier_name") or "UNKNOWN")
            if _known(payment) or deposit_raw != "UNKNOWN":
                break
    except Exception:
        pass

    return {
        "supplier": supplier_name,
        "payment_terms": payment,
        "deposit_raw": deposit_raw,
        "payment_timing": payment_timing,
        "lead_time": lead,
    }


def _funding_path_visibility(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Documented funding path status — never claims 'financing available' without evidence."""
    deal = deal or {}
    funding_row = _as_dict(row.get("funding_requirement") or row.get("funding"))
    deal_funding = _as_dict(deal.get("funding"))
    fin_stored = _as_dict(row.get("financing_path_intelligence") or row.get("financing_profile"))
    cash = _as_dict(row.get("cash_survival"))

    status_raw = (
        funding_row.get("status")
        or funding_row.get("funding_status")
        or deal_funding.get("status")
        or row.get("funding_status")
        or "UNKNOWN"
    )
    status_u = str(status_raw).upper().strip() if _known(status_raw) else ST_UNKNOWN

    # Map to gate vocabulary
    if status_u in {"VERIFIED", "FUNDED", "SECURED", "FUNDING_SECURED", "APPROVED"}:
        path_state = ST_VERIFIED if status_u == "VERIFIED" else ST_CONDITIONAL
        # Even "FUNDED" on row is only accepted as CONDITIONAL unless evidence is explicit
        if status_u in {"FUNDED", "SECURED", "FUNDING_SECURED"} and not funding_row.get("evidence"):
            path_state = ST_CONDITIONAL
        elif status_u in {"FUNDED", "SECURED", "FUNDING_SECURED"}:
            path_state = ST_VERIFIED
    elif status_u in {"CONDITIONAL", "CONDITIONALLY_APPROVED", "NEEDS_VERIFICATION", "PRELIMINARY"}:
        path_state = ST_CONDITIONAL
    elif status_u in {"STALE"}:
        path_state = ST_STALE
    elif status_u in {"PROPOSED_AI_UNCONFIRMED", "PROPOSED", "AI_PROPOSED"}:
        path_state = ST_PROPOSED_AI
    elif status_u in {"UNKNOWN", "", "UNASSESSED", "NO_KNOWN_PATH", "THEORETICAL_PATH_ONLY"}:
        path_state = ST_UNKNOWN
    else:
        path_state = ST_PARTIAL if _known(status_raw) else ST_UNKNOWN

    paths: list[dict[str, Any]] = []
    try:
        from m3_supplier_capital_ops_read import build_financing_path_intelligence

        fin = build_financing_path_intelligence(row)
        for p in fin.get("paths") or []:
            if not isinstance(p, dict):
                continue
            st = str(p.get("status") or ST_UNKNOWN).upper()
            if st != ST_UNKNOWN:
                paths.append(
                    {
                        "path": p.get("path"),
                        "status": st,
                        "conditions": p.get("requirements") or p.get("unknowns") or ["UNKNOWN"],
                        "evidence": p.get("evidence"),
                        "next_action": p.get("next_action"),
                    }
                )
        # If any path is evidenced beyond UNKNOWN, surface CONDITIONAL at minimum
        if paths and path_state == ST_UNKNOWN:
            path_state = ST_CONDITIONAL
    except Exception:
        fin = {"is_financeable_claim": False, "paths": []}

    stored_paths = fin_stored.get("paths") or cash.get("financing_paths") or []
    for p in stored_paths:
        if isinstance(p, dict) and _known(p.get("path") or p.get("name")):
            st = str(p.get("status") or ST_UNKNOWN).upper()
            if st != ST_UNKNOWN and not any(x.get("path") == (p.get("path") or p.get("name")) for x in paths):
                paths.append(
                    {
                        "path": p.get("path") or p.get("name"),
                        "status": st,
                        "conditions": p.get("unknowns") or p.get("requirements") or ["UNKNOWN"],
                        "evidence": p.get("evidence"),
                    }
                )
                if path_state == ST_UNKNOWN:
                    path_state = ST_CONDITIONAL if st != ST_VERIFIED else ST_VERIFIED

    assignment = (
        funding_row.get("assignment_status")
        or deal_funding.get("assignment_status")
        or "UNKNOWN"
    )
    conditions = []
    if paths:
        for p in paths:
            for c in p.get("conditions") or []:
                if _known(c) and c not in conditions:
                    conditions.append(c)
    unknowns = []
    if path_state in {ST_UNKNOWN, ST_CONDITIONAL, ST_PARTIAL, ST_STALE, ST_PROPOSED_AI}:
        unknowns.append("Funding source not verified as available")
    if not paths:
        unknowns.append("No documented funding path on this opportunity")

    # Mandatory: path exists ≠ funded
    funded_claim = False
    source_state = ST_UNKNOWN
    if path_state == ST_VERIFIED and funding_row.get("evidence"):
        source_state = ST_VERIFIED
    elif path_state in {ST_CONDITIONAL, ST_PARTIAL, ST_STALE}:
        source_state = ST_CONDITIONAL
    elif path_state == ST_PROPOSED_AI:
        source_state = ST_PROPOSED_AI

    return {
        "path_state": path_state if path_state != ST_PARTIAL else ST_CONDITIONAL,
        "source_state": source_state,
        "funded_claim": funded_claim,
        "financing_available_claim": False,  # never emit unsupported claim
        "paths_documented": paths,
        "assignment_status": _unknown_or(assignment),
        "conditions": conditions or ["UNKNOWN"],
        "unknowns": unknowns,
        "status_raw": status_raw if _known(status_raw) else "UNKNOWN",
        "note": "Documented path ≠ financing available. Capital source separate from capital requirement.",
    }


def build_capital_requirement_assessment(
    row: dict[str, Any] | None,
    *,
    deal: dict[str, Any] | None = None,
    ensure_actions: bool = False,
    persist_actions: bool = False,
) -> dict[str, Any]:
    """Deterministic Cash / Capital Requirement Gate for one opportunity."""
    row = row if isinstance(row, dict) else {}
    deal = deal if isinstance(deal, dict) else {}
    oid = row.get("canonical_id") or deal.get("canonical_id") or "UNKNOWN"

    econ = _as_dict(
        deal.get("economics")
        or row.get("transaction_economics")
        or row.get("economics")
    )
    funding_deal = _as_dict(deal.get("funding"))

    acquisition = _parse_number(
        econ.get("acquisition")
        or econ.get("supplier_quote_total")
        or econ.get("acquisition_evidence")
        or row.get("acquisition_cost")
    )
    unit = _parse_number(econ.get("acquisition_unit") or econ.get("unit_price"))
    qty = _parse_number(econ.get("quantity"))
    freight = _parse_number(econ.get("freight") or row.get("freight_cost"))
    other = _parse_number(econ.get("other_costs"))
    currency = _unknown_or(econ.get("currency"))

    # If total unknown but unit*qty known, compute acquisition (commercial validation pattern)
    if acquisition == "UNKNOWN" and unit != "UNKNOWN" and qty != "UNKNOWN":
        try:
            acquisition = float(unit) * float(qty)
        except (TypeError, ValueError):
            acquisition = "UNKNOWN"

    terms = _extract_supplier_terms(row)
    deposit = _parse_deposit(terms["deposit_raw"], acquisition=acquisition)
    # Also check econ deposit
    if deposit["state"] == ST_UNKNOWN and _known(econ.get("deposit_requirement")):
        deposit = _parse_deposit(econ.get("deposit_requirement"), acquisition=acquisition)

    payment_terms = terms["payment_terms"]
    if not _known(payment_terms):
        payment_terms = _unknown_or(econ.get("payment_terms"))
    payment_timing = terms["payment_timing"]

    # Customer / gov payment timing — never invent Net-30 etc.
    cash_blob = _as_dict(row.get("cash_survival") or row.get("financing_profile"))
    gov_pay = _unknown_or(
        cash_blob.get("payment_timing")
        or cash_blob.get("invoice_timing")
        or funding_deal.get("expected_government_payment_days")
        or _as_dict(row.get("funding_requirement")).get("expected_government_payment_days")
    )
    delivery = _unknown_or(
        terms["lead_time"]
        or cash_blob.get("expected_delivery_days")
        or econ.get("lead_time")
    )

    # --- Known additional pre-delivery costs ---
    known_additional: list[dict[str, Any]] = []
    unknown_costs: list[str] = []
    additional_sum = 0.0
    additional_any = False

    if freight != "UNKNOWN":
        known_additional.append({"label": "Freight / shipping", "value": freight})
        additional_sum += float(freight)
        additional_any = True
    else:
        unknown_costs.append("Freight / shipping")

    if other != "UNKNOWN":
        known_additional.append({"label": "Other pre-delivery costs", "value": other})
        additional_sum += float(other)
        additional_any = True
    else:
        unknown_costs.append("Other pre-delivery costs")

    if acquisition == "UNKNOWN":
        unknown_costs.append("Acquisition / supplier cost")

    # --- Known prepayment / deposit ---
    known_prepayment: Any = deposit["amount"] if deposit["state"] == ST_KNOWN else "UNKNOWN"
    if deposit["state"] == ST_PARTIAL:
        unknown_costs.append("Deposit amount (percent known, base unknown)" if deposit.get("percent") else "Deposit amount")

    # --- Capital requirement (amount we may need before gov payment) ---
    # Only sum evidenced pre-delivery cash. Do NOT treat full acquisition as
    # "known capital required" when payment timing / deposit is unknown.
    known_pieces: list[float] = []
    known_labels: list[str] = []

    if known_prepayment != "UNKNOWN":
        known_pieces.append(float(known_prepayment))
        known_labels.append("deposit/prepayment")
    if additional_any:
        known_pieces.append(additional_sum)
        known_labels.append("known additional costs")

    # Full acquisition counts as known capital need only when payment timing evidences
    # prepayment / payment before ship / COD / due on receipt — not on assumed Net terms.
    full_acq_as_capital = False
    pt_u = str(payment_terms).upper() if _known(payment_terms) else ""
    timing_u = str(payment_timing).upper() if _known(payment_timing) else ""
    prepaid_signals = (
        "PREPAY",
        "PRE-PAY",
        "PREPAYMENT",
        "PAYMENT BEFORE",
        "BEFORE SHIP",
        "COD",
        "DUE ON RECEIPT",
        "DUE BEFORE",
        "100%",
        "FULL PAYMENT",
        "WIRE BEFORE",
    )
    if acquisition != "UNKNOWN" and (
        any(sig in pt_u for sig in prepaid_signals)
        or any(sig in timing_u for sig in prepaid_signals)
    ):
        # Avoid double-counting if deposit already equals acquisition
        if known_prepayment == "UNKNOWN" or (
            isinstance(known_prepayment, (int, float)) and float(known_prepayment) < float(acquisition)
        ):
            # If deposit is partial, capital known = deposit + (nothing else assumed)
            # If terms say full prepay and no separate deposit, use acquisition
            if known_prepayment == "UNKNOWN":
                known_pieces.append(float(acquisition))
                known_labels.append("acquisition (prepay evidenced)")
                full_acq_as_capital = True

    known_requirement: Any = "UNKNOWN"
    if known_pieces:
        known_requirement = round(sum(known_pieces), 2)

    # State for capital required
    blocking_unknowns: list[str] = []
    if acquisition == "UNKNOWN":
        blocking_unknowns.append("Acquisition cost unknown")
        capital_state = ST_UNKNOWN
    elif not _known(payment_terms) and deposit["state"] == ST_UNKNOWN:
        blocking_unknowns.append("Supplier payment terms unknown")
        blocking_unknowns.append("Deposit / prepayment requirement unknown")
        # Acquisition known but capital timing/amount for execution unresolved
        capital_state = ST_PARTIAL
        if known_requirement == "UNKNOWN" and additional_any:
            capital_state = ST_PARTIAL
    elif deposit["state"] == ST_UNKNOWN and not full_acq_as_capital:
        blocking_unknowns.append("Deposit / prepayment requirement unknown")
        capital_state = ST_PARTIAL if known_requirement != "UNKNOWN" or acquisition != "UNKNOWN" else ST_UNKNOWN
    elif not _known(payment_terms) and not full_acq_as_capital:
        blocking_unknowns.append("Supplier payment timing unknown")
        capital_state = ST_PARTIAL
    elif known_requirement != "UNKNOWN":
        capital_state = ST_KNOWN
    else:
        capital_state = ST_PARTIAL

    # Cash timing / cycle
    supplier_pay_when = "UNKNOWN"
    if full_acq_as_capital or (
        _known(payment_terms) and any(sig in pt_u for sig in prepaid_signals)
    ):
        supplier_pay_when = "Before shipment / delivery (evidenced prepay)"
    elif deposit["state"] == ST_KNOWN and known_prepayment != "UNKNOWN":
        supplier_pay_when = f"Deposit due per supplier terms ({deposit.get('raw')})"
        if not _known(payment_terms):
            blocking_unknowns.append("Remaining balance payment timing unknown")
    elif _known(payment_terms):
        supplier_pay_when = str(payment_terms)
    else:
        supplier_pay_when = "UNKNOWN"

    cash_timing_state = ST_UNKNOWN
    timing_known_n = sum(
        [
            supplier_pay_when != "UNKNOWN",
            _known(delivery),
            _known(gov_pay),
        ]
    )
    if timing_known_n == 3:
        cash_timing_state = ST_KNOWN
    elif timing_known_n >= 1:
        cash_timing_state = ST_PARTIAL
    else:
        cash_timing_state = ST_UNKNOWN
        if "Capital timing unknown" not in blocking_unknowns:
            blocking_unknowns.append("Capital timing unknown")

    cash_cycle = {
        "supplier_payment": supplier_pay_when,
        "shipment_delivery": delivery if _known(delivery) else "UNKNOWN",
        "government_payment": gov_pay if _known(gov_pay) else "UNKNOWN",
        "gap_note": (
            "Gap between supplier payment and government payment cannot be computed — timing UNKNOWN"
            if cash_timing_state != ST_KNOWN
            else "Timing fields evidenced — review dates before assuming float"
        ),
        "state": cash_timing_state,
        "invents_net_terms": False,
    }

    funding = _funding_path_visibility(row, deal)

    # Capital needed vs funding source — mandatory separation
    capital_vs_source = {
        "capital_required_state": capital_state,
        "capital_required_amount": known_requirement,
        "funding_source_state": funding["source_state"],
        "funded": False,  # never auto-convert
        "note": (
            "Capital requirement and funding source are separate. "
            "A known requirement with UNKNOWN source is NOT funded."
        ),
    }

    # Operator narrative (evidence-gated)
    if capital_state == ST_KNOWN and funding["source_state"] == ST_UNKNOWN:
        narrative = (
            "Economically attractive may still be true separately — "
            "execution funding is unresolved. Capital requirement is known; funding source is UNKNOWN."
        )
    elif capital_state == ST_KNOWN and funding["path_state"] in {ST_CONDITIONAL, ST_PARTIAL}:
        narrative = (
            "Known capital requirement appears documented; funding path exists but remains "
            "conditional/unverified — not marked funded."
        )
    elif capital_state == ST_KNOWN and deposit["state"] == ST_KNOWN and known_prepayment == 0.0:
        narrative = (
            "Known capital requirement appears manageable based on documented supplier terms "
            "(no deposit evidenced); confirm payment timing still applies before execution."
        )
    elif capital_state == ST_PARTIAL:
        narrative = (
            "Some cost evidence exists, but capital required before government payment "
            "is only partially known — do not treat as a precise funding ask."
        )
    elif capital_state == ST_UNKNOWN:
        narrative = "Capital requirement unknown — gather supplier commercial evidence first."
    else:
        narrative = "Review known capital figures and funding path conditions before deciding."

    # Next action (material blockers only)
    next_action = _select_next_action(
        capital_state=capital_state,
        deposit=deposit,
        payment_terms=payment_terms,
        funding=funding,
        acquisition=acquisition,
    )

    # Deduped material actions
    actions_created: list[dict[str, Any]] = []
    if ensure_actions and _known(oid):
        actions_created = ensure_capital_gate_actions(
            oid,
            next_action=next_action,
            blocking_unknowns=blocking_unknowns,
            capital_state=capital_state,
            funding=funding,
            acquisition=acquisition,
            persist=persist_actions,
        )

    beginner = {
        "title": "Can we fund this deal?",
        "money_before_we_get_paid": known_requirement,
        "money_state": capital_state,
        "when_we_may_need_it": cash_timing_state,
        "do_we_have_a_funding_source": funding["source_state"],
        "what_blocks_us": blocking_unknowns[:5],
        "what_to_do_next": (next_action or {}).get("what") or "UNKNOWN",
        "simple_summary": narrative,
    }

    return {
        "kind": "M3CapitalRequirementGate",
        "build": BUILD_TAG,
        "opportunity_id": oid,
        "question": (
            "Based on what we actually know, how much capital may be required "
            "before government payment, what causes that, and what remains unknown?"
        ),
        "capital_required": {
            "state": capital_state,
            "known_requirement": known_requirement,
            "known_prepayment": known_prepayment,
            "known_additional_costs": known_additional,
            "known_additional_total": additional_sum if additional_any else "UNKNOWN",
            "acquisition_cost": acquisition,
            "unit_price": unit,
            "quantity": qty,
            "currency": currency,
            "labels": known_labels,
        },
        "unknown_capital": {
            "items": list(dict.fromkeys(unknown_costs + [
                u for u in blocking_unknowns if "unknown" in u.lower()
            ])),
            "deposit_state": deposit["state"],
            "payment_terms": payment_terms if _known(payment_terms) else "UNKNOWN",
            "note": "UNKNOWN is not $0 and not 'probably manageable'",
        },
        "deposit": deposit,
        "payment_terms": payment_terms if _known(payment_terms) else "UNKNOWN",
        "cash_timing": cash_cycle,
        "cash_cycle": cash_cycle,
        "funding_path": {
            "state": funding["path_state"],
            "source_state": funding["source_state"],
            "paths": funding["paths_documented"],
            "conditions": funding["conditions"],
            "assignment_status": funding["assignment_status"],
            "unknowns": funding["unknowns"],
            "financing_available_claim": False,
            "funded_claim": False,
            "note": funding["note"],
        },
        "capital_vs_funding_source": capital_vs_source,
        "blocking_unknowns": list(dict.fromkeys(blocking_unknowns)),
        "next_action": next_action,
        "narrative": narrative,
        "beginner": beginner,
        "distinctions": {
            "profitability_vs_executability": True,
            "capital_required_vs_funding_source": True,
            "assumptions_forbidden": list(ASSUMPTIONS_FORBIDDEN),
        },
        "principles": {
            "unknown_never_zero": True,
            "no_assumed_personal_cash": True,
            "no_assumed_personal_credit": True,
            "no_assumed_personal_guarantee": True,
            "no_assumed_business_credit": True,
            "no_assumed_supplier_terms": True,
            "no_assumed_financing": True,
            "no_lender_approval": True,
            "no_auto_outreach": True,
            "no_ai_spend": True,
            "does_not_mark_funded_without_evidence": True,
            "does_not_mark_red_merely_for_unknown_financing": True,
            "does_not_mark_green_for_theoretical_path": True,
            "human_verification_required": True,
        },
        "actions_ensured": actions_created,
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
        "generated_at": _utc(),
        "read_only": True,
    }


def _select_next_action(
    *,
    capital_state: str,
    deposit: dict[str, Any],
    payment_terms: Any,
    funding: dict[str, Any],
    acquisition: Any,
) -> dict[str, Any] | None:
    """Material next steps only — not every optional gap."""
    if acquisition == "UNKNOWN":
        return {
            "what": "Obtain supplier quote / acquisition cost evidence",
            "why": "Cannot assess capital need without acquisition evidence",
            "evidence_required": ["Supplier quote with price"],
        }
    if deposit["state"] == ST_UNKNOWN and capital_state != ST_KNOWN:
        return {
            "what": "Determine required deposit",
            "why": "Deposit unknown — pre-delivery capital need cannot be finalized",
            "evidence_required": ["Supplier deposit / prepayment terms"],
        }
    if not _known(payment_terms):
        return {
            "what": "Confirm supplier payment terms",
            "why": "Payment timing unknown — cash cycle and capital timing unresolved",
            "evidence_required": ["Supplier payment terms evidence"],
        }
    if (
        capital_state in {ST_KNOWN, ST_PARTIAL}
        and funding.get("source_state") in {ST_UNKNOWN, ST_CONDITIONAL, ST_PROPOSED_AI, ST_STALE}
        and acquisition != "UNKNOWN"
    ):
        return {
            "what": "Verify funding path conditions",
            "why": "Capital need is at least partially known; funding source remains unresolved",
            "evidence_required": ["Documented funding path conditions / lender or program evidence"],
        }
    return {
        "what": "Review capital figures before pursuit decision",
        "why": "Re-check known requirement against funding source evidence",
        "evidence_required": ["Current quote + funding path evidence"],
    }


def ensure_capital_gate_actions(
    opportunity_id: str,
    *,
    next_action: dict[str, Any] | None,
    blocking_unknowns: list[str],
    capital_state: str,
    funding: dict[str, Any],
    acquisition: Any,
    persist: bool = False,
) -> list[dict[str, Any]]:
    """Create Action Orchestration items for material capital blockers — deduped."""
    if not _known(opportunity_id):
        return []
    # Only when acquisition known OR capital partially known — optional gaps skipped
    material = capital_state in {ST_KNOWN, ST_PARTIAL} or acquisition != "UNKNOWN"
    if not material and acquisition == "UNKNOWN":
        # Pricing gap already handled by pursuit readiness
        return []

    titles: list[tuple[str, str, list[str]]] = []
    lows = " ".join(blocking_unknowns).lower()
    if "deposit" in lows or (next_action and "deposit" in str(next_action.get("what") or "").lower()):
        titles.append(
            (
                "Determine required deposit",
                "Deposit / prepayment unknown — material to capital need",
                ["Supplier deposit terms evidence"],
            )
        )
    if "payment" in lows or (next_action and "payment terms" in str(next_action.get("what") or "").lower()):
        titles.append(
            (
                "Confirm supplier payment terms",
                "Supplier payment timing unknown — material to cash cycle",
                ["Supplier payment terms evidence"],
            )
        )
    if (
        capital_state in {ST_KNOWN, ST_PARTIAL}
        and funding.get("source_state") in {ST_UNKNOWN, ST_CONDITIONAL, ST_PROPOSED_AI, ST_STALE}
    ):
        titles.append(
            (
                "Verify funding path conditions",
                "Capital need known/partial; funding source unresolved",
                ["Funding path conditions evidence"],
            )
        )

    if not titles:
        return []

    created: list[dict[str, Any]] = []
    try:
        from m3_action_orchestration_read import create_action, list_actions

        existing = {
            str(a.get("title") or "").lower()
            for a in list_actions(opportunity_id=str(opportunity_id), limit=80)
        }
    except Exception:
        return []

    for title, why, evid in titles:
        if title.lower() in existing:
            continue
        try:
            action = create_action(
                {
                    "title": title,
                    "action_type": "RESEARCH",
                    "why": why,
                    "trigger_source": f"capital_requirement_gate:{opportunity_id}:{title}",
                    "opportunity_id": opportunity_id,
                    "related_opportunity": opportunity_id,
                    "evidence_requirements": evid,
                    "created_by": "capital_requirement_gate",
                },
                persist=persist,
            )
            created.append(action)
            existing.add(title.lower())
        except Exception as e:
            log.debug("capital gate action skipped: %s", e)
    return created


def attach_capital_requirement_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        r = row or {"canonical_id": deal.get("canonical_id")}
        assessment = build_capital_requirement_assessment(r, deal=out, ensure_actions=False)
        out["capital_requirement_gate"] = {
            "kind": "M3CapitalRequirementDealRoom",
            "build": BUILD_TAG,
            "opportunity_id": assessment.get("opportunity_id"),
            "assessment": assessment,
            "beginner": assessment.get("beginner"),
            "display": {
                "title": "Can we fund this deal?",
                "capital_required": assessment["capital_required"]["state"],
                "known_requirement": assessment["capital_required"]["known_requirement"],
                "known_prepayment": assessment["capital_required"]["known_prepayment"],
                "known_additional_costs": assessment["capital_required"]["known_additional_costs"],
                "cash_timing": assessment["cash_timing"]["state"],
                "funding_path": assessment["funding_path"]["state"],
                "blocking_unknowns": assessment["blocking_unknowns"],
                "next_action": assessment.get("next_action"),
            },
            "read_only": True,
        }
        # Surface capital into deal.funding without inventing funded status
        funding = dict(_as_dict(out.get("funding")))
        funding["capital_requirement"] = assessment["capital_required"]["known_requirement"]
        funding["capital_requirement_state"] = assessment["capital_required"]["state"]
        funding["cash_timing_state"] = assessment["cash_timing"]["state"]
        funding["funding_path_state"] = assessment["funding_path"]["state"]
        funding["funding_source_state"] = assessment["funding_path"]["source_state"]
        funding["funded"] = False
        funding["unknowns"] = assessment["blocking_unknowns"]
        funding["capital_gate_build"] = BUILD_TAG
        out["funding"] = funding
    except Exception as e:
        log.warning("capital gate attach failed: %s", e)
        out["capital_requirement_gate"] = {
            "kind": "M3CapitalRequirementDealRoom",
            "build": BUILD_TAG,
            "error": "capital_requirement_gate_unavailable",
        }
    return out


def enrich_economics_visibility_with_capital(
    dim: dict[str, Any],
    assessment: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge capital gate signals into Pursuit Readiness economics dimension — no RED/GREEN force."""
    if not isinstance(dim, dict) or not isinstance(assessment, dict):
        return dim
    out = dict(dim)
    cap = _as_dict(assessment.get("capital_required"))
    fund = _as_dict(assessment.get("funding_path"))
    unknowns = list(out.get("unknowns") or [])
    for u in assessment.get("blocking_unknowns") or []:
        if u not in unknowns:
            unknowns.append(u)
    out["unknowns"] = unknowns

    qa = dict(out.get("questions_answered") or {})
    cap_state = cap.get("state") or ST_UNKNOWN
    qa["cash_requirements_understood"] = (
        ST_KNOWN if cap_state == ST_KNOWN else (ST_PARTIAL if cap_state == ST_PARTIAL else ST_UNKNOWN)
    )
    qa["funding_source_resolved"] = (
        ST_KNOWN
        if fund.get("source_state") == ST_VERIFIED
        else (ST_PARTIAL if fund.get("source_state") == ST_CONDITIONAL else ST_UNKNOWN)
    )
    out["questions_answered"] = qa

    # Refine why when capital known but funding unresolved — stay PARTIAL, never auto GREEN/RED
    if cap_state == ST_KNOWN and fund.get("source_state") in {ST_UNKNOWN, ST_CONDITIONAL}:
        if out.get("state") == ST_KNOWN:
            out["state"] = ST_PARTIAL
        out["why"] = (
            "Margin/cost may be supported, but cash requirement is known and funding path is unresolved"
        )
        out["next_action"] = assessment.get("next_action") or out.get("next_action")
    elif cap_state == ST_PARTIAL and out.get("state") in {ST_KNOWN, ST_PARTIAL, ST_UNKNOWN}:
        if out.get("state") == ST_KNOWN:
            out["state"] = ST_PARTIAL
        if "acquisition" in str(out.get("why") or "").lower() or not out.get("why"):
            out["why"] = (
                "Acquisition cost may be known; supplier payment timing or deposit still unknown"
            )
        na = assessment.get("next_action")
        if na:
            out["next_action"] = na

    out["capital_requirement_gate"] = {
        "state": cap_state,
        "known_requirement": cap.get("known_requirement"),
        "funding_path_state": fund.get("state"),
        "funding_source_state": fund.get("source_state"),
        "build": BUILD_TAG,
    }
    return out
