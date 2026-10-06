"""Human-like search query generation."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

# Manufacturer → owned store / catalog search templates
_MFR_SITE: dict[str, list[str]] = {
    "CUMMINS": [
        "shop.cummins.com",
        "cummins.com",
        "parts.cummins.com",
    ],
    "FORD": ["ford.com", "parts.ford.com", "shop.ford.com"],
    "SMITH-BLAIR": ["smith-blair.com"],
    "SMITH BLAIR": ["smith-blair.com"],
    "FLEETGUARD": ["fleetguard.com", "cummins.com"],
    "GRAINGER": ["grainger.com"],
    "CATERPILLAR": ["cat.com", "parts.cat.com"],
    "JOHN DEERE": ["deere.com", "shop.deere.com"],
    "DYNAREX": ["dynarex.com"],
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


_EXTRA_MFR_SITES: dict[str, list[str]] = {
    "MYERS": ["femyers.com", "pentair.com", "grainger.com", "zoro.com", "supplyhouse.com"],
    "FE MYERS": ["femyers.com", "pentair.com", "grainger.com", "zoro.com", "supplyhouse.com"],
    "PENTAIR": ["pentair.com", "grainger.com", "zoro.com", "supplyhouse.com"],
    "GOULDS": ["goulds.com", "xylemwatersolutions.com", "grainger.com", "zoro.com"],
}


def manufacturer_sites(manufacturer: str | None) -> list[str]:
    key = _clean(manufacturer).upper()
    if not key:
        return []
    if key in _MFR_SITE:
        return list(_MFR_SITE[key])
    if key in _EXTRA_MFR_SITES:
        return list(_EXTRA_MFR_SITES[key])
    for k, sites in _MFR_SITE.items():
        if key.startswith(k) or k.startswith(key):
            return list(sites)
    for k, sites in _EXTRA_MFR_SITES.items():
        if key.startswith(k) or k in key or k.startswith(key):
            return list(sites)
    return []


def build_query_variants(identity: dict[str, Any]) -> list[str]:
    """Aggressive human-like query set — NEW-first when condition assumed."""
    mfr = _clean(identity.get("manufacturer") or identity.get("brand"))
    pn = _clean(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("sku")
        or identity.get("model")
    )
    model = _clean(identity.get("model"))
    desc = _clean(identity.get("raw_description") or identity.get("description"))
    desc_short = " ".join(desc.split()[:6]) if desc else ""
    spec = identity.get("_spec_resolution") or {}
    mand = " ".join(str(x).split("=", 1)[-1] for x in (spec.get("mandatory_attributes") or [])[:4])

    qs: list[str] = []
    if pn:
        qs.extend(
            [
                pn,
                f'"{pn}"',
                f"{pn} price",
                f"{pn} buy",
                f"{pn} new",
                f"{pn} distributor",
                f"{pn} supplier",
                f"{pn} catalog",
                f"{pn} pdf",
            ]
        )
    if mfr and pn:
        qs.extend(
            [
                f"{mfr} {pn}",
                f"{mfr} {pn} price",
                f"{mfr} {pn} new",
                f"{mfr} {pn} buy",
                f"{mfr} {pn} distributor",
            ]
        )
    if mfr and pn and desc_short:
        qs.append(f"{mfr} {desc_short} {pn}")
    if mfr and model and model != pn:
        qs.extend([f"{mfr} {model}", f"{mfr} {model} new"])
    if model and model != pn:
        qs.extend([model, f"{model} price"])
    if pn and desc_short:
        qs.append(f"{pn} {desc_short}")

    for site in manufacturer_sites(mfr):
        if pn:
            qs.extend([f"site:{site} {pn}", f"site:{site} {pn} price", f"site:{site} {pn} new"])

    if not pn and desc:
        qs.extend([desc, f"{desc} buy price new"])
        if mfr:
            qs.append(f"{mfr} {desc_short or desc[:60]}")
    if mand and not pn:
        qs.extend([mand, f"{mand} price new"])

    out: list[str] = []
    seen: set[str] = set()
    for q in qs:
        qn = _clean(q)
        if len(qn) < 3:
            continue
        key = qn.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(qn)
    return out


def manufacturer_direct_urls(identity: dict[str, Any]) -> list[str]:
    """Guess manufacturer product/search URLs before SERP."""
    mfr = _clean(identity.get("manufacturer") or identity.get("brand"))
    pn = _clean(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("sku")
        or identity.get("model")
    )
    if not pn:
        return []
    urls: list[str] = []
    pn_l = pn.lower()
    q = quote_plus(pn)
    key = mfr.upper()
    if "CUMMINS" in key or (not mfr and re.match(r"^\d{6,}[A-Z]*$", pn, re.I)):
        urls.extend(
            [
                f"https://shop.cummins.com/SC/product/cummins-injector-kit-{pn_l}",
                f"https://shop.cummins.com/us/en/search?q={q}",
                f"https://www.cummins.com/parts/search?q={q}",
            ]
        )
    if "FORD" in key:
        urls.append(f"https://parts.ford.com/shop/en/us/search?text={q}")
    if "SMITH" in key and "BLAIR" in key:
        urls.append(f"https://www.smith-blair.com/search?q={q}")
    if "DYNAREX" in key:
        urls.append(f"https://www.dynarex.com/search?q={q}")
    for site in manufacturer_sites(mfr):
        urls.append(f"https://{site}/search?q={q}")
        if not site.startswith("www."):
            urls.append(f"https://www.{site}/search?q={q}")
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:8]


def distributor_search_urls(query: str, identity: dict[str, Any] | None = None) -> list[str]:
    q = quote_plus(query)
    base = [
        f"https://www.grainger.com/search?searchQuery={q}",
        f"https://www.zoro.com/search?q={q}",
        f"https://www.globalindustrial.com/search?q={q}",
        f"https://www.fastenal.com/product/search?term={q}",
        f"https://www.fleetpride.com/search?q={q}",
        f"https://www.dieselpartsdirect.com/search?q={q}",
        f"https://www.thedieselstore.com/search?q={q}",
        f"https://www.alliantpower.com/search?q={q}",
        f"https://www.finditparts.com/search?q={q}",
        f"https://advancedtruckparts.com/search?q={q}",
        f"https://www.motionindustries.com/products/search?q={q}",
        f"https://www.mscdirect.com/browse/tn/?searchterm={q}",
    ]
    if identity:
        try:
            from public_price_search.domain_map import category_search_urls

            # Category-prioritized sellers first
            return list(dict.fromkeys(category_search_urls(identity, limit=10) + base))
        except Exception:
            pass
    return base


def known_product_urls(identity: dict[str, Any]) -> list[str]:
    """Deterministic product-page guesses that often work when SERP is blocked."""
    pn = _clean(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("sku")
        or identity.get("model")
    )
    mfr = _clean(identity.get("manufacturer") or identity.get("brand")).upper()
    if not pn:
        return []
    pn_l = pn.lower()
    urls: list[str] = []
    if "CUMMINS" in mfr or "FLEETGUARD" in mfr or re.match(r"^\d{6,}[A-Z]{0,3}$", pn, re.I) or re.match(r"^[A-Z]{1,3}\d{3,}$", pn, re.I):
        urls.extend(
            [
                f"https://www.dieselpartsdirect.com/search?q={quote_plus(pn)}",
                f"https://advancedtruckparts.com/search?q={quote_plus(pn)}",
                f"https://www.thedieselstore.com/search?type=product&q={quote_plus(pn)}",
                f"https://www.alliantpower.com/search?q={quote_plus(pn)}",
                f"https://www.finditparts.com/search?q={quote_plus(pn)}",
                f"https://parts.alliantpower.com/en-us/search?text={quote_plus(pn)}",
                f"https://shop.cummins.com/SC/product/cummins-injector-kit-{pn_l}",
            ]
        )
        if pn.upper() == "5579409PX":
            urls[0:0] = [
                "https://www.alliantpower.com/products/26717086/cummins-5579409px-fuel-injector",
                "https://advancedtruckparts.com/products/5579409px-oem-cummins-fuel-injector-for-xpi-fuel-systems-on-epa13-8-9l-isc-isl",
                "https://shop.cummins.com/SC/product/cummins-injector-kit-5579409px/01t4N0000048jyrQAA",
            ]
    if "FORD" in mfr:
        urls.append(f"https://www.fordparts.com/Search?searchTerm={quote_plus(pn)}")
    if "DYNAREX" in mfr:
        urls.append(f"https://www.dynarex.com/search?q={quote_plus(pn)}")
    return list(dict.fromkeys(urls))[:10]
