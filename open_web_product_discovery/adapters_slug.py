"""Diesel Parts Direct + slug pattern adapters."""

from __future__ import annotations

from typing import Any

from exact_product_url_discovery.normalize import mpn_variants
from exact_product_url_discovery.validate_identity import validate_product_page


def diesel_slug_candidates(mpn: str) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for v in mpn_variants(mpn)[:4]:
        slug = v.lower()
        url = f"https://www.dieselpartsdirect.com/{slug}"
        if url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "url": url,
                "domain": "dieselpartsdirect.com",
                "discovery_method": "DIESEL_SLUG_PATTERN",
                "confidence": 0.55,
            }
        )
    return out


def northernsafety_candidates(mpn: str, manufacturer: str | None = None) -> list[dict[str, Any]]:
    mfr = (manufacturer or "Product").split()[0]
    return [
        {
            "url": f"https://www.northernsafety.com/Product/{mpn}/{mfr}-{mpn}",
            "domain": "northernsafety.com",
            "discovery_method": "NORTHERN_SAFETY_PATTERN",
            "confidence": 0.35,
        }
    ]
