"""Extensible seller-domain adapters for blocked-route recovery.

Build: 20261004-m3-price-coverage-80-v1
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse

from public_price_search.catalog_intel import extract_structured_prices
from public_price_search.extract import _f, detect_condition, identity_on_page
from public_price_search.search import fetch_page

AdapterFn = Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any] | None]


@dataclass
class SellerAdapter:
    domain: str
    name: str
    search_patterns: list[str]
    product_url_patterns: list[str] = field(default_factory=list)
    extract: AdapterFn | None = None
    structured_preferred: bool = True
    notes: str = ""


def classify_block_cause(fetch_result: dict[str, Any]) -> str:
    """Exact block taxonomy — do not lump into ROUTE_BLOCKED."""
    from price_coverage_80.models import (
        BLOCK_403,
        BLOCK_429,
        BLOCK_BOT_PAGE,
        BLOCK_DOMAIN_UNAVAILABLE,
        BLOCK_EMPTY_HTML,
        BLOCK_JS_HIDDEN,
        BLOCK_LOGIN,
        BLOCK_UNKNOWN,
    )

    status = int(fetch_result.get("status_code") or 0)
    text = (fetch_result.get("text") or "")[:8000].lower()
    err = str(fetch_result.get("error") or "").lower()
    if fetch_result.get("blocked"):
        if status == 403:
            return BLOCK_403
        if status == 429:
            return BLOCK_429
        if "captcha" in text or "cf-browser" in text or "attention required" in text:
            return BLOCK_BOT_PAGE
        if "login" in text or "sign in" in text or "authenticate" in text:
            return BLOCK_LOGIN
        if status in {0, 502, 503, 504} or "timeout" in err or "connect" in err:
            return BLOCK_DOMAIN_UNAVAILABLE
        return BLOCK_UNKNOWN
    if not text or len(text.strip()) < 80:
        return BLOCK_EMPTY_HTML
    # Price likely JS-hidden if product signals but no $ / price JSON
    if ("product" in text or "sku" in text) and "$" not in text and "price" not in text:
        return BLOCK_JS_HIDDEN
    if "login" in text and "price" not in text:
        return BLOCK_LOGIN
    return ""


def _extract_generic(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    text = fr.get("text") or ""
    if not text:
        return None
    pn = str(identity.get("part_number") or "").upper()
    path = urlparse(url).path or ""
    # Search/listing URLs often include the query MPN — that is NOT product evidence.
    is_search = any(x in (url or "").lower() for x in ("/search", "search?", "keywords=", "Ntt=", "searchterm="))
    on_page = identity_on_page(text, identity)
    if not on_page:
        # Allow URL soft-match only on product-detail paths (not search shells)
        if is_search or not pn or pn.lower() not in path.lower():
            return None
    structured = extract_structured_prices(text)
    price = None
    via = "adapter_html"
    if structured:
        price = structured[0].get("price")
        via = "adapter_structured"
        # Reject common sitewide placeholders when identity is weak
        if price in {5.0, 8.0, 25.0} and not on_page:
            price = None
    if not price:
        m = re.search(r'(?:["\']price["\']\s*:\s*["\']?)(\d+(?:\.\d{1,2})?)', text, re.I)
        if m:
            price = _f(m.group(1))
            via = "adapter_json_price"
    if not price:
        # Dollar scrape requires on-page identity AND price near the MPN (not first $ on shell)
        if on_page and pn:
            idx = text.upper().find(pn.upper())
            window = text[max(0, idx - 800) : idx + 2000] if idx >= 0 else ""
            m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", window)
            if m:
                price = _f(m.group(1).replace(",", ""))
                via = "adapter_dollar"
                # Reject tiny generic shell amounts unless structured/near evidence
                if price is not None and price <= 8.0 and is_search:
                    price = None
    if not price or price < 1.51:
        return None
    cond = detect_condition(text[:4000])
    return {
        "unit_price": price,
        "condition": cond,
        "url": fr.get("url") or url,
        "via": via,
        "seller": urlparse(url).netloc.replace("www.", ""),
        "status": "PRICE_OK",
    }


def _grainger_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    text = fr.get("text") or ""
    # Grainger often embeds price in dataLayer / JSON-LD / __NEXT_DATA__
    structured = extract_structured_prices(text)
    pn = str(identity.get("part_number") or "").upper()
    on_page = identity_on_page(text, identity) or (pn and pn.lower() in url.lower()) or (pn and pn in text.upper())
    if structured and on_page:
        price = structured[0].get("price")
        if price and float(price) >= 1.51:
            return {
                "unit_price": float(price),
                "condition": detect_condition(text[:3000]),
                "url": fr.get("url") or url,
                "via": "adapter_grainger_structured",
                "seller": "grainger.com",
                "status": "PRICE_OK",
            }
    for pat in (
        r'"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?',
        r'data-price=["\'](?P<p>\d+\.\d{2})["\']',
        r'"sellingPrice"\s*:\s*(?P<p>\d+\.\d{2})',
        r'"customerPrice"\s*:\s*(?P<p>\d+\.\d{2})',
        r'"unitPrice"\s*:\s*(?P<p>\d+\.\d{2})',
        r'price\\?":\s*\\?"?(?P<p>\d+\.\d{2})',
    ):
        m = re.search(pat, text, re.I)
        if m and on_page:
            price = _f(m.group("p"))
            if price and price >= 1.51:
                return {
                    "unit_price": price,
                    "condition": detect_condition(text[:3000]),
                    "url": fr.get("url") or url,
                    "via": "adapter_grainger",
                    "seller": "grainger.com",
                    "status": "PRICE_OK",
                }
    # Search pages: follow first product detail link
    if "/search" in url and "/product/" not in url and pn:
        links = re.findall(r'href=["\'](/product/[^"\']+)["\']', text)
        if links:
            from public_price_search.search import fetch_page as _fp

            href = links[0]
            pfr = _fp("https://www.grainger.com" + href, use_budget=True)
            nested = _grainger_extract(identity, "https://www.grainger.com" + href, pfr)
            if nested:
                nested["via"] = (nested.get("via") or "adapter_grainger") + "+product_follow"
                return nested
    return _extract_generic(identity, url, fr)


def _zoro_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    text = fr.get("text") or ""
    m = re.search(r'"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?', text)
    if m and identity_on_page(text, identity):
        price = _f(m.group("p"))
        if price and price >= 1.51:
            return {
                "unit_price": price,
                "condition": detect_condition(text[:3000]),
                "url": fr.get("url") or url,
                "via": "adapter_zoro",
                "seller": "zoro.com",
                "status": "PRICE_OK",
            }
    return _extract_generic(identity, url, fr)


def _global_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    text = fr.get("text") or ""
    # Global Industrial search shells embed sitewide $5/$25 placeholders without the MPN.
    if not identity_on_page(text, identity):
        return None
    pn = str(identity.get("part_number") or identity.get("mpn") or "")
    path = (url or "").lower()
    if pn and pn.lower() not in path and "/p/" not in path and "/product" not in path:
        # Require product-detail URL when only search shell is present
        if "/search" in path:
            return None
    for pat in (
        r'"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?',
        r'itemprop=["\']price["\'][^>]*content=["\'](?P<p>\d+\.\d{2})["\']',
    ):
        m = re.search(pat, text, re.I)
        if m:
            price = _f(m.group("p"))
            # Reject known sitewide placeholder amounts
            if price in {5.0, 25.0}:
                continue
            if price and price >= 1.51:
                return {
                    "unit_price": price,
                    "condition": detect_condition(text[:3000]),
                    "url": fr.get("url") or url,
                    "via": "adapter_globalindustrial",
                    "seller": "globalindustrial.com",
                    "status": "PRICE_OK",
                }
    return None


def _bulbs_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    return _extract_generic(identity, url, fr)


def _supplyhouse_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    return _extract_generic(identity, url, fr)


def _findit_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    return _extract_generic(identity, url, fr)


def _fleetpride_extract(identity: dict[str, Any], url: str, fr: dict[str, Any]) -> dict[str, Any] | None:
    return _extract_generic(identity, url, fr)


ADAPTERS: dict[str, SellerAdapter] = {
    "grainger.com": SellerAdapter(
        domain="grainger.com",
        name="Grainger",
        search_patterns=[
            "https://www.grainger.com/search?searchQuery={q}",
            "https://www.grainger.com/product/search?searchQuery={q}",
        ],
        extract=_grainger_extract,
        notes="JSON-LD + data-price",
    ),
    "zoro.com": SellerAdapter(
        domain="zoro.com",
        name="Zoro",
        search_patterns=["https://www.zoro.com/search?q={q}"],
        extract=_zoro_extract,
    ),
    "globalindustrial.com": SellerAdapter(
        domain="globalindustrial.com",
        name="Global Industrial",
        search_patterns=["https://www.globalindustrial.com/search?q={q}"],
        extract=_global_extract,
    ),
    "1000bulbs.com": SellerAdapter(
        domain="1000bulbs.com",
        name="1000Bulbs",
        search_patterns=["https://www.1000bulbs.com/search?q={q}"],
        extract=_bulbs_extract,
    ),
    "supplyhouse.com": SellerAdapter(
        domain="supplyhouse.com",
        name="SupplyHouse",
        search_patterns=["https://www.supplyhouse.com/search?q={q}"],
        extract=_supplyhouse_extract,
    ),
    "finditparts.com": SellerAdapter(
        domain="finditparts.com",
        name="FinditParts",
        search_patterns=["https://www.finditparts.com/search?q={q}"],
        extract=_findit_extract,
    ),
    "fleetpride.com": SellerAdapter(
        domain="fleetpride.com",
        name="FleetPride",
        search_patterns=["https://www.fleetpride.com/search?q={q}"],
        extract=_fleetpride_extract,
    ),
    "staples.com": SellerAdapter(
        domain="staples.com",
        name="Staples",
        search_patterns=["https://www.staples.com/search?query={q}"],
        extract=_extract_generic,
    ),
    "mscdirect.com": SellerAdapter(
        domain="mscdirect.com",
        name="MSC",
        search_patterns=["https://www.mscdirect.com/browse/tn/?searchterm={q}"],
        extract=_extract_generic,
    ),
    "fastenal.com": SellerAdapter(
        domain="fastenal.com",
        name="Fastenal",
        search_patterns=["https://www.fastenal.com/product/search?term={q}"],
        extract=_extract_generic,
    ),
}


def get_adapter(domain: str) -> SellerAdapter | None:
    d = (domain or "").lower().replace("www.", "")
    if d in ADAPTERS:
        return ADAPTERS[d]
    for k, a in ADAPTERS.items():
        if k in d or d.endswith(k):
            return a
    return None


def register_adapter(adapter: SellerAdapter) -> None:
    """Extensible registration for newly discovered high-value domains."""
    ADAPTERS[adapter.domain.lower().replace("www.", "")] = adapter


def search_urls_for_domain(domain: str, identity: dict[str, Any]) -> list[str]:
    adapter = get_adapter(domain)
    pn = str(identity.get("part_number") or "")
    mfr = str(identity.get("manufacturer") or identity.get("brand") or "")
    queries = [pn]
    if mfr and pn:
        queries.append(f"{mfr} {pn}")
    if not adapter:
        return [f"https://www.{domain}/search?q={quote_plus(pn)}"] if pn else []
    urls: list[str] = []
    for q in queries:
        for pat in adapter.search_patterns:
            urls.append(pat.format(q=quote_plus(q)))
    return list(dict.fromkeys(urls))


def try_adapter_fetch(
    identity: dict[str, Any],
    *,
    domain: str,
    use_budget: bool = True,
    client=None,
) -> dict[str, Any]:
    """Direct site search via seller adapter. Returns candidates + block causes."""
    urls = search_urls_for_domain(domain, identity)
    adapter = get_adapter(domain)
    candidates: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    for url in urls[:3]:
        fr = fetch_page(url, client=client, use_budget=use_budget)
        cause = classify_block_cause(fr)
        # Hard blocks: do not extract. Soft (js_hidden): still attempt structured extract.
        hard = cause in {
            "403",
            "429",
            "bot_page",
            "login_required",
            "domain_unavailable",
            "empty_html",
        }
        if hard:
            blocks.append({"url": url, "cause": cause, "status_code": fr.get("status_code")})
            continue
        extract_fn = (adapter.extract if adapter and adapter.extract else _extract_generic)
        cand = extract_fn(identity, url, fr)
        if cand:
            cand["domain"] = domain
            if cause == "js_hidden":
                cand["via"] = (cand.get("via") or "adapter") + "+js_recovery"
            candidates.append(cand)
        elif cause:
            blocks.append({"url": url, "cause": cause, "status_code": fr.get("status_code")})
    return {"domain": domain, "candidates": candidates, "blocks": blocks, "urls": urls}


# Persist domain search intelligence
_DOMAIN_INTEL_FILE = "m3_price_coverage_80_domain_intel.json"


def load_domain_intel() -> dict[str, Any]:
    from m3_data_root import data_path

    p = data_path(_DOMAIN_INTEL_FILE)
    if not p.exists():
        return {"domains": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"domains": {}}


def note_domain_result(domain: str, *, success: bool, method: str = "", latency_ms: float = 0) -> None:
    from application_clock import now_utc
    from m3_data_root import data_path

    intel = load_domain_intel()
    domains = intel.setdefault("domains", {})
    d = domain.lower().replace("www.", "")
    row = domains.setdefault(
        d,
        {
            "domain": d,
            "attempts": 0,
            "successful_price_extractions": 0,
            "block_rate_num": 0,
            "methods": {},
            "last_verified": None,
        },
    )
    row["attempts"] = int(row.get("attempts") or 0) + 1
    if success:
        row["successful_price_extractions"] = int(row.get("successful_price_extractions") or 0) + 1
        row["last_verified"] = now_utc().isoformat()
        if method:
            methods = row.setdefault("methods", {})
            methods[method] = int(methods.get(method) or 0) + 1
    else:
        row["block_rate_num"] = int(row.get("block_rate_num") or 0) + 1
    row["success_rate"] = (
        float(row["successful_price_extractions"]) / float(row["attempts"]) if row["attempts"] else 0.0
    )
    p = data_path(_DOMAIN_INTEL_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(intel, indent=2, default=str), encoding="utf-8")
