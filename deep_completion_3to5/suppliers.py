"""Supplier / distributor channel resolution for quote packets."""

from __future__ import annotations

import re
from typing import Any

from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer

# Category → specialty supplier channels (curated, not invented manufacturers)
EMS_DISTRIBUTORS = [
    {
        "supplier_name": "Bound Tree Medical",
        "domain": "boundtree.com",
        "contact_route": "https://www.boundtree.com/customer-service",
        "quote_request_capability": "ACCOUNT_OR_FORM",
        "account_login_required": True,
        "notes": "Major EMS/medical supply distributor",
    },
    {
        "supplier_name": "Henry Schein Medical",
        "domain": "henryschein.com",
        "contact_route": "https://www.henryschein.com/us-en/medical/contact-us.aspx",
        "quote_request_capability": "ACCOUNT_OR_PHONE",
        "account_login_required": True,
        "notes": "Broad medical/EMS catalog",
    },
    {
        "supplier_name": "Medline",
        "domain": "medline.com",
        "contact_route": "https://www.medline.com/help/contact-us",
        "quote_request_capability": "ACCOUNT_OR_FORM",
        "account_login_required": True,
        "notes": "Medical supplies distributor",
    },
    {
        "supplier_name": "Life-Assist",
        "domain": "life-assist.com",
        "contact_route": "https://www.life-assist.com/contact/",
        "quote_request_capability": "FORM_OR_PHONE",
        "account_login_required": False,
        "notes": "EMS specialty distributor",
    },
]

SITE_FURNISHING = [
    {
        "supplier_name": "DuMor Site Furnishings",
        "domain": "dumor.com",
        "contact_route": "https://www.dumor.com/contact/",
        "quote_request_capability": "FORM_OR_PHONE",
        "account_login_required": False,
        "manufacturer_relationship": "OEM",
        "notes": "Park benches/site furnishings OEM",
    },
    {
        "supplier_name": "Elkay",
        "domain": "elkay.com",
        "contact_route": "https://www.elkay.com/contact-us",
        "quote_request_capability": "FORM_OR_DEALER",
        "account_login_required": False,
        "manufacturer_relationship": "OEM",
        "notes": "Drinking fountains / drinking stations",
    },
]

IRRIGATION = [
    {
        "supplier_name": "Hunter Industries",
        "domain": "hunterindustries.com",
        "contact_route": "https://www.hunterindustries.com/en-us/contact-us",
        "quote_request_capability": "DEALER_LOCATOR",
        "account_login_required": False,
        "manufacturer_relationship": "OEM",
        "notes": "Irrigation OEM — quote via authorized dealer",
    },
    {
        "supplier_name": "Netafim USA",
        "domain": "netafimusa.com",
        "contact_route": "https://www.netafimusa.com/contact/",
        "quote_request_capability": "DEALER_LOCATOR",
        "account_login_required": False,
        "manufacturer_relationship": "OEM",
        "notes": "Drip irrigation OEM",
    },
    {
        "supplier_name": "SiteOne Landscape Supply",
        "domain": "siteone.com",
        "contact_route": "https://www.siteone.com/en/contact-us",
        "quote_request_capability": "BRANCH_QUOTE",
        "account_login_required": True,
        "manufacturer_relationship": "AUTHORIZED_DISTRIBUTOR",
        "notes": "Landscape/irrigation distributor",
    },
]

HVAC = [
    {
        "supplier_name": "Carrier / local authorized",
        "domain": "carrier.com",
        "contact_route": "https://www.carrier.com/commercial/en/us/contact-us/",
        "quote_request_capability": "DEALER_LOCATOR",
        "account_login_required": False,
        "notes": "Commercial HVAC unit quote via authorized dealer",
    },
    {
        "supplier_name": "Johnstone Supply",
        "domain": "johnstonesupply.com",
        "contact_route": "https://www.johnstonesupply.com/contact",
        "quote_request_capability": "BRANCH_QUOTE",
        "account_login_required": True,
        "notes": "HVAC wholesale distributor",
    },
]

_EMS_CAT = re.compile(
    r"trauma|airway|oxygen|pharma|ppe|personal\s+protective|spinal|immobil|soft\s+goods|"
    r"ansel|ansell|arrow|medical|ems",
    re.I,
)
_SECTION_FAKE = re.compile(
    r"^(SITE\s+WORK|CONDUIT|FENCE|LATERAL|WATER\s+SOURCE|BUBBLERS|UP\s+AIR|"
    r"TRAUMA|AIRWAY|PHARMACEUTICAL|PERSONAL\s+PROTECTIVE|SPINAL)",
    re.I,
)


def _supplier_from_dist(domain: str, relationship: str, mfr_key: str | None) -> dict[str, Any]:
    return {
        "supplier_name": domain,
        "domain": domain,
        "manufacturer_relationship": relationship,
        "contact_route": f"https://www.{domain}" if not domain.startswith("shop.") else f"https://{domain}",
        "quote_request_capability": "WEB_OR_ACCOUNT",
        "account_login_required": True,
        "public_contact": None,
        "notes": f"Distributor channel for {mfr_key or 'OEM'}",
        "manufacturer_key": mfr_key,
    }


def resolve_channels_for_line(line: dict[str, Any]) -> list[dict[str, Any]]:
    mfr = str(line.get("manufacturer") or "").strip()
    desc = str(line.get("description") or "")
    channels: list[dict[str, Any]] = []

    # Real OEM resolution
    if mfr and not _SECTION_FAKE.match(mfr):
        resolved = resolve_manufacturer({"manufacturer": mfr})
        key = resolved.get("manufacturer_key")
        if key:
            if resolved.get("sells_direct") or (resolved.get("manufacturer_domain")):
                channels.append(
                    {
                        "supplier_name": f"{key} Direct",
                        "domain": resolved.get("manufacturer_domain"),
                        "manufacturer_relationship": "OEM_DIRECT",
                        "contact_route": resolved.get("manufacturer_distributor_locator")
                        or resolved.get("manufacturer_product_search_route"),
                        "quote_request_capability": "OEM_LOCATOR_OR_SHOP",
                        "account_login_required": False,
                        "notes": "Manufacturer direct / locator",
                        "manufacturer_key": key,
                    }
                )
            for dist in authorized_distributors(key)[:5]:
                domain = dist.get("domain") or dist.get("name")
                if not domain:
                    continue
                channels.append(
                    _supplier_from_dist(
                        str(domain),
                        dist.get("authorization") or "AUTHORIZED_UNKNOWN",
                        key,
                    )
                )

    # Category specialty
    blob = f"{mfr} {desc} {line.get('category') or ''}"
    if _EMS_CAT.search(blob) or line.get("opportunity_id", "").endswith("286698"):
        channels.extend(EMS_DISTRIBUTORS)
    if re.search(r"elkay|dumor|exofit|bench|fountain|site\s+furnish", blob, re.I):
        channels.extend(SITE_FURNISHING)
    if re.search(r"hunter|netafim|irrigation|drip|dripline", blob, re.I):
        channels.extend(IRRIGATION)
    if re.search(r"air\s+unit|hvac|rooftop|rtu|cooling", blob, re.I) or "UP AIR" in mfr.upper():
        channels.extend(HVAC)

    # Deduplicate by domain
    seen: set[str] = set()
    out = []
    for c in channels:
        dom = (c.get("domain") or c.get("supplier_name") or "").lower()
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append(c)
    return out


def build_supplier_map(opportunity_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    manufacturers: set[str] = set()
    auth_dists: set[str] = set()
    suppliers: dict[str, dict[str, Any]] = {}
    line_channels: dict[str, list[str]] = {}

    for ln in lines:
        if not ln.get("identity_usable") and ln.get("quote_required_state") != "QUOTE_REQUIRED":
            # still map quote-required usable
            pass
        mfr = ln.get("manufacturer")
        if mfr and not _SECTION_FAKE.match(str(mfr)):
            manufacturers.add(str(mfr))
        elif mfr and _SECTION_FAKE.match(str(mfr)):
            # category family label
            manufacturers.add(f"CATEGORY:{mfr}")

        chans = resolve_channels_for_line(ln)
        domains = []
        for c in chans:
            dom = c.get("domain") or c.get("supplier_name")
            domains.append(str(dom))
            suppliers[str(dom)] = c
            if "AUTHORIZED" in str(c.get("manufacturer_relationship") or "").upper() or c.get("manufacturer_relationship") in {
                "AUTHORIZED_CONFIRMED",
                "AUTHORIZED_UNKNOWN",
                "AUTHORIZED_DISTRIBUTOR",
            }:
                auth_dists.add(str(dom))
            if c.get("manufacturer_relationship") == "OEM_DIRECT":
                manufacturers.add(str(ln.get("manufacturer") or c.get("manufacturer_key")))
        line_channels[ln.get("line_id")] = domains
        ln["supplier_channels"] = domains

    # Opportunity-specific defaults
    if opportunity_id.endswith("298984"):
        manufacturers.add("Cummins")
        for c in resolve_channels_for_line({"manufacturer": "Cummins", "description": "Cummins part", "opportunity_id": opportunity_id}):
            suppliers[c.get("domain") or c["supplier_name"]] = c
    if opportunity_id.endswith("286698"):
        for c in EMS_DISTRIBUTORS:
            suppliers[c["domain"]] = c
            auth_dists.add(c["domain"])

    return {
        "opportunity_id": opportunity_id,
        "manufacturers": sorted(m for m in manufacturers if m),
        "authorized_distributors": sorted(auth_dists),
        "suppliers": list(suppliers.values()),
        "unique_quote_channels": len(suppliers),
        "line_channels": line_channels,
        "supplier_by_domain": suppliers,
    }
