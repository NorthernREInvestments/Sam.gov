"""Site-wide portal forms linked beside a bid are not that bid's solicitation documents."""
from __future__ import annotations

from m3.research.documents import find_attachment_urls, unrelated_site_document

GILROY = "Call for Artists For the Love of Gilroy Public Art Banners 2026-27"


def test_site_forms_rejected_when_they_do_not_name_the_solicitation():
    for url, head in (
        ("https://cityofgilroy.org/DocumentCenter/View/7702/Building-Permit-Application-Form?bidId=",
         "BUILDING PERMIT APPLICATION 7351 Rosanna Street Gilroy"),
        ("https://cityofgilroy.org/DocumentCenter/View/8066/Reroof-Requirements-Handout-?bidId=",
         "Development Service Center Re-Roof Requirements"),
        ("https://www.cityofgilroy.org/DocumentCenter/View/5498/Police-Records---Application-for-Release-of-Information-Police-Report-Request?bidId=",
         "Gilroy Police Department REQUEST TO OBTAIN RECORDS"),
    ):
        assert unrelated_site_document(url, head, title=GILROY, solicitation_number="27-CFA-AD-103"), url


def test_solicitation_documents_are_kept():
    keep = (
        ("https://cityofgilroy.org/DocumentCenter/View/9001/27-CFA-AD-103-Call-for-Artists", ""),
        ("https://x.gov/files/RFP-2026-09-Specifications.pdf", ""),
        ("https://x.gov/files/Attachment-A.pdf", ""),
        ("https://x.gov/files/Permit-Application-Software-RFP.pdf", ""),
        ("https://x.gov/files/application.pdf", "27-CFA-AD-103 artist application for the Public Art Banners"),
    )
    for url, head in keep:
        assert unrelated_site_document(url, head, title=GILROY, solicitation_number="27-CFA-AD-103") is None, url


def test_find_attachment_urls_skips_site_forms():
    html = ('<a href="/DocumentCenter/View/7702/Building-Permit-Application-Form?bidId=">Permit</a>'
            '<a href="/files/IFB-26-14-Bid-Documents.pdf">Bid</a>')
    assert find_attachment_urls(html, "https://city.gov/bids.aspx?bidID=1") == ["https://city.gov/files/IFB-26-14-Bid-Documents.pdf"]
