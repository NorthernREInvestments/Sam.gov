"""PriceSourceAdapter base + domain-specific adapters.

Build: 20261004-m3-price-adapters-v1
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urlparse

from price_adapters.browser import bounded_browser_fetch, note_route_success
from price_adapters.models import (
    BLOCKED_403,
    BLOCKED_429,
    DOMAIN_UNAVAILABLE,
    EXHAUSTED,
    FOUND_PRODUCT_NO_PRICE,
    FOUND_VALID_PRICE,
    JS_HIDDEN,
    PRODUCT_NOT_FOUND,
    RETRYABLE,
)
from price_adapters.validate import (
    extract_jsonld_exact,
    extract_near_mpn_price,
    seller_of,
    validate_candidate,
)
from public_price_search.catalog_intel import extract_api_hints, extract_structured_prices, fetch_structured_api
from public_price_search.search import fetch_page


class PriceSourceAdapter:
    domain: str = ""
    name: str = ""

    def can_handle(self, domain: str) -> bool:
        d = (domain or "").lower().replace("www.", "")
        return d == self.domain or d.endswith(self.domain)

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        return []

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        return []

    def discover_product(self, identity: dict[str, Any]) -> dict[str, Any]:
        urls = list(dict.fromkeys(self.known_product_urls(identity) + self.search_urls(identity)))
        return {"urls": urls[:6], "status": RETRYABLE if urls else PRODUCT_NOT_FOUND}

    def extract_static(self, page: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any] | None:
        html = page.get("html") or page.get("text") or ""
        url = page.get("url") or ""
        exact = extract_jsonld_exact(
            html,
            mpn=str(identity.get("part_number") or identity.get("mpn") or ""),
            manufacturer=identity.get("manufacturer"),
        )
        if exact:
            c = exact[0]
            c["url"] = url
            c["seller"] = seller_of(url)
            return c
        # Prefer structured only on product-detail URLs with MPN in path
        path = (urlparse(url).path or "").lower()
        mpn = str(identity.get("part_number") or identity.get("mpn") or "")
        if mpn and mpn.lower().replace("-", "") in path.replace("-", "") and "/search" not in path:
            from public_price_search.catalog_intel import extract_structured_prices
            from public_price_search.extract import detect_condition

            for row in extract_structured_prices(html):
                price = row.get("price")
                if price and float(price) >= 1.51 and float(price) not in {5.0, 25.0}:
                    return {
                        "unit_price": float(price),
                        "url": url,
                        "seller": seller_of(url),
                        "via": "structured_mpn_page",
                        "condition": detect_condition(html[:2000]),
                        "name": "",
                        "sku": mpn,
                    }
        near = extract_near_mpn_price(
            html,
            mpn=mpn,
            manufacturer=identity.get("manufacturer"),
        )
        if near:
            # Block Global Industrial placeholder / search-shell prices
            if "globalindustrial" in seller_of(url) and float(near.get("unit_price") or 0) in {5.0, 25.0}:
                return None
            if "globalindustrial" in seller_of(url) and "/search" in path:
                return None
            near["url"] = url
            near["seller"] = seller_of(url)
            return near
        return None

    def extract_structured(self, page: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any] | None:
        return self.extract_static(page, identity)

    def discover_public_endpoint(self, page: dict[str, Any]) -> list[str]:
        html = page.get("html") or ""
        url = page.get("url") or ""
        hints = extract_api_hints(html, page_url=url)
        hints.extend(page.get("json_urls") or [])
        return list(dict.fromkeys(hints))[:6]

    def extract_hydration(self, page: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any] | None:
        return self.extract_static(page, identity)

    def render_bounded(self, url: str, identity: dict[str, Any]) -> dict[str, Any]:
        return bounded_browser_fetch(url, timeout_ms=9000, wait_ms=1200)

    def validate_match(self, identity: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        return validate_candidate(
            identity,
            candidate,
            expected_condition=str(identity.get("expected_condition") or "NEW"),
        )

    def normalize_price(self, candidate: dict[str, Any]) -> dict[str, Any]:
        out = dict(candidate)
        out["displayed_price"] = out.get("unit_price")
        out["landed_estimate"] = out.get("unit_price")
        out["freight_status"] = "FREIGHT_UNKNOWN"
        return out

    def _classify_fetch(self, fr: dict[str, Any]) -> str:
        status = int(fr.get("status_code") or 0)
        text = fr.get("text") or ""
        if fr.get("blocked"):
            if status == 403:
                return BLOCKED_403
            if status == 429:
                return BLOCKED_429
            if status in {0, 502, 503, 504} or not text:
                return DOMAIN_UNAVAILABLE
            return BLOCKED_403 if status >= 400 else DOMAIN_UNAVAILABLE
        if len(text) < 100:
            return DOMAIN_UNAVAILABLE
        if "price" not in text.lower() and "$" not in text:
            return JS_HIDDEN
        return ""

    def recover(self, identity: dict[str, Any], *, allow_browser: bool = True) -> dict[str, Any]:
        """Full cascade for this domain. Diagnostics always returned."""
        diag: dict[str, Any] = {
            "adapter": self.name,
            "domain": self.domain,
            "steps": [],
            "status": EXHAUSTED,
            "candidate": None,
        }
        discovered = self.discover_product(identity)
        urls = discovered.get("urls") or []
        diag["steps"].append({"discover_product": urls})
        if not urls:
            diag["status"] = PRODUCT_NOT_FOUND
            return diag

        for url in urls:
            fr = fetch_page(url, use_budget=True)
            page = {"url": fr.get("url") or url, "html": fr.get("text") or "", "text": fr.get("text") or ""}
            cause = self._classify_fetch(fr)
            diag["steps"].append({"fetch": url, "cause": cause or "OK", "status": fr.get("status_code"), "len": len(page["html"])})

            # static / structured / hydration
            for extractor_name, fn in (
                ("extract_static", self.extract_static),
                ("extract_structured", self.extract_structured),
                ("extract_hydration", self.extract_hydration),
            ):
                cand = fn(page, identity)
                diag["steps"].append({extractor_name: bool(cand)})
                if cand:
                    cand = self.normalize_price(cand)
                    v = self.validate_match(identity, cand)
                    diag["steps"].append({"validate": v})
                    if v.get("ok"):
                        note_route_success(self.domain, url, extractor_name)
                        diag["status"] = FOUND_VALID_PRICE
                        diag["candidate"] = cand
                        return diag

            # public endpoints
            endpoints = self.discover_public_endpoint(page)
            diag["steps"].append({"endpoints": endpoints})
            for ep in endpoints:
                prices = fetch_structured_api(ep, identity=identity)
                if prices:
                    cand = {
                        "unit_price": prices[0]["price"],
                        "url": ep,
                        "seller": seller_of(ep) or self.domain,
                        "via": "product_api_xhr",
                        "condition": "NEW",
                        "name": "",
                        "sku": identity.get("part_number"),
                    }
                    cand = self.normalize_price(cand)
                    v = self.validate_match(identity, cand)
                    diag["steps"].append({"endpoint_validate": v, "ep": ep})
                    if v.get("ok"):
                        note_route_success(self.domain, ep, "product_api_xhr")
                        diag["status"] = FOUND_VALID_PRICE
                        diag["candidate"] = cand
                        return diag

            # bounded browser last — only first URL (exact search/product), not every variant
            if allow_browser and cause in {JS_HIDDEN, BLOCKED_403, "", DOMAIN_UNAVAILABLE} and url == urls[0]:
                br = self.render_bounded(url, identity)
                diag["steps"].append({"browser": {"ok": br.get("ok"), "error": br.get("error"), "json_urls": br.get("json_urls")}})
                if br.get("ok"):
                    bpage = {"url": url, "html": br.get("html") or "", "text": br.get("text") or "", "json_urls": br.get("json_urls") or []}
                    cand = self.extract_static(bpage, identity) or self.extract_hydration(bpage, identity)
                    if cand:
                        cand = self.normalize_price(cand)
                        v = self.validate_match(identity, cand)
                        diag["steps"].append({"browser_validate": v})
                        if v.get("ok"):
                            cand["via"] = (cand.get("via") or "browser") + "+browser"
                            note_route_success(self.domain, url, "browser")
                            diag["status"] = FOUND_VALID_PRICE
                            diag["candidate"] = cand
                            return diag
                    for ep in (bpage.get("json_urls") or [])[:3]:
                        prices = fetch_structured_api(ep, identity=identity)
                        if prices:
                            cand = {
                                "unit_price": prices[0]["price"],
                                "url": ep,
                                "seller": self.domain,
                                "via": "browser_xhr",
                                "condition": "NEW",
                                "sku": identity.get("part_number"),
                                "name": "",
                            }
                            v = self.validate_match(identity, self.normalize_price(cand))
                            if v.get("ok"):
                                diag["status"] = FOUND_VALID_PRICE
                                diag["candidate"] = cand
                                return diag

            if cause:
                diag["status"] = cause
            elif page["html"]:
                diag["status"] = FOUND_PRODUCT_NO_PRICE

        if diag["status"] not in {FOUND_VALID_PRICE}:
            diag["status"] = diag.get("status") or EXHAUSTED
        return diag


class GraingerAdapter(PriceSourceAdapter):
    domain = "grainger.com"
    name = "Grainger"

    # Cached successful / high-confidence product path patterns by MPN
    _KNOWN = {
        "121943": ["https://www.grainger.com/product/BRADY-Lockout-Tag-4E241"],
        "05089": ["https://www.grainger.com/search?searchQuery=CRC+05089"],
        "24221": ["https://www.grainger.com/search?searchQuery=Loctite+24221"],
        "490040": ["https://www.grainger.com/search?searchQuery=WD-40+490040"],
        "2004DC-6": ["https://www.grainger.com/search?searchQuery=Filtrete+2004DC-6"],
        "2097": ["https://www.grainger.com/search?searchQuery=3M+2097"],
        "8210": ["https://www.grainger.com/search?searchQuery=3M+8210"],
        "48-22-1902": ["https://www.grainger.com/search?searchQuery=48-22-1902"],
        "B-45580": ["https://www.grainger.com/search?searchQuery=Makita+B-45580"],
        "30-076": ["https://www.grainger.com/search?searchQuery=Ideal+30-076"],
        "5320-S": ["https://www.grainger.com/search?searchQuery=Leviton+5320-S"],
        "38507": ["https://www.grainger.com/search?searchQuery=Gates+38507"],
        "SET6": ["https://www.grainger.com/search?searchQuery=Timken+SET6"],
    }

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = str(identity.get("part_number") or identity.get("mpn") or "")
        return list(self._KNOWN.get(pn) or [])

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        mfr = identity.get("manufacturer") or ""
        q1 = quote_plus(str(pn))
        q2 = quote_plus(f"{mfr} {pn}".strip())
        return [
            f"https://www.grainger.com/search?searchQuery={q2}",
            f"https://www.grainger.com/search?searchQuery={q1}",
        ]


class SupplyHouseAdapter(PriceSourceAdapter):
    domain = "supplyhouse.com"
    name = "SupplyHouse"

    _KNOWN = {
        "UC248LFA": [
            "https://www.supplyhouse.com/SharkBite-UC248LFA",
            "https://www.supplyhouse.com/search?q=UC248LFA",
            # alternate public seller when SupplyHouse blocked
            "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9",
        ],
        "LF777M2-QT": [
            "https://www.supplyhouse.com/Watts-LF777M2-QT",
            "https://www.supplyhouse.com/search?q=LF777M2-QT",
        ],
        "TH8320U1008": [
            "https://www.supplyhouse.com/Honeywell-Home-TH8320U1008",
            "https://www.supplyhouse.com/search?q=TH8320U1008",
        ],
    }

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = str(identity.get("part_number") or identity.get("mpn") or "")
        return list(self._KNOWN.get(pn) or [])

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        return [
            f"https://www.supplyhouse.com/search?q={quote_plus(str(pn))}",
            f"https://www.supplyhouse.com/{quote_plus(str(pn))}",
        ]


class ZoroAdapter(PriceSourceAdapter):
    domain = "zoro.com"
    name = "Zoro"

    _KNOWN = {
        "DWHT11131": ["https://www.zoro.com/search?q=DWHT11131"],
    }

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = str(identity.get("part_number") or identity.get("mpn") or "")
        return list(self._KNOWN.get(pn) or [])

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        mfr = identity.get("manufacturer") or ""
        return [
            f"https://www.zoro.com/search?q={quote_plus(f'{mfr} {pn}'.strip())}",
            f"https://www.zoro.com/search?q={quote_plus(str(pn))}",
        ]


class FinditPartsAdapter(PriceSourceAdapter):
    domain = "finditparts.com"
    name = "FinditParts"

    _KNOWN = {
        "FF63009": ["https://www.finditparts.com/search?q=FF63009"],
        "LF9009": ["https://www.finditparts.com/search?q=LF9009"],
        "4938461": ["https://www.finditparts.com/search?q=4938461"],
    }

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = str(identity.get("part_number") or identity.get("mpn") or "")
        return list(self._KNOWN.get(pn) or [])

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        return [f"https://www.finditparts.com/search?q={quote_plus(str(pn))}"]


class GlobalIndustrialAdapter(PriceSourceAdapter):
    domain = "globalindustrial.com"
    name = "Global Industrial"

    _KNOWN = {
        "DWT-6": [
            "https://www.globalindustrial.com/p/dwt-6",
            "https://www.globalindustrial.com/search?q=DWT-6",
        ],
    }

    def known_product_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = str(identity.get("part_number") or identity.get("mpn") or "")
        return list(self._KNOWN.get(pn) or [])

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        return [
            f"https://www.globalindustrial.com/search?q={quote_plus(str(pn))}",
            f"https://www.globalindustrial.com/p/{quote_plus(str(pn).lower())}",
        ]


class QuillAdapter(PriceSourceAdapter):
    """High-yield alternate for many MRO/office-adjacent exact IDs."""

    domain = "quill.com"
    name = "Quill"

    def search_urls(self, identity: dict[str, Any]) -> list[str]:
        pn = identity.get("part_number") or identity.get("mpn") or ""
        mfr = identity.get("manufacturer") or ""
        return [f"https://www.quill.com/search?keywords={quote_plus(f'{mfr} {pn}'.strip())}"]


ADAPTERS: list[PriceSourceAdapter] = [
    GraingerAdapter(),
    SupplyHouseAdapter(),
    ZoroAdapter(),
    FinditPartsAdapter(),
    GlobalIndustrialAdapter(),
    QuillAdapter(),
]


def get_adapter_for_domain(domain: str) -> PriceSourceAdapter | None:
    d = (domain or "").lower().replace("www.", "")
    for a in ADAPTERS:
        if a.can_handle(d):
            return a
    return None
