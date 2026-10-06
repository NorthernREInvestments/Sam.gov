"""Phase 4 — Dania supplier contact validation (no send)."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

from final_pre_scale_proof.models import DANIA_OID, PRIOR_DC_PACKETS, SUPPLIER_CONTACT_READY
from m3_data_root import data_path

# Known public routes for Dania packet suppliers (static validation map + live HEAD/GET soft check)
_SUPPLIER_ROUTES = {
    "dumorsitefurnishings.com": {
        "supplier_name": "DuMor Site Furnishings",
        "product_family": "site_furnishings_park_benches_tables",
        "contact_route": "https://www.dumorsitefurnishings.com/contact/",
        "quote_request_route": "https://www.dumorsitefurnishings.com/contact/",
        "account_required": False,
        "manufacturer_relationship": "OEM_DIRECT",
    },
    "dumor.com": {
        "supplier_name": "DuMor, Inc.",
        "product_family": "site_furnishings",
        "contact_route": "https://www.dumor.com/contact/",
        "quote_request_route": "https://www.dumor.com/contact/",
        "account_required": False,
        "manufacturer_relationship": "OEM_DIRECT",
    },
    "elkay.com": {
        "supplier_name": "Elkay",
        "product_family": "bottle_filling_drinking_fountains",
        "contact_route": "https://www.elkay.com/us/en/support/contact-us.html",
        "quote_request_route": "https://www.elkay.com/us/en/support/contact-us.html",
        "account_required": False,
        "manufacturer_relationship": "OEM_DIRECT",
    },
    "unknown-supplier": {
        "supplier_name": "unknown-supplier",
        "product_family": "UNRESOLVED",
        "contact_route": None,
        "quote_request_route": None,
        "account_required": True,
        "manufacturer_relationship": "UNKNOWN",
    },
}


def _alive(url: str | None, timeout: float = 8.0) -> bool | None:
    if not url:
        return None
    try:
        req = Request(url, method="GET", headers={"User-Agent": "GovTrackerContactCheck/1.0"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — public contact URL check
            return 200 <= getattr(resp, "status", 200) < 400
    except Exception:
        # Soft: DNS/SSL/blocks — mark unchecked not false permanently
        return False


def validate_dania_suppliers() -> dict[str, Any]:
    packets = json.loads(data_path(PRIOR_DC_PACKETS).read_text(encoding="utf-8")).get("packets") or []
    dania = [p for p in packets if p.get("opportunity_id") == DANIA_OID]
    rows = []
    for p in dania:
        dom = (p.get("supplier") or {}).get("domain") or "unknown-supplier"
        base = dict(_SUPPLIER_ROUTES.get(dom) or _SUPPLIER_ROUTES["unknown-supplier"])
        # Prefer packet-declared contact
        contact = (p.get("supplier") or {}).get("contact_route") or base.get("contact_route")
        quote_route = base.get("quote_request_route") or contact
        alive = _alive(contact)
        appropriate = base.get("product_family") not in {None, "UNRESOLVED"}
        ready = bool(contact) and appropriate and dom != "unknown-supplier"
        rows.append(
            {
                "packet_id": p.get("packet_id"),
                "supplier_domain": dom,
                "supplier_name": (p.get("supplier") or {}).get("supplier_name") or base.get("supplier_name"),
                "still_active": alive if alive is not None else "UNCHECKED",
                "appropriate_product_family": appropriate,
                "product_family": base.get("product_family"),
                "public_business_contact_route": contact,
                "quote_request_route": quote_route,
                "account_requirement": base.get("account_required"),
                "manufacturer_relationship": (p.get("supplier") or {}).get("manufacturer_relationship")
                or base.get("manufacturer_relationship"),
                "status": SUPPLIER_CONTACT_READY if ready else "SUPPLIER_CONTACT_NOT_READY",
                "do_not_send": True,
                "line_count": p.get("line_count"),
            }
        )
    ready_n = sum(1 for r in rows if r["status"] == SUPPLIER_CONTACT_READY)
    return {
        "opportunity_id": DANIA_OID,
        "channels": rows,
        "ready_count": ready_n,
        "not_ready_count": len(rows) - ready_n,
        "SUPPLIER_CONTACT_READY": ready_n == len(rows) and len(rows) > 0,
    }
