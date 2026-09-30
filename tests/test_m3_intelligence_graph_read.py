"""BUILD 3 — Intelligence Graph read API tests."""

from __future__ import annotations

from federal_dla_product_constants import ID_EXACT_NSN, ID_AMBIGUOUS, READY_COMMERCIAL_RESEARCH
from m3_intelligence_graph_read import (
    BUILD_TAG,
    INTEL_RESEARCH_REQUIRED,
    INTEL_UNKNOWN,
    INTEL_VALIDATED,
    assemble_intelligence_graph,
    get_intelligence_graph,
    graph_snapshot_for_tests,
    resolve_pipeline_row,
)
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import INTEL_POSSIBLE


def _complete_row(**extra):
    base = {
        "canonical_id": "sol:SPE7M126T9:dla",
        "notice_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "external_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M126T9",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "deadline": "2026-10-01",
        "lifecycle": "RESEARCH",
        "updated_at": "2026-09-18T12:00:00+00:00",
        "enriched_at": "2026-09-18T12:00:00+00:00",
        "readiness_state": READY_COMMERCIAL_RESEARCH,
        "package_access": "PUBLIC",
        "product_identity": {"identity_state": ID_EXACT_NSN, "confidence": "HIGH"},
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "part_number": "PN-99",
            "cage": "12345",
            "quantity": 10,
            "unit_of_issue": "EA",
            "has_exact_nsn": True,
            "fields": {
                "nsn": {
                    "value": "4320-01-243-1951",
                    "confidence": "HIGH",
                    "evidence_source": "title+description",
                    "evidence_snippet": "NSN 4320-01-243-1951",
                },
                "part_number": {
                    "value": "PN-99",
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "P/N PN-99",
                },
                "cage": {
                    "value": "12345",
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "CAGE 12345",
                },
                "quantity": {
                    "value": 10,
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "Qty 10",
                },
                "unit_of_issue": {
                    "value": "EA",
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "UI EA",
                },
            },
        },
        "knowledge_product_id": 42,
        "product_fact_projection": {
            "knowledge_product_id": 42,
            "projected_at": "2026-09-18T12:05:00+00:00",
        },
        "supplier_intelligence": {
            "kind": "M3SupplierIntelligence",
            "generated_at": "2026-09-18T12:10:00+00:00",
            "ACQUISITION_COST_CONFIDENCE": "HIGH",
            "Supply_chain": {
                "all_channels": [
                    {
                        "company": "Acme Parts",
                        "role": "distributor",
                        "website": "https://example.com",
                        "validation_status": "VALIDATED",
                    }
                ]
            },
            "Pricing_evidence": {
                "primary_level": "LEVEL_1",
                "items": [
                    {
                        "source": "supplier_quote_file",
                        "price": 120.0,
                        "confidence": "HIGH",
                        "note": "quoted unit",
                    }
                ],
            },
            "Margin": {"margin_status": "MARGIN_VIABLE"},
        },
        "historical_award_amount": 15000,
        "historical_unit_price": 150.0,
        "government_price_history": {
            "kind": "M3GovernmentPriceHistory",
            "confidence": "HIGH",
            "generated_at": "2026-09-18T11:00:00+00:00",
        },
        "procurement_path": {"selected_path_id": "supplier_quote", "path_id": "supplier_quote"},
        "funding_requirement": {
            "status": "FUNDING_VERIFICATION_REQUIRED",
            "capital_amount": 5000,
        },
        "deal_economics": {
            "kind": "M3DealEconomics",
            "PRICE_CONFIDENCE": "HIGH",
            "PROFIT_TARGET_STATUS": "ON_TARGET",
            "Next_Action": "CONFIRM_FREIGHT",
            "generated_at": "2026-09-18T12:15:00+00:00",
            "DEAL_ECONOMICS_PROFILE": {
                "Revenue": 20000,
                "Projected_profit": 4500,
                "Current_acquisition_cost": 12000,
                "pricing_source": "supplier_quote",
            },
        },
    }
    base.update(extra)
    return base


def _partial_row(**extra):
    base = {
        "canonical_id": "sol:SPE7M126T0:dla",
        "notice_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "external_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M126T0",
        "agency": "DLA",
        "title": "Ambiguous pump requirement",
        "updated_at": "2026-09-18T10:00:00+00:00",
        "product_identity": {"identity_state": ID_AMBIGUOUS},
        "dla_product_structure": {"has_exact_nsn": False, "fields": {}},
    }
    base.update(extra)
    return base


class _FakeStore:
    def __init__(self, rows: list[dict]):
        self._rows = {r["canonical_id"]: r for r in rows}

    def get(self, cid: str):
        return self._rows.get(cid)

    def all(self):
        return list(self._rows.values())


def test_complete_opportunity_returns_full_graph():
    g = graph_snapshot_for_tests(_complete_row())
    assert g["kind"] == "M3IntelligenceGraph"
    assert g["build"] == BUILD_TAG
    assert g["found"] is True
    assert g["read_only"] is True
    assert g["product_identity"]["status"] == INTEL_VALIDATED
    assert g["product_identity"]["facts"]["nsn"] == "4320-01-243-1951"
    assert g["supplier_intelligence"]["status"] == INTEL_VALIDATED
    assert g["supplier_intelligence"]["facts"]["validated_supplier_count"] == 1
    assert g["economics"]["status"] == INTEL_VALIDATED
    assert g["economics"]["facts"]["revenue"] == 20000
    assert g["historical_intelligence"]["status"] == INTEL_VALIDATED
    assert g["procurement_path"]["facts"]["selected_path_id"] == "supplier_quote"
    for key in (
        "opportunity",
        "requirement",
        "product_identity",
        "supplier_intelligence",
        "historical_intelligence",
        "demand_signals",
        "procurement_path",
        "financing",
        "economics",
        "decision",
    ):
        assert key in g["graph"]
        assert "status" in g["graph"][key]


def test_partial_opportunity_unknown_states():
    g = graph_snapshot_for_tests(_partial_row())
    assert g["supplier_intelligence"]["status"] == INTEL_UNKNOWN
    assert "No validated supplier" in (g["supplier_intelligence"]["reason"] or "")
    assert g["historical_intelligence"]["status"] == INTEL_RESEARCH_REQUIRED
    assert g["financing"]["status"] == INTEL_UNKNOWN
    assert g["economics"]["status"] == INTEL_UNKNOWN
    assert g["product_identity"]["status"] in {INTEL_POSSIBLE, INTEL_UNKNOWN}
    assert "supplier_intelligence" in g["summary"]["unknown"] or g["supplier_intelligence"]["status"] == INTEL_UNKNOWN


def test_pipeline_id_resolves_via_store():
    row = _complete_row()
    store = _FakeStore([row])
    resolver = OpportunityIdentityResolver()
    resolved, ident = resolve_pipeline_row(row["canonical_id"], store=store, identity_resolver=resolver)
    assert resolved is not None
    assert resolved["canonical_id"] == row["canonical_id"]
    assert ident.get("opportunity_uid")

    g = get_intelligence_graph(row["canonical_id"], store=store, identity_resolver=resolver)
    assert g["found"] is True
    assert g["opportunity"]["facts"]["canonical_id"] == row["canonical_id"]


def test_notice_id_resolves_to_pipeline_row():
    row = _complete_row()
    store = _FakeStore([row])
    resolver = OpportunityIdentityResolver()
    # Register identity first
    resolver.resolve_pipeline_row(row, register=True)
    g = get_intelligence_graph(row["notice_id"], store=store, identity_resolver=resolver)
    assert g["found"] is True
    assert g["opportunity"]["facts"]["notice_id"] == row["notice_id"]


def test_evidence_sources_retained():
    g = graph_snapshot_for_tests(_complete_row())
    pi_ev = g["product_identity"]["evidence"]
    assert pi_ev
    assert any(e.get("source") == "title+description" for e in pi_ev)
    assert any(e.get("snippet") and "NSN" in str(e.get("snippet")) for e in pi_ev)
    assert g["product_identity"]["timestamp"] == "2026-09-18T12:05:00+00:00"
    assert g["supplier_intelligence"]["evidence"]
    assert g["supplier_intelligence"]["evidence"][0]["source"] == "supplier_quote_file"


def test_missing_opportunity_unknown_graph():
    g = assemble_intelligence_graph(None, opportunity_id="missing-id")
    assert g["found"] is False
    assert g["opportunity"]["status"] == INTEL_UNKNOWN
    assert g["supplier_intelligence"]["status"] == INTEL_UNKNOWN
    assert g["financing"]["status"] == INTEL_UNKNOWN


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-intelligence-graph-read")
