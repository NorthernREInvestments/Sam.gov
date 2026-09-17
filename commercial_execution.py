"""Commercial execution plan, RFQ packets, call sheets, scripts, financing, CO timing.

Operator-facing — no Stage 0–3 jargon. Zero external calls.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from typing import Any

from quote_validation import NEGOTIATION_SUGGESTIONS, default_negotiation_suggestions

FACT_VERIFIED = "VERIFIED"
FACT_CALCULATED = "CALCULATED"
FACT_UNKNOWN = "UNKNOWN"
FACT_ASSESSMENT = "ASSESSMENT"
FACT_SUGGESTION = "SUGGESTION"  # never a fact

PLAN_STEP_OPEN = "OPEN"
PLAN_STEP_BLOCKED = "BLOCKED"
PLAN_STEP_DONE = "DONE"
PLAN_STEP_READY = "READY"


def _utc() -> str:
    return now_utc().isoformat()


def _bom_known_lines(bom: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Only VERIFIED/CALCULATED components — UNKNOWN never presented as known."""
    out = []
    for item in bom or []:
        if not isinstance(item, dict):
            continue
        st = str(item.get("status") or "").upper()
        if st not in {FACT_VERIFIED, FACT_CALCULATED}:
            continue
        out.append(
            {
                "component": item.get("component"),
                "value": item.get("value"),
                "quantity": item.get("quantity"),
                "quantity_per_server": item.get("quantity_per_server"),
                "status": st,
                "evidence": item.get("solicitation_evidence"),
            }
        )
    return out


def build_commercial_execution_plan(
    *,
    bom_complete: bool,
    has_valid_quote: bool,
    channel_resolved: bool,
    availability_verified: bool,
    freight_known: bool,
    compliance_resolved: bool,
    financing_pass: bool,
    proposed_bid_set: bool,
    actual_profit_complete: bool,
    deal_ready: bool,
    bid_ready: bool,
) -> dict[str, Any]:
    """Dependency-aware commercial sequence."""
    steps = [
        {
            "id": 1,
            "action": "Obtain exact supplier quote(s)",
            "depends_on": [],
            "blocking": True,
            "status": PLAN_STEP_DONE if has_valid_quote else (PLAN_STEP_READY if bom_complete else PLAN_STEP_BLOCKED),
            "blocked_reason": None if bom_complete else "BOM must be complete before quote requests",
        },
        {
            "id": 2,
            "action": "Verify Dell federal channel / OEM letter",
            "depends_on": [1],
            "blocking": True,
            "status": PLAN_STEP_DONE if channel_resolved else (PLAN_STEP_READY if has_valid_quote else PLAN_STEP_BLOCKED),
            "blocked_reason": None if has_valid_quote else "Need supplier engagement/quote path first",
        },
        {
            "id": 3,
            "action": "Verify 14-unit availability and 30-day delivery",
            "depends_on": [1],
            "blocking": True,
            "status": PLAN_STEP_DONE if availability_verified else (PLAN_STEP_READY if has_valid_quote else PLAN_STEP_BLOCKED),
        },
        {
            "id": 4,
            "action": "Determine freight treatment/cost",
            "depends_on": [1],
            "blocking": True,
            "status": PLAN_STEP_DONE if freight_known else PLAN_STEP_READY,
            "notes": "May use supplier quote and/or CO FOB clarification",
        },
        {
            "id": 5,
            "action": "Determine exact compliance representations needed",
            "depends_on": [1, 2],
            "blocking": True,
            "status": PLAN_STEP_DONE if compliance_resolved else PLAN_STEP_BLOCKED,
        },
        {
            "id": 6,
            "action": "Obtain financing confirmation",
            "depends_on": [1],
            "blocking": True,
            "status": PLAN_STEP_DONE if financing_pass else (PLAN_STEP_READY if has_valid_quote else PLAN_STEP_BLOCKED),
            "blocked_reason": None if has_valid_quote else "Need acquisition size context from quote first",
        },
        {
            "id": 7,
            "action": "Set company proposed bid price",
            "depends_on": [1, 4, 6],
            "blocking": True,
            "status": PLAN_STEP_DONE if proposed_bid_set else PLAN_STEP_BLOCKED,
        },
        {
            "id": 8,
            "action": "Calculate actual profit",
            "depends_on": [7],
            "blocking": True,
            "status": PLAN_STEP_DONE if actual_profit_complete else PLAN_STEP_BLOCKED,
        },
        {
            "id": 9,
            "action": "Deal readiness",
            "depends_on": [8],
            "blocking": True,
            "status": PLAN_STEP_DONE if deal_ready else PLAN_STEP_BLOCKED,
        },
        {
            "id": 10,
            "action": "Bid readiness",
            "depends_on": [9],
            "blocking": True,
            "status": PLAN_STEP_DONE if bid_ready else PLAN_STEP_BLOCKED,
        },
    ]
    # Fix step 5 ready when quote exists
    if has_valid_quote and not compliance_resolved:
        steps[4]["status"] = PLAN_STEP_READY
    if has_valid_quote and freight_known is False:
        steps[3]["status"] = PLAN_STEP_READY
    if has_valid_quote and channel_resolved is False:
        steps[1]["status"] = PLAN_STEP_READY
    if (
        has_valid_quote
        and freight_known
        and financing_pass
        and not proposed_bid_set
    ):
        steps[6]["status"] = PLAN_STEP_READY
    if proposed_bid_set and not actual_profit_complete:
        steps[7]["status"] = PLAN_STEP_READY
    if actual_profit_complete and not deal_ready:
        steps[8]["status"] = PLAN_STEP_READY
    if deal_ready and not bid_ready:
        steps[9]["status"] = PLAN_STEP_READY

    current = next((s for s in steps if s["status"] == PLAN_STEP_READY), None)
    if current is None:
        current = next((s for s in steps if s["status"] == PLAN_STEP_BLOCKED), steps[-1])

    return {
        "schema": "commercial-execution-plan-v1",
        "steps": steps,
        "current_step": current,
        "LIVE_API_REQUESTS": 0,
    }


def build_supplier_rfq_packet(
    *,
    solicitation_number: str,
    agency: str | None,
    bom: list[dict[str, Any]] | None,
    destination: str,
    delivery_requirement: str,
    installation_note: str | None,
    quantity: int = 14,
) -> dict[str, Any]:
    known = _bom_known_lines(bom)
    unknowns = [
        {
            "component": i.get("component"),
            "status": i.get("status"),
            "note": "Do not present to supplier as known",
        }
        for i in (bom or [])
        if str(i.get("status") or "").upper() not in {FACT_VERIFIED, FACT_CALCULATED}
    ]
    return {
        "schema": "supplier-rfq-packet-v1",
        "opportunity": {
            "solicitation_number": solicitation_number,
            "agency": agency,
            "fact_class": "VERIFIED_GOVERNMENT_FACT",
        },
        "product": {
            "name": next((k["value"] for k in known if k["component"] == "base_system"), None),
            "part_number": next((k["value"] for k in known if k["component"] == "part_number"), None),
            "quantity": quantity,
        },
        "exact_configuration": known,
        "unknown_configuration_fields_excluded": unknowns,
        "destination": destination,
        "delivery": delivery_requirement,
        "installation": installation_note,
        "required_commercial_confirmations": [
            "exact configuration match",
            "quantity 14",
            "unit price",
            "extended price",
            "quote total",
            "availability",
            "lead time",
            "ability to meet 30-day ARO",
            "freight included or separate",
            "freight amount if separate",
            "quote expiration",
            "payment terms",
            "government PO terms if available",
            "Dell federal-channel eligibility",
            "OEM / authorized reseller letter availability",
            "COO for exact supplied configuration where applicable",
            "applicable BAA/TAA representations if supplier can provide them",
            "warranty/support confirmation",
            "any mandatory fees",
            "any deposit/upfront requirement",
        ],
        "rules": {
            "never_include_unknown_as_known": True,
            "blank_is_not_zero": True,
        },
        "LIVE_API_REQUESTS": 0,
    }


def build_supplier_call_sheet(
    *,
    supplier_name: str,
    supplier_id: int | None,
    website: str | None = None,
    phone: str | None = None,
    contact_name: str | None = None,
    contact_verified: bool = False,
    why: str,
    what_we_need: list[str],
    questions: list[str],
    negotiation_goals: list[str],
    last_contact: str | None = None,
    next_followup: str | None = None,
) -> dict[str, Any]:
    contact_block: dict[str, Any]
    if contact_verified and (phone or contact_name):
        contact_block = {
            "status": "VERIFIED_CONTACT_FACT" if contact_verified else "OPERATOR_REPORTED",
            "name": contact_name,
            "phone": phone,
        }
    else:
        contact_block = {
            "status": "CONTACT_INFORMATION_NEEDED",
            "name": None,
            "phone": None,
            "message": "CONTACT INFORMATION NEEDED — do not invent",
        }
    return {
        "supplier": supplier_name,
        "supplier_id": supplier_id,
        "website": website,
        "contact_status": contact_block["status"],
        "contact": contact_block,
        "why_we_are_calling": why,
        "what_we_need": what_we_need,
        "questions": questions,
        "negotiation_goals": negotiation_goals,
        "last_contact": last_contact,
        "next_followup": next_followup,
        "LIVE_API_REQUESTS": 0,
    }


def build_supplier_call_script(
    *,
    product_summary: str,
    quantity: int = 1,
    delivery_prompt: str | None = None,
) -> dict[str, Any]:
    delivery = delivery_prompt or "Can you meet the solicitation delivery requirement to the stated destination?"
    return {
        "title": "Supplier call script",
        "opening": (
            f"I'm working on a federal RFQ for {quantity} {product_summary} and need a firm "
            "reseller quote for the exact configuration."
        ),
        "sections": [
            {"topic": "Configuration", "prompt": "Confirm you can quote the exact BOM we send — not a substitute."},
            {"topic": "Quantity", "prompt": f"Confirm pricing and availability for quantity {quantity}."},
            {"topic": "Delivery", "prompt": delivery},
            {"topic": "Availability / lead time", "prompt": "What is current lead time for this exact build?"},
            {"topic": "Federal authorization", "prompt": "Confirm Dell federal-channel eligibility for this transaction."},
            {"topic": "OEM letter", "prompt": "Can you provide an OEM/authorized reseller letter naming our company?"},
            {"topic": "Pricing", "prompt": "Please provide unit, extended, and total firm quote."},
            {"topic": "Freight", "prompt": "Is freight included to destination, or quoted separately?"},
            {"topic": "Payment terms", "prompt": "What payment terms apply? Can you accept a government PO?"},
            {"topic": "Quote validity", "prompt": "How long is the quote valid? Can you hold pricing through expected award?"},
        ],
        "prohibited_claims": [
            "Do not claim award, existing government PO, competitor price, Dell partner status,",
            "financing approval, past purchases, or any other unverified fact.",
        ],
        "tone": "legitimate business buyer",
        "is_fact": False,
        "LIVE_API_REQUESTS": 0,
    }


def build_negotiation_playbook() -> dict[str, Any]:
    strategies = []
    mapping = [
        ("federal/public-sector pricing", "May yield authorized government-path pricing"),
        ("project / bid pricing", "RFQ volume may qualify for project pricing"),
        ("volume pricing for 14", "Quantity 14 can support volume ask"),
        ("Dell special / deal registration", "If available through authorized channel"),
        ("manager / public-sector escalation", "Escalate when front-line quote is incomplete"),
        ("freight included", "Reduces unknown freight cost"),
        ("price hold through anticipated award", "Protects economics during bid window"),
        ("longer quote validity", "Aligns with award timing"),
        ("supplier-direct fulfillment", "May simplify logistics"),
        ("government PO terms", "Required for financing/execution path"),
        ("alternative authorized distribution path", "Compare compliant channels"),
        ("manufacturer-supported competitive pricing", "Only via legitimate channel"),
    ]
    for strategy, why in mapping:
        strategies.append(
            {
                "strategy": strategy,
                "why_it_may_help": why,
                "status": "SUGGESTION",
                "operator_result": None,
                "evidence": None,
                "next_action": None,
                "is_fact": False,
            }
        )
    # Also include default_negotiation_suggestions for consistency
    return {
        "title": "Negotiation playbook — lowest legitimate acquisition cost",
        "rules": ["Never recommend deception", "Suggestions are not facts"],
        "strategies": strategies,
        "legacy_suggestions": default_negotiation_suggestions(),
        "LIVE_API_REQUESTS": 0,
    }


def build_financing_verification_packet(
    *,
    approximate_amount: float | None = None,
    amount_status: str = FACT_UNKNOWN,
    transaction_description: str = "Federal product resale — Dell servers",
) -> dict[str, Any]:
    return {
        "schema": "financing-verification-packet-v1",
        "hard_requirements": {
            "personal_guarantee": False,
            "personal_credit": False,
            "personal_cash_upfront": 0,
            "status": "POLICY",
        },
        "transaction": {
            "description": transaction_description,
            "approximate_amount": approximate_amount,
            "amount_status": amount_status,
            "notes": "Do not invent amount — only use verified/usable quote totals",
        },
        "questions": [
            "Will you finance this exact transaction type?",
            "Federal government customer?",
            "Product resale?",
            "New operating company eligible?",
            "Minimum transaction?",
            "Maximum transaction?",
            "PG required? (must be NO)",
            "Personal credit pull? (must be NO)",
            "Borrower cash contribution? (must be ZERO)",
            "Supplier paid directly?",
            "Advance percentage?",
            "Fees?",
            "Recourse?",
            "Required supplier terms?",
            "Required government PO status?",
            "Time to fund?",
            "Can funding support delivery/performance timing?",
            "What documentation is required?",
        ],
        "pass_rule": "FINANCING_PASS only when hard requirements are VERIFIED",
        "LIVE_API_REQUESTS": 0,
    }


def evaluate_co_clarification_timing(
    *,
    co_ready: bool,
    co_topic: str | None,
    has_valid_quote: bool,
    bid_deadline: date | None,
    clarification_deadline: date | None = None,
    today: date | None = None,
    hours_remaining: float | None = None,
    fatal_for_viability: bool = False,
) -> dict[str, Any]:
    """
    CO question can be READY BUT NOT SENT while commercial viability is tested first,
    unless deadline risk elevates it.

    Nonfatal FOB/freight normally yields to supplier quote first.
    Elevate when clarification deadline is imminent, bid window is nearly closed
    (<24h), or the question is fatal for commercial viability.
    """
    from datetime import timedelta

    as_of = today or today_local()
    elevate = False
    risk_notes: list[str] = []
    if clarification_deadline and clarification_deadline <= as_of + timedelta(days=2):
        elevate = True
        risk_notes.append("clarification_deadline_within_2_days")
    if bid_deadline:
        days = (bid_deadline - as_of).days
        if days < 0:
            elevate = True
            risk_notes.append("bid_deadline_passed")
        elif days == 0 or (hours_remaining is not None and hours_remaining < 24):
            elevate = True
            risk_notes.append("bid_deadline_within_24_hours")
        elif fatal_for_viability and days <= 2 and not has_valid_quote:
            elevate = True
            risk_notes.append("fatal_clarification_and_bid_within_2_days")
    if hours_remaining is not None and hours_remaining < 24:
        elevate = True
        if "bid_deadline_within_24_hours" not in risk_notes:
            risk_notes.append("under_24_hours_remaining")

    if not co_ready:
        return {
            "co_question_ready": False,
            "status": "NOT_READY",
            "topic": co_topic,
            "first_action": False,
            "LIVE_API_REQUESTS": 0,
        }

    if elevate:
        return {
            "co_question_ready": True,
            "status": "READY — ELEVATED BY DEADLINE RISK",
            "topic": co_topic,
            "first_action": True,
            "reason": "Deadline risk — clarify before window closes",
            "deadline_risk": risk_notes,
            "auto_email": False,
            "LIVE_API_REQUESTS": 0,
        }

    return {
        "co_question_ready": True,
        "status": "READY BUT NOT SENT",
        "topic": co_topic or "FOB / freight responsibility",
        "first_action": False,
        "reason": "Commercial viability not yet established — obtain supplier quote first unless operator chooses otherwise",
        "deadline_risk": risk_notes,
        "auto_email": False,
        "operator_may_review_send_manually": True,
        "LIVE_API_REQUESTS": 0,
    }


def seed_commercial_artifacts(
    *,
    bom: list[dict[str, Any]],
    suppliers: list[dict[str, Any]],
    fob_draft: str | None,
    fob_safe_to_ask: bool,
    due_date: date | None,
    hours_remaining: float | None = None,
    solicitation_number: str = "",
    agency: str | None = None,
    destination: str = "",
    delivery_requirement: str = "",
    installation_note: str | None = None,
    quantity: int | float | None = None,
    product_summary: str | None = None,
) -> dict[str, Any]:
    from deal_context import part_number_from_bom, primary_product_label, required_quantity_from_bom

    qty = int(quantity or required_quantity_from_bom(bom) or 1)
    product = product_summary or primary_product_label(bom) or "configured product"
    part = part_number_from_bom(bom)
    if part and part not in product:
        product = f"{product} (part {part})"

    packet = build_supplier_rfq_packet(
        solicitation_number=solicitation_number,
        agency=agency,
        bom=bom,
        destination=destination,
        delivery_requirement=delivery_requirement,
        installation_note=installation_note,
        quantity=qty,
    )
    script = build_supplier_call_script(
        product_summary=product,
        quantity=qty,
        delivery_prompt=f"Can you meet delivery requirement: {delivery_requirement or 'per solicitation'}?",
    )
    playbook = build_negotiation_playbook()
    questions = [
        f"Can you quote exact configuration for qty {qty}?",
        "Unit + extended + total?",
        f"Availability and lead time for quantity {qty}?",
        "Freight included or separate?",
        "Federal channel / OEM letter for our company?",
        "Payment / government PO terms?",
        "Quote validity / price hold?",
    ]
    sheets = []
    for s in suppliers:
        sheets.append(
            build_supplier_call_sheet(
                supplier_name=s.get("name") or "Unknown",
                supplier_id=s.get("id"),
                website=s.get("website"),
                phone=s.get("phone"),
                contact_name=s.get("contact_name"),
                contact_verified=bool(s.get("contact_verified")),
                why="Need firm reseller quote for exact federal RFQ configuration",
                what_we_need=packet["required_commercial_confirmations"][:8],
                questions=questions,
                negotiation_goals=[x["strategy"] for x in playbook["strategies"][:6]],
                last_contact=s.get("last_contact"),
                next_followup=s.get("next_followup"),
            )
        )
    financing = build_financing_verification_packet(
        approximate_amount=None,
        amount_status=FACT_UNKNOWN,
    )
    co_timing = evaluate_co_clarification_timing(
        co_ready=bool(fob_safe_to_ask),
        co_topic="FOB / freight responsibility",
        has_valid_quote=False,
        bid_deadline=due_date,
        hours_remaining=hours_remaining,
    )
    co_card = {
        "title": "CO CLARIFICATION",
        "topic": "FOB / freight responsibility",
        "status": co_timing.get("status"),
        "safe_to_ask_co": fob_safe_to_ask,
        "confidence": "HIGH",
        "draft": fob_draft,
        "timing": co_timing,
        "auto_email": False,
    }
    plan = build_commercial_execution_plan(
        bom_complete=True,
        has_valid_quote=False,
        channel_resolved=False,
        availability_verified=False,
        freight_known=False,
        compliance_resolved=False,
        financing_pass=False,
        proposed_bid_set=False,
        actual_profit_complete=False,
        deal_ready=False,
        bid_ready=False,
    )
    return {
        "supplier_rfq_packet": packet,
        "supplier_call_sheets": sheets,
        "supplier_call_script": script,
        "negotiation_playbook": playbook,
        "financing_packet": financing,
        "co_clarification_card": co_card,
        "commercial_plan": plan,
        "generated_at": _utc(),
        "LIVE_API_REQUESTS": 0,
    }


def seed_opp199_commercial_artifacts(**kwargs: Any) -> dict[str, Any]:
    """Deprecated — prefer fixtures.opp199 for Opp199-specific seeds."""
    from fixtures.opp199 import seed_opp199_commercial_artifacts as _seed

    return _seed(**kwargs)
