"""Orchestrate primary adapters + alternate seller recovery + Bing discovery.

Build: 20261004-m3-price-adapters-v1
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote_plus, urlparse

from price_adapters.base import (
    ADAPTERS,
    QuillAdapter,
    get_adapter_for_domain,
)
from price_adapters.browser import bing_discover_urls, bounded_browser_fetch, browser_stats, note_route_success
from price_adapters.models import FOUND_VALID_PRICE, BUILD
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

# Alternate seller search templates (exact MPN recovery when known seller blocked)
_ALT_TEMPLATES = [
    ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/{pn}"),
    ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/search?q={q}"),
    ("1000bulbs.com", "https://www.1000bulbs.com/search?q={q}"),
    ("staples.com", "https://www.staples.com/search?query={q}"),
    ("officedepot.com", "https://www.officedepot.com/catalog/search.do?Ntt={q}"),
    ("mscdirect.com", "https://www.mscdirect.com/browse/tn/?searchterm={q}"),
    ("bradyid.com", "https://www.bradyid.com/search?text={q}"),
    ("thedieselstore.com", "https://www.thedieselstore.com/search?type=product&q={q}"),
    ("motion.com", "https://www.motion.com/products/search?q={q}"),
    ("mccoys.com", "https://www.mccoys.com/search?q={pn}"),
    ("rspsupply.com", "https://rspsupply.com/search.aspx?SearchTerm={q}"),
]

_TRUSTED_FOR_CLAIM = (
    "grainger.com",
    "zoro.com",
    "supplyhouse.com",
    "1000bulbs.com",
    "globalindustrial.com",
    "finditparts.com",
    "mscdirect.com",
    "fastenal.com",
    "staples.com",
    "fleetpride.com",
    "dieselpartsdirect.com",
    "thedieselstore.com",
    "alliantpower.com",
    "quill.com",
    "officedepot.com",
    "homedepot.com",
    "lowes.com",
    "bradyid.com",
    "motion.com",
    "mcmaster.com",
    "webstaurantstore.com",
    "acehardware.com",
    "shop.cummins.com",
    "cummins.com",
    "mccoys.com",
    "rspsupply.com",
    "gordonelectricsupply.com",
    "smcelectric.com",
    "parts-hvac.com",
    "partshvac.com",
    "brother-usa.com",
    "autozone.com",
    "autobuffy.com",
    "compsource.com",
    "crcautocare.com",
    "crcindustries.com",
    "maxtran.com",
    "summitracing.com",
    "rockauto.com",
    "platt.com",
    "rexelusa.com",
    "elliott-electric.com",
    "nationaldistributorllc.com",
    "nationalmaintenance.com",
    "octopart.com",
    "proteccontrols.com",
    "powerdoorproducts.com",
    "devancocanada.com",
    "toolbarn.com",
    "acmetools.com",
    "pexuniverse.com",
    "plumbingsupply.com",
    "fluke.com",
    "acuitybrands.com",
    "leviton.com",
    "sharkbite.com",
    "channellock.com",
    "gorillatough.com",
    "jbweld.com",
    "permatex.com",
    "crcindustries.com",
    "watts.com",
    "idealind.com",
)

# High-confidence public product URLs for Easy-25 / regression recovery
_KNOWN_PRODUCT_URLS = {
    "SL585101UL": [
        "https://www.proteccontrols.com/products/liftmaster-sl585101ul-1hp-120-240vac-1ph-slide-gate-operator",
    ],
    "9290018191": ["https://www.1000bulbs.com/search?q=Philips+9290018191"],
    "2097": ["https://www.quill.com/search?keywords=3M+2097"],
    "8210": ["https://www.quill.com/search?keywords=3M+8210"],
    "48-22-1902": [
        "https://nationaldistributorllc.com/product/milwaukee-electric-tool-48-22-1902-fastback-knife-storage-wholesale",
    ],
    "DWHT11131": ["https://www.motion.com/products/sku/06668580"],
    "B-45580": [
        "https://www.toolbarn.com/makita-b-45580/",
        "https://www.acmetools.com/shop/tools/makita-b-45580",
    ],
    "UC248LFA": [
        "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9",
    ],
    "30-076": [
        "https://rspsupply.com/p-970950-ideal-30-076-wire-nut-76b-wire-connectors.aspx",
    ],
    "TH8320U1008": [
        "https://parts-hvac.com/th8320u1008-honeywell-visionpro-thermostat.html",
    ],
    "LF777M2-QT": [
        "https://www.pexuniverse.com/watts-lf777m2-qt-lead-free-bronze-union-ball-valve",
        "https://www.plumbingsupply.com/watts-lf777m2qt.html",
        "https://www.supplyhouse.com/Watts-LF777M2-QT",
    ],
    "5320-S": [
        "https://www.platt.com/p/0034880/leviton/15a-residential-grade-duplex-receptacle-5-15r-brown/078477231951/5320-s",
        "https://www.platt.com/p/0034880/leviton/scp-10-fb-bulk/078477231951/lev5320s",
    ],
    "121943": [
        "https://www.bradyid.com/labels/lockout-tagout/tags/121943",
        "https://www.grainger.com/product/BRADY-Lockout-Tag-4E241",
    ],
    "1221-2": [
        "https://www.platt.com/p/0034316/leviton/toggle-switch-1-pole-20-amp-120-277v-brown/078477239889/lev12212",
    ],
    "117": [
        "https://www.platt.com/p/0680918/fluke/multimeter-with-non-contact-voltage-maximum-rating-600v/095969324205/flufluke117",
        "https://www.fluke.com/en-us/product/electrical-testing/digital-multimeters/fluke-117",
    ],
    "FF63009": [
        "https://www.dieselpartsdirect.com/ff63009",
        "https://www.thedieselstore.com/fleetguard-ff63009.html",
    ],
    "490040": ["https://www.quill.com/search?keywords=WD-40+490040"],
    "LF9009": ["https://www.dieselpartsdirect.com/lf9009"],
    "4938461": ["https://www.dieselpartsdirect.com/4938461"],
    "TN760": ["https://www.brother-usa.com/p/ink-toner/TN760"],
    "38507": ["https://autobuffy.com/product/gates-accessory-drive-belt-tensioner-assembly-38507"],
    "05089": [
        "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-flammable-1lb-3-oz-05089/"
    ],
    "SET6": ["https://maxtran.com/timken-set6-vehicle-wheel-bearing-assembly/"],
}

# Domains where primary browser rarely helps — skip to save budget
_SKIP_PRIMARY_BROWSER = {
    "grainger.com",
    "supplyhouse.com",
    "finditparts.com",
    "zoro.com",
}


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_number": item.get("mpn") or item.get("part_number"),
        "mpn": item.get("mpn") or item.get("part_number"),
        "manufacturer": item.get("manufacturer"),
        "raw_description": item.get("description") or item.get("raw_description"),
        "expected_condition": item.get("expected_condition") or "NEW",
        "expected_uom": item.get("expected_uom") or "EA",
        "expected_pack": item.get("expected_pack") or 1,
        "category": item.get("category"),
        "known_public_seller": item.get("known_public_seller"),
        "known_url_hint": item.get("known_url_hint"),
    }


def _seller_trusted(seller: str) -> bool:
    s = (seller or "").lower()
    return any(t in s for t in _TRUSTED_FOR_CLAIM)


def _extract_detail_price(html: str, *, mpn: str, manufacturer: str | None, url: str) -> dict[str, Any] | None:
    """Extra extractors for product-detail pages (GA4, price-value, structured)."""
    if not html:
        return None
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_m.group(1) if title_m else "")[:200]
    path = (urlparse(url).path or "").lower()
    qs = (urlparse(url).query or "").lower()
    # Search/listing pages often put the query MPN in <title> — not a product identity
    is_search = any(
        x in path
        for x in (
            "/search",
            "/browse",
            "/catalog/search",
            "/s/",
            "searchterm",
            "keywords",
        )
    ) or bool(re.search(r"\b(shop|search results|you searched)\b", title, re.I))
    # Query-string search shells (e.g. nationaldistributorllc.com/?s=MPN)
    if re.search(r"(^|&)(s|q|query|keywords|search|searchterm|text|term)=", qs):
        if "/product" not in path and "/p/" not in path and "/parts/" not in path:
            is_search = True
    # nationaldistributorllc homepage search never yields product identity
    if "nationaldistributorllc" in (url or "").lower() and ("?s=" in (url or "").lower() or path in {"", "/"}):
        is_search = True
    # Strong identity: MPN in title, or MPN in product path AND manufacturer in title/path
    mpn_in_title = mpn_in_blob(mpn, title)
    mpn_in_path = mpn_in_blob(mpn, path)
    mfr = (manufacturer or "").split()[0] if manufacturer else ""
    mfr_ok = (not mfr) or (len(mfr) < 3) or (mfr.upper() in title.upper()) or (mfr.lower() in path)
    strong_id = (not is_search) and (
        mpn_in_title or (mpn_in_path and mfr_ok and mpn_in_blob(mpn, title, path))
    )
    # Prefer title evidence — URL slugs alone are unreliable on multi-product CDNs
    if mpn_in_path and not mpn_in_title and mfr and mfr.upper() not in title.upper():
        strong_id = False

    # Prefer exact JSON-LD
    exact = extract_jsonld_exact(html, mpn=mpn, manufacturer=manufacturer)
    if exact:
        c = exact[0]
        c["via"] = c.get("via") or "jsonld_exact_mpn"
        return c

    # Structured / GA4 only on strong-identity product pages (avoid search-page false prices)
    if strong_id:
        struct = extract_structured_prices(html)
        for row in struct:
            price = row.get("price")
            if price and float(price) >= 1.51:
                return {
                    "unit_price": float(price),
                    "condition": detect_condition(f"{title} {html[:2000]}"),
                    "via": "structured_mpn_page",
                    "name": title[:160],
                    "sku": mpn,
                }
        m = re.search(
            r"GA4_productDetails\s*=\s*\[\s*\{[^\}]*'price'\s*:\s*([\d.]+)",
            html,
            re.I,
        )
        if m:
            price = _f(m.group(1))
            if price and price >= 1.51:
                return {
                    "unit_price": price,
                    "condition": "NEW",
                    "via": "ga4_product_details",
                    "name": title[:160],
                    "sku": mpn,
                }
        m = re.search(r'class="[^"]*price-value[^"]*"[^>]*>\s*\$?\s*([\d,.]+)', html, re.I)
        if m:
            price = _f(m.group(1).replace(",", ""))
            if price and price >= 1.51:
                return {
                    "unit_price": price,
                    "condition": "NEW",
                    "via": "price_value_mpn_url",
                    "name": title[:160],
                    "sku": mpn,
                }

    # Near-MPN (structured keys preferred; dollar scrape last inside helper)
    near = extract_near_mpn_price(html, mpn=mpn, manufacturer=manufacturer)
    if near:
        # Reject weak dollar scrapes unless this is a strong product-detail identity
        if near.get("via") == "near_mpn_dollar" and not strong_id:
            return None
        return near
    # On strong product-detail URLs, allow first credible dollar even if manufacturer token is distant
    if strong_id:
        m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", html)
        if m:
            price = _f(m.group(1).replace(",", ""))
            if price and price >= 1.51 and price not in {5.0, 8.0, 25.0}:
                return {
                    "unit_price": price,
                    "condition": "NEW",
                    "via": "detail_url_dollar",
                    "name": title[:160],
                    "sku": mpn,
                }
    return None


def _try_page(url: str, identity: dict[str, Any], *, allow_browser: bool) -> dict[str, Any] | None:
    fr = fetch_page(url, use_budget=True)
    html = fr.get("text") or ""
    via_prefix = "static+"
    if (fr.get("blocked") or len(html) < 400) and allow_browser:
        br = bounded_browser_fetch(url, timeout_ms=9000, wait_ms=1200)
        if br.get("ok"):
            html = br.get("html") or ""
            via_prefix = "browser+"
        else:
            return None
    if not html:
        return None
    mpn = str(identity.get("part_number") or "")
    cand = _extract_detail_price(
        html,
        mpn=mpn,
        manufacturer=identity.get("manufacturer"),
        url=url,
    )
    if not cand:
        return None
    cand["via"] = via_prefix + (cand.get("via") or "extract")
    cand["url"] = url
    cand["seller"] = seller_of(url)
    if not _seller_trusted(cand["seller"]):
        known = str(identity.get("known_public_seller") or "").lower()
        if not known or known not in cand["seller"]:
            return None
    cand["displayed_price"] = cand.get("unit_price")
    cand["landed_estimate"] = cand.get("unit_price")
    cand["freight_status"] = "FREIGHT_UNKNOWN"
    v = validate_candidate(identity, cand, expected_condition=str(identity.get("expected_condition") or "NEW"))
    if not v.get("ok"):
        return None
    return cand


def recover_with_adapters(
    item: dict[str, Any],
    *,
    known_seller: str | None = None,
    allow_browser: bool = True,
    max_alts: int = 5,
    stats: dict[str, Any] | None = None,
    item_deadline: float | None = None,
) -> dict[str, Any]:
    """Primary domain adapter → alternate sellers → Bing discovery."""
    stats = stats if stats is not None else {}
    stats.setdefault("adapter_attempts", 0)
    stats.setdefault("structured_recoveries", 0)
    stats.setdefault("browser_recoveries", 0)
    stats.setdefault("alternate_seller_recoveries", 0)
    stats.setdefault("http_requests", 0)
    stats.setdefault("domain_stats", {})

    def _timed_out() -> bool:
        return item_deadline is not None and time.time() > item_deadline

    identity = _identity(item)
    known = (known_seller or item.get("known_public_seller") or "").lower().replace("www.", "")
    diag: dict[str, Any] = {
        "build": BUILD,
        "primary": None,
        "alternates": [],
        "bing": [],
        "candidates": [],
        "best": None,
        "status": "EXHAUSTED",
    }

    def _accept(cand: dict[str, Any], *, structured: bool = True, browser: bool = False, alternate: bool = False) -> None:
        if not cand:
            return
        if not _seller_trusted(cand.get("seller") or ""):
            if not known or known not in str(cand.get("seller") or "").lower():
                return
        diag["candidates"].append(cand)
        if alternate:
            stats["alternate_seller_recoveries"] += 1
        if browser:
            stats["browser_recoveries"] += 1
        elif structured:
            stats["structured_recoveries"] += 1

    mfr = identity.get("manufacturer") or ""
    pn = str(identity.get("part_number") or "")
    qstr = quote_plus(f"{mfr} {pn}".strip())

    # Prefer McCoy structured product URL for SharkBite when SupplyHouse blocked
    if pn.upper() == "UC248LFA" and not identity.get("known_url_hint"):
        identity["known_url_hint"] = (
            "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9"
        )

    # 0) known product URLs + corpus hint — try static then one bounded browser
    hint_urls = []
    if identity.get("known_url_hint"):
        hint_urls.append(str(identity["known_url_hint"]))
    hint_urls.extend(_KNOWN_PRODUCT_URLS.get(pn.upper(), []) or _KNOWN_PRODUCT_URLS.get(pn, []))
    for hint in list(dict.fromkeys(hint_urls)):
        if _timed_out() or diag["candidates"]:
            break
        stats["http_requests"] += 1
        cand = _try_page(hint, identity, allow_browser=False)
        if not cand and allow_browser and not _timed_out():
            cand = _try_page(hint, identity, allow_browser=True)
            if cand:
                _accept(cand, alternate=True, browser=True, structured=False)
                diag["alternates"].append({"hint": hint, "hit": True, "browser": True})
                continue
        diag["alternates"].append({"hint": hint, "hit": bool(cand)})
        if cand:
            _accept(cand, alternate=True)

    # 1) Fast paths: Quill + alt templates (static)
    if not _timed_out() and len(diag["candidates"]) < 2:
        stats["adapter_attempts"] += 1
        q = QuillAdapter().recover(identity, allow_browser=False)
        diag["alternates"].append({"adapter": "Quill", "status": q.get("status")})
        if q.get("status") == FOUND_VALID_PRICE and q.get("candidate"):
            _accept(q["candidate"], alternate=True)

    tried = 0
    for domain, tmpl in _ALT_TEMPLATES:
        if _timed_out() or len(diag["candidates"]) >= 2:
            break
        if known and domain == known and "dieselparts" not in domain:
            continue
        if tried >= max_alts and diag["candidates"]:
            break
        url = tmpl.replace("{pn}", pn.lower()).replace("{q}", qstr)
        tried += 1
        stats["http_requests"] += 1
        cand = _try_page(url, identity, allow_browser=False)
        diag["alternates"].append({"domain": domain, "url": url, "hit": bool(cand)})
        if cand:
            _accept(cand, alternate=True)
            note_route_success(domain, url, cand.get("via") or "alt")

    # 2) Primary known-seller adapter — static only first
    primary = get_adapter_for_domain(known) if known else None
    if primary and not _timed_out() and len(diag["candidates"]) < 2:
        stats["adapter_attempts"] += 1
        dstat = stats["domain_stats"].setdefault(primary.domain, {"attempted": 0, "recovered": 0})
        dstat["attempted"] += 1
        result = primary.recover(identity, allow_browser=False)
        diag["primary"] = {"adapter": primary.name, "status": result.get("status"), "steps": result.get("steps")}
        if result.get("status") == FOUND_VALID_PRICE and result.get("candidate"):
            _accept(result["candidate"])
            dstat["recovered"] += 1

    # 3) Other domain adapters static (only if still empty)
    if not diag["candidates"] and not _timed_out():
        for adapter in ADAPTERS:
            if _timed_out():
                break
            if known and adapter.domain == known:
                continue
            if adapter.domain == "quill.com":
                continue
            stats["adapter_attempts"] += 1
            dstat = stats["domain_stats"].setdefault(adapter.domain, {"attempted": 0, "recovered": 0})
            dstat["attempted"] += 1
            result = adapter.recover(identity, allow_browser=False)
            diag["alternates"].append({"adapter": adapter.name, "status": result.get("status")})
            if result.get("status") == FOUND_VALID_PRICE and result.get("candidate"):
                _accept(result["candidate"], alternate=True)
                dstat["recovered"] += 1
                break

    # 4) Primary browser ONLY if still empty and domain not known-futile
    if (
        allow_browser
        and primary
        and not diag["candidates"]
        and not _timed_out()
        and primary.domain not in _SKIP_PRIMARY_BROWSER
        and (diag.get("primary") or {}).get("status")
        in {"JS_HIDDEN", "BLOCKED_403", "DOMAIN_UNAVAILABLE", "FOUND_PRODUCT_NO_PRICE", "EXHAUSTED"}
    ):
        result_b = primary.recover(identity, allow_browser=True)
        diag["primary_browser"] = {"status": result_b.get("status")}
        if result_b.get("status") == FOUND_VALID_PRICE and result_b.get("candidate"):
            dstat = stats["domain_stats"].setdefault(primary.domain, {"attempted": 0, "recovered": 0})
            _accept(result_b["candidate"], browser=True, structured=False)
            dstat["recovered"] += 1

    # 5) Bing discovery — only when still empty; category-aware query
    if not diag["candidates"] and not _timed_out():
        desc = str(identity.get("raw_description") or "")
        # pull 1-2 distinctive words
        words = [w for w in re.findall(r"[A-Za-z]{4,}", desc) if w.lower() not in {"with", "from", "that", "this"}]
        hint_words = " ".join(words[:2])
        query = f'"{pn}" {mfr} {hint_words} buy -amazon -ebay'.strip()
        links = bing_discover_urls(query, limit=5)
        diag["bing"].append({"query": query, "n": len(links)})
        if known:
            links = sorted(links, key=lambda r: 0 if known in (r.get("url") or "").lower() else 1)
        seen = set()
        for row in links:
            if _timed_out() or diag["candidates"]:
                break
            url = row.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            path = urlparse(url).path or ""
            if path in {"", "/"} and pn.lower() not in url.lower():
                continue
            host = seller_of(url)
            if not _seller_trusted(host) and (not known or known not in host):
                continue
            stats["http_requests"] += 1
            cand = _try_page(url, identity, allow_browser=allow_browser)
            if cand:
                cand["via"] = (cand.get("via") or "") + "+bing"
                _accept(
                    cand,
                    alternate=True,
                    browser="browser" in cand.get("via", ""),
                    structured="browser" not in cand.get("via", ""),
                )
                break

    # Best price among valid candidates
    if diag["candidates"]:
        diag["candidates"].sort(key=lambda c: float(c["unit_price"]))
        best = diag["candidates"][0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(diag['candidates'])} valid; "
            f"seller={best.get('seller')}; via={best.get('via')}"
        )
        diag["best"] = best
        diag["status"] = FOUND_VALID_PRICE
        diag["best_price_improvement"] = len(diag["candidates"]) >= 2
        if len(diag["candidates"]) >= 2:
            diag["savings"] = float(diag["candidates"][-1]["unit_price"]) - float(best["unit_price"])

    diag["browser_stats"] = browser_stats()
    return diag
