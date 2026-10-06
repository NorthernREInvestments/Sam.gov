"""Direct catalog intelligence — structured/hydration price recovery.

Build: 20261004-m3-price-search-reliability-v1

Prefer JSON-LD / embedded product JSON over DOM scraping.
Bounded browser render only as last-resort fallback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from public_price_search.circuits import domain_of, is_open, note
from public_price_search.extract import (
    _f,
    detect_condition,
    identity_on_page,
    identity_tokens,
)

log = logging.getLogger("govtracker.public_price_search.catalog_intel")

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
}


def _walk_prices(node: Any, out: list[dict[str, Any]], *, depth: int = 0) -> None:
    if depth > 12 or node is None:
        return
    if isinstance(node, dict):
        price = None
        for k in (
            "price",
            "lowPrice",
            "highPrice",
            "productPrice",
            "unitPrice",
            "listPrice",
            "salePrice",
            "amount",
            "value",
            "priceRange",
        ):
            if k in node and node[k] is not None:
                if k == "priceRange" and isinstance(node[k], dict):
                    price = _f(str(node[k].get("min") or node[k].get("max") or ""))
                else:
                    price = _f(str(node[k]))
                if price:
                    break
        currency = str(node.get("priceCurrency") or node.get("currency") or "USD").upper()
        availability = str(node.get("availability") or node.get("stock") or "")
        if price and currency in {"USD", "US", ""}:
            out.append(
                {
                    "price": price,
                    "currency": currency or "USD",
                    "availability": availability,
                    "condition_hint": str(node.get("itemCondition") or node.get("condition") or ""),
                }
            )
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_prices(v, out, depth=depth + 1)
    elif isinstance(node, list):
        for item in node[:40]:
            _walk_prices(item, out, depth=depth + 1)


def extract_structured_prices(html: str) -> list[dict[str, Any]]:
    """JSON-LD, schema.org Offer, hydration blobs, data-* price attrs."""
    found: list[dict[str, Any]] = []
    html = html or ""

    # JSON-LD blocks
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _walk_prices(data, found)

    # Next.js / hydration __NEXT_DATA__
    for m in re.finditer(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        _walk_prices(data, found)

    # Shopify / generic product JSON in script
    for m in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        if "price" not in raw.lower() and "offer" not in raw.lower():
            continue
        if len(raw) > 400_000:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _walk_prices(data, found)

    # Inline JS assignments
    for m in re.finditer(
        r'(?:productPrice|unitPrice|listPrice|salePrice|product_price|item_price)'
        r'\s*[:=]\s*["\']?(\d+(?:\.\d{1,2})?)',
        html,
        re.I,
    ):
        v = _f(m.group(1))
        if v:
            found.append({"price": v, "currency": "USD", "availability": "", "condition_hint": ""})

    # data-* attributes
    for m in re.finditer(
        r'data-(?:price|product-price|unit-price|list-price)=["\'](\d+(?:\.\d{1,2})?)["\']',
        html,
        re.I,
    ):
        v = _f(m.group(1))
        if v:
            found.append({"price": v, "currency": "USD", "availability": "", "condition_hint": ""})

    # meta itemprop
    for m in re.finditer(
        r'itemprop=["\']price["\'][^>]*content=["\'](\d+(?:\.\d{1,2})?)["\']',
        html,
        re.I,
    ):
        v = _f(m.group(1))
        if v:
            found.append({"price": v, "currency": "USD", "availability": "", "condition_hint": ""})

    # Dedup
    out: list[dict[str, Any]] = []
    seen: set[float] = set()
    for row in found:
        p = row.get("price")
        if p is None or p in seen:
            continue
        seen.add(float(p))
        out.append(row)
    return out[:20]


def extract_api_hints(html: str, *, page_url: str) -> list[str]:
    """Find likely product/price API endpoints referenced in page source."""
    urls: list[str] = []
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    patterns = [
        r'["\']((?:https?:)?//[^"\']*(?:product|price|offer|variant|graphql)[^"\']*)["\']',
        r'["\'](/api/[^"\']*(?:product|price|offer|variant)[^"\']*)["\']',
        r'["\'](/products\.json[^"\']*)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html or "", re.I):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                u = urljoin(base, u)
            if u.startswith("http") and u not in urls:
                urls.append(u)
            if len(urls) >= 6:
                return urls
    return urls


def fetch_structured_api(
    url: str,
    *,
    client: httpx.Client | None = None,
    identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch a public product JSON endpoint and walk prices."""
    route = "STRUCTURED_PRODUCT_DATA"
    dom = domain_of(url)
    if is_open(route, domain=dom):
        return []
    own = client is None
    c = client or httpx.Client(timeout=6.0, follow_redirects=True, headers=_UA)
    try:
        r = c.get(url)
        if r.status_code >= 400:
            note(route, ok=False, domain=dom, reason=f"HTTP_{r.status_code}")
            return []
        ct = (r.headers.get("content-type") or "").lower()
        text = r.text or ""
        if "json" not in ct and not text.strip().startswith(("{", "[")):
            note(route, ok=False, domain=dom, reason="NOT_JSON")
            return []
        try:
            data = r.json()
        except Exception:
            note(route, ok=False, domain=dom, reason="JSON_PARSE")
            return []
        if identity and not identity_on_page(json.dumps(data)[:200_000], identity):
            # Still allow prices if MPN in URL
            toks = identity_tokens(identity or {})
            if not any(t.lower() in url.lower() for t in toks):
                return []
        found: list[dict[str, Any]] = []
        _walk_prices(data, found)
        note(route, ok=bool(found), domain=dom, reason=None if found else "NO_PRICE")
        return found
    except Exception as exc:
        note(route, ok=False, domain=dom, reason=type(exc).__name__)
        return []
    finally:
        if own:
            c.close()


def try_sitemap_product_urls(
    domain: str,
    identity: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    limit: int = 5,
) -> list[str]:
    """Locate product URLs via public sitemap when SERP is down."""
    route = "PUBLIC_CATALOG_PDF"
    if is_open(route, domain=domain):
        return []
    toks = identity_tokens(identity)
    if not toks:
        return []
    own = client is None
    c = client or httpx.Client(timeout=6.0, follow_redirects=True, headers=_UA)
    out: list[str] = []
    try:
        for path in ("/sitemap.xml", "/sitemap_products_1.xml", "/product-sitemap.xml"):
            url = f"https://{domain}{path}"
            try:
                r = c.get(url)
            except Exception:
                continue
            if r.status_code >= 400 or not r.text:
                continue
            text = r.text
            for m in re.finditer(r"<loc>\s*([^<]+)\s*</loc>", text, re.I):
                loc = m.group(1).strip()
                norm = re.sub(r"[^A-Z0-9]", "", loc.upper())
                if any(t in norm for t in toks if len(t) >= 5):
                    if loc not in out:
                        out.append(loc)
                if len(out) >= limit:
                    break
            if out:
                break
        note(route, ok=bool(out), domain=domain, reason=None if out else "NO_SITEMAP_HIT")
    finally:
        if own:
            c.close()
    return out[:limit]


def browser_render_price(
    url: str,
    identity: dict[str, Any],
    *,
    timeout_ms: int = 8000,
) -> dict[str, Any] | None:
    """Bounded Playwright fallback — only when static HTML lacks price."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    toks = identity_tokens(identity)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
                html = page.content()
                text = page.inner_text("body")[:80_000]
            finally:
                browser.close()
    except Exception as exc:
        log.debug("browser_render failed: %s", type(exc).__name__)
        return None

    blob = html + "\n" + text
    if toks and not identity_on_page(blob, identity):
        return None
    structured = extract_structured_prices(html)
    price = None
    if structured:
        price = structured[0]["price"]
    else:
        m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", text)
        if m:
            price = _f(m.group(1))
    if not price:
        return None
    return {
        "status": "PRICE_OK",
        "url": url,
        "unit_price": price,
        "displayed_price": price,
        "condition": detect_condition(blob),
        "via": "js_render",
        "seller": urlparse(url).netloc,
        "exact_match": True,
        "all_prices_seen": [s["price"] for s in structured[:6]] or [price],
    }
