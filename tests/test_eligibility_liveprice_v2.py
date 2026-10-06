"""Tests for eligibility file mining + liveprice v2."""

from eligibility_and_recovery.file_mining import BUILD, mine_opportunity_eligibility, extract_document_pages
from public_price_search.search import reset_serp_circuit, serp_circuit_open, search_web, route_health_snapshot
from public_price_search.queries import build_query_variants


def test_build():
    assert BUILD.startswith("20261004-m3-eligibility-liveprice")


def test_query_variants_include_new():
    qs = build_query_variants({"part_number": "5579409PX", "manufacturer": "Cummins"})
    assert any("new" in q.lower() for q in qs)
    assert any("distributor" in q.lower() for q in qs)


def test_serp_circuit_does_not_raise_and_is_serp_only():
    reset_serp_circuit()
    # Force circuit via flag
    import public_price_search.search as s

    s._SERP_SKIP = True
    r = search_web("test query that should skip", use_budget=False)
    assert r.get("skipped") == "SERP_CIRCUIT_OPEN"
    assert serp_circuit_open() is True
    snap = route_health_snapshot()
    assert "SERP" in snap
    reset_serp_circuit()
    assert serp_circuit_open() is False


def test_mine_go_metro_has_docs():
    ev = mine_opportunity_eligibility("opengov:go-metro:298984")
    assert ev["file_coverage"]["documents_total"] >= 1
    assert ev["eligibility_status"] in {
        "BID_ELIGIBLE",
        "BID_ELIGIBLE_WITH_ACTION",
        "ELIGIBILITY_UNKNOWN",
        "BID_INELIGIBLE",
    }
    assert "file_coverage" in ev
