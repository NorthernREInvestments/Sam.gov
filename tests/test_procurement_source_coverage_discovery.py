"""Focused tests — procurement source coverage discovery inventory."""

from __future__ import annotations

import json
from pathlib import Path

from procurement_source_coverage_discovery import (
    ACCESS_LIMITED_FREE,
    ACCESS_PAID,
    ACCESS_PUBLIC,
    ACCESS_PUBLIC_PLUS_FREE_BID,
    ACCESS_UNKNOWN,
    COVERAGE_EXPANSION_ENTITIES,
    ProcurementSourceCoverageDiscovery,
    classify_access_level,
    entity_id_for,
    extract_bidnet_buyer_names,
    is_paid_aggregator_url,
    source_identity_key,
)
from procurement_source_registry import ProcurementSourceRegistry, source_record


def test_paid_aggregator_exclusion():
    assert is_paid_aggregator_url("https://www.demandstar.com/agency/foo") is True
    assert is_paid_aggregator_url("https://www.bidnetdirect.com/illinois/solicitations/open-bids") is False
    assert (
        classify_access_level({"discovery_url": "https://govwin.com/x", "publicly_searchable": True})
        == ACCESS_PAID
    )


def test_public_and_free_registration_classification():
    assert (
        classify_access_level(
            {"publicly_searchable": True, "docs_public": True, "auth_requirement": "NONE"}
        )
        == ACCESS_PUBLIC
    )
    assert (
        classify_access_level(
            {
                "publicly_searchable": True,
                "registration_required_to_bid": True,
                "notes": "View without login; register to bid",
            }
        )
        == ACCESS_PUBLIC_PLUS_FREE_BID
    )
    assert (
        classify_access_level(
            {
                "health_state": "REGISTRATION_REQUIRED",
                "auth_requirement": "ACCOUNT_REQUIRED",
                "publicly_searchable": False,
            }
        )
        == "FREE_REGISTRATION"
    )
    assert (
        classify_access_level(
            {
                "limited_free_tier": True,
                "notes": "marketing vendor-registration; open bids not publicly listable",
                "publicly_searchable": False,
            }
        )
        == ACCESS_LIMITED_FREE
    )
    assert classify_access_level({}) == ACCESS_UNKNOWN


def test_source_deduplication_by_url_identity():
    a = {"source_id": "s1", "discovery_url": "https://Example.com/bids/"}
    b = {"source_id": "s2", "portal_url": "https://example.com/bids"}
    assert source_identity_key(a) == source_identity_key(b)
    disc = ProcurementSourceCoverageDiscovery(
        registry=ProcurementSourceRegistry(path=Path("/tmp/m3_cov_reg_dedupe.json")),
        live_probe=False,
    )
    # avoid writing to real path — use tmp via constructor path that may not exist yet
    disc.registry = ProcurementSourceRegistry.__new__(ProcurementSourceRegistry)
    disc.registry.path = Path("artifacts/_tmp_cov_dedupe_registry.json")
    disc.registry._sources = {}
    disc._upsert_source(
        {
            "source_id": "alpha",
            "source_name": "Alpha",
            "discovery_url": "https://agency.example.gov/procurement",
            "provenance": "path_a",
            "government_level": "LOCAL",
        }
    )
    disc._upsert_source(
        {
            "source_id": "beta",
            "source_name": "Beta",
            "discovery_url": "https://agency.example.gov/procurement/",
            "provenance": "path_b",
            "government_level": "LOCAL",
        }
    )
    assert len(disc.sources) == 1
    only = next(iter(disc.sources.values()))
    assert "path_a" in (only.get("discovery_paths") or [])
    assert "path_b" in (only.get("discovery_paths") or [])


def test_platform_entity_distinction_and_multi_portal_entity():
    disc = ProcurementSourceCoverageDiscovery(live_probe=False)
    disc.registry = ProcurementSourceRegistry.__new__(ProcurementSourceRegistry)
    disc.registry.path = Path("artifacts/_tmp_cov_multi_registry.json")
    disc.registry._sources = {}
    eid = disc._register_entity(
        {
            "name": "School District X",
            "entity_type": "K12_SCHOOL_DISTRICT",
            "state": "TX",
            "government_level": "LOCAL",
        }
    )
    disc._upsert_source(
        {
            "source_id": "portal_a",
            "source_name": "District Bid Board",
            "discovery_url": "https://district.example.gov/bids",
            "government_level": "LOCAL",
            "source_platform": "Bonfire",
        }
    )
    disc._upsert_source(
        {
            "source_id": "portal_b",
            "source_name": "Public Works Portal",
            "discovery_url": "https://district.example.gov/public-works/bids",
            "government_level": "LOCAL",
            "source_platform": "SimpleHTML",
        }
    )
    disc._link(eid, "portal_a", portal_url="https://district.example.gov/bids", platform="Bonfire")
    disc._link(
        eid,
        "portal_b",
        portal_url="https://district.example.gov/public-works/bids",
        platform="SimpleHTML",
    )
    ent = disc.entities[eid]
    assert len(ent["procurement_portals"]) == 2
    assert len(disc.sources) == 2


def test_provenance_preserved_on_merge():
    disc = ProcurementSourceCoverageDiscovery(live_probe=False)
    disc.registry = ProcurementSourceRegistry.__new__(ProcurementSourceRegistry)
    disc.registry.path = Path("artifacts/_tmp_cov_prov.json")
    disc.registry._sources = {}
    disc._upsert_source(
        {
            "source_id": "st_ga",
            "source_name": "Georgia Procurement Registry",
            "discovery_url": "https://ssl.doas.state.ga.us/gpr/",
            "provenance": "state_matrix",
            "government_level": "STATE",
            "state": "GA",
        }
    )
    disc._upsert_source(
        {
            "source_id": "st_ga_dup",
            "source_name": "GA GPR",
            "discovery_url": "https://ssl.doas.state.ga.us/gpr",
            "provenance": "search_hit",
            "government_level": "STATE",
            "state": "GA",
        }
    )
    s = next(iter(disc.sources.values()))
    assert set(s.get("discovery_paths") or []) >= {"state_matrix", "search_hit"}


def test_bidnet_buyer_extraction_and_entity_ids():
    html = """
    <div class="agency">City of Springfield</div>
    <span data-agency="Lincoln County Schools">x</span>
    <div>Agency: Metro Transit Authority</div>
    """
    buyers = extract_bidnet_buyer_names(html, state_code="IL")
    assert any("Springfield" in b["name"] for b in buyers)
    a = entity_id_for(name="City of Springfield", entity_type="CITY_MUNICIPAL", state="IL")
    b = entity_id_for(name="City of Springfield", entity_type="CITY_MUNICIPAL", state="IL")
    assert a == b


def test_source_health_probe_annotation_offline():
    disc = ProcurementSourceCoverageDiscovery(live_probe=False)
    disc.ingest_registry_and_seeds()
    assert len(disc.sources) >= 50
    assert len(disc.entities) >= 50
    # All 50 states represented in state portal ingest
    states = {s.get("state") for s in disc.sources.values() if s.get("government_level") == "STATE"}
    assert len(states) >= 50


def test_state_coverage_reporting_and_artifacts(tmp_path):
    reg_path = tmp_path / "reg.json"
    reg = ProcurementSourceRegistry(path=reg_path)
    reg.upsert(
        source_record(
            source_id="state_il",
            source_name="Illinois BidBuy",
            discovery_url="https://www.bidbuy.illinois.gov/",
            government_level="STATE",
            entity_type="STATE",
            jurisdiction="IL",
            geographic_scope="IL",
            health_state="HEALTHY_PRODUCTION",
        )
    )
    disc = ProcurementSourceCoverageDiscovery(
        registry=reg,
        artifacts_dir=tmp_path,
        live_probe=False,
    )
    disc.ingest_registry_and_seeds()
    disc.ingest_coverage_expansion()
    reports = disc.build_reports()
    paths = disc.write_artifacts(reports)
    assert Path(paths["summary"]).exists()
    assert Path(paths["inventory"]).exists()
    assert Path(paths["relationships"]).exists()
    assert Path(paths["states"]).exists()
    assert Path(paths["gaps"]).exists()
    assert Path(paths["access"]).exists()
    assert Path(paths["results"]).exists()
    assert Path(paths["human"]).exists()
    summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    assert summary["total_unique_procurement_sources"] >= 1
    assert summary["honesty"]["paid_aggregators_not_counted_as_coverage"] is True
    assert summary["k12"]["coverage_percent_invented"] is False
    states = json.loads(Path(paths["states"]).read_text(encoding="utf-8"))
    assert len(states["states"]) == 50


def test_expansion_catalog_has_real_urls_not_empty():
    assert len(COVERAGE_EXPANSION_ENTITIES) >= 20
    for row in COVERAGE_EXPANSION_ENTITIES:
        assert row.get("name")
        assert str(row.get("procurement_url") or "").startswith("http")
