"""Phase L.20 supplier acquisition evidence tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.manufacturer_channels import (
    MANUFACTURER_LIST_PRICE,
    PUBLIC_ACQUISITION_PRICE,
    QUOTE_REQUIRED,
    marketplace_blocked,
    resolve_manufacturer_channels,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, sam_api_park_status
from discovery.supplier_profiles import classify_price_kind, upsert_supplier_profile
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l20_supplier_acquisition import BUILD, L19_BASELINE, SKIP_EXECUTION_RISK, SKIP_SUPPLIER_PATH, load_l19_targets
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.quality_audit import SUPPLIER_A, SUPPLIER_B, SUPPLIER_C, SUPPLIER_D, grade_supplier
from phase_l.quote_readiness import AUTHORIZED_CONFIRMED, AUTHORIZED_LIKELY

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_load_l19_targets_priority():
    targets = load_l19_targets()
    assert len(targets) >= 4
    # Gov C first
    assert any("Caterpillar" in str(t.get("title") or "") or "CATERPILLAR" in str(t.get("title") or "") for t in targets[:4])


def test_manufacturer_channel_resolution():
    apple = resolve_manufacturer_channels(manufacturer="Apple", model="iPad 11")
    assert apple["resolved"] is True
    assert len(apple["candidates"]) >= 3
    ford = resolve_manufacturer_channels(manufacturer="Ford", model="Police Pursuit Interceptor")
    assert ford["resolved"] and ford.get("territory_restrictions") is True
    cat = resolve_manufacturer_channels(manufacturer="Caterpillar", model="C18")
    assert cat["resolved"]


def test_supplier_abcd_and_marketplace_guardrail():
    commercial = {"manufacturer": "Apple", "model": "iPad 11"}
    oem = upsert_supplier_profile(
        {
            "supplier_domain": "apple.com",
            "source_type": "OEM",
            "authorization_state": AUTHORIZED_LIKELY,
            "product_fit": "EXACT",
            "exact_product_evidence": True,
            "price_kind": MANUFACTURER_LIST_PRICE,
        },
        commercial=commercial,
    )
    assert oem["supplier_grade"] in {SUPPLIER_A, SUPPLIER_B, SUPPLIER_C}
    confirmed = grade_supplier(
        {
            "supplier_domain": "cdw-g.com",
            "authorization_state": AUTHORIZED_CONFIRMED,
            "product_fit": "EXACT",
            "exact_product_evidence": True,
            "source_type": "DISTRIBUTOR",
        },
        commercial=commercial,
    )
    assert confirmed == SUPPLIER_A
    assert marketplace_blocked("https://www.ebay.com/itm/123")
    mkt = upsert_supplier_profile(
        {"supplier_domain": "ebay.com", "source_type": "RESELLER", "product_fit": "EXACT"},
        commercial=commercial,
    )
    assert mkt["supplier_grade"] == SUPPLIER_D


def test_public_price_vs_actual_quote_distinction():
    assert classify_price_kind({"price_kind": "PUBLIC_ACQUISITION_PRICE"}) == PUBLIC_ACQUISITION_PRICE
    assert classify_price_kind({"actual_quote": True}) != PUBLIC_ACQUISITION_PRICE
    assert classify_price_kind({"supplier_domain": "cdw.com"}) == QUOTE_REQUIRED


def test_quantity_and_contract_eligibility_flags():
    from discovery.manufacturer_channels import CONTRACT_CATALOG_PRICE

    ford = resolve_manufacturer_channels(manufacturer="Ford", model="Police Pursuit Interceptor")
    coop = next(c for c in ford["candidates"] if c.get("source_type") == "COOPERATIVE")
    assert coop.get("eligibility")
    assert coop.get("price_kind") == CONTRACT_CATALOG_PRICE


def test_gates_no_outreach():
    assert_no_fixed_positive_cap()
    assert BIDNET_AUTH_HISTORY_PARKED
    st = sam_api_park_status()
    assert st["status"] == SAM_API_PENDING_REPLACEMENT_KEY
    assert st["calls_consumed"] == 0
    assert BUILD.startswith("20260928-m3-phase-l20")
    assert SKIP_SUPPLIER_PATH and SKIP_EXECUTION_RISK
    assert L19_BASELINE["supplier"]["A"] == 0


def test_l20_artifacts_when_present():
    required = [
        "l20_target_population.json",
        "l20_manufacturer_channels.json",
        "l20_supplier_candidates.json",
        "l20_authorization_evidence.json",
        "l20_public_acquisition_prices.json",
        "l20_payment_terms.json",
        "l20_freight_evidence.json",
        "l20_supplier_upgrades.json",
        "l20_financing_screen.json",
        "l20_economics_recomputed.json",
        "l20_quote_targets.json",
        "l20_quote_outreach_queue.json",
        "l20_summary.json",
    ]
    if not (OUT / "l20_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l20_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L20_")
    assert summary.get("no_outreach") is True
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("evidence_gate_unchanged") is True
    assert summary["quote_outreach_queue"]["sent"] == 0
    for doc in (
        "phase_l20_supplier_strategy.md",
        "phase_l20_manufacturer_channels.md",
        "phase_l20_authorization.md",
        "phase_l20_public_pricing.md",
        "phase_l20_terms_financing.md",
        "phase_l20_economics.md",
        "phase_l20_quote_target_delta.md",
        "phase_l20_supplier_memory.md",
        "phase_l20_legacy_cleanup.md",
        "phase_l20_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
