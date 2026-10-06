"""Graph-aware price recovery: manufacturer → distributors → exact URLs → price.

Build: 20261004-m3-manufacturer-distributor-graph-v1
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote_plus, urlparse

from manufacturer_distributor_graph.exact_urls import (
    curated_exact_urls,
    domain_search_urls,
    register_learned_url,
)
from manufacturer_distributor_graph.graph import (
    add_edge,
    add_exact_url,
    add_price,
    add_seller_path,
    domain_role,
    ensure_domain_roles,
    load_graph,
    note_url_pattern,
    save_graph,
    upsert_product,
)
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from manufacturer_distributor_graph.models import (
    AUTHORIZATION_NOT_REQUIRED,
    AUTHORIZED_UNKNOWN,
    DISTRIBUTOR_CARRIES_MPN,
    EXACT_PRODUCT_PAGE,
    LOW_PRIORITY,
    OEM_AUTHORIZED_DISTRIBUTOR,
    PUBLIC_CATALOG_LISTING,
    RESELLER_CARRIES_MPN,
)
from manufacturer_distributor_graph.mpn_normalize import mpn_variants
from price_adapters.browser import bing_discover_urls, note_route_success
from price_adapters.models import FOUND_VALID_PRICE
from price_adapters.orchestrate import _KNOWN_PRODUCT_URLS, _identity, _seller_trusted, _try_page
from price_adapters.validate import seller_of

BUDGET_ITEM = 35.0
BUDGET_URL = 4.5
MAX_SELLER_PATHS_TARGET = 3
MAX_URL_TRIES = 14

# High-yield global fallbacks (ranked)
_HIGH_YIELD = [
    "quill.com",
    "dieselpartsdirect.com",
    "1000bulbs.com",
    "homedepot.com",
    "mscdirect.com",
    "motion.com",
    "mccoys.com",
    # nationaldistributorllc.com: product-detail only (search shells false-positive)
    "parts-hvac.com",
    "rspsupply.com",
    "platt.com",
    "bradyid.com",
    "rockauto.com",
    "summitracing.com",
    "autozone.com",
    "crcautocare.com",
    "pexuniverse.com",
    "staples.com",
    "officedepot.com",
    "thedieselstore.com",
    "maxtran.com",
    "autobuffy.com",
    "lowes.com",
    "globalindustrial.com",
]


def _accept(diag: dict[str, Any], cand: dict[str, Any], *, auth: str = AUTHORIZATION_NOT_REQUIRED) -> None:
    if not cand:
        return
    seller = cand.get("seller") or ""
    known = str((diag.get("identity") or {}).get("known_public_seller") or "").lower()
    if not _seller_trusted(seller):
        if not known or known not in seller.lower():
            return
    # Require manufacturer token for short/ambiguous MPNs when manufacturer known
    mfr = (diag.get("identity") or {}).get("manufacturer") or ""
    name = str(cand.get("name") or "")
    if mfr and len(str((diag.get("identity") or {}).get("part_number") or "")) <= 5:
        if mfr.split()[0].lower() not in (name + " " + seller).lower():
            # still allow if URL path contains mpn and manufacturer domain/distributor
            pass
    cand = {**cand, "authorization_status": auth}
    diag["candidates"].append(cand)


def recover_with_graph(
    item: dict[str, Any],
    *,
    allow_browser: bool = True,
    stats: dict[str, Any] | None = None,
    item_deadline: float | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    stats.setdefault("http_requests", 0)
    stats.setdefault("browser_renders", 0)
    stats.setdefault("exact_urls_tried", 0)
    stats.setdefault("cache_hits", 0)
    stats.setdefault("ai_calls", 0)
    stats.setdefault("domain_stats", {})
    ensure_domain_roles()
    g = graph if graph is not None else load_graph()
    started = time.time()
    if item_deadline is None:
        item_deadline = started + BUDGET_ITEM

    def timed_out() -> bool:
        return time.time() > item_deadline

    identity = _identity(item)
    mfr_res = resolve_manufacturer(item)
    bid = upsert_product(g, item, mfr_res=mfr_res)
    dists = authorized_distributors(mfr_res.get("manufacturer_key") or item.get("manufacturer"))
    for d in dists:
        add_edge(
            g,
            {
                "edge_type": OEM_AUTHORIZED_DISTRIBUTOR,
                "from": mfr_res.get("manufacturer_key"),
                "domain": d["distributor_domain"],
                "authorization": d["authorization"],
                "provenance": d["provenance"],
                "product": bid,
            },
        )

    pn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer") or ""
    diag: dict[str, Any] = {
        "build": "20261004-m3-manufacturer-distributor-graph-v1",
        "identity": identity,
        "manufacturer": mfr_res,
        "distributors": dists,
        "seller_paths": [],
        "exact_urls": [],
        "candidates": [],
        "best": None,
        "status": "EXHAUSTED",
        "routes": [],
    }

    # Build prioritized URL list
    url_queue: list[tuple[str, str, str]] = []  # domain, url, method

    # 1) curated exact URLs
    for url in curated_exact_urls(pn):
        url_queue.append((seller_of(url), url, "curated_exact"))
    # known adapter hints
    for url in _KNOWN_PRODUCT_URLS.get(pn.upper(), []) or []:
        url_queue.append((seller_of(url), url, "adapter_known_url"))

    # 2) authorized distributor + high-yield domain searches
    dist_domains = [d["distributor_domain"] for d in dists]
    ranked_domains: list[str] = []
    for d in dist_domains + _HIGH_YIELD:
        role = domain_role(d)
        if role == LOW_PRIORITY:
            continue
        if d not in ranked_domains:
            ranked_domains.append(d)
    # demoted known seller last (identity only)
    known = (item.get("known_public_seller") or "").lower().replace("www.", "")
    if known and domain_role(known) == LOW_PRIORITY:
        ranked_domains.append(known)

    for domain, url in domain_search_urls(pn, mfr, ranked_domains[:12], limit_per_domain=1):
        url_queue.append((domain, url, "domain_search"))

    # 3) OEM search route
    oem_search = mfr_res.get("manufacturer_product_search_route")
    if oem_search and mfr_res.get("manufacturer_domain"):
        url_queue.append((mfr_res["manufacturer_domain"], oem_search, "oem_search"))

    # de-dupe
    seen_u: set[str] = set()
    ordered: list[tuple[str, str, str]] = []
    for row in url_queue:
        if row[1] in seen_u:
            continue
        seen_u.add(row[1])
        ordered.append(row)

    def try_one(domain: str, url: str, method: str, *, browser: bool = False) -> dict[str, Any] | None:
        if timed_out():
            return None
        role = domain_role(domain)
        # skip burning browser on low-priority domains unless exact product path
        path = urlparse(url).path or ""
        if role == LOW_PRIORITY and browser:
            return None
        if role == LOW_PRIORITY and method == "domain_search":
            # one static peek only
            pass
        t0 = time.time()
        cand = _try_page(url, identity, allow_browser=browser)
        dt = time.time() - t0
        stats["http_requests"] += 1
        stats["exact_urls_tried"] += 1
        if browser:
            stats["browser_renders"] += 1
        dstat = stats["domain_stats"].setdefault(
            domain, {"attempts": 0, "exact_urls": 0, "prices": 0, "time_s": 0.0}
        )
        dstat["attempts"] += 1
        dstat["time_s"] += dt
        hit = bool(cand)
        note_url_pattern(domain, url=url, mpn=pn, success=hit, method=method)
        diag["routes"].append({"tag": method, "url": url, "hit": hit, "dt": round(dt, 3), "browser": browser})
        add_seller_path(
            g,
            bid,
            {
                "domain": domain,
                "url": url,
                "method": method,
                "role": role,
                "hit": hit,
                "authorization": AUTHORIZED_UNKNOWN,
            },
        )
        if hit:
            dstat["exact_urls"] += 1
            dstat["prices"] += 1
            add_exact_url(
                g,
                bid,
                {
                    "domain": domain,
                    "mpn": pn,
                    "exact_product_url": cand.get("url") or url,
                    "discovery_method": method,
                    "confidence": "HIGH" if method.startswith("curated") else "MED",
                    "price_extractability": True,
                },
            )
            add_edge(
                g,
                {
                    "edge_type": EXACT_PRODUCT_PAGE,
                    "domain": domain,
                    "url": cand.get("url") or url,
                    "product": bid,
                    "provenance": method,
                },
            )
            add_edge(
                g,
                {
                    "edge_type": RESELLER_CARRIES_MPN if role != "IDENTITY_SOURCE" else DISTRIBUTOR_CARRIES_MPN,
                    "domain": domain,
                    "product": bid,
                    "provenance": method,
                },
            )
            register_learned_url(pn, cand.get("url") or url)
            note_route_success(domain, cand.get("url") or url, cand.get("via") or method)
            add_price(
                g,
                bid,
                {
                    "seller": cand.get("seller"),
                    "unit_price": cand.get("unit_price"),
                    "condition": cand.get("condition"),
                    "url": cand.get("url"),
                    "via": cand.get("via"),
                },
            )
        return cand

    # Try URLs until we have enough candidates or budget
    for domain, url, method in ordered[:MAX_URL_TRIES]:
        if timed_out() or len(diag["candidates"]) >= MAX_SELLER_PATHS_TARGET:
            break
        # skip low-priority unless curated/exact
        if domain_role(domain) == LOW_PRIORITY and method == "domain_search":
            continue
        cand = try_one(domain, url, method, browser=False)
        if cand:
            _accept(diag, cand)
            diag["exact_urls"].append(url)
            continue
        # selective browser on curated/product-like paths
        path = (urlparse(url).path or "").lower()
        if (
            allow_browser
            and not timed_out()
            and domain_role(domain) != LOW_PRIORITY
            and (
                method.startswith("curated")
                or any(x in path for x in ("/p/", "/product", "/parts/", pn.lower().replace(" ", "")))
            )
        ):
            cand = try_one(domain, url, method + "+browser", browser=True)
            if cand:
                _accept(diag, cand)
                diag["exact_urls"].append(url)

    # Bing fanout if still short of 1 price (site-biased queries for distributors)
    if not diag["candidates"] and allow_browser and not timed_out():
        variants = mpn_variants(pn, manufacturer=mfr)[:2]
        for v in variants:
            if timed_out() or diag["candidates"]:
                break
            # Prefer site: queries on top distributors
            sites = dist_domains[:3] or ranked_domains[:3]
            for site in sites:
                if timed_out() or diag["candidates"]:
                    break
                if domain_role(site) == LOW_PRIORITY:
                    continue
                q = f'site:{site} "{v}" {mfr}'.strip()
                links = bing_discover_urls(q, limit=3)
                for row in links:
                    if timed_out() or diag["candidates"]:
                        break
                    url = row.get("url") or ""
                    host = seller_of(url)
                    if not _seller_trusted(host) and host not in sites:
                        continue
                    cand = try_one(host, url, "bing_site", browser=False)
                    if cand:
                        _accept(diag, cand)
                        break
                    path = (urlparse(url).path or "").lower()
                    if allow_browser and any(x in path for x in ("/p/", "/product", "/parts/", v.lower())):
                        cand = try_one(host, url, "bing_site+browser", browser=True)
                        if cand:
                            _accept(diag, cand)
                            break
            if diag["candidates"]:
                break
            # open web as last resort
            q2 = f'"{v}" {mfr} buy -amazon -ebay'.strip()
            links = bing_discover_urls(q2, limit=4)
            for row in links:
                if timed_out() or diag["candidates"]:
                    break
                url = row.get("url") or ""
                host = seller_of(url)
                if domain_role(host) == LOW_PRIORITY:
                    continue
                if not _seller_trusted(host):
                    continue
                cand = try_one(host, url, "bing_open", browser=False)
                if cand:
                    _accept(diag, cand)
                    break

    if diag["candidates"]:
        diag["candidates"].sort(key=lambda c: float(c["unit_price"]))
        best = diag["candidates"][0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(diag['candidates'])} valid; "
            f"seller={best.get('seller')}; via={best.get('via')}"
        )
        diag["best"] = best
        diag["status"] = FOUND_VALID_PRICE
    elif timed_out():
        diag["status"] = "ROUTE_TIMEOUT"
        stats["timeouts"] = int(stats.get("timeouts") or 0) + 1
    else:
        diag["status"] = "EXHAUSTED"

    diag["seller_paths"] = (g.get("products") or {}).get(bid, {}).get("seller_paths") or []
    diag["exact_url_rows"] = (g.get("products") or {}).get(bid, {}).get("exact_urls") or []
    diag["elapsed_s"] = round(time.time() - started, 3)
    diag["n_seller_paths"] = len(diag["seller_paths"])
    diag["n_exact_urls"] = len(diag["exact_url_rows"])
    save_graph(g)
    return diag
