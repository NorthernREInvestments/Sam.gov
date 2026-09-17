"""Live verification semantics + diversified TINY selection — mocked HTTP only."""

from __future__ import annotations

from discovery.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BLOCKED,
    ADAPTER_BROKEN,
    ADAPTER_DEGRADED,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_UNVERIFIED_LIVE,
)
from discovery.coverage import build_coverage_report
from discovery.http_client import HttpResponse, PublicProcurementHttpClient, RequestBudget, RequestMeta
from discovery.live_fetchers import JaggaerPublicLiveFetcher
from discovery.selection import (
    asserts_not_alpha_first_five,
    select_diversified_sources,
    select_validation_candidates,
)
from discovery.state_matrix import all_states_enriched, state_coverage_summary
from discovery.validate import validate_sources
from discovery.verification import build_validation_evidence, map_validation_to_adapter_status


SCIQUEST_HTML = """
<html><body>
<h2>Business Opportunities</h2>
<a href="/register">Register</a> <a href="/login">Supplier Login</a>
<table class="phx table-hover">
<tr><th>Status</th><th>Title</th><th>Details</th></tr>
<tr>
<td><span class="mosaic status-badge status-badge-blue">Open</span></td>
<td><div><a class="btn btn-link btn-large btn-link-header" id="InvestmentBanking" href="https://app01.jaggaer.com/apps/Router/ViewSourcingEvent?AuthToken=REDACTED">Investment Banking Services for the State Revolving Fund</a></div>
<div>Open 9/1/2026, 8:00 AM CDT Close 12/1/2026, 2:00 PM CST
<div class="phx table-layout">
<div class="phx table-row-layout"><div class="phx table-cell-layout"><div class="phx data-row-name"><div id="SourcingPublicSite_LABEL_TYPE" class="phx phxText">Type</div></div></div><div class="phx table-cell-layout"><div class="phx data-row-content">RFP</div></div></div>
<div class="phx table-row-layout"><div class="phx table-cell-layout"><div class="phx data-row-name"><div id="SourcingPublicSite_LABEL_NUMBER" class="phx phxText">Number</div></div></div><div class="phx table-cell-layout"><div class="phx data-row-content">270-RFP-3051-2027</div></div></div>
<a href="https://solutions-selectsite-documents.s3.amazonaws.com/Sourcingevent/1449001-event.pdf">View as PDF</a>
</div></td></tr>
<tr>
<td><span class="mosaic status-badge status-badge-blue">Open</span></td>
<td><div><a class="btn btn-link btn-large btn-link-header" id="FoodItems" href="https://app01.jaggaer.com/apps/Router/ViewSourcingEvent?AuthToken=REDACTED">Food Items for Central Warehouse</a></div>
<div>Open 9/2/2026, 8:00 AM CDT Close 11/15/2026, 3:00 PM CST
<div class="phx table-layout">
<div class="phx table-row-layout"><div class="phx table-cell-layout"><div class="phx data-row-name"><div id="SourcingPublicSite_LABEL_TYPE_2" class="phx phxText">Type</div></div></div><div class="phx table-cell-layout"><div class="phx data-row-content">RFB</div></div></div>
<div class="phx table-row-layout"><div class="phx table-cell-layout"><div class="phx data-row-name"><div id="SourcingPublicSite_LABEL_NUMBER_2" class="phx phxText">Number</div></div></div><div class="phx table-cell-layout"><div class="phx data-row-content">005-RFB-3032-2027</div></div></div>
<a href="https://solutions-selectsite-documents.s3.amazonaws.com/Sourcingevent/1449002-event.pdf">View as PDF</a>
</div></td></tr>
</table>
</body></html>
"""


def _resp(url: str, text: str, status: int = 200, content_type: str = "text/html") -> HttpResponse:
    raw = text.encode("utf-8")
    return HttpResponse(
        text=text,
        content=raw,
        status_code=status,
        headers={"content-type": content_type},
        meta=RequestMeta(url=url, http_status=status, content_type=content_type, bytes_len=len(raw)),
    )


def test_unvalidated_fetcher_not_live_verified():
    from discovery.state_matrix import _STATUS_OVERRIDES

    _STATUS_OVERRIDES.clear()
    summary = state_coverage_summary()
    # Without loading persisted overrides, matrix seed has zero LIVE_VERIFIED
    assert summary["states_LIVE_VERIFIED"] == 0
    assert summary["states_UNVERIFIED_LIVE"] >= 10
    assert summary["states_LIVE_CAPABLE"] == 0  # strict alias
    cov = build_coverage_report(load_persisted=False)
    assert cov["TOTAL_LIVE_VERIFIED_SOURCES"] == 0
    assert cov["TOTAL_UNVERIFIED_LIVE_SOURCES"] > 0


def test_404_403_login_410_cannot_be_live_verified():
    assert map_validation_to_adapter_status(
        http_status=404, validation={"valid": False, "failure_type": "HTTP_404", "health_status": "BROKEN"},
        records_found=0, parser_ok=False,
    ) == ADAPTER_BROKEN
    assert map_validation_to_adapter_status(
        http_status=403, validation={"valid": False, "failure_type": "AUTH_REQUIRED", "health_status": "AUTH_REQUIRED"},
        records_found=0, parser_ok=False,
    ) == ADAPTER_AUTH_REQUIRED
    assert map_validation_to_adapter_status(
        http_status=200, validation={"valid": False, "failure_type": "AUTH_REQUIRED", "health_status": "AUTH_REQUIRED"},
        records_found=0, parser_ok=False,
    ) == ADAPTER_AUTH_REQUIRED
    assert map_validation_to_adapter_status(
        http_status=410, validation={"valid": False, "failure_type": "HTTP_410", "health_status": "BROKEN"},
        records_found=0, parser_ok=False,
    ) == ADAPTER_BROKEN
    assert map_validation_to_adapter_status(
        http_status=200, validation={"valid": False, "failure_type": "CAPTCHA", "health_status": "BLOCKED"},
        records_found=0, parser_ok=False,
    ) == ADAPTER_BLOCKED


def test_zero_records_alone_cannot_prove_live_verified():
    st = map_validation_to_adapter_status(
        http_status=200,
        validation={"valid": True, "health_status": "HEALTHY", "zero_records_ok": True},
        records_found=0,
        parser_ok=True,
    )
    assert st == ADAPTER_DEGRADED
    assert st != ADAPTER_LIVE_VERIFIED


def test_successful_listing_promotes_to_live_verified():
    from discovery.schema import CanonicalOpportunity

    opps = [CanonicalOpportunity(external_id="1", source_id="x", title="Network Switches Bid")]
    evidence = build_validation_evidence(
        source_id="state_ia",
        list_url="https://example.test",
        http_status=200,
        validation={"valid": True, "health_status": "HEALTHY", "warnings": []},
        records_found=1,
        opportunities=opps,
        requests=1,
    )
    assert evidence["adapter_status"] == ADAPTER_LIVE_VERIFIED
    assert evidence["live_verified"] is True


def test_failed_validation_downgrades():
    evidence = build_validation_evidence(
        source_id="state_al",
        list_url="https://example.test",
        http_status=404,
        validation={"valid": False, "failure_type": "HTTP_404", "health_status": "BROKEN", "warnings": ["http_404"]},
        records_found=0,
        opportunities=[],
        requests=1,
    )
    assert evidence["adapter_status"] == ADAPTER_BROKEN


def test_validation_evidence_persisted():
    from database import SessionLocal
    from discovery.registry import seed_discovery_sources
    from discovery.verification import persist_validation_result
    from models import DiscoverySource

    session = SessionLocal()
    try:
        seed_discovery_sources(session)
        session.commit()
        evidence = build_validation_evidence(
            source_id="state_ia",
            list_url="https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
            http_status=200,
            validation={"valid": True, "health_status": "HEALTHY", "warnings": []},
            records_found=2,
            opportunities=JaggaerPublicLiveFetcher().parse_listing(SCIQUEST_HTML, list_url="https://x"),
            requests=1,
        )
        out = persist_validation_result(session, evidence)
        session.commit()
        assert out["persisted"] is True
        row = session.query(DiscoverySource).filter_by(source_id="state_ia").first()
        assert row is not None
        assert row.adapter_status == ADAPTER_LIVE_VERIFIED
        assert row.last_live_http_status == 200
        assert row.last_live_record_count == 2
        assert row.last_live_validation_result == ADAPTER_LIVE_VERIFIED
    finally:
        session.rollback()
        session.close()


def test_coverage_counts_verified_separately():
    cov = build_coverage_report()
    assert "LIVE_VERIFIED" in cov["counts"]
    assert "UNVERIFIED_LIVE" in cov["counts"]
    assert cov["counts"]["LIVE_VERIFIED"] == cov["TOTAL_LIVE_VERIFIED_SOURCES"]


def test_tiny_diversified_selection_not_alpha_five():
    selected = select_diversified_sources(max_sources=5)
    assert len(selected) == 5
    assert asserts_not_alpha_first_five(selected)
    ids = [s["source_id"] for s in selected]
    assert ids != ["state_al", "state_ak", "state_az", "state_ar", "state_ca"]
    kinds = {s["kind"] for s in selected}
    assert "STATE" in kinds
    # Should include local and/or cooperative in mix
    assert kinds & {"LOCAL", "COOPERATIVE", "FEDERAL", "STATE"}


def test_state_local_cooperative_can_coexist_in_tiny():
    selected = select_diversified_sources(max_sources=5)
    kinds = [s["kind"] for s in selected]
    assert kinds.count("STATE") >= 1
    assert "LOCAL" in kinds or "COOPERATIVE" in kinds


def test_validation_candidates_at_least_ten_states_priority():
    cands = select_validation_candidates(max_sources=15)
    assert len(cands) >= 10
    state_cands = [c for c in cands if c["kind"] == "STATE"]
    assert len(state_cands) >= 5
    assert "state_al" not in [c["source_id"] for c in cands[:3]]


def test_first_five_states_honest_classification():
    from discovery.state_matrix import STATE_MATRIX, _STATUS_OVERRIDES, enrich_state_row

    _STATUS_OVERRIDES.clear()
    by = {r["state"]: enrich_state_row(r, status_overrides={}) for r in STATE_MATRIX}
    assert by["AL"]["adapter_status"] == ADAPTER_AUTH_REQUIRED
    assert by["AK"]["adapter_status"] == ADAPTER_AUTH_REQUIRED
    assert by["AZ"]["adapter_status"] == ADAPTER_AUTH_REQUIRED
    assert by["AR"]["adapter_status"] == ADAPTER_UNVERIFIED_LIVE
    assert "sas.arkansas.gov" in by["AR"]["list_url"]
    assert by["CA"]["adapter_status"] == ADAPTER_BLOCKED


def test_jaggaer_parser_extracts_sciquest_open_events():
    opps = JaggaerPublicLiveFetcher().parse_listing(SCIQUEST_HTML, list_url="https://bids.sciquest.com/x")
    assert len(opps) >= 2
    assert any("270-RFP-3051" in (o.solicitation_number or "") for o in opps)


def test_validate_sources_mocked_promotes_and_no_sam():
    from discovery.state_matrix import _STATUS_OVERRIDES

    _STATUS_OVERRIDES.clear()

    def transport(url, headers=None, timeout=None):
        if "sciquest" in url or "DASIowa" in url or "Montana" in url:
            return _resp(url, SCIQUEST_HTML)
        if "sourcewell" in url:
            from pathlib import Path

            sw = (Path(__file__).resolve().parents[1] / "discovery" / "fixtures" / "sourcewell_open_solicitations.html").read_text(
                encoding="utf-8"
            )
            return _resp(url, sw)
        if "nebraska" in url:
            return _resp(
                url,
                "<html><body><h1>Bid Opportunities</h1><table>"
                "<tr><td>Title</td><td>Sol</td><td>Deadline</td></tr>"
                "<tr><td><a href='https://das.nebraska.gov/bid/1'>Network Switches Equipment Purchase RFP</a></td>"
                "<td>NE-IFB-100</td><td>12/01/2026</td></tr></table></body></html>",
            )
        if "arkansas" in url or "sas." in url:
            return _resp(
                url,
                "<html><body><a href='https://sas.arkansas.gov/bid/1'>SP-27-019-IFB AWIN Tower Generator Maintenance</a>"
                " Current Solicitations Bid Opportunities</body></html>",
            )
        return _resp(url, "<html><body>Bid Opportunities solicitation listing No solicitations at this time.</body></html>")

    out = validate_sources(
        None,
        authorize_live=False,
        transport=transport,
        max_sources=8,
        max_requests_per_source=1,
        max_total_requests=20,
        persist_registry=False,
        source_ids=[
            "state_ia",
            "state_mt",
            "state_ne",
            "state_ar",
            "coop_sourcewell_live",
        ],
    )
    assert out["SAM"] == 0 and out["OpenAI"] == 0 and out["USAspending"] == 0 and out["paid"] == 0
    assert out["opportunities_persisted"] == 0
    assert out["LIVE_VERIFIED"] >= 3
    _STATUS_OVERRIDES.clear()


def test_no_sam_openai_in_validate_module():
    import inspect
    import discovery.validate as v

    src = inspect.getsource(v)
    assert "sam_client" not in src
    assert "api.sam.gov" not in src
    assert "from openai" not in src
