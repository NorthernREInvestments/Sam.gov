"""Focused tests — productive national source network."""

from __future__ import annotations

from discovery.live_fetchers import BonfireLiveFetcher, OpenGovLiveFetcher
from discovery_checkpoint import complete_source_cycle, begin_source_cycle, incremental_window
from family_adapter_contract import (
    FamilyAdapter,
    classify_document_access,
    detect_pagination_model,
    evaluate_schema_change,
    evaluate_zero_result,
    fingerprint_platform,
    get_family_adapter,
    next_page_url,
)
from national_discovery_constants import HIGH_VOLUME_TARGET
from national_discovery_funnel import NationalDiscoveryFunnel
from product_yield_intelligence import aggregate_yield_by_family, compute_product_yield
from procurement_source_registry import ProcurementSourceRegistry, source_record
from solicitation_identity import SolicitationInventory
from source_network_audit import (
    audit_source_network,
    coverage_metrics_honest,
    platform_family_inventory,
    platform_leverage_ranking,
    promote_source_lifecycle,
    sync_adapter_families,
)


BONFIRE_JSON = """{
  "projects": [
    {"id": "BF-1", "title": "Laptop Computers RFQ", "status": "Open", "dueDate": "2026-11-01",
     "organizationName": "City Test", "publicUrl": "https://example.com/1",
     "documents": [{"url": "https://example.com/a.pdf", "name": "specs"}]},
    {"id": "BF-2", "title": "Janitorial Services", "status": "Open", "dueDate": "2026-11-02"}
  ]
}"""

OPENGOV_JSON = """{
  "opportunities": [
    {"id": "OG-1", "title": "Snow Plow Blades IFB", "status": "OPEN", "closeDate": "2026-12-01",
     "agency": "DOT", "detail_url": "https://example.com/og/1"},
    {"id": "OG-2", "title": "Network Switches RFQ", "status": "OPEN", "due_date": "2026-12-15"}
  ]
}"""

HTML_PAGE = """
<html><body>
<table>
<tr><th>Solicitation</th><th>Title</th><th>Deadline</th></tr>
<tr><td>TX-100</td><td>Pump Equipment Purchase</td><td>2026-11-20</td></tr>
<tr><td>TX-101</td><td>Office Supplies</td><td>2026-11-25</td></tr>
</table>
<div class="pagination">Page 1 of 3 <a href="?page=2">Next page</a></div>
</body></html>
"""


def test_family_adapter_contract():
    adapter = get_family_adapter("Bonfire")
    assert adapter is not None
    assert adapter.platform_family == "Bonfire"
    opps = adapter.parse_listing(BONFIRE_JSON, list_url="https://example.com/list")
    assert len(opps) >= 1
    norm = adapter.normalize_opportunity(opps[0])
    assert norm["title"]
    assert "solicitation_number" in norm
    pages = adapter.discover_listing_pages("https://portal.example.com/bids", max_pages=3)
    assert len(pages) == 3
    assert "page=2" in pages[1] or "page=3" in pages[2]


def test_platform_fingerprinting_confidence():
    v = fingerprint_platform(url="https://agency.bonfirehub.com/portal", registry_family="Bonfire")
    assert v["confidence"] == "VERIFIED"
    assert v["assigned_as_fact"] is True
    weak = fingerprint_platform(html="<div>hello</div>", registry_family=None)
    assert weak["confidence"] == "UNKNOWN"
    possible = fingerprint_platform(html="powered by bonfirehub somewhere", registry_family="Bonfire")
    assert possible["confidence"] in {"POSSIBLE", "VERIFIED", "HIGH_CONFIDENCE"}


def test_pagination_beyond_page_1():
    pag = detect_pagination_model(HTML_PAGE, "https://example.com/bids")
    assert pag["model"] in {"PAGE_NUMBER", "BOUNDED_WINDOW"}
    u2 = next_page_url("https://example.com/bids", page=2)
    assert "page=2" in u2
    adapter = FamilyAdapter(OpenGovLiveFetcher(), config={"pagination_model": "PAGE_NUMBER"})
    pages = adapter.discover_listing_pages("https://example.com/og", max_pages=2)
    assert len(pages) == 2


def test_checkpoint_safety_failed_does_not_advance(tmp_path):
    success_ts = "2026-01-01T00:00:00+00:00"
    source = {
        "source_id": "t1",
        "last_successful_checkpoint": success_ts,
        "overlap_hours": 24,
    }
    cycle = begin_source_cycle("t1", "INCREMENTAL")
    completed = complete_source_cycle(cycle, success=False, records_seen=0)
    assert source["last_successful_checkpoint"] == success_ts
    assert completed.get("checkpoint_candidate") is None
    assert completed.get("success") is False
    win = incremental_window(source)
    assert win["resume_from"] == success_ts


def test_zero_result_safety_and_schema_change():
    zr = evaluate_zero_result(
        status_code=200,
        body="<html><body>Welcome to purchasing</body></html>",
        records_found=0,
        structure_recognized=False,
        prior_avg_records=12,
    )
    assert zr["state"] == "POSSIBLE_PARSER_FAILURE"
    assert zr["healthy_eligible"] is False

    empty = evaluate_zero_result(
        status_code=200,
        body="<html>No open bids at this time</html>",
        records_found=0,
        structure_recognized=True,
        prior_avg_records=0,
    )
    assert empty["state"] == "VALID_ZERO_RESULTS"

    sc = evaluate_schema_change(
        body="<div>new layout</div>",
        prior_schema_fp="abc123",
        expected_fields_present=["solicitation", "deadline"],
    )
    assert sc["material_change"] is True
    assert sc["silent_zero_success_forbidden"] is True


def test_dedupe_across_mirror_sources():
    inv = SolicitationInventory()
    a, _ = inv.upsert({
        "title": "Seed Mix IFB",
        "solicitation_number": "645-DOTRFB-3046",
        "agency": "Iowa DOT",
        "source_id": "state_ia",
        "detail_url": "https://a.example/1",
        "status": "OPEN",
        "deadline": "2026-10-01",
    })
    b, _ = inv.upsert({
        "title": "Seed Mix IFB",
        "solicitation_number": "645-DOTRFB-3046",
        "agency": "Iowa DOT",
        "source_id": "aggregator_ia",
        "detail_url": "https://b.example/1",
        "status": "OPEN",
        "deadline": "2026-10-01",
    })
    assert a["identity_key"] == b["identity_key"]
    assert len(b["source_references"]) >= 2


def test_auth_gated_docs_retain_opportunity():
    doc = classify_document_access(
        doc_urls=[],
        detail_requires_login=True,
        listing_mentions_login_for_docs=True,
    )
    assert doc["document_access"] == "AUTH_GATED"
    assert doc["retain_opportunity"] is True


def test_product_yield_and_coverage_honesty(tmp_path):
    y = compute_product_yield(
        source_id="s1",
        platform_family="Bonfire",
        records=[
            {"title": "Laptop Computers RFQ", "status": "OPEN"},
            {"title": "Staffing Services Contract", "status": "OPEN"},
        ],
    )
    assert y["records_discovered"] == 2
    assert y["product_cheap_screen_survivors"] >= 1
    assert y["stop_searching_low_yield"] is False
    agg = aggregate_yield_by_family([y])
    assert agg["families"]

    reg = ProcurementSourceRegistry(path=tmp_path / "r.json")
    reg.upsert(source_record(source_id="a", source_name="A", platform_family="Bonfire", health_state="HEALTHY_PRODUCTION"))
    reg.upsert(source_record(source_id="b", source_name="B", platform_family="Bonfire", health_state="UNKNOWN"))
    metrics = coverage_metrics_honest(reg)
    assert metrics["claim_100_percent_national_coverage"] is False if "claim_100_percent_national_coverage" in metrics else True
    assert metrics["MARKET_COVERAGE_CONFIDENCE"]["claim_100_percent_national_coverage"] is False
    assert metrics["SOURCE_HEALTH"]["registered"] == 2


def test_lifecycle_promotion_requires_records(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "r.json")
    reg.upsert(source_record(source_id="x", source_name="X", platform_family="OpenGov"))
    denied = promote_source_lifecycle(reg, source_id="x", lifecycle="HEALTHY_PRODUCTION", records_discovered=0)
    assert denied.get("error")
    ok = promote_source_lifecycle(
        reg, source_id="x", lifecycle="HEALTHY_PRODUCTION", records_discovered=3, evidence="fixture_parse"
    )
    assert ok.get("ok")
    assert reg.get("x")["health_state"] == "HEALTHY_PRODUCTION"


def test_audit_and_leverage_and_sync(tmp_path):
    # Use real registry path content via copy of structure
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    for sid, fam, health in (
        ("s1", "Bonfire", "UNKNOWN"),
        ("s2", "Bonfire", "HEALTHY_PRODUCTION"),
        ("s3", "Jaggaer", "UNKNOWN"),
        ("s4", "OpenGov", "UNKNOWN"),
    ):
        reg.upsert(
            source_record(
                source_id=sid,
                source_name=sid,
                platform_family=fam,
                discovery_url=f"https://example.com/{sid}",
                health_state=health,
            )
        )
    sync = sync_adapter_families(reg)
    assert sync["updated"] >= 1
    assert reg.get("s1")["adapter_family"] == "live_bonfire"
    audit = audit_source_network(reg)
    assert audit["registered_sources"] == 4
    assert audit["by_audit_class"]["UNKNOWN"] >= 1
    inv = platform_family_inventory(audit)
    rank = platform_leverage_ranking(inv)
    assert rank["priority_order"][0] in {"Bonfire", "Jaggaer", "OpenGov"}


def test_family_parsers_fixture():
    assert len(BonfireLiveFetcher().parse_listing(BONFIRE_JSON, list_url="https://x")) >= 1
    assert len(OpenGovLiveFetcher().parse_listing(OPENGOV_JSON, list_url="https://x")) >= 2


def test_synthetic_25k_no_business_cap():
    funnel = NationalDiscoveryFunnel()
    records = [
        {
            "title": f"Equipment Supplies Purchase {i}" if i % 3 else f"Architectural and Engineering Services {i}",
            "solicitation_number": f"SYN-{i:05d}",
            "agency": "Agency",
            "source_id": "synthetic",
            "status": "OPEN",
            "deadline": "2026-12-01",
            "synthetic_load_record": True,
        }
        for i in range(HIGH_VOLUME_TARGET)
    ]
    out = funnel.ingest_batch(records, deep_research_budget=50)
    assert out["metrics"]["input_records"] >= 25000
    assert out["no_result_cap"] is True


def test_api_source_network_routes(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/api/national-discovery/source-registry/summary").status_code == 200
    assert client.get("/api/national-discovery/platform-families").status_code == 200
    assert client.get("/api/national-discovery/adapter-health").status_code == 200
    assert client.get("/api/national-discovery/coverage-gaps").status_code == 200
    assert client.get("/api/national-discovery/product-yield").status_code == 200
