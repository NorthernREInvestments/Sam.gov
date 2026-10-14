"""Regression: BidNet search-page detection + interception search→detail mapping."""

from __future__ import annotations

from pathlib import Path

from bidnet_discovery.detail import SEARCH_PAGE_NOT_DETAIL, classify_detail_failure
from bidnet_engine.package_materialization import (
    extract_private_solicitation_refs_from_html,
    extract_search_result_interception_links,
    is_bidnet_detail_page_url,
    is_bidnet_search_page_url,
    is_bidnet_solicitation_detail_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bidnet"


def _search_html() -> str:
    return (FIXTURES / "search_results_interception_v1.html").read_text(encoding="utf-8")


def test_authenticated_search_page_detection():
    assert is_bidnet_search_page_url(
        "https://www.bidnetdirect.com/private/supplier/solicitations/search"
    )
    assert is_bidnet_search_page_url(
        "https://www.bidnetdirect.com/private/supplier/solicitations/search?target=init&keywords=Paper"
    )
    assert not is_bidnet_search_page_url(
        "https://www.bidnetdirect.com/private/solicitations/9755638705/abstract?target=view"
    )
    assert not is_bidnet_detail_page_url(
        "https://www.bidnetdirect.com/private/supplier/solicitations/search"
    )


def test_search_page_not_classified_as_detail_ok():
    status = classify_detail_failure(
        final_url="https://www.bidnetdirect.com/private/supplier/solicitations/search",
        html="<html><title>Solicitation Search</title></html>",
    )
    assert status == SEARCH_PAGE_NOT_DETAIL


def test_extract_interception_open_solicitation_links():
    rows = extract_search_result_interception_links(
        _search_html(), title_hint="Copy Paper and Envelopes"
    )
    assert rows, "expected interception title links from fixture"
    kinds = {r["kind"] for r in rows}
    assert "open_solicitation" in kinds
    top = rows[0]
    assert top["kind"] == "open_solicitation"
    assert top["solicitation_id"] == "9755638705"
    assert "/interception/open-solicitation/9755638705" in top["href"]
    assert "target=view" in top["href"]


def test_solicitations_title_link_mapping_prefers_open_solicitation():
    rows = extract_search_result_interception_links(_search_html(), title_hint="Copy Paper")
    open_rows = [r for r in rows if r["kind"] == "open_solicitation"]
    assert open_rows
    assert open_rows[0]["solicitation_id"] == "9755638705"


def test_numeric_hint_scores_solicitation_id_match():
    """Open-bids path IDs rarely appear in titles — match solicitation_id/href digits."""
    html = _search_html()
    rows = extract_search_result_interception_links(html, title_hint="9755638705")
    assert rows
    assert rows[0]["solicitation_id"] == "9755638705"
    rows2 = extract_search_result_interception_links(html, title_hint="00009755638705")
    assert rows2
    assert rows2[0]["solicitation_id"] == "9755638705"


def test_search_redirect_must_not_count_as_detail_url():
    assert not is_bidnet_solicitation_detail_url(
        "https://www.bidnetdirect.com/private/supplier/solicitations/search"
    )
    assert is_bidnet_solicitation_detail_url(
        "https://www.bidnetdirect.com/private/solicitations/9755638705/abstract?target=view"
    )
    assert is_bidnet_solicitation_detail_url(
        "https://www.bidnetdirect.com/private/supplier/interception/open-solicitation/9755638705?target=view"
    )


def test_extract_private_refs_includes_interception_gateways():
    refs = extract_private_solicitation_refs_from_html(_search_html())
    assert any("interception/open-solicitation/9755638705" in r for r in refs)


def test_statewide_and_open_bids_ids_still_not_synthesized_as_private_view():
    from bidnet_engine.package_materialization import resolve_bidnet_private_detail_url_candidates

    assert (
        resolve_bidnet_private_detail_url_candidates(
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/444166577324/abstract"
        )
        == []
    )
    assert (
        resolve_bidnet_private_detail_url_candidates(
            "https://www.bidnetdirect.com/new-york/solicitations/open-bids/Copy-Paper/0000438542"
        )
        == []
    )


def test_incomplete_product_lines_title_fallback_not_complete():
    """Title-only lines are incomplete — mirrors bounded recovery acceptance gate."""
    lines = [
        {
            "DESCRIPTION": {"VALUE": "Copy Paper and Envelopes"},
            "QTY": {"VALUE": None},
            "LINE_SOURCE_TYPE": "TITLE_FALLBACK",
        }
    ]
    qty_ok = any(
        (isinstance(li.get("QTY"), dict) and li["QTY"].get("VALUE") not in (None, ""))
        for li in lines
    )
    title_only = len(lines) == 1 and lines[0].get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"
    assert not (qty_ok and not title_only)


def test_item_timeout_constant_still_45s():
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import ITEM_TIMEOUT_S

    assert ITEM_TIMEOUT_S == 45.0


def test_resolve_detail_uses_fixture_html_without_live_auth():
    """Parse-only path: search_html supplied → interception candidate selected."""
    from bidnet_auth.client import AuthResult, BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    client.last_result = AuthResult(status="SESSION_REUSED", authenticated=True, message="fixture")

    class _FakePage:
        url = "https://www.bidnetdirect.com/private/supplier/solicitations/search"

        def goto(self, href, **kwargs):
            # Mirror verified live redirect: interception → /private/solicitations/{id}/abstract
            if "open-solicitation/9755638705" in href:
                self.url = (
                    "https://www.bidnetdirect.com/private/solicitations/9755638705/abstract?target=view"
                )
            elif href.startswith("http"):
                self.url = href
            else:
                self.url = "https://www.bidnetdirect.com" + href

        def wait_for_load_state(self, *a, **k):
            return None

        def wait_for_timeout(self, *a, **k):
            return None

        def content(self):
            return (FIXTURES / "private_abstract_action_required_v1.html").read_text(encoding="utf-8")

    client._page = _FakePage()
    out = client.resolve_detail_via_authenticated_search(
        title="Copy Paper and Envelopes",
        search_html=_search_html(),
        timeout_ms=5_000,
    )
    assert out.get("ok") is True
    assert out.get("solicitation_id") == "9755638705"
    assert "9755638705" in str(out.get("detail_url") or "")
    assert out.get("access_gate") == "ACTION_REQUIRED"
    assert not is_bidnet_search_page_url(str(out.get("detail_url") or ""))
