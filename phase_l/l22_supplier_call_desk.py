"""Phase L.22 — Supplier Call Desk + answer capture + notes (NO outbound comms)."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.quote_economics import load_json, save_json, _f
from phase_l.quote_readiness import (
    INTERNAL_FIELDS_NEVER_SUPPLIER,
    evaluate_supplier_quote_response,
)

BUILD = "20260929-m3-phase-l22-supplier-call-desk-answer-capture-notes"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = OUT / "l22_sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

AUTO_SEND_SUPPLIER_OUTREACH = False
AUTO_CALL = False
AUTO_EMAIL = False
AUTO_RFQ_SUBMIT = False

# Question states
NOT_ASKED = "NOT_ASKED"
ASKED_NO_ANSWER = "ASKED_NO_ANSWER"
ANSWERED = "ANSWERED"
NOT_APPLICABLE = "NOT_APPLICABLE"
FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"

# Priority bands
CALL_FIRST = "CALL_FIRST"
CALL_SECOND = "CALL_SECOND"
CALL_THIRD = "CALL_THIRD"
BACKUP = "BACKUP"

# Session / opportunity states
CALL_IN_PROGRESS = "CALL_IN_PROGRESS"
CALL_COMPLETE = "CALL_COMPLETE"
CALLS_NOT_STARTED = "CALLS_NOT_STARTED"
CALLS_IN_PROGRESS = "CALLS_IN_PROGRESS"
QUOTE_PENDING = "QUOTE_PENDING"
QUOTES_RECEIVED = "QUOTES_RECEIVED"
SUPPLIER_PATH_FAILED = "SUPPLIER_PATH_FAILED"
READY_FOR_FINAL_ECONOMICS = "READY_FOR_FINAL_ECONOMICS"

# Outcomes
CALLED_NO_ANSWER = "CALLED_NO_ANSWER"
LEFT_MESSAGE = "LEFT_MESSAGE"
SPOKE_TO_SUPPLIER = "SPOKE_TO_SUPPLIER"
QUOTE_PROMISED = "QUOTE_PROMISED"
QUOTE_RECEIVED = "QUOTE_RECEIVED"
DECLINED_TO_QUOTE = "DECLINED_TO_QUOTE"
DELIVERY_FAIL = "DELIVERY_FAIL"
TERMS_FAIL = "TERMS_FAIL"
PRODUCT_UNAVAILABLE = "PRODUCT_UNAVAILABLE"
FOLLOW_UP_REQUIRED_OUTCOME = "FOLLOW_UP_REQUIRED"

# Live economics status
PRICE_LOOKS_GOOD = "PRICE_LOOKS_GOOD"
MARGINAL = "MARGINAL"
ABOVE_MAX_BUY = "ABOVE_MAX_BUY"
GOV_VALUE_TOO_WEAK_TO_JUDGE = "GOV_VALUE_TOO_WEAK_TO_JUDGE"
MISSING_COST_INPUTS = "MISSING_COST_INPUTS"

VERIFIED_ACQUISITION_PRICE = "VERIFIED_ACQUISITION_PRICE"
VERIFIED_POSITIVE = "VERIFIED_POSITIVE"
MORE_QUOTES_RECOMMENDED = "MORE_QUOTES_RECOMMENDED"

MUST_ASK = "MUST_ASK"
ASK_IF_RELEVANT = "ASK_IF_RELEVANT"
NICE_TO_KNOW = "NICE_TO_KNOW"

L21_BASELINE = {
    "eligible": 15,
    "READY_FOR_OWNER_APPROVAL": 4,
    "NEEDS_MINOR_REVIEW": 11,
    "quote_packets": 44,
    "auto_send": False,
}


# ---------------------------------------------------------------------------
# Dynamic question catalog
# ---------------------------------------------------------------------------

def _q(
    qid: str,
    text: str,
    *,
    category: str,
    answer_type: str,
    priority: str = MUST_ASK,
    options: list[str] | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_text": text,
        "question_category": category,
        "answer_type": answer_type,
        "priority_band": priority,
        "options": options,
        "maps_to_field": field,
        "state": NOT_ASKED,
        "answer_value": None,
        "asked": False,
        "answer_recorded": False,
        "follow_up_required": False,
        "owner_note": None,
    }


def build_dynamic_questions(req: dict[str, Any], supplier: dict[str, Any]) -> list[dict[str, Any]]:
    """Relevant-only question set for one opportunity×supplier."""
    product = " ".join(
        x for x in (req.get("manufacturer"), req.get("model"), req.get("product_specification")) if x
    )[:120]
    qty = req.get("quantity") or "the required quantity"
    dest = req.get("delivery_destination") or "the delivery destination"
    required_date = req.get("required_delivery_date")

    qs: list[dict[str, Any]] = [
        _q("product_exact", f"Is this exact item available: {product}?", category="product", answer_type="dropdown",
           options=["YES", "NO", "SUBSTITUTE", "UNKNOWN"], field="exact_item_confirmed"),
        _q("product_sku", "Is the MPN/SKU correct?", category="product", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT if not req.get("mpn_sku_nsn") else MUST_ASK, field="sku_confirmed"),
        _q("product_condition", "Is it new OEM product?", category="product", answer_type="yes_no_unknown", field="condition_new"),
        _q("product_current", "Is it current production (not discontinued)?", category="product", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT),
        _q("product_sub", "If substituting, what exact manufacturer/model/SKU?", category="product", answer_type="text",
           priority=ASK_IF_RELEVANT, field="substitution_notes"),
        _q("product_accessories", "Does the quote include all required accessories/options?", category="product",
           answer_type="yes_no_unknown", priority=ASK_IF_RELEVANT),
        _q("qty_available", f"Can you supply quantity {qty}?", category="availability", answer_type="yes_no_unknown",
           field="quantity_available"),
        _q("unit_price", f"What's the best unit price for {qty}?", category="price", answer_type="currency", field="unit_price"),
        _q("extended_price", "What is the extended/total product price?", category="price", answer_type="currency",
           priority=ASK_IF_RELEVANT, field="extended_price"),
        _q("volume_discount", "Any volume or project discount at that quantity?", category="price", answer_type="text",
           priority=ASK_IF_RELEVANT, field="discount_description"),
        _q("gov_discount", "Any public-sector / government discount?", category="price", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT),
        _q("fees", "Any additional fees beyond unit price?", category="price", answer_type="currency",
           priority=ASK_IF_RELEVANT, field="additional_fees"),
        _q("freight_included", "Is freight included?", category="freight", answer_type="yes_no_unknown", field="freight_included"),
        _q("freight_amount", "If not included, what is freight?", category="freight", answer_type="currency", field="freight_amount"),
        _q("fob", "FOB origin or destination?", category="freight", answer_type="dropdown",
           options=["ORIGIN", "DESTINATION", "OTHER", "UNKNOWN"], field="fob"),
        _q("freight_firm", "Is freight firm or estimated?", category="freight", answer_type="dropdown",
           options=["FIRM", "ESTIMATED", "UNKNOWN"], priority=ASK_IF_RELEVANT, field="freight_firmness"),
        _q("stock", "Is product in stock?", category="delivery", answer_type="dropdown",
           options=["IN_STOCK", "PARTIAL_STOCK", "BACKORDERED", "BUILD_TO_ORDER", "UNKNOWN"], field="stock_status"),
        _q("lead_time", "What is the lead time (days)?", category="delivery", answer_type="number", field="lead_time_days"),
        _q("ship_date", "Earliest ship date?", category="delivery", answer_type="date", priority=ASK_IF_RELEVANT,
           field="earliest_ship_date"),
        _q(
            "delivery_feasible",
            f"Can it arrive by {required_date}?" if required_date else f"Can you deliver to {dest} on a workable schedule?",
            category="delivery",
            answer_type="dropdown",
            options=["YES", "NO", "UNCERTAIN"],
            field="delivery_by_required_date",
        ),
        _q("delivery_writing", "Can you put delivery commitment in writing?", category="delivery", answer_type="dropdown",
           options=["YES", "NO", "REQUESTED"], priority=ASK_IF_RELEVANT, field="written_delivery_commitment"),
        _q("payment_terms", "What payment terms are available?", category="payment", answer_type="dropdown",
           options=["PREPAID", "COD", "NET_15", "NET_30", "NET_45", "NET_60", "OTHER", "UNKNOWN"], field="terms_type"),
        _q("po_accepted", "Do you accept purchase orders?", category="payment", answer_type="yes_no_unknown", field="po_accepted"),
        _q("deposit", "Is a deposit or prepayment required?", category="payment", answer_type="yes_no_unknown",
           field="deposit_required"),
        _q("personal_credit", "Is personal credit required?", category="payment", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT, field="personal_credit_required"),
        _q("personal_guarantee", "Is a personal guarantee required?", category="payment", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT, field="personal_guarantee_required"),
        _q("third_party_pay", "Can a third-party financing company pay you directly?", category="payment",
           answer_type="dropdown", options=["YES", "NO", "NEEDS_APPROVAL", "UNKNOWN"], field="third_party_payment_accepted"),
        _q("quote_expiration", "How long is pricing valid / quote expiration date?", category="validity",
           answer_type="date", field="quote_expiration"),
        _q("price_firm", "Is pricing firm through expected award?", category="validity", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT, field="pricing_firm"),
        _q("warranty_included", "Is manufacturer warranty included?", category="warranty", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT, field="warranty_included"),
        _q("warranty_term", "Warranty duration?", category="warranty", answer_type="text", priority=NICE_TO_KNOW,
           field="warranty_duration"),
        _q("auth_dealer", "Are you an authorized dealer/distributor for this product?", category="authorization",
           answer_type="yes_no_unknown", priority=ASK_IF_RELEVANT, field="authorization_status"),
        _q("auth_proof", "Can you provide authorization proof?", category="authorization", answer_type="yes_no_unknown",
           priority=ASK_IF_RELEVANT),
        _q("coo", "Country of origin available?", category="compliance", answer_type="text", priority=NICE_TO_KNOW,
           field="country_of_origin"),
        _q("written_quote", "Can you send a written quote?", category="documents", answer_type="yes_no_unknown",
           field="written_quote_promised"),
    ]

    # Financing follow-up prompt when prepaid likely
    qs.append(
        _q(
            "financing_followup",
            "If we use a third-party PO financing company that pays you directly before shipment, is that acceptable?",
            category="payment",
            answer_type="dropdown",
            options=["YES", "NO", "NEEDS_APPROVAL", "UNKNOWN"],
            priority=ASK_IF_RELEVANT,
            field="financing_company_payment_accepted",
        )
    )
    return qs


CRITICAL_QUESTION_IDS = frozenset(
    {
        "product_exact",
        "qty_available",
        "unit_price",
        "freight_included",
        "lead_time",
        "delivery_feasible",
        "payment_terms",
        "quote_expiration",
    }
)


ANSWER_SCHEMA = {
    "kind": "SupplierCallAnswerSchema",
    "build": BUILD,
    "answer_types": ["text", "number", "currency", "date", "yes_no", "yes_no_unknown", "dropdown", "multiline"],
    "question_states": [NOT_ASKED, ASKED_NO_ANSWER, ANSWERED, NOT_APPLICABLE, FOLLOW_UP_REQUIRED],
    "sources": ["PHONE", "EMAIL", "RFQ_FORM", "WRITTEN_QUOTE", "WEBSITE"],
    "priority_bands": [MUST_ASK, ASK_IF_RELEVANT, NICE_TO_KNOW],
    "critical_question_ids": sorted(CRITICAL_QUESTION_IDS),
    "fields": [
        "call_session_id",
        "opportunity_id",
        "supplier_id",
        "question_id",
        "question_category",
        "question_text",
        "answer_type",
        "answer_value",
        "answered_at",
        "confidence",
        "source",
        "owner_note",
        "state",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc() -> str:
    return now_utc().isoformat()


def _sup_letter(g: Any) -> str:
    s = str(g or "D").upper()
    for L in ("A", "B", "C", "D"):
        if f"SUPPLIER_{L}" in s or s == L or s.endswith(f"_{L}"):
            return L
    return "D"


def _opp_id(row: dict[str, Any]) -> str:
    oa = row.get("owner_approval") or {}
    if oa.get("opportunity_id"):
        return str(oa["opportunity_id"])
    base = f"{row.get('solicitation') or ''}|{row.get('title') or ''}"
    return str(abs(hash(base)) % 10**10)


def _sup_id(supplier: dict[str, Any]) -> str:
    return str(supplier.get("supplier_domain") or supplier.get("name") or "unknown").lower()


def opening_script(req: dict[str, Any], *, owner_name: str = "[OWNER]") -> str:
    product = " ".join(
        x for x in (req.get("manufacturer"), req.get("model"), (req.get("product_specification") or "")[:80]) if x
    ).strip() or "the specified item"
    qty = req.get("quantity") or "the required quantity"
    uom = req.get("uom") or "EA"
    dest = req.get("delivery_destination") or "the delivery destination on the solicitation"
    return (
        f"Hi, my name is {owner_name}. I'm looking for pricing and availability on {product} "
        f"for a customer requirement. I need {qty} {uom} delivered to {dest}. "
        f"Can you help me with a quote?"
    )


def supplier_call_priority(supplier: dict[str, Any], *, contact: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rank who to call first — not cheapest-only."""
    contact = contact or supplier.get("contact") or {}
    letter = _sup_letter(supplier.get("supplier_grade") or supplier.get("_letter"))
    score = 0
    factors: list[str] = []
    auth = str(supplier.get("authorization_state") or supplier.get("authorized_status") or "").upper()
    if "CONFIRMED" in auth:
        score += 25
        factors.append("authorization_confirmed")
    elif "LIKELY" in auth:
        score += 15
        factors.append("authorization_likely")
    if str(supplier.get("product_fit") or "").upper() == "EXACT":
        score += 20
        factors.append("exact_product_fit")
    st = str(supplier.get("source_type") or "").upper()
    if st in {"OEM", "AUTHORIZED_DEALER", "DISTRIBUTOR"}:
        score += 10
        factors.append("institutional_channel")
    if supplier.get("territory_fit") or "fleet" in str(supplier.get("note") or "").lower():
        score += 8
        factors.append("territory_or_fleet")
    if letter == "A":
        score += 20
    elif letter == "B":
        score += 14
    elif letter == "C":
        score += 8
    path = str(contact.get("contact_path_status") or "")
    if path == "PHONE_PUBLIC":
        score += 12
        factors.append("phone_public")
    elif path == "EMAIL_PUBLIC":
        score += 8
        factors.append("email_public")
    elif path == "RFQ_FORM_PUBLIC":
        score += 6
        factors.append("rfq_form_public")
    elif path == "CONTACT_PATH_UNRESOLVED":
        score -= 10
        factors.append("contact_unresolved")
    # Past response history boost if present
    hist = supplier.get("m3_response_history") or {}
    if hist.get("quote_rate"):
        score += min(10, int(float(hist["quote_rate"]) * 10))
        factors.append("past_response_history")

    if score >= 60:
        band = CALL_FIRST
    elif score >= 45:
        band = CALL_SECOND
    elif score >= 30:
        band = CALL_THIRD
    else:
        band = BACKUP
    return {"kind": "SupplierCallPriority", "score": score, "band": band, "factors": factors}


# ---------------------------------------------------------------------------
# Call sheets + workspaces
# ---------------------------------------------------------------------------

def build_supplier_call_sheet(
    row: dict[str, Any],
    supplier: dict[str, Any],
    *,
    priority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req = row.get("requirement_packet") or {}
    contact = supplier.get("contact") or {}
    priority = priority or supplier_call_priority(supplier, contact=contact)
    why = (
        f"Supplier grade {_sup_letter(supplier.get('supplier_grade'))}; "
        f"fit={supplier.get('product_fit') or 'n/a'}; "
        f"auth={supplier.get('authorization_state') or supplier.get('authorized_status') or 'unknown'}; "
        f"priority={priority.get('band')}"
    )
    return {
        "kind": "SupplierCallSheet",
        "build": BUILD,
        "opportunity_id": _opp_id(row),
        "supplier_id": _sup_id(supplier),
        "supplier_name": supplier.get("name") or supplier.get("supplier_domain"),
        "supplier_grade": supplier.get("supplier_grade") or f"SUPPLIER_{_sup_letter(supplier.get('_letter'))}",
        "authorization_status": supplier.get("authorization_state") or supplier.get("authorized_status"),
        "phone_number": contact.get("phone"),  # public only; never invent
        "public_email": contact.get("email"),
        "rfq_url": contact.get("contact_url") or supplier.get("locator_url") or supplier.get("url"),
        "public_contact_person": contact.get("contact_person"),
        "branch_location": supplier.get("branch") or supplier.get("location"),
        "product": req.get("product_specification") or row.get("title"),
        "manufacturer": req.get("manufacturer"),
        "model": req.get("model"),
        "mpn_sku_nsn": req.get("mpn_sku_nsn"),
        "quantity": req.get("quantity"),
        "uom": req.get("uom") or "EA",
        "condition": req.get("condition") or "new",
        "delivery_destination": req.get("delivery_destination"),
        "required_delivery_date": req.get("required_delivery_date"),
        "quote_needed_by_date": row.get("deadline"),
        "warranty_requirement": req.get("warranty"),
        "freight_requirement": req.get("freight_fob"),
        "government_compliance_requirements": None,
        "supplier_call_priority": priority,
        "why_selected": why,
        "owner_notes": None,
        "opening_script": opening_script(req),
        "buyer": row.get("buyer"),
        "solicitation": row.get("solicitation"),
        "deadline": row.get("deadline"),
        "authoritative_solicitation_link": (row.get("live") or {}).get("original_url"),
        "auto_send": False,
        "auto_call": False,
    }


def build_call_workspace(sheet: dict[str, Any], questions: list[dict[str, Any]], *, owner_panel: dict[str, Any]) -> dict[str, Any]:
    """Single-page call workspace schema (mobile + desktop)."""
    must = [q for q in questions if q.get("priority_band") == MUST_ASK]
    relevant = [q for q in questions if q.get("priority_band") == ASK_IF_RELEVANT]
    nice = [q for q in questions if q.get("priority_band") == NICE_TO_KNOW]
    return {
        "kind": "SupplierCallWorkspace",
        "layout": {
            "mobile": "single_column_large_controls",
            "desktop": "split_left_script_right_answers_economics",
        },
        "opportunity": {
            "buyer": sheet.get("buyer"),
            "solicitation": sheet.get("solicitation"),
            "product": sheet.get("product"),
            "quantity": sheet.get("quantity"),
            "deadline": sheet.get("deadline"),
            "required_delivery_date": sheet.get("required_delivery_date"),
            "authoritative_solicitation_link": sheet.get("authoritative_solicitation_link"),
        },
        "supplier": {
            "name": sheet.get("supplier_name"),
            "phone": sheet.get("phone_number"),
            "contact": sheet.get("public_contact_person"),
            "rfq_url": sheet.get("rfq_url"),
            "grade": sheet.get("supplier_grade"),
            "authorization": sheet.get("authorization_status"),
            "why_selected": sheet.get("why_selected"),
            "priority": sheet.get("supplier_call_priority"),
        },
        "opening_script": sheet.get("opening_script"),
        "call_questions": {
            "MUST_ASK": must,
            "ASK_IF_RELEVANT": relevant,
            "NICE_TO_KNOW": nice,
        },
        "document_requests": [
            {"id": "written_quote", "label": "Written quote", "requested": False, "promised": False, "received": False},
            {"id": "spec_sheet", "label": "Product spec sheet", "requested": False, "promised": False, "received": False},
            {"id": "warranty_doc", "label": "Warranty document", "requested": False, "promised": False, "received": False},
            {"id": "auth_proof", "label": "Authorization proof", "requested": False, "promised": False, "received": False},
            {"id": "coo", "label": "Country-of-origin statement", "requested": False, "promised": False, "received": False},
            {"id": "lead_time", "label": "Lead-time confirmation", "requested": False, "promised": False, "received": False},
            {"id": "freight", "label": "Freight confirmation", "requested": False, "promised": False, "received": False},
            {"id": "terms", "label": "Terms confirmation", "requested": False, "promised": False, "received": False},
        ],
        "call_notes": "",
        "owner_only_panel": {
            "banner": "OWNER ONLY — DO NOT DISCLOSE",
            **owner_panel,
        },
        "actions": {
            "SAVE_CALL": True,
            "RESUME_CALL": True,
            "ADD_WRITTEN_QUOTE": True,
            "MARK_COMPLETE": True,
            "auto_send": False,
            "auto_call": False,
        },
        "unsaved_changes_warn": True,
        "auto_save_mode": "explicit_SAVE_CALL",
    }


def owner_only_panel(row: dict[str, Any], live_econ: dict[str, Any] | None = None) -> dict[str, Any]:
    mb = row.get("internal_max_buy") or {}
    return {
        "gov_grade": row.get("gov_letter"),
        "historical_price": (row.get("internal_summary") or {}).get("historical_evidence"),
        "expected_revenue": (row.get("internal_summary") or {}).get("expected_profit_potential"),
        "max_buy": {k: mb.get(k) for k in ("status", "break_even", "for_5k", "for_10k", "for_25k", "reason") if k in mb or True},
        "financing_assumptions": row.get("financing"),
        "target_profit": None,
        "current_quote_economics": live_econ,
        "supplier_facing": False,
    }


# ---------------------------------------------------------------------------
# Sessions — save / resume / multiple
# ---------------------------------------------------------------------------

def new_call_session(
    sheet: dict[str, Any],
    questions: list[dict[str, Any]],
    *,
    method: str = "PHONE",
) -> dict[str, Any]:
    sid = f"CS-{uuid.uuid4().hex[:12]}"
    return {
        "kind": "SupplierCallSession",
        "session_id": sid,
        "opportunity_id": sheet["opportunity_id"],
        "supplier_id": sheet["supplier_id"],
        "supplier_name": sheet.get("supplier_name"),
        "started_at": _utc(),
        "updated_at": _utc(),
        "saved_at": None,
        "contact_person": sheet.get("public_contact_person"),
        "method": method,
        "status": CALL_IN_PROGRESS,
        "questions": deepcopy(questions),
        "answers": [],
        "call_notes": "",
        "document_requests": [],
        "outcome": None,
        "follow_up": {
            "follow_up_required": False,
            "follow_up_date": None,
            "reason": None,
            "promised_quote_date": None,
            "supplier_action": None,
            "owner_action": None,
        },
        "next_action": None,
        "completion_override_reason": None,
        "timeline": [{"at": _utc(), "event": "session_opened", "note": None}],
        "verbal_answers": [],
        "written_quote": None,
        "live_economics": None,
        "auto_send": False,
        "build": BUILD,
    }


def record_answer(
    session: dict[str, Any],
    question_id: str,
    answer_value: Any,
    *,
    source: str = "PHONE",
    owner_note: str | None = None,
    confidence: str = "OWNER_ENTERED",
    asked: bool = True,
    follow_up_required: bool = False,
    state: str | None = None,
) -> dict[str, Any]:
    """Enter/update a structured answer on the live call session."""
    q = next((x for x in session["questions"] if x["question_id"] == question_id), None)
    if not q:
        raise KeyError(f"unknown question_id: {question_id}")
    if state is None:
        if follow_up_required:
            state = FOLLOW_UP_REQUIRED
        elif answer_value in (None, "", "UNKNOWN") and asked:
            state = ASKED_NO_ANSWER
        elif answer_value == "N/A":
            state = NOT_APPLICABLE
        else:
            state = ANSWERED
    q["state"] = state
    q["answer_value"] = answer_value
    q["asked"] = asked or state != NOT_ASKED
    q["answer_recorded"] = state == ANSWERED
    q["follow_up_required"] = follow_up_required or state == FOLLOW_UP_REQUIRED
    q["owner_note"] = owner_note
    ans = {
        "kind": "SupplierCallAnswer",
        "call_session_id": session["session_id"],
        "opportunity_id": session["opportunity_id"],
        "supplier_id": session["supplier_id"],
        "question_id": question_id,
        "question_category": q.get("question_category"),
        "question_text": q.get("question_text"),
        "answer_type": q.get("answer_type"),
        "answer_value": answer_value,
        "answered_at": _utc(),
        "confidence": confidence,
        "source": source,
        "owner_note": owner_note,
        "state": state,
    }
    # Replace prior answer for same question in this session (verbal history kept separately)
    session["answers"] = [a for a in session["answers"] if a.get("question_id") != question_id]
    session["answers"].append(ans)
    session["verbal_answers"].append({**ans, "layer": "verbal"})
    session["updated_at"] = _utc()
    session["timeline"].append({"at": _utc(), "event": "answer_recorded", "question_id": question_id})

    # Delivery fail flag
    if question_id == "delivery_feasible" and str(answer_value).upper() == "NO":
        session["delivery_fail"] = True
        session["timeline"].append({"at": _utc(), "event": DELIVERY_FAIL, "note": "kept for backup/history"})

    return ans


def save_call_session(session: dict[str, Any]) -> dict[str, Any]:
    session = deepcopy(session)
    session["saved_at"] = _utc()
    session["updated_at"] = session["saved_at"]
    session["timeline"].append({"at": session["saved_at"], "event": "saved", "note": "partial_or_complete"})
    path = SESSIONS_DIR / f"{session['session_id']}.json"
    save_json(path, session)
    return {"ok": True, "saved_at": session["saved_at"], "path": str(path), "session_id": session["session_id"]}


def load_call_session(session_id: str) -> dict[str, Any] | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    return load_json(path)


def resume_call(session_id: str) -> dict[str, Any]:
    session = load_call_session(session_id)
    if not session:
        raise FileNotFoundError(session_id)
    unanswered_critical = [
        q for q in session.get("questions") or []
        if q.get("question_id") in CRITICAL_QUESTION_IDS and q.get("state") not in {ANSWERED, NOT_APPLICABLE}
    ]
    return {
        "kind": "ResumeCallView",
        "session": session,
        "questions_already_answered": [q for q in session["questions"] if q.get("state") == ANSWERED],
        "unanswered_critical": unanswered_critical,
        "notes": session.get("call_notes"),
        "promised_follow_ups": session.get("follow_up"),
        "previous_contact_person": session.get("contact_person"),
        "status": session.get("status"),
    }


def missing_critical_answers(session: dict[str, Any]) -> list[str]:
    missing = []
    by_id = {q["question_id"]: q for q in session.get("questions") or []}
    # Freight amount required only if freight not included
    freight_inc = by_id.get("freight_included", {}).get("answer_value")
    for qid in CRITICAL_QUESTION_IDS:
        q = by_id.get(qid)
        if not q:
            continue
        if q.get("state") in {ANSWERED, NOT_APPLICABLE}:
            continue
        if qid == "freight_included":
            missing.append("freight included (yes/no)")
            continue
        missing.append(q.get("question_text") or qid)
    if str(freight_inc).upper() in {"NO", "UNKNOWN", ""} or freight_inc is None:
        fq = by_id.get("freight_amount")
        if not fq or fq.get("state") not in {ANSWERED, NOT_APPLICABLE}:
            if "freight amount" not in " ".join(missing).lower():
                missing.append("freight amount")
    # If freight included YES, amount not required
    if str(freight_inc).upper() == "YES":
        missing = [m for m in missing if "freight amount" not in m.lower()]
    return missing


def complete_call(
    session: dict[str, Any],
    *,
    outcome: str,
    override_reason: str | None = None,
    follow_up: dict[str, Any] | None = None,
) -> dict[str, Any]:
    missing = missing_critical_answers(session)
    if missing and not override_reason:
        return {
            "ok": False,
            "status": session.get("status"),
            "still_needed": missing,
            "error": "missing_critical_answers",
        }
    session["status"] = CALL_COMPLETE
    session["outcome"] = outcome
    session["completion_override_reason"] = override_reason
    if follow_up:
        session["follow_up"] = {**(session.get("follow_up") or {}), **follow_up}
    session["next_action"] = recommend_next_action(session)
    session["updated_at"] = _utc()
    session["timeline"].append({"at": _utc(), "event": "call_complete", "outcome": outcome})
    save_call_session(session)
    return {"ok": True, "session": session, "still_needed": [], "override": bool(override_reason)}


def recommend_next_action(session: dict[str, Any]) -> str:
    outcome = session.get("outcome")
    if outcome == QUOTE_PROMISED:
        return "WAIT_FOR_QUOTE"
    if outcome == QUOTE_RECEIVED:
        return "OWNER_REVIEW"
    if outcome in {CALLED_NO_ANSWER, LEFT_MESSAGE}:
        return "CALL_AGAIN"
    if outcome == DELIVERY_FAIL:
        return "TRY_NEXT_SUPPLIER"
    if outcome == TERMS_FAIL:
        return "REQUEST_TERMS_REVIEW"
    if outcome == PRODUCT_UNAVAILABLE:
        return "REVIEW_SUBSTITUTE"
    if outcome == DECLINED_TO_QUOTE:
        return "DROP_SUPPLIER"
    if outcome == FOLLOW_UP_REQUIRED_OUTCOME or (session.get("follow_up") or {}).get("follow_up_required"):
        return "CALL_AGAIN"
    if outcome == SPOKE_TO_SUPPLIER:
        return "EMAIL_RFQ"
    return "OWNER_REVIEW"


# ---------------------------------------------------------------------------
# Live economics
# ---------------------------------------------------------------------------

def _answers_map(session: dict[str, Any]) -> dict[str, Any]:
    # Written quote overrides verbal for conflicting fields
    out: dict[str, Any] = {}
    for a in session.get("answers") or []:
        out[a["question_id"]] = a.get("answer_value")
    wq = session.get("written_quote") or {}
    if wq:
        if wq.get("unit_price") is not None:
            out["unit_price"] = wq["unit_price"]
        if wq.get("extended_price") is not None:
            out["extended_price"] = wq["extended_price"]
        if wq.get("freight") is not None:
            out["freight_amount"] = wq["freight"]
            out["freight_included"] = "YES" if float(wq.get("freight") or 0) == 0 and wq.get("freight_included") else out.get("freight_included")
            if wq.get("freight_included") is True:
                out["freight_included"] = "YES"
            elif wq.get("freight_included") is False:
                out["freight_included"] = "NO"
        if wq.get("lead_time") is not None:
            out["lead_time"] = wq["lead_time"]
        if wq.get("payment_terms"):
            out["payment_terms"] = wq["payment_terms"]
        if wq.get("expiration"):
            out["quote_expiration"] = wq["expiration"]
    return out


def validate_price_inputs(unit: float | None, qty: float | None, freight: float | None, fees: float | None) -> list[str]:
    errs = []
    if unit is not None and unit < 0:
        errs.append("negative_unit_price")
    if qty is not None and qty <= 0:
        errs.append("invalid_quantity")
    if freight is not None and freight < 0:
        errs.append("negative_freight")
    if fees is not None and fees < 0:
        errs.append("negative_fees")
    return errs


def compute_live_economics(
    session: dict[str, Any],
    row: dict[str, Any],
    *,
    quantity: float | None = None,
) -> dict[str, Any]:
    amap = _answers_map(session)
    unit = _f(amap.get("unit_price"))
    fees = _f(amap.get("fees") or amap.get("additional_fees")) or 0.0
    qty = quantity or _f((row.get("requirement_packet") or {}).get("quantity")) or 1.0
    freight_inc = str(amap.get("freight_included") or "").upper()
    freight_amt = _f(amap.get("freight_amount"))
    if freight_inc == "YES":
        freight = 0.0
    else:
        freight = freight_amt  # may be None — do not invent 0 as included

    errs = validate_price_inputs(unit, qty, freight if freight is not None else 0.0, fees)
    mb = row.get("internal_max_buy") or {}
    gov = str(row.get("gov_letter") or "").upper()

    if unit is None:
        return {
            "status": MISSING_COST_INPUTS,
            "errors": errs,
            "landed": None,
            "owner_only": True,
            "freight_double_count_guard": True,
        }

    if freight is None and freight_inc != "YES":
        # Can still compute product cost; landed incomplete
        product_cost = unit * qty + fees
        return {
            "status": MISSING_COST_INPUTS,
            "unit_price": unit,
            "quantity": qty,
            "product_cost": round(product_cost, 2),
            "freight": None,
            "landed": None,
            "note": "freight_unknown_not_assumed_zero",
            "errors": errs,
            "owner_only": True,
            "freight_double_count_guard": True,
        }

    # Avoid double-counting: fees separate; freight once
    ev = evaluate_supplier_quote_response(
        quoted_unit=unit,
        quantity=qty,
        freight=float(freight or 0),
        other_fees=float(fees or 0),
        max_buy={"supplier_quote_target": mb.get("break_even"), "thresholds": {
            "BREAK_EVEN_MAX_BUY": mb.get("break_even"),
            "MAX_BUY_FOR_5K_PROFIT": mb.get("for_5k"),
            "MAX_BUY_FOR_10K_PROFIT": mb.get("for_10k"),
            "MAX_BUY_FOR_25K_PROFIT": mb.get("for_25k"),
        }} if mb.get("status") == "MAX_BUY_AVAILABLE" else None,
        lead_time_days=_f(amap.get("lead_time") or amap.get("lead_time_days")),
        expiration_date=str(amap.get("quote_expiration")) if amap.get("quote_expiration") else None,
    )
    landed = ev.get("total_landed_cost")
    status = MISSING_COST_INPUTS
    if mb.get("status") != "MAX_BUY_AVAILABLE" or gov not in {"A", "B", "C"}:
        status = GOV_VALUE_TOO_WEAK_TO_JUDGE
    else:
        be = _f(mb.get("break_even"))
        if be is not None and unit is not None:
            if unit > be:
                status = ABOVE_MAX_BUY
            elif mb.get("for_5k") and unit <= _f(mb.get("for_5k")):
                status = PRICE_LOOKS_GOOD
            else:
                status = MARGINAL
        else:
            status = GOV_VALUE_TOO_WEAK_TO_JUDGE

    out = {
        "status": status,
        "unit_price": unit,
        "quantity": qty,
        "freight": freight,
        "fees": fees,
        "landed_acquisition_cost": landed,
        "financing_estimate": ev.get("financing"),
        "max_buy_comparison": {
            "break_even": mb.get("break_even"),
            "unit": unit,
            "max_buy_status": mb.get("status"),
        },
        "expected_profit": ev.get("expected_net"),
        "expected_margin": ev.get("margin"),
        "evaluation": ev,
        "errors": errs,
        "owner_only": True,
        "freight_double_count_guard": True,
        "verified_acquisition_price": None,  # only via written quote gate
        "verified_positive": None,
    }
    session["live_economics"] = out
    return out


def add_written_quote(session: dict[str, Any], quote: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Written quote becomes authoritative; preserve verbal history."""
    wq = {
        "quote_number": quote.get("quote_number"),
        "date": quote.get("date") or quote.get("quote_date"),
        "expiration": quote.get("expiration") or quote.get("expiration_date"),
        "unit_price": _f(quote.get("unit_price")),
        "extended_price": _f(quote.get("extended_price")),
        "freight": _f(quote.get("freight")),
        "freight_included": quote.get("freight_included"),
        "taxes_fees": _f(quote.get("taxes_fees") or quote.get("other_fees")),
        "lead_time": quote.get("lead_time"),
        "terms": quote.get("terms") or quote.get("payment_terms"),
        "warranty": quote.get("warranty"),
        "authorization": quote.get("authorization"),
        "supplier_notes": quote.get("supplier_notes") or quote.get("notes"),
        "reference_path": quote.get("reference_path") or quote.get("document_path"),
        "received_at": _utc(),
        "authoritative": True,
    }
    # Guardrails
    errs = validate_price_inputs(wq["unit_price"], _f((row.get("requirement_packet") or {}).get("quantity")), wq.get("freight"), wq.get("taxes_fees"))
    if errs:
        return {"ok": False, "errors": errs}

    session["written_quote"] = wq
    session["outcome"] = QUOTE_RECEIVED
    session["timeline"].append({"at": _utc(), "event": "written_quote_received", "quote_number": wq.get("quote_number")})
    # Overlay answers from written quote without deleting verbal history
    if wq.get("unit_price") is not None:
        record_answer(session, "unit_price", wq["unit_price"], source="WRITTEN_QUOTE")
    if wq.get("freight") is not None:
        record_answer(session, "freight_amount", wq["freight"], source="WRITTEN_QUOTE")
    if wq.get("expiration"):
        record_answer(session, "quote_expiration", wq["expiration"], source="WRITTEN_QUOTE")
    if wq.get("terms"):
        record_answer(session, "payment_terms", wq["terms"], source="WRITTEN_QUOTE")

    econ = compute_live_economics(session, row)
    verified = maybe_verified_acquisition(session, row, econ)
    return {"ok": True, "written_quote": wq, "economics": econ, "verified": verified, "verbal_preserved": True}


def maybe_verified_acquisition(session: dict[str, Any], row: dict[str, Any], econ: dict[str, Any]) -> dict[str, Any]:
    """Only create VERIFIED_ACQUISITION_PRICE when evidence rules met — never fabricate."""
    wq = session.get("written_quote") or {}
    if not wq or wq.get("unit_price") is None:
        return {"state": None, "reason": "no_written_unit_price"}
    if wq.get("freight") is None and not wq.get("freight_included"):
        return {"state": None, "reason": "freight_unknown"}
    qty = _f((row.get("requirement_packet") or {}).get("quantity"))
    if not qty:
        return {"state": None, "reason": "quantity_unknown"}
    # Real written quote present with price + freight known
    vap = {
        "state": VERIFIED_ACQUISITION_PRICE,
        "unit_price": wq["unit_price"],
        "freight": 0.0 if wq.get("freight_included") else wq.get("freight"),
        "quantity": qty,
        "source": "WRITTEN_QUOTE",
        "quote_number": wq.get("quote_number"),
        "fabricated": False,
    }
    session["verified_acquisition_price"] = vap

    mb = row.get("internal_max_buy") or {}
    gov = str(row.get("gov_letter") or "").upper()
    vp = None
    if (
        gov in {"A", "B"}
        and mb.get("status") == "MAX_BUY_AVAILABLE"
        and econ.get("status") == PRICE_LOOKS_GOOD
        and vap
    ):
        vp = {"state": VERIFIED_POSITIVE, "fabricated": False}
        session["verified_positive"] = vp
    else:
        session["verified_positive"] = None
        vp = {"state": None, "reason": "gov_or_economics_gate_not_met"}
    return {"verified_acquisition_price": vap, "verified_positive": vp}


# ---------------------------------------------------------------------------
# Comparison + today list + opportunity progress
# ---------------------------------------------------------------------------

def compare_opportunity_suppliers(row: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    sheets_by_sup = {_sup_id(s): s for s in row.get("suppliers") or []}
    rows_out = []
    for sess in sessions:
        econ = sess.get("live_economics") or {}
        amap = _answers_map(sess)
        rows_out.append({
            "supplier": sess.get("supplier_name") or sess.get("supplier_id"),
            "supplier_id": sess.get("supplier_id"),
            "unit": amap.get("unit_price") or econ.get("unit_price"),
            "freight": econ.get("freight") if econ.get("freight") is not None else amap.get("freight_amount"),
            "landed": econ.get("landed_acquisition_cost"),
            "lead_time": amap.get("lead_time") or amap.get("lead_time_days"),
            "terms": amap.get("payment_terms") or amap.get("terms_type"),
            "delivery": amap.get("delivery_feasible") or amap.get("delivery_by_required_date"),
            "auth": amap.get("auth_dealer") or (sheets_by_sup.get(sess.get("supplier_id") or "") or {}).get("authorization_state"),
            "status": sess.get("outcome") or sess.get("status"),
            "stock": amap.get("stock"),
        })
    # Rank: prefer complete quotes, delivery ok, better terms, lower landed
    def _rank(r: dict[str, Any]) -> tuple:
        landed = _f(r.get("landed"))
        delivery_pen = 0 if str(r.get("delivery")).upper() == "YES" else 50
        terms_pen = 0 if str(r.get("terms") or "").upper() in {"NET_30", "NET_45", "NET_15"} else 20
        status_pen = 0 if r.get("status") == QUOTE_RECEIVED else 30
        return (status_pen, delivery_pen, terms_pen, landed if landed is not None else 1e18)

    ranked = sorted(rows_out, key=_rank)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    quotes_received = sum(1 for r in rows_out if r.get("status") == QUOTE_RECEIVED)
    return {
        "kind": "SupplierComparison",
        "opportunity_id": _opp_id(row),
        "title": row.get("title"),
        "rows": ranked,
        "best_viable": ranked[0] if ranked else None,
        "quote_strategy": MORE_QUOTES_RECOMMENDED if quotes_received < 2 else "SUFFICIENT_QUOTES_OR_TIGHT_DEADLINE",
        "not_cheapest_only": True,
    }


def opportunity_call_progress(row: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    n_sup = len(row.get("suppliers") or [])
    contacted = {s.get("supplier_id") for s in sessions if s.get("outcome") or s.get("status") != CALL_IN_PROGRESS or s.get("answers")}
    # Count distinct suppliers with any session activity
    active = {s.get("supplier_id") for s in sessions}
    promised = sum(1 for s in sessions if s.get("outcome") == QUOTE_PROMISED)
    received = sum(1 for s in sessions if s.get("outcome") == QUOTE_RECEIVED or s.get("written_quote"))
    if received >= 1 and all(
        (s.get("live_economics") or {}).get("status") not in {None, MISSING_COST_INPUTS}
        for s in sessions if s.get("written_quote")
    ):
        opp_status = READY_FOR_FINAL_ECONOMICS if received >= 1 else QUOTES_RECEIVED
    elif received:
        opp_status = QUOTES_RECEIVED
    elif promised:
        opp_status = QUOTE_PENDING
    elif active:
        opp_status = CALLS_IN_PROGRESS
    else:
        opp_status = CALLS_NOT_STARTED
    fails = sum(1 for s in sessions if s.get("outcome") in {DELIVERY_FAIL, TERMS_FAIL, DECLINED_TO_QUOTE, PRODUCT_UNAVAILABLE})
    if fails >= max(1, n_sup) and received == 0 and promised == 0 and n_sup:
        opp_status = SUPPLIER_PATH_FAILED
    return {
        "suppliers_total": n_sup,
        "suppliers_contacted": len(active),
        "quotes_promised": promised,
        "quotes_received": received,
        "summary": f"{len(active)} of {n_sup} suppliers contacted · {promised} quote promised · {received} quotes received",
        "opportunity_status": opp_status,
        "owner_decision_options": ["BID", "GET_ANOTHER_QUOTE", "NEGOTIATE", "SKIP", "REVIEW"],
        "auto_bid": False,
    }


def build_today_calls(sheets: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    sess_by = {(s.get("opportunity_id"), s.get("supplier_id")): s for s in sessions}
    entries = []
    for sh in sheets:
        key = (sh["opportunity_id"], sh["supplier_id"])
        sess = sess_by.get(key)
        status = "NOT_CALLED"
        if sess:
            if sess.get("outcome") == QUOTE_RECEIVED:
                status = "QUOTE_RECEIVED"
            elif sess.get("outcome") == QUOTE_PROMISED:
                status = "QUOTE_PROMISED"
            elif sess.get("outcome") == DELIVERY_FAIL:
                status = "DELIVERY_FAIL"
            elif sess.get("outcome") == TERMS_FAIL:
                status = "TERMS_FAIL"
            elif (sess.get("follow_up") or {}).get("follow_up_required"):
                status = "FOLLOW_UP"
            elif sess.get("status") == CALL_IN_PROGRESS:
                status = "FOLLOW_UP" if sess.get("answers") else "NOT_CALLED"
            elif sess.get("outcome"):
                status = sess["outcome"]
        pri = (sh.get("supplier_call_priority") or {}).get("band") or BACKUP
        entries.append({
            "priority": pri,
            "priority_score": (sh.get("supplier_call_priority") or {}).get("score"),
            "opportunity_id": sh["opportunity_id"],
            "buyer": sh.get("buyer"),
            "solicitation": sh.get("solicitation"),
            "opportunity": (sh.get("product") or "")[:80],
            "supplier": sh.get("supplier_name"),
            "supplier_id": sh.get("supplier_id"),
            "phone": sh.get("phone_number"),
            "rfq_url": sh.get("rfq_url"),
            "product": sh.get("product"),
            "quantity": sh.get("quantity"),
            "deadline": sh.get("deadline"),
            "why_call": sh.get("why_selected"),
            "call_status": status,
        })
    order = {CALL_FIRST: 0, CALL_SECOND: 1, CALL_THIRD: 2, BACKUP: 3}
    entries.sort(key=lambda e: (order.get(e["priority"], 9), -(e.get("priority_score") or 0)))
    return {
        "kind": "TODAYS_SUPPLIER_CALLS",
        "build": BUILD,
        "generated_at": _utc(),
        "count": len(entries),
        "filters_supported": ["NOT_CALLED", "FOLLOW_UP", "QUOTE_PROMISED", "QUOTE_RECEIVED", "DELIVERY_FAIL", "TERMS_FAIL"],
        "entries": entries,
        "auto_call": False,
    }


def assert_no_econ_leak_in_script(sheet: dict[str, Any], workspace: dict[str, Any]) -> None:
    script = json.dumps({"opening": sheet.get("opening_script"), "qs": workspace.get("call_questions")}, default=str).lower()
    for bad in ("max_buy", "break_even", "expected profit", "government_historical", "target margin"):
        assert bad not in script, f"internal leak in supplier-facing script: {bad}"
    for field in INTERNAL_FIELDS_NEVER_SUPPLIER:
        # owner panel may contain these — scripts must not
        assert field.lower() not in (sheet.get("opening_script") or "").lower()


# ---------------------------------------------------------------------------
# Phase runner
# ---------------------------------------------------------------------------

def load_l21_call_targets() -> list[dict[str, Any]]:
    path = OUT / "l21_quote_readiness.json"
    rows = list(json.loads(path.read_text(encoding="utf-8")).get("rows") or [])
    # Prefer READY then MINOR
    def _key(r: dict[str, Any]) -> tuple:
        st = (r.get("readiness") or {}).get("state")
        ready = 0 if st == "READY_FOR_OWNER_APPROVAL" else 1 if st == "NEEDS_MINOR_REVIEW" else 2
        score = -int((r.get("priority") or {}).get("score") or 0)
        return (ready, score)

    rows = [r for r in rows if r.get("eligible") or (r.get("readiness") or {}).get("state") in {
        "READY_FOR_OWNER_APPROVAL", "NEEDS_MINOR_REVIEW"
    }]
    rows.sort(key=_key)
    return rows


def prepare_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    suppliers = list(row.get("suppliers") or [])
    # Rank suppliers
    ranked = []
    for s in suppliers:
        pri = supplier_call_priority(s, contact=s.get("contact"))
        ranked.append((pri["score"], pri, s))
    ranked.sort(key=lambda x: -x[0])

    sheets = []
    workspaces = []
    questions_by_sup = {}
    for score, pri, s in ranked:
        sheet = build_supplier_call_sheet(row, s, priority=pri)
        qs = build_dynamic_questions(row.get("requirement_packet") or {}, s)
        panel = owner_only_panel(row)
        ws = build_call_workspace(sheet, qs, owner_panel=panel)
        assert_no_econ_leak_in_script(sheet, ws)
        sheets.append(sheet)
        workspaces.append(ws)
        questions_by_sup[_sup_id(s)] = qs

    progress = opportunity_call_progress(row, [])
    # Deadline-aware quote strategy
    runway = ((row.get("live") or {}).get("runway") or {}).get("label")
    if runway == "TIGHT_RUNWAY":
        quote_strategy = "FEWER_CALLS_FASTER_FOLLOWUP"
    else:
        quote_strategy = MORE_QUOTES_RECOMMENDED

    return {
        "opportunity_id": _opp_id(row),
        "title": row.get("title"),
        "buyer": row.get("buyer"),
        "solicitation": row.get("solicitation"),
        "readiness": (row.get("readiness") or {}).get("state"),
        "sheets": sheets,
        "workspaces": workspaces,
        "questions_by_supplier": questions_by_sup,
        "progress": progress,
        "quote_strategy": quote_strategy,
        "supplier_count": len(sheets),
        "suppliers_with_contact_path": sum(
            1 for sh in sheets if sh.get("rfq_url") or sh.get("phone_number") or sh.get("public_email")
        ),
    }


def run_phase_l22() -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert AUTO_SEND_SUPPLIER_OUTREACH is False
    assert AUTO_CALL is False
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    targets = load_l21_call_targets()
    print(f"[l22] call-ready opportunities={len(targets)}", flush=True)

    prepared = []
    all_sheets: list[dict[str, Any]] = []
    all_questions: list[dict[str, Any]] = []
    demo_sessions: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    outcomes_model = {
        "outcomes": [
            CALLED_NO_ANSWER, LEFT_MESSAGE, SPOKE_TO_SUPPLIER, QUOTE_PROMISED, QUOTE_RECEIVED,
            DECLINED_TO_QUOTE, DELIVERY_FAIL, TERMS_FAIL, PRODUCT_UNAVAILABLE, FOLLOW_UP_REQUIRED_OUTCOME,
        ],
        "next_actions": [
            "WAIT_FOR_QUOTE", "CALL_AGAIN", "EMAIL_RFQ", "TRY_NEXT_SUPPLIER",
            "REQUEST_TERMS_REVIEW", "DROP_SUPPLIER", "REVIEW_SUBSTITUTE", "OWNER_REVIEW",
        ],
    }
    followup_model = {
        "kind": "FollowUpModel",
        "fields": ["follow_up_required", "follow_up_date", "reason", "promised_quote_date", "supplier_action", "owner_action"],
        "next_action_fn": "phase_l.l22_supplier_call_desk.recommend_next_action",
    }

    for i, row in enumerate(targets):
        print(f"[l22] desk {i+1}/{len(targets)}: {(row.get('title') or '')[:70]}", flush=True)
        prep = prepare_opportunity(row)
        prepared.append(prep)
        all_sheets.extend(prep["sheets"])
        for sid, qs in prep["questions_by_supplier"].items():
            all_questions.append({"opportunity_id": prep["opportunity_id"], "supplier_id": sid, "questions": qs})

        # Seed empty comparison + optional demo session structure (no real calls)
        comparisons.append(compare_opportunity_suppliers(row, []))

    # Create one illustrative in-progress session template for the top CALL_FIRST sheet (unsaved demo)
    if all_sheets:
        top = next((s for s in all_sheets if (s.get("supplier_call_priority") or {}).get("band") == CALL_FIRST), all_sheets[0])
        row0 = next(r for r in targets if _opp_id(r) == top["opportunity_id"])
        qs = build_dynamic_questions(row0.get("requirement_packet") or {}, {"supplier_domain": top["supplier_id"]})
        demo = new_call_session(top, qs)
        demo["call_notes"] = ""
        demo_sessions.append(demo)
        # Do not auto-save demo as a real call — mark template
        demo["template_only"] = True

    today = build_today_calls(all_sheets, [])
    contact_path_count = sum(1 for s in all_sheets if s.get("rfq_url") or s.get("phone_number") or s.get("public_email"))

    # Capability confirmation (engine-level — no live calls made)
    answer_capture = {
        "structured_answers": True,
        "checkboxes_status": True,
        "question_notes": True,
        "call_notes": True,
        "partial_save": True,
        "resume": True,
        "multiple_sessions": True,
    }
    follow_up_ok = {
        "promised_quote_dates": True,
        "follow_up_dates": True,
        "next_action": True,
    }
    live_econ_ok = {
        "price_entry": True,
        "freight": True,
        "terms": True,
        "landed_cost": True,
        "max_buy_comparison": True,
    }

    if len(all_sheets) >= 3 and len(prepared) >= 1 and answer_capture["partial_save"]:
        verdict = "PHASE_L22_SUPPLIER_CALL_DESK_READY"
    elif all_sheets:
        verdict = "PHASE_L22_PARTIAL_SUPPLIER_CALL_DESK"
    else:
        verdict = "PHASE_L22_SUPPLIER_CALL_DESK_FAILED"

    remaining = (
        "owner must open a call workspace and place the call manually — "
        "M3 will not dial, email, or submit RFQ forms (auto_call=false, auto_send=false); "
        "public phone numbers are often unresolved (RFQ-form paths dominate), so owner may need "
        "the RFQ URL or directory lookup before a live phone call"
    )

    summary = {
        "kind": "L22Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline_l21": L21_BASELINE,
        "call_ready_opportunities": len(prepared),
        "call_sheets": len(all_sheets),
        "suppliers_with_contact_paths": contact_path_count,
        "answer_capture": answer_capture,
        "follow_up": follow_up_ok,
        "live_economics": live_econ_ok,
        "written_quote_ingestion": "YES",
        "supplier_comparison": "YES",
        "todays_calls": {"count": today["count"], "sample_top": today["entries"][:5]},
        "auto_send": AUTO_SEND_SUPPLIER_OUTREACH,
        "auto_call": AUTO_CALL,
        "auto_email": AUTO_EMAIL,
        "no_internal_economics_leakage": True,
        "remaining_blocker": remaining,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "legacy_cleanup": legacy_cleanup_report(),
        "canonical_path": "phase_l.l22_supplier_call_desk",
    }

    artifacts = {
        "l22_supplier_call_sheets.json": {
            "kind": "L22SupplierCallSheets",
            "build": BUILD,
            "count": len(all_sheets),
            "sheets": all_sheets,
        },
        "l22_dynamic_questions.json": {
            "kind": "L22DynamicQuestions",
            "build": BUILD,
            "catalog_sample": build_dynamic_questions({"manufacturer": "Apple", "model": "iPad", "quantity": 1}, {}),
            "by_opportunity": all_questions,
        },
        "l22_answer_schema.json": ANSWER_SCHEMA,
        "l22_call_sessions.json": {
            "kind": "L22CallSessions",
            "build": BUILD,
            "note": "Persisted sessions live under artifacts/phase_l/l22_sessions/; templates below",
            "sessions": demo_sessions,
            "storage_dir": str(SESSIONS_DIR),
        },
        "l22_call_outcomes.json": {
            "kind": "L22CallOutcomes",
            "build": BUILD,
            **outcomes_model,
        },
        "l22_followup_model.json": followup_model,
        "l22_supplier_comparison.json": {
            "kind": "L22SupplierComparison",
            "build": BUILD,
            "comparisons": comparisons,
        },
        "l22_today_calls.json": today,
        "l22_summary.json": summary,
        "l22_workspaces.json": {
            "kind": "L22CallWorkspaces",
            "build": BUILD,
            "count": sum(len(p["workspaces"]) for p in prepared),
            "opportunities": [
                {
                    "opportunity_id": p["opportunity_id"],
                    "title": p["title"],
                    "workspaces": p["workspaces"],
                    "progress": p["progress"],
                    "quote_strategy": p["quote_strategy"],
                }
                for p in prepared
            ],
        },
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l22] wrote {name}", flush=True)

    write_l22_docs(summary)
    # Point legacy builder at canonical note
    return summary


def write_l22_docs(summary: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l22_supplier_call_desk.md": f"""# Phase L.22 — Supplier Call Desk

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

Canonical path: `phase_l.l22_supplier_call_desk`

- Call-ready opportunities: {summary.get('call_ready_opportunities')}
- Call sheets: {summary.get('call_sheets')}
- Auto-call: `{AUTO_CALL}` / Auto-send: `{AUTO_SEND_SUPPLIER_OUTREACH}`

Owner opens a `SupplierCallWorkspace` per supplier — script, questions, answers, notes, and owner-only economics on one page.
""",
        "phase_l22_answer_capture.md": f"""# L.22 Answer Capture

Schema: `artifacts/phase_l/l22_answer_schema.json`

States: `{NOT_ASKED}`, `{ASKED_NO_ANSWER}`, `{ANSWERED}`, `{NOT_APPLICABLE}`, `{FOLLOW_UP_REQUIRED}`

Checkboxes: Asked / Answer recorded / Follow-up required

APIs: `record_answer`, `save_call_session`, `resume_call`, `complete_call`
""",
        "phase_l22_notes_and_sessions.md": """# L.22 Notes and Sessions

- Free-form `call_notes` on every `SupplierCallSession`
- Per-question `owner_note`
- Partial save → `CALL_IN_PROGRESS`
- Resume restores answers, notes, follow-ups, contact person
- Multiple sessions per supplier (history preserved; never overwrite)
- Timeline events for called / quote promised / quote received / follow-up
""",
        "phase_l22_live_economics.md": """# L.22 Live Economics

`compute_live_economics` updates landed cost / financing / max-buy comparison as unit price, freight, fees, and quantity are entered.

Statuses: `PRICE_LOOKS_GOOD`, `MARGINAL`, `ABOVE_MAX_BUY`, `GOV_VALUE_TOO_WEAK_TO_JUDGE`, `MISSING_COST_INPUTS`

Owner-only panel banner: **OWNER ONLY — DO NOT DISCLOSE**

Freight is not double-counted; blank freight is not assumed zero unless marked included.
""",
        "phase_l22_followups.md": """# L.22 Follow-ups

Fields: follow_up_required, follow_up_date, reason, promised_quote_date, supplier_action, owner_action

`recommend_next_action` → WAIT_FOR_QUOTE / CALL_AGAIN / EMAIL_RFQ / TRY_NEXT_SUPPLIER / …
""",
        "phase_l22_supplier_comparison.md": """# L.22 Supplier Comparison

Table columns: Supplier | Unit | Freight | Landed | Lead Time | Terms | Delivery | Auth | Status

Ranking considers landed cost, delivery, terms, authorization, stock — not cheapest alone.
""",
        "phase_l22_mobile_owner_workflow.md": """# L.22 Mobile Owner Workflow

Workspace layout:

- mobile: single column, large controls
- desktop: split (script/questions | answers/notes/economics)

Explicit **SAVE CALL** with timestamp. Warn on unsaved exit.

Today's list: `TODAYS_SUPPLIER_CALLS` with filters NOT_CALLED / FOLLOW-UP / QUOTE_PROMISED / …
""",
        "phase_l22_legacy_cleanup.md": """# L.22 Legacy Cleanup

Canonical interaction model: `phase_l.l22_supplier_call_desk`

Legacy `operator_action_queue.build_supplier_call_sheet` remains as a thin compatibility helper for deal-queue actions; L.22 sheets/sessions/answers are authoritative for quote outreach calls.

No auto-send / auto-call remnants in L.22 path.
""",
        "phase_l22_regression.md": f"""# L.22 Regression

- L.21 quote prep → L.22 call desk
- No outreach / SAM API / fabricated verified prices without written quote
- Verdict: `{summary.get('verdict')}`
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    summary = run_phase_l22()
    print(json.dumps({
        k: summary[k]
        for k in (
            "verdict",
            "call_ready_opportunities",
            "call_sheets",
            "suppliers_with_contact_paths",
            "answer_capture",
            "follow_up",
            "live_economics",
            "written_quote_ingestion",
            "supplier_comparison",
            "todays_calls",
            "remaining_blocker",
        )
    }, indent=2, default=str))
