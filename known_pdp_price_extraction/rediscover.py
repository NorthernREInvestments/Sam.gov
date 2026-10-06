"""Same-domain live PDP rediscovery when known URLs are 404/invalid."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from exact_product_url_discovery.validate_identity import validate_product_page
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from open_web_product_discovery.transport import search_open_web
from price_adapters.validate import seller_of
from public_price_search.search import fetch_page


def shopify_suggest_products(domain: str, query: str, *, limit: int = 5) -> list[str]:
    """Public Shopify predictive search → product URLs (MPN-filtered when possible)."""
    from known_pdp_price_extraction.adapters import shopify_suggest_products_filtered

    parts = (query or "").split()
    mfr = parts[0] if parts else ""
    mpn = parts[-1] if parts else ""
    filtered = shopify_suggest_products_filtered(domain, mfr, mpn, limit=limit)
    if filtered:
        return filtered
    # fallback unfiltered (caller validates identity)
    urls: list[str] = []
    for path in (
        f"https://www.{domain}/search/suggest.json?q={quote_plus(query)}&resources[type]=product&resources[limit]={limit}",
        f"https://{domain}/search/suggest.json?q={quote_plus(query)}&resources[type]=product&resources[limit]={limit}",
    ):
        fr = fetch_page(path, use_budget=True)
        text = fr.get("text") or ""
        if not text.startswith("{") and not text.startswith("["):
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        products = (
            ((data.get("resources") or {}).get("results") or {}).get("products")
            or data.get("products")
            or []
        )
        for p in products[:limit]:
            if not isinstance(p, dict):
                continue
            handle = p.get("handle") or ""
            if handle:
                urls.append(f"https://www.{domain}/products/{handle}")
        if urls:
            break
    return list(dict.fromkeys(urls))[:limit]


def rediscover_live_pdps(item: dict[str, Any], *, domains: list[str] | None = None, limit: int = 4) -> list[dict[str, Any]]:
    """Find live exact PDPs on known productive domains (not broad web fanout)."""
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    cat = str(item.get("category") or "").lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Category / manufacturer preferred open domains that have worked before
    prefs = list(domains or [])
    if not prefs:
        prefs = {
            "lighting": ["lightbulbs.com", "1000bulbs.com", "bulbs.com"],
            "tools": ["platt.com", "acmetools.com", "toolbarn.com"],
            "mro": ["rshughes.com", "homelectrical.com", "pksafety.com", "crcautocare.com"],
            "ppe": ["pksafety.com", "northernsafety.com", "seton.com"],
            "plumbing": ["activeplumbing.com", "plumbingsupply.com"],
            "electrical": ["platt.com", "homelectrical.com"],
            "furniture": ["quill.com"],
            "industrial": ["quill.com", "rshughes.com"],
        }.get(cat, ["rshughes.com", "lightbulbs.com", "platt.com", "homelectrical.com", "quill.com"])
        # Manufacturer overrides
        mfr_u = mfr.upper()
        if "PHILIPS" in mfr_u or "SYLVANIA" in mfr_u or mfr_u == "GE":
            prefs = ["lightbulbs.com", "1000bulbs.com", "bulbs.com"] + prefs
        if "BRADY" in mfr_u:
            prefs = ["seton.com", "northernsafety.com", "quill.com"] + prefs
        if any(x in mfr_u for x in ("MAKITA", "DEWALT", "CRESCENT", "IRWIN", "KLEIN")):
            prefs = ["platt.com", "acmetools.com"] + prefs
        if any(x in mfr_u for x in ("CRC", "PERMATEX", "GORILLA", "J-B", "JB WELD", "3M", "LOCTITE", "WD-40")):
            prefs = ["rshughes.com", "homelectrical.com"] + prefs

    # de-dupe prefs
    seen_d: set[str] = set()
    prefs2 = []
    for d in prefs:
        if d and d not in seen_d:
            seen_d.add(d)
            prefs2.append(d)
    prefs = prefs2[:6]

    def _add(url: str, method: str) -> None:
        if not url.startswith("http") or url in seen or is_search_shell(url):
            return
        seen.add(url)
        out.append({"url": url, "domain": seller_of(url), "discovery_method": method, "soft_identity": False})

    # Platt GraphQL for tools/electrical/mro
    if any(d == "platt.com" for d in prefs):
        for row in platt_suggest_urls(mpn, manufacturer=mfr, limit=3):
            _add(row.get("url") or "", "PLATT_GRAPHQL")

    for dom in prefs:
        if len(out) >= limit:
            break
        # Shopify suggest
        for u in shopify_suggest_products(dom, f"{mfr} {mpn}".strip(), limit=3):
            _add(u, "SHOPIFY_SUGGEST")
        # Internal search conversion
        for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
            _add(row.get("url") or "", "INTERNAL_SEARCH")
        # Site-scoped Bing (bounded)
        if len(out) < limit:
            res = search_open_web(f'site:{dom} {mfr} "{mpn}"', limit=4, prefer_playwright=True)
            for hit in res.get("results") or []:
                u = hit.get("url") or ""
                if seller_of(u) != dom.replace("www.", ""):
                    # require domain match
                    if dom not in u.lower():
                        continue
                _add(u, "SITE_BING")

    # Validate identity quickly; keep matches first
    validated: list[dict[str, Any]] = []
    for row in out[:12]:
        v = validate_product_page(
            row["url"],
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=False,
        )
        if v.get("identity_match"):
            row["title"] = v.get("title")
            validated.append(row)
        elif len(validated) < 2:
            v2 = validate_product_page(
                row["url"],
                mpn=mpn,
                manufacturer=mfr,
                description=item.get("description"),
                category=item.get("category"),
                allow_browser=True,
            )
            if v2.get("identity_match"):
                row["title"] = v2.get("title")
                validated.append(row)
    # Only return identity-validated live PDPs (never unvalidated suggest hits)
    return validated[:limit]
