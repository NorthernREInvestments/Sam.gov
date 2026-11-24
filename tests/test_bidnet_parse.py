"""Restored BidNet parsers (offline HTML only) and the fail-closed acquisition gate."""
from __future__ import annotations

from pathlib import Path

from m3.research import bidnet_parse as b
from m3.research.staged_inventory import parse_deadline

ROW = """
<table><tr class="mets-table-row odd">
 <td><a href="/california/solicitations/Annual-Water-Material/444176375001?purchasingGroupId=123" class="solicitation-link mets-command-link">
   <span class="rowTitle">Annual Water Material Purchase Contract 2027</span></a>
   <span class="accessibility-hidden">444176375001</span>
   <span class="location">California</span></td>
 <td class="publicationDate"><span class="dateValue">09/28/2026 02:58 PM EDT</span></td>
 <td class="closingDate"><span class="dateValue">10/20/2026 03:30 PM EDT</span></td>
</tr></table>
"""


def test_list_row_parse():
    rows = b.parse_search_results_html(ROW, list_url="https://www.bidnetdirect.com/california/open-bids")
    assert len(rows) == 1
    r = rows[0]
    assert r["provider_record_id"] == "444176375001"
    assert r["title"] == "Annual Water Material Purchase Contract 2027"
    assert r["deadline_at"] == "2026-10-20T19:30:00+00:00"
    assert r["official_url"].startswith("https://www.bidnetdirect.com/california/solicitations/")


def test_open_bids_pagination_only_by_path():
    assert b.next_page_urls_from_html("", list_url="https://x/open-bids/page3", current_page=3) == ["https://x/open-bids/page4"]
    assert b.next_page_urls_from_html("", list_url="https://x/private/search?q=1", current_page=1) == []


def test_abstract_locked_fields():
    html = ('<h1>Road Salt 2027</h1><span>Closing Date</span><div class="mets-field-body">11/30/2026 02:00 PM CST</div>'
            '<span>Issuing Organization</span><div class="mets-field-body"><span class="member-only-info">Registered members only</span></div>')
    a = b.parse_bidnet_abstract(html)
    assert a["title"] == "Road Salt 2027"
    assert a["deadline_at"] == "2026-11-30T20:00:00+00:00"
    assert a["buyer"] is None and "Issuing Organization" in a["locked_fields"] and a["auth_wall"]


def test_us_formats_and_abbreviations():
    assert parse_deadline("11/30/2026 2:00 PM", tz_name=None, state="IL")["deadline_at"].isoformat() == "2026-11-30T20:00:00+00:00"
    assert parse_deadline("November 30, 2026 2:00 PM PST", tz_name=None, state="NY")["deadline_at"].isoformat() == "2026-11-30T22:00:00+00:00"


def test_acquisition_gate_fails_closed():
    g = b.bidnet_acquisition_allowed()
    assert g["allowed"] is False
    assert g["checks"]["live_client_present"] is False


def test_module_has_no_network_imports():
    src = Path(b.__file__).read_text(encoding="utf-8")
    for mod in ("httpx", "requests", "urllib.request", "playwright", "selenium", "socket"):
        assert f"import {mod}" not in src and f"from {mod}" not in src
