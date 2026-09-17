"""Focused tests — on-demand procurement intelligence (knowledge, not bulk mirrors)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application_clock import CLOCK_HISTORICAL_SIMULATION, CLOCK_SYSTEM, clock_mode, freeze_time, reset_clock
from document_locator import (
    FOUND_PUBLIC,
    classify_located_document,
    generate_search_keys,
    locate_procurement_documents,
)
from historical_benchmark_constants import LIVE_IOWA_2975
from historical_case_inventory import verify_live_iowa_isolation
from information_need_router import (
    NEED_FINANCING_INFORMATION,
    NEED_HISTORICAL_AWARD,
    NEED_SPECIFICATION,
    InformationSourceRouter,
)
from on_demand_research_loop import OnDemandResearchLoop, STOP_BUDGET, STOP_DEAL_REJECTED
from persistence_policy import (
    KIND_BULK_PDF,
    KIND_SOURCE_RECIPE,
    KIND_SUPPLIER_QUOTE,
    KIND_URL_REFERENCE,
    PERSIST,
    PERSIST_REFERENCE_ONLY,
    TEMPORARY,
    may_persist_bulk_body,
    persistence_decision,
)
from procurement_source_knowledge import (
    ProcurementSourceKnowledgeBase,
    preserve_signed_query_string,
    signed_url_would_break_if_stripped,
    source_profile,
)
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact
from temporary_retrieval import LIFE_RELEASED, LIFE_TEMP_RETRIEVED, TemporaryRetrievalStore


ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"


@pytest.fixture(autouse=True)
def _clock(tmp_path):
    reset_clock()
    yield
    reset_clock()


def test_source_knowledge_persists(tmp_path):
    kb = ProcurementSourceKnowledgeBase(tmp_path / "kb.json")
    assert kb.get("iowa_sciquest_jaggaer") is not None
    kb.save()
    kb2 = ProcurementSourceKnowledgeBase(tmp_path / "kb.json")
    kb2.load()
    assert "PRESERVE_X_AMZ" in (kb2.get("iowa_sciquest_jaggaer") or {}).get("signed_url_behavior", "")


def test_bulk_source_data_does_not_persist_by_default():
    d = persistence_decision(KIND_BULK_PDF)
    assert d["category"] == TEMPORARY
    assert d["retain_body"] is False
    assert may_persist_bulk_body(KIND_BULK_PDF) is False


def test_source_recipe_persists():
    assert persistence_decision(KIND_SOURCE_RECIPE)["category"] == PERSIST


def test_url_reference_without_body():
    d = persistence_decision(KIND_URL_REFERENCE)
    assert d["category"] == PERSIST_REFERENCE_ONLY
    assert d["retain_body"] is False


def test_supplier_quote_may_persist():
    d = persistence_decision(KIND_SUPPLIER_QUOTE, is_business_record=True)
    assert d["category"] == PERSIST
    assert d["retain_body"] is True


def test_temporary_lifecycle_and_cleanup(tmp_path):
    store = TemporaryRetrievalStore(tmp_path, run_id="t1")
    rec = store.discover("https://example.com/a.pdf")
    assert rec["lifecycle"] == "DISCOVERED"
    # Simulate temp file without HTTP
    p = store.root / f"{rec['item_id']}.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    rec["path"] = str(p)
    rec["lifecycle"] = LIFE_TEMP_RETRIEVED
    rec["sha256"] = "abc"
    store._items[rec["item_id"]] = rec
    store.stats["documents_temporarily_retrieved"] = 1
    report = store.cleanup()
    assert report["stats"]["documents_released"] >= 1
    assert not p.exists()
    assert store._items[rec["item_id"]]["lifecycle"] == LIFE_RELEASED
    # provenance hash retained
    assert store._items[rec["item_id"]]["sha256"] == "abc"


def test_operator_retained_survives_cleanup(tmp_path):
    store = TemporaryRetrievalStore(tmp_path, run_id="t2")
    rec = store.discover("https://example.com/keep.pdf")
    p = store.root / f"{rec['item_id']}.pdf"
    p.write_bytes(b"keep")
    rec["path"] = str(p)
    store._items[rec["item_id"]] = rec
    store.mark_operator_retain(rec["item_id"])
    store.cleanup()
    assert p.exists()


def test_signed_query_strings_preserved():
    url = "https://s3.amazonaws.com/Sourcingevent/1-event.pdf?X-Amz-Signature=abc&X-Amz-Algorithm=AWS4"
    assert preserve_signed_query_string(url) == url
    assert signed_url_would_break_if_stripped(url) is True


def test_document_classification():
    c = classify_located_document(title="Purchase Specification", filename="spec.pdf")
    assert c["document_class"] == "SPECIFICATION"


def test_information_need_routes():
    r = InformationSourceRouter()
    hist = r.route(NEED_HISTORICAL_AWARD, agency="HHS", jurisdiction="US")
    assert any(s.get("route_key") == "usaspending" or s.get("source_id") == "usaspending" for s in hist["ordered_sources"])
    spec = r.route(NEED_SPECIFICATION, opportunity={"agency": "Iowa DOT", "source": "sciquest"})
    assert spec["ordered_sources"][0]["source_id"] == "iowa_sciquest_jaggaer"


def test_locator_public_and_blocks_benchmark_urls():
    hidden = ["https://cheat.example/secret.pdf"]
    loc = locate_procurement_documents(
        solicitation_id="645-DOTRFB-2975-2027",
        agency="Iowa DOT",
        known_metadata={"state": "IA"},
        known_document_links=[
            {
                "url": "https://s3.amazonaws.com/Sourcingevent/1-event.pdf?X-Amz-Signature=x",
                "title": "event",
            },
            {"url": hidden[0], "title": "cheat"},
        ],
        benchmark_hidden_urls=hidden,
    )
    assert loc["overall_status"] == FOUND_PUBLIC
    assert loc["benchmark_answer_key_used"] is False
    assert loc["winning_vendor_seeded"] is False
    assert any(c.get("rejected") for c in loc["candidates"])


def test_benchmark_specific_url_cannot_become_recipe():
    kb = ProcurementSourceKnowledgeBase()
    with pytest.raises(ValueError):
        kb.upsert(
            source_profile(
                source_id="cheat",
                source_name="cheat",
                notes="benchmark",
            )
            | {"benchmark_case_specific": True, "base_url": "https://hidden/case.pdf"}
        )


def test_search_keys_bounded():
    keys = generate_search_keys(solicitation_number="645-DOTRFB-2975-2027", agency="Iowa DOT", title="blades")
    assert len(keys) <= 12
    assert "645-DOTRFB-2975-2027" in keys


def test_research_loop_terminates_on_reject(tmp_path):
    temp = TemporaryRetrievalStore(tmp_path, run_id="rej")
    loop = OnDemandResearchLoop(temp_store=temp, max_http=5, max_iterations=5)
    out = loop.run(
        {"rejected": True},
        opportunity={"solicitation_number": "X", "agency": "A"},
        fetch=False,
    )
    assert out["stop_reason"] == STOP_DEAL_REJECTED


def test_request_budget_terminates(tmp_path):
    temp = TemporaryRetrievalStore(tmp_path, run_id="bud")
    temp.stats["requests_attempted"] = 99
    loop = OnDemandResearchLoop(temp_store=temp, max_http=1, max_iterations=5)
    out = loop.run(
        {
            "transactional_fit_ok": True,
            "requirements_complete": False,
            "bom_present": False,
            "supplier_identified": False,
            "cost_established": False,
            "deadline_known": True,
        },
        opportunity={"solicitation_number": "Y", "agency": "Iowa DOT", "source": "sciquest"},
        known_document_links=[],
        fetch=False,
    )
    assert out["stop_reason"] == STOP_BUDGET


def test_funding_deferred_until_economics(tmp_path):
    r = InformationSourceRouter()
    needs = r.missing_needs_from_deal_state(
        {
            "transactional_fit_ok": True,
            "deadline_known": True,
            "requirements_complete": True,
            "bom_present": True,
            "supplier_identified": True,
            "cost_established": False,
            "funding_assessed": False,
        }
    )
    assert all(n["need_type"] != NEED_FINANCING_INFORMATION for n in needs)
    needs2 = r.missing_needs_from_deal_state(
        {
            "transactional_fit_ok": True,
            "deadline_known": True,
            "requirements_complete": True,
            "bom_present": True,
            "supplier_identified": True,
            "cost_established": True,
            "funding_assessed": False,
        }
    )
    assert any(n["need_type"] == NEED_FINANCING_INFORMATION for n in needs2)


def test_finance_knowledge_persists_and_staleness():
    store = ReusableKnowledgeStore()
    store.add_finance(finance_fact("p1", "pg_required", True, source="call", verified=True))
    assert store.counts()["finance_facts"] == 1
    assert store.finance[0]["staleness"] in {"CURRENT", "AGING", "STALE", "UNKNOWN"}


def test_supplier_catalog_not_mirrored():
    store = ReusableKnowledgeStore()
    with pytest.raises(ValueError):
        store.add_supplier({"supplier": "A", "catalog_items": [1, 2, 3], "source": "x"})
    store.add_supplier(supplier_fact("Dist A", manufacturer="M", source="public", quote_required=True))


def test_historical_simulation_uses_firewall(tmp_path):
    reset_clock()
    as_of = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with freeze_time(as_of, mode=CLOCK_HISTORICAL_SIMULATION):
        assert clock_mode() == CLOCK_HISTORICAL_SIMULATION
        loc = locate_procurement_documents(
            solicitation_id="TEST",
            agency="Test",
            known_document_links=[
                {"url": "https://example.com/a.pdf", "title": "sol"},
            ],
        )
        assert "HISTORICAL_SIMULATION" in loc["temporal_note"]
    assert clock_mode() == CLOCK_SYSTEM


def test_duplicate_requests_deduped(tmp_path, monkeypatch):
    store = TemporaryRetrievalStore(tmp_path, run_id="dup")

    class FakeResp:
        status_code = 200
        content = b"data"
        headers = {"content-type": "application/pdf"}

    class FakeClient:
        def get(self, url):
            return FakeResp()

        def close(self):
            pass

    r1 = store.retrieve("https://example.com/x.pdf", client=FakeClient())
    r2 = store.retrieve("https://example.com/x.pdf", client=FakeClient())
    assert r1["item_id"] == r2["item_id"]
    assert store.stats["requests_reused"] >= 1
    assert store.stats["requests_attempted"] == 1


def test_live_iowa_isolation():
    r = verify_live_iowa_isolation(ARTIFACTS)
    assert r["live_solicitation"] == LIVE_IOWA_2975


def test_no_outreach_in_modules():
    root = Path(__file__).resolve().parent.parent
    for name in (
        "on_demand_research_loop.py",
        "document_locator.py",
        "procurement_source_knowledge.py",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert "mailto:" not in text
