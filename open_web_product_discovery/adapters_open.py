"""Additional open-domain URL pattern adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from exact_product_url_discovery.normalize import mpn_variants


def homelectrical_candidates(mpn: str, manufacturer: str | None = None) -> list[dict[str, Any]]:
    out = []
    for v in mpn_variants(mpn)[:2]:
        slug = v.lower().replace("/", "-")
        out.append(
            {
                "url": f"https://www.homelectrical.com/search?q={quote_plus(v)}",
                "domain": "homelectrical.com",
                "discovery_method": "HOMELECTRICAL_SEARCH",
                "confidence": 0.2,
                "note": "search_shell_for_conversion",
            }
        )
        if manufacturer and manufacturer.lower() in {"3m"}:
            out.append(
                {
                    "url": f"https://www.homelectrical.com/search?q={quote_plus('3M ' + v)}",
                    "domain": "homelectrical.com",
                    "discovery_method": "HOMELECTRICAL_SEARCH",
                    "confidence": 0.2,
                }
            )
    # Known exact patterns for 3M respiratory
    known = {
        "5P71": "https://www.homelectrical.com/6000-7000-series-half-and-full-facepiece-cartridges-filters.mco-5p71.1.html",
        "2097": "https://www.homelectrical.com/particulate-filter-p100-w-nuisance-ov.mmm-2097.1.html",
        "60926": "https://www.homelectrical.com/multi-gas-vapor-cartridge-filter-p100.mmm-60926.1.html",
    }
    key = (mpn or "").upper()
    if key in known:
        out.insert(
            0,
            {
                "url": known[key],
                "domain": "homelectrical.com",
                "discovery_method": "HOMELECTRICAL_PATTERN",
                "confidence": 0.8,
            },
        )
    return out


def activeplumbing_candidates(mpn: str) -> list[dict[str, Any]]:
    slug = (mpn or "").lower().replace("/", "-")
    return [
        {
            "url": f"https://www.activeplumbing.com/buy/product/{slug}/",
            "domain": "activeplumbing.com",
            "discovery_method": "ACTIVE_PLUMBING_PATTERN",
            "confidence": 0.45,
        }
    ]


def gsistore_candidates(mpn: str, manufacturer: str | None = None) -> list[dict[str, Any]]:
    slug = (mpn or "").lower()
    mfr = (manufacturer or "").lower().replace(" ", "-")
    urls = [f"https://www.gsistore.com/products/{slug}"]
    if mfr:
        urls.insert(0, f"https://www.gsistore.com/products/{mfr}-{slug}")
        urls.insert(0, f"https://www.gsistore.com/products/{mfr}-{slug}-thermostat")
    known = {
        "TH6220U2000": "https://www.gsistore.com/products/resideo-th6220u2000-thermostat",
        "RTH2300B": "https://www.gsistore.com/products/honeywell-rth2300b",
    }
    out = []
    if (mpn or "").upper() in known:
        out.append(
            {
                "url": known[mpn.upper()],
                "domain": "gsistore.com",
                "discovery_method": "GSISTORE_PATTERN",
                "confidence": 0.85,
            }
        )
    for u in urls:
        out.append(
            {
                "url": u,
                "domain": "gsistore.com",
                "discovery_method": "GSISTORE_PATTERN",
                "confidence": 0.35,
            }
        )
    return out
