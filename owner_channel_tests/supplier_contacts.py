"""Phases 3–4 — Public supplier contact validation (no guessed data)."""

from __future__ import annotations

from typing import Any
from urllib.request import Request, urlopen

from owner_channel_tests.models import (
    ACCOUNT_REQUIRED,
    EMAIL_READY,
    PHONE_READY,
    SALES_REP_REQUIRED,
    UNKNOWN,
    WEB_FORM_READY,
)

# Only publicly documented routes — soft-validated; no inventing emails/phones.
_SUPPLIERS: dict[str, dict[str, Any]] = {
    "cummins.com": {
        "supplier_key": "CUMMINS_DIRECT",
        "supplier_name": "CUMMINS Direct",
        "official_website": "https://www.cummins.com/",
        "sales_quote_contact_page": "https://www.cummins.com/support/find-location",
        "quote_request_form": "https://shop.cummins.com/",
        "sales_email_public": None,  # not inventing
        "sales_phone_public": None,
        "government_public_sector_contact": "https://www.cummins.com/support/find-location",
        "account_requirement": "May require shop.cummins.com / dealer account for pricing",
        "dealer_distributor_restrictions": "Genuine parts often via Cummins dealers / Parts.Cummins",
        "minimum_order_notes": "Confirm with dealer/shop at quote time",
        "quote_submission_method": "Dealer locator / shop RFQ / phone at listed location",
        "preferred_route_class": WEB_FORM_READY,
        "alternate_route_class": SALES_REP_REQUIRED,
        "priority_rank": 2,  # government/public via locator
    },
    "boundtree.com": {
        "supplier_key": "BOUND_TREE_MEDICAL",
        "supplier_name": "Bound Tree Medical",
        "official_website": "https://www.boundtree.com/",
        "sales_quote_contact_page": "https://www.boundtree.com/customer-service",
        "quote_request_form": "https://www.boundtree.com/customer-service",
        "sales_email_public": None,
        "sales_phone_public": "1-800-533-0523",  # published on Bound Tree customer service materials
        "government_public_sector_contact": "https://www.boundtree.com/customer-service",
        "account_requirement": "B2B account typically required for contract pricing",
        "dealer_distributor_restrictions": "EMS distributor — account / tax-exempt may apply",
        "minimum_order_notes": "Confirm at quote time",
        "quote_submission_method": "Customer service / account portal RFQ",
        "preferred_route_class": ACCOUNT_REQUIRED,
        "alternate_route_class": PHONE_READY,
        "priority_rank": 5,
    },
    "henryschein.com": {
        "supplier_key": "HENRY_SCHEIN_MEDICAL",
        "supplier_name": "Henry Schein Medical",
        "official_website": "https://www.henryschein.com/",
        "sales_quote_contact_page": "https://www.henryschein.com/us-en/medical/contact-us.aspx",
        "quote_request_form": "https://www.henryschein.com/us-en/medical/contact-us.aspx",
        "sales_email_public": None,
        "sales_phone_public": None,
        "government_public_sector_contact": "https://www.henryschein.com/us-en/medical/contact-us.aspx",
        "account_requirement": "Account login typically required",
        "dealer_distributor_restrictions": "Medical supply — licensing / account verification may apply",
        "minimum_order_notes": "Confirm at quote time",
        "quote_submission_method": "Contact-us / account quote request",
        "preferred_route_class": ACCOUNT_REQUIRED,
        "alternate_route_class": WEB_FORM_READY,
        "priority_rank": 5,
    },
    "medline.com": {
        "supplier_key": "MEDLINE",
        "supplier_name": "Medline",
        "official_website": "https://www.medline.com/",
        "sales_quote_contact_page": "https://www.medline.com/help/contact-us/",
        "quote_request_form": "https://www.medline.com/help/contact-us/",
        "sales_email_public": None,
        "sales_phone_public": None,
        "government_public_sector_contact": "https://www.medline.com/help/contact-us/",
        "account_requirement": "Account often required for institutional pricing",
        "dealer_distributor_restrictions": "Institutional/medical sales channel",
        "minimum_order_notes": "Confirm at quote time",
        "quote_submission_method": "Contact-us / sales rep",
        "preferred_route_class": ACCOUNT_REQUIRED,
        "alternate_route_class": SALES_REP_REQUIRED,
        "priority_rank": 5,
    },
}


def _alive(url: str | None, timeout: float = 8.0) -> bool | None:
    if not url:
        return None
    try:
        req = Request(url, method="GET", headers={"User-Agent": "GovTrackerOwnerChannelCheck/1.0"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            code = getattr(resp, "status", 200)
            return 200 <= int(code) < 400
    except Exception:
        return False


def validate_supplier_contacts() -> dict[str, Any]:
    rows = []
    for domain, base in _SUPPLIERS.items():
        site_ok = _alive(base["official_website"])
        contact_ok = _alive(base["sales_quote_contact_page"])
        form_ok = _alive(base.get("quote_request_form"))
        route = base["preferred_route_class"]
        if route == ACCOUNT_REQUIRED and base.get("sales_phone_public"):
            # phone as secondary validated path
            pass
        if base.get("sales_email_public"):
            route = EMAIL_READY
        elif form_ok and route not in {ACCOUNT_REQUIRED}:
            route = WEB_FORM_READY
        rows.append(
            {
                **base,
                "domain": domain,
                "website_reachable": site_ok,
                "contact_page_reachable": contact_ok,
                "form_reachable": form_ok,
                "contact_route_class": route,
                "public_contact_validated": bool(contact_ok or site_ok),
                "guessed_contact_data": False,
                "notes": "Emails not invented; use published pages/phones only",
            }
        )
    return {"suppliers": rows, "count": len(rows)}


def contact_for_domain(domain: str) -> dict[str, Any]:
    return dict(_SUPPLIERS.get(domain) or {"supplier_name": domain, "contact_route_class": UNKNOWN})
