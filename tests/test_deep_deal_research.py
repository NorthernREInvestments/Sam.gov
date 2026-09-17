"""Deep Deal Research + Bid Qualification tests (no live HTTP / OpenAI by default)."""

from __future__ import annotations

from deep_deal_compliance import evaluate_bid_compliance, evaluate_nonmanufacturer_rule
from deep_deal_constants import (
    CHEAP_LIKELY_PRODUCT,
    CHEAP_LIKELY_SERVICE,
    COMP_NEEDS,
    DEAL_CONSTRUCTION,
    DEAL_COOP_MASTER,
    DEAL_ONE_TIME_PRODUCT,
    DEC_STRATEGIC,
    EV_PROPOSED_AI,
    FIT_CORE_TRANSACTIONAL,
    FIT_LONG_TERM_CHANNEL,
    FIT_POOR_LAUNCH,
    NMR_NEEDS,
    PROFIT_POTENTIAL,
    PROFIT_UNKNOWN,
    QUEUE_IMMEDIATE,
    QUEUE_STRATEGIC,
    STATE_STRATEGIC,
)
from deep_deal_documents import extract_solicitation_facts, reclassify_from_documents
from deep_deal_economics import (
    build_deal_economics,
    maximum_allowable_supplier_cost,
    minimum_required_bid_price,
)
from deep_deal_openai import merge_ai_proposals_safely, propose_ai_fact, run_openai_advisory
from deep_deal_qualification import (
    cheap_second_stage_classify,
    classify_deal_type,
    evaluate_pre_deep_fit,
    qualify_candidate,
)
from deep_deal_research import research_one_deal, run_deep_deal_research
from deep_deal_suppliers import build_supplier_contact_packet, commercial_price_benchmark, historical_pricing_record


def test_sourcewell_classified_as_cooperative_master():
    row = {
        "title": "Heavy and Compact Construction Equipment with Related Attachments and Services",
        "agency": "Sourcewell",
        "source_id": "coop_sourcewell_live",
        "product_classification": "CORE_PRODUCT",
        "solicitation_number": "11364",
    }
    dt = classify_deal_type(row)
    assert dt["deal_type"] == DEAL_COOP_MASTER
    assert dt["revenue_certainty"] == "NO_COMMITTED_PURCHASE"
    fit = evaluate_pre_deep_fit(row, dt)
    assert fit["pre_deep_fit"] == FIT_LONG_TERM_CHANNEL
    assert fit["admit_to_deep_research"] is False


def test_iowa_hvac_classified_construction_not_core_transactional():
    row = {
        "title": "DOT D1 Ames Materials Lab HVAC Upgrade",
        "agency": "State of Iowa Department of Administrative Services",
        "source_id": "state_ia",
        "product_classification": "CORE_PRODUCT",
        "solicitation_number": "645-DOTRFB-3063-2027",
    }
    dt = classify_deal_type(row)
    assert dt["deal_type"] == DEAL_CONSTRUCTION
    fit = evaluate_pre_deep_fit(row, dt)
    assert fit["pre_deep_fit"] == FIT_POOR_LAUNCH


def test_one_time_rfq_is_core_transactional():
    row = {
        "title": "RFQ Purchase of network switches and server hardware",
        "description": "Procurement of IT hardware equipment",
        "agency": "State IT",
        "source_id": "state_ia",
        "product_classification": "CORE_PRODUCT",
    }
    q = qualify_candidate(row)
    assert q["deal_type"] == DEAL_ONE_TIME_PRODUCT
    assert q["pre_deep_fit"] == FIT_CORE_TRANSACTIONAL
    assert q["operator_queue_bucket"] == QUEUE_IMMEDIATE


def test_coop_does_not_enter_immediate_queue():
    q = qualify_candidate(
        {
            "title": "Portable Construction Equipment",
            "agency": "Sourcewell",
            "source_id": "coop_sourcewell_live",
            "product_classification": "CORE_PRODUCT",
        }
    )
    assert q["operator_queue_bucket"] == QUEUE_STRATEGIC


def test_cheap_classification_reduces_unknown():
    c = cheap_second_stage_classify(
        {
            "title": "Purchase of laboratory equipment and supplies",
            "product_classification": "UNKNOWN",
        }
    )
    assert c["cheap_classification"] == CHEAP_LIKELY_PRODUCT
    assert c["classification_changed"] is True

    s = cheap_second_stage_classify(
        {
            "title": "Professional consulting services for modernization",
            "product_classification": "UNKNOWN",
        }
    )
    assert s["cheap_classification"] == CHEAP_LIKELY_SERVICE


def test_extract_solicitation_facts_provenance():
    text = """
    Solicitation Number: RFQ-2026-99
    Response Deadline: October 5, 2026 CDT
    Brand name or equal: Dell PowerEdge
    Small business set-aside
    NAICS: 334111
    F.O.B. Destination
    """
    ex = extract_solicitation_facts(text, source_url="https://example.gov/r.pdf")
    assert ex["facts"]["solicitation_id"] == "RFQ-2026-99"
    assert ex["provenance"]["solicitation_id"]["verification_status"] == "VERIFIED_DOCUMENT"
    assert ex["facts"]["brand_or_equal"] is True
    assert ex["facts"]["set_aside"] == "SMALL_BUSINESS"
    assert ex["facts"]["naics"] == "334111"


def test_document_reclassification_supersedes_unknown():
    extracted = extract_solicitation_facts(
        "Item 1) Commercial generator equipment 40 ea\nSupply and install",
        source_url="https://example.gov",
    )
    r = reclassify_from_documents(listing_classification="UNKNOWN", extracted=extracted)
    assert r["document_classification"] in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}
    assert r["classification_changed"] is True


def test_nmr_and_compliance_unknown_safe():
    nmr = evaluate_nonmanufacturer_rule(set_aside="SMALL_BUSINESS")
    assert nmr["nmr_status"] == NMR_NEEDS
    assert nmr["nonmanufacturer_rule_potentially_applicable"] is True
    comp = evaluate_bid_compliance(extracted_facts={"set_aside": "SMALL_BUSINESS"})
    assert comp["overall_compliance"] == COMP_NEEDS
    assert "sam_registration" in comp["needs_verification"]
    assert "entity_eligibility" in (comp["categories"])


def test_unknown_supplier_cost_not_zero():
    eco = build_deal_economics(expected_revenue=100000, revenue_status="ESTIMATED")
    assert eco["cogs"]["value"] is None
    assert eco["cogs"]["status"] == "UNKNOWN"
    assert eco["total_cost"]["status"] == "UNKNOWN"
    assert eco["profit_confidence"] in {PROFIT_UNKNOWN, PROFIT_POTENTIAL}


def test_max_supplier_cost_and_min_bid_require_known_inputs():
    mx = maximum_allowable_supplier_cost(expected_revenue=None)
    assert mx["maximum_allowable_supplier_cost"] is None
    mx2 = maximum_allowable_supplier_cost(expected_revenue=185000, freight=3000, financing_fee=2000)
    assert mx2["status"] == "CALCULATED"
    assert mx2["maximum_allowable_supplier_cost"] == 185000 - 10000 - 3000 - 2000
    mn = minimum_required_bid_price(supplier_cost=None)
    assert mn["minimum_required_bid_price"] is None
    mn2 = minimum_required_bid_price(supplier_cost=120000, freight=4000)
    assert mn2["minimum_required_bid_price"] == 120000 + 4000 + 10000


def test_ai_cannot_overwrite_verified():
    facts = {"set_aside": "UNRESTRICTED"}
    prov = {"set_aside": {"verification_status": "VERIFIED_DOCUMENT"}}
    proposals = [propose_ai_fact(field="set_aside", value="8A")]
    merged = merge_ai_proposals_safely(existing_facts=facts, existing_provenance=prov, proposals=proposals)
    assert merged["facts"]["set_aside"] == "UNRESTRICTED"
    assert merged["rejected_proposals"]
    assert merged["ai_proposals_auto_verified"] is False
    assert propose_ai_fact(field="x", value=1)["verification_status"] == EV_PROPOSED_AI


def test_openai_degrades_offline():
    r = run_openai_advisory(
        opportunity={"title": "Test", "solicitation_number": "1"},
        force_offline=True,
    )
    assert r["status"] == "DEGRADED_OFFLINE"
    assert r["OpenAI"] == 0


def test_supplier_packet_no_outreach():
    pkt = build_supplier_contact_packet(
        opportunity={"title": "Generators", "solicitation_number": "X-1", "agency": "DOT"},
        line_items=[{"description": "Generator 50kW", "quantity": 4, "part_number": "G-50"}],
        delivery_location="Ames, IA",
    )
    assert pkt["outreach_performed"] is False
    assert "deposit" in " ".join(pkt["suppliers_to_contact"][0]["questions_to_ask"]).lower()


def test_price_types_separated():
    p = commercial_price_benchmark(amount=100.0, price_type="MSRP", source="mfr")
    assert p["price_type"] == "MSRP"
    h = historical_pricing_record(award_value=50000, quantity=None)
    assert h["unit_price"] is None
    assert h["is_current_deal_revenue"] is False


def test_research_sourcewell_strategic_no_fake_revenue():
    r = research_one_deal(
        {
            "title": "Heavy and Compact Construction Equipment with Related Attachments and Services",
            "agency": "Sourcewell",
            "source_id": "coop_sourcewell_live",
            "solicitation_id": "11364",
            "solicitation_number": "11364",
            "source_url": "https://www.sourcewell-mn.gov/solicitations/11364",
            "product_classification": "CORE_PRODUCT",
            "due_date": "November 03, 2026",
        },
        authorize_live=False,
        force_openai_offline=True,
        fetch_documents=False,
    )
    assert r["deal_type"] == DEAL_COOP_MASTER
    assert r["decision"] == DEC_STRATEGIC
    assert r["bid_qualification_state"] == STATE_STRATEGIC
    assert r["economics"].get("gross_revenue", {}).get("value") in (None,) or r.get("money_note")
    assert r["primary_next_action"] == "FILE_AS_STRATEGIC_CONTRACT_VEHICLE"
    assert r["OpenAI"] == 0
    assert r["SAM"] == 0


def test_research_iowa_hvac_not_auto_core_product_pursuit():
    r = research_one_deal(
        {
            "title": "DOT D1 Ames Materials Lab HVAC Upgrade",
            "agency": "State of Iowa DAS",
            "source_id": "state_ia",
            "solicitation_id": "645-DOTRFB-3063-2027",
            "source_url": "https://example.gov/iowa",
            "product_classification": "CORE_PRODUCT",
            "detail_url": "https://example.gov/iowa",
        },
        authorize_live=False,
        force_openai_offline=True,
        fetch_documents=False,
    )
    assert r["deal_type"] == DEAL_CONSTRUCTION
    assert r["pre_deep_fit"] == FIT_POOR_LAUNCH
    assert r["primary_next_action"] == "REVIEW_SOLICITATION_DOCUMENTS"
    assert r["funding"].get("funding_secured") is False


def test_run_deep_offline_zero_paid():
    result = run_deep_deal_research(
        candidates=[
            {
                "title": "Sourcewell Equipment",
                "agency": "Sourcewell",
                "source_id": "coop_sourcewell_live",
                "solicitation_id": "11364",
                "product_classification": "CORE_PRODUCT",
                "source_url": "https://www.sourcewell-mn.gov/solicitations/11364",
            },
            {
                "title": "RFQ Purchase of industrial tools and supplies",
                "agency": "State Agency",
                "source_id": "state_tx",
                "solicitation_id": "TX-1",
                "product_classification": "CORE_PRODUCT",
                "source_url": "https://example.gov/tx",
            },
        ],
        authorize_live=False,
        force_openai_offline=True,
    )
    assert result["SAM"] == 0
    assert result["OpenAI"] == 0
    assert result["paid"] == 0
    assert result["lender_outreach_performed"] is False
    assert result["supplier_outreach_performed"] is False
    assert len(result["results"]) == 2
