"""Deep Deal Research + Bid Qualification orchestrator."""

from __future__ import annotations
from application_clock import now_utc

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deep_deal_compliance import evaluate_bid_compliance
from deep_deal_constants import (
    DEC_BID_READY,
    DEC_CONTINUE,
    DEC_QUALIFIED,
    DEC_REJECT,
    DEC_STRATEGIC,
    DEC_WAIT_FUNDING,
    DEC_WAIT_QUOTE,
    DEAL_COOP_MASTER,
    FIT_CORE_TRANSACTIONAL,
    FIT_LONG_TERM_CHANNEL,
    FIT_NOT_OUR_MODEL,
    FIT_POOR_LAUNCH,
    FIT_POTENTIAL_VEHICLE,
    HARD_MAX_PUBLIC_HTTP,
    PROFIT_BELOW,
    PROFIT_UNKNOWN,
    QUEUE_IMMEDIATE,
    QUEUE_REJECTED,
    QUEUE_STRATEGIC,
    STATE_BID_READY,
    STATE_DOC_REVIEWED,
    STATE_NEEDS_DOC_REVIEW,
    STATE_NEEDS_PRODUCT_ID,
    STATE_NEEDS_SUPPLIER_QUOTE,
    STATE_QUALIFIED_REVIEW,
    STATE_REJECTED,
    STATE_STRATEGIC,
    STATE_WAITING_EXTERNAL,
    STOP_FUNDING_ONLY_PG,
    STOP_INELIGIBLE,
    STOP_NOT_RESALE,
    STOP_OEM_REQUIRED,
    STOP_PROFIT_BELOW,
    STOP_SELF_PERFORM,
    STOP_STRATEGIC_ONLY,
    TARGET_PUBLIC_HTTP,
)

_HARD_RESEARCH_STOPS = frozenset(
    {
        STOP_NOT_RESALE,
        STOP_INELIGIBLE,
        STOP_PROFIT_BELOW,
        STOP_FUNDING_ONLY_PG,
        STOP_OEM_REQUIRED,
        STOP_SELF_PERFORM,
    }
)
from deep_deal_documents import (
    discover_document_links,
    extract_solicitation_facts,
    reclassify_from_documents,
    retrieve_solicitation_page,
)
from deep_deal_economics import (
    build_deal_economics,
    maximum_allowable_supplier_cost,
    minimum_required_bid_price,
)
from deep_deal_openai import OpenAIBudget, merge_ai_proposals_safely, run_openai_advisory
from deep_deal_qualification import qualify_candidate
from deep_deal_suppliers import (
    build_line_item_record,
    build_supplier_contact_packet,
    public_supplier_search_plan,
)
from funding_path_intelligence import (
    assess_deal_funding_confidence,
    build_funding_requirement,
    build_operator_call_sheet,
    match_funding_source,
)
from funding_source_kb import production_knowledge_base_seed


def _utc() -> str:
    return now_utc().isoformat()


def _safe_id(sol: str | None) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(sol or "unknown"))
    return s[:80] or "unknown"


class ResearchBudget:
    def __init__(
        self,
        *,
        public_http_hard_max: int = HARD_MAX_PUBLIC_HTTP,
        public_http_target: int = TARGET_PUBLIC_HTTP,
        usaspending_hard_max: int = 10,
        sam_hard_max: int = 0,
    ):
        self.public_http = 0
        self.public_http_hard_max = public_http_hard_max
        self.public_http_target = public_http_target
        self.usaspending = 0
        self.usaspending_hard_max = usaspending_hard_max
        self.sam = 0
        self.sam_hard_max = sam_hard_max
        self.openai = OpenAIBudget()
        self.lender_outreach = 0
        self.supplier_outreach = 0
        self.bid_submissions = 0

    def can_http(self) -> bool:
        return self.public_http < self.public_http_hard_max

    def record_http(self, n: int = 1) -> None:
        self.public_http += n

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_http": self.public_http,
            "public_http_target": self.public_http_target,
            "public_http_hard_max": self.public_http_hard_max,
            "USAspending": self.usaspending,
            "SAM": self.sam,
            "OpenAI": self.openai.used,
            "paid": 0,
            "lender_outreach": self.lender_outreach,
            "supplier_outreach": self.supplier_outreach,
            "bid_submissions": self.bid_submissions,
            **self.openai.to_dict(),
        }


def determine_bid_state(packet: dict[str, Any]) -> str:
    """BID_READY only with sufficient verified evidence — never from preliminary optimism."""
    stop = packet.get("research_stop_reason")
    if stop in _HARD_RESEARCH_STOPS or packet.get("pre_deep_fit") == FIT_NOT_OUR_MODEL:
        return STATE_REJECTED
    if packet.get("pre_deep_fit") in {FIT_LONG_TERM_CHANNEL, FIT_POTENTIAL_VEHICLE} or packet.get("deal_type") == DEAL_COOP_MASTER:
        return STATE_STRATEGIC
    if packet.get("hard_blockers"):
        return STATE_REJECTED
    if packet.get("pre_deep_fit") == FIT_POOR_LAUNCH or stop:
        # Soft stop — needs operator document confirmation, not auto BID path
        return STATE_NEEDS_DOC_REVIEW

    has_docs = bool(packet.get("document_review", {}).get("reviewed"))
    has_product = bool(packet.get("line_items")) and packet.get("product_id_certainty") not in {None, "PRODUCT_UNKNOWN"}
    has_quote = packet.get("supplier_cost_known") is True
    has_profit = packet.get("economics", {}).get("profit_confidence") not in {None, PROFIT_UNKNOWN, PROFIT_BELOW}
    has_funding = bool(packet.get("funding", {}).get("top_matches"))
    compliance_ok = packet.get("compliance", {}).get("overall_compliance") == "PASS"

    if has_docs and has_product and has_quote and has_profit and has_funding and compliance_ok:
        if packet.get("submission_documents_known"):
            return STATE_BID_READY
        return STATE_QUALIFIED_REVIEW

    if not has_docs:
        return STATE_NEEDS_DOC_REVIEW
    if not has_product:
        return STATE_NEEDS_PRODUCT_ID
    if not has_quote:
        return STATE_NEEDS_SUPPLIER_QUOTE
    if packet.get("funding_status") in {"NEEDS_LENDER_VERIFICATION", "NO_KNOWN_PATH"}:
        return STATE_WAITING_EXTERNAL
    return STATE_DOC_REVIEWED


def determine_decision(packet: dict[str, Any]) -> str:
    state = packet.get("bid_qualification_state")
    if state == STATE_REJECTED:
        return DEC_REJECT
    if state == STATE_STRATEGIC:
        return DEC_STRATEGIC
    if state == STATE_BID_READY:
        return DEC_BID_READY
    if state == STATE_NEEDS_SUPPLIER_QUOTE:
        return DEC_WAIT_QUOTE
    if state in {STATE_WAITING_EXTERNAL} and packet.get("funding", {}).get("top_matches"):
        return DEC_WAIT_FUNDING
    if state == STATE_QUALIFIED_REVIEW:
        return DEC_QUALIFIED
    return DEC_CONTINUE


def primary_next_action_for_deal(packet: dict[str, Any]) -> dict[str, Any]:
    decision = packet.get("decision")
    state = packet.get("bid_qualification_state")
    if decision == DEC_REJECT:
        return {"primary_next_action": "REJECT_OPPORTUNITY", "reason": packet.get("research_stop_reason") or "not_fit"}
    if decision == DEC_STRATEGIC:
        return {
            "primary_next_action": "FILE_AS_STRATEGIC_CONTRACT_VEHICLE",
            "reason": "cooperative_or_vehicle_no_committed_purchase",
        }
    if packet.get("pre_deep_fit") == FIT_POOR_LAUNCH or packet.get("deal_type") == "CONSTRUCTION_OR_UPGRADE":
        return {
            "primary_next_action": "REVIEW_SOLICITATION_DOCUMENTS",
            "reason": "confirm_whether_equipment_resale_or_construction_performance",
        }
    if state == STATE_NEEDS_DOC_REVIEW:
        return {"primary_next_action": "REVIEW_SOLICITATION_DOCUMENTS", "reason": "need_documents"}
    if state == STATE_NEEDS_PRODUCT_ID:
        return {"primary_next_action": "IDENTIFY_EXACT_PRODUCT", "reason": "product_not_identified"}
    if state == STATE_NEEDS_SUPPLIER_QUOTE:
        return {"primary_next_action": "GET_SUPPLIER_QUOTE", "reason": "supplier_cost_unknown"}
    if decision == DEC_WAIT_FUNDING:
        return {"primary_next_action": "VERIFY_FUNDING_WITH_LENDER", "reason": "deal_worth_lender_verification"}
    return {"primary_next_action": "CONTINUE_DEEP_RESEARCH", "reason": "incomplete_evidence"}


def build_lender_call_packet(
    *,
    opportunity: dict[str, Any],
    economics: dict[str, Any],
    funding_match: dict[str, Any],
    call_sheet: dict[str, Any],
) -> dict[str, Any]:
    """Lender packet only when deal survived far enough — no call performed."""
    return {
        "solicitation": opportunity.get("solicitation_number") or opportunity.get("solicitation_id"),
        "agency": opportunity.get("agency"),
        "product": opportunity.get("title"),
        "expected_sale": (economics.get("gross_revenue") or {}).get("value"),
        "supplier_cost": (economics.get("cogs") or {}).get("value"),
        "gross_margin_pct": economics.get("gross_margin_pct"),
        "expected_profit": (economics.get("expected_deal_profit") or {}).get("value"),
        "funding_required": (economics.get("working_capital_required") or {}).get("value"),
        "supplier": None,
        "delivery": opportunity.get("delivery_location"),
        "government_payment_timing": "UNKNOWN",
        "award_status": "PRE_BID",
        "best_apparent_lender": call_sheet.get("source"),
        "phone": call_sheet.get("phone"),
        "role_title": call_sheet.get("department_title"),
        "why_matched": call_sheet.get("why_this_source_may_match"),
        "verified_criteria": call_sheet.get("what_we_already_know"),
        "unknown_criteria": call_sheet.get("what_we_still_need_to_verify"),
        "opening": call_sheet.get("opening"),
        "exact_questions": [q.get("question") for q in (call_sheet.get("questions") or [])],
        "first_questions_priority": [
            "Will you finance a company's first government contract?",
            "Is any borrower cash/equity contribution required?",
            "Is a personal guarantee required?",
            "Do you pull or underwrite personal credit?",
        ],
        "match_status": funding_match.get("match_status"),
        "funding_secured": False,
        "outreach_performed": False,
        "LIVE_API_REQUESTS": 0,
    }


def research_one_deal(
    opportunity: dict[str, Any],
    *,
    budget: ResearchBudget | None = None,
    authorize_live: bool = False,
    transport_client: Any | None = None,
    force_openai_offline: bool = True,
    fetch_documents: bool = True,
) -> dict[str, Any]:
    """
    Controlled deep research for one opportunity.
    Stops early on strategic vehicles / hard failures to avoid wasted spend.
    """
    budget = budget or ResearchBudget()
    opp = dict(opportunity)
    sol = opp.get("solicitation_number") or opp.get("solicitation_id") or opp.get("external_id")

    # 1) Deal type + pre-deep gate (cheap)
    qualification = qualify_candidate(opp)
    packet: dict[str, Any] = {
        "researched_at": _utc(),
        "opportunity": {
            "agency": opp.get("agency"),
            "solicitation_id": sol,
            "title": opp.get("title"),
            "source": opp.get("source_id") or opp.get("source"),
            "source_url": opp.get("source_url") or opp.get("detail_url"),
            "due_date": opp.get("due_date") or opp.get("deadline_raw") or opp.get("response_deadline"),
            "product_classification_listing": opp.get("product_classification"),
        },
        **qualification,
        "research_stop_reason": None,
        "document_review": {"reviewed": False, "documents": [], "http_requests": 0},
        "extracted_facts": {},
        "provenance": {},
        "line_items": [],
        "compliance": {},
        "economics": {},
        "funding": {},
        "suppliers": {},
        "openai_advisory": {},
        "lender_call_packet": None,
        "critical_unknowns": [],
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }

    # Strategic coop / vehicle — classify accurately, optional light page fetch, no fake PO economics
    if qualification.get("pre_deep_fit") in {FIT_LONG_TERM_CHANNEL, FIT_POTENTIAL_VEHICLE} or qualification.get("deal_type") == DEAL_COOP_MASTER:
        packet["research_stop_reason"] = STOP_STRATEGIC_ONLY
        packet["bid_qualification_state"] = STATE_STRATEGIC
        packet["decision"] = DEC_STRATEGIC
        packet["operator_queue_bucket"] = QUEUE_STRATEGIC
        packet.update(primary_next_action_for_deal(packet))
        packet["why_fit"] = ["may_be_valuable_long_term_sales_channel"]
        packet["why_not_immediate"] = [
            "no_guaranteed_purchase_quantity",
            "no_immediate_government_receivable",
            "ceiling_or_program_volume_is_not_committed_revenue",
            "not_financeable_as_single_awarded_transaction_until_member_po",
        ]
        packet["money_note"] = "Do not treat cooperative ceiling / estimated member demand as current contract revenue"
        # Optional light page fetch for confirmation only
        url = packet["opportunity"]["source_url"]
        if fetch_documents and authorize_live and url and budget.can_http():
            page = retrieve_solicitation_page(url, client=transport_client, authorize_live=True, source_id="deep_deal")
            budget.record_http(page.get("LIVE_API_REQUESTS") or 0)
            packet["document_review"]["http_requests"] += page.get("LIVE_API_REQUESTS") or 0
            packet["LIVE_API_REQUESTS"] += page.get("LIVE_API_REQUESTS") or 0
            if page.get("ok") and page.get("body"):
                extracted = extract_solicitation_facts(page["body"], source_url=url)
                packet["extracted_facts"] = extracted.get("facts") or {}
                packet["provenance"] = extracted.get("provenance") or {}
                packet["document_review"]["reviewed"] = True
                reclass = reclassify_from_documents(
                    listing_classification=opp.get("product_classification"),
                    extracted=extracted,
                    deal_type=DEAL_COOP_MASTER,
                )
                packet.update(reclass)
        packet["operator_summary"] = build_operator_deal_packet_text(packet)
        packet["budgets"] = budget.to_dict()
        return packet

    if qualification.get("pre_deep_fit") == FIT_NOT_OUR_MODEL:
        packet["research_stop_reason"] = STOP_NOT_RESALE
        packet["bid_qualification_state"] = STATE_REJECTED
        packet["decision"] = DEC_REJECT
        packet["operator_queue_bucket"] = QUEUE_REJECTED
        packet.update(primary_next_action_for_deal(packet))
        packet["operator_summary"] = build_operator_deal_packet_text(packet)
        packet["budgets"] = budget.to_dict()
        return packet

    # 2) Document acquisition for admitted candidates
    url = packet["opportunity"]["source_url"]
    body = None
    if fetch_documents and url and budget.can_http() and (authorize_live or transport_client is not None):
        page = retrieve_solicitation_page(url, client=transport_client, authorize_live=authorize_live, source_id="deep_deal")
        n = page.get("LIVE_API_REQUESTS") or 0
        budget.record_http(n)
        packet["LIVE_API_REQUESTS"] += n
        packet["document_review"]["http_requests"] += n
        if page.get("ok"):
            body = page.get("body")
            packet["document_review"]["page_hash"] = page.get("content_hash")
            packet["document_review"]["retrieved_at"] = page.get("retrieved_at")
            links = discover_document_links(body or "", base_url=url, opportunity_id=str(sol))
            packet["document_review"]["documents"] = links[:20]
            packet["document_review"]["reviewed"] = True

    if body:
        extracted = extract_solicitation_facts(body, source_url=url, document_name="solicitation_page")
        packet["extracted_facts"] = extracted.get("facts") or {}
        packet["provenance"] = extracted.get("provenance") or {}
        # Re-qualify with document text
        qualification2 = qualify_candidate(opp, document_text=body[:15000])
        packet["deal_type"] = qualification2.get("deal_type")
        packet["pre_deep_fit"] = qualification2.get("pre_deep_fit")
        packet["admit_to_deep_research"] = qualification2.get("admit_to_deep_research")
        packet["operator_queue_bucket"] = qualification2.get("operator_queue_bucket")
        reclass = reclassify_from_documents(
            listing_classification=opp.get("product_classification"),
            extracted=extracted,
            deal_type=packet.get("deal_type"),
        )
        packet.update(reclass)

    # Construction-heavy after docs → stop or reclassify
        if packet.get("deal_type") == "CONSTRUCTION_OR_UPGRADE" or packet.get("pre_deep_fit") == FIT_POOR_LAUNCH:
            packet["research_stop_reason"] = packet.get("research_stop_reason") or (
                "hvac_upgrade_or_construction_appears_service_heavy_not_pure_resale"
            )
            packet["pre_deep_fit"] = FIT_POOR_LAUNCH
            packet["why_not_immediate"] = [
                "construction_or_upgrade_likely_requires_performance_labor",
                "not_pure_product_resale_without_document_proof_of_equipment_dominant_scope",
            ]

        for li in (packet["extracted_facts"].get("line_items") or [])[:20]:
            packet["line_items"].append(
                build_line_item_record(
                    line_number=li.get("line_number"),
                    description=li.get("description"),
                    manufacturer=li.get("manufacturer"),
                    model=li.get("model"),
                    part_number=li.get("part_number"),
                    quantity=li.get("quantity"),
                    uom=li.get("uom"),
                    source_document=li.get("source_document"),
                    product_certainty=packet["extracted_facts"].get("product_id_certainty") or "PRODUCT_UNKNOWN",
                )
            )

        packet["product_id_certainty"] = packet["extracted_facts"].get("product_id_certainty") or "PRODUCT_UNKNOWN"

    # 3) OpenAI advisory (optional, gated)
    if packet.get("admit_to_deep_research") and body and not force_openai_offline:
        advisory = run_openai_advisory(
            opportunity=opp,
            document_excerpt=body[:10000],
            task="summarize_requirements",
            budget=budget.openai,
            force_offline=force_openai_offline,
        )
        packet["openai_advisory"] = advisory
        packet["OpenAI"] += advisory.get("OpenAI") or 0
        if advisory.get("advisory", {}).get("proposed_facts"):
            merged = merge_ai_proposals_safely(
                existing_facts=packet["extracted_facts"],
                existing_provenance=packet["provenance"],
                proposals=advisory["advisory"]["proposed_facts"],
            )
            packet["extracted_facts"] = merged["facts"]
            packet["provenance"] = merged["provenance"]
            packet["ai_proposals"] = merged
    else:
        packet["openai_advisory"] = run_openai_advisory(
            opportunity=opp,
            document_excerpt=(body or "")[:2000],
            force_offline=True,
            budget=budget.openai,
        )

    # 4) Compliance
    compliance = evaluate_bid_compliance(opportunity=opp, extracted_facts=packet.get("extracted_facts") or {})
    packet["compliance"] = compliance
    packet["hard_blockers"] = compliance.get("hard_blockers") or []

    if compliance.get("hard_blockers"):
        packet["research_stop_reason"] = packet["research_stop_reason"] or f"compliance_hard:{compliance['hard_blockers'][0]}"

    # 5) Economics (honest UNKNOWN — no invented supplier cost)
    economics = build_deal_economics(
        expected_revenue=None,
        revenue_status="UNKNOWN",
        supplier_cost=None,
        supplier_cost_status="UNKNOWN",
        freight=None,
        freight_status="UNKNOWN",
    )
    packet["economics"] = economics
    packet["supplier_cost_known"] = False
    packet["max_supplier_cost"] = maximum_allowable_supplier_cost(expected_revenue=None)
    packet["min_bid_price"] = minimum_required_bid_price(supplier_cost=None)

    if economics.get("profit_confidence") == PROFIT_BELOW:
        packet["research_stop_reason"] = STOP_PROFIT_BELOW

    # 6) Funding rematch with known facts (supplier cost still unknown)
    req = build_funding_requirement(
        opportunity={
            **opp,
            "solicitation_number": sol,
            "product_classification": packet.get("document_classification") or opp.get("product_classification"),
            "deadline_viability": opp.get("deadline_viability"),
            "deadline_runway_days": opp.get("deadline_runway_days"),
        },
        economics={"estimated_bid_value": None, "estimated_supplier_cost": None},
        extras={
            "company_is_new_entity": True,
            "is_first_government_contract": True,
            "deal_qualified": packet.get("pre_deep_fit") == FIT_CORE_TRANSACTIONAL,
        },
    )
    sources = list((production_knowledge_base_seed().get("private_lenders") or []))
    matches = [match_funding_source(req, s) for s in sources]
    viable = [m for m in matches if m.get("match_status") != "REJECT"]
    viable.sort(key=lambda m: (-(m.get("match_score") or 0), m.get("source_name") or ""))
    top = viable[:3]
    sheets = []
    for i, m in enumerate(top, start=1):
        src = next((s for s in sources if s.get("source_name") == m.get("source_name")), {})
        sheets.append(build_operator_call_sheet(requirement=req, source=src, match_result=m, call_priority=i))
    conf = assess_deal_funding_confidence(requirement=req, match_results=matches)
    packet["funding"] = {
        "requirement": req,
        "top_matches": [
            {
                "company": s.get("source"),
                "match_status": s.get("match_status"),
                "phone": s.get("phone"),
                "why": s.get("why_this_source_may_match"),
                "verified": s.get("what_we_already_know"),
                "unknowns": s.get("what_we_still_need_to_verify"),
                "funding_secured": False,
            }
            for s in sheets
        ],
        "funding_confidence": conf.get("funding_confidence"),
        "funding_secured": False,
        "zero_cash_status": "NEEDS_VERIFICATION",
        "pg_status": "NEEDS_VERIFICATION",
        "personal_credit_status": "NEEDS_VERIFICATION",
        "first_contract_status": "NEEDS_VERIFICATION",
    }
    packet["funding_status"] = conf.get("funding_confidence")

    # Lender packet only if transactional and not stopped
    if (
        packet.get("pre_deep_fit") in {FIT_CORE_TRANSACTIONAL, "PRODUCT_PLUS_SUBCONTRACTABLE_WORK"}
        and not packet.get("research_stop_reason")
        and sheets
    ):
        src_match = next((m for m in matches if m.get("source_name") == sheets[0].get("source")), {})
        packet["lender_call_packet"] = build_lender_call_packet(
            opportunity=opp,
            economics=economics,
            funding_match=src_match,
            call_sheet=sheets[0],
        )

    # 7) Supplier packet
    packet["suppliers"] = {
        "search_plan": public_supplier_search_plan(packet.get("line_items") or []),
        "candidates": [],
        "contact_packet": build_supplier_contact_packet(
            opportunity=opp,
            line_items=packet.get("line_items"),
            delivery_location=(packet.get("extracted_facts") or {}).get("delivery_location"),
            delivery_deadline=(packet.get("extracted_facts") or {}).get("due_date_raw"),
        ),
        "outreach_performed": False,
    }

    # Critical unknowns
    unknowns = list(compliance.get("needs_verification") or [])
    unknowns.extend(["supplier_cost", "freight", "expected_revenue", "exact_product"][:])
    if not packet.get("document_review", {}).get("reviewed"):
        unknowns.append("solicitation_documents")
    packet["critical_unknowns"] = sorted(set(unknowns))

    packet["bid_qualification_state"] = determine_bid_state(packet)
    packet["decision"] = determine_decision(packet)
    packet.update(primary_next_action_for_deal(packet))
    packet["secondary_actions"] = _secondary_actions(packet)
    packet["why_could_fail"] = _why_could_fail(packet)
    packet["why_fit"] = _why_fit(packet)
    packet["operator_summary"] = build_operator_deal_packet_text(packet)
    packet["budgets"] = budget.to_dict()
    packet["submission_documents_known"] = False
    return packet


def _secondary_actions(packet: dict[str, Any]) -> list[str]:
    acts = []
    if not packet.get("document_review", {}).get("reviewed"):
        acts.append("REVIEW_SOLICITATION_DOCUMENTS")
    acts.append("IDENTIFY_EXACT_PRODUCT")
    acts.append("GET_SUPPLIER_QUOTE")
    acts.append("VERIFY_SUPPLIER_TERMS")
    acts.append("VERIFY_COMPLIANCE_REQUIREMENTS")
    if packet.get("lender_call_packet"):
        acts.append("VERIFY_FUNDING_WITH_LENDER")
    primary = packet.get("primary_next_action")
    return [a for a in acts if a != primary][:5]


def _why_fit(packet: dict[str, Any]) -> list[str]:
    out = []
    if packet.get("pre_deep_fit") == FIT_CORE_TRANSACTIONAL:
        out.append("one_time_product_resale_structure")
    if packet.get("document_classification") in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}:
        out.append("document_supports_product_class")
    if packet.get("deadline_viability") in {"GOOD", "PLENTY_OF_TIME", "RUSH"} or True:
        if packet.get("opportunity", {}).get("due_date"):
            out.append("deadline_present")
    return out or ["needs_more_evidence"]


def _why_could_fail(packet: dict[str, Any]) -> list[str]:
    risks = list(packet.get("hard_blockers") or [])
    risks.extend(packet.get("critical_unknowns") or [])
    if packet.get("pre_deep_fit") == FIT_POOR_LAUNCH:
        risks.append("may_be_construction_or_service_heavy")
    if packet.get("economics", {}).get("profit_confidence") == PROFIT_UNKNOWN:
        risks.append("profit_unproven")
    return sorted(set(risks))[:12]


def build_operator_deal_packet_text(packet: dict[str, Any]) -> str:
    """Human-readable deal packet for Brian."""
    opp = packet.get("opportunity") or {}
    econ = packet.get("economics") or {}
    funding = packet.get("funding") or {}
    comp = packet.get("compliance") or {}
    lines = [
        "=========================",
        "DEAL SUMMARY",
        "=========================",
        f"Agency: {opp.get('agency')}",
        f"Solicitation: {opp.get('solicitation_id')}",
        f"Title: {opp.get('title')}",
        f"Deal type: {packet.get('deal_type')}",
        f"Due date: {opp.get('due_date')}",
        f"Product classification (listing): {opp.get('product_classification_listing')}",
        f"Product classification (document): {packet.get('document_classification')}",
        f"Purchase structure / revenue certainty: {packet.get('revenue_certainty')}",
        f"Pre-deep fit: {packet.get('pre_deep_fit')}",
        f"Queue bucket: {packet.get('operator_queue_bucket')}",
        "",
        "=========================",
        "WHAT THEY ARE BUYING",
        "=========================",
    ]
    items = packet.get("line_items") or []
    if items:
        for it in items[:15]:
            lines.append(f"- Line {it.get('line_number')}: {it.get('description')} qty={it.get('quantity')}")
    else:
        lines.append(f"- {opp.get('title') or 'UNKNOWN'} (line items not yet extracted)")
        lines.append(f"- Product ID certainty: {packet.get('product_id_certainty') or 'PRODUCT_UNKNOWN'}")

    lines.extend(
        [
            "",
            "=========================",
            "WHY THIS FITS / DOESN'T",
            "=========================",
            f"Fit: {', '.join(packet.get('why_fit') or [])}",
            f"Doesn't / risks: {', '.join(packet.get('why_not_immediate') or packet.get('why_could_fail') or [])}",
            "",
            "=========================",
            "MONEY",
            "=========================",
            f"Expected revenue: {(econ.get('gross_revenue') or {}).get('value')} [{(econ.get('gross_revenue') or {}).get('status')}]",
            f"Supplier cost: {(econ.get('cogs') or {}).get('value')} [{(econ.get('cogs') or {}).get('status')}]",
            f"Total cost: {(econ.get('total_cost') or {}).get('value')} [{(econ.get('total_cost') or {}).get('status')}]",
            f"Expected profit: {(econ.get('expected_deal_profit') or {}).get('value')} [{(econ.get('expected_deal_profit') or {}).get('status')}]",
            f"Profit confidence: {econ.get('profit_confidence')}",
            f"Max allowable supplier cost: {(packet.get('max_supplier_cost') or {}).get('maximum_allowable_supplier_cost')}",
            f"Min required bid price: {(packet.get('min_bid_price') or {}).get('minimum_required_bid_price')}",
            packet.get("money_note") or "",
            "",
            "=========================",
            "FUNDING",
            "=========================",
            f"Working capital required: {(econ.get('working_capital_required') or {}).get('value')}",
            f"Funding confidence: {funding.get('funding_confidence')} (NOT secured)",
            f"Zero-cash / PG / personal-credit: {funding.get('zero_cash_status')} / {funding.get('pg_status')} / {funding.get('personal_credit_status')}",
        ]
    )
    for m in funding.get("top_matches") or []:
        lines.append(f"- {m.get('company')}: {m.get('match_status')} phone={m.get('phone')}")

    lines.extend(
        [
            "",
            "=========================",
            "SUPPLIERS",
            "=========================",
            f"Contact packet ready: {bool((packet.get('suppliers') or {}).get('contact_packet'))}",
            f"Outreach performed: False",
            "",
            "=========================",
            "COMPLIANCE",
            "=========================",
            f"Overall: {comp.get('overall_compliance')}",
            f"Pass/NA: {', '.join(comp.get('pass_or_na') or [])}",
            f"Needs verification: {', '.join(comp.get('needs_verification') or [])}",
            f"Hard blockers: {', '.join(comp.get('hard_blockers') or [])}",
            "",
            "=========================",
            "WHAT WE STILL DON'T KNOW",
            "=========================",
        ]
    )
    for u in packet.get("critical_unknowns") or []:
        lines.append(f"- {u}")

    lines.extend(
        [
            "",
            "=========================",
            "NEXT ACTION",
            "=========================",
            f"PRIMARY: {packet.get('primary_next_action')}",
            f"Secondary: {', '.join(packet.get('secondary_actions') or [])}",
            "",
            "=========================",
            "DECISION",
            "=========================",
            f"{packet.get('decision')}",
            f"Bid state: {packet.get('bid_qualification_state')}",
            f"Research stop: {packet.get('research_stop_reason')}",
        ]
    )
    return "\n".join(lines)


def load_tiny_top_candidates(artifacts_path: Path | None = None) -> list[dict[str, Any]]:
    base = artifacts_path or Path(__file__).resolve().parent / "artifacts" / "tiny_end_to_end_results.json"
    if not base.exists():
        return []
    data = json.loads(base.read_text(encoding="utf-8"))
    cards = data.get("top_candidate_cards") or []
    # Merge richer fields from top_candidates if present
    tops = data.get("top_candidates") or []
    by_sol = {}
    for t in tops:
        key = str(t.get("solicitation_number") or t.get("external_id") or "")
        by_sol[key] = t
    out = []
    for c in cards:
        sol = str(c.get("solicitation_id") or "")
        rich = by_sol.get(sol) or by_sol.get(f"sourcewell:{sol}") or {}
        merged = {**rich, **c}
        merged.setdefault("solicitation_number", c.get("solicitation_id"))
        merged.setdefault("detail_url", c.get("source_url"))
        merged.setdefault("source_id", c.get("source"))
        out.append(merged)
    return out


def run_deep_deal_research(
    *,
    candidates: list[dict[str, Any]] | None = None,
    authorize_live: bool = False,
    force_openai_offline: bool = True,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Run qualification + controlled deep research on a sample."""
    budget = ResearchBudget()
    rows = candidates if candidates is not None else load_tiny_top_candidates()
    rows = rows[:max_candidates]

    results = []
    for row in rows:
        results.append(
            research_one_deal(
                row,
                budget=budget,
                authorize_live=authorize_live,
                force_openai_offline=force_openai_offline,
                fetch_documents=True,
            )
        )

    # Also cheap-classify a slice of UNKNOWN survivors if present in TINY artifact
    cheap_improved = []
    tiny_path = Path(__file__).resolve().parent / "artifacts" / "tiny_end_to_end_results.json"
    if tiny_path.exists():
        tiny = json.loads(tiny_path.read_text(encoding="utf-8"))
        # Sample unknowns from discovery opportunities if available
        opps = (tiny.get("discovery") or {}).get("opportunities") or tiny.get("top_candidates") or []
        unknowns = [o for o in opps if (o.get("product_classification") or "UNKNOWN") == "UNKNOWN"][:30]
        from deep_deal_qualification import cheap_second_stage_classify

        for u in unknowns:
            c = cheap_second_stage_classify(u)
            if c.get("classification_changed"):
                cheap_improved.append({"title": u.get("title"), "external_id": u.get("external_id"), **c})

    queues: dict[str, list[str]] = {
        QUEUE_IMMEDIATE: [],
        QUEUE_STRATEGIC: [],
        QUEUE_REJECTED: [],
        "DEEP_RESEARCH": [],
        "NEEDS_CHEAP_CLASSIFICATION": [],
    }
    for r in results:
        bucket = r.get("operator_queue_bucket") or "DEEP_RESEARCH"
        queues.setdefault(bucket, []).append(str(r.get("opportunity", {}).get("solicitation_id")))

    return {
        "run_at": _utc(),
        "candidate_count": len(results),
        "results": results,
        "queues": queues,
        "cheap_unknown_improvements": cheap_improved,
        "cheap_unknown_improved_count": len(cheap_improved),
        "budgets": budget.to_dict(),
        "SAM": 0,
        "OpenAI": budget.openai.used,
        "USAspending": 0,
        "paid": 0,
        "LIVE_API_REQUESTS": budget.public_http,
        "lender_outreach_performed": False,
        "supplier_outreach_performed": False,
        "bid_submissions": 0,
    }


def write_deep_deal_artifacts(result: dict[str, Any], artifacts_dir: Path | None = None) -> dict[str, str]:
    base = artifacts_dir or Path(__file__).resolve().parent / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    packets_dir = base / "deal_packets"
    packets_dir.mkdir(parents=True, exist_ok=True)

    json_path = base / "deep_deal_research_results.json"
    csv_path = base / "deep_deal_research_queue.csv"
    md_path = base / "deep_deal_research_report.md"

    serializable = json.loads(json.dumps(result, default=str))
    json_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    fieldnames = [
        "solicitation_id",
        "agency",
        "title",
        "deal_type",
        "pre_deep_fit",
        "queue_bucket",
        "bid_state",
        "decision",
        "primary_next_action",
        "profit_confidence",
        "funding_confidence",
        "research_stop_reason",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in result.get("results") or []:
            opp = r.get("opportunity") or {}
            w.writerow(
                {
                    "solicitation_id": opp.get("solicitation_id"),
                    "agency": opp.get("agency"),
                    "title": opp.get("title"),
                    "deal_type": r.get("deal_type"),
                    "pre_deep_fit": r.get("pre_deep_fit"),
                    "queue_bucket": r.get("operator_queue_bucket"),
                    "bid_state": r.get("bid_qualification_state"),
                    "decision": r.get("decision"),
                    "primary_next_action": r.get("primary_next_action"),
                    "profit_confidence": (r.get("economics") or {}).get("profit_confidence"),
                    "funding_confidence": (r.get("funding") or {}).get("funding_confidence"),
                    "research_stop_reason": r.get("research_stop_reason"),
                }
            )

    lines = [
        "# Deep Deal Research + Bid Qualification Report",
        "",
        f"Run at: {result.get('run_at')}",
        "",
        "## Budgets",
        f"- Public HTTP: {result.get('LIVE_API_REQUESTS')}",
        f"- OpenAI: {result.get('OpenAI')}",
        f"- SAM: {result.get('SAM')}",
        f"- USAspending: {result.get('USAspending')}",
        f"- Lender outreach: {result.get('lender_outreach_performed')}",
        f"- Supplier outreach: {result.get('supplier_outreach_performed')}",
        "",
        f"## Cheap UNKNOWN improvements: {result.get('cheap_unknown_improved_count')}",
        "",
        "## Queue separation",
    ]
    for k, v in (result.get("queues") or {}).items():
        lines.append(f"- {k}: {', '.join(v) if v else '(none)'}")
    lines.append("")
    lines.append("## Deals")
    for r in result.get("results") or []:
        opp = r.get("opportunity") or {}
        sid = _safe_id(opp.get("solicitation_id"))
        packet_json = packets_dir / f"{sid}.json"
        packet_md = packets_dir / f"{sid}.md"
        packet_json.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
        packet_md.write_text(r.get("operator_summary") or "", encoding="utf-8")
        lines.extend(
            [
                f"### {opp.get('solicitation_id')} — {opp.get('title')}",
                f"- Deal type: {r.get('deal_type')}",
                f"- Fit: {r.get('pre_deep_fit')}",
                f"- Decision: {r.get('decision')}",
                f"- Next: {r.get('primary_next_action')}",
                f"- Stop: {r.get('research_stop_reason')}",
                f"- Packet: artifacts/deal_packets/{sid}.md",
                "",
            ]
        )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path), "packets_dir": str(packets_dir)}
