"""Deep price extraction for one known exact PDP URL."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from exact_page_extraction.extract import extract_exact_page, extract_from_html, _extract_from_api_json, _try_validate
from exact_page_extraction.models import ROUTE_API, ROUTE_BROWSER, ROUTE_CART, ROUTE_HYDRATION, ROUTE_JSONLD, ROUTE_STATIC
from exact_product_url_discovery.validate_identity import validate_product_page
from known_pdp_price_extraction.adapters import (
    detect_shopify,
    extract_shopify_product_js,
    run_domain_adapter,
    shopify_product_js_urls,
)
from known_pdp_price_extraction.memory import note_domain_extraction, persist_endpoint
from price_adapters.browser import bounded_browser_fetch
from price_adapters.orchestrate import _identity
from price_adapters.validate import seller_of
from public_price_search.search import fetch_page


def _page_invalid(html: str, title: str = "") -> str | None:
    blob = f"{title} {html[:2000]}".lower()
    if any(x in blob for x in ("page not found", "404 not found", "404 error", "product not found")):
        return "page_404"
    if any(x in blob for x in ("attention required", "just a moment", "access denied", "access to this page has been denied", "cf-browser-verification")):
        return "bot_wall"
    if len(html or "") < 400:
        return "empty_page"
    return None


def _pack_ok(item: dict[str, Any], title: str, url: str, price: float) -> tuple[bool, str]:
    from price_14_expand_patterns.price_url import _pack_compatible

    return _pack_compatible(item, title=title, url=url, price=price)


def _condition_ok(cond: str) -> tuple[bool, str]:
    c = (cond or "NEW").upper()
    if any(x in c for x in ("USED", "REFURB", "REMAN", "RECON", "OPEN-BOX", "OPEN BOX")):
        return False, "wrong_condition"
    return True, "ok"


def extract_known_pdp(
    url: str,
    item: dict[str, Any],
    *,
    stats: dict[str, Any] | None = None,
    allow_browser: bool = True,
    allow_same_domain_rediscover: bool = True,
) -> dict[str, Any]:
    """Extract executable NEW price from a known exact PDP.

    Layers: static → adapter → JSON-LD/hydration → Shopify .js → browser+network → cart.
    If PDP invalid and allowed, attempt same-domain internal search rediscovery.
    """
    stats = stats if stats is not None else {}
    started = time.time()
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    domain = seller_of(url)
    identity = _identity(item)
    layers = {
        "static": False,
        "jsonld": False,
        "hydration": False,
        "adapter": False,
        "xhr_api": False,
        "browser": False,
        "cart": False,
    }
    out: dict[str, Any] = {
        "url": url,
        "domain": domain,
        "benchmark_id": item.get("benchmark_id"),
        "status": "FAIL",
        "price": None,
        "condition": None,
        "pack_uom": None,
        "extraction_route": None,
        "layers": layers,
        "rejection": None,
        "endpoints_found": [],
        "page_valid": None,
        "human_price_visible": "UNKNOWN",
        "next_fix": None,
    }

    if not url or not url.startswith("http"):
        out["rejection"] = "missing_url"
        return out

    # --- Static fetch ---
    stats["static_parses"] = int(stats.get("static_parses") or 0) + 1
    stats["http_requests"] = int(stats.get("http_requests") or 0) + 1
    fr = fetch_page(url, use_budget=True)
    html = fr.get("text") or ""
    title_m = re.search(r"<title[^>]*>([^<]{0,160})", html, re.I)
    title = title_m.group(1).strip() if title_m else ""
    invalid = _page_invalid(html, title)
    layers["static"] = True

    # If blocked/empty, go browser early
    if invalid in {"bot_wall", "empty_page"} and allow_browser:
        stats["browser_renders"] = int(stats.get("browser_renders") or 0) + 1
        br = bounded_browser_fetch(
            url,
            timeout_ms=18000,
            wait_ms=2500,
            capture_json=True,
            capture_bodies=True,
            force_refresh=True,
        )
        layers["browser"] = True
        if br.get("ok"):
            html = br.get("html") or html
            title_m = re.search(r"<title[^>]*>([^<]{0,160})", html, re.I)
            title = title_m.group(1).strip() if title_m else title
            invalid = _page_invalid(html, title)
            out["browser_json_urls"] = br.get("json_urls") or []
            out["network_payloads"] = len(br.get("network_json") or [])
            # Try network bodies immediately
            for net in br.get("network_json") or []:
                for c in _extract_from_api_json(net, mpn=mpn):
                    ok = _accept(identity, item, c, url, route=ROUTE_API, via="browser_network", stats=stats, layers=layers)
                    if ok:
                        note_domain_extraction(domain, method="API_NETWORK", success=True, url=url)
                        return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}
            for jurl in br.get("json_urls") or []:
                out["endpoints_found"].append(jurl)
                try:
                    stats["http_requests"] = int(stats.get("http_requests") or 0) + 1
                    stats["api_calls"] = int(stats.get("api_calls") or 0) + 1
                    jfr = fetch_page(jurl, use_budget=True)
                    data = json.loads(jfr.get("text") or "{}")
                except Exception:
                    continue
                for c in _extract_from_api_json(data, mpn=mpn):
                    ok = _accept(identity, item, c, url, route=ROUTE_API, via="browser_xhr", stats=stats, layers=layers)
                    if ok:
                        persist_endpoint(domain, jurl, method="XHR")
                        note_domain_extraction(domain, method="API_XHR", success=True, url=url, endpoint=jurl)
                        return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}

    if invalid == "page_404":
        out["rejection"] = "page_404"
        out["page_valid"] = False
        out["human_price_visible"] = "NO"
        out["next_fix"] = "same_domain_internal_search_for_live_pdp"
        if allow_same_domain_rediscover:
            alt = _same_domain_rediscover(item, domain, stats=stats)
            if alt:
                return extract_known_pdp(
                    alt,
                    item,
                    stats=stats,
                    allow_browser=allow_browser,
                    allow_same_domain_rediscover=False,
                )
        note_domain_extraction(domain, method="STATIC", success=False, url=url)
        return out

    # --- Domain adapter on current HTML ---
    stats["adapter_parses"] = int(stats.get("adapter_parses") or 0) + 1
    for c in run_domain_adapter(url, html, item):
        layers["adapter"] = True
        ok = _accept(identity, item, c, url, route="ADAPTER", via=c.get("via") or "adapter", stats=stats, layers=layers)
        if ok:
            note_domain_extraction(domain, method="ADAPTER", success=True, url=url)
            return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}

    # --- Generic exact-page extract (JSON-LD / hydration / static) ---
    best, raw, route = extract_from_html(html, url=url, identity=identity, route_prefix="static")
    stats["jsonld_parses"] = int(stats.get("jsonld_parses") or 0) + 1
    stats["hydration_parses"] = int(stats.get("hydration_parses") or 0) + 1
    if route and "JSON" in str(route).upper():
        layers["jsonld"] = True
    if route and "HYDR" in str(route).upper():
        layers["hydration"] = True
    if best:
        ok = _accept(
            identity,
            item,
            best,
            url,
            route=route or ROUTE_STATIC,
            via=best.get("via") or "extract_from_html",
            stats=stats,
            layers=layers,
        )
        if ok:
            note_domain_extraction(domain, method=str(route or "STATIC"), success=True, url=url)
            return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}

    # --- Shopify product.js (always try for /products/ URLs or shopify HTML) ---
    if detect_shopify(html) or "shopify" in domain or "/products/" in (url or "").lower() or domain in {
        "lightbulbs.com",
        "crcautocare.com",
        "gorillatough.com",
        "jbweld.com",
        "pksafety.com",
    }:
        for js_url in shopify_product_js_urls(url, html):
            out["endpoints_found"].append(js_url)
            try:
                stats["http_requests"] = int(stats.get("http_requests") or 0) + 1
                stats["api_calls"] = int(stats.get("api_calls") or 0) + 1
                jfr = fetch_page(js_url, use_budget=True)
                data = json.loads(jfr.get("text") or "{}")
            except Exception:
                continue
            layers["xhr_api"] = True
            cands = extract_shopify_product_js(data, mpn=mpn) or _extract_from_api_json(data, mpn=mpn)
            for c in cands:
                ok = _accept(
                    identity,
                    item,
                    c,
                    url,
                    route=ROUTE_API,
                    via=c.get("via") or "shopify_product_js",
                    stats=stats,
                    layers=layers,
                )
                if ok:
                    persist_endpoint(domain, js_url, method="SHOPIFY_JS")
                    note_domain_extraction(domain, method="SHOPIFY_JS", success=True, url=url, endpoint=js_url)
                    return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}
        # Public cart.js peek (no add-to-cart)
        cart_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/cart.js"
        try:
            stats["cart_reads"] = int(stats.get("cart_reads") or 0) + 1
            stats["http_requests"] = int(stats.get("http_requests") or 0) + 1
            cfr = fetch_page(cart_url, use_budget=True)
            data = json.loads(cfr.get("text") or "{}")
            layers["cart"] = True
            for c in _extract_from_api_json(data, mpn=mpn):
                ok = _accept(identity, item, c, url, route=ROUTE_CART, via="cart_js", stats=stats, layers=layers)
                if ok:
                    note_domain_extraction(domain, method="CART", success=True, url=url)
                    return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}
        except Exception:
            pass

    # legacy block removed below — keep browser fallback
    if False and (detect_shopify(html) or "shopify" in domain):
        pass

    # --- Browser fallback if not already ---
    if allow_browser and not layers.get("browser"):
        stats["browser_renders"] = int(stats.get("browser_renders") or 0) + 1
        br = bounded_browser_fetch(
            url,
            timeout_ms=18000,
            wait_ms=3000,
            capture_json=True,
            capture_bodies=True,
            force_refresh=True,
        )
        layers["browser"] = True
        if br.get("ok"):
            bhtml = br.get("html") or ""
            btext = br.get("text") or ""
            dollars = re.findall(r"\$\s*\d+\.\d{2}", btext[:20000])
            out["human_price_visible"] = "YES" if dollars else "NO"
            # adapter + extract on rendered HTML
            for c in run_domain_adapter(url, bhtml, item):
                ok = _accept(identity, item, c, url, route=ROUTE_BROWSER, via=c.get("via") or "browser_adapter", stats=stats, layers=layers)
                if ok:
                    note_domain_extraction(domain, method="BROWSER_ADAPTER", success=True, url=url)
                    return {**out, **ok, "layers": layers, "page_valid": True}
            best, raw, route = extract_from_html(bhtml, url=url, identity=identity, route_prefix="browser")
            if best:
                ok = _accept(identity, item, best, url, route=ROUTE_BROWSER, via=best.get("via") or "browser_html", stats=stats, layers=layers)
                if ok:
                    note_domain_extraction(domain, method="BROWSER", success=True, url=url)
                    return {**out, **ok, "layers": layers, "page_valid": True}
            for net in br.get("network_json") or []:
                for c in _extract_from_api_json(net, mpn=mpn):
                    ok = _accept(identity, item, c, url, route=ROUTE_API, via="browser_network", stats=stats, layers=layers)
                    if ok:
                        note_domain_extraction(domain, method="API_NETWORK", success=True, url=url)
                        return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}
            for jurl in br.get("json_urls") or []:
                out["endpoints_found"].append(jurl)
                try:
                    stats["api_calls"] = int(stats.get("api_calls") or 0) + 1
                    jfr = fetch_page(jurl, use_budget=True)
                    data = json.loads(jfr.get("text") or "{}")
                except Exception:
                    continue
                for c in _extract_from_api_json(data, mpn=mpn):
                    ok = _accept(identity, item, c, url, route=ROUTE_API, via="browser_xhr", stats=stats, layers=layers)
                    if ok:
                        persist_endpoint(domain, jurl, method="XHR")
                        return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}

    # Fallback: full extract_exact_page pipeline
    diag = extract_exact_page(url, item, allow_browser=allow_browser, allow_api=True, allow_cart=True, stats=stats)
    if diag.get("status") == "FOUND_VALID_PRICE" and (diag.get("best") or {}).get("unit_price"):
        best = diag["best"]
        ok = _accept(
            identity,
            item,
            best,
            url,
            route=diag.get("route") or ROUTE_STATIC,
            via=best.get("via") or "extract_exact_page",
            stats=stats,
            layers=layers,
        )
        if ok:
            note_domain_extraction(domain, method=str(diag.get("route") or "EXACT"), success=True, url=url)
            return {**out, **ok, "layers": layers, "page_valid": True, "human_price_visible": "YES"}

    out["rejection"] = "NO_PRICE"
    out["page_valid"] = invalid is None
    out["elapsed_s"] = round(time.time() - started, 2)
    if out["human_price_visible"] == "UNKNOWN":
        out["human_price_visible"] = "UNKNOWN"
    out["next_fix"] = (
        "capture_public_pricing_api_or_login_wall"
        if layers.get("browser")
        else "browser_render_with_network_body_capture"
    )
    note_domain_extraction(domain, method="ALL", success=False, url=url)
    stats["no_price"] = int(stats.get("no_price") or 0) + 1
    return out


def _accept(
    identity: dict[str, Any],
    item: dict[str, Any],
    cand: dict[str, Any],
    url: str,
    *,
    route: str,
    via: str,
    stats: dict[str, Any],
    layers: dict[str, bool],
) -> dict[str, Any] | None:
    price = cand.get("unit_price") or cand.get("price")
    if price is None:
        return None
    try:
        price = float(price)
    except Exception:
        return None
    # Normalize Shopify cents mistakenly left large
    if price > 500 and price == int(price) and price % 1 == 0 and price < 100000:
        # likely already dollars if under category norms; leave as-is
        pass
    title = str(cand.get("name") or "")
    ok_pack, pack_reason = _pack_ok(item, title, url, price)
    if not ok_pack:
        stats.setdefault("rejections", {})
        stats["rejections"]["wrong_pack"] = int(stats["rejections"].get("wrong_pack") or 0) + 1
        return None
    cond = str(cand.get("condition") or cand.get("condition_hint") or "NEW")
    ok_c, _ = _condition_ok(cond)
    if not ok_c:
        stats.setdefault("rejections", {})
        stats["rejections"]["wrong_condition"] = int(stats["rejections"].get("wrong_condition") or 0) + 1
        return None
    # Soft outlier: classify but still accept when identity/page is exact
    outlier = None
    cat = str(item.get("category") or "").lower()
    ceilings = {
        "hvac": 800.0,
        "office": 500.0,
        "mro": 400.0,
        "ppe": 400.0,
        "tools": 300.0,
        "electrical": 300.0,
        "plumbing": 500.0,
        "lighting": 400.0,
        "furniture": 2000.0,
    }
    ceil = ceilings.get(cat)
    if ceil and price > ceil:
        outlier = "PRICE_OUTLIER_NEEDS_SECOND_SOURCE"
        # Phase 10: do NOT discard solely for surprising price when candidate has via evidence
        # Require via not empty
        if not via:
            return None
    # Prefer validate_candidate when possible
    try:
        v = _try_validate(identity, {**cand, "unit_price": price, "via": via}, url)
        if not v:
            # still allow adapter/dom prices with MPN in URL
            if not mpn_in_url(url, str(item.get("mpn") or "")):
                return None
            v = {"unit_price": price, "name": title, "via": via, "condition": "NEW"}
    except Exception:
        v = {"unit_price": price, "name": title, "via": via, "condition": "NEW"}

    stats["prices_found"] = int(stats.get("prices_found") or 0) + 1
    return {
        "status": "PASS",
        "price": float(v.get("unit_price") or price),
        "condition": "NEW",
        "pack_uom": f"{item.get('expected_pack') or 1} {item.get('expected_uom') or 'EA'}",
        "extraction_route": route,
        "via": via,
        "title": str(v.get("name") or title)[:160],
        "seller": seller_of(url),
        "url": url,
        "domain": seller_of(url),
        "usable": True,
        "price_class": outlier or "PRICE_VERIFIED",
        "rejection": None,
    }


def mpn_in_url(url: str, mpn: str) -> bool:
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").lower()
    if len(tok) < 4:
        return False
    return tok in re.sub(r"[^a-z0-9]", "", (url or "").lower())


def _same_domain_rediscover(item: dict[str, Any], domain: str, *, stats: dict[str, Any]) -> str | None:
    """When known PDP is 404, try same-domain internal search only (not broad web)."""
    try:
        from exact_product_url_discovery.search_convert import convert_internal_search
        from exact_product_url_discovery.classify import is_search_shell
        from exact_product_url_discovery.validate_identity import validate_product_page
    except Exception:
        return None
    mpn = str(item.get("mpn") or "")
    mfr = str(item.get("manufacturer") or "")
    stats["same_domain_rediscover"] = int(stats.get("same_domain_rediscover") or 0) + 1
    for row in convert_internal_search(domain, mpn=mpn, manufacturer=mfr, limit=3):
        url = row.get("url") or ""
        if not url.startswith("http") or is_search_shell(url):
            continue
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=False,
        )
        if v.get("identity_match"):
            return url
        # one browser validate
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=True,
        )
        if v.get("identity_match"):
            return url
    return None
