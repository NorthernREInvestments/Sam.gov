"""REAL TINY — end-to-end launch validation pipeline (discovery → operator queue).

No SAM, OpenAI, USAspending, paid APIs, or lender outreach.
"""

from __future__ import annotations
from application_clock import now_utc

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_integrity import parse_money
from discovery.classify import classify_discovery_opportunity, early_reject_reasons
from discovery.deadline import normalize_deadline
from discovery.deadline_viability import (
    VIABILITY_TOO_LATE,
    VIABILITY_UNKNOWN,
    enrich_opportunity_deadline,
    may_enter_normal_pursuit_queue,
    sort_operator_queue,
)
from discovery.dedup import strong_canonical_key
from discovery.opportunity_gate import is_structurally_valid_opportunity
from economic_integrity import min_actual_profit_usd
from funding_path_constants import (
    CONF_NO_KNOWN_PATH,
    CONF_NEEDS_LENDER_VERIFICATION,
    CONF_PRELIMINARY_MATCH,
    CONF_UNASSESSED,
    MATCH_MATCH,
    MATCH_NEEDS_VERIFICATION,
    MATCH_REJECT,
)
from funding_path_intelligence import (
    assess_deal_funding_confidence,
    assess_government_contract_financing,
    build_funding_requirement,
    build_operator_call_sheet,
    match_funding_source,
)
from funding_source_kb import production_knowledge_base_seed

# --- Executability ---
EXEC_PASS = "PASS"
EXEC_POSSIBLE = "POSSIBLE"
EXEC_NEEDS_REVIEW = "NEEDS_REVIEW"
EXEC_HARD_FAIL = "HARD_FAIL"
EXEC_UNKNOWN = "UNKNOWN"

# --- Preliminary economics ---
ECON_PROMISING = "ECONOMICS_PROMISING"
ECON_POSSIBLE = "ECONOMICS_POSSIBLE"
ECON_UNKNOWN = "ECONOMICS_UNKNOWN"
ECON_UNLIKELY = "ECONOMICS_UNLIKELY"
ECON_FAIL = "ECONOMICS_FAIL"

# --- Profit status ---
PROFIT_CONFIRMED_ABOVE = "PROFIT_CONFIRMED_ABOVE_TARGET"
PROFIT_ESTIMATED_ABOVE = "PROFIT_ESTIMATED_ABOVE_TARGET"
PROFIT_POTENTIALLY_ABOVE = "PROFIT_POTENTIALLY_ABOVE_TARGET"
PROFIT_UNKNOWN = "PROFIT_UNKNOWN"
PROFIT_BELOW = "PROFIT_BELOW_TARGET"

# --- Primary next actions ---
ACTION_REVIEW_SOLICITATION = "REVIEW_SOLICITATION_DOCUMENTS"
ACTION_VERIFY_DEADLINE = "VERIFY_DEADLINE"
ACTION_IDENTIFY_PRODUCT = "IDENTIFY_EXACT_PRODUCT"
ACTION_GET_SUPPLIER_QUOTE = "GET_SUPPLIER_QUOTE"
ACTION_VERIFY_SUPPLIER_TERMS = "VERIFY_SUPPLIER_TERMS"
ACTION_RESEARCH_HISTORICAL = "RESEARCH_HISTORICAL_PRICING"
ACTION_VERIFY_COMPLIANCE = "VERIFY_COMPLIANCE_REQUIREMENTS"
ACTION_VERIFY_FUNDING = "VERIFY_FUNDING_WITH_LENDER"
ACTION_VERIFY_GOV_FINANCING = "VERIFY_GOVERNMENT_FINANCING"
ACTION_READY_DEEP = "READY_FOR_DEEP_RESEARCH"
ACTION_REJECT = "REJECT_OPPORTUNITY"
ACTION_WAIT = "WAIT"

PRODUCT_LAUNCH_CLASSES = frozenset({"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"})
PRODUCT_DISPLAY = {
    "CORE_PRODUCT": "CORE_PRODUCT",
    "PRODUCT_PLUS_SERVICE": "PRODUCT_PLUS_MINOR_SERVICE",
    "SERVICE": "SERVICE_HEAVY",
    "UNKNOWN": "UNKNOWN",
    "CLEARLY_IRRELEVANT": "NON_PRODUCT",
}

LAUNCH_COMPANY_FACTS: dict[str, Any] = {
    "company_is_new_entity": True,
    "is_first_government_contract": True,
    "time_in_business_years": None,
    "annual_revenue": None,
}

_HARD_EXEC_PATTERNS = (
    r"\bsecurity\s+clearance\b",
    r"\btop\s+secret\b",
    r"\bsecret\s+clearance\b",
    r"\bts/sci\b",
    r"\bprevailing\s+wage\b",
    r"\bdavis[\s-]bacon\b",
    r"\bmanufacturer[\s-]only\b",
    r"\boem[\s-]only\b",
    r"\bauthorized\s+dealer\s+only\b",
    r"\bbrand[\s-]name\s+only\b",
    r"\bsole[\s-]source\s+manufacturer\b",
    r"\bperformance[\s-]based\s+contract\b",
    r"\bconstruction[\s-]only\b",
    r"\bdesign[\s-]build\b",
    r"\bturnkey\s+construction\b",
)

_REVIEW_EXEC_PATTERNS = (
    r"\bbond(?:ing)?\s+required\b",
    r"\bperformance\s+bond\b",
    r"\bbid\s+bond\b",
    r"\binsurance\s+required\b",
    r"\blicense\s+required\b",
    r"\bcertification\s+required\b",
    r"\bset[\s-]aside\b",
    r"\b8\s*\(\s*a\s*\)\b",
    r"\bhubzone\b",
    r"\bsdvosb\b",
    r"\bwosb\b",
    r"\bsmall\s+business\s+set[\s-]aside\b",
    r"\bbuy\s+american\b",
    r"\btaa\b",
    r"\btrade\s+agreements\s+act\b",
    r"\bdirect\s+ship\s+only\b",
    r"\bsite\s+visit\s+mandatory\b",
    r"\bmandatory\s+site\s+visit\b",
    r"\bnonmanufacturer\s+rule\b",
    r"\breseller\s+not\s+permitted\b",
)

_INSTALL_PATTERNS = (
    r"\bsupply\s+and\s+install\b",
    r"\bfurnish\s+and\s+install\b",
    r"\bprovide\s+and\s+install\b",
    r"\binstallation\s+required\b",
    r"\bwith\s+installation\b",
)


def _blob(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(k) or "")
        for k in ("title", "description", "commodity_hint", "agency")
    ).lower()


def _parse_value(row: dict[str, Any]) -> float | None:
    raw = row.get("estimated_value")
    if raw is None:
        return None
    if row.get("estimated_value_status") == "UNKNOWN":
        return None
    return parse_money(str(raw))


def evaluate_executability(row: dict[str, Any]) -> dict[str, Any]:
    """Basic executability from listing evidence only — no fabricated compliance."""
    text = _blob(row)
    blockers: list[str] = []
    reviews: list[str] = []
    positives: list[str] = []

    for pat in _HARD_EXEC_PATTERNS:
        if re.search(pat, text, re.I):
            blockers.append(pat.strip("\\b"))

    for pat in _REVIEW_EXEC_PATTERNS:
        if re.search(pat, text, re.I):
            reviews.append(pat.strip("\\b"))

    product_class = row.get("product_classification") or "UNKNOWN"
    if product_class == "SERVICE":
        blockers.append("service_heavy_classification")
    elif product_class == "CLEARLY_IRRELEVANT":
        blockers.append("non_product_classification")

    if re.search(r"\bequipment\b|\bsupplies\b|\bhardware\b|\bvehicles?\b|\bparts?\b", text, re.I):
        positives.append("product_identifiable_from_listing")
    elif product_class in PRODUCT_LAUNCH_CLASSES:
        positives.append("product_class_positive")
    else:
        reviews.append("product_not_clearly_identifiable")

    if any(re.search(p, text, re.I) for p in _INSTALL_PATTERNS):
        reviews.append("installation_may_be_required")

    if not row.get("detail_url") and not row.get("document_link_discovered"):
        reviews.append("limited_solicitation_evidence")

    if blockers:
        status = EXEC_HARD_FAIL
    elif reviews and not positives:
        status = EXEC_NEEDS_REVIEW
    elif reviews:
        status = EXEC_POSSIBLE if positives else EXEC_NEEDS_REVIEW
    elif positives:
        status = EXEC_PASS
    else:
        status = EXEC_UNKNOWN

    return {
        "executability_status": status,
        "execution_blockers": blockers,
        "execution_review_items": reviews,
        "execution_positives": positives,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def evaluate_preliminary_economics(row: dict[str, Any]) -> dict[str, Any]:
    """Stage-A economics from inexpensive listing evidence only."""
    value = _parse_value(row)
    product_class = row.get("product_classification") or "UNKNOWN"
    qty_match = re.search(r"\bqty(?:uantity)?\s*[:\-]?\s*(\d+)\b", _blob(row), re.I)
    quantity = int(qty_match.group(1)) if qty_match else None

    if product_class in {"SERVICE", "CLEARLY_IRRELEVANT"}:
        return {
            "economics_status": ECON_FAIL,
            "estimated_contract_value": value,
            "quantity": quantity,
            "economics_reason": "non_product_or_service_heavy",
            "LIVE_API_REQUESTS": 0,
        }

    if value is not None:
        if value >= 150_000:
            status = ECON_PROMISING
        elif value >= 50_000:
            status = ECON_POSSIBLE
        elif value >= 15_000:
            status = ECON_UNKNOWN
        else:
            status = ECON_UNLIKELY
    elif product_class in PRODUCT_LAUNCH_CLASSES:
        status = ECON_UNKNOWN
    else:
        status = ECON_UNKNOWN

    return {
        "economics_status": status,
        "estimated_contract_value": value,
        "quantity": quantity,
        "economics_reason": "listing_value_and_product_signals",
        "LIVE_API_REQUESTS": 0,
    }


def evaluate_profit_status(
    row: dict[str, Any],
    economics: dict[str, Any],
) -> dict[str, Any]:
    """Never treat unknown supplier cost as zero or automatic failure."""
    target = min_actual_profit_usd()
    supplier_cost = row.get("estimated_supplier_cost")
    if supplier_cost is not None and str(supplier_cost).upper() == "UNKNOWN":
        supplier_cost = None

    bid = economics.get("estimated_contract_value")
    known_profit = None
    if bid is not None and supplier_cost is not None:
        try:
            known_profit = float(bid) - float(supplier_cost)
        except (TypeError, ValueError):
            known_profit = None

    if known_profit is not None:
        if known_profit >= target:
            status = PROFIT_CONFIRMED_ABOVE
        else:
            status = PROFIT_BELOW
    elif bid is not None and bid >= target * 4:
        status = PROFIT_POTENTIALLY_ABOVE
    elif bid is not None and bid >= target * 2:
        status = PROFIT_ESTIMATED_ABOVE
    elif economics.get("economics_status") == ECON_FAIL:
        status = PROFIT_BELOW
    else:
        status = PROFIT_UNKNOWN

    return {
        "profit_status": status,
        "profit_target_usd": target,
        "known_profit_usd": known_profit,
        "supplier_cost_known": supplier_cost is not None,
        "LIVE_API_REQUESTS": 0,
    }


def _funding_sources_from_kb() -> list[dict[str, Any]]:
    kb = production_knowledge_base_seed()
    return list(kb.get("private_lenders") or [])


def rank_funding_matches(
    row: dict[str, Any],
    economics: dict[str, Any],
    *,
    company_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preliminary funding match — ranking is NOT approval."""
    facts = {**LAUNCH_COMPANY_FACTS, **(company_facts or {})}
    req = build_funding_requirement(
        opportunity=row,
        economics={
            "estimated_bid_value": economics.get("estimated_contract_value"),
            "estimated_supplier_cost": None,
        },
        deadline=row,
        extras={
            **facts,
            "deal_qualified": row.get("executability_status") != EXEC_HARD_FAIL,
        },
    )
    sources = _funding_sources_from_kb()
    matches = [
        {**match_funding_source(req, src, deal_facts=facts), "source_profile": src}
        for src in sources
    ]
    viable = [m for m in matches if m.get("match_status") != MATCH_REJECT]
    viable.sort(key=lambda m: (-(m.get("match_score") or 0), m.get("source_name") or ""))

    top = viable[:3]
    sheets = []
    for i, m in enumerate(top, start=1):
        src = m.get("source_profile") or {}
        sheets.append(
            build_operator_call_sheet(
                requirement=req,
                source=src,
                match_result=m,
                call_priority=i,
            )
        )

    conf = assess_deal_funding_confidence(requirement=req, match_results=matches)
    gov_fin = assess_government_contract_financing(
        solicitation_evidence=row.get("government_financing_evidence")
    )

    if not viable:
        funding_status = CONF_NO_KNOWN_PATH
    elif conf.get("funding_confidence") in {CONF_UNASSESSED, CONF_NEEDS_LENDER_VERIFICATION}:
        funding_status = CONF_NEEDS_LENDER_VERIFICATION
    else:
        funding_status = conf.get("funding_confidence") or CONF_PRELIMINARY_MATCH

    return {
        "funding_requirement": req,
        "funding_matches": matches,
        "top_funding_matches": top,
        "top_funding_call_sheets": sheets,
        "funding_status": funding_status,
        "funding_secured": False,
        "government_financing": gov_fin,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "paid": 0,
    }


def determine_primary_next_action(row: dict[str, Any]) -> dict[str, Any]:
    """One primary operator action per surviving candidate."""
    exec_status = row.get("executability_status") or EXEC_UNKNOWN
    viability = row.get("deadline_viability") or VIABILITY_UNKNOWN
    economics = row.get("economics_status") or ECON_UNKNOWN
    profit = row.get("profit_status") or PROFIT_UNKNOWN
    product_class = row.get("product_classification") or "UNKNOWN"

    if exec_status == EXEC_HARD_FAIL:
        return {"primary_next_action": ACTION_REJECT, "next_action_reason": "execution_hard_fail"}

    pursuit = may_enter_normal_pursuit_queue(viability, deal_qualified=True)
    if viability == VIABILITY_TOO_LATE and not pursuit.get("allowed"):
        return {"primary_next_action": ACTION_REJECT, "next_action_reason": "deadline_too_late"}

    if viability == VIABILITY_UNKNOWN:
        return {"primary_next_action": ACTION_VERIFY_DEADLINE, "next_action_reason": "deadline_unverified"}

    if not row.get("document_link_discovered") and not row.get("description"):
        return {
            "primary_next_action": ACTION_REVIEW_SOLICITATION,
            "next_action_reason": "insufficient_solicitation_documents",
        }

    if product_class == "UNKNOWN":
        return {"primary_next_action": ACTION_IDENTIFY_PRODUCT, "next_action_reason": "product_unclassified"}

    if profit in {PROFIT_UNKNOWN, PROFIT_POTENTIALLY_ABOVE, PROFIT_ESTIMATED_ABOVE} and economics in {
        ECON_PROMISING,
        ECON_POSSIBLE,
        ECON_UNKNOWN,
    }:
        if row.get("estimated_supplier_cost") is None:
            return {
                "primary_next_action": ACTION_GET_SUPPLIER_QUOTE,
                "next_action_reason": "supplier_cost_unknown",
            }
        return {"primary_next_action": ACTION_READY_DEEP, "next_action_reason": "economics_warrants_deep_research"}

    if row.get("funding_status") == CONF_NEEDS_LENDER_VERIFICATION and row.get("top_funding_matches"):
        return {
            "primary_next_action": ACTION_VERIFY_FUNDING,
            "next_action_reason": "preliminary_funding_match_needs_operator_verification_later",
        }

    if row.get("government_financing", {}).get("availability") == "NEEDS_VERIFICATION":
        return {
            "primary_next_action": ACTION_VERIFY_GOV_FINANCING,
            "next_action_reason": "government_financing_not_established",
        }

    if economics == ECON_FAIL:
        return {"primary_next_action": ACTION_REJECT, "next_action_reason": "economics_fail"}

    return {"primary_next_action": ACTION_READY_DEEP, "next_action_reason": "default_deep_research_path"}


def build_operator_summary_card(row: dict[str, Any]) -> dict[str, Any]:
    """Human-readable operator card — unknown fields stay empty/UNKNOWN."""
    top_sheets = row.get("top_funding_call_sheets") or []
    top_matches = []
    for sheet in top_sheets[:3]:
        top_matches.append(
            {
                "company": sheet.get("source"),
                "phone": sheet.get("phone"),
                "who_to_ask_for": sheet.get("who_to_ask_for"),
                "department_title": sheet.get("department_title"),
                "match_status": sheet.get("match_status"),
                "why_it_fits": sheet.get("why_this_source_may_match"),
                "verified_known": sheet.get("what_we_already_know"),
                "critical_unknowns": sheet.get("what_we_still_need_to_verify"),
                "deal_specific_questions": [q.get("question") for q in (sheet.get("questions") or [])],
                "funding_secured": False,
            }
        )

    unknowns = list(row.get("execution_review_items") or [])
    unknowns.extend(row.get("funding_requirement", {}).get("funding_confidence") == "UNKNOWN" and ["funding_amount"] or [])
    if row.get("profit_status") == PROFIT_UNKNOWN:
        unknowns.append("supplier_cost_and_margin")
    if row.get("deadline_viability") == VIABILITY_UNKNOWN:
        unknowns.append("deadline")

    return {
        "agency": row.get("agency"),
        "solicitation_id": row.get("solicitation_number") or row.get("external_id"),
        "title": row.get("title"),
        "source": row.get("source_id"),
        "source_url": row.get("detail_url") or row.get("source_url"),
        "due_date": row.get("response_deadline") or row.get("deadline_raw"),
        "time_remaining": row.get("deadline_display"),
        "deadline_badge": row.get("deadline_badge"),
        "product_classification": PRODUCT_DISPLAY.get(
            row.get("product_classification") or "UNKNOWN", "UNKNOWN"
        ),
        "what_they_are_buying": row.get("title"),
        "quantity": row.get("quantity"),
        "estimated_contract_value": row.get("estimated_contract_value"),
        "executability": row.get("executability_status"),
        "economics_status": row.get("economics_status"),
        "profit_status": row.get("profit_status"),
        "funding_status": row.get("funding_status"),
        "top_apparent_funding_matches": top_matches,
        "critical_unknowns": sorted(set(unknowns)),
        "primary_next_action": row.get("primary_next_action"),
        "why_worth_working": row.get("priority_reasons", {}).get("worth_working"),
        "why_could_fail": row.get("priority_reasons", {}).get("could_fail"),
        "priority_rank": row.get("operator_priority_rank"),
        "provenance": {
            "source_system": row.get("source_id"),
            "source_url": row.get("detail_url") or row.get("source_url"),
            "retrieval_time": row.get("retrieved_at") or row.get("discovered_at"),
            "raw_source_identifier": row.get("external_id"),
            "raw_title": row.get("title"),
            "raw_deadline": row.get("deadline_raw"),
            "normalization_parser": row.get("adapter_family") or row.get("platform_family"),
        },
        "LIVE_API_REQUESTS": 0,
    }


def compute_operator_priority(row: dict[str, Any]) -> dict[str, Any]:
    """Transparent priority with reason codes — not a hidden single score."""
    product = row.get("product_classification") or "UNKNOWN"
    viability = row.get("deadline_viability") or VIABILITY_UNKNOWN
    exec_status = row.get("executability_status") or EXEC_UNKNOWN
    economics = row.get("economics_status") or ECON_UNKNOWN
    profit = row.get("profit_status") or PROFIT_UNKNOWN
    structural = row.get("structural_gate", {}).get("strong_field_count") or 0

    product_score = {
        "CORE_PRODUCT": 100,
        "PRODUCT_PLUS_SERVICE": 85,
        "UNKNOWN": 45,
        "SERVICE": 10,
        "CLEARLY_IRRELEVANT": 0,
    }.get(product, 40)

    deadline_score = {
        "PLENTY_OF_TIME": 90,
        "GOOD": 80,
        "RUSH": 55,
        "UNKNOWN": 35,
        "TOO_LATE": 0,
    }.get(viability, 30)

    exec_score = {
        EXEC_PASS: 85,
        EXEC_POSSIBLE: 70,
        EXEC_NEEDS_REVIEW: 50,
        EXEC_UNKNOWN: 40,
        EXEC_HARD_FAIL: 0,
    }.get(exec_status, 40)

    econ_score = {
        ECON_PROMISING: 80,
        ECON_POSSIBLE: 65,
        ECON_UNKNOWN: 45,
        ECON_UNLIKELY: 20,
        ECON_FAIL: 0,
    }.get(economics, 40)

    profit_score = {
        PROFIT_CONFIRMED_ABOVE: 90,
        PROFIT_ESTIMATED_ABOVE: 75,
        PROFIT_POTENTIALLY_ABOVE: 65,
        PROFIT_UNKNOWN: 50,
        PROFIT_BELOW: 10,
    }.get(profit, 40)

    funding_score = 50
    if row.get("top_funding_matches"):
        funding_score = 70
    if row.get("funding_status") == CONF_NO_KNOWN_PATH:
        funding_score = 20

    total = (
        product_score * 0.22
        + deadline_score * 0.20
        + exec_score * 0.18
        + econ_score * 0.15
        + profit_score * 0.15
        + funding_score * 0.10
        + structural * 2
    )

    worth = []
    if product in PRODUCT_LAUNCH_CLASSES:
        worth.append("product_resale_fit")
    if viability in {"GOOD", "PLENTY_OF_TIME", "RUSH"}:
        worth.append("deadline_viable")
    if economics in {ECON_PROMISING, ECON_POSSIBLE}:
        worth.append("economics_promising_or_possible")
    if row.get("top_funding_matches"):
        worth.append("preliminary_funding_path_exists")

    could_fail = list(row.get("execution_blockers") or [])
    could_fail.extend(row.get("execution_review_items") or [])
    if profit == PROFIT_BELOW:
        could_fail.append("profit_below_target")
    if row.get("funding_status") == CONF_NO_KNOWN_PATH:
        could_fail.append("no_known_funding_path")

    return {
        "operator_priority_score": round(total, 2),
        "priority_reasons": {
            "worth_working": worth or ["needs_manual_review"],
            "could_fail": could_fail or ["insufficient_evidence"],
            "component_scores": {
                "product": product_score,
                "deadline": deadline_score,
                "executability": exec_score,
                "economics": econ_score,
                "profit": profit_score,
                "funding": funding_score,
                "structural_fields": structural,
            },
        },
    }


def dedupe_opportunities(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Cross-source dedupe using strong identifiers only."""
    kept: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    duplicates = 0

    for row in rows:
        key = strong_canonical_key(row)
        if not key:
            kept.append(row)
            continue
        if key in seen:
            duplicates += 1
            existing = seen[key]
            if (row.get("trust_tier") or 99) < (existing.get("trust_tier") or 99):
                seen[key] = row
                kept = [r for r in kept if strong_canonical_key(r) != key]
                kept.append(row)
            continue
        seen[key] = row
        kept.append(row)

    return kept, duplicates


def process_opportunity_pipeline(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full post-discovery pipeline for one opportunity."""
    out = dict(row)
    out.setdefault("retrieved_at", now_utc().isoformat())

    gate = is_structurally_valid_opportunity(out)
    out["structural_gate"] = gate
    if not gate.get("valid"):
        out["pipeline_rejected"] = True
        out["pipeline_reject_reason"] = "structural_gate_failed"
        return out

    enrich_input = dict(out)
    if enrich_input.get("deadline_raw"):
        enrich_input.pop("response_deadline", None)
    out = enrich_opportunity_deadline(enrich_input, now=now)

    if not out.get("product_classification"):
        cls = classify_discovery_opportunity(
            title=out.get("title"),
            description=out.get("description"),
            status=out.get("status"),
        )
        out["product_classification"] = cls["classification"]

    dl = normalize_deadline(out.get("deadline_raw"))
    reject = early_reject_reasons(
        title=out.get("title"),
        description=out.get("description"),
        status=out.get("status"),
        deadline_passed=bool(dl.get("deadline_passed")),
        classification=out.get("product_classification"),
        estimated_value=out.get("estimated_value"),
        estimated_value_status=out.get("estimated_value_status", "UNKNOWN"),
    )
    if reject.get("reject"):
        out["pipeline_rejected"] = True
        out["pipeline_reject_reason"] = reject.get("reasons")
        return out

    exec_result = evaluate_executability(out)
    out.update(exec_result)

    econ = evaluate_preliminary_economics(out)
    out.update(econ)

    profit = evaluate_profit_status(out, econ)
    out.update(profit)

    funding = rank_funding_matches(out, econ)
    out.update(funding)
    out["government_financing"] = funding.get("government_financing")

    if out.get("product_classification") in PRODUCT_LAUNCH_CLASSES and out.get("estimated_supplier_cost") is None:
        out["supplier_funding_action"] = ACTION_VERIFY_SUPPLIER_TERMS

    action = determine_primary_next_action(out)
    out.update(action)
    if action.get("primary_next_action") == ACTION_REJECT:
        out["pipeline_rejected"] = True
        out["pipeline_reject_reason"] = action.get("next_action_reason")

    priority = compute_operator_priority(out)
    out.update(priority)

    out["operator_summary_card"] = build_operator_summary_card(out)
    out.setdefault("pipeline_rejected", False)
    return out


def build_funnel_metrics(
    *,
    discovery: dict[str, Any],
    raw_rows: list[dict[str, Any]],
    processed: list[dict[str, Any]],
    deduped: list[dict[str, Any]],
    duplicates_removed: int,
) -> dict[str, Any]:
    """Aggregate funnel counts for TINY report."""
    metrics = discovery.get("metrics") or {}
    per_source = metrics.get("per_source") or {}

    product_counts: dict[str, int] = {}
    deadline_counts: dict[str, int] = {}
    exec_counts: dict[str, int] = {}
    econ_counts: dict[str, int] = {}
    profit_counts: dict[str, int] = {}
    funding_counts: dict[str, int] = {}

    early_rejected = max(0, int(metrics.get("raw_records") or 0) - int(metrics.get("unique_records") or 0))

    survivors = []
    for row in deduped:
        if row.get("pipeline_rejected"):
            continue
        survivors.append(row)
        pc = PRODUCT_DISPLAY.get(row.get("product_classification") or "UNKNOWN", "UNKNOWN")
        product_counts[pc] = product_counts.get(pc, 0) + 1
        dv = row.get("deadline_viability") or "UNKNOWN"
        deadline_counts[dv] = deadline_counts.get(dv, 0) + 1
        es = row.get("executability_status") or "UNKNOWN"
        exec_counts[es] = exec_counts.get(es, 0) + 1
        ec = row.get("economics_status") or "UNKNOWN"
        econ_counts[ec] = econ_counts.get(ec, 0) + 1
        ps = row.get("profit_status") or "UNKNOWN"
        profit_counts[ps] = profit_counts.get(ps, 0) + 1
        fs = row.get("funding_status") or "UNASSESSED"
        funding_counts[fs] = funding_counts.get(fs, 0) + 1

    structurally_valid = sum(1 for r in processed if (r.get("structural_gate") or {}).get("valid"))
    pipeline_rejected = sum(1 for r in processed if r.get("pipeline_rejected"))
    rejected_non_opp = early_rejected + pipeline_rejected

    funding_plausible = sum(
        1
        for r in survivors
        if r.get("top_funding_matches")
        and r.get("funding_status") in {CONF_PRELIMINARY_MATCH, CONF_NEEDS_LENDER_VERIFICATION}
    )

    return {
        "sources_attempted": metrics.get("sources_attempted", 0),
        "sources_successful": metrics.get("sources_successful", 0),
        "http_requests": discovery.get("LIVE_API_REQUESTS", 0),
        "raw_listings": metrics.get("raw_records", len(raw_rows)),
        "structurally_valid": structurally_valid,
        "early_discovery_rejected": early_rejected,
        "pipeline_rejected": pipeline_rejected,
        "non_opportunity_rejected": rejected_non_opp,
        "duplicates_removed": duplicates_removed,
        "deduped_count": len(deduped),
        "product_classification": product_counts,
        "deadline_viability": deadline_counts,
        "executability": exec_counts,
        "economics": econ_counts,
        "profit": profit_counts,
        "funding": funding_counts,
        "surviving_operator_candidates": len(survivors),
        "funding_plausible_or_needs_verification": funding_plausible,
        "per_source": per_source,
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
    }


def run_tiny_end_to_end(
    *,
    authorize_live: bool = False,
    transport: Any | None = None,
    source_ids: list[str] | None = None,
    max_sources: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run live TINY discovery and full integration pipeline."""
    from discovery.live_runner import run_live_discovery

    default_sources = [
        "state_ia",
        "state_mt",
        "coop_sourcewell_live",
        "state_ne",
        "state_tx",
        "state_pa",
    ]
    ids = source_ids or default_sources
    discovery = run_live_discovery(
        profile="tiny",
        preview=True,
        persist=False,
        authorize_live=authorize_live,
        transport=transport,
        source_ids=ids,
        max_sources=max_sources or len(ids),
    )

    raw_rows = list(discovery.get("opportunities") or [])
    processed = [process_opportunity_pipeline(dict(r), now=now) for r in raw_rows]
    deduped, duplicates_removed = dedupe_opportunities(processed)

    queue_result = sort_operator_queue(
        [r for r in deduped if not r.get("pipeline_rejected")],
        now=now,
    )
    ordered = queue_result.get("ordered_for_operator") or []
    for rank, row in enumerate(sorted(ordered, key=lambda r: -(r.get("operator_priority_score") or 0)), start=1):
        row["operator_priority_rank"] = rank
        row["operator_summary_card"] = build_operator_summary_card(row)

    actionable = [r for r in deduped if not r.get("pipeline_rejected")]
    top_candidates = sorted(
        actionable,
        key=lambda r: (-(r.get("operator_priority_score") or 0), r.get("operator_priority_rank") or 999),
    )[:5]

    funnel = build_funnel_metrics(
        discovery=discovery,
        raw_rows=raw_rows,
        processed=processed,
        deduped=deduped,
        duplicates_removed=duplicates_removed,
    )

    return {
        "run_at": now_utc().isoformat(),
        "discovery": discovery,
        "funnel": funnel,
        "processed_count": len(processed),
        "deduped_count": len(deduped),
        "duplicates_removed": duplicates_removed,
        "operator_queue": queue_result,
        "top_candidates": top_candidates,
        "top_candidate_cards": [build_operator_summary_card(c) for c in top_candidates],
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
        "LIVE_API_REQUESTS": discovery.get("LIVE_API_REQUESTS", 0),
        "lender_outreach_performed": False,
    }


def write_tiny_artifacts(result: dict[str, Any], artifacts_dir: Path | None = None) -> dict[str, str]:
    """Write JSON, CSV, and markdown report artifacts."""
    base = artifacts_dir or Path(__file__).resolve().parents[1] / "artifacts"
    base.mkdir(parents=True, exist_ok=True)

    json_path = base / "tiny_end_to_end_results.json"
    csv_path = base / "tiny_end_to_end_queue.csv"
    md_path = base / "tiny_end_to_end_report.md"

    serializable = json.loads(json.dumps(result, default=str))

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)

    cards = result.get("top_candidate_cards") or []
    fieldnames = [
        "priority_rank",
        "agency",
        "solicitation_id",
        "title",
        "source",
        "source_url",
        "due_date",
        "deadline_badge",
        "product_classification",
        "executability",
        "economics_status",
        "profit_status",
        "funding_status",
        "primary_next_action",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for card in cards:
            writer.writerow(card)

    funnel = result.get("funnel") or {}
    lines = [
        "# REAL TINY End-to-End Validation Report",
        "",
        f"Run at: {result.get('run_at')}",
        "",
        "## Request budget",
        f"- Public HTTP: {result.get('LIVE_API_REQUESTS', 0)}",
        "- SAM: 0",
        "- OpenAI: 0",
        "- USAspending: 0",
        "- Paid APIs: 0",
        "- Lender outreach: none",
        "",
        "## Funnel",
        f"- Raw listings: {funnel.get('raw_listings')}",
        f"- Structurally valid: {funnel.get('structurally_valid')}",
        f"- Non-opportunity rejected: {funnel.get('non_opportunity_rejected')}",
        f"- Duplicates removed: {funnel.get('duplicates_removed')}",
        f"- Surviving operator candidates: {funnel.get('surviving_operator_candidates')}",
        "",
        "## Top candidates",
    ]
    for i, card in enumerate(cards, start=1):
        lines.extend(
            [
                f"### #{i} {card.get('title') or 'Untitled'}",
                f"- Agency: {card.get('agency')}",
                f"- Solicitation: {card.get('solicitation_id')}",
                f"- Source: {card.get('source')}",
                f"- Due: {card.get('due_date')} ({card.get('deadline_badge')})",
                f"- Product: {card.get('product_classification')}",
                f"- Executability: {card.get('executability')}",
                f"- Economics: {card.get('economics_status')}",
                f"- Profit: {card.get('profit_status')}",
                f"- Funding: {card.get('funding_status')}",
                f"- Next action: {card.get('primary_next_action')}",
                "",
            ]
        )

    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "markdown": str(md_path),
    }
