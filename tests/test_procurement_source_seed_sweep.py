"""Focused tests — real procurement source seeding + discovery sweep."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from procurement_source_coverage_discovery import ACCESS_PAID, is_paid_aggregator_url
from procurement_source_registry import ProcurementSourceRegistry
from procurement_source_seed_sweep import (
    ProcurementSourceSeedSweep,
    _guess_entity_type,
    _norm_url,
    build_seed_catalog,
    classify_visit_health,
    extract_directory_entities,
    extract_procurement_links,
)


def test_seed_catalog_has_concrete_urls_and_federal_state_platform_coop():
    seeds = build_seed_catalog()
    assert len(seeds) >= 80
    assert all(str(s["url"]).startswith("http") for s in seeds)
    kinds = {s["kind"] for s in seeds}
    assert "FEDERAL" in kinds
    assert "STATE" in kinds
    assert "PLATFORM" in kinds
    assert "COOPERATIVE" in kinds
    assert any(s.get("entity_map_only") for s in seeds)
    # Deduped by normalized URL
    keys = [_norm_url(s["url"]).lower() for s in seeds]
    assert len(keys) == len(set(keys))
    # All 50 states represented via STATE_MATRIX / known seeds
    states = {s.get("state") for s in seeds if s.get("state") and len(str(s["state"])) == 2}
    assert len(states) >= 50


def test_url_normalization_and_dedupe():
    assert _norm_url("https://example.gov/bids/") == "https://example.gov/bids"
    assert _norm_url("//example.gov/x#frag") == "https://example.gov/x"
    a = build_seed_catalog()
    urls = [_norm_url(s["url"]).lower() for s in a]
    assert len(urls) == len(set(urls))


def test_extract_procurement_links_platform_and_keyword():
    html = """
    <a href="/login">Sign in</a>
    <a href="https://vendors.bonfirehub.com/agency/foo">Bonfire portal</a>
    <a href="https://city.example.gov/procurement/bids">Open Bids</a>
    <a href="https://www.facebook.com/x">Facebook</a>
    """
    links = extract_procurement_links(html, base_url="https://city.example.gov/")
    urls = [l["url"] for l in links]
    assert any("bonfirehub" in u for u in urls)
    assert any("procurement/bids" in u for u in urls)
    assert not any("facebook" in u for u in urls)


def test_extract_directory_entities_and_entity_typing():
    html = """
    <h2>Springfield School District</h2>
    <div>City of Springfield</div>
    <div>Greene County</div>
    <div>State University of Example</div>
    <div>Regional Airport Authority</div>
    """
    ents = extract_directory_entities(html, source_hint="test")
    types = {e["entity_type"] for e in ents}
    assert "K12_SCHOOL_DISTRICT" in types
    assert "CITY_MUNICIPAL" in types or "COUNTY" in types
    assert _guess_entity_type("Dallas Independent School District") == "K12_SCHOOL_DISTRICT"
    assert _guess_entity_type("Ohio State University") == "HIGHER_EDUCATION"


def test_visit_health_not_just_http_200():
    assert classify_visit_health(status_code=200, text="Open Bids RFP Solicitation", opportunities=3) == "HEALTHY"
    assert classify_visit_health(status_code=200, text="Welcome. Please sign in to continue.", opportunities=0) == "AUTH_REQUIRED"
    assert classify_visit_health(status_code=200, text="Current solicitations and bid opportunities", opportunities=0) == "DEGRADED"
    assert classify_visit_health(status_code=403, text="Forbidden", opportunities=0) == "BLOCKED"
    assert classify_visit_health(status_code=401, text="login required", opportunities=0) == "AUTH_REQUIRED"
    assert classify_visit_health(status_code=503, text="", opportunities=0) == "UNAVAILABLE"


def test_paid_aggregator_exclusion_on_portal_register(tmp_path: Path):
    assert is_paid_aggregator_url("https://www.demandstar.com/agency/x") is True
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
        max_seed_attempts=0,
        max_secondary_visits=0,
    )
    pid = sweep._register_portal(
        {
            "source_name": "DemandStar",
            "url": "https://www.demandstar.com/agency/x",
            "government_level": "NETWORK",
        }
    )
    assert pid == ""
    assert any("paid_aggregator" in str(f.get("reason")) for f in sweep.failures)


def test_platform_portal_entity_relationships_and_multi_portal(tmp_path: Path):
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    p1 = sweep._register_portal(
        {
            "source_name": "District Bonfire",
            "url": "https://vendors.bonfirehub.com/district-x",
            "platform_family": "Bonfire",
            "government_level": "LOCAL",
            "state": "TX",
        }
    )
    p2 = sweep._register_portal(
        {
            "source_name": "District own site",
            "url": "https://district-x.k12.tx.us/purchasing",
            "platform_family": "SimpleHTML",
            "government_level": "LOCAL",
            "state": "TX",
        }
    )
    eid = sweep._register_entity(
        {
            "name": "District X ISD",
            "entity_type": "K12_SCHOOL_DISTRICT",
            "state": "TX",
            "procurement_portals": [
                "https://vendors.bonfirehub.com/district-x",
                "https://district-x.k12.tx.us/purchasing",
            ],
        }
    )
    sweep._link(eid, p1, url="https://vendors.bonfirehub.com/district-x", platform="Bonfire")
    sweep._link(eid, p2, url="https://district-x.k12.tx.us/purchasing", platform="SimpleHTML")
    assert len(sweep.portals) == 2
    assert len(sweep.entities[eid]["procurement_portals"]) == 2
    assert len(sweep.relationships) == 2


def test_entity_deduplication_and_state_entity_id(tmp_path: Path):
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    a = sweep._register_entity(
        {"name": "State of IL", "entity_type": "STATE", "state": "IL", "government_level": "STATE"}
    )
    b = sweep._register_entity(
        {"name": "Illinois", "entity_type": "STATE", "state": "IL", "government_level": "STATE"}
    )
    assert a == b
    assert a.startswith("ent:state:IL")
    assert len(sweep.entities) == 1


def test_public_access_and_provenance_on_mocked_visit(tmp_path: Path):
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
        max_secondary_visits=0,
    )
    fake = {
        "ok": True,
        "status_code": 200,
        "text": "<html><body>Open Bids Solicitation RFP #123 Closing date</body></html>",
        "final_url": "https://ssl.doas.state.ga.us/gpr/",
        "error": None,
    }
    with patch("procurement_source_seed_sweep.live_http_get", return_value=fake):
        with patch("procurement_source_seed_sweep._try_parse_opportunities", return_value=[]):
            with patch(
                "procurement_source_seed_sweep.validate_source_candidate",
                return_value={"promote": False, "state": "OK"},
            ):
                visit = sweep.visit(
                    {
                        "seed_id": "seed:ga:test",
                        "seed_name": "Georgia Procurement Registry",
                        "url": "https://ssl.doas.state.ga.us/gpr/",
                        "kind": "STATE",
                        "government_level": "STATE",
                        "state": "GA",
                        "platform_family": "SimpleHTML",
                        "role": "PROCUREMENT_ENTRY",
                        "provenance": "seed_catalog",
                    }
                )
    assert visit["health"] == "DEGRADED"
    assert visit["access_level"] != ACCESS_PAID
    portal = next(iter(sweep.portals.values()))
    assert portal.get("provenance") == "seed_catalog"
    assert "ent:state:GA" in sweep.entities


def test_state_coverage_report_includes_all_50(tmp_path: Path):
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    reports = sweep.build_reports(seeds_loaded=10, seeds_attempted=0)
    states = reports["states"]["states"]
    assert len(states) == 50
    codes = {r["state"] for r in states}
    assert "CA" in codes and "TX" in codes and "WY" in codes


def test_artifact_generation(tmp_path: Path):
    sweep = ProcurementSourceSeedSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    sweep._register_portal(
        {
            "source_name": "Test Portal",
            "url": "https://example.gov/bids",
            "government_level": "STATE",
            "state": "IA",
            "source_health": "HEALTHY",
            "provenance": "test",
        }
    )
    reports = sweep.build_reports(seeds_loaded=1, seeds_attempted=1)
    paths = sweep.write_artifacts(reports)
    required = [
        "procurement_source_inventory.json",
        "procurement_entity_inventory.json",
        "source_entity_relationships.json",
        "source_coverage_report.json",
        "state_coverage_report.json",
        "source_failures.json",
        "source_discovery_run.json",
    ]
    for name in required:
        assert name in paths
        assert Path(paths[name]).is_file()
