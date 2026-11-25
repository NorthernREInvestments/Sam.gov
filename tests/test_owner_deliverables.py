"""Owner deliverables: research tasks never invent values; registration and retrieval queues are traceable."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone

from m3.research import owner_deliverables as od

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _row(sid, queue, **kw):
    base = {
        "staged_id": sid, "source_record_id": f"src:{sid}", "owner_queue": queue, "title": "Unication G5 Dual Band P25 Voice Pagers",
        "solicitation_number": "155840", "buyer": "stmaryscountymd", "state": "MD", "source": "opengov",
        "official_url": "https://procurement.opengov.com/portal/stmaryscountymd/projects/326590",
        "deadline_at": "2026-11-30T20:00:00+00:00",
        "classification": {"product_presence": "EXPLICIT_PRODUCT", "evidence_completeness": "SUMMARY"},
        "requirements": {"line_items": [{"description": "Unication G5 pagers", "quantity": 53.0, "unit_of_measure": "EA"}],
                         "solicitation_requirements": {}},
        "eligibility": {"eligibility_state": "ELIGIBILITY_UNKNOWN", "registration_required": True},
        "execution": {"execution_state": "EXECUTION_UNKNOWN"},
        "economics": {"profit_class": "UNPROVEN", "missing_inputs": ["government_unit_price", "supplier_unit_cost"]},
        "sourcing": {"identity_level": "EXACT", "identity_basis": "title_brand_model", "identity_evidence": "Unication G5"},
    }
    base.update(kw)
    return base


def test_research_task_lists_unknowns_instead_of_inventing():
    t = od.supplier_research_task(_row("STG-1", "commercial_research_ready"), NOW)
    assert t["quantity"] == 53.0 and t["unit_of_measure"] == "EA"
    assert t["public_retail_benchmark"] is None and t["historical_award_benchmark"] is None and t["required_lead_time"] is None
    assert "supplier_unit_cost_unverified" in t["unresolved"] and "bid_eligibility_terms_unverified" in t["unresolved"]
    assert any("Net-30" in q for q in t["supplier_questions"]) and any("personal guarantee" in q for q in t["supplier_questions"])
    assert "D-U-N-S" in t["duns_note"] and not re.search(r"\d{2}-?\d{3}-?\d{4}|\d{9}", t["duns_note"])
    assert t["days_left"] == 6.3


def test_price_evidence_bands_never_exceed_possible():
    retail = {"kind": "retail", "match_keys": ["unication g5"], "match_level": "EXACT_MPN", "unit_price": 726.75,
              "source_url": "https://retail.example/g5", "observed_at": "2026-11-24", "seller_or_buyer": "dealer"}
    award = {"kind": "award", "match_keys": ["unication g5"], "match_level": "EXACT_MPN", "unit_price": 688.50,
             "source_url": "https://county.example/tab.pdf", "observed_at": "2025-01-08", "seller_or_buyer": "county"}
    vague = {**retail, "match_level": "GENERIC_SPECIFICATION_MATCH", "unit_price": 100.0}
    t = od.supplier_research_task(_row("STG-1", "commercial_research_ready"), NOW, [retail, award, vague])
    assert t["indicative_economics"]["band"] == "UNPROFITABLE" and t["indicative_economics"]["unit_spread"] == -38.25
    assert "726.75" in t["public_retail_benchmark"] and "government_price_benchmark_unverified" not in t["unresolved"]
    assert od.indicative_economics([retail])["band"] == "UNPROVEN"
    assert od.indicative_economics([{**retail, "unit_price": 600}, award])["band"] == "POSSIBLE"
    other = od.supplier_research_task(_row("STG-9", "commercial_research_ready", title="Traffic cones", official_url="u"), NOW, [retail])
    assert other["price_evidence"] == [] and other["public_retail_benchmark"] is None


def test_deliverables_written_and_registration_grouped(tmp_path):
    rows = [_row("STG-1", "commercial_research_ready"),
            _row("STG-2", "document_verification", title="Traffic cones", official_url="https://procurement.opengov.com/portal/x/projects/9"),
            _row("STG-3", "archived", duplicate_of="src:STG-1"),
            _row("STG-4", "supplier_research_ready", title="Purchase of one Caterpillar 836G",
                 official_url="https://www.bidnetdirect.com/florida/solicitations/open-bids/x/1",
                 requirements={"line_items": [{"description": "Caterpillar 836G", "quantity": 1.0, "unit_of_measure": "EA"}],
                               "solicitation_requirements": {}},
                 qualification={"qualification_state": "SUPPLIER_RESEARCH_READY",
                                "conditions": {"SUBMISSION_VERIFIED": False, "READY_TO_SUBMIT": False}}),
            _row("STG-5", "access_required", title="Weg motor purchase",
                 queue_reason="retrieval:restricted:restricted_http_403", retrieval_status="restricted")]
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc_checkpoint.json").write_text(json.dumps({"done": {"OLD-2": {"status": "robots_disallow"}}}), encoding="utf-8")
    (docs / "staged_id_map.json").write_text(json.dumps({"OLD-2": ["src:STG-2"]}), encoding="utf-8")
    res = od.build_owner_deliverables(tmp_path, rows, NOW)
    assert res["supplier_research_tasks"] == 2 and res["registration_portals"] >= 1
    assert res["owner_buckets"]["access_required"] >= 1
    assert (tmp_path / "owner" / "all_active_products.jsonl").exists()
    assert (tmp_path / "owner" / "ready_to_bid.jsonl").read_text(encoding="utf-8") == ""
    access = [json.loads(l) for l in (tmp_path / "owner" / "access_required.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert access[0]["staged_id"] == "STG-5" and "Manual portal access" in access[0]["next_action"]
    reg = json.loads((tmp_path / "owner" / "registration_queue.json").read_text(encoding="utf-8"))
    assert sum(g["opportunity_count"] for g in reg) >= 2 and reg[0]["owner_involvement_needed"] is True
    work = list(csv.DictReader((tmp_path / "owner" / "document_retrieval_worklist.csv").open(encoding="utf-8")))
    assert [w["staged_id"] for w in work] == ["STG-1", "STG-2"]
    assert work[1]["automated_retrieval"] == "robots_disallow"
    table = list(csv.DictReader((tmp_path / "owner" / "opportunity_table.csv").open(encoding="utf-8")))
    assert table[0]["verified_retail_cost"] == "not verified" and table[0]["gross_spread"] == "unknown"
