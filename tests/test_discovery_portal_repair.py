"""Portal parser repair — SciQuest live structure, Sourcewell, PublicPurchase, gates."""

from __future__ import annotations

from pathlib import Path

from discovery.classify import classify_discovery_opportunity
from discovery.http_client import HttpResponse, PublicProcurementHttpClient, RequestBudget, RequestMeta
from discovery.live_fetchers import (
    CooperativeOpenSolicitationFetcher,
    JaggaerPublicLiveFetcher,
    PublicPurchaseLiveFetcher,
)
from discovery.live_runner import run_live_discovery
from discovery.opportunity_gate import (
    is_garbage_solicitation_number,
    is_structurally_valid_opportunity,
    sanitize_deadline_raw,
)
from discovery.sciquest import parse_sciquest_public_events, sciquest_has_public_event_structure
from discovery.sourcewell import parse_sourcewell_open_solicitations
from discovery.validation import validate_listing_response

FIX = Path(__file__).resolve().parents[1] / "discovery" / "fixtures"
IA = (FIX / "sciquest_iowa_listing.html").read_text(encoding="utf-8")
MT = (FIX / "sciquest_montana_listing.html").read_text(encoding="utf-8")
SW = (FIX / "sourcewell_open_solicitations.html").read_text(encoding="utf-8")
IA_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"
MT_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana"
SW_URL = "https://www.sourcewell-mn.gov/solicitations"

PP_MARKETING = """
<html><body><h1>Best Deal</h1>
<p>All the benefits of free registration</p>
<p>Start Browsing Now</p>
<table><tr><td>Free Registration</td><td>Start Browsing Now</td><td>Select Region</td></tr></table>
</body></html>
"""

GATED_LOGIN = """
<html><body><h1>Sign In</h1>
<p>Please log in to view solicitations</p>
<form><input name="username"/><input type="password" name="password"/><button>Log in</button></form>
</body></html>
"""


def _resp(url: str, text: str) -> HttpResponse:
    b = text.encode("utf-8")
    return HttpResponse(
        text=text,
        content=b,
        status_code=200,
        headers={"content-type": "text/html"},
        meta=RequestMeta(url=url, http_status=200, content_type="text/html", bytes_len=len(b)),
    )


def test_login_chrome_not_auth_when_public_events_exist():
    assert sciquest_has_public_event_structure(IA)
    v = validate_listing_response(
        status_code=200,
        content_type="text/html",
        body=IA,
        records_found=4,
        structure_recognized=True,
    )
    assert v["valid"] is True
    assert v["health_status"] in {"HEALTHY", "DEGRADED"}
    assert v["failure_type"] is None
    assert any("login_chrome" in w for w in v.get("warnings") or [])


def test_gated_page_is_auth_required():
    v = validate_listing_response(
        status_code=200,
        content_type="text/html",
        body=GATED_LOGIN,
        records_found=0,
    )
    assert v["valid"] is False
    assert v["health_status"] == "AUTH_REQUIRED"


def test_sciquest_live_fixture_parses_distinct_events():
    for html, url in [(IA, IA_URL), (MT, MT_URL)]:
        opps = parse_sciquest_public_events(html, list_url=url)
        assert len(opps) >= 3
        ids = [o.external_id for o in opps]
        assert len(ids) == len(set(ids))
        for o in opps:
            assert o.solicitation_number
            assert o.solicitation_number.lower() != "open"
            assert o.status == "OPEN"
            assert o.title and "\t" not in o.title
            assert o.agency
            assert o.deadline_raw and "2026" in o.deadline_raw
            assert o.external_id.startswith("sciquest:")


def test_sciquest_shared_parser_iowa_montana():
    ia = JaggaerPublicLiveFetcher().parse_listing(IA, list_url=IA_URL)
    mt = JaggaerPublicLiveFetcher().parse_listing(MT, list_url=MT_URL)
    assert len(ia) >= 3 and len(mt) >= 3
    assert all(o.agency for o in ia + mt)


def test_http200_structure_zero_parse_is_degraded():
    v = validate_listing_response(
        status_code=200,
        content_type="text/html",
        body=IA,
        records_found=0,
        structure_recognized=True,
    )
    assert v["valid"] is False
    assert v["health_status"] == "DEGRADED"
    assert v["failure_type"] == "PARSER_FAILURE"


def test_publicpurchase_marketing_yields_zero():
    fetcher = PublicPurchaseLiveFetcher()
    opps = fetcher.parse_listing(
        PP_MARKETING,
        list_url="https://www.publicpurchase.com/gems/cheyenne/buyer/public/home",
    )
    assert opps == []


def test_marketing_fails_structural_gate():
    gate = is_structurally_valid_opportunity(
        {
            "title": "Best Deal — Free Registration",
            "solicitation_number": "Free Registration",
            "external_id": "Free Registration",
            "deadline_raw": "Start Browsing Now Select Region Alabama",
            "detail_url": "/gems/register/vendor/registerInfo",
            "agency": None,
            "status": "OPEN",
        }
    )
    assert gate["valid"] is False
    assert gate["strong_field_count"] < 2


def test_sourcewell_parses_open_excludes_awarded():
    opps = parse_sourcewell_open_solicitations(SW, list_url=SW_URL)
    assert len(opps) >= 5
    ids = {o.external_id for o in opps}
    assert len(ids) == len(opps)
    for o in opps:
        assert o.status == "OPEN"
        assert o.solicitation_number.isdigit()
        assert "award" not in o.title.lower() or "solicitation" in o.title.lower()
    # Awarded section titles must not appear
    titles = " ".join(o.title.lower() for o in opps)
    assert "recently awarded" not in titles


def test_sourcewell_fetcher_uses_html_parser():
    opps = CooperativeOpenSolicitationFetcher().parse_listing(SW, list_url=SW_URL)
    assert len(opps) >= 5


def test_arbitrary_deadline_and_garbage_sol_rejected():
    assert sanitize_deadline_raw("Start Browsing Now Select Region") is None
    assert sanitize_deadline_raw("11/10/2026, 2:00 PM CST") is not None
    assert is_garbage_solicitation_number("Open") is True
    assert is_garbage_solicitation_number("Free Registration") is True
    assert is_garbage_solicitation_number("005-RFP-3057-2027") is False


def test_structural_gate_accepts_real_row():
    gate = is_structurally_valid_opportunity(
        {
            "title": "Heavy Construction Equipment Purchase RFP",
            "solicitation_number": "11364",
            "external_id": "sourcewell:11364",
            "deadline_raw": "November 03, 2026",
            "detail_url": "https://www.sourcewell-mn.gov/solicitations/11364",
            "agency": "Sourcewell",
            "status": "OPEN",
        }
    )
    assert gate["valid"] is True
    assert gate["strong_field_count"] >= 2


def test_concessionaire_service_classification():
    cls = classify_discovery_opportunity(title="Concessionaire Operations of Honey Creek Resort")
    assert cls["classification"] == "SERVICE"


def test_broken_source_does_not_stop_others():
    def transport(url, headers=None, timeout=None):
        if "DASIowa" in url:
            raise ConnectionError("boom")
        if "StateOfMontana" in url:
            return _resp(url, MT)
        if "sourcewell" in url:
            return _resp(url, SW)
        return _resp(url, "<html><body>Bid Opportunities No solicitations at this time.</body></html>")

    out = run_live_discovery(
        profile="tiny",
        preview=True,
        transport=transport,
        source_ids=["state_ia", "state_mt", "coop_sourcewell_live"],
        fetch_details=False,
    )
    assert out["metrics"]["per_source"]["state_ia"]["ok"] is False
    assert out["metrics"]["per_source"]["state_mt"]["ok"] is True
    assert out["SAM"] == 0 and out["OpenAI"] == 0 and out["USAspending"] == 0 and out["paid"] == 0


def test_no_paid_apis_in_portal_repair():
    client = PublicProcurementHttpClient(
        budget=RequestBudget(min_interval_seconds=0),
        transport=lambda url, headers=None, timeout=None: _resp(url, IA),
    )
    JaggaerPublicLiveFetcher().fetch_listing(client, list_url=IA_URL, source_id="state_ia")
    acct = client.accounting()
    assert acct["SAM"] == 0 and acct["OpenAI"] == 0 and acct["USAspending"] == 0 and acct["paid"] == 0
