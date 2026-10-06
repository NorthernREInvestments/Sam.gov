"""Evidence breakthrough — gov value + public acquisition cost resolvers."""

from __future__ import annotations

from evidence_breakthrough.acquisition_cost_resolver import _query_variants
from evidence_breakthrough.corpus import select_identities
from evidence_breakthrough.models import BUILD, FOUND, GOV_EXACT_LINE_HISTORY
from evidence_breakthrough.opengov_history import extract_bid_tab_lines, parse_opengov_opportunity_id, search_buyer_history


def test_parse_opengov_opportunity_id():
    code, pid = parse_opengov_opportunity_id("opengov:go-metro:298984")
    assert code == "go-metro"
    assert pid == "298984"


def test_extract_bid_tab_lines_priced():
    project = {
        "id": 1,
        "title": "RFQ",
        "financialId": "RFQ-1",
        "closedAt": "2026-10-01T00:00:00Z",
        "isPublicBidPricingResult": True,
        "showBidsWithPricing": True,
        "government": {"code": "go-metro", "name": "SORTA"},
        "bidResults": {
            "proposalsData": [{"vendorName": "Acme"}],
            "bidTabulations": [
                {
                    "rows": [
                        {
                            "description": "WIDGET",
                            "lineItem": "1",
                            "quantity": 2,
                            "unitToMeasure": "EA",
                            "isHeaderRow": False,
                            "vendorResponses": [
                                {"unitPrice": 12.5, "noBid": False, "custom1": "W-100", "quantity": 2}
                            ],
                        }
                    ]
                }
            ],
        },
    }
    lines = extract_bid_tab_lines(project)
    assert len(lines) == 1
    assert lines[0]["unit_price"] == 12.5
    assert lines[0]["part_number"] == "W-100"
    assert lines[0]["winning_vendor"] == "Acme"


def test_search_buyer_history_exact_pn():
    profile = {
        "priced_lines": [
            {
                "part_number": "3944593",
                "description": "SCREW",
                "unit_price": 3.3,
                "match_keys": ["pn:3944593", "tok:3944593"],
            }
        ],
        "index": {"pn:3944593": [0], "tok:3944593": [0]},
    }
    hits = search_buyer_history(profile, part_number="3944593")
    assert hits
    assert hits[0]["match_grade"] in {"EXACT_PN", "EXACT_PN_TOKEN"}


def test_query_variants_include_mfr_pn():
    qs = _query_variants(
        {"manufacturer": "Cummins", "part_number": "3944593", "model": "3944593", "raw_description": "screw"}
    )
    assert any("Cummins" in q and "3944593" in q for q in qs)


def test_select_identities_prioritizes_commercial_mpn():
    ids = select_identities(limit=5)
    assert ids
    assert ids[0].get("confidence_grade") == "A"
    assert ids[0].get("part_number") or ids[0].get("model")


def test_build_constant():
    assert BUILD.startswith("20261003-m3-evidence-breakthrough")
