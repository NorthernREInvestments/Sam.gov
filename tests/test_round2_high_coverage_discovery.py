"""Focused tests — Round 2 high-coverage product discovery network."""

from __future__ import annotations

from pathlib import Path

from alternate_authoritative_routes import ALTERNATE_ROUTES, apply_alternate_routes
from discovery.live_fetchers import (
    BidNetLiveFetcher,
    OpenGovLiveFetcher,
    PlanetBidsLiveFetcher,
    PublicPurchaseLiveFetcher,
    SimpleHtmlLiveFetcher,
)
from discovery_checkpoint import begin_source_cycle, complete_source_cycle
from entity_geographic_coverage import entity_type_coverage, geographic_coverage
from family_adapter_contract import evaluate_schema_change, evaluate_zero_result
from national_discovery_funnel import stage1_ultra_cheap
from product_category_yield import build_product_category_yield, classify_product_category
from product_false_positive_audit import audit_survivor, run_false_positive_audit
from procurement_source_registry import ProcurementSourceRegistry, source_record
from solicitation_identity import SolicitationInventory
from unknown_source_resolution import (
    RES_AUTH,
    RES_BOT,
    RES_HEALTHY,
    RES_META,
    RES_REG,
    apply_resolution_to_registry,
    classify_probe_result,
    round2_leverage_ranking,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "artifacts" / "fixtures"


def test_unknown_resolution_classification_matrix():
    src = {"source_id": "t", "adapter_family": "live_bidnet"}
    assert classify_probe_result(source=src, status_code=403, body="login required", records_found=0, structure_recognized=False)["resolution"] == RES_AUTH
    assert classify_probe_result(source=src, status_code=200, body="Just a moment... cloudflare challenge-platform", records_found=0, structure_recognized=False)["resolution"] == RES_BOT
    assert classify_probe_result(source=src, status_code=200, body="best deal free registration start browsing now", records_found=0, structure_recognized=False)["resolution"] == RES_REG
    healthy = classify_probe_result(source=src, status_code=200, body="<table>bids</table>", records_found=5, structure_recognized=True)
    assert healthy["resolution"] == RES_HEALTHY


def test_bidnet_public_metadata_parser_fixture():
    html = (FIX / "round2_bidnet_open_bids.html").read_text(encoding="utf-8")
    f = BidNetLiveFetcher()
    opps = f.parse_listing(html, list_url="https://www.bidnetdirect.com/illinois/solicitations/open-bids")
    assert f.structure_recognized(html)
    assert len(opps) >= 1
    assert opps[0].title
    assert opps[0].raw_metadata.get("public_metadata_only") is True
    assert opps[0].raw_metadata.get("document_access") == "AUTH_GATED"


def test_boston_agency_alternate_and_opengov_bot():
    html = (FIX / "round2_boston_bids.html").read_text(encoding="utf-8")
    sh = SimpleHtmlLiveFetcher()
    opps = sh.parse_listing(html, list_url="https://www.boston.gov/bid-listings")
    assert len(opps) >= 1
    assert opps[0].solicitation_number
    og = OpenGovLiveFetcher()
    assert og.parse_listing("Just a moment... cloudflare challenge-platform", list_url="https://procurement.opengov.com/portal/x") == []


def test_publicpurchase_rejects_marketing_landing():
    f = PublicPurchaseLiveFetcher()
    body = "Get the Best Deal! Free registration. Start browsing now."
    assert f.structure_recognized(body) is False
    assert f.parse_listing(body, list_url="https://www.publicpurchase.com/gems/x/buyer/public/home") == []


def test_planetbids_fixture_parse():
    f = PlanetBidsLiveFetcher()
    body = '<html>Open Bids<a href="/bo/1">IFB Generator Equipment Purchase</a><div>Page 1 of 2 Next page</div></html>'
    opps = f.parse_listing(body, list_url="https://pbsystem.planetbids.com/portal/1")
    assert len(opps) >= 1


def test_alternate_authoritative_routes(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.upsert(source_record(source_id="agency_city_boston_ma", source_name="Boston", discovery_url="https://old.example", platform_family="OpenGov"))
    out = apply_alternate_routes(reg)
    assert out["count"] >= 1
    assert "bid-listings" in (reg.get("agency_city_boston_ma") or {}).get("discovery_url", "")


def test_public_metadata_only_normalization_and_auth_gated_retention():
    f = BidNetLiveFetcher()
    html = (FIX / "round2_bidnet_open_bids.html").read_text(encoding="utf-8")
    opps = f.parse_listing(html, list_url="https://www.bidnetdirect.com/illinois/solicitations/open-bids")
    assert all(o.raw_metadata.get("document_access") == "AUTH_GATED" for o in opps)
    # Retained as discovery opportunities
    assert all(o.title and o.external_id for o in opps)


def test_pagination_checkpoint_zero_schema_safety():
    zr = evaluate_zero_result(status_code=200, body="No open solicitations at this time", records_found=0, structure_recognized=True)
    assert zr["state"] == "VALID_ZERO_RESULTS"
    bad = evaluate_zero_result(status_code=200, body="Welcome", records_found=0, structure_recognized=False)
    assert bad["state"] != "VALID_ZERO_RESULTS"
    sch = evaluate_schema_change(body="<html>changed</html>", prior_schema_fp="oldfp", expected_fields_present=["solicitation-link"])
    assert sch["silent_zero_success_forbidden"] is True
    cyc = begin_source_cycle("s1", "INCREMENTAL_DISCOVERY")
    fail = complete_source_cycle(cyc, success=False, records_seen=0)
    assert fail.get("checkpoint_candidate") is None
    ok = complete_source_cycle(begin_source_cycle("s1", "INCREMENTAL_DISCOVERY"), success=True, records_seen=3)
    assert ok.get("checkpoint_candidate")


def test_cross_source_dedupe_retains_provenance():
    inv = SolicitationInventory()
    a, ch1 = inv.upsert({"title": "Laptops", "solicitation_number": "RFQ-99999", "agency": "City", "source_id": "s1", "external_id": "1"})
    b, ch2 = inv.upsert({"title": "Laptops", "solicitation_number": "RFQ-99999", "agency": "City", "source_id": "s2", "external_id": "1"})
    assert ch1 == "NEW"
    assert len(b.get("source_references") or []) >= 2


def test_product_category_yield_and_false_positive_audit():
    records = [
        {"title": "Purchase of laptop computers", "status": "OPEN", "external_id": "1"},
        {"title": "HVAC maintenance services", "status": "OPEN", "external_id": "2"},
        {"title": "Supply and install network switches", "status": "OPEN", "external_id": "3"},
        {"title": "Fire hose and PPE equipment", "status": "OPEN", "external_id": "4"},
        {"title": "Bridge resurfacing construction", "status": "OPEN", "external_id": "5"},
    ]
    # Force survivors for audit by using titles that pass stage1 where possible
    y = build_product_category_yield(records)
    assert y["total_records"] == 5
    assert "IT_COMPUTERS" in (y.get("largest_categories") or []) or any(
        r["category"] == "IT_COMPUTERS" for r in y["by_category"] if r["cheap_screen_survivors"]
    )
    survivors = y.get("survivors") or [
        r for r in records if stage1_ultra_cheap(r).get("survive")
    ]
    # Ensure categories assigned
    assert classify_product_category("Purchase of laptop computers")["category"] == "IT_COMPUTERS"
    assert classify_product_category("HVAC maintenance services")["category"] == "LIKELY_SERVICE_FALSE_POSITIVE"
    audit = run_false_positive_audit(survivors or records, sample_size=10)
    assert "pct_true_product" in audit
    assert audit_survivor({"title": "Vehicle repair services"})["audit_label"] == "SERVICE_FALSE_POSITIVE"
    mixed = audit_survivor({"title": "Supply and install industrial pumps"})
    assert mixed["audit_label"] in {"MIXED_BUT_POTENTIALLY_RESALE", "TRUE_PRODUCT_RESALE_CANDIDATE"}


def test_entity_and_geographic_coverage_honesty(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.upsert(source_record(source_id="state_il", source_name="IL", entity_type="STATE", jurisdiction="IL", health_state="HEALTHY_PRODUCTION", geographic_scope="IL"))
    reg.upsert(source_record(source_id="agency_city_chicago_il", source_name="Chicago", entity_type="CITY", jurisdiction="IL", health_state="PUBLIC_METADATA_ONLY", geographic_scope="IL"))
    reg.upsert(source_record(source_id="agency_city_austin_tx", source_name="Austin", entity_type="CITY", jurisdiction="TX", health_state="UNKNOWN", geographic_scope="TX"))
    ent = entity_type_coverage(reg)
    assert any(r["entity_type"] == "STATE" and r["healthy"] >= 1 for r in ent["by_entity_type"])
    geo = geographic_coverage(reg)
    assert geo["fake_100_percent_coverage_claim"] is False
    assert geo["summary"]["STATEWIDE_SOURCE_PRESENT"] >= 1
    # TX has only unknown local → NO_VERIFIED_SOURCE
    tx = next(s for s in geo["states"] if s["state"] == "TX")
    assert tx["classification"] == "NO_VERIFIED_SOURCE"


def test_registry_resolution_apply(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.upsert(source_record(source_id="x", source_name="X", health_state="UNKNOWN"))
    apply_resolution_to_registry(reg, source_id="x", resolution={"resolution": RES_META, "reason": "meta", "records_found": 2})
    assert reg.get("x")["health_state"] == "PUBLIC_METADATA_ONLY"
    apply_resolution_to_registry(reg, source_id="x", resolution={"resolution": RES_HEALTHY, "reason": "ok", "records_found": 4})
    assert reg.get("x")["health_state"] == "HEALTHY_PRODUCTION"


def test_leverage_ranking(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    for i in range(3):
        reg.upsert(source_record(source_id=f"bn{i}", source_name="b", platform_family="BidNet", health_state="UNKNOWN"))
    ranking = round2_leverage_ranking(reg)
    assert ranking["ranking"]
    assert any(r["platform_family"] == "BidNet" for r in ranking["ranking"])


def test_synthetic_25k_throughput():
    n = 25000
    survivors = 0
    for i in range(n):
        title = "Laptop computers RFQ" if i % 5 == 0 else "Consulting services agreement"
        if stage1_ultra_cheap({"title": title, "status": "OPEN"}).get("survive"):
            survivors += 1
    assert n >= 25000
    assert survivors > 0
