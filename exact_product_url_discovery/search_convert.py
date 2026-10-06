"""Convert internal seller search pages into product detail hrefs."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.models import (
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    METHOD_INTERNAL_SEARCH,
)
from exact_product_url_discovery.normalize import is_short_mpn, mpn_in_text, mpn_variants
from public_price_search.search import fetch_page


# Domain → search URL templates (query inserted). Conversion extracts product links.
_SEARCH_TEMPLATES: dict[str, list[str]] = {
    "platt.com": ["https://www.platt.com/search?q={q}"],
    "quill.com": ["https://www.quill.com/search?keywords={q}"],
    "1000bulbs.com": ["https://www.1000bulbs.com/search?q={q}"],
    "globalindustrial.com": ["https://www.globalindustrial.com/search?q={q}"],
    "rspsupply.com": ["https://rspsupply.com/search.aspx?SearchTerm={q}"],
    "parts-hvac.com": ["https://parts-hvac.com/search?q={q}"],
    "dieselpartsdirect.com": ["https://www.dieselpartsdirect.com/search?q={q}"],
    "toolbarn.com": ["https://www.toolbarn.com/catalogsearch/result/?q={q}"],
    "mccoys.com": ["https://www.mccoys.com/search?q={q}"],
    "filtersfast.com": ["https://www.filtersfast.com/Search.asp?Search={q}"],
    "seton.com": ["https://www.seton.com/catalogsearch/result/?q={q}"],
    "carid.com": ["https://www.carid.com/search/?keyword={q}"],
    "bulbs.com": ["https://www.bulbs.com/search?q={q}"],
    "leestools.com": ["https://leestools.com/search?q={q}"],
    "standardelectricsupply.com": ["https://www.standardelectricsupply.com/search?q={q}"],
    "northernsafety.com": ["https://www.northernsafety.com/Product/Search?q={q}"],
    "channellock.com": ["https://www.channellock.com/search?q={q}"],
    "makitatools.com": ["https://www.makitatools.com/search?q={q}"],
    "loctiteproducts.com": ["https://www.loctiteproducts.com/search?q={q}"],
    "megadepot.com": ["https://megadepot.com/?s={q}"],
    "homelectrical.com": ["https://www.homelectrical.com/search?q={q}"],
    "activeplumbing.com": ["https://www.activeplumbing.com/buy/search/?q={q}"],
    "gsistore.com": ["https://www.gsistore.com/search?q={q}"],
}


def _hrefs_from_html(html: str, *, base_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or "", re.I | re.S):
        href = m.group(1)
        text = re.sub(r"<[^>]+>", " ", m.group(2) or "")
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith("http"):
            continue
        out.append({"url": abs_url, "text": text[:160]})
    # also canonical / og:url
    for pat in (
        r'rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            out.append({"url": urljoin(base_url, m.group(1)), "text": "canonical"})
    return out


def convert_internal_search(
    domain: str,
    *,
    mpn: str,
    manufacturer: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Fetch seller search shell → extract product detail hrefs. Never return the search URL."""
    host = (domain or "").lower().replace("www.", "")
    templates = _SEARCH_TEMPLATES.get(host) or [f"https://www.{host}/search?q={{q}}"]
    queries = [mpn]
    if manufacturer:
        queries.insert(0, f"{manufacturer} {mpn}")
    for v in mpn_variants(mpn)[:2]:
        if v not in queries:
            queries.append(v)

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in queries[:3]:
        for tmpl in templates[:1]:
            search_url = tmpl.format(q=quote_plus(q))
            fr = fetch_page(search_url)
            if not fr.get("ok"):
                continue
            html = fr.get("text") or ""
            for row in _hrefs_from_html(html, base_url=str(fr.get("url") or search_url)):
                url = row["url"]
                if url in seen or is_search_shell(url):
                    continue
                low = url.lower()
                if any(
                    x in low
                    for x in (
                        "/register",
                        "/login",
                        "/signin",
                        "/account",
                        "/cart",
                        "/checkout",
                        "returnurl=",
                    )
                ):
                    continue
                # same host preferred
                uh = urlparse(url).netloc.lower().replace("www.", "")
                if uh and host not in uh and uh not in host:
                    continue
                cls = classify_candidate_url(url, mpn=mpn)
                if cls not in {EXACT_PRODUCT_VERIFIED, EXACT_PRODUCT_UNVERIFIED}:
                    continue
                # short MPN: require manufacturer in link text/url
                if is_short_mpn(mpn) and manufacturer:
                    blob = f"{row.get('text') or ''} {url}".lower()
                    mfr0 = manufacturer.split()[0].lower()
                    if mfr0 not in blob and not mpn_in_text(mpn, url):
                        continue
                if not mpn_in_text(mpn, url, row.get("text") or "") and cls != EXACT_PRODUCT_VERIFIED:
                    continue
                seen.add(url)
                found.append(
                    {
                        "url": url,
                        "domain": uh or host,
                        "url_class": cls,
                        "discovery_method": METHOD_INTERNAL_SEARCH,
                        "from_search_url": search_url,
                        "anchor_text": (row.get("text") or "")[:120],
                        "confidence": 0.75 if cls == EXACT_PRODUCT_VERIFIED else 0.5,
                    }
                )
                if len(found) >= limit:
                    return found
    return found
