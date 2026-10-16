"""v4e30/v4e31: PG form URL minting uses private interception context."""

from __future__ import annotations

import importlib


def test_mint_fresh_form_url_from_gate_html():
    q = importlib.import_module("bidnet_auth.purchasing_group_queue")

    class _Client:
        def __init__(self):
            self.urls = []

        def fetch_html(self, url, timeout_ms=20000, prefer_http=True):
            self.urls.append(url)
            # Only the private interception target returns the gate dialog
            if "open-solicitation" in url:
                return (
                    '<h3>California Purchasing Group</h3>'
                    '<p>Incomplete Purchasing Group Form</p>'
                    '<a id="interceptorProceedBtn" '
                    'data-href="/private/supplier/9785456456/purchasing-groups/88020151/'
                    'group-form/edit?accessedElementId=9755638705&originalAction=SOLICITATION_OPEN"'
                    ' href="#">Complete Form</a>'
                )
            return "<html><body>statewide abstract without gate</body></html>"

    client = _Client()
    out = q.mint_fresh_form_url_from_source(
        client,
        purchasing_group_id="88020151",
        source_url=(
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            "444191583889/abstract?purchasingGroupId=88020151&origin=1"
        ),
    )
    assert out.get("ok") is True
    assert any("open-solicitation/444191583889" in u for u in client.urls)
    assert "accessedElementId=9755638705" in str(out.get("form_url") or "")
    assert out.get("solicitation_id") == "9755638705"
    stages = [t.get("stage") for t in (out.get("trace") or [])]
    assert "gate_proceed_url" in stages


def test_statewide_only_builds_path_only_form():
    q = importlib.import_module("bidnet_auth.purchasing_group_queue")

    class _Client:
        def fetch_html(self, url, timeout_ms=20000, prefer_http=True):
            return "<html><body>no gate here</body></html>"

    out = q.mint_fresh_form_url_from_source(
        _Client(),
        purchasing_group_id="88020151",
        source_url=(
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            "444191583889/abstract?purchasingGroupId=88020151"
        ),
    )
    assert out.get("ok") is True
    assert "accessedElementId=" not in str(out.get("form_url") or "")
    assert "88020151/group-form/edit" in str(out.get("form_url") or "")
