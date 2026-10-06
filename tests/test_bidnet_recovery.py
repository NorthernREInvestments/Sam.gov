"""Tests for BidNet detail/document/evidence recovery."""

from __future__ import annotations

from pathlib import Path

from bidnet_recovery.parse_abstract import parse_bidnet_abstract
from bidnet_recovery.recover import recover_one
from bidnet_recovery.states import (
    AUTH_REQUIRED,
    DETAIL_RECOVERED,
    ECONOMICS_READY,
    EXPIRED,
    NOT_PRODUCT,
    PRODUCT_IDENTIFIED,
)


FIXTURE = Path(__file__).resolve().parents[1] / "_bidnet_abstract_sample.html"


def _html() -> str:
    if FIXTURE.exists():
        return FIXTURE.read_text(encoding="utf-8")
    # Minimal frozen fixture
    return """
    <html><title>Widget Supply Contract - BidNet</title>
    <div id="ai-public-overview-content" class="mets-field-body">
      City of Test is soliciting bids for widget supplies and poly tubing valves.
    </div>
    <span class="mets-field-label">Location</span>
    <div class="mets-field-body ">California<br /></div>
    <span class="mets-field-label">Publication Date</span>
    <div class="mets-field-body ">09/28/2026 02:58 PM EDT</div>
    <span class="mets-field-label">Closing Date</span>
    <div class="mets-field-body ">10/20/2026 03:30 PM EDT</div>
    <div class="locked mets-field mets-field-view">
      <span class="mets-field-label">Issuing Organization</span>
      <div class="mets-field-body "><span class="member-only-info">Registered members only</span></div>
    </div>
    <div class="locked mets-field mets-field-view">
      <span class="mets-field-label">Solicitation Number</span>
      <div class="mets-field-body "><span class="member-only-info">Registered members only</span></div>
    </div>
    <a href="/public/user-registration" id="abstractRegisterNowButton">Get Access</a>
    </html>
    """


def test_parse_abstract_deadline_and_overview():
    p = parse_bidnet_abstract(_html())
    assert p["parse_ok"] is True
    assert p["close_date_raw"]
    assert p["deadline"]
    assert p["material_improvement"] is True
    assert p["auth_wall"] is True
    assert p["overview"] or p["description"]


def test_metadata_only_stays_discovered_until_enriched():
    rec = {
        "title": "Widget Supply",
        "platform": "live_bidnet",
        "authoritative_url": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/1/abstract",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {},
    }
    # No fetch — fixture path
    out = recover_one("t1", rec, fetch_live=False, html_fixture=_html())
    assert out["ok"] is True
    assert out["deadline_recovered"] is True
    assert out["state"] in {DETAIL_RECOVERED, PRODUCT_IDENTIFIED, "DETAIL_RECOVERED", "PRODUCT_IDENTIFIED"}
    assert out["state"] not in {"DISCOVERED"}
    assert rec["deadline"]
    assert rec["bidnet_recovery"]["deadline_confidence"] == "CONFIRMED"


def test_expired_from_recovered_deadline():
    html = _html().replace("10/20/2026", "01/01/2020").replace("09/28/2026", "12/01/2019")
    rec = {
        "title": "Old Bid",
        "platform": "live_bidnet",
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {},
    }
    out = recover_one("exp1", rec, fetch_live=False, html_fixture=html)
    assert out["state"] == EXPIRED
    assert rec["current_funnel_state"] == "EXPIRED"


def test_service_reclassified_not_product():
    html = """
    <html><title>IT Consulting Professional Services - BidNet</title>
    <div id="ai-public-overview-content" class="mets-field-body">
      Seeking staffing and consulting professional services for software development.
    </div>
    <span class="mets-field-label">Closing Date</span>
    <div class="mets-field-body ">12/31/2026 05:00 PM EDT</div>
    </html>
    """
    rec = {
        "title": "IT Consulting Professional Services",
        "platform": "live_bidnet",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "universe_class": "TANGIBLE_PRODUCT",
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {},
    }
    out = recover_one("svc1", rec, fetch_live=False, html_fixture=html)
    assert out["state"] == NOT_PRODUCT
    assert rec["universe_class"] == "PURE_SERVICE"
    assert out.get("reclassified") is True


def test_construction_corrected():
    html = """
    <html><title>Roadway Paving Construction - BidNet</title>
    <div id="ai-public-overview-content" class="mets-field-body">
      General contractor for asphalt paving renovation and site work labor-only.
    </div>
    <span class="mets-field-label">Closing Date</span>
    <div class="mets-field-body ">12/31/2026 05:00 PM EDT</div>
    </html>
    """
    rec = {
        "title": "Roadway Paving Construction",
        "platform": "live_bidnet",
        "universe_class": "TANGIBLE_PRODUCT",
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {},
    }
    out = recover_one("con1", rec, fetch_live=False, html_fixture=html)
    assert out["state"] == NOT_PRODUCT
    assert rec["universe_class"] == "CONSTRUCTION"


def test_auth_blocker_when_docs_locked():
    rec = {
        "title": "Annual Water Material Purchase Contract",
        "platform": "live_bidnet",
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {},
    }
    out = recover_one("a1", rec, fetch_live=False, html_fixture=_html())
    assert AUTH_REQUIRED in (out.get("blockers") or rec["bidnet_recovery"].get("blockers") or [])


def test_economics_ready_only_with_both_sides():
    html = _html()
    rec = {
        "title": "Poly Tubing Valves Supply",
        "platform": "live_bidnet",
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "current_funnel_state": "RAW",
        "freshness": "LIVE",
        "row_ref": {
            "economics": {
                "expected_revenue": 30000,
                "public_retail_total": 18000,
                "price_basis": "PUBLIC_RETAIL",
            }
        },
    }
    out = recover_one("econ1", rec, fetch_live=False, html_fixture=html)
    assert out["economics_ready"] is True
    assert out["state"] == ECONOMICS_READY
    assert rec.get("profit_first", {}).get("profit_status") in {
        "PROVEN_PROFITABLE",
        "LIKELY_PROFITABLE",
        "POSSIBLE_PROFIT",
        "UNPROVEN",
        "UNPROFITABLE",
    }


def test_no_detail_url_blocked():
    rec = {"title": "X", "platform": "live_bidnet", "row_ref": {}}
    out = recover_one("n1", rec, fetch_live=False)
    assert out["state"] == "RECOVERY_BLOCKED"
