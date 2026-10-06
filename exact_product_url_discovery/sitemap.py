"""XML / product sitemap discovery for exact product URLs."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from exact_product_url_discovery.classify import classify_candidate_url
from exact_product_url_discovery.models import (
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    METHOD_PRODUCT_SITEMAP,
    METHOD_SITEMAP,
    SITEMAP_CANDIDATES,
)
from exact_product_url_discovery.normalize import mpn_in_text, mpn_variants
from public_price_search.search import fetch_page


def _extract_locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I)


def discover_sitemap_urls(
    domain: str,
    *,
    mpn: str,
    manufacturer: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Search domain sitemaps for MPN-matching product URLs."""
    host = (domain or "").lower().replace("www.", "")
    if not host:
        return []
    base = f"https://www.{host}" if not host.startswith("http") else host
    if base.startswith("http") and "://" in base:
        # normalize to origin
        parsed = urlparse(base if "://" in domain else f"https://{host}")
        origin = f"{parsed.scheme}://{parsed.netloc}"
    else:
        origin = f"https://www.{host}"

    variants = mpn_variants(mpn)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Fetch root sitemaps + follow one index level
    queue: list[str] = [urljoin(origin + "/", path.lstrip("/")) for path in SITEMAP_CANDIDATES]
    fetched_indexes = 0
    while queue and len(out) < limit and fetched_indexes < 6:
        sm_url = queue.pop(0)
        fr = fetch_page(sm_url)
        if not fr.get("ok"):
            continue
        text = fr.get("text") or ""
        fetched_indexes += 1
        locs = _extract_locs(text)
        # sitemap index → enqueue child sitemaps
        child_smaps = [u for u in locs if "sitemap" in u.lower() and u.endswith(".xml")]
        for cu in child_smaps[:8]:
            if cu not in seen:
                queue.append(cu)
                seen.add(cu)
        for loc in locs:
            if loc in seen:
                continue
            if not any(mpn_in_text(v, loc) for v in variants[:3]):
                # also try manufacturer+mpn in path for short ids
                if manufacturer and mpn_in_text(mpn, loc):
                    pass
                else:
                    continue
            cls = classify_candidate_url(loc, mpn=mpn)
            if cls not in {EXACT_PRODUCT_VERIFIED, EXACT_PRODUCT_UNVERIFIED}:
                continue
            seen.add(loc)
            method = METHOD_PRODUCT_SITEMAP if "product" in sm_url.lower() else METHOD_SITEMAP
            out.append(
                {
                    "url": loc,
                    "domain": urlparse(loc).netloc.lower().replace("www.", ""),
                    "url_class": cls,
                    "discovery_method": method,
                    "sitemap": sm_url,
                    "match_type": "mpn_in_sitemap_loc",
                    "confidence": 0.7 if cls == EXACT_PRODUCT_VERIFIED else 0.45,
                }
            )
            if len(out) >= limit:
                break
    return out
