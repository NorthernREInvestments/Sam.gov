"""ManufacturerChannelProfile — reusable OEM/dealer/distributor channel maps (L.20)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.quote_readiness import (
    AUTHORIZATION_NOT_REQUIRED,
    AUTHORIZATION_UNKNOWN,
    AUTHORIZED_LIKELY,
)

BUILD = "20260928-m3-phase-l20-supplier-acquisition-evidence-recovery"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CHANNELS_PATH = DATA / "manufacturer_channel_profiles.json"

# Price evidence kinds (§5)
ACTUAL_SUPPLIER_QUOTE = "ACTUAL_SUPPLIER_QUOTE"
PUBLIC_ACQUISITION_PRICE = "PUBLIC_ACQUISITION_PRICE"
CONTRACT_CATALOG_PRICE = "CONTRACT_CATALOG_PRICE"
MANUFACTURER_LIST_PRICE = "MANUFACTURER_LIST_PRICE"
QUOTE_REQUIRED = "QUOTE_REQUIRED"
NO_ACQUISITION_EVIDENCE = "NO_ACQUISITION_EVIDENCE"

AUTHORIZED_CONFIRMED = "AUTHORIZED_CONFIRMED"
RESELLER_CONFIRMED = "RESELLER_CONFIRMED"
CHANNEL_UNKNOWN = "CHANNEL_UNKNOWN"

KNOWN_SUPPLIER_FOR_PRODUCT = "KNOWN_SUPPLIER_FOR_PRODUCT"

# Curated manufacturer channel maps — evidence URLs are official directories / OEM sites.
# Authorization is LIKELY unless manufacturer partner confirmation is attached.
MANUFACTURER_CHANNEL_CATALOG: dict[str, dict[str, Any]] = {
    "apple": {
        "manufacturer": "Apple",
        "product_categories": ["IT", "tablets", "computers"],
        "direct_sales": True,
        "distributor_network": True,
        "authorized_dealer_directory": "https://locate.apple.com/",
        "government_channel": "https://www.apple.com/education/",
        "contract_pricing_source": ["CDW-G", "Insight", "SHI"],
        "territory_restrictions": False,
        "typical_quote_requirement": True,
        "channels": [
            {
                "supplier_domain": "apple.com",
                "name": "Apple",
                "source_type": "OEM",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "manufacturer_direct_store",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.apple.com/",
                "price_kind": MANUFACTURER_LIST_PRICE,
                "note": "MSRP/list only — not acquisition cost",
            },
            {
                "supplier_domain": "cdw-g.com",
                "name": "CDW-G",
                "source_type": "DISTRIBUTOR",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "apple_education_government_authorized_reseller",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.cdwg.com/",
                "price_kind": QUOTE_REQUIRED,
            },
            {
                "supplier_domain": "insight.com",
                "name": "Insight",
                "source_type": "DISTRIBUTOR",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "apple_authorized_enterprise_reseller",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.insight.com/",
                "price_kind": QUOTE_REQUIRED,
            },
            {
                "supplier_domain": "shi.com",
                "name": "SHI International",
                "source_type": "DISTRIBUTOR",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "apple_authorized_reseller",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.shi.com/",
                "price_kind": QUOTE_REQUIRED,
            },
        ],
    },
    "ford": {
        "manufacturer": "Ford",
        "product_categories": ["VEHICLE", "fleet", "police"],
        "direct_sales": False,
        "distributor_network": True,
        "authorized_dealer_directory": "https://www.ford.com/dealerships/",
        "government_channel": "https://www.ford.com/fleet/",
        "contract_pricing_source": ["Sourcewell", "NASPO"],
        "territory_restrictions": True,
        "typical_quote_requirement": True,
        "channels": [
            {
                "supplier_domain": "ford.com",
                "name": "Ford Fleet",
                "source_type": "OEM",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "oem_fleet_program",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.ford.com/fleet/",
                "price_kind": QUOTE_REQUIRED,
                "territory_restricted": True,
            },
            {
                "supplier_domain": "sourcewell-mn.gov",
                "name": "Sourcewell Fleet Contracts",
                "source_type": "COOPERATIVE",
                "authorization_state": AUTHORIZATION_NOT_REQUIRED,
                "authorization_evidence": "public_cooperative_contract_catalog",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.sourcewell-mn.gov/",
                "price_kind": CONTRACT_CATALOG_PRICE,
                "eligibility": "cooperative_member_or_participating_agency",
                "note": "Catalog visible; purchasing eligibility may be required",
            },
            {
                "supplier_domain": "naspovaluepoint.org",
                "name": "NASPO ValuePoint",
                "source_type": "COOPERATIVE",
                "authorization_state": AUTHORIZATION_NOT_REQUIRED,
                "authorization_evidence": "naspo_vehicle_contracts",
                "product_fit": "FAMILY",
                "locator_url": "https://www.naspovaluepoint.org/",
                "price_kind": CONTRACT_CATALOG_PRICE,
                "eligibility": "participating_entity",
            },
        ],
    },
    "caterpillar": {
        "manufacturer": "Caterpillar",
        "product_categories": ["EQUIPMENT", "marine", "engines"],
        "direct_sales": False,
        "distributor_network": True,
        "authorized_dealer_directory": "https://www.cat.com/en_US/support/dealer-locator.html",
        "government_channel": "https://www.cat.com/en_US/products/new/power-systems.html",
        "contract_pricing_source": ["Sourcewell"],
        "territory_restrictions": True,
        "typical_quote_requirement": True,
        "channels": [
            {
                "supplier_domain": "cat.com",
                "name": "Caterpillar Dealer Network",
                "source_type": "OEM",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "oem_dealer_locator",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "locator_url": "https://www.cat.com/en_US/support/dealer-locator.html",
                "price_kind": QUOTE_REQUIRED,
                "territory_restricted": True,
                "note": "Territory dealer quote required — cross-territory not assumed",
            },
            {
                "supplier_domain": "sourcewell-mn.gov",
                "name": "Sourcewell Heavy Equipment",
                "source_type": "COOPERATIVE",
                "authorization_state": AUTHORIZATION_NOT_REQUIRED,
                "authorization_evidence": "sourcewell_construction_equipment",
                "product_fit": "FAMILY",
                "locator_url": "https://www.sourcewell-mn.gov/",
                "price_kind": CONTRACT_CATALOG_PRICE,
                "eligibility": "cooperative_member",
            },
        ],
    },
    "asus": {
        "manufacturer": "ASUS",
        "product_categories": ["IT"],
        "direct_sales": True,
        "distributor_network": True,
        "authorized_dealer_directory": "https://www.asus.com/us/support/",
        "government_channel": None,
        "contract_pricing_source": ["CDW", "Insight"],
        "territory_restrictions": False,
        "typical_quote_requirement": True,
        "channels": [
            {
                "supplier_domain": "asus.com",
                "name": "ASUS",
                "source_type": "OEM",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "oem_direct",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "price_kind": MANUFACTURER_LIST_PRICE,
            },
            {
                "supplier_domain": "cdw.com",
                "name": "CDW",
                "source_type": "DISTRIBUTOR",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "national_it_distributor",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "price_kind": QUOTE_REQUIRED,
            },
        ],
    },
    "cisco": {
        "manufacturer": "Cisco",
        "product_categories": ["IT", "networking"],
        "direct_sales": False,
        "distributor_network": True,
        "authorized_dealer_directory": "https://locatr.cloudapps.cisco.com/WWChannels/LOCATR/openBasicSearch.do",
        "government_channel": "https://www.cisco.com/c/en/us/solutions/industries/government.html",
        "contract_pricing_source": ["CDW-G", "SHI", "Insight"],
        "territory_restrictions": False,
        "typical_quote_requirement": True,
        "channels": [
            {
                "supplier_domain": "cisco.com",
                "name": "Cisco Partner Locator",
                "source_type": "OEM",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "partner_locator",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "price_kind": QUOTE_REQUIRED,
            },
            {
                "supplier_domain": "cdw-g.com",
                "name": "CDW-G",
                "source_type": "DISTRIBUTOR",
                "authorization_state": AUTHORIZED_LIKELY,
                "authorization_evidence": "cisco_authorized_partner",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
                "price_kind": QUOTE_REQUIRED,
            },
        ],
    },
}


def _utc() -> str:
    return now_utc().isoformat()


def _norm_mfr(name: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    aliases = {
        "cat": "caterpillar",
        "caterpillarc18": "caterpillar",
        "appleinc": "apple",
        "fordmotor": "ford",
        "fordmotorcompany": "ford",
    }
    return aliases.get(s, s)


def get_manufacturer_channel_profile(manufacturer: str | None) -> dict[str, Any] | None:
    key = _norm_mfr(manufacturer)
    if not key:
        return None
    for k, prof in MANUFACTURER_CHANNEL_CATALOG.items():
        if key == k or key.startswith(k) or k in key:
            out = dict(prof)
            out["kind"] = "ManufacturerChannelProfile"
            out["profile_key"] = k
            out["build"] = BUILD
            return out
    return None


def resolve_manufacturer_channels(
    *,
    manufacturer: str | None,
    model: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Return ManufacturerChannelProfile + candidate supplier dicts for grading."""
    mfr = manufacturer
    if not mfr and title:
        low = title.lower()
        if "apple" in low or "ipad" in low:
            mfr = "Apple"
        elif "ford" in low:
            mfr = "Ford"
        elif "caterpillar" in low or re.search(r"\bcat\b", low):
            mfr = "Caterpillar"
        elif "asus" in low or "chromebox" in low:
            mfr = "ASUS"
        elif "cisco" in low:
            mfr = "Cisco"
    prof = get_manufacturer_channel_profile(mfr)
    if not prof:
        return {
            "kind": "ManufacturerChannelResolution",
            "manufacturer": mfr,
            "model": model,
            "resolved": False,
            "profile": None,
            "candidates": [],
            "status": CHANNEL_UNKNOWN,
        }
    candidates = []
    for ch in prof.get("channels") or []:
        c = dict(ch)
        c["manufacturer"] = prof.get("manufacturer")
        c["model"] = model
        c["channel_profile_key"] = prof.get("profile_key")
        if model and c.get("product_fit") == "EXACT":
            c["exact_sku_or_model"] = model
        candidates.append(c)
    return {
        "kind": "ManufacturerChannelResolution",
        "manufacturer": prof.get("manufacturer"),
        "model": model,
        "resolved": True,
        "profile": prof,
        "candidates": candidates,
        "status": AUTHORIZED_LIKELY,
        "territory_restrictions": prof.get("territory_restrictions"),
        "typical_quote_requirement": prof.get("typical_quote_requirement"),
    }


def persist_channel_profiles(profiles: list[dict[str, Any]]) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "ManufacturerChannelProfileRegistry",
        "build": BUILD,
        "generated_at": _utc(),
        "profiles": profiles,
    }
    CHANNELS_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return CHANNELS_PATH


def marketplace_blocked(domain_or_url: str | None) -> bool:
    t = str(domain_or_url or "").lower()
    return any(
        x in t
        for x in (
            "ebay.",
            "amazon.",
            "alibaba.",
            "craigslist.",
            "facebook.com/marketplace",
            "offerup.",
            "mercari.",
        )
    )
