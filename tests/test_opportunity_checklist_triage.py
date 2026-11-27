"""Strict JSON + title-mandatory triage guards."""

from __future__ import annotations

from m3.research.openai_client import _parse_json_blob, require_json_object
from m3.research.opportunity_checklist_triage import normalize_decision, title_signal_decision


def test_parse_rejects_broken_json_does_not_soft_succeed():
    bad = """```json
{
  "decision": "PRODUCT",
  "source_snippets": [
    "procurement_kind": 'mixed'
  ]
}
```"""
    ok, obj, err = require_json_object(bad)
    assert ok is False
    assert obj is None
    assert err != "ok"


def test_parse_accepts_fenced_valid_json():
    good = """```json
{"decision": "PRODUCT", "want_type": "PRODUCT", "title_used": true}
```"""
    ok, obj, err = require_json_object(good)
    assert ok is True
    assert obj["decision"] == "PRODUCT"
    assert err == "ok"
    assert _parse_json_blob(good)["title_used"] is True


def test_title_override_office_supply_not_unclear():
    hit = title_signal_decision("2027 Office Supply Contract")
    assert hit is not None
    assert hit["decision"] == "PRODUCT"
    assert hit["next_step"] == "FETCH_DOCUMENTS"


def test_title_override_food_items_skipped():
    hit = title_signal_decision("Request for Proposal, Food Items 2026-2027")
    assert hit is not None
    assert hit["next_step"] == "SKIP"
    assert "food" in hit["why"].lower() or "perishable" in hit["why"].lower()


def test_perishable_food_reason_blocks_milk_not_equipment():
    from m3.research.opportunity_checklist_triage import perishable_food_reason

    assert perishable_food_reason("FS Milk Supply 2026-27")
    assert perishable_food_reason("Food Items RFP")
    assert perishable_food_reason("Food Service Equipment Steamers") is None


def test_title_override_pump_rebuild_is_service():
    hit = title_signal_decision("Pacheco Pumping Plant Pump 12 Rebuild")
    assert hit is not None
    assert hit["decision"] == "SERVICE_OR_MIX"
    assert hit["next_step"] == "SKIP"


def test_title_override_park_improvements_skip():
    hit = title_signal_decision(
        "Crossings Park Improvements Phase 2 Bristol Sports Arena-Skate Park and Pump Track"
    )
    assert hit is not None
    assert hit["decision"] == "SERVICE_OR_MIX"


def test_normalize_mix_skips():
    d, n = normalize_decision({"want_type": "MIX", "decision": "SERVICE_OR_MIX", "next_step": "SKIP"})
    assert d == "SERVICE_OR_MIX"
    assert n == "SKIP"


def test_non_competitive_listing_filter():
    from m3.research.nextbid_owner_filters import (
        is_competitive_listing_candidate,
        listing_non_competitive_reason,
        listing_service_title_reason,
        listing_skip_before_detail_reason,
    )

    assert listing_non_competitive_reason(
        "Sole Source Notice: Purchase of Laboratory Supplies from Idexx"
    )
    assert listing_non_competitive_reason("NOTICE AND REQUEST FOR EXEMPTION - ZOLL MEDICAL CORP.")
    assert listing_non_competitive_reason("Request for Product Demos: Firefighting Hose Nozzles")
    assert not is_competitive_listing_candidate("Sole Source Notice: Widgets")
    assert is_competitive_listing_candidate("Gasoline and Diesel Fuel")
    assert is_competitive_listing_candidate("Oliver Hill New Courtroom Furniture")
    assert is_competitive_listing_candidate("PS, Fire- Training Academy Passenger Van")
    assert is_competitive_listing_candidate("Two (2) New/Unused 2026 Hybrid pick up")
    assert listing_service_title_reason("Pest Control Services for Transit Vehicles")
    assert listing_service_title_reason("QU27003 Fire Protection Services")
    assert listing_service_title_reason(
        "Helicopter transport services for Panther Gap in Humboldt Redwoods State Park"
    )
    assert listing_skip_before_detail_reason("Pest Control Services for Transit Vehicles")
    assert not is_competitive_listing_candidate("Pest Control Services for Transit Vehicles")


def test_title_override_skips_sole_source():
    hit = title_signal_decision("Sole Source Notice: Purchase of Laboratory Supplies")
    assert hit is not None
    assert hit["decision"] == "SERVICE_OR_MIX"
    assert hit["next_step"] == "SKIP"


def test_title_triage_schema_constants_exist():
    from m3.research.opportunity_checklist_triage import TITLE_TRIAGE_SCHEMA, triage_listing_titles

    assert "results" in TITLE_TRIAGE_SCHEMA
    assert callable(triage_listing_titles)


def test_contiguous_chunks_cover_every_character():
    from m3.research.every_word_doc_read import contiguous_chunks
    from m3.research.parallel_work import map_parallel

    text = "abcdefghijklmnopqrstuvwxyz" * 2000
    chunks = contiguous_chunks(text, size=100)
    assert "".join(c[2] for c in chunks) == text
    assert chunks[0][0] == 0
    assert chunks[-1][1] == len(text)
    # parallel map preserves order / completeness
    out = map_parallel(lambda x: x * 2, [1, 2, 3, 4, 5], max_workers=10)
    assert out == [2, 4, 6, 8, 10]


def test_la_trucks_incentive_is_agency_not_buying():
    from m3.research.opportunity_checklist_triage import (
        authorize_document_fetch,
        body_agency_not_buying_reason,
        goods_supply_salvage,
    )

    opp = {
        "title": "Port of Los Angeles Clean Ports Program – Zero-Emission Truck Purchase Incentive Project",
        "description": (
            "The City of Los Angeles Harbor Department invites the submittal of proposals "
            "from one or more Licensed Motor Carriers (LMC) to purchase at least ten (10) "
            "zero emissions (ZE) battery electric drayage trucks. Selected LMCs shall be "
            "required to enter into a grant agreement with the Harbor Department."
        ),
        "document_count": 1,
        "attachment_count": 1,
        "documents": [{"name": "notice.pdf"}],
        "procurement_kind": "goods",
    }
    assert body_agency_not_buying_reason(opp)
    assert goods_supply_salvage(opp) is None
    gate = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "parsed": {"agency_is_buying": True, "is_product_only": True},
        },
        opportunity=opp,
    )
    assert gate["authorized"] is False
    assert "not_buying" in gate["reason"]


def test_goods_supply_salvage_recovers_false_mix_cases():
    from m3.research.opportunity_checklist_triage import goods_supply_salvage, normalize_decision

    cases = [
        {
            "title": "FURNISH AND DELIVER CONCRETE FOR VARIOUS MONMOUTH COUNTY DEPARTMENTS ON-CALL, AS NEEDED",
            "description": "NJDOT MIX CLASS B CONCRETE — furnish and deliver cu.yd",
            "procurement_kind": "mixed",
        },
        {
            "title": "Vehicle Filters",
            "description": (
                "establishing a three (3} year service contract (s) for the furnishing "
                "of all METRO'S requirements for vehicle filters"
            ),
            "procurement_kind": "goods",
        },
        {
            "title": "Purchase and Delivery of Filled and Unfilled Sandbags VW0720",
            "description": "",
            "procurement_kind": "goods",
        },
        {
            "title": "Bulk Propane Fuel Supply",
            "description": (
                "on-demand delivery of bulk liquid propane (LP) to the City’s existing "
                "5,000-gallon stationary storage tank"
            ),
            "procurement_kind": "goods",
        },
    ]
    for opp in cases:
        hit = goods_supply_salvage(opp)
        assert hit is not None, opp["title"]
        assert hit["decision"] == "PRODUCT"
        assert hit["next_step"] == "FETCH_DOCUMENTS"
        # normalize would have skipped MIX — salvage is what saves revenue
        d, n = normalize_decision(
            {"want_type": "MIX", "decision": "SERVICE_OR_MIX", "next_step": "SKIP"}
        )
        assert d == "SERVICE_OR_MIX" and n == "SKIP"


def test_goods_supply_salvage_does_not_rescue_true_install_mix():
    from m3.research.opportunity_checklist_triage import goods_supply_salvage

    opp = {
        "title": "FURNISH AND INSTALL INSERTS FOR SIPHONS",
        "description": "furnishing and installing inserts; labor and materials required",
    }
    assert goods_supply_salvage(opp) is None


def test_madison_surplus_sale_blocks_document_fetch():
    from m3.research.opportunity_checklist_triage import (
        authorize_document_fetch,
        body_surplus_sale_reason,
        opportunity_shows_no_documents,
    )

    opp = {
        "title": "Town of Madison - Highway Vehicle (2005 Sterling)",
        "description": (
            "NOTICE OF SALE OF MUNICIPAL EQUIPMENT: The Town of Madison is currently "
            "accepting bids for a highway vehicle. Description of Equipment for Sale: "
            "2005 Sterling Tandem Axle Truck"
        ),
        "document_count": None,
        "attachment_count": 0,
        "documents": [],
        "procurement_kind": "goods",
    }
    assert body_surplus_sale_reason(opp)
    assert opportunity_shows_no_documents(opp)
    # Even a prior mistaken PRODUCT triage must not authorize credits.
    bad_triage = {
        "ok": True,
        "want_type": "PRODUCT",
        "decision": "PRODUCT",
        "next_step": "FETCH_DOCUMENTS",
        "parsed": {"agency_is_buying": True, "is_product_only": True},
    }
    gate = authorize_document_fetch(bad_triage, opportunity=opp)
    assert gate["authorized"] is False
    assert "surplus" in gate["reason"] or "no_documents" in gate["reason"]


def test_nebraska_surplus_property_auction_nav_is_not_surplus_sale():
    from m3.research.opportunity_checklist_triage import (
        authorize_document_fetch,
        body_surplus_sale_reason,
    )

    opp = {
        "title": "Water Softener Salt",
        "description": (
            "Links of Interest Contact Us Search State Contract Database "
            "Surplus Property Auction ITB 127641 OR AGENCY: State Purchasing Bureau "
            "Invitation to Bid for Water Softener Salt. Agency is purchasing bulk salt "
            "for delivery to state facilities."
        ),
        "document_count": 3,
        "attachment_count": 3,
        "documents": [{"url": "https://example/salt.docx"}],
    }
    assert body_surplus_sale_reason(opp) is None
    gate = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "parsed": {"agency_is_buying": True, "is_product_only": True},
        },
        opportunity=opp,
    )
    assert gate["authorized"] is True


def test_authorize_document_fetch_requires_strict_product():
    from m3.research.opportunity_checklist_triage import authorize_document_fetch

    opp = {
        "title": "Purchase of Gasoline and Diesel Fuel",
        "description": "Agency seeks vendors to supply gasoline and diesel fuel.",
        "document_count": 4,
        "attachment_count": 4,
        "documents": [{"name": "IFB.pdf"}],
    }
    ok = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "parsed": {"agency_is_buying": True, "is_product_only": True},
        },
        opportunity=opp,
    )
    assert ok["authorized"] is True
    blocked = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "parsed": {"agency_is_buying": True, "is_product_only": False},
        },
        opportunity=opp,
    )
    assert blocked["authorized"] is False


def test_document_fetch_plan_skips_document0_when_real_pdf_exists():
    from m3.research.document_fetch_plan import build_document_fetch_plan

    entries = [
        {
            "name": "ZE Truck Purchase Incentive Notice.pdf",
            "sha256": "66d027e2d47ae01441ec6c91948b7ab47f146f9a46d04292179e0658b7f6fb4f",
            "source_url": "https://vendors.planetbids.com/portal/x",
        },
        {
            "path": "nb1/published/planetbids/document0/NB00286337/NB00286337.document0.json",
            "kind": "document0",
        },
        {
            "path": "nb1/published/planetbids/document0/NB00286337/NB00286337.document0.md",
            "kind": "document0",
        },
    ]
    plan = build_document_fetch_plan(
        nb_id="NB00286337",
        entries=entries,
        opportunity={"document_count": 1, "attachment_count": 1},
    )
    assert plan["real_attachment_count"] == 1
    assert plan["document0_sidecar_count"] == 2
    assert plan["allow_document0_read_file"] is False
    assert len(plan["do_not_spend_credits_on"]) == 2
    assert len(plan["targets_for_download"]) == 1
    assert plan["max_paid_credits_planned"] == 1


def test_read_file_blocks_document0_without_cache(monkeypatch):
    from m3.research.nextbid_client import NextBidClient
    from m3.research.nextbid_controller import NextBidControllerError

    monkeypatch.setattr(
        "m3.research.nextbid_response_store.lookup_cached_response",
        lambda *a, **k: None,
    )
    client = NextBidClient(job_id="test_doc0_block")
    path = "nb1/published/planetbids/document0/NB00286337/NB00286337.document0.md"
    try:
        client.read_file("NB00286337", path)
        assert False, "expected block"
    except NextBidControllerError as exc:
        assert "document0_sidecar_metered_read_blocked" in str(exc)


def test_oliver_hill_install_labor_blocks_fetch():
    from m3.research.opportunity_checklist_triage import (
        authorize_document_fetch,
        body_install_labor_reason,
    )

    opp = {
        "title": "Oliver Hill New Courtroom Furniture",
        "description": (
            "disassembling and removing the existing courtroom furniture and "
            "building and installing the new courtroom furniture"
        ),
        "document_count": 19,
        "attachment_count": 19,
        "documents": [{"name": "spec.pdf"}],
    }
    assert body_install_labor_reason(opp)
    gate = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "parsed": {"agency_is_buying": True, "is_product_only": True},
        },
        opportunity=opp,
    )
    assert gate["authorized"] is False
    assert "install" in gate["reason"] or "labor" in gate["reason"]


def test_pbm_service_contradiction_blocks_product_fetch():
    from m3.research.opportunity_checklist_triage import (
        authorize_document_fetch,
        body_service_contract_reason,
        goods_supply_salvage,
        triage_product_service_contradiction_reason,
    )

    opp = {
        "title": "RFP 31786-00191 Pharmacy Benefits Manager",
        "description": (
            "The State seeks to procure pharmacy benefits management services "
            "for State Group Insurance Program participants."
        ),
        "document_count": 29,
        "attachment_count": 29,
        "documents": [{"name": "RFP.pdf"}],
    }
    assert body_service_contract_reason(opp)
    assert goods_supply_salvage(opp) is None
    gate = authorize_document_fetch(
        {
            "ok": True,
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "what_they_want": "Pharmacy Benefits Management services",
            "parsed": {
                "agency_is_buying": True,
                "is_product_only": True,
                "what_they_want": "Pharmacy Benefits Management services",
            },
        },
        opportunity=opp,
    )
    assert gate["authorized"] is False
    assert "service" in gate["reason"]

    # Doc-read free-form want_type must also fail closed.
    reason = triage_product_service_contradiction_reason(
        {
            "want_type": "Pharmacy Benefits Management",
            "decision": "Approved",
            "what_they_want": "The State seeks to procure pharmacy benefits management services.",
        }
    )
    assert reason


def test_xlsx_extract_not_utf8_binary_junk():
    import io
    from pathlib import Path

    from openpyxl import Workbook

    from m3.research.every_word_doc_read import extract_file_units, is_sensitive_non_product_doc

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Item"
    ws["B1"] = "Quantity"
    ws["A2"] = "Gloves"
    ws["B2"] = 500
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    tmp = Path("data") / "_test_xlsx_extract_tmp.xlsx"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        units = extract_file_units(tmp)
        assert units[0]["method"] == "openpyxl"
        assert "Gloves" in (units[0].get("text") or "")
        assert "PK" not in (units[0].get("text") or "")[:20]
    finally:
        tmp.unlink(missing_ok=True)

    assert is_sensitive_non_product_doc("Appendix 7.8 Claims Data History_July2022.xlsx")
    assert is_sensitive_non_product_doc("Appendix 7.17 HIPAA Business Associate Assessment.xlsx")
    assert not is_sensitive_non_product_doc("State of TN_Cost Proposal_REVISED.xlsx")


def test_real_attachment_fingerprint_excludes_office_guts():
    from m3.research.free_source_step_pipeline import (
        is_real_attachment_doc,
        real_attachment_hashes,
    )

    docs = [
        {"name": "RFP.pdf", "sha256": "aaa"},
        {"name": "document.xml", "sha256": "bbb", "from_zip": "packet.zip", "path": "word/document.xml"},
        {"name": "Cost Proposal.xlsx", "sha256": "ccc"},
        {"name": "[Content_Types].xml", "sha256": "ddd", "from_zip": "x.docx"},
    ]
    assert is_real_attachment_doc(docs[0])
    assert not is_real_attachment_doc(docs[1])
    assert is_real_attachment_doc(docs[2])
    assert not is_real_attachment_doc(docs[3])
    assert real_attachment_hashes(docs) == {"aaa", "ccc"}
