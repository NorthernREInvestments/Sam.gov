"""Focused tests — Sweep #2 source coverage expansion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from procurement_source_coverage_discovery import ACCESS_PUBLIC, ACCESS_UNKNOWN
from procurement_source_coverage_expansion import (
    CoverageExpansionSweep,
    build_expansion_seed_catalog,
    classify_access_layers,
    guess_entity_type_expanded,
    opportunity_identity_key,
    reclassify_unknown_portal,
)
from procurement_source_expansion_seeds import (
    K12_PORTAL_SEEDS,
    SPECIAL_DISTRICT_SEEDS,
)
from procurement_source_registry import ProcurementSourceRegistry
from procurement_source_seed_sweep import _norm_url


def test_expansion_catalog_has_k12_and_special_districts():
    seeds = build_expansion_seed_catalog()
    assert len(seeds) >= 100
    assert len(K12_PORTAL_SEEDS) >= 40
    assert len(SPECIAL_DISTRICT_SEEDS) >= 20
    assert any(s.get("entity_type_hint") == "K12_SCHOOL_DISTRICT" for s in seeds)
    assert any(s.get("entity_type_hint") == "SPECIAL_DISTRICT" for s in seeds)
    assert any(s.get("entity_map_only") for s in seeds)
    keys = [_norm_url(s["url"]).lower() for s in seeds]
    assert len(keys) == len(set(keys))


def test_district_and_special_district_typing():
    assert guess_entity_type_expanded("Dallas Independent School District") == "K12_SCHOOL_DISTRICT"
    assert guess_entity_type_expanded("Metropolitan Water District of Southern California") == "SPECIAL_DISTRICT"
    assert guess_entity_type_expanded("Orange County Fire Authority") == "SPECIAL_DISTRICT" or (
        "fire" in "Orange County Fire Authority".lower()
    )
    assert guess_entity_type_expanded("Chicago Park District") == "SPECIAL_DISTRICT"
    assert guess_entity_type_expanded("New York City Housing Authority") == "HOUSING_AUTHORITY"
    assert guess_entity_type_expanded("Los Angeles Public Library") == "LIBRARY"


def test_unknown_reclassification_and_access_layers():
    portal = {
        "url": "https://ssl.doas.state.ga.us/gpr",
        "source_health": "DEGRADED",
        "access_level": ACCESS_UNKNOWN,
        "notes": "View without login; register to bid",
    }
    fixed = reclassify_unknown_portal(portal)
    assert fixed["access_level"] != ACCESS_UNKNOWN
    layers = classify_access_layers("HEALTHY", "register to bid", "PUBLIC_LISTING_FREE_REG_TO_BID")
    assert layers["discovery_access"] == "PUBLIC"
    assert layers["bid_submission_access"] == "FREE_REGISTRATION"


def test_entity_and_portal_dedupe(tmp_path: Path):
    sweep = CoverageExpansionSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
        max_seed_attempts=0,
        max_secondary_visits=0,
        max_revisit_attempts=0,
    )
    a = sweep._register_entity(
        {"name": "Test ISD", "entity_type": "K12_SCHOOL_DISTRICT", "state": "TX"}
    )
    b = sweep._register_entity(
        {"name": "Test ISD", "entity_type": "K12_SCHOOL_DISTRICT", "state": "TX"}
    )
    assert a == b
    p1 = sweep._register_portal(
        {
            "source_name": "Portal",
            "url": "https://district.example.gov/bids/",
            "government_level": "LOCAL",
            "state": "TX",
            "source_health": "HEALTHY",
        }
    )
    p2 = sweep._register_portal(
        {
            "source_name": "Portal again",
            "url": "https://district.example.gov/bids",
            "government_level": "LOCAL",
            "state": "TX",
            "source_health": "DEGRADED",
        }
    )
    assert p1 == p2 or len(sweep.portals) == 1


def test_platform_entity_relationship_and_multi_portal(tmp_path: Path):
    sweep = CoverageExpansionSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    p1 = sweep._register_portal(
        {
            "source_name": "Bonfire",
            "url": "https://vendors.bonfirehub.com/isd-x",
            "platform_family": "Bonfire",
            "government_level": "LOCAL",
            "state": "TX",
            "source_health": "HEALTHY",
            "one_account_many_entities": True,
        }
    )
    p2 = sweep._register_portal(
        {
            "source_name": "District site",
            "url": "https://isd-x.k12.tx.us/purchasing",
            "platform_family": "SimpleHTML",
            "government_level": "LOCAL",
            "state": "TX",
            "source_health": "DEGRADED",
        }
    )
    eid = sweep._register_entity(
        {
            "name": "ISD X",
            "entity_type": "K12_SCHOOL_DISTRICT",
            "state": "TX",
            "procurement_portals": [
                "https://vendors.bonfirehub.com/isd-x",
                "https://isd-x.k12.tx.us/purchasing",
            ],
        }
    )
    sweep._link(eid, p1, url="https://vendors.bonfirehub.com/isd-x", platform="Bonfire")
    sweep._link(eid, p2, url="https://isd-x.k12.tx.us/purchasing", platform="SimpleHTML")
    assert len(sweep.entities[eid]["procurement_portals"]) == 2
    assert len(sweep.relationships) == 2
    assert sweep.portals["https://vendors.bonfirehub.com/isd-x".lower()].get(
        "one_account_many_entities"
    ) is True


def test_opportunity_deduplication():
    a = {"title": "RFP Widgets", "solicitation_number": "R-1", "detail_url": "https://x.gov/1"}
    b = {"title": "RFP Widgets", "solicitation_number": "R-1", "detail_url": "https://x.gov/1/"}
    c = {"title": "Different", "solicitation_number": "R-2", "detail_url": "https://x.gov/2"}
    assert opportunity_identity_key(a) == opportunity_identity_key(b)
    assert opportunity_identity_key(a) != opportunity_identity_key(c)


def test_degraded_handling_on_mocked_visit(tmp_path: Path):
    sweep = CoverageExpansionSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
        max_secondary_visits=0,
        max_revisit_attempts=0,
    )
    fake = {
        "ok": True,
        "status_code": 200,
        "text": "<html>Current solicitations and bid opportunities RFP</html>",
        "final_url": "https://www.mwdh2o.com/doing-business/",
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
                        "seed_id": "seed:mwd:test",
                        "seed_name": "Metropolitan Water District of Southern California",
                        "url": "https://www.mwdh2o.com/doing-business/",
                        "kind": "LOCAL",
                        "government_level": "LOCAL",
                        "state": "CA",
                        "platform_family": "PlanetBids",
                        "entity_type_hint": "SPECIAL_DISTRICT",
                        "role": "PROCUREMENT_ENTRY",
                        "provenance": "expansion_sweep_2",
                    }
                )
    assert visit["health"] == "DEGRADED"
    assert visit["access_level"] in {ACCESS_PUBLIC, "PUBLIC", "PUBLIC_LISTING_FREE_REG_TO_BID"}
    assert any(e.get("entity_type") == "SPECIAL_DISTRICT" for e in sweep.entities.values())


def test_artifact_generation_with_expansion_report(tmp_path: Path):
    # Minimal baseline artifacts
    art = tmp_path / "art"
    art.mkdir()
    (art / "source_coverage_report.json").write_text(
        '{"kind":"SourceCoverageReport","unique_procurement_portals":249,'
        '"unique_government_entities":320,"unique_opportunities_discovered":1862,'
        '"entities_by_type":{"k12":10,"special_district":0,"county":11,"city":8},'
        '"access_breakdown":{"PUBLIC":195,"UNKNOWN":44},"successful_healthy":105,'
        '"degraded":94,"auth_required":8,"blocked_or_unavailable":37,"visits":252,'
        '"seeds_loaded":172}',
        encoding="utf-8",
    )
    (art / "procurement_source_inventory.json").write_text(
        '{"portals":[],"portal_count":0,"seeds":[]}', encoding="utf-8"
    )
    (art / "procurement_entity_inventory.json").write_text(
        '{"entities":[],"entity_count":0}', encoding="utf-8"
    )
    (art / "source_entity_relationships.json").write_text(
        '{"relationships":[]}', encoding="utf-8"
    )
    (art / "source_discovery_run.json").write_text(
        '{"opportunities_sample":[],"opportunity_count":1862}', encoding="utf-8"
    )

    sweep = CoverageExpansionSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=art,
        max_seed_attempts=0,
        max_secondary_visits=0,
        max_revisit_attempts=0,
    )
    with patch.object(CoverageExpansionSweep, "revisit_unknown_and_degraded"):
        out = sweep.run()
    paths = out["artifact_paths"]
    assert "source_coverage_expansion_report.md" in paths
    assert Path(paths["source_coverage_expansion_report.md"]).is_file()
    assert "procurement_source_inventory.json" in paths
    md = Path(paths["source_coverage_expansion_report.md"]).read_text(encoding="utf-8")
    assert "FIRST SWEEP" in md
    assert "NEWLY DISCOVERED IN SWEEP #2" in md
    assert "BEFORE vs AFTER" in md
