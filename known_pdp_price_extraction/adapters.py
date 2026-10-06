"""Seller-specific price adapters for known exact PDPs."""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from public_price_search.extract import _f
from price_adapters.validate import mpn_in_blob, seller_of


def _walk_prices(obj: Any, *, mpn: str, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 8 or len(out) >= 20:
        return
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").lower()
    if isinstance(obj, dict):
        blob = " ".join(str(obj.get(k) or "") for k in ("sku", "mpn", "product_id", "id", "name", "title", "handle", "vendor"))
        has_mpn = bool(tok) and tok in re.sub(r"[^a-z0-9]", "", blob.lower())
        for key in (
            "price",
            "sale_price",
            "salePrice",
            "regular_price",
            "regularPrice",
            "currentPrice",
            "productPrice",
            "unit_price",
            "unitPrice",
            "price_min",
            "priceMin",
            "amount",
        ):
            if key in obj:
                raw = obj.get(key)
                # Shopify often stores cents as int
                if isinstance(raw, (int, float)) and raw > 100 and key in {"price", "amount"} and "currency" not in str(obj.get("currency") or "").lower():
                    # could be cents
                    price = float(raw) / 100.0 if raw > 999 else float(raw)
                else:
                    price = _f(str(raw))
                if price and price >= 1.51:
                    out.append(
                        {
                            "unit_price": price,
                            "name": str(obj.get("name") or obj.get("title") or "")[:160],
                            "sku": str(obj.get("sku") or obj.get("mpn") or ""),
                            "via": f"adapter_walk:{key}",
                            "mpn_bound": has_mpn,
                        }
                    )
        # Shopify variants
        for v in obj.get("variants") or []:
            _walk_prices(v, mpn=mpn, out=out, depth=depth + 1)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_prices(v, mpn=mpn, out=out, depth=depth + 1)
    elif isinstance(obj, list):
        for it in obj[:60]:
            _walk_prices(it, mpn=mpn, out=out, depth=depth + 1)


def extract_shopify_signals(html: str, url: str, *, mpn: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "shopify" not in (html or "").lower() and "cdn.shopify.com" not in (html or "").lower():
        return out
    # product JSON in page
    for m in re.finditer(r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>", html or "", re.I | re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        _walk_prices(data, mpn=mpn, out=out)
    # __NEXT not shopify but keep
    m = re.search(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", html or "", re.I | re.S)
    if m:
        try:
            _walk_prices(json.loads(m.group(1)), mpn=mpn, out=out)
        except Exception:
            pass
    return out


def shopify_product_js_urls(url: str, html: str = "") -> list[str]:
    """Derive public Shopify product.js endpoints from PDP URL/html."""
    out: list[str] = []
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    # /products/handle → /products/handle.js
    if "/products/" in path or "/product/" in path:
        handle = path.split("/")[-1].split("?")[0]
        if handle:
            if "/products/" in path:
                out.append(f"{origin}/products/{handle}.js")
            # also try products/ form for /product/
            out.append(f"{origin}/products/{handle}.js")
    # look for product handle in html
    for m in re.finditer(r"/products/([a-z0-9\-]+)\.js", html or "", re.I):
        out.append(urljoin(origin, f"/products/{m.group(1)}.js"))
    for m in re.finditer(r'"handle"\s*:\s*"([a-z0-9\-]+)"', html or "", re.I):
        out.append(f"{origin}/products/{m.group(1)}.js")
    # dedupe
    seen: set[str] = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:4]


def extract_dom_price_selectors(html: str, text: str, *, mpn: str) -> list[dict[str, Any]]:
    """Last-resort DOM/meta price patterns after render."""
    out: list[dict[str, Any]] = []
    blob = html or ""
    patterns = [
        (r'data-price-amount=["\']([\d.]+)["\']', "data-price-amount"),
        (r'itemprop=["\']price["\'][^>]*content=["\']([\d.]+)["\']', "itemprop-content"),
        (r'content=["\']([\d.]+)["\'][^>]*itemprop=["\']price["\']', "itemprop-content-rev"),
        (r'"price"\s*:\s*"?([\d]+\.[\d]{2})"?', "json-price"),
        (r'class=["\'][^"\']*price[^"\']*["\'][^>]*>\s*\$?\s*([\d,]+\.\d{2})', "css-price"),
        (r'id=["\'](?:price|product-price|productPrice)[^"\']*["\'][^>]*>\s*\$?\s*([\d,]+\.\d{2})', "id-price"),
    ]
    for pat, via in patterns:
        for m in re.finditer(pat, blob[:500000], re.I):
            price = _f(m.group(1).replace(",", ""))
            if price and price >= 1.51:
                out.append({"unit_price": price, "via": f"dom:{via}", "name": "", "sku": ""})
            if len(out) >= 12:
                break
    # Visible dollars near MPN in text
    if mpn and text:
        tok = re.sub(r"[^A-Za-z0-9]", "", mpn).lower()
        tlow = text.lower()
        if tok and tok in re.sub(r"[^a-z0-9]", "", tlow):
            for m in re.finditer(r"\$\s*(\d{1,5}\.\d{2})", text[:30000]):
                price = _f(m.group(1))
                if price and price >= 1.51:
                    out.append({"unit_price": price, "via": "text_dollar_near_mpn", "name": "", "sku": ""})
                    break
    return out


def extract_shopify_product_js(data: dict[str, Any], *, mpn: str) -> list[dict[str, Any]]:
    """Parse Shopify /products/{handle}.js — prices are integer cents."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    title = str(data.get("title") or "")
    handle = str(data.get("handle") or "")
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").lower()
    blob = f"{title} {handle} {json.dumps(data)[:800]}"
    if tok and tok not in re.sub(r"[^a-z0-9]", "", blob.lower()):
        # also check variant skus
        skus = " ".join(str(v.get("sku") or "") for v in (data.get("variants") or []) if isinstance(v, dict))
        if tok not in re.sub(r"[^a-z0-9]", "", skus.lower()):
            return out
    for v in data.get("variants") or []:
        if not isinstance(v, dict):
            continue
        raw = v.get("price")
        try:
            cents = float(raw)
        except Exception:
            continue
        # Shopify .js prices are cents
        price = cents / 100.0 if cents >= 100 else cents
        if price < 1.51:
            continue
        out.append(
            {
                "unit_price": price,
                "name": title[:160],
                "sku": str(v.get("sku") or ""),
                "via": "shopify_product_js_cents",
                "condition": "NEW",
            }
        )
        break
    return out


def shopify_suggest_products_filtered(domain: str, mfr: str, mpn: str, *, limit: int = 5) -> list[str]:
    """Shopify suggest filtered to results mentioning MPN."""
    from urllib.parse import quote_plus
    from public_price_search.search import fetch_page

    q = f"{mfr} {mpn}".strip()
    urls: list[str] = []
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").lower()
    path = (
        f"https://www.{domain}/search/suggest.json?q={quote_plus(q)}"
        f"&resources[type]=product&resources[limit]=10"
    )
    fr = fetch_page(path, use_budget=True)
    try:
        data = json.loads(fr.get("text") or "{}")
    except Exception:
        return []
    products = ((data.get("resources") or {}).get("results") or {}).get("products") or []
    for p in products:
        if not isinstance(p, dict):
            continue
        handle = str(p.get("handle") or "")
        title = str(p.get("title") or "")
        blob = re.sub(r"[^a-z0-9]", "", f"{handle} {title} {p.get('sku') or ''}".lower())
        if tok and tok not in blob:
            continue
        if handle:
            urls.append(f"https://www.{domain}/products/{handle}")
        if len(urls) >= limit:
            break
    return urls


def detect_shopify(html: str) -> bool:
    h = (html or "").lower()
    return "cdn.shopify.com" in h or "shopify.theme" in h or "shopify-section" in h


AdapterFn = Callable[[str, str, dict[str, Any]], list[dict[str, Any]]]


def adapter_lightbulbs(html: str, url: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    mpn = str(item.get("mpn") or "")
    cands = extract_shopify_signals(html, url, mpn=mpn)
    cands.extend(extract_dom_price_selectors(html, "", mpn=mpn))
    return cands


def adapter_generic_shopify(html: str, url: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    if not detect_shopify(html):
        return []
    return extract_shopify_signals(html, url, mpn=str(item.get("mpn") or ""))


def adapter_magento_like(html: str, url: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    """AcmeTools / Magento-style data-price-amount."""
    return extract_dom_price_selectors(html, "", mpn=str(item.get("mpn") or ""))


def adapter_1000bulbs(html: str, url: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    out = extract_dom_price_selectors(html, "", mpn=str(item.get("mpn") or ""))
    # 1000bulbs often has JSON product
    for m in re.finditer(r"var\s+product\s*=\s*(\{.*?\});", html or "", re.I | re.S):
        try:
            data = json.loads(m.group(1))
            _walk_prices(data, mpn=str(item.get("mpn") or ""), out=out)
        except Exception:
            pass
    return out


DOMAIN_ADAPTERS: dict[str, AdapterFn] = {
    "lightbulbs.com": adapter_lightbulbs,
    "crcautocare.com": adapter_generic_shopify,
    "gorillatough.com": adapter_generic_shopify,
    "jbweld.com": adapter_generic_shopify,
    "pksafety.com": adapter_generic_shopify,
    "acmetools.com": adapter_magento_like,
    "toolbarn.com": adapter_magento_like,
    "1000bulbs.com": adapter_1000bulbs,
    "seton.com": adapter_magento_like,
    "northernsafety.com": adapter_magento_like,
    "zoro.com": adapter_magento_like,
    "plumbingsupply.com": adapter_magento_like,
    "activeplumbing.com": adapter_magento_like,
    "rshughes.com": adapter_magento_like,
    "autozone.com": adapter_magento_like,
}


def run_domain_adapter(url: str, html: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    dom = seller_of(url)
    fn = DOMAIN_ADAPTERS.get(dom)
    if not fn:
        # try generic shopify
        return adapter_generic_shopify(html, url, item)
    return fn(html, url, item) or []
