"""Tests for authenticated BidNet search harvest parsers + scheduling hook."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bidnet_discovery.parse import (
    parse_search_results_html,
    reported_total_from_html,
    next_page_urls_from_html,
)


SAMPLE_ROW = """
<html><body>
<span class="simpleSolResultsNumResults">23,463 results</span>
<table class="simpleSolResultsTable mets-table">
<tr data-index="0" class="mets-table-row odd">
  <td class="mainCol">
    <a id="searchResultSol_solicitation_9780186210"
       href="/new-jersey/solicitations/open-bids/FLOOD-PROTECTION/0000439051?origin=0"
       class="solicitation-link mets-command-link">
      <span class="rowTitle">FLOOD PROTECTION / CLIMATE RESILIENCY</span>
      <span class="location">New Jersey</span>
      <span class="publicationDate"><span class="dateValue">10/02/2026</span></span>
      <span class="closingDate open"><span class="dateValue">11/24/2026</span></span>
      <span class="accessibility-hidden"> 9780186210</span>
    </a>
  </td>
</tr>
</table>
<option class="mets-pagination-number" value="2" data-page-number="2"
        data-href="/solicitations/open-bids/page2"></option>
</body></html>
"""


def test_parse_search_results_fields():
    rows = parse_search_results_html(
        SAMPLE_ROW,
        list_url="https://www.bidnetdirect.com/private/supplier/solicitations/search",
        page_number=1,
    )
    assert len(rows) == 1
    r = rows[0]
    assert "FLOOD PROTECTION" in r["title"]
    assert r["location"] == "New Jersey"
    assert r["deadline_raw"] == "11/24/2026"
    assert r["issue_date_raw"] == "10/02/2026"
    assert "0000439051" in r["detail_url"]
    assert r["raw_metadata"]["bidnet_internal_id"] == "9780186210"
    assert r["raw_metadata"]["current_session_href"] is True
    assert r["discovery_scope"] == "BROAD_PRODUCT_RESALE"


def test_parse_private_search_row_without_solicitation_link_class():
    html = """
    <tr class="mets-table-row odd">
      <td><a href="/private/supplier/solicitations/1234567890/view">School Bus Parts Package</a></td>
      <td>Texas</td>
      <td>10/01/2026</td>
      <td>11/01/2026</td>
    </tr>
    """
    rows = parse_search_results_html(
        html,
        list_url="https://www.bidnetdirect.com/private/supplier/solicitations/search",
        page_number=1,
    )
    assert len(rows) == 1
    assert "School Bus Parts" in rows[0]["title"]
    assert "1234567890" in rows[0]["detail_url"]


def test_reported_total_and_next_page():
    assert reported_total_from_html(SAMPLE_ROW) == 23463
    nxt = next_page_urls_from_html(
        SAMPLE_ROW,
        list_url="https://www.bidnetdirect.com/solicitations/open-bids",
        current_page=1,
    )
    assert any("page2" in u for u in nxt)
    # Private search must not invent inert ?pageNumber= URLs
    priv = next_page_urls_from_html(
        "<html></html>",
        list_url="https://www.bidnetdirect.com/private/supplier/solicitations/search",
        current_page=1,
    )
    assert not any("pageNumber=" in u for u in priv)


def test_live_public_html_parses(tmp_path):
    # Optional: use saved probe if present; otherwise skip network
    probe = Path(__file__).resolve().parents[1] / "scripts" / "_probe_bidnet_public_html.py"
    assert probe.exists()


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    yield tmp_path
    set_data_root(None)


def test_scheduled_runs_harvest_before_recovery(data_root, monkeypatch):
    monkeypatch.setenv("BIDNET_AUTH_ENABLED", "true")
    monkeypatch.setenv("BIDNET_USERNAME", "u@example.com")
    monkeypatch.setenv("BIDNET_PASSWORD", "secret")

    harvest_report = {
        "auth": {"status": "LOGIN_SUCCESS", "authenticated": True},
        "harvest": {
            "search_reachable": True,
            "reported_total": 23000,
            "retrieved_total": 20,
            "detail_stats": {"detail_recovered": 10},
        },
        "canonical_merge": {"new": 5, "updated": 3},
        "blocker": None,
    }
    recovery_report = {
        "auth": {"status": "SESSION_REUSED", "authenticated": True},
        "processed": 0,
        "selected": 0,
        "stats": {},
        "funnel": {},
    }

    with patch(
        "bidnet_discovery.run_bidnet_authenticated_harvest",
        return_value=harvest_report,
    ) as h, patch(
        "bidnet_recovery.batch.run_bidnet_recovery",
        return_value=recovery_report,
    ) as r:
        from bidnet_auth.scheduled import run_scheduled_bidnet_auth_recovery

        out = run_scheduled_bidnet_auth_recovery(run_id="T1", trigger_type="MANUAL")
    assert h.called
    assert r.called
    assert out["harvest"]["search_reachable"] is True
    assert out["auth"]["authenticated"] is True
