"""Regression: BidNet Documents-tab attachment discovery + XLS schedule extract."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bidnet"
DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "bidnet_auth" / "documents" / "9797265001"


def test_docs_tab_fixture_has_four_attachment_download_links():
    html = (FIXTURES / "private_abstract_docs_tab_v1.html").read_text(encoding="utf-8")
    assert html.count("attachment-download") == 4
    assert "solicitationDocumentsTable" in html
    assert "APPENDIX C - BID RESPONSE FORM.xls" in html
    assert "innerTabId=docs-items" in html


def test_discover_collects_attachment_download_from_fixture_page():
    """Offline: page already on docs tab HTML → harvest four links (no live download)."""
    from bidnet_auth.client import AuthResult, BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config

    html = (FIXTURES / "private_abstract_docs_tab_v1.html").read_text(encoding="utf-8")
    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    client.last_result = AuthResult(status="SESSION_REUSED", authenticated=True, message="fixture")

    class _FakeLocator:
        def __init__(self, elements):
            self._elements = elements

        def count(self):
            return len(self._elements)

        def all(self):
            return self._elements

        @property
        def first(self):
            return self._elements[0] if self._elements else self

        def click(self, **kwargs):
            return None

    class _FakeEl:
        def __init__(self, href, text):
            self._href = href
            self._text = text

        def get_attribute(self, name):
            if name == "href":
                return self._href
            if name == "title":
                return ""
            return None

        def inner_text(self):
            return self._text

    class _FakePage:
        url = "https://www.bidnetdirect.com/private/solicitations/9797265001/abstract?target=view"

        def __init__(self):
            self._html = html
            self._listeners = []

        def on(self, *a, **k):
            return None

        def remove_listener(self, *a, **k):
            return None

        def title(self):
            return "28010 - Copy Paper"

        def content(self):
            return self._html

        def wait_for_timeout(self, *a, **k):
            return None

        def wait_for_selector(self, *a, **k):
            return None

        def fetch_html_compat(self):
            return self._html

        def locator(self, sel):
            import re

            els = []
            if "attachment-download" in sel or "attachmentDownloadLnk" in sel:
                for m in re.finditer(
                    r'href="([^"]*attachment-download[^"]*)"[^>]*>([^<]+)',
                    self._html,
                    re.I,
                ):
                    els.append(_FakeEl(m.group(1), m.group(2).strip()))
                if not els:
                    for m in re.finditer(
                        r'<a[^>]+id="attachmentDownloadLnk_\d+"[^>]*href="([^"]+)"[^>]*>([^<]+)',
                        self._html,
                        re.I,
                    ):
                        els.append(_FakeEl(m.group(1), m.group(2).strip()))
            elif "innerTabId=docs-items" in sel:
                for m in re.finditer(
                    r'href="([^"]*innerTabId=docs-items[^"]*)"[^>]*>([\s\S]*?)</a>',
                    self._html,
                    re.I,
                ):
                    els.append(_FakeEl(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()[:80]))
            elif sel == "a[href]":
                for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>([^<]*)</a>', self._html, re.I):
                    els.append(_FakeEl(m.group(1), m.group(2).strip()))
            return _FakeLocator(els)

        def get_by_role(self, *a, **k):
            return _FakeLocator([])

        def expect_download(self, **kwargs):
            raise TimeoutError("no download in fixture")

    page = _FakePage()
    client._page = page

    # Bypass navigation; exercise harvest path by stubbing fetch_html
    client.fetch_html = lambda url, timeout_ms=20_000, prefer_http=False: html  # type: ignore

    out = client.discover_attachment_links(
        "https://www.bidnetdirect.com/private/solicitations/9797265001/abstract?target=view",
        timeout_ms=5_000,
    )
    names = {d.get("document_name") for d in out}
    assert len(out) >= 4
    assert any("APPENDIX A" in (n or "") for n in names)
    assert any("APPENDIX C" in (n or "") and ".xls" in (n or "") for n in names)
    assert any(d.get("attachment_id") for d in out)


def test_map_headers_prefers_item_description_over_subcategory():
    from product_identity.spreadsheet import map_headers

    mapped = map_headers(
        [
            "Bid ID",
            "Item ID",
            "Subcategory",
            "Subcategory Description",
            "Item Description",
            "Unit Measure",
            "Quantity",
            "Unit Cost",
            "Vendor Catalog Number",
        ]
    )
    assert mapped["description"] == 4
    assert mapped["item"] == 1
    assert mapped["quantity"] == 6
    assert mapped["uom"] == 5


def test_xls_bid_response_extracts_qty_lines():
    xls = DOCS_DIR / "APPENDIX_C_-_BID_RESPONSE_FORM.xls"
    if not xls.exists():
        # Allow CI without live download artifact — skip rather than fail
        import pytest

        pytest.skip("live downloaded XLS not present")
    from bidnet_engine.schedule_extraction import extract_schedules_from_package

    sched = extract_schedules_from_package(
        [
            {
                "local_path": str(xls),
                "document_name": "APPENDIX C - BID RESPONSE FORM.xls",
                "filename": "APPENDIX C - BID RESPONSE FORM.xls",
            }
        ],
        opportunity_id="b5d99d0c52d52b22",
        body_text="28010 - Copy Paper",
    )
    rows = sched.get("schedule_rows") or []
    assert len(rows) >= 20

    def qty(r):
        q = r.get("QTY") or r.get("quantity") or r.get("Quantity")
        if isinstance(q, dict):
            return q.get("VALUE")
        return q

    with_qty = [r for r in rows if qty(r) not in (None, "", 0, "0")]
    assert len(with_qty) >= 10
