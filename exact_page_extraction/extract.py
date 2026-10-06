"""Deep exact-page price extraction (static → structured → hydration → API → browser).

Build: 20261004-m3-exact-page-price-extraction-v1
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from exact_page_extraction.domain_memory import note_extraction, preferred_routes
from exact_page_extraction.models import (
    PLACEHOLDER_10_58,
    ROUTE_API,
    ROUTE_BROWSER,
    ROUTE_CART,
    ROUTE_GRAPHQL,
    ROUTE_HYDRATION,
    ROUTE_JSONLD,
    ROUTE_STATIC,
)
from exact_page_extraction.url_classify import classify_url
from price_adapters.browser import bounded_browser_fetch
from price_adapters.orchestrate import _identity
from price_adapters.validate import (
    extract_jsonld_exact,
    extract_near_mpn_price,
    mpn_in_blob,
    seller_of,
    validate_candidate,
)
from public_price_search.catalog_intel import extract_structured_prices
from public_price_search.extract import _f, detect_condition
from public_price_search.search import fetch_page


def _structured_prices_bound_to_mpn(html: str, *, mpn: str, page_url: str) -> list[dict[str, Any]]:
    """Return structured prices only when Offer/Product context contains the MPN."""
    out: list[dict[str, Any]] = []
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").lower()
    if len(tok) < 4:
        return out
    page_tok = re.sub(r"[^A-Za-z0-9]", "", page_url or "").lower()

    # Walk JSON-LD Offer blocks with surrounding Product context
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                name = str(node.get("name") or "")
                sku = str(node.get("sku") or node.get("mpn") or node.get("productID") or "")
                offers = node.get("offers")
                offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
                for off in offer_list:
                    if not isinstance(off, dict):
                        continue
                    price = _f(str(off.get("price") or ""))
                    if not price or price < 1.51:
                        continue
                    offer_url = str(off.get("url") or "")
                    blob = f"{name} {sku} {offer_url}"
                    blob_tok = re.sub(r"[^A-Za-z0-9]", "", blob).lower()
                    if tok in blob_tok or (tok in page_tok and tok in re.sub(r"[^A-Za-z0-9]", "", offer_url).lower()):
                        out.append(
                            {
                                "price": price,
                                "condition_hint": str(off.get("itemCondition") or name),
                                "name": name[:160],
                                "sku": sku,
                            }
                        )
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node[:80])

    # Fallback: price JSON objects that also mention MPN within a tight window
    for m in re.finditer(
        r'\{[^{}]{0,400}"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?[^{}]{0,400}\}',
        html or "",
        re.I,
    ):
        block = m.group(0)
        if tok not in re.sub(r"[^A-Za-z0-9]", "", block).lower():
            continue
        price = _f(m.group("p"))
        if price and price >= 1.51:
            out.append({"price": price, "condition_hint": block[:80], "name": mpn, "sku": mpn})
    return out


def _is_placeholder(price: float | None, url: str) -> bool:
    if price is None:
        return False
    u = (url or "").lower()
    # Permanent regression: ND search-shell $10.58 only (real /product/ pages allowed)
    if abs(float(price) - PLACEHOLDER_10_58) < 0.001 and "nationaldistributorllc" in u:
        if "/product/" not in u or "?s=" in u or "/?s=" in u:
            return True
        # product page with $10.58 alone is still suspicious if also search-like
        if re.search(r"nationaldistributorllc\.com/?(\?|$)", u):
            return True
    if float(price) in {5.0, 8.0, 25.0} and "globalindustrial" in u:
        return True
    return False


def _meta_prices(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    patterns = [
        (r'itemprop=["\']price["\'][^>]*content=["\']([\d.]+)["\']', "itemprop_price"),
        (r'content=["\']([\d.]+)["\'][^>]*itemprop=["\']price["\']', "itemprop_price"),
        (r'property=["\']product:price:amount["\'][^>]*content=["\']([\d.]+)["\']', "og_price"),
        (r'property=["\']og:price:amount["\'][^>]*content=["\']([\d.]+)["\']', "og_price"),
        (r'name=["\']twitter:data1["\'][^>]*content=["\']\$?([\d.]+)["\']', "twitter_price"),
        (r'data-price=["\']([\d.]+)["\']', "data_price"),
        (r'data-product-price=["\']([\d.]+)["\']', "data_product_price"),
        (r'"regularPrice"\s*:\s*"?([\d.]+)"?', "regularPrice"),
        (r'"salePrice"\s*:\s*"?([\d.]+)"?', "salePrice"),
        (r'"currentPrice"\s*:\s*"?([\d.]+)"?', "currentPrice"),
        (r'"variantPrice"\s*:\s*"?([\d.]+)"?', "variantPrice"),
        (r'"unitPrice"\s*:\s*"?([\d.]+)"?', "unitPrice"),
        (r'"productPrice"\s*:\s*"?([\d.]+)"?', "productPrice"),
    ]
    for pat, via in patterns:
        for m in re.finditer(pat, html or "", re.I):
            price = _f(m.group(1))
            if price and price >= 1.51:
                out.append({"unit_price": price, "via": via, "condition": "NEW"})
    return out


def _hydration_candidates(html: str, *, mpn: str, manufacturer: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    blobs: list[tuple[str, Any]] = []

    for m in re.finditer(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            blobs.append(("__NEXT_DATA__", json.loads(m.group(1))))
        except Exception:
            pass

    for m in re.finditer(
        r'window\.(?:__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__)\s*=\s*(\{.*?\});',
        html or "",
        re.I | re.S,
    ):
        raw = m.group(1)
        if len(raw) > 500_000:
            continue
        try:
            blobs.append(("initial_state", json.loads(raw)))
        except Exception:
            pass

    for m in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        if "price" not in raw.lower():
            continue
        if len(raw) > 400_000:
            continue
        try:
            blobs.append(("app_json", json.loads(raw)))
        except Exception:
            pass

    # dataLayer ecommerce
    for m in re.finditer(r"dataLayer\s*=\s*(\[.*?\]);", html or "", re.I | re.S):
        try:
            blobs.append(("dataLayer", json.loads(m.group(1))))
        except Exception:
            pass

    mpn_tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").upper()

    def _node_mpn_hit(node: dict[str, Any], path: str) -> bool:
        name = str(node.get("name") or node.get("title") or node.get("productName") or "")
        sku = str(
            node.get("sku")
            or node.get("mpn")
            or node.get("productID")
            or node.get("id")
            or node.get("itemNumber")
            or node.get("manufacturerPartNumber")
            or node.get("manufacturerPartNo")
            or node.get("partNumber")
            or node.get("modelNumber")
            or node.get("productNumber")
            or ""
        )
        # Global Industrial etc. often nest codes under attributes
        for k in ("itemNumber", "manufacturerPartNumber", "partNumber", "model", "code"):
            if node.get(k) is not None and not sku:
                sku = str(node.get(k))
        if mpn_in_blob(mpn, name, sku, path):
            return True
        # Loose: normalized equality on short codes
        if mpn_tok and len(mpn_tok) >= 4:
            for part in (name, sku):
                if re.sub(r"[^A-Za-z0-9]", "", part or "").upper() == mpn_tok:
                    return True
        return False

    def _price_from_node(node: dict[str, Any]) -> float | None:
        for k in (
            "price",
            "salePrice",
            "regularPrice",
            "unitPrice",
            "productPrice",
            "currentPrice",
            "amount",
            "value",
            "priceCatalog",
            "priceOriginal",
            "priceSale",
            "minSalePrice",
            "priceMsr",
        ):
            if node.get(k) is None:
                continue
            if isinstance(node[k], dict):
                price = _f(str(node[k].get("value") or node[k].get("amount") or node[k].get("min") or ""))
            else:
                price = _f(str(node[k]))
            if price and price >= 1.51:
                # Prefer sale when present and > 0
                if k in {"priceSale", "minSalePrice"} and price < 1.51:
                    continue
                return float(price)
        # priceBreaks[0]
        breaks = node.get("priceBreaks")
        if isinstance(breaks, list) and breaks:
            b0 = breaks[0] or {}
            for k in ("priceCatalog", "priceOriginal", "price"):
                price = _f(str(b0.get(k) or ""))
                if price and price >= 1.51:
                    return float(price)
        offers = node.get("offers")
        if isinstance(offers, dict):
            price = _f(str(offers.get("price") or ""))
            if price and price >= 1.51:
                return float(price)
        if isinstance(offers, list) and offers:
            price = _f(str((offers[0] or {}).get("price") or ""))
            if price and price >= 1.51:
                return float(price)
        return None

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            if _node_mpn_hit(node, path):
                price = _price_from_node(node)
                if price:
                    name = str(node.get("name") or node.get("title") or node.get("productName") or "")
                    sku = str(node.get("sku") or node.get("mpn") or node.get("itemNumber") or node.get("manufacturerPartNumber") or "")
                    out.append(
                        {
                            "unit_price": price,
                            "name": name[:160],
                            "sku": sku,
                            "condition": detect_condition(f"{name} {sku}"),
                            "via": "hydration_mpn",
                        }
                    )
            for k, v in list(node.items())[:100]:
                if isinstance(v, (dict, list)):
                    walk(v, path + "/" + str(k)[:40])
        elif isinstance(node, list):
            for item in node[:80]:
                walk(item, path)

    for label, data in blobs:
        before = len(out)
        walk(data, label)
        for c in out[before:]:
            c["via"] = f"hydration:{label}"
    return out


def _discover_api_urls(html: str, page_url: str) -> list[str]:
    """Heuristic public product/price endpoint URLs from page source."""
    found: list[str] = []
    host = urlparse(page_url).scheme + "://" + urlparse(page_url).netloc
    patterns = [
        r'["\']((?:https?:)?//[^"\']*(?:/api/|/graphql|/product|/products|/pricing|/price|/catalog)[^"\']*)["\']',
        r'["\'](/(?:api|v1|v2)/[^"\']*(?:product|price|variant|sku)[^"\']*)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html or "", re.I):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                u = urljoin(host, u)
            if u.startswith("http") and u not in found:
                found.append(u)
            if len(found) >= 6:
                return found
    # Domain-specific known public patterns
    host_l = urlparse(page_url).netloc.lower().replace("www.", "")
    path = urlparse(page_url).path
    if "quill.com" in host_l:
        # Quill sometimes exposes product JSON near cbs paths — keep page only
        pass
    if "dieselpartsdirect.com" in host_l:
        slug = path.strip("/").split("/")[0]
        if slug:
            found.append(f"https://www.dieselpartsdirect.com/{slug}.js")
    if "shopify" in (html or "").lower() or "cdn.shopify.com" in (html or "").lower():
        # Shopify product.js
        if path.rstrip("/"):
            found.append(urljoin(page_url.rstrip("/") + "/", ".js"))
            found.append(page_url.rstrip("/") + ".js")
    return found[:6]


def _extract_from_api_json(data: Any, *, mpn: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("title") or "")
            sku = str(node.get("sku") or node.get("mpn") or node.get("product_id") or "")
            price = None
            for k in ("price", "salePrice", "regularPrice", "unitPrice", "productPrice", "amount", "variants"):
                if k == "variants" and isinstance(node.get(k), list) and node[k]:
                    v0 = node[k][0] or {}
                    price = _f(str(v0.get("price") or ""))
                    if price and price > 50:  # shopify cents
                        if price > 1000 and price == int(price):
                            price = price / 100.0
                elif node.get(k) is not None and k != "variants":
                    price = _f(str(node[k]))
                    # Shopify often stores cents as integer
                    if price and price >= 100 and "shopify" in str(node.get("vendor") or "").lower():
                        if price == int(price) and price >= 151:
                            price = price / 100.0
                    if price:
                        break
            if price and price >= 1.51 and (mpn_in_blob(mpn, name, sku) or mpn_in_blob(mpn, json.dumps(node)[:500])):
                out.append(
                    {
                        "unit_price": float(price) if price < 100000 else float(price) / 100.0,
                        "name": name[:160],
                        "sku": sku,
                        "condition": "NEW",
                        "via": "api_json",
                    }
                )
            for v in list(node.values())[:50]:
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node[:40]:
                walk(item)

    walk(data)
    # Fix shopify cent prices: if all prices look like cents for low-cost items
    fixed = []
    for c in out:
        p = float(c["unit_price"])
        if p >= 151 and p == int(p) and p < 100000 and "api_json" in c["via"]:
            # heuristic: if mpn page likely <$200, divide
            if p > 500:
                c = {**c, "unit_price": p / 100.0, "via": c["via"] + "+cents"}
        fixed.append(c)
    return fixed


def _try_validate(identity: dict[str, Any], cand: dict[str, Any], url: str) -> dict[str, Any] | None:
    if not cand or cand.get("unit_price") is None:
        return None
    if _is_placeholder(float(cand["unit_price"]), url):
        return None
    cand = {
        **cand,
        "url": url,
        "seller": seller_of(url),
        "displayed_price": cand.get("unit_price"),
        "landed_estimate": cand.get("unit_price"),
        "freight_status": "FREIGHT_UNKNOWN",
    }
    v = validate_candidate(identity, cand, expected_condition=str(identity.get("expected_condition") or "NEW"))
    if not v.get("ok"):
        return None
    return cand


def extract_from_html(
    html: str,
    *,
    url: str,
    identity: dict[str, Any],
    route_prefix: str = "static",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Return (best_validated, all_raw_candidates, route_used)."""
    mpn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer")
    raw: list[dict[str, Any]] = []
    cls = classify_url(url, mpn=mpn)
    from exact_page_extraction.models import SEARCH_RESULT_SHELL

    if cls == SEARCH_RESULT_SHELL:
        return None, [], SEARCH_RESULT_SHELL

    # 1 JSON-LD exact
    for c in extract_jsonld_exact(html, mpn=mpn, manufacturer=mfr):
        raw.append({**c, "route": ROUTE_JSONLD})
        ok = _try_validate(identity, {**c, "via": route_prefix + "+jsonld"}, url)
        if ok:
            return ok, raw, ROUTE_JSONLD

    # 2 Hydration
    for c in _hydration_candidates(html, mpn=mpn, manufacturer=mfr):
        raw.append({**c, "route": ROUTE_HYDRATION})
        ok = _try_validate(identity, {**c, "via": route_prefix + "+" + c.get("via", "hydration")}, url)
        if ok:
            return ok, raw, ROUTE_HYDRATION

    # Exact product URL already embeds MPN in path — useful signal but NOT sufficient
    # to accept arbitrary page prices (cross-sell / related-offer contamination).
    from exact_page_extraction.models import EXACT_PRODUCT_VERIFIED

    url_is_exact = cls == EXACT_PRODUCT_VERIFIED
    html_has_mpn = mpn_in_blob(mpn, html[:80000] if html else "") or url_is_exact

    # 3 Meta / static structured — meta tags are page-primary when MPN is on page/URL
    for c in _meta_prices(html):
        raw.append({**c, "route": ROUTE_STATIC})
        if not html_has_mpn and not mpn_in_blob(mpn, html[:8000] if html else ""):
            continue
        ok = _try_validate(
            identity,
            {**c, "name": identity.get("raw_description") or mpn, "sku": mpn, "via": route_prefix + "+" + c["via"]},
            url,
        )
        if ok:
            return ok, raw, ROUTE_STATIC

    # Structured catalog prices: ONLY accept when offer/product context binds to MPN.
    # Never take the first orphan Offer on a multi-product shell.
    for row in _structured_prices_bound_to_mpn(html, mpn=mpn, page_url=url):
        price = row.get("price")
        if not price or float(price) < 1.51:
            continue
        c = {
            "unit_price": float(price),
            "condition": detect_condition(str(row.get("condition_hint") or "")),
            "via": "structured_mpn_bound",
            "name": row.get("name") or mpn,
            "sku": row.get("sku") or mpn,
            "route": ROUTE_STATIC,
        }
        raw.append(c)
        ok = _try_validate(identity, {**c, "via": route_prefix + "+structured_bound"}, url)
        if ok:
            return ok, raw, ROUTE_STATIC

    # 4 Near-MPN
    near = extract_near_mpn_price(html, mpn=mpn, manufacturer=mfr)
    if near and near.get("via") != "near_mpn_dollar":
        raw.append({**near, "route": ROUTE_STATIC})
        ok = _try_validate(identity, {**near, "via": route_prefix + "+" + near.get("via", "near")}, url)
        if ok:
            return ok, raw, ROUTE_STATIC

    return None, raw, None


def extract_exact_page(
    url: str,
    item: dict[str, Any],
    *,
    allow_browser: bool = True,
    allow_api: bool = True,
    allow_cart: bool = True,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full exact-page extraction pipeline for one URL."""
    stats = stats if stats is not None else {}
    stats.setdefault("http_requests", 0)
    stats.setdefault("browser_renders", 0)
    stats.setdefault("endpoint_calls", 0)
    stats.setdefault("cart_reads", 0)
    stats.setdefault("route_counts", {})
    stats.setdefault("rejections", {})

    identity = _identity(item)
    mpn = str(identity.get("part_number") or "")
    domain = seller_of(url)
    started = time.time()
    diag: dict[str, Any] = {
        "url": url,
        "url_class": classify_url(url, mpn=mpn),
        "routes_attempted": [],
        "raw_candidates": [],
        "best": None,
        "status": "NO_PRICE",
        "rejection": None,
    }

    from exact_page_extraction.models import (
        EXACT_PRODUCT_UNVERIFIED,
        EXACT_PRODUCT_VERIFIED,
        SEARCH_RESULT_SHELL,
    )

    if diag["url_class"] == SEARCH_RESULT_SHELL:
        diag["status"] = SEARCH_RESULT_SHELL
        diag["rejection"] = "search_shell"
        stats["rejections"]["search_shell"] = int(stats["rejections"].get("search_shell") or 0) + 1
        return diag

    # Prefer high-yield route order for domain
    _ = preferred_routes(domain)

    # Static fetch
    fr = fetch_page(url, use_budget=True)
    stats["http_requests"] += 1
    html = fr.get("text") or ""
    diag["routes_attempted"].append("STATIC_FETCH")

    best, raw, route = extract_from_html(html, url=url, identity=identity, route_prefix="static")
    diag["raw_candidates"].extend(raw)
    if best:
        note_extraction(domain, route=route or ROUTE_STATIC, success=True, latency_s=time.time() - started)
        stats["route_counts"][route or ROUTE_STATIC] = int(stats["route_counts"].get(route or ROUTE_STATIC) or 0) + 1
        diag["best"] = best
        diag["status"] = "FOUND_VALID_PRICE"
        diag["route"] = route
        return diag

    # API / product.js endpoints
    if allow_api and html:
        diag["routes_attempted"].append("API_DISCOVERY")
        for api_url in _discover_api_urls(html, url):
            try:
                stats["endpoint_calls"] += 1
                stats["http_requests"] += 1
                fr2 = fetch_page(api_url, use_budget=True)
                body = fr2.get("text") or ""
                data = None
                try:
                    data = json.loads(body)
                except Exception:
                    continue
                for c in _extract_from_api_json(data, mpn=mpn):
                    diag["raw_candidates"].append({**c, "route": ROUTE_API, "api_url": api_url})
                    ok = _try_validate(identity, {**c, "via": "api+" + c.get("via", "")}, url)
                    if ok:
                        note_extraction(
                            domain,
                            route=ROUTE_API,
                            success=True,
                            latency_s=time.time() - started,
                            endpoint=api_url,
                        )
                        stats["route_counts"][ROUTE_API] = int(stats["route_counts"].get(ROUTE_API) or 0) + 1
                        diag["best"] = ok
                        diag["status"] = "FOUND_VALID_PRICE"
                        diag["route"] = ROUTE_API
                        return diag
            except Exception:
                continue

    # Bounded browser
    if allow_browser and (fr.get("blocked") or len(html) < 800 or not best):
        diag["routes_attempted"].append("BROWSER")
        br = bounded_browser_fetch(url, timeout_ms=12000, wait_ms=1500)
        stats["browser_renders"] += 1
        if br.get("ok"):
            bhtml = br.get("html") or ""
            best, raw, route = extract_from_html(bhtml, url=url, identity=identity, route_prefix="browser")
            diag["raw_candidates"].extend(raw)
            if best:
                note_extraction(domain, route=ROUTE_BROWSER, success=True, latency_s=time.time() - started)
                stats["route_counts"][ROUTE_BROWSER] = int(stats["route_counts"].get(ROUTE_BROWSER) or 0) + 1
                diag["best"] = best
                diag["status"] = "FOUND_VALID_PRICE"
                diag["route"] = ROUTE_BROWSER
                return diag
            # Follow discovered public JSON endpoints from browser network
            for jurl in br.get("json_urls") or []:
                try:
                    stats["endpoint_calls"] += 1
                    stats["http_requests"] += 1
                    jfr = fetch_page(jurl, use_budget=True)
                    data = json.loads(jfr.get("text") or "{}")
                except Exception:
                    continue
                for c in _extract_from_api_json(data, mpn=mpn):
                    ok = _try_validate(identity, {**c, "via": "browser_xhr_json"}, url)
                    if ok:
                        note_extraction(domain, route=ROUTE_API, success=True, latency_s=time.time() - started, endpoint=jurl)
                        stats["route_counts"][ROUTE_API] = int(stats["route_counts"].get(ROUTE_API) or 0) + 1
                        diag["best"] = ok
                        diag["status"] = "FOUND_VALID_PRICE"
                        diag["route"] = ROUTE_API
                        return diag
            for net in br.get("network_json") or []:
                try:
                    data = net if isinstance(net, dict) else json.loads(str(net))
                except Exception:
                    continue
                for c in _extract_from_api_json(data, mpn=mpn):
                    ok = _try_validate(identity, {**c, "via": "browser_network"}, url)
                    if ok:
                        note_extraction(domain, route=ROUTE_API, success=True, latency_s=time.time() - started)
                        stats["route_counts"][ROUTE_API] = int(stats["route_counts"].get(ROUTE_API) or 0) + 1
                        diag["best"] = ok
                        diag["status"] = "FOUND_VALID_PRICE"
                        diag["route"] = ROUTE_API
                        return diag

    # Public cart peek — only for domains known to expose cart JSON without login
    if allow_cart and html and any(x in domain for x in ("shopify",)):
        diag["routes_attempted"].append("CART")
        stats["cart_reads"] += 1
        # Shopify /cart.js is often public
        cart_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/cart.js"
        try:
            stats["http_requests"] += 1
            cfr = fetch_page(cart_url, use_budget=True)
            data = json.loads(cfr.get("text") or "{}")
            for c in _extract_from_api_json(data, mpn=mpn):
                ok = _try_validate(identity, {**c, "via": "cart_json"}, url)
                if ok:
                    note_extraction(domain, route=ROUTE_CART, success=True, latency_s=time.time() - started)
                    stats["route_counts"][ROUTE_CART] = int(stats["route_counts"].get(ROUTE_CART) or 0) + 1
                    diag["best"] = ok
                    diag["status"] = "FOUND_VALID_PRICE"
                    diag["route"] = ROUTE_CART
                    return diag
        except Exception:
            pass

    note_extraction(domain, route=ROUTE_STATIC, success=False, latency_s=time.time() - started)
    diag["status"] = "NO_PRICE"
    diag["elapsed_s"] = round(time.time() - started, 3)
    return diag
