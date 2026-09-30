"""R1 corpus validation — fixtures representing real solicitation patterns.

0 SAM API calls. Writes artifacts/response_engine/r1_*.json
"""

from __future__ import annotations

import json
from pathlib import Path

from phase_l.quote_economics import save_json
from response_engine.compliance import operator_compliance_summary
from response_engine.constants import BUILD
from response_engine.deliverables import deliverable_counts_by_type
from response_engine.firewall import firewall_report, ingest_supplier_quote_as_internal
from response_engine.service import compile_project, create_or_get_project_from_opportunity, ingest_document
from response_engine.store import STORE_DIR

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "response_engine"
OUT.mkdir(parents=True, exist_ok=True)


def _cases() -> list[dict]:
    from tests.test_response_engine_r1 import (
        AMENDMENT_001,
        AMENDMENT_BASE,
        BRAND_OR_EQUAL,
        DIBBS_LIKE,
        EXACT_BRAND,
        FEDERAL_RFQ,
        FORMAT_RULES,
    )

    return [
        {
            "id": "federal-commercial-rfq",
            "buyer": "Federal Agency Example",
            "solicitation_number": "RFQ-PIEE-001",
            "jurisdiction": "FEDERAL",
            "discovery_source": "SAM",
            "authoritative_source": "PIEE",
            "submission_system": "PIEE",
            "docs": [{"title": "Base RFQ", "document_type": "BASE_SOLICITATION", "text": FEDERAL_RFQ}],
        },
        {
            "id": "state-brand-or-equal",
            "buyer": "State of Nebraska",
            "solicitation_number": "ITB-NE-88",
            "jurisdiction": "STATE",
            "discovery_source": "BidNet",
            "authoritative_source": "OpenGov",
            "submission_system": "OpenGov",
            "docs": [{"title": "ITB", "document_type": "BASE_SOLICITATION", "text": BRAND_OR_EQUAL}],
        },
        {
            "id": "exact-brand",
            "buyer": "City Procurement",
            "solicitation_number": "RFQ-EXACT-1",
            "jurisdiction": "LOCAL",
            "discovery_source": "agency mirror",
            "authoritative_source": "agency procurement page",
            "submission_system": "EMAIL",
            "docs": [{"title": "Exact brand RFQ", "document_type": "BASE_SOLICITATION", "text": EXACT_BRAND}],
        },
        {
            "id": "amendment-quantity",
            "buyer": "County Schools",
            "solicitation_number": "RFQ-100",
            "jurisdiction": "LOCAL",
            "discovery_source": "Bonfire",
            "authoritative_source": "Bonfire",
            "submission_system": "Bonfire",
            "docs": [
                {"title": "Base", "document_type": "BASE_SOLICITATION", "text": AMENDMENT_BASE},
                {"title": "Amendment 001", "document_type": "AMENDMENT", "text": AMENDMENT_001, "amendment_number": "001"},
            ],
            "post": "amendment_qty",
        },
        {
            "id": "dibbs-like",
            "buyer": "DLA",
            "solicitation_number": "SPE7M1-26-T-0001",
            "jurisdiction": "FEDERAL",
            "discovery_source": "SAM",
            "authoritative_source": "DIBBS",
            "submission_system": "DIBBS",
            "docs": [
                {"title": "DIBBS RFQ", "document_type": "BASE_SOLICITATION", "text": DIBBS_LIKE},
                {"title": "Master Solicitation", "document_type": "MASTER_SOLICITATION", "text": "DLA Master Solicitation current version"},
            ],
        },
        {
            "id": "format-rules-rfp",
            "buyer": "Federal RFP Office",
            "solicitation_number": "RFP-FMT-1",
            "jurisdiction": "FEDERAL",
            "discovery_source": "SAM",
            "authoritative_source": "agency page",
            "submission_system": "EMAIL",
            "docs": [{"title": "RFP formatting", "document_type": "BASE_SOLICITATION", "text": FORMAT_RULES}],
        },
        {
            "id": "buyer-template",
            "buyer": "Los Angeles County",
            "solicitation_number": "232178",
            "jurisdiction": "LOCAL",
            "discovery_source": "BidNet",
            "authoritative_source": "county portal",
            "submission_system": "portal",
            "docs": [
                {
                    "title": "Commodity ITB",
                    "document_type": "BASE_SOLICITATION",
                    "text": "Invitation for Bid. Lowest responsive responsible bidder. Quantity: 50 each. Pricing sheet required. Submit via portal.",
                },
                {
                    "title": "Pricing Sheet",
                    "document_type": "PRICING_SHEET",
                    "filename": "LA_232178_pricing.xlsx",
                    "text": "Buyer pricing template columns: CLIN, Desc, Qty, Unit Price, Ext",
                    "is_buyer_template": True,
                },
            ],
        },
    ]


def run() -> dict:
    results = []
    graph_rows = []
    req_rows = []
    compliance_rows = []

    for case in _cases():
        # Unique opportunity id per run case
        oid = f"corpus-{case['id']}"
        # Force fresh by using unique id including build slice
        project = create_or_get_project_from_opportunity(
            canonical_opportunity_id=oid,
            buyer=case["buyer"],
            solicitation_number=case["solicitation_number"],
            jurisdiction=case["jurisdiction"],
            discovery_source=case["discovery_source"],
            authoritative_source=case["authoritative_source"],
            submission_system=case["submission_system"],
            force_new=True,
        )
        for d in case["docs"]:
            ingest_document(project, **d)
        if case.get("post") == "amendment_qty":
            from response_engine.service import apply_amendment_quantity_change

            apply_amendment_quantity_change(project, amendment_number="001", new_quantity=20, text=case["docs"][1]["text"])
        else:
            compile_project(project)

        ingest_supplier_quote_as_internal(
            project,
            {"supplier": "Fixture Supplier", "unit_price": 100, "terms": "pricing subject to change", "max_buy": 90, "margin": 0.1},
        )
        fw = firewall_report(project)
        summary = operator_compliance_summary(project)
        row = {
            "case_id": case["id"],
            "buyer": case["buyer"],
            "solicitation": case["solicitation_number"],
            "response_project_id": project["response_project_id"],
            "response_type": project.get("response_type"),
            "evaluation_method": project.get("evaluation_method"),
            "documents": len(project.get("documents") or []),
            "amendments": len(project.get("amendments") or []),
            "requirements": summary["requirements_found"],
            "mandatory": (project.get("compliance_matrix") or {}).get("summary", {}).get("mandatory_requirements"),
            "PASS": summary["satisfied"],
            "UNKNOWN": summary["need_data"],
            "REVIEW": summary["need_owner_review"],
            "hard_blockers": summary["hard_blockers"],
            "clarifications": summary["clarifications_open"],
            "deliverables": deliverable_counts_by_type(project),
            "status": summary["status"],
            "firewall_clean": fw["clean"],
            "discovery_source": project.get("discovery_source"),
            "authoritative_source": project.get("authoritative_source"),
            "submission_system": project.get("submission_system"),
        }
        results.append(row)
        graph_rows.append(
            {
                "case_id": case["id"],
                "edges": (project.get("document_graph") or {}).get("edges"),
                "conflicts": (project.get("document_graph") or {}).get("conflicts"),
                "controlling_version": project.get("current_controlling_version"),
                "superseded_docs": sum(1 for d in project.get("documents") or [] if d.get("controlling_status") == "SUPERSEDED"),
            }
        )
        req_rows.append(
            {
                "case_id": case["id"],
                "categories": sorted({r.get("requirement_category") for r in project.get("requirements") or [] if not r.get("superseded")}),
                "material": sum(1 for r in project.get("requirements") or [] if r.get("materiality") == "MATERIAL" and not r.get("superseded")),
                "timing": sorted({r.get("timing") for r in project.get("requirements") or [] if not r.get("superseded")}),
            }
        )
        compliance_rows.append(
            {
                "case_id": case["id"],
                "summary": (project.get("compliance_matrix") or {}).get("summary"),
                "hard_blocks": [{"reason": b.get("reason"), "resolution": b.get("resolution_action")} for b in project.get("hard_blocks") or []],
            }
        )

    summary = {
        "kind": "R1CorpusValidationSummary",
        "build": BUILD,
        "sam_api_calls": 0,
        "cases": len(results),
        "results": results,
        "store_dir": str(STORE_DIR),
        "verdict_hint": "PHASE_R1_SOLICITATION_COMPILER_FOUNDATION_READY"
        if results and all(r["firewall_clean"] for r in results)
        else "PHASE_R1_SOLICITATION_COMPILER_PARTIAL",
    }
    save_json(OUT / "r1_real_corpus_results.json", summary)
    save_json(OUT / "r1_document_graph_validation.json", {"build": BUILD, "rows": graph_rows})
    save_json(OUT / "r1_requirement_validation.json", {"build": BUILD, "rows": req_rows})
    save_json(OUT / "r1_compliance_validation.json", {"build": BUILD, "rows": compliance_rows})
    save_json(OUT / "r1_summary.json", summary)
    print(json.dumps({"cases": len(results), "sam_api_calls": 0, "out": str(OUT)}, indent=2))
    return summary


if __name__ == "__main__":
    run()
