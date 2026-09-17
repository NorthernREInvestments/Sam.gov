"""SAM scarcity architecture tests — zero live SAM/OpenAI by default."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sam_scarcity import (
    PURPOSE_BROAD_DISCOVERY,
    PURPOSE_DASHBOARD_LOAD,
    PURPOSE_NAICS_SCAN,
    PURPOSE_OPPORTUNITY_VERIFY,
    SAM_ELIGIBLE,
    SAM_NOT_ELIGIBLE,
    SAM_NOT_NEEDED,
    evaluate_sam_api_eligibility,
    fully_qualified_sam_context,
    gate_sam_api_call,
    local_or_public_satisfies_fact,
    sam_budget_snapshot,
)


def _opp(**kwargs):
    base = dict(
        id=199,
        notice_id="610bea2df5c1436994e316259e29a9a6",
        title="Dell Server part number 210-BNZH QTY14",
        due_date=None,
        set_aside="Small Business Set Aside - Total",
        naics_code="423430",
        sam_raw={
            "noticeId": "610bea2df5c1436994e316259e29a9a6",
            "solicitationNumber": "47QACA26Q0439",
            "resourceLinks": [
                "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc/download"
            ],
            "typeOfSetAsideDescription": "Small Business Set Aside - Total",
            "classificationCode": "7B22",
        },
        description="https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=610bea2df5c1436994e316259e29a9a6",
        attachment_text=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_broad_discovery_cannot_automatically_call_sam(monkeypatch):
    monkeypatch.setenv("SAM_SCARCITY_MODE", "true")
    monkeypatch.delenv("SAM_ALLOW_BROAD_DISCOVERY", raising=False)
    elig = evaluate_sam_api_eligibility(purpose=PURPOSE_BROAD_DISCOVERY)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert "broad_sam_discovery_blocked" in elig["reason_codes"]
    gate = gate_sam_api_call(
        purpose=PURPOSE_BROAD_DISCOVERY,
        authorize_live=True,
        authorize_broad_sam_discovery=False,
        persist_audit=False,
    )
    assert gate["allowed"] is False


def test_naics_scanning_cannot_automatically_call_sam(monkeypatch):
    monkeypatch.setenv("SAM_SCARCITY_MODE", "true")
    monkeypatch.delenv("SAM_ALLOW_BROAD_DISCOVERY", raising=False)
    elig = evaluate_sam_api_eligibility(purpose=PURPOSE_NAICS_SCAN, authorize_broad_sam_discovery=False)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    with pytest.raises(ValueError, match="scarcity"):
        from sam_client import fetch_naics_from_sam

        fetch_naics_from_sam("423430", authorize_live=False)


def test_dashboard_load_cannot_call_sam():
    elig = evaluate_sam_api_eligibility(_opp(), purpose=PURPOSE_DASHBOARD_LOAD)
    assert elig["status"] == SAM_NOT_NEEDED
    assert elig["sam_api_needed"] is False


def test_core_product_alone_cannot_qualify_for_sam():
    elig = evaluate_sam_api_eligibility(
        _opp(),
        purpose=PURPOSE_OPPORTUNITY_VERIFY,
        context={
            "core_fit": "CORE_PRODUCT",
            "title_has_part_number": True,
            "title_has_quantity": True,
        },
    )
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert "title_identity_insufficient_for_sam" in elig["reason_codes"] or any(
        ("supplier" in r) or ("pricing" in r) or ("profit" in r) for r in elig["reason_codes"]
    )


def test_unknown_supplier_acquisition_cost_blocks():
    ctx = fully_qualified_sam_context(acquisition_pricing_established="UNKNOWN")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("acquisition_pricing" in r for r in elig["reason_codes"])


def test_unknown_supplier_availability_blocks():
    ctx = fully_qualified_sam_context(supplier_availability="UNKNOWN")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("supplier_availability" in r for r in elig["reason_codes"])


def test_unknown_freight_blocks_when_applicable():
    ctx = fully_qualified_sam_context(freight_applicable=True, freight_established="UNKNOWN")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("freight" in r for r in elig["reason_codes"])


def test_unresolved_manufacturer_channel_blocks():
    ctx = fully_qualified_sam_context(manufacturer_channel_resolved="UNRESOLVED")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("manufacturer_channel" in r for r in elig["reason_codes"])


def test_unresolved_financing_blocks():
    ctx = fully_qualified_sam_context(financing_path_established="UNRESOLVED")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("financing" in r for r in elig["reason_codes"])


def test_no_pg_unresolved_blocks():
    ctx = fully_qualified_sam_context(no_personal_guarantee="UNRESOLVED")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("no_personal_guarantee" in r for r in elig["reason_codes"])


def test_personal_credit_unresolved_blocks():
    ctx = fully_qualified_sam_context(no_personal_credit="UNKNOWN")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("no_personal_credit" in r for r in elig["reason_codes"])


def test_zero_upfront_unresolved_blocks():
    ctx = fully_qualified_sam_context(zero_personal_cash_upfront="UNKNOWN")
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("zero_personal_cash" in r for r in elig["reason_codes"])


def test_actual_profit_unknown_blocks():
    ctx = fully_qualified_sam_context(actual_profit={"status": "UNKNOWN", "value": None})
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("actual_profit" in r for r in elig["reason_codes"])


def test_actual_profit_below_10k_blocks():
    ctx = fully_qualified_sam_context(actual_profit={"value": 9999.99, "status": "CALCULATED"})
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert any("actual_profit" in r for r in elig["reason_codes"])


def test_verified_fatal_blocker_blocks():
    ctx = fully_qualified_sam_context(fatal_blocker=True)
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_NOT_ELIGIBLE
    assert "verified_fatal_blocker" in elig["reason_codes"]


def test_fully_qualified_may_become_eligible():
    ctx = fully_qualified_sam_context()
    elig = evaluate_sam_api_eligibility(_opp(), context=ctx)
    assert elig["status"] == SAM_ELIGIBLE
    assert "all_pre_sam_gates_pass" in elig["reason_codes"]


def test_narrow_exception_requires_explicit_reason():
    bad = evaluate_sam_api_eligibility(
        _opp(),
        context={"core_fit": "CORE_PRODUCT"},
        exception={"missing_fact": "x", "why_necessary": "need more information"},
    )
    assert bad["status"] == SAM_NOT_ELIGIBLE
    assert any("exception" in r for r in bad["reason_codes"])

    good = evaluate_sam_api_eligibility(
        _opp(),
        context={"core_fit": "CORE_PRODUCT"},
        exception={
            "missing_fact": "authoritative_amendment_version",
            "why_necessary": "Must confirm current amendment before bid submission",
            "why_local_public_insufficient": "Local sam_raw lacks amendment sequence; public page not authoritative",
            "why_sam_authoritative": "SAM notice resources are the system of record for amendments",
            "reason_code": "SAM_ONLY_AMENDMENT_VERSION",
        },
    )
    assert good["status"] == SAM_ELIGIBLE
    assert good["exception_applied"] is True
    assert "SAM_ONLY_AMENDMENT_VERSION" in good["reason_codes"]


def test_cache_can_prevent_duplicate_sam_request():
    opp = _opp()
    assert local_or_public_satisfies_fact(opp, "set_aside", {}) is True
    elig = evaluate_sam_api_eligibility(
        opp,
        purpose=PURPOSE_OPPORTUNITY_VERIFY,
        context={"requested_fact": "set_aside"},
    )
    assert elig["status"] == SAM_NOT_NEEDED


def test_budget_exhaustion_blocks_sam_request(monkeypatch):
    monkeypatch.setenv("SAM_SCARCITY_MODE", "true")
    monkeypatch.setenv("SAM_ALLOW_BROAD_DISCOVERY", "true")
    monkeypatch.setattr("api_budget.can_spend_sam", lambda credits=1: False)
    gate = gate_sam_api_call(
        purpose=PURPOSE_BROAD_DISCOVERY,
        authorize_live=True,
        authorize_broad_sam_discovery=True,
        persist_audit=False,
    )
    assert gate["allowed"] is False
    assert gate["blocked_reason"] == "sam_budget_exhausted"


def test_every_sam_call_is_audited_when_persist(monkeypatch):
    monkeypatch.setenv("SAM_SCARCITY_MODE", "true")
    calls = []

    def fake_audit(**kwargs):
        calls.append(kwargs)
        return 42

    monkeypatch.setattr("sam_scarcity.record_sam_api_audit", fake_audit)
    gate_sam_api_call(
        purpose=PURPOSE_NAICS_SCAN,
        authorize_live=False,
        persist_audit=True,
    )
    assert len(calls) == 1
    assert calls[0]["purpose"] == PURPOSE_NAICS_SCAN
    assert calls[0]["executed"] is False


def test_opportunity_199_cannot_currently_make_another_sam_api_call():
    opp = _opp()
    elig = evaluate_sam_api_eligibility(
        opp,
        purpose=PURPOSE_OPPORTUNITY_VERIFY,
        context={
            "core_fit": "CORE_PRODUCT",
            "title_has_part_number": True,
            "title_has_quantity": True,
        },
    )
    assert elig["status"] in {SAM_NOT_ELIGIBLE, SAM_NOT_NEEDED}
    gate = gate_sam_api_call(
        purpose=PURPOSE_OPPORTUNITY_VERIFY,
        opportunity=opp,
        authorize_live=True,
        context={
            "core_fit": "CORE_PRODUCT",
            "title_has_part_number": True,
            "title_has_quantity": True,
        },
        persist_audit=False,
    )
    assert gate["allowed"] is False


def test_direct_retrieval_does_not_count_as_sam_api(monkeypatch):
    from direct_document_retrieval import collect_stored_public_urls, retrieve_direct_public_documents

    opp = _opp(id=199)
    urls = collect_stored_public_urls(opp)
    assert any(u["retrievable_direct"] == "yes" for u in urls)
    assert any(u["kind"] == "sam_noticedesc_api" for u in urls)

    class FakeResp:
        status_code = 200
        content = b"%PDF-1.4 fake"
        headers = {"content-type": "application/pdf"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    monkeypatch.setattr("direct_document_retrieval.httpx.Client", FakeClient)
    monkeypatch.setattr("pdf_text.extract_pdf_text", lambda data: "Dell 210-BNZH QTY 14 CLIN delivery FOB")
    persisted = []

    def fake_persist(session, contract, files, extracted_by_name=None):
        persisted.extend(files)
        return len(files)

    monkeypatch.setattr("direct_document_retrieval.persist_attachment_files", fake_persist)
    session = MagicMock()
    result = retrieve_direct_public_documents(opp, session)
    assert result["SAM_API_CALLS"] == 0
    assert result["OPENAI_CALLS"] == 0
    assert result["documents_persisted"] >= 1
    assert persisted


def test_product_sync_blocked_without_broad_auth(monkeypatch):
    monkeypatch.setenv("SAM_SCARCITY_MODE", "true")
    monkeypatch.delenv("SAM_ALLOW_BROAD_DISCOVERY", raising=False)
    from product_discovery import run_product_sync

    out = run_product_sync(authorize_live=True, authorize_broad_sam_discovery=False)
    assert out["executed"] is False
    assert out["error"] == "broad_sam_discovery_blocked"
    assert out.get("LIVE_SAM_CALLS", 0) == 0


def test_non_sam_discovery_adapters_registered():
    from opportunity_source import get_source, list_sources

    names = {s["source_name"] for s in list_sources()}
    assert "openai_web_discovery" in names
    assert "public_procurement_page" in names
    assert "direct_public_source" in names
    adapter = get_source("openai_web_discovery")
    assert adapter is not None
    pre = adapter.preflight()
    assert pre["LIVE_API_REQUESTS"] == 0
    with pytest.raises(PermissionError):
        adapter.fetch(authorize_live=False)
    cand = adapter.normalize(
        {
            "id": "cand-1",
            "title": "Widgets",
            "evidence_urls": ["https://example.com/rfp.pdf"],
        }
    )
    assert cand.raw_payload["_verification_policy"] == "CANDIDATE_NOT_AUTO_VERIFIED"


def test_sam_budget_snapshot_shape():
    snap = sam_budget_snapshot()
    assert "configured_limit" in snap
    assert "used" in snap
    assert "remaining" in snap
    assert snap["LIVE_API_REQUESTS"] == 0


def test_sibling_products_table_remains_isolated():
    from product_deal import collect_known_knowledge_from_postgres

    with pytest.raises(ValueError):
        collect_known_knowledge_from_postgres(SimpleNamespace(id=1), allow_sibling_products_table=True)


def test_document_readiness_keyword_only_not_verified():
    from direct_document_retrieval import inspect_document_readiness

    opp = _opp(attachment_text="Dell part 210-BNZH quantity 14 with FOB destination and TAA")
    ready = inspect_document_readiness(opp)
    assert ready["evidence"]["dell"]["keyword_present"] is True
    assert ready["evidence"]["dell"]["verification_status"] == "KEYWORD_ONLY_NOT_VERIFIED"
