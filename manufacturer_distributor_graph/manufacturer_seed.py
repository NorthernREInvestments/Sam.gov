"""Seeded manufacturer identity + authorized distributor maps.

Build: 20261004-m3-manufacturer-distributor-graph-v1

Confidence is only HIGH when domain is curated; never invent manufacturers.
"""

from __future__ import annotations

from typing import Any

from manufacturer_distributor_graph.models import (
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_UNKNOWN,
    OEM_AUTHORIZED_DISTRIBUTOR,
    OEM_SELLS_DIRECT,
)

# manufacturer_key → identity + locators + known authorized/high-yield sellers
_MANUFACTURERS: dict[str, dict[str, Any]] = {
    "3M": {
        "domain": "3m.com",
        "product_search": "https://www.3m.com/3M/en_US/p/c/b/?Ntt={q}",
        "locator": "https://www.3m.com/3M/en_US/company-us/where-to-buy/",
        "sells_direct": True,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("mscdirect.com", AUTHORIZED_UNKNOWN),
            ("grainger.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "WATTS": {
        "domain": "watts.com",
        "product_search": "https://www.watts.com/search?q={q}",
        "locator": "https://www.watts.com/resources/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("pexuniverse.com", AUTHORIZED_UNKNOWN),
            ("plumbingsupply.com", AUTHORIZED_UNKNOWN),
            ("mccoys.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "LEVITON": {
        "domain": "leviton.com",
        "product_search": "https://www.leviton.com/en/search?q={q}",
        "locator": "https://www.leviton.com/en/support/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("platt.com", AUTHORIZED_CONFIRMED),
            ("rspsupply.com", AUTHORIZED_UNKNOWN),
            ("rexelusa.com", AUTHORIZED_UNKNOWN),
            ("elliott-electric.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "BRADY": {
        "domain": "bradyid.com",
        "product_search": "https://www.bradyid.com/search?text={q}",
        "locator": "https://www.bradyid.com/support/where-to-buy",
        "sells_direct": True,
        "distributors": [
            ("bradyid.com", AUTHORIZED_CONFIRMED),
            ("mscdirect.com", AUTHORIZED_UNKNOWN),
            ("quill.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "FLEETGUARD": {
        "domain": "cummins.com",
        "product_search": "https://shop.cummins.com/us/en/search/?text={q}",
        "locator": "https://www.cummins.com/support/find-location",
        "sells_direct": True,
        "distributors": [
            ("dieselpartsdirect.com", AUTHORIZED_UNKNOWN),
            ("thedieselstore.com", AUTHORIZED_UNKNOWN),
            ("shop.cummins.com", AUTHORIZED_CONFIRMED),
            ("finditparts.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "CUMMINS": {
        "domain": "cummins.com",
        "product_search": "https://shop.cummins.com/us/en/search/?text={q}",
        "locator": "https://www.cummins.com/support/find-location",
        "sells_direct": True,
        "distributors": [
            ("dieselpartsdirect.com", AUTHORIZED_UNKNOWN),
            ("shop.cummins.com", AUTHORIZED_CONFIRMED),
            ("thedieselstore.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "SHARKBITE": {
        "domain": "sharkbite.com",
        "product_search": "https://www.sharkbite.com/us/en/search?q={q}",
        "locator": "https://www.sharkbite.com/us/en/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("mccoys.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("lowes.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "HONEYWELL": {
        "domain": "honeywellhome.com",
        "product_search": "https://www.honeywellhome.com/us/en/search/?text={q}",
        "locator": "https://www.honeywellhome.com/us/en/support/where-to-buy/",
        "sells_direct": True,
        "distributors": [
            ("parts-hvac.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "RESIDEO": {
        "domain": "resideo.com",
        "product_search": "https://www.resideo.com/us/en/search/?text={q}",
        "locator": "https://www.resideo.com/us/en/corporate/where-to-buy/",
        "sells_direct": True,
        "distributors": [
            ("parts-hvac.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "APRILAIRE": {
        "domain": "aprilaire.com",
        "product_search": "https://www.aprilaire.com/search?q={q}",
        "locator": "https://www.aprilaire.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("parts-hvac.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "MAKITA": {
        "domain": "makitatools.com",
        "product_search": "https://www.makitatools.com/search?q={q}",
        "locator": "https://www.makitatools.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("toolbarn.com", AUTHORIZED_UNKNOWN),
            ("acmetools.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("nationaldistributorllc.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "HUBBELL": {
        "domain": "hubbell.com",
        "product_search": "https://www.hubbell.com/hubbell/en/search?q={q}",
        "locator": "https://www.hubbell.com/hubbell/en/support/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("platt.com", AUTHORIZED_UNKNOWN),
            ("rspsupply.com", AUTHORIZED_UNKNOWN),
            ("rexelusa.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "IDEAL": {
        "domain": "idealind.com",
        "product_search": "https://www.idealind.com/us/en/search.html?q={q}",
        "locator": "https://www.idealind.com/us/en/about-us/where-to-buy.html",
        "sells_direct": False,
        "distributors": [
            ("rspsupply.com", AUTHORIZED_UNKNOWN),
            ("platt.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "FLUKE": {
        "domain": "fluke.com",
        "product_search": "https://www.fluke.com/en-us/search?q={q}",
        "locator": "https://www.fluke.com/en-us/support/where-to-buy",
        "sells_direct": True,
        "distributors": [
            ("fluke.com", AUTHORIZED_CONFIRMED),
            ("rspsupply.com", AUTHORIZED_UNKNOWN),
            ("platt.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "OATEY": {
        "domain": "oatey.com",
        "product_search": "https://www.oatey.com/search?q={q}",
        "locator": "https://www.oatey.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("lowes.com", AUTHORIZED_UNKNOWN),
            ("mccoys.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "SIOUX CHIEF": {
        "domain": "siouxchief.com",
        "product_search": "https://www.siouxchief.com/search?q={q}",
        "locator": "https://www.siouxchief.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("mccoys.com", AUTHORIZED_UNKNOWN),
            ("supplyhouse.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "CRC": {
        "domain": "crcindustries.com",
        "product_search": "https://www.crcindustries.com/search?q={q}",
        "locator": "https://www.crcindustries.com/where-to-buy",
        "sells_direct": True,
        "distributors": [
            ("crcautocare.com", AUTHORIZED_CONFIRMED),
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("autozone.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "LOCTITE": {
        "domain": "henkel-adhesives.com",
        "product_search": "https://www.henkel-adhesives.com/us/en/search.html?q={q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("mscdirect.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "PERMATEX": {
        "domain": "permatex.com",
        "product_search": "https://www.permatex.com/search?q={q}",
        "locator": "https://www.permatex.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("autozone.com", AUTHORIZED_UNKNOWN),
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "GATES": {
        "domain": "gates.com",
        "product_search": "https://www.gates.com/us/en/search.html?q={q}",
        "locator": "https://www.gates.com/us/en/support/where-to-buy.html",
        "sells_direct": False,
        "distributors": [
            ("autobuffy.com", AUTHORIZED_UNKNOWN),
            ("rockauto.com", AUTHORIZED_UNKNOWN),
            ("summitracing.com", AUTHORIZED_UNKNOWN),
            ("dieselpartsdirect.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "TIMKEN": {
        "domain": "timken.com",
        "product_search": "https://www.timken.com/search/?q={q}",
        "locator": "https://www.timken.com/contact/find-a-distributor/",
        "sells_direct": False,
        "distributors": [
            ("maxtran.com", AUTHORIZED_UNKNOWN),
            ("rockauto.com", AUTHORIZED_UNKNOWN),
            ("summitracing.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "WIX": {
        "domain": "wixfilters.com",
        "product_search": "https://www.wixfilters.com/catalog/search?q={q}",
        "locator": "https://www.wixfilters.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("rockauto.com", AUTHORIZED_UNKNOWN),
            ("summitracing.com", AUTHORIZED_UNKNOWN),
            ("autozone.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "FRAM": {
        "domain": "fram.com",
        "product_search": "https://www.fram.com/search?q={q}",
        "locator": "https://www.fram.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("autozone.com", AUTHORIZED_UNKNOWN),
            ("rockauto.com", AUTHORIZED_UNKNOWN),
            ("summitracing.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "FEIT": {
        "domain": "feit.com",
        "product_search": "https://www.feit.com/search?q={q}",
        "locator": "https://www.feit.com/pages/where-to-buy",
        "sells_direct": True,
        "distributors": [
            ("1000bulbs.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("lowes.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "SYLVANIA": {
        "domain": "sylvania-automotive.com",
        "product_search": "https://www.1000bulbs.com/search?q=Sylvania+{q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("1000bulbs.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "GE": {
        "domain": "gelighting.com",
        "product_search": "https://www.1000bulbs.com/search?q=GE+{q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("1000bulbs.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "LITHONIA": {
        "domain": "acuitybrands.com",
        "product_search": "https://www.1000bulbs.com/search?q=Lithonia+{q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("1000bulbs.com", AUTHORIZED_UNKNOWN),
            ("grainger.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "BIC": {
        "domain": "bic.com",
        "product_search": "https://www.quill.com/search?keywords=BIC+{q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("staples.com", AUTHORIZED_UNKNOWN),
            ("officedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "HON": {
        "domain": "hon.com",
        "product_search": "https://www.hon.com/search?q={q}",
        "locator": "https://www.hon.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("staples.com", AUTHORIZED_UNKNOWN),
            ("officedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "SAFCO": {
        "domain": "safcoproducts.com",
        "product_search": "https://www.safcoproducts.com/search?q={q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("globalindustrial.com", AUTHORIZED_UNKNOWN),
            ("quill.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "GLOBAL INDUSTRIAL": {
        "domain": "globalindustrial.com",
        "product_search": "https://www.globalindustrial.com/search?q={q}",
        "locator": None,
        "sells_direct": True,
        "distributors": [
            ("globalindustrial.com", AUTHORIZED_CONFIRMED),
        ],
    },
    "CHANNELLOCK": {
        "domain": "channellock.com",
        "product_search": "https://www.channellock.com/search?q={q}",
        "locator": "https://www.channellock.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("nationaldistributorllc.com", AUTHORIZED_UNKNOWN),
            ("quill.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "GORILLA": {
        "domain": "gorillatough.com",
        "product_search": "https://www.gorillatough.com/search?q={q}",
        "locator": "https://www.gorillatough.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
            ("lowes.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "J-B WELD": {
        "domain": "jbweld.com",
        "product_search": "https://www.jbweld.com/search?q={q}",
        "locator": "https://www.jbweld.com/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("quill.com", AUTHORIZED_UNKNOWN),
            ("autozone.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "UVEX": {
        "domain": "uvex.us",
        "product_search": "https://www.uvex.us/search?q={q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [
            ("mscdirect.com", AUTHORIZED_UNKNOWN),
            ("quill.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "PHILIPS": {
        "domain": "philips.com",
        "product_search": "https://www.1000bulbs.com/search?q=Philips+{q}",
        "locator": None,
        "sells_direct": False,
        "distributors": [("1000bulbs.com", AUTHORIZED_UNKNOWN)],
    },
    "BROTHER": {
        "domain": "brother-usa.com",
        "product_search": "https://www.brother-usa.com/search?q={q}",
        "locator": None,
        "sells_direct": True,
        "distributors": [("brother-usa.com", AUTHORIZED_CONFIRMED), ("quill.com", AUTHORIZED_UNKNOWN)],
    },
    "MILWAUKEE": {
        "domain": "milwaukeetool.com",
        "product_search": "https://www.milwaukeetool.com/search?term={q}",
        "locator": "https://www.milwaukeetool.com/Where-To-Buy",
        "sells_direct": False,
        "distributors": [
            ("nationaldistributorllc.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
    "DEWALT": {
        "domain": "dewalt.com",
        "product_search": "https://www.dewalt.com/search?term={q}",
        "locator": "https://www.dewalt.com/en-us/where-to-buy",
        "sells_direct": False,
        "distributors": [
            ("motion.com", AUTHORIZED_UNKNOWN),
            ("homedepot.com", AUTHORIZED_UNKNOWN),
        ],
    },
}


def _key(manufacturer: str | None) -> str | None:
    if not manufacturer:
        return None
    u = manufacturer.strip().upper()
    if u in _MANUFACTURERS:
        return u
    for k in _MANUFACTURERS:
        if k in u or u in k:
            return k
    # first token fallback only if exact seed exists
    tok = u.split()[0] if u.split() else ""
    return tok if tok in _MANUFACTURERS else None


def resolve_manufacturer(item: dict[str, Any]) -> dict[str, Any]:
    """Resolve manufacturer/OEM from identity — never invent."""
    mfr = (item.get("manufacturer") or item.get("brand") or "").strip()
    key = _key(mfr)
    if not key:
        return {
            "manufacturer": mfr or None,
            "manufacturer_key": None,
            "manufacturer_domain": None,
            "confidence": "NONE",
            "manufacturer_product_search_route": None,
            "manufacturer_distributor_locator": None,
            "manufacturer_quote_route": None,
            "sells_direct": False,
            "resolved": False,
        }
    seed = _MANUFACTURERS[key]
    q = (item.get("mpn") or item.get("part_number") or "").strip()
    search = (seed.get("product_search") or "").replace("{q}", q)
    return {
        "manufacturer": mfr or key.title(),
        "manufacturer_key": key,
        "manufacturer_domain": seed.get("domain"),
        "confidence": "HIGH",
        "manufacturer_product_search_route": search or None,
        "manufacturer_distributor_locator": seed.get("locator"),
        "manufacturer_quote_route": seed.get("locator") or search or None,
        "sells_direct": bool(seed.get("sells_direct")),
        "resolved": True,
        "edge_type": OEM_SELLS_DIRECT if seed.get("sells_direct") else None,
    }


def authorized_distributors(manufacturer: str | None) -> list[dict[str, Any]]:
    key = _key(manufacturer)
    if not key:
        return []
    seed = _MANUFACTURERS[key]
    rows = []
    for domain, auth in seed.get("distributors") or []:
        rows.append(
            {
                "manufacturer": key,
                "distributor_domain": domain,
                "authorization": auth,
                "edge_type": OEM_AUTHORIZED_DISTRIBUTOR,
                "provenance": "seeded_manufacturer_map",
            }
        )
    return rows


def all_seeded_manufacturers() -> list[str]:
    return sorted(_MANUFACTURERS.keys())
