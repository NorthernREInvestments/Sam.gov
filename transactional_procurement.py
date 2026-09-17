"""Transactional procurement intelligence — package → BOM → suppliers → economics.

No supplier/lender outreach. No bid submission. SAM/USAspending/OpenAI default 0.
Financing runs only after meaningful acquisition-cost evidence exists.
"""

from __future__ import annotations
from application_clock import complete_run_metadata, now_utc, start_run_metadata

import csv
import html as html_lib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deep_deal_constants import (
    DEAL_ONE_TIME_PRODUCT,
    DEAL_PRODUCT_PLUS_INSTALL,
    DEAL_REQUIREMENTS,
    DEAL_SERVICE,
    PROFIT_BELOW,
    PROFIT_ESTIMATED,
    PROFIT_POTENTIAL,
    PROFIT_UNKNOWN,
    STATE_BID_READY,
)
from deep_deal_economics import build_deal_economics
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.sciquest import parse_sciquest_public_events
from economic_integrity import min_actual_profit_usd
from funding_path_intelligence import run_funding_path_workflow
from solicitation_package_retrieval import (
    enumerate_listed_attachments_from_text,
    fetch_document,
    resolve_document_authority,
)
from solicitation_package_constants import (
    ACCESS_LOGIN_REQUIRED,
    ACCESS_PUBLIC_FETCHED,
    AUTH_BLOCKED,
    COMPLETE_ENOUGH_FOR_PRODUCT_ID,
    CRITICAL_DOCUMENT_MISSING,
    DEC_CONTINUE,
    DEC_GET_MISSING_DOCUMENT,
    DEC_GET_SUPPLIER_QUOTE,
    DEC_MANUAL_DOCUMENT_REVIEW,
    DEC_REJECT,
    LIKELY_COMPLETE,
    NEXT_FREIGHT,
    NEXT_GET_MISSING_DOC,
    NEXT_MANUAL_REVIEW,
    NEXT_REVENUE,
    NEXT_SUPPLIER_QUOTE,
    PARTIAL,
    PRICE_NONE,
    PRICE_QUOTE_REQUIRED,
    PROCUREMENT_HARD_MAX_HTTP,
    PROCUREMENT_TARGET_HTTP,
    PRODUCT_BRAND_OR_EQUAL,
    PRODUCT_INSUFFICIENT,
    PRODUCT_MULTIPLE,
    PRODUCT_SPEC_COMMODITY,
    REV_UNKNOWN,
)
from transactional_bom import (
    assess_document_completeness,
    build_transactional_requirement,
    extract_delivery_and_terms,
    extract_sciquest_product_line_items,
    identify_product,
)

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
EVIDENCE_DIR = ARTIFACTS_DIR / "transactional_procurement_evidence"
PACKETS_DIR = ARTIFACTS_DIR / "transactional_procurement_packets"

PRIORITY_SOLICITATIONS = [
    "645-DOTRFB-2975-2027",  # tungsten blades first
    "645-DOTRFB-3046-2027",
    "005-RFB-3030-2027",
]

IOWA_LIST_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"


def _utc() -> str:
    return now_utc().isoformat()


def _strip_runtime(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove bulky content bytes from serializable records."""
    out = {k: v for k, v in doc.items() if k not in {"content", "text"}}
    if doc.get("text"):
        out["text_length"] = len(doc["text"])
        out["text_preview"] = doc["text"][:500]
    return out


# ---------------------------------------------------------------------------
# Deal-type correction from documents
# ---------------------------------------------------------------------------


def correct_deal_type_from_documents(
    *,
    title: str | None,
    line_items: list[dict[str, Any]],
    prior_deal_type: str | None = None,
) -> dict[str, Any]:
    product = [li for li in line_items if not li.get("is_service_line")]
    service = [li for li in line_items if li.get("is_service_line")]
    reasons: list[str] = []

    # Catalog-style qty=1 across many badge types + repair services
    if service and product:
        qty_ones = all((li.get("quantity") or 0) == 1 for li in product)
        if qty_ones and any("repair" in (li.get("description") or "").lower() for li in service):
            return {
                "deal_type": DEAL_REQUIREMENTS,
                "corrected": True,
                "prior": prior_deal_type,
                "reasons": [
                    "product_lines_qty_1_catalog_style",
                    "service_repair_lines_present",
                    "not_pure_one_time_product_resale",
                ],
                "transactional_resale_fit": "POOR",
            }
        return {
            "deal_type": DEAL_PRODUCT_PLUS_INSTALL,
            "corrected": True,
            "prior": prior_deal_type,
            "reasons": ["product_and_service_lines"],
            "transactional_resale_fit": "MIXED",
        }
    if service and not product:
        return {
            "deal_type": DEAL_SERVICE,
            "corrected": True,
            "prior": prior_deal_type,
            "reasons": ["service_lines_only"],
            "transactional_resale_fit": "REJECT",
        }
    if product:
        # Seed: unit-priced multi-line commodity still one-time product RFB
        return {
            "deal_type": DEAL_ONE_TIME_PRODUCT,
            "corrected": prior_deal_type not in (None, DEAL_ONE_TIME_PRODUCT),
            "prior": prior_deal_type,
            "reasons": ["product_line_items_only"],
            "transactional_resale_fit": "GOOD",
        }
    return {
        "deal_type": prior_deal_type or "UNKNOWN_DEAL_TYPE",
        "corrected": False,
        "prior": prior_deal_type,
        "reasons": reasons or ["insufficient_line_items"],
        "transactional_resale_fit": "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# Supplier discovery (public, deterministic seed list + optional live pages)
# ---------------------------------------------------------------------------


def discover_suppliers_for_product(
    *,
    product_id: dict[str, Any],
    title: str | None = None,
    max_suppliers: int = 4,
) -> list[dict[str, Any]]:
    """
    Return 2–4 credible supplier candidates from curated public knowledge.
    Does NOT contact suppliers. Does NOT invent authorization or prices.
    """
    desc = (product_id.get("sourcing_description") or title or "").lower()
    suppliers: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        if len(suppliers) >= max_suppliers:
            return
        suppliers.append(
            {
                "supplier_name": kwargs.get("supplier_name"),
                "supplier_type": kwargs.get("supplier_type", "ESTABLISHED_DISTRIBUTOR"),
                "website": kwargs.get("website"),
                "public_phone": kwargs.get("public_phone"),
                "product_url": kwargs.get("product_url"),
                "manufacturer": kwargs.get("manufacturer"),
                "model": kwargs.get("model"),
                "part_number": kwargs.get("part_number"),
                "public_price": None,
                "price_basis": PRICE_QUOTE_REQUIRED,
                "quantity_break": None,
                "stock_status": "UNKNOWN",
                "lead_time": "UNKNOWN",
                "minimum_order": "UNKNOWN",
                "shipping_terms": "UNKNOWN",
                "direct_ship_evidence": None,
                "government_sales_evidence": kwargs.get("government_sales_evidence"),
                "authorized_distributor_evidence": None,
                "quote_required": True,
                "evidence_url": kwargs.get("evidence_url") or kwargs.get("website"),
                "evidence_date": None,
                "confidence": kwargs.get("confidence", "ASSESSMENT"),
                "notes": kwargs.get("notes"),
            }
        )

    if "tungsten" in desc or "carbide" in desc or "snow" in desc and "blade" in desc:
        add(
            supplier_name="Winter Equipment Company",
            supplier_type="SPECIALTY_SUPPLIER",
            website="https://www.winterequipment.com",
            evidence_url="https://www.winterequipment.com",
            notes="Specialty snowplow cutting edges / carbide blades — public catalog presence",
            confidence="ASSESSMENT",
            government_sales_evidence="UNKNOWN",
        )
        add(
            supplier_name="Kennametal Inc.",
            supplier_type="MANUFACTURER",
            website="https://www.kennametal.com",
            manufacturer="Kennametal",
            notes="Industrial carbide wear products manufacturer — quote required for plow edges",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="Valley Blades Limited",
            supplier_type="MANUFACTURER",
            website="https://www.valleyblades.com",
            notes="Snowplow blade manufacturer — public site; quote required",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="Nordic Auto Plow / industrial distributors via MSC or Grainger category search",
            supplier_type="ESTABLISHED_DISTRIBUTOR",
            website="https://www.grainger.com",
            notes="Large industrial distributor may stock plow edges; verify exact Iowa DOT stock codes",
            confidence="LOW",
        )
    elif "seed" in desc or "bluestem" in desc or "wildflower" in desc or "grama" in desc:
        add(
            supplier_name="Ion Exchange Native Seed",
            supplier_type="SPECIALTY_SUPPLIER",
            website="https://www.ionxchange.com",
            notes="Native seed supplier — Iowa-region specialty; quote/availability required",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="Prairie Moon Nursery",
            supplier_type="SPECIALTY_SUPPLIER",
            website="https://www.prairiemoon.com",
            notes="Native seed catalog; public prices may exist for some species — verify quantities",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="Ernst Conservation Seeds",
            supplier_type="SPECIALTY_SUPPLIER",
            website="https://www.ernstseed.com",
            notes="Conservation/native seed wholesaler — quote required for DOT volumes",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="Iowa Prairie Seed / regional growers",
            supplier_type="SPECIALTY_SUPPLIER",
            website=None,
            notes="Source-identified / Iowa-grown requirements may constrain suppliers — see seed specs attachment",
            confidence="LOW",
        )
    elif "badge" in desc:
        add(
            supplier_name="Blackinton",
            supplier_type="MANUFACTURER",
            website="https://www.blackinton.com",
            notes="Law-enforcement badge manufacturer — styles referenced in solicitation; quote required",
            confidence="ASSESSMENT",
        )
        add(
            supplier_name="V.H. Blackinton & Co. authorized dealers",
            supplier_type="AUTHORIZED_DISTRIBUTOR",
            website="https://www.blackinton.com",
            notes="Authorization not verified — do not invent dealer status",
            confidence="LOW",
        )
        add(
            supplier_name="Smith & Warren",
            supplier_type="MANUFACTURER",
            website="https://www.smithwarren.com",
            notes="Badge manufacturer alternate — brand-or-equal unknown until spec doc retrieved",
            confidence="ASSESSMENT",
        )
    else:
        add(
            supplier_name="UNSPECIFIED_INDUSTRIAL_DISTRIBUTOR",
            supplier_type="ESTABLISHED_DISTRIBUTOR",
            website=None,
            notes="Product class not mapped to curated supplier set",
            confidence="LOW",
        )

    return suppliers[:max_suppliers]


def build_supplier_quote_packet(
    *,
    solicitation_number: str,
    agency: str | None,
    product_id: dict[str, Any],
    line_items: list[dict[str, Any]],
    terms: dict[str, Any],
    supplier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exact quote request packet for operator use — DO NOT SEND automatically."""
    delivery = (terms.get("delivery_location") or {}).get("value")
    deadline = (terms.get("bid_deadline") or {}).get("value")
    product_lines = [li for li in line_items if not li.get("is_service_line")]
    return {
        "do_not_send_automatically": True,
        "outreach_performed": False,
        "solicitation_number": solicitation_number,
        "government_agency": agency,
        "supplier_name": (supplier or {}).get("supplier_name"),
        "exact_product_or_specification": product_id.get("sourcing_description"),
        "line_items": [
            {
                "line": li.get("line_number"),
                "item_number": li.get("CLIN_or_item_number"),
                "description": li.get("description"),
                "quantity": li.get("quantity"),
                "uom": li.get("unit_of_measure"),
            }
            for li in product_lines
        ],
        "quantity_summary": sum((li.get("quantity") or 0) for li in product_lines) or None,
        "unit_of_measure": "mixed" if len({li.get("unit_of_measure") for li in product_lines}) > 1 else (
            product_lines[0].get("unit_of_measure") if product_lines else None
        ),
        "delivery_destination": delivery,
        "required_delivery_date": (terms.get("delivery_deadline") or {}).get("value") or "UNKNOWN",
        "bid_deadline_context": deadline,
        "requested_quote_validity_period": "through bid deadline + 30 days (operator to confirm)",
        "requests": [
            "unit_and_extended_pricing_per_line",
            "freight_to_delivery_destination",
            "direct_ship_to_government_facility_capability",
            "lead_time_after_receipt_of_order",
            "stock_availability",
            "manufacturer_authorization_if_applicable",
            "payment_terms",
            "deposit_requirement",
            "accept_payment_from_third_party_PO_or_contract_financier",
            "warranty_confirmation",
            "country_of_origin_and_compliance",
            "mill_certifications_if_required",
        ],
        "notes_for_brian": [
            "Do not fabricate supplier answers.",
            "Mill certifications required to enter Iowa DOT blade bid.",
            "Samples may be required within 5 business days of request.",
        ],
        "generated_at": _utc(),
    }


# ---------------------------------------------------------------------------
# Economics + funding (downstream)
# ---------------------------------------------------------------------------


def compute_procurement_economics(
    *,
    line_items: list[dict[str, Any]],
    public_price_evidence: dict[str, Any] | None = None,
    freight: float | None = None,
    freight_status: str = "UNKNOWN",
    expected_revenue: float | None = None,
    revenue_status: str = "UNKNOWN",
) -> dict[str, Any]:
    """Feed evidence into economics engine. UNKNOWN never becomes 0."""
    price_ev = public_price_evidence or {}
    unit = price_ev.get("unit_price")
    qty = sum(
        (li.get("quantity") or 0)
        for li in line_items
        if not li.get("is_service_line") and li.get("quantity") is not None
    )
    supplier_cost = None
    supplier_status = "UNKNOWN"
    if unit is not None and qty:
        try:
            supplier_cost = float(unit) * float(qty)
            supplier_status = price_ev.get("confidence") or "ESTIMATED"
            if supplier_status not in {"VERIFIED", "CALCULATED", "ESTIMATED"}:
                supplier_status = "ESTIMATED"
        except (TypeError, ValueError):
            supplier_cost = None

    # Range support
    unit_low = price_ev.get("unit_price_low")
    unit_high = price_ev.get("unit_price_high")
    cost_range = None
    if unit_low is not None and unit_high is not None and qty:
        cost_range = {
            "low": float(unit_low) * float(qty),
            "high": float(unit_high) * float(qty),
        }

    # Map to economics-engine statuses (COGS requires VERIFIED/CALCULATED)
    eng_supplier_status = (
        "CALCULATED" if supplier_cost is not None else "UNKNOWN"
    )
    eng_revenue_status = "UNKNOWN"
    if expected_revenue is not None:
        if revenue_status in {"VERIFIED", "CALCULATED", "ESTIMATED", "KNOWN_BUDGET", "KNOWN_HISTORICAL_PRICE", "KNOWN_PRIOR_AWARD", "ESTIMATED_FROM_EVIDENCE"}:
            eng_revenue_status = "ESTIMATED" if revenue_status not in {"VERIFIED", "CALCULATED"} else revenue_status
        else:
            eng_revenue_status = "ESTIMATED"

    econ = build_deal_economics(
        expected_revenue=expected_revenue,
        revenue_status=eng_revenue_status,
        supplier_cost=supplier_cost,
        supplier_cost_status=eng_supplier_status,
        freight=freight,
        freight_status=("CALCULATED" if freight is not None else "UNKNOWN"),
    )

    # Working capital only when meaningful cost exists
    wc = None
    wc_status = "UNKNOWN"
    if supplier_cost is not None:
        wc = supplier_cost + (freight if freight is not None else 0.0)
        wc_status = "ESTIMATED" if freight is not None else "PARTIAL_SUPPLIER_ONLY"
    elif cost_range:
        wc = cost_range
        wc_status = "RANGE_ESTIMATE"

    target = min_actual_profit_usd()
    profit_state = econ.get("profit_confidence") or PROFIT_UNKNOWN
    ten_k = "UNKNOWN"
    profit_val = None
    for key in ("expected_deal_profit", "gross_profit", "estimated_profit"):
        node = econ.get(key)
        if isinstance(node, dict) and node.get("value") is not None:
            profit_val = node["value"]
            break
    # Local fallback when both cost and revenue known
    if profit_val is None and supplier_cost is not None and expected_revenue is not None:
        profit_val = expected_revenue - supplier_cost - (freight or 0.0)
        profit_state = PROFIT_ESTIMATED

    if profit_val is not None:
        ten_k = "PASS" if profit_val >= target else "FAIL"
        if profit_val < target:
            profit_state = PROFIT_BELOW
    elif cost_range and expected_revenue is not None:
        p_low = expected_revenue - cost_range["high"] - (freight or 0)
        p_high = expected_revenue - cost_range["low"] - (freight or 0)
        if p_low >= target:
            ten_k = "PASS"
            profit_state = PROFIT_ESTIMATED
        elif p_high >= target:
            ten_k = "POSSIBLE"
            profit_state = PROFIT_POTENTIAL
        else:
            ten_k = "FAIL"
            profit_state = PROFIT_BELOW
    elif supplier_cost is None and expected_revenue is None:
        ten_k = "UNKNOWN"
        profit_state = PROFIT_UNKNOWN

    return {
        "economics": econ,
        "quantity_total": qty or None,
        "supplier_cost": supplier_cost,
        "supplier_cost_status": supplier_status if supplier_cost is not None else "UNKNOWN",
        "supplier_cost_range": cost_range,
        "freight": freight,
        "freight_status": "UNKNOWN" if freight is None else freight_status,
        "landed_cost": (
            (supplier_cost + freight)
            if supplier_cost is not None and freight is not None
            else None
        ),
        "landed_cost_status": (
            "CALCULATED"
            if supplier_cost is not None and freight is not None
            else "UNKNOWN"
        ),
        "expected_revenue": expected_revenue,
        "revenue_status": revenue_status if expected_revenue is not None else REV_UNKNOWN,
        "estimated_profit": profit_val,
        "profit_state": profit_state,
        "ten_k_target": ten_k,
        "working_capital": wc,
        "working_capital_status": wc_status,
        "funding_downstream_only": True,
        "funding_warranted": supplier_cost is not None or bool(cost_range),
    }


def maybe_run_funding(economics_block: dict[str, Any]) -> dict[str, Any]:
    """Funding only after meaningful cost. Match ≠ approval. No outreach."""
    if not economics_block.get("funding_warranted"):
        return {
            "status": "NOT_YET_NEEDED",
            "reason": "supplier_cost_unknown",
            "lender_outreach": False,
            "approval": False,
            "matches": [],
        }
    wc = economics_block.get("working_capital")
    amount = None
    if isinstance(wc, (int, float)):
        amount = float(wc)
    elif isinstance(wc, dict) and wc.get("high") is not None:
        amount = float(wc["high"])
    if amount is None:
        return {
            "status": "NOT_YET_NEEDED",
            "reason": "working_capital_unknown",
            "lender_outreach": False,
            "approval": False,
            "matches": [],
        }
    try:
        wf = run_funding_path_workflow(
            opportunity={"working_capital_required": amount},
            economics={
                "working_capital_required": {"value": amount, "status": "ESTIMATED"},
                "supplier_product_cost": {
                    "value": economics_block.get("supplier_cost"),
                    "status": economics_block.get("supplier_cost_status"),
                },
            },
            deal_qualified=False,
        )
    except Exception as exc:
        return {
            "status": "NEEDS_VERIFICATION",
            "reason": f"funding_workflow_error:{exc}",
            "lender_outreach": False,
            "approval": False,
            "matches": [],
        }

    return {
        "status": "NEEDS_VERIFICATION",
        "reason": "apparent_matches_only_not_approval",
        "lender_outreach": False,
        "approval": False,
        "match_is_not_approval": True,
        "working_capital_input": amount,
        "workflow": wf if isinstance(wf, dict) else {"raw": str(wf)[:500]},
        "matches": (wf or {}).get("matches")
        or (wf or {}).get("apparent_matches")
        or (wf or {}).get("source_matches")
        or [],
    }


def evaluate_bid_ready_strict(packet: dict[str, Any]) -> dict[str, Any]:
    """BID_READY remains strict — product ID alone is never enough."""
    blockers: list[str] = []
    req = packet.get("requirement") or {}
    econ = packet.get("economics_block") or {}
    funding = packet.get("funding") or {}
    compliance = packet.get("compliance") or {}

    if (req.get("product_identification") or {}).get("product_id_state") in {
        None,
        PRODUCT_INSUFFICIENT,
    }:
        blockers.append("product_not_identified")
    if not req.get("line_items"):
        blockers.append("line_items_missing")
    if econ.get("supplier_cost") is None and not econ.get("supplier_cost_range"):
        blockers.append("acquisition_cost_unknown")
    if econ.get("freight_status") == "UNKNOWN":
        blockers.append("freight_unknown")
    if econ.get("expected_revenue") is None:
        blockers.append("expected_revenue_unknown")
    if econ.get("profit_state") in {None, PROFIT_UNKNOWN}:
        blockers.append("profit_unknown")
    if econ.get("working_capital_status") == "UNKNOWN":
        blockers.append("working_capital_unknown")
    if funding.get("status") not in {"SECURED", "VERIFIED"}:  # we never mark secured here
        blockers.append("funding_not_verified")
    if compliance.get("entity_eligibility") != "PASS":
        blockers.append("entity_eligibility_unverified")
    if compliance.get("set_aside") not in {"PASS", "NOT_APPLICABLE", "UNRESTRICTED"}:
        blockers.append("set_aside_unverified")

    return {
        "bid_ready": False,  # never true in this pass without full verification
        "would_be_bid_ready": False,
        "state": "NOT_BID_READY",
        "blockers": blockers,
        "note": "BID_READY unchanged/strict — identification and supplier path alone insufficient",
        "forbidden_shortcut": STATE_BID_READY,
    }


# ---------------------------------------------------------------------------
# Analyze one deal
# ---------------------------------------------------------------------------


def analyze_solicitation_package(
    solicitation_number: str,
    *,
    client: PublicProcurementHttpClient,
    list_html: str | None = None,
    list_url: str = IOWA_LIST_URL,
    title: str | None = None,
    agency: str | None = None,
    prior_deal_type: str | None = DEAL_ONE_TIME_PRODUCT,
    cache_dir: Path | None = None,
    allow_supplier_http: bool = False,
) -> dict[str, Any]:
    cache_dir = cache_dir or (EVIDENCE_DIR / solicitation_number)
    cache_dir.mkdir(parents=True, exist_ok=True)

    documents: list[dict[str, Any]] = []
    listing_retrieved = False
    row_html = None

    if list_html:
        listing_retrieved = True
        rows = re.findall(r"<tr\b[^>]*>.*?</tr>", list_html, re.I | re.S)
        for row in rows:
            if solicitation_number in row or solicitation_number.split("-")[-2] in row and solicitation_number[:3] in row:
                # safer: exact number present
                if solicitation_number in re.sub(r"\s+", "", row) or solicitation_number in row:
                    row_html = row
                    break
        if row_html is None:
            for row in rows:
                if solicitation_number in row:
                    row_html = row
                    break

    signed_pdf = None
    detail_url = None
    if row_html:
        for h in [html_lib.unescape(x) for x in re.findall(r'href="([^"]+)"', row_html)]:
            if "event.pdf" in h and "Sourcingevent" in h:
                signed_pdf = h
            if "ViewSourcingEvent" in h:
                detail_url = h
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row_html))
        if not title:
            tm = re.search(r"Open\s+(.+?)\s+\1", plain)
            if tm:
                title = tm.group(1).strip()
        if not agency and "Iowa" in plain:
            agency = "State of Iowa Department of Administrative Services"

    # Fetch listing page counted externally; fetch event PDF
    event_doc = None
    if signed_pdf:
        event_doc = fetch_document(
            signed_pdf,
            client=client,
            solicitation_number=solicitation_number,
            source_portal="sciquest_iowa",
            title=f"{solicitation_number}-event.pdf",
            cache_dir=cache_dir,
            document_class="SOLICITATION",
        )
        documents.append(event_doc)
    else:
        documents.append(
            {
                "solicitation_number": solicitation_number,
                "source_portal": "sciquest_iowa",
                "document_title": "event.pdf",
                "document_url": None,
                "access_status": "NOT_FOUND",
                "fetch_status": "MISSING_FROM_LISTING",
                "document_class": "SOLICITATION",
                "provenance": "listing_row_absent",
                "retrieval_timestamp": _utc(),
            }
        )

    # Detail page (usually login)
    if detail_url:
        detail_doc = fetch_document(
            detail_url,
            client=client,
            solicitation_number=solicitation_number,
            source_portal="sciquest_iowa",
            title="ViewSourcingEvent",
            cache_dir=cache_dir,
            document_class="SOLICITATION",
        )
        documents.append(detail_doc)

    text = (event_doc or {}).get("text") or ""
    # Named buyer attachments (no public URL)
    listed = enumerate_listed_attachments_from_text(
        text,
        solicitation_number=solicitation_number,
        source_portal="sciquest_iowa",
        parent_document=(event_doc or {}).get("document_title"),
    )
    documents.extend(listed)

    documents = resolve_document_authority(documents)

    line_items = extract_sciquest_product_line_items(
        text, source_document=(event_doc or {}).get("document_title")
    )
    terms = extract_delivery_and_terms(
        text, source_document=(event_doc or {}).get("document_title")
    )
    # Propagate delivery onto lines
    loc = (terms.get("delivery_location") or {}).get("value")
    fob = (terms.get("FOB_terms") or {}).get("value")
    brand_eq = (terms.get("brand_name_or_equal") or {}).get("value")
    for li in line_items:
        if loc:
            li["delivery_location"] = loc
        if fob:
            li["FOB_terms"] = fob
        if brand_eq:
            li["brand_name_or_equal"] = True
            li["approved_equal_language"] = (terms.get("approved_equal_language") or {}).get("value")

    product_id = identify_product(line_items=line_items, terms=terms, title=title)
    completeness = assess_document_completeness(
        documents=[_strip_runtime(d) for d in documents],
        line_items=line_items,
        terms=terms,
        product_id=product_id,
    )
    deal_corr = correct_deal_type_from_documents(
        title=title, line_items=line_items, prior_deal_type=prior_deal_type
    )

    suppliers: list[dict[str, Any]] = []
    quote_packets: list[dict[str, Any]] = []
    if (
        completeness.get("document_completeness")
        in {COMPLETE_ENOUGH_FOR_PRODUCT_ID, LIKELY_COMPLETE}
        and product_id.get("product_id_state") != PRODUCT_INSUFFICIENT
        and deal_corr.get("transactional_resale_fit") != "REJECT"
    ):
        suppliers = discover_suppliers_for_product(product_id=product_id, title=title, max_suppliers=4)
        if len(suppliers) < 2 and product_id.get("product_id_state") != PRODUCT_INSUFFICIENT:
            # still return what we have
            pass
        for s in suppliers[:4]:
            quote_packets.append(
                build_supplier_quote_packet(
                    solicitation_number=solicitation_number,
                    agency=agency,
                    product_id=product_id,
                    line_items=line_items,
                    terms=terms,
                    supplier=s,
                )
            )

    # Public pricing — this pass: no invented prices; optional live fetch disabled by default
    public_price = {
        "price_evidence": PRICE_NONE if not allow_supplier_http else PRICE_QUOTE_REQUIRED,
        "unit_price": None,
        "confidence": "UNKNOWN",
        "source": None,
        "notes": "no_defensible_public_unit_price_captured_without_fabrication",
    }
    if suppliers:
        public_price["price_evidence"] = PRICE_QUOTE_REQUIRED
        public_price["notes"] = "suppliers_identified_quote_required"

    # Government revenue — local/cached only; no USAspending
    gov_value = {
        "revenue_confidence": REV_UNKNOWN,
        "expected_revenue": None,
        "evidence": [],
        "notes": "no_budget_or_historical_award_in_local_cache_for_this_solicitation",
    }

    economics_block = compute_procurement_economics(
        line_items=line_items,
        public_price_evidence=public_price,
        freight=None,
        freight_status="UNKNOWN",
        expected_revenue=gov_value["expected_revenue"],
        revenue_status=gov_value["revenue_confidence"],
    )
    funding = maybe_run_funding(economics_block)

    compliance = {
        "entity_eligibility": "UNKNOWN",
        "set_aside": "UNKNOWN",
        "nmr_reseller": "UNKNOWN",
        "mill_certification_required": bool((terms.get("required_certifications") or {}).get("value")),
        "samples_may_be_required": bool((terms.get("samples_may_be_required") or {}).get("value")),
        "bid_lines_tied": bool((terms.get("bid_lines_tied") or {}).get("value")),
        "notes": [],
    }
    if compliance["mill_certification_required"]:
        compliance["notes"].append("mill_certifications_required_to_enter_bid")
    if compliance["samples_may_be_required"]:
        compliance["notes"].append("DOT_may_require_samples_within_5_business_days")

    requirement = build_transactional_requirement(
        solicitation_number=solicitation_number,
        agency=agency,
        buyer=(terms.get("payment_terms") and None) or None,
        source="sciquest_iowa",
        deal_type=deal_corr["deal_type"],
        line_items=line_items,
        terms=terms,
        product_id=product_id,
        completeness=completeness,
    )

    # Contact from PDF
    contact = None
    cm = re.search(r"Contacts\s*\n\s*([A-Z][^\n]+)\s*\n\s*(\S+@\S+)", text)
    if cm:
        contact = {"name": cm.group(1).strip(), "email": cm.group(2).strip()}
        requirement["buyer"] = contact["name"]

    decision, next_actions = decide_next(completeness, product_id, deal_corr, economics_block, suppliers)

    packet = {
        "solicitation_number": solicitation_number,
        "title": title,
        "agency": agency,
        "contact": contact,
        "deal_type": deal_corr["deal_type"],
        "deal_type_correction": deal_corr,
        "listing_page_retrieved": listing_retrieved,
        "documents": [_strip_runtime(d) for d in documents],
        "document_retrieval_summary": summarize_retrieval(documents, listed),
        "requirement": requirement,
        "suppliers": suppliers,
        "supplier_count": len(suppliers),
        "public_price": public_price,
        "government_value_evidence": gov_value,
        "economics_block": economics_block,
        "funding": funding,
        "compliance": compliance,
        "quote_packets": quote_packets,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
        "next_actions": next_actions,
        "decision": decision,
        "analyzed_at": _utc(),
        "bidder_priced": True,
    }
    packet["bid_ready_evaluation"] = evaluate_bid_ready_strict(packet)
    try:
        from bid_price_targets import compute_bid_price_targets
        from procurement_blockers import classify_procurement_blockers

        packet["bid_price_targets"] = compute_bid_price_targets(
            supplier_cost=economics_block.get("supplier_cost"),
            freight=economics_block.get("freight"),
            landed_cost=economics_block.get("landed_cost"),
        )
        packet["blocker_model"] = classify_procurement_blockers(packet)
        # Prefer explicit blocker-driven next actions for auth-gated specs
        codes = set((packet["blocker_model"] or {}).get("codes") or [])
        if "AUTHENTICATION_BLOCKED_DOCUMENT" in codes or "OPERATOR_DOCUMENT_REQUIRED" in codes:
            packet["decision"] = "GET MISSING DOCUMENT"
            packet["next_actions"] = [
                "OPERATOR_DOCUMENT_REQUIRED",
                "INGEST_VIA_scripts/ingest_solicitation_document.py",
                "REQUEST_SUPPLIER_QUOTE",
            ]
    except Exception:
        packet["blocker_model"] = {"blockers": [], "primary_blocker": None, "funding_premature": True}
    packet["operator_markdown"] = render_operator_packet_md(packet)
    bm = packet.get("blocker_model") or {}
    if bm.get("blockers"):
        packet["operator_markdown"] += (
            "\n\nBLOCKERS:\n"
            + "\n".join(
                f"- {b['code']} ({b.get('severity')}): {b.get('detail')}" for b in bm["blockers"]
            )
            + f"\n\nPRIMARY BLOCKER: {bm.get('primary_blocker')}"
            + f"\nFUNDING PREMATURE: {bm.get('funding_premature')}"
        )
    return packet


def summarize_retrieval(documents: list[dict[str, Any]], listed_attachments: list[dict[str, Any]]) -> dict[str, Any]:
    def has(cls: str, access: str | None = None) -> bool:
        for d in documents:
            if d.get("document_class") == cls:
                if access is None or d.get("access_status") == access:
                    return True
        return False

    return {
        "solicitation_document": has("SOLICITATION", ACCESS_PUBLIC_FETCHED),
        "attachments_listed": len(listed_attachments),
        "attachments_fetched": sum(
            1
            for d in documents
            if d.get("document_class") == "SPECIFICATION" and d.get("access_status") == ACCESS_PUBLIC_FETCHED
        ),
        "amendments": sum(1 for d in documents if d.get("document_class") == "AMENDMENT"),
        "bid_schedule_in_event_pdf": True,  # SciQuest embeds line items in event PDF when present
        "specifications_attachment": any(
            d.get("document_class") == "SPECIFICATION" for d in documents
        ),
        "pricing_sheet": any(d.get("document_class") == "PRICING_SHEET" for d in documents),
        "missing_documents": [
            d.get("document_title")
            for d in documents
            if d.get("access_status") in {ACCESS_LOGIN_REQUIRED, "PUBLIC_LISTED_NOT_FETCHED", "NOT_FOUND"}
        ],
        "auth_barriers": [
            d.get("document_title")
            for d in documents
            if d.get("access_status") in {ACCESS_LOGIN_REQUIRED, "AUTH_REQUIRED"}
        ],
    }


def decide_next(
    completeness: dict[str, Any],
    product_id: dict[str, Any],
    deal_corr: dict[str, Any],
    economics_block: dict[str, Any],
    suppliers: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    if deal_corr.get("transactional_resale_fit") == "REJECT":
        return DEC_REJECT, ["REMOVE_FROM_IMMEDIATE_RESALE_QUEUE"]
    if deal_corr.get("transactional_resale_fit") == "POOR":
        return DEC_REJECT, [
            "RECLASSIFY_AS_REQUIREMENTS_OR_SERVICE_VEHICLE",
            "PULL_NEXT_TIER_B_CANDIDATE",
        ]
    status = completeness.get("document_completeness")
    if status == AUTH_BLOCKED:
        return DEC_GET_MISSING_DOCUMENT, [NEXT_GET_MISSING_DOC, NEXT_MANUAL_REVIEW]
    if status == CRITICAL_DOCUMENT_MISSING:
        return DEC_GET_MISSING_DOCUMENT, [NEXT_GET_MISSING_DOC]
    if product_id.get("product_id_state") == PRODUCT_INSUFFICIENT:
        return DEC_MANUAL_DOCUMENT_REVIEW, [NEXT_MANUAL_REVIEW, NEXT_GET_MISSING_DOC]
    if not suppliers:
        return DEC_CONTINUE, ["EXPAND_SUPPLIER_RESEARCH"]
    actions = [NEXT_SUPPLIER_QUOTE]
    if economics_block.get("freight_status") == "UNKNOWN":
        actions.append(NEXT_FREIGHT)
    if economics_block.get("expected_revenue") is None:
        actions.append(NEXT_REVENUE)
    if completeness.get("critical_spec_missing"):
        actions.insert(0, NEXT_GET_MISSING_DOC)
    return DEC_GET_SUPPLIER_QUOTE, actions


def render_operator_packet_md(packet: dict[str, Any]) -> str:
    req = packet.get("requirement") or {}
    pid = req.get("product_identification") or {}
    terms = req.get("terms") or {}
    econ = packet.get("economics_block") or {}
    lines = packet.get("requirement", {}).get("line_items") or []
    product_lines = [li for li in lines if not li.get("is_service_line")]

    def fv(fact: Any) -> str:
        if isinstance(fact, dict):
            v = fact.get("value")
            return "UNKNOWN" if v is None or v == "" else str(v)
        return "UNKNOWN" if fact is None else str(fact)

    qty_bits = [
        f"{li.get('line_number')}: {li.get('quantity')} {li.get('unit_of_measure')} — {li.get('description')}"
        for li in product_lines[:12]
    ]
    suppliers = packet.get("suppliers") or []
    sup_lines = [
        f"{i+1}. {s.get('supplier_name')} ({s.get('supplier_type')}) — {s.get('website') or 'n/a'} — quote_required={s.get('quote_required')}"
        for i, s in enumerate(suppliers)
    ]
    missing = (req.get("completeness") or {}).get("missing") or []
    next_actions = packet.get("next_actions") or []

    return f"""## {packet.get('solicitation_number')}
{packet.get('title') or ''}

DEAL TYPE:
{packet.get('deal_type')}
(correction: {json.dumps(packet.get('deal_type_correction') or {}, default=str)})

DOCUMENT STATUS:
{(req.get('completeness') or {}).get('document_completeness')}

PRODUCT:
{pid.get('sourcing_description') or 'UNKNOWN'}
STATE: {pid.get('product_id_state')}

QUANTITY:
{chr(10).join(qty_bits) if qty_bits else 'UNKNOWN'}

SPEC:
brand_or_equal={fv(terms.get('brand_name_or_equal'))}
certifications={fv(terms.get('required_certifications'))}
listed_attachments={fv(terms.get('listed_buyer_attachments'))}

DELIVERY:
location={fv(terms.get('delivery_location'))}
FOB={fv(terms.get('FOB_terms'))}
deadline={fv(terms.get('delivery_deadline'))}

BID DEADLINE:
{fv(terms.get('bid_deadline'))}

SUPPLIERS:
{chr(10).join(sup_lines) if sup_lines else 'NONE'}

PUBLIC PRICE:
{(packet.get('public_price') or {}).get('price_evidence')} — {(packet.get('public_price') or {}).get('notes')}

ESTIMATED SUPPLIER COST:
{econ.get('supplier_cost') if econ.get('supplier_cost') is not None else 'UNKNOWN'}
(range={econ.get('supplier_cost_range')})

FREIGHT:
{econ.get('freight') if econ.get('freight') is not None else 'UNKNOWN'}

ESTIMATED LANDED COST:
{econ.get('landed_cost') if econ.get('landed_cost') is not None else 'UNKNOWN'}

GOVERNMENT VALUE EVIDENCE:
{(packet.get('government_value_evidence') or {}).get('notes')}

ESTIMATED REVENUE:
{econ.get('expected_revenue') if econ.get('expected_revenue') is not None else 'UNKNOWN'}

ESTIMATED PROFIT:
{econ.get('estimated_profit') if econ.get('estimated_profit') is not None else 'UNKNOWN'} ({econ.get('profit_state')})

$10K TARGET:
{econ.get('ten_k_target')}

WORKING CAPITAL:
{econ.get('working_capital') if econ.get('working_capital') is not None else 'UNKNOWN'} ({econ.get('working_capital_status')})

FUNDING:
{(packet.get('funding') or {}).get('status')} — match≠approval; outreach=0

COMPLIANCE:
{json.dumps(packet.get('compliance') or {}, default=str)}

MISSING:
{', '.join(str(m) for m in missing) if missing else 'none_flagged'}

NEXT ACTIONS:
{chr(10).join(f'{i+1}. {a}' for i,a in enumerate(next_actions))}

DECISION:
{packet.get('decision')}

BID_READY:
{json.dumps(packet.get('bid_ready_evaluation') or {}, default=str)}
"""


# ---------------------------------------------------------------------------
# Replacement + runner
# ---------------------------------------------------------------------------


def load_tier_b_queue(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or (ARTIFACTS_DIR / "transactional_discovery_queue.csv")
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select_deals_with_replacement(
    *,
    priority: list[str] | None = None,
    killed: set[str] | None = None,
    max_deals: int = 3,
) -> list[str]:
    priority = list(priority or PRIORITY_SOLICITATIONS)
    killed = killed or set()
    queue = load_tier_b_queue()
    selected: list[str] = []
    for s in priority:
        if s not in killed:
            selected.append(s)
        if len(selected) >= max_deals:
            return selected
    for row in queue:
        sol = row.get("solicitation") or ""
        if sol and sol not in selected and sol not in killed:
            selected.append(sol)
        if len(selected) >= max_deals:
            break
    return selected


def run_transactional_procurement(
    *,
    authorize_live: bool = False,
    solicitations: list[str] | None = None,
    max_http: int = PROCUREMENT_HARD_MAX_HTTP,
    target_http: int = PROCUREMENT_TARGET_HTTP,
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    run_meta = start_run_metadata(extra={"run_kind": "transactional_procurement"})
    artifacts_dir = artifacts_dir or ARTIFACTS_DIR
    packets_dir = artifacts_dir / "transactional_procurement_packets"
    evidence_dir = artifacts_dir / "transactional_procurement_evidence"
    packets_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    budget = RequestBudget(
        max_total_requests=max_http,
        max_requests_per_source=max_http,
        max_pages_per_source=5,
        max_records_per_source=50,
        max_runtime_seconds=300.0,
        min_interval_seconds=1.0,
        timeout_seconds=45.0,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=authorize_live)

    list_html = None
    list_meta = None
    if authorize_live:
        resp = client.get(IOWA_LIST_URL, source_id="sciquest_iowa")
        list_html = resp.text
        list_meta = resp.meta.to_dict() if resp.meta else None

    targets = list(solicitations or PRIORITY_SOLICITATIONS)
    analyzed: list[dict[str, Any]] = []
    killed: set[str] = set()
    request_counts = {
        "public_http_search": 0,
        "SAM": 0,
        "USAspending": 0,
        "OpenAI": 0,
        "Paid": 0,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }

    # Enrich titles from listing parse when possible
    title_map: dict[str, str] = {}
    agency_map: dict[str, str] = {}
    if list_html:
        opps = parse_sciquest_public_events(list_html, list_url=IOWA_LIST_URL)
        for o in opps:
            if o.solicitation_number:
                title_map[o.solicitation_number] = o.title
                agency_map[o.solicitation_number] = o.agency or ""

    i = 0
    while len([a for a in analyzed if a.get("decision") != DEC_REJECT]) < 3 and i < 8:
        if i < len(targets):
            sol = targets[i]
        else:
            # replacement from queue
            repl = select_deals_with_replacement(priority=targets, killed=killed | {a["solicitation_number"] for a in analyzed}, max_deals=len(analyzed) + 3)
            remaining = [s for s in repl if s not in {a["solicitation_number"] for a in analyzed}]
            if not remaining:
                break
            sol = remaining[0]
            targets.append(sol)
        i += 1

        if client.request_count >= max_http:
            break
        if client.request_count >= target_http and len(analyzed) >= 3:
            break

        packet = analyze_solicitation_package(
            sol,
            client=client,
            list_html=list_html,
            title=title_map.get(sol),
            agency=agency_map.get(sol) or "State of Iowa Department of Administrative Services",
            cache_dir=evidence_dir / sol,
        )
        analyzed.append(packet)

        # Persist packet
        (packets_dir / f"{sol}.json").write_text(
            json.dumps({k: v for k, v in packet.items() if k != "operator_markdown"}, indent=2, default=str),
            encoding="utf-8",
        )
        (packets_dir / f"{sol}.md").write_text(packet.get("operator_markdown") or "", encoding="utf-8")

        if packet.get("decision") == DEC_REJECT:
            killed.add(sol)

        # Stop accepting rejects toward the "3 analyzed transactional" goal —
        # continue loop to pull replacements

    request_counts["public_http_search"] = client.request_count

    # Artifacts
    results = {
        "generated_at": _utc(),
        "run_metadata": complete_run_metadata(run_meta),
        "next_state": "TRANSACTIONAL_PROCUREMENT_INTELLIGENCE_OPERATIONAL",
        "list_meta": list_meta,
        "request_counts": request_counts,
        "target_http": target_http,
        "hard_max_http": max_http,
        "analyzed": [
            {
                "solicitation": p.get("solicitation_number"),
                "title": p.get("title"),
                "deal_type": p.get("deal_type"),
                "decision": p.get("decision"),
                "document_completeness": (p.get("requirement") or {}).get("document_completeness"),
                "product_id_state": ((p.get("requirement") or {}).get("product_identification") or {}).get(
                    "product_id_state"
                ),
                "supplier_count": p.get("supplier_count"),
                "ten_k_target": (p.get("economics_block") or {}).get("ten_k_target"),
                "profit_state": (p.get("economics_block") or {}).get("profit_state"),
                "working_capital_status": (p.get("economics_block") or {}).get("working_capital_status"),
                "funding_status": (p.get("funding") or {}).get("status"),
                "bid_ready": False,
            }
            for p in analyzed
        ],
        "packets": analyzed,
        "integration_defects_fixed": [
            "sciquest_event_pdf_urls_now_preserve_aws_signed_query_strings",
        ],
    }

    (artifacts_dir / "transactional_procurement_results.json").write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )

    # Queue CSV
    qpath = artifacts_dir / "transactional_procurement_queue.csv"
    with qpath.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "priority",
                "solicitation",
                "title",
                "deal_type",
                "document_completeness",
                "product_id_state",
                "supplier_count",
                "ten_k_target",
                "decision",
                "next_action",
            ],
        )
        w.writeheader()
        for idx, p in enumerate(analyzed, 1):
            w.writerow(
                {
                    "priority": idx,
                    "solicitation": p.get("solicitation_number"),
                    "title": p.get("title"),
                    "deal_type": p.get("deal_type"),
                    "document_completeness": (p.get("requirement") or {}).get("document_completeness"),
                    "product_id_state": ((p.get("requirement") or {}).get("product_identification") or {}).get(
                        "product_id_state"
                    ),
                    "supplier_count": p.get("supplier_count"),
                    "ten_k_target": (p.get("economics_block") or {}).get("ten_k_target"),
                    "decision": p.get("decision"),
                    "next_action": (p.get("next_actions") or [""])[0],
                }
            )

    # Line items CSV
    lipath = artifacts_dir / "transactional_line_items.csv"
    with lipath.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "solicitation",
            "agency",
            "line_number",
            "item_number",
            "description",
            "manufacturer",
            "model",
            "part_number",
            "quantity",
            "UOM",
            "product_id_state",
            "supplier_count",
            "best_public_cost",
            "cost_confidence",
            "expected_revenue",
            "revenue_confidence",
            "estimated_profit",
            "profit_state",
            "working_capital",
            "next_action",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in analyzed:
            req = p.get("requirement") or {}
            pid = (req.get("product_identification") or {}).get("product_id_state")
            econ = p.get("economics_block") or {}
            for li in req.get("line_items") or []:
                w.writerow(
                    {
                        "solicitation": p.get("solicitation_number"),
                        "agency": p.get("agency"),
                        "line_number": li.get("line_number"),
                        "item_number": li.get("CLIN_or_item_number"),
                        "description": li.get("description"),
                        "manufacturer": li.get("manufacturer"),
                        "model": li.get("model"),
                        "part_number": li.get("part_number"),
                        "quantity": li.get("quantity"),
                        "UOM": li.get("unit_of_measure"),
                        "product_id_state": pid,
                        "supplier_count": p.get("supplier_count"),
                        "best_public_cost": econ.get("supplier_cost"),
                        "cost_confidence": econ.get("supplier_cost_status"),
                        "expected_revenue": econ.get("expected_revenue"),
                        "revenue_confidence": econ.get("revenue_status"),
                        "estimated_profit": econ.get("estimated_profit"),
                        "profit_state": econ.get("profit_state"),
                        "working_capital": econ.get("working_capital"),
                        "next_action": (p.get("next_actions") or [""])[0],
                    }
                )

    report = render_run_report(results, analyzed, request_counts)
    (artifacts_dir / "transactional_procurement_report.md").write_text(report, encoding="utf-8")
    results["report_markdown"] = report
    return results


def render_run_report(results: dict[str, Any], analyzed: list[dict[str, Any]], counts: dict[str, Any]) -> str:
    parts = [
        "# Transactional Procurement Intelligence Report",
        "",
        f"Generated: {results.get('generated_at')}",
        "",
        "## Analyzed deals",
    ]
    for p in analyzed:
        parts.append(p.get("operator_markdown") or "")
        parts.append("")
    parts.extend(
        [
            "## Request counts",
            json.dumps(counts, indent=2),
            "",
            "NEXT STATE:",
            "TRANSACTIONAL_PROCUREMENT_INTELLIGENCE_OPERATIONAL",
            "",
        ]
    )
    return "\n".join(parts)
