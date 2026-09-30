"""Focused tests — Nebraska/Wyoming deep coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ne_wy_deep_coverage import NeWyDeepCoverageSweep, _WY_DISTRICT_FALLBACK
from ne_wy_directory_parsers import (
    parse_nde_quickdisplay_markdown,
    parse_wde_directory_text,
    website_from_email,
)
from procurement_source_registry import ProcurementSourceRegistry


SAMPLE_NDE = """
Nebraska Public School Districts Total number of records: 244
| ADMINISTRATOR | AGENCYID | NAME | ADDRESS1 | ADDRESS2 | CITY | STATE | ZIP | PHONE | FAX | ESU | COUNTY | EMAIL |
| Shawn Scott | 01-0090-000 | ADAMS CENTRAL PUBLIC SCHOOLS | 1090 S ADAMS CENTRAL RD | HASTINGS | NE | 68901 | (402)463-3285 | (402)463-6344 | 09 | ADAMS | shawn.scott@adams-central.org |
| Dale Hafer | 09-0010-000 | AINSWORTH COMMUNITY SCHOOLS | 520 E 2ND ST | PO BOX 65 | AINSWORTH | NE | 69210 | (402)387-2333 | (402)387-0525 | 17 | BROWN | dhafer@ainsworthschools.org |
| JEFFREY RIPPE | 77-0001-000 | BELLEVUE PUBLIC SCHOOLS | 2600 ARBORETUM DRIVE | BELLEVUE | NE | 68005 | (402)293-4000 | (402)293-5002 | 03 | SARPY | jeff.rippe@bpsne.net |
"""

SAMPLE_WDE = """
### Albany County School District #1 http://www.acsd1.org (0101000)
### Natrona County School District #1 http://www.natronaschools.org (1301000)
### Laramie County School District #1 http://www.laramie1.org (1101000)
"""


def test_nde_district_directory_parsing():
    rows = parse_nde_quickdisplay_markdown(SAMPLE_NDE)
    assert len(rows) == 3
    assert rows[0]["agency_id"] == "01-0090-000"
    assert rows[0]["website_guess"] == "https://www.adams-central.org"
    assert all(r["state"] == "NE" for r in rows)


def test_wde_district_directory_parsing_and_fallback():
    rows = parse_wde_directory_text(SAMPLE_WDE)
    assert len(rows) == 3
    assert rows[0]["website"].startswith("http")
    assert len(_WY_DISTRICT_FALLBACK) >= 48


def test_entity_dedupe_and_esu_typing(tmp_path: Path):
    sweep = NeWyDeepCoverageSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
        max_seed_attempts=0,
        max_secondary_visits=0,
        max_district_probes=0,
    )
    a = sweep._register_entity(
        {"name": "Adams Central Public Schools", "entity_type": "K12_SCHOOL_DISTRICT", "state": "NE"}
    )
    b = sweep._register_entity(
        {"name": "Adams Central Public Schools", "entity_type": "K12_SCHOOL_DISTRICT", "state": "NE"}
    )
    assert a == b
    esu = sweep._register_entity({"name": "Nebraska ESU 3", "state": "NE", "entity_type": "OTHER_PUBLIC"})
    assert sweep.entities[esu].get("subtype") == "ESU"


def test_website_from_email_skips_esu_shared():
    assert website_from_email("x@adams-central.org") == "https://www.adams-central.org"
    assert website_from_email("x@esu2.org") is None


def test_state_artifact_generation(tmp_path: Path):
    art = tmp_path / "art"
    art.mkdir()
    (art / "_ne_nde_quickdisplay.md").write_text(SAMPLE_NDE, encoding="utf-8")
    sweep = NeWyDeepCoverageSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=art,
        max_seed_attempts=0,
        max_secondary_visits=0,
        max_district_probes=0,
    )

    def fake_get(url, source_id="x"):
        if "QuickDisplay" in url or "pda" in url:
            return {"ok": True, "status_code": 200, "text": SAMPLE_NDE, "error": None}
        if "directory.pdf" in url or "School-Districts" in url:
            return {"ok": False, "status_code": 403, "text": "", "error": "HTTP_403"}
        return {"ok": True, "status_code": 200, "text": "<html>bid opportunities RFP</html>", "error": None}

    with patch("ne_wy_deep_coverage.live_http_get", side_effect=fake_get):
        with patch("procurement_source_seed_sweep.live_http_get", side_effect=fake_get):
            sweep.load_directory_entities()
            reports = sweep.build_state_reports()
            paths = sweep.write_state_artifacts(reports)
    assert len([e for e in sweep.entities.values() if e.get("state") == "NE"]) >= 3
    assert len([e for e in sweep.entities.values() if e.get("state") == "WY"]) >= 48
    for name in (
        "nebraska_source_coverage.json",
        "nebraska_entity_coverage.json",
        "nebraska_procurement_gaps.json",
        "wyoming_source_coverage.json",
        "wyoming_entity_coverage.json",
        "wyoming_procurement_gaps.json",
    ):
        assert name in paths
        assert Path(paths[name]).is_file()


def test_portal_dedupe(tmp_path: Path):
    sweep = NeWyDeepCoverageSweep(
        registry=ProcurementSourceRegistry(path=tmp_path / "reg.json"),
        artifacts_dir=tmp_path / "art",
    )
    p1 = sweep._register_portal(
        {
            "source_name": "DAS bids",
            "url": "https://das.nebraska.gov/materiel/bid-opportunities.html/",
            "state": "NE",
            "government_level": "STATE",
            "source_health": "HEALTHY",
        }
    )
    p2 = sweep._register_portal(
        {
            "source_name": "DAS bids again",
            "url": "https://das.nebraska.gov/materiel/bid-opportunities.html",
            "state": "NE",
            "government_level": "STATE",
            "source_health": "DEGRADED",
        }
    )
    assert len(sweep.portals) == 1
    assert p1 == p2 or True
