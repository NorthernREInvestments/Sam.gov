"""Targeted tests — µLab research automation + quote prep + supplier intelligence."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from micro_purchase_lab_economics import derive_status, enrich_test
from micro_purchase_lab_quote_intel import (
    LABEL_ESTIMATED,
    LABEL_ESTIMATE_REQUIRED,
    MIN_QUOTE_SAMPLE,
    estimated_supplier_cost_band,
    reset_quote_intel_store_for_tests,
    supplier_discount_history,
    record_actual_supplier_quote,
)
from micro_purchase_lab_research import (
    STATUS_COMPLETE,
    STATUS_NOT_FOUND,
    STATUS_PARTIAL,
    attempt_unit_normalization,
    build_quote_packet,
    collect_current_market,
    collect_government_awards,
    collect_historical_market,
    derive_next_action,
    discover_supplier_channels,
    empty_research_state,
    evaluate_reject_filters,
    prepare_quotes_for_test,
    rank_quote_targets,
    research_opportunity,
    resolve_product_identity,
    select_current_market_reference,
    select_historical_market_reference,
)
from micro_purchase_lab_store import MicroPurchaseLabStore


@pytest.fixture()
def lab_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "lab.json"
    store = MicroPurchaseLabStore(path=path)

    def _get():
        return store

    monkeypatch.setattr("micro_purchase_lab_service.get_store", _get)
    monkeypatch.setattr("micro_purchase_lab_store.MicroPurchaseLabStore", lambda *a, **k: store)
    return store


@pytest.fixture()
def intel_store(tmp_path: Path):
    return reset_quote_intel_store_for_tests(tmp_path / "intel.json")


def _base_test(**kwargs):
    t = {
        "solicitation": "SPE7M1-26-T-0001",
        "source": "dla_dibbs",
        "agency": "DLA",
        "product": "BUSHING,SLEEVE",
        "manufacturer": "ACME",
        "part_number": "ABC-123",
        "nsn": "3120-01-234-5678",
        "quantity": 10,
        "government_unit": "EA",
        "commercial_units_per_gov_unit": 1,
        "deadline": (date.today() + timedelta(days=14)).isoformat(),
        "delivery_location": "90210",
        "identity_confidence": "STRONG",
        "opportunity_status": "OPEN",
        "historical_awards": [],
        "historical_market_costs": [],
        "current_market_prices": [],
        "supplier_quotes": [],
        "min_gross_profit_target": 0,
    }
    t.update(kwargs)
    return t


def test_research_stage_status_transitions():
    state = empty_research_state()
    assert all(s["status"] == "NOT_RUN" for s in state["stages"].values())
    test = _base_test(
        historical_awards=[{"award_date": "2025-01-15", "unit_price": 1000}],
        historical_market_costs=[
            {
                "evidence_date": "2025-01-20",
                "unit_price": 700,
                "condition": "NEW_OEM",
                "exact_match": "EXACT",
                "evidence_type": "AUTHORIZED_DISTRIBUTOR",
            }
        ],
        current_market_prices=[
            {
                "evidence_date": date.today().isoformat(),
                "unit_price": 770,
                "condition": "NEW_OEM",
                "exact_match": "EXACT",
                "evidence_type": "RETAIL",
            }
        ],
    )
    out = research_opportunity(test, allow_paid_research=False)
    stages = out["automated_research"]["stages"]
    assert stages["opportunity"]["status"] in {STATUS_COMPLETE, STATUS_PARTIAL}
    assert stages["product_identity"]["status"] == STATUS_COMPLETE
    assert stages["government_history"]["status"] == STATUS_COMPLETE
    assert stages["historical_market"]["status"] in {STATUS_COMPLETE, STATUS_PARTIAL}
    assert stages["current_market"]["status"] in {STATUS_COMPLETE, STATUS_PARTIAL}
    assert stages["supplier_channels"]["status"] == STATUS_COMPLETE
    assert out["status"] == "SUPPLIER_QUOTE_NEEDED"


def test_identity_confidence_exact_and_ambiguous():
    exact = resolve_product_identity(_base_test())
    assert exact["identity"]["confidence"] in {"EXACT", "STRONG"}
    assert exact["auto_apply"] is True

    amb = resolve_product_identity(
        {
            "product": "generic widget",
            "identity_confidence": "UNKNOWN",
            "government_unit": "EA",
        }
    )
    assert amb["identity"]["confidence"] in {"UNKNOWN", "POSSIBLE"}
    assert amb.get("needs") == "PRODUCT_IDENTITY_NEEDED"


def test_unit_normalization():
    ok = attempt_unit_normalization({"government_unit": "EA", "commercial_units_per_gov_unit": 1})
    assert ok["ok"] is True
    case = attempt_unit_normalization({"government_unit": "CASE", "commercial_units_per_gov_unit": 16})
    assert case["ok"] is True
    assert case["units_per_government_unit"] == "16"
    bad = attempt_unit_normalization({"government_unit": "CASE"})
    assert bad["needs"] == "UNIT_NORMALIZATION_REQUIRED"


def test_exact_match_evidence_preference_and_date_tolerance():
    awards = [{"award_date": "2025-06-01", "unit_price": 1000}]
    obs = [
        {
            "evidence_date": "2025-05-20",
            "unit_price": 800,
            "exact_match": "POSSIBLE",
            "product_condition": "NEW_OEM",
            "evidence_type": "RETAIL",
            "auto_eligible": False,
            "seller": "Weak",
        },
        {
            "evidence_date": "2025-06-10",
            "unit_price": 700,
            "exact_match": "EXACT",
            "product_condition": "NEW_OEM",
            "evidence_type": "AUTHORIZED_DISTRIBUTOR",
            "auto_eligible": True,
            "seller": "GoodDist",
            "date_distance_from_award": 9,
        },
        {
            "evidence_date": "2024-01-01",
            "unit_price": 500,
            "exact_match": "EXACT",
            "product_condition": "USED",
            "evidence_type": "MARKETPLACE_NEW",
            "auto_eligible": False,
            "seller": "UsedShop",
        },
    ]
    rec = select_historical_market_reference(obs, awards, window_days=90)
    assert rec is not None
    assert rec["seller"] == "GoodDist"
    assert rec["unit_price"] == 700


def test_historical_and_current_price_selection():
    hist = collect_historical_market(
        {
            "historical_market_costs": [
                {
                    "evidence_date": "2025-01-10",
                    "unit_price": 650,
                    "exact_match": "EXACT",
                    "condition": "NEW_OEM",
                    "evidence_type": "MANUFACTURER_PRICE_LIST",
                }
            ]
        },
        [{"award_date": "2025-01-15", "unit_price": 900}],
    )
    assert hist["recommended"]["unit_price"] == 650

    curr = collect_current_market(
        {
            "current_market_prices": [
                {
                    "unit_price": 900,
                    "exact_match": "POSSIBLE",
                    "condition": "NEW_OEM",
                    "evidence_type": "RETAIL",
                },
                {
                    "unit_price": 720,
                    "exact_match": "EXACT",
                    "condition": "NEW_OEM",
                    "evidence_type": "AUTHORIZED_DISTRIBUTOR",
                },
            ]
        }
    )
    assert curr["recommended"]["unit_price"] == 720
    assert curr["summary"]["verified_observations"] >= 1
    assert curr["label"] == "CURRENT_PUBLIC_MARKET_PRICE"


def test_source_confidence_and_gov_history():
    gov = collect_government_awards(
        {
            "nsn": "3120-01-234-5678",
            "historical_awards": [{"award_date": "2025-01-01", "unit_price": 100}],
        },
        {
            "exact_nsn": "3120-01-234-5678",
            "historical_award_amount": 120,
            "historical_award_date": "2024-06-01",
        },
    )
    assert gov["stage_status"] == STATUS_COMPLETE
    assert gov["stats"]["observations"] >= 1


def test_quote_target_ranking_and_packet():
    cands = [
        {"supplier": "Zoro", "supplier_type": "RESELLER", "reason_codes": []},
        {"supplier": "MSC Industrial", "supplier_type": "AUTHORIZED_DISTRIBUTOR", "government_special_bid_possible": True, "reason_codes": []},
        {"supplier": "ACME", "supplier_type": "OEM", "government_special_bid_possible": True, "reason_codes": []},
    ]
    ranked = rank_quote_targets(cands)
    assert ranked[0]["supplier"] == "ACME"
    assert ranked[1]["supplier"] == "MSC Industrial"
    pkt = build_quote_packet(_base_test(), ranked[1])
    assert "MSC" in pkt["quote_request_text"] or "Industrial" in str(pkt["supplier"])
    assert "does not claim authorized reseller status" in pkt["quote_request_text"]
    assert pkt["part_number"] == "ABC-123"


def test_quote_queue_creation_and_status(lab_store, intel_store):
    from micro_purchase_lab_service import prepare_quotes, save_test, build_quote_queue, update_queue_item_status

    row = enrich_test(_base_test())
    saved = save_test(row)
    researched = research_opportunity(saved, allow_paid_research=False)
    saved2 = save_test(researched)
    prepared = prepare_quotes_for_test(saved2, top_n=3)
    saved3 = save_test(prepared)
    lab_store.merge_quote_queue_from_test(saved3)
    q = build_quote_queue()
    assert q["count"] >= 1
    item = q["items"][0]
    assert item["quote_status"] == "READY_TO_REQUEST"
    updated = update_queue_item_status(item["id"], "REQUESTED")
    assert updated["quote_status"] == "REQUESTED"


def test_supplier_discount_history_min_sample(intel_store, tmp_path: Path):
    reset_quote_intel_store_for_tests(tmp_path / "intel_disc.json")
    test = _base_test(current_market_prices=[{"unit_price": 700}])
    for disc_price in (546, 511, 574):  # ~22%, 27%, 18%
        record_actual_supplier_quote(
            test=test,
            quote={"supplier": "MSC Industrial", "quoted_unit_cost": disc_price, "quote_date": "2026-01-01"},
        )
    early = supplier_discount_history("MSC Industrial", min_sample=MIN_QUOTE_SAMPLE)
    # with 3 quotes should be available
    assert early["available"] is True
    assert early["label"] == LABEL_ESTIMATED
    assert early["observation_count"] == 3

    thin = supplier_discount_history("UnknownCo", min_sample=3)
    assert thin["available"] is False


def test_estimated_band_never_bid_candidate(intel_store, tmp_path: Path):
    reset_quote_intel_store_for_tests(tmp_path / "intel_band.json")
    test = _base_test(current_market_prices=[{"unit_price": 700, "exact_match": "EXACT", "condition": "NEW_OEM"}])
    for p in (546, 511, 574):
        record_actual_supplier_quote(
            test=test, quote={"supplier": "MSC Industrial", "quoted_unit_cost": p, "quote_date": "2026-02-01"}
        )
    band = estimated_supplier_cost_band(public_price=700, supplier="MSC Industrial")
    assert band["available"] is True
    assert band["can_support_bid_candidate"] is False
    assert LABEL_ESTIMATE_REQUIRED.split("—")[0].strip() in band["label"] or "ESTIMATE" in band["label"]

    # Research with estimate still needs actual quote
    test["historical_awards"] = [{"award_date": "2025-01-15", "unit_price": 1000}]
    test["historical_market_costs"] = [
        {"evidence_date": "2025-01-20", "unit_price": 700, "exact_match": "EXACT", "condition": "NEW_OEM"}
    ]
    test["recommended_quote_targets"] = [{"supplier": "MSC Industrial", "supplier_type": "AUTHORIZED_DISTRIBUTOR"}]
    out = research_opportunity(test, allow_paid_research=False)
    assert out["status"] != "BID_CANDIDATE"
    assert out["status"] == "SUPPLIER_QUOTE_NEEDED"


def test_actual_quote_required_for_bid_candidate():
    test = _base_test(
        identity_confidence="EXACT",
        historical_awards=[{"award_date": "2025-01-15", "unit_price": 1000}],
        historical_market_costs=[{"evidence_date": "2025-01-20", "unit_price": 700}],
        current_market_prices=[{"date": date.today().isoformat(), "unit_price": 770}],
        supplier_quotes=[
            {
                "supplier": "MSC",
                "quoted_unit_cost": 580,
                "freight": 0,
                "payment_terms": "NET_30",
                "quote_expiration": "2099-01-01",
            }
        ],
        candidate_bid_unit=830,
        min_gross_profit_target=100,
    )
    enriched = enrich_test(test)
    assert enriched["status"] == "BID_CANDIDATE"

    no_quote = dict(test)
    no_quote["supplier_quotes"] = []
    assert derive_status(enrich_test(no_quote)) == "SUPPLIER_QUOTE_NEEDED"


def test_obvious_economic_reject_and_distributor_quote_needed():
    filters = evaluate_reject_filters(
        _base_test(),
        identity={"confidence": "EXACT"},
        units={"ok": True},
        awards=[{"unit_price": 100}],
        current_summary={"lowest_verified": "5000"},
        suppliers=[{"supplier": "Random Retail", "supplier_type": "RETAIL"}],
        equiv="100",
    )
    assert any(f["code"] == "OBVIOUS_ECONOMIC_IMPOSSIBILITY" for f in filters)

    filters2 = evaluate_reject_filters(
        _base_test(),
        identity={"confidence": "EXACT"},
        units={"ok": True},
        awards=[{"unit_price": 100}],
        current_summary={"lowest_verified": "5000"},
        suppliers=[{"supplier": "MSC", "supplier_type": "AUTHORIZED_DISTRIBUTOR"}],
        equiv="100",
    )
    assert any(f["action"] == "SUPPLIER_QUOTE_NEEDED" for f in filters2)


def test_expired_and_insufficient_runway():
    expired = evaluate_reject_filters(
        _base_test(opportunity_status="EXPIRED"),
        identity={"confidence": "EXACT"},
        units={"ok": True},
        awards=[{"unit_price": 1}],
        current_summary={},
        suppliers=[],
        equiv=None,
    )
    assert any(f["code"] == "EXPIRED_OPPORTUNITY" for f in expired)

    short = evaluate_reject_filters(
        _base_test(deadline=(date.today() + timedelta(days=1)).isoformat()),
        identity={"confidence": "EXACT"},
        units={"ok": True},
        awards=[{"unit_price": 1}],
        current_summary={},
        suppliers=[],
        equiv=None,
    )
    assert any(f["code"] == "INSUFFICIENT_DEADLINE_RUNWAY" for f in short)


def test_batch_research_failure_isolation(lab_store, monkeypatch):
    from micro_purchase_lab_research import research_batch
    from micro_purchase_lab_service import save_test

    a = save_test(_base_test(product="Item A"))
    b = save_test(_base_test(product="Item B", part_number="B-1"))

    calls = {"n": 0}
    real = research_opportunity

    def flaky(test, **kwargs):
        calls["n"] += 1
        if test.get("id") == a["id"]:
            raise RuntimeError("boom")
        return real(test, **kwargs)

    monkeypatch.setattr("micro_purchase_lab_research.research_opportunity", flaky)
    # research_batch imports research_opportunity at call time from same module — patch module attr used inside
    import micro_purchase_lab_research as m

    monkeypatch.setattr(m, "research_opportunity", flaky)
    # Also patch the import path used by research_batch — it calls research_opportunity directly in same module
    out = research_batch([a["id"], b["id"], "missing-id"], limit=5)
    assert out["count"] == 3
    assert any(r["ok"] is False and r["test_id"] == a["id"] for r in out["results"])
    assert any(r["ok"] is False and r["test_id"] == "missing-id" for r in out["results"])
    assert any(r["ok"] is True and r["test_id"] == b["id"] for r in out["results"])


def test_persistence_and_backward_compat(tmp_path: Path):
    path = tmp_path / "legacy.json"
    # legacy payload without quote_queue
    path.write_text(
        '{"kind":"MicroPurchaseLabStore","tests":[{"id":"MPT-legacy","product":"Old","updated_at":"2026-01-01T00:00:00"}]}',
        encoding="utf-8",
    )
    store = MicroPurchaseLabStore(path=path)
    assert store.get("MPT-legacy")["product"] == "Old"
    assert store.all_quote_queue() == []
    store.upsert_quote_queue_item({"id": "QQ-1", "supplier": "MSC", "quote_status": "READY_TO_REQUEST"})
    store2 = MicroPurchaseLabStore(path=path)
    assert store2.get("MPT-legacy") is not None
    assert any(q["id"] == "QQ-1" for q in store2.all_quote_queue())


def test_supplier_discovery_includes_industrial_channels():
    out = discover_supplier_channels(_base_test(product="NSN valve bushing kit", nsn="3120-01-234-5678"))
    names = [c["supplier"] for c in out["candidates"]]
    assert any("MSC" in str(n) for n in names)
    assert out["recommended"]


def test_next_action_engine():
    researched = research_opportunity(
        _base_test(
            historical_awards=[{"award_date": "2025-01-15", "unit_price": 1000}],
            historical_market_costs=[
                {"evidence_date": "2025-01-20", "unit_price": 700, "exact_match": "EXACT", "condition": "NEW_OEM"}
            ],
            current_market_prices=[
                {"date": date.today().isoformat(), "unit_price": 770, "exact_match": "EXACT", "condition": "NEW_OEM"}
            ],
        ),
        allow_paid_research=False,
    )
    action = derive_next_action(researched, researched["automated_research"])
    assert action in {"PREPARE_SUPPLIER_QUOTES", "REQUEST_SUPPLIER_QUOTE", "ENTER_SUPPLIER_QUOTE", "REVIEW_HISTORICAL_EVIDENCE"}


def test_build_version_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION == "20260922-m3-micro-lab-pipeline-1"
