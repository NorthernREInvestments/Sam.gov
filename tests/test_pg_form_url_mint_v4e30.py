"""v4e30: PG form URL minting preserves solicitation context."""

from __future__ import annotations

import importlib


def test_mint_fresh_form_url_from_gate_html():
    q = importlib.import_module("bidnet_auth.purchasing_group_queue")

    class _Client:
        def fetch_html(self, url, timeout_ms=20000, prefer_http=True):
            return (
                '<h3>California Purchasing Group</h3>'
                '<button id="interceptorProceedBtn" '
                'data-href="/private/supplier/9785456456/purchasing-groups/88020151/'
                'group-form/edit?accessedElementId=999888777&originalAction=SOLICITATION_OPEN">'
                "Proceed</button>"
            )

    out = q.mint_fresh_form_url_from_source(
        _Client(),
        purchasing_group_id="88020151",
        source_url=(
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            "999888777/abstract?purchasingGroupId=88020151&origin=1"
        ),
    )
    assert out.get("ok") is True
    assert "accessedElementId=999888777" in str(out.get("form_url") or "")
    assert out.get("solicitation_id") == "999888777"
