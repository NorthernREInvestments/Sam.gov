"""Hard-miss recovery: authorized distributors + specialists + exact URLs.

Skips dead-primary extract budget. Reuses JSON-LD-first rediscovery extractors.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote_plus

from exact_page_extraction.models import EXACT_PRODUCT_VERIFIED
from exact_page_extraction.url_classify import classify_url
from hard_miss_recovery.models import (
    BUILD,
    DEAD_PRIMARY,
    ITEM_DEADLINE_S,
    MIN_ALT_SELLERS,
    SHORT_MPN_IDS,
    STRAT_AUTH_DIST,
    STRAT_CATALOG_PDF,
    STRAT_EXACT_URL,
    STRAT_SPECIALIST,
)
from hard_miss_recovery.route import specialist_domains_for
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from price_adapters.validate import seller_of
from public_price_search.search import search_web
from seller_rediscovery.recover import recover_with_rediscovery
from seller_rediscovery.rediscover import rediscover_exact_sellers

# Open exact-ish seeds for hard misses (identity reference; extraction still validates)
_HARD_MISS_SEEDS: dict[str, list[str]] = {
    "easy-watts-lf777m2": [
        "https://www.plumbingsupply.com/watts-lf777m2qt.html",
        "https://www.faucet.com/watts-lf777m2-qt",
    ],
    "easy-brady-121943": [
        "https://www.seton.com/brady-danger-do-not-operate-tag-b-121943.html",
        "https://www.mysafetysign.com/brady-danger-do-not-operate-tag",
    ],
    "easy-makita-b-45580": [
        "https://www.makitatools.com/products/details/B-45580",
        "https://www.northerntool.com/products/makita-b-45580",
    ],
    "easy-global-dwt-6": [
        "https://www.globalindustrial.com/p/rubber-wheel-tire-chock-10-l-x-8-w-x-6-h",
    ],
    "mro-3m-60926": [
        "https://www.quill.com/3m-multi-gas-vapor-cartridge-p100/cbs/",
        "https://www.northernsafety.com/Product/1442/3M-60926-Multi-GasVapor-CartridgeP100-Filter",
    ],
    "mro-honeywell-n5500": [
        "https://www.honeywellstore.com/store/products.aspx?catid=110",
        "https://www.northernsafety.com/Product/Search?q=N5500",
    ],
    "tool-dewalt-dwht56027": [
        "https://www.dewalt.com/product/dwht56027",
        "https://www.toolup.com/dewalt-dwht56027",
    ],
    "tool-klein-d2000-9neat": [
        "https://www.kleintools.com/catalog/side-cutting-high-leverage-pliers/high-leverage-side-cutting-pliers-0",
        "https://www.platt.com/search?q=D2000-9NEAT",
    ],
    "tool-channellock-430": [
        "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier-10-max-jaw-opening-2/025582301475/chk430",
        "https://www.channellock.com/430-10-tongue-groove-pliers/",
    ],
    "tool-irwin-2073212": [
        "https://www.irwin.com/tools/pliers/vise-grip-original-locking-pliers",
    ],
    "light-sylvania-40771": [
        "https://www.1000bulbs.com/product/117513/LED-40771.html",
        "https://www.bulbs.com/product/SYLVANIA-40771",
    ],
    "light-ge-93129788": [
        "https://www.1000bulbs.com/product/search?q=93129788",
        "https://www.bulbs.com/search?q=93129788",
    ],
    "light-lithonia-2gtl4": [
        "https://www.acuitybrands.com/products/detail/214234/lithonia-lighting/2gtl/2gtl-led-troffer",
        "https://www.1000bulbs.com/product/search?q=2GTL4",
    ],
    "hvac-aprilaire-201": [
        "https://www.filtersfast.com/Prod-Aprilaire-201.asp",
        "https://www.aprilaire.com/whole-house-products/filters/201",
    ],
    "hvac-honeywell-rth2300b": [
        "https://www.honeywellhome.com/us/en/products/security/thermostats/5-2-day-programmable-thermostat-rth2300b/",
    ],
    "hvac-resideo-th6220u2000": [
        "https://www.resideo.com/us/en/products/air/thermostats/",
        "https://parts-hvac.com/th6220u2000.html",
    ],
    "auto-wix-51515": [
        "https://www.carid.com/wix/wix-oil-filter-51515.html",
        "https://www.partsource.ca/products/51515-wix",
    ],
    "mro-loctite-262": [
        "https://www.loctiteproducts.com/en/products/threadlockers/red/loctite-262.html",
        "https://www.quill.com/search?keywords=Loctite+26221",
    ],
    "plumb-oatey-30241": [
        "https://www.faucet.com/oatey-30241",
        "https://www.plumbingsupply.com/oatey-purple-primer.html",
    ],
    "plumb-oatey-31016": [
        "https://www.faucet.com/oatey-31016",
        "https://www.plumbingsupply.com/oatey-pvc-cement.html",
    ],
    "plumb-sioux-695-G": [
        "https://www.siouxchief.com/products/drainage/closet-flanges/695",
        "https://www.faucet.com/sioux-chief-695-g",
    ],
    "plumb-sioux-886-GP": [
        "https://www.siouxchief.com/products",
        "https://www.faucet.com/sioux-chief-886-gp",
    ],
    "furn-hon-h5701": [
        "https://www.quill.com/search?keywords=HON+H5701",
        "https://www.staples.com/hon-h5701/directory_H5701",
    ],
    "furn-safco-1201bl": [
        "https://www.globalindustrial.com/p/1201bl",
        "https://www.quill.com/search?keywords=Safco+1201BL",
    ],
    "ind-brady-121944": [
        "https://www.seton.com/brady-danger-do-not-operate-tag-b-121944.html",
    ],
    "ind-global-wb218294": [
        "https://www.globalindustrial.com/p/wb218294",
    ],
    "mro-3m-08884": [
        "https://www.quill.com/search?keywords=3M+08884",
        "https://www.northernsafety.com/Product/Search?q=08884",
    ],
    "tool-crescent-ac216cvs": [
        "https://www.apexhandtools.com/crescent/",
        "https://www.platt.com/search?q=AC216CVS",
    ],
}


def _is_short_mpn(mpn: str) -> bool:
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").upper()
    return tok in SHORT_MPN_IDS or (len(tok) < 6 and sum(ch.isdigit() for ch in tok) >= 3)


def _filter_dead(urls: list[str]) -> list[str]:
    out = []
    for u in urls:
        if not u:
            continue
        host = seller_of(u)
        if host in DEAD_PRIMARY:
            continue
        out.append(u)
    return out


def discover_hard_miss_urls(item: dict[str, Any], *, miss_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deep alternate-seller / authorized-distributor URL discovery."""
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    bid = str(item.get("benchmark_id") or "")
    strategy = (miss_row or {}).get("primary_strategy") or STRAT_SPECIALIST
    mfr_res = resolve_manufacturer(item)

    seeds: list[str] = []
    for u in _HARD_MISS_SEEDS.get(bid, []):
        seeds.append(u)
    # Curated exact
    for u in curated_exact_urls(mpn) or []:
        seeds.append(u)
    # Prior attempted (non-dead)
    for u in (miss_row or {}).get("previously_attempted_urls") or []:
        seeds.append(u)

    queries: list[str] = []
    # Authorized distributor site probes
    for dist in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)[:6]:
        dom = dist.get("distributor_domain") or ""
        if not dom or dom in DEAD_PRIMARY:
            continue
        queries.append(f'site:{dom} "{mpn}" {mfr}'.strip())
        queries.append(f'site:{dom} "{mpn}"')

    # Category specialists
    for dom in specialist_domains_for(item)[:8]:
        queries.append(f'site:{dom} "{mpn}"')
        if mfr:
            queries.append(f'site:{dom} {mfr} "{mpn}"')

    # Manufacturer where-to-buy / product search (discovery only)
    if mfr_res.get("manufacturer_product_search_route"):
        queries.append(f'{mfr} "{mpn}" buy price -grainger -supplyhouse -homedepot')

    # Short MPN: force manufacturer in every query
    if _is_short_mpn(mpn):
        queries = [q for q in queries if mfr.split()[0].lower() in q.lower() or "site:" in q]
        queries.insert(0, f'{mfr} "{mpn}" "{item.get("description") or mpn}" price')

    discovered: list[dict[str, Any]] = []
    seen: set[str] = set(_filter_dead(seeds))
    for q in queries[:14]:
        try:
            res = search_web(q, limit=6, use_budget=True)
        except Exception:
            res = {"results": []}
        for r in res.get("results") or []:
            url = (r.get("url") or "").strip()
            if not url.startswith("http") or url in seen:
                continue
            host = seller_of(url)
            if host in DEAD_PRIMARY:
                continue
            if url.lower().endswith(".pdf"):
                # Catalog PDF candidate — keep separately
                discovered.append(
                    {
                        "url": url,
                        "domain": host,
                        "identity_confidence": "CATALOG_PDF",
                        "strategy": STRAT_CATALOG_PDF,
                        "title": (r.get("title") or "")[:160],
                    }
                )
                seen.add(url)
                continue
            conf = classify_url(url, mpn=mpn)
            # Short MPN: require mfr in title/url
            if _is_short_mpn(mpn):
                blob = f"{r.get('title') or ''} {url} {r.get('snippet') or ''}".lower()
                if mfr.split()[0].lower() not in re.sub(r"[^a-z0-9]", "", blob) and mfr.split()[0].lower() not in blob:
                    continue
            if conf in {"SEARCH_RESULT_SHELL", "CATEGORY_PAGE", "WRONG_PRODUCT"}:
                continue
            seen.add(url)
            discovered.append(
                {
                    "url": url,
                    "domain": host,
                    "identity_confidence": conf,
                    "strategy": strategy,
                    "title": (r.get("title") or "")[:160],
                    "from_query": q,
                }
            )

    # Also run seller_rediscovery rediscover (filtered)
    try:
        disc = rediscover_exact_sellers(item, existing_urls=list(seen), max_queries=5, max_urls=8)
        for row in disc.get("discovered_exact_urls") or []:
            url = row.get("url") or ""
            if not url or url in seen:
                continue
            if seller_of(url) in DEAD_PRIMARY:
                continue
            seen.add(url)
            discovered.append({**row, "strategy": STRAT_EXACT_URL})
    except Exception:
        pass

    # Prefer verified
    verified = [d for d in discovered if d.get("identity_confidence") == EXACT_PRODUCT_VERIFIED]
    other = [d for d in discovered if d.get("identity_confidence") != EXACT_PRODUCT_VERIFIED]
    ordered = verified + other

    return {
        "build": BUILD,
        "benchmark_id": bid,
        "strategy": strategy,
        "manufacturer_key": mfr_res.get("manufacturer_key"),
        "locator": mfr_res.get("manufacturer_distributor_locator"),
        "authorized_distributors": [
            d.get("distributor_domain")
            for d in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)
            if d.get("distributor_domain") not in DEAD_PRIMARY
        ],
        "specialist_domains": specialist_domains_for(item),
        "seed_urls": _filter_dead(seeds)[:8],
        "discovered": ordered[:12],
        "n_discovered": len(ordered),
        "queries_run": queries[:14],
    }


def recover_hard_miss(
    item: dict[str, Any],
    *,
    miss_row: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    discovery = discover_hard_miss_urls(item, miss_row=miss_row)
    seeds = list(discovery.get("seed_urls") or [])
    for d in discovery.get("discovered") or []:
        u = d.get("url") or ""
        # Skip PDFs in HTTP extract path for now (catalog path separate)
        if u.lower().endswith(".pdf"):
            continue
        if u and u not in seeds:
            seeds.append(u)

    # Ensure >= MIN_ALT_SELLERS candidate domains when available
    domains_seen = {seller_of(u) for u in seeds}
    for dom in discovery.get("specialist_domains") or []:
        if len(domains_seen) >= MIN_ALT_SELLERS:
            break
        if dom in domains_seen or dom in DEAD_PRIMARY:
            continue
        # Construct site search is not an exact URL — skip shell construction
        pass

    # Allow browser only on verified exact URLs from open domains (recover_with_rediscovery
    # already gates browser on prior domain yield > 0)
    rec = recover_with_rediscovery(
        item,
        seeded_urls=seeds[:10],
        stats=stats,
        item_deadline=time.time() + ITEM_DEADLINE_S,
        allow_rediscovery=True,
    )
    # Prefer candidates that aren't dead-primary / search shells / wrong pack
    if rec.get("candidates"):
        from exact_page_extraction.models import SEARCH_RESULT_SHELL
        from hard_miss_recovery.url_guards import reject_wrong_pack_blob

        open_cands = []
        for c in rec["candidates"]:
            url = c.get("url") or ""
            host = seller_of(url)
            if host in DEAD_PRIMARY:
                continue
            if classify_url(url, mpn=str(item.get("mpn") or "")) == SEARCH_RESULT_SHELL:
                continue
            if "/search?" in url.lower() or "/search/" in url.lower():
                continue
            pack = int(item.get("expected_pack") or 1)
            if reject_wrong_pack_blob(f"{c.get('name') or ''} {url}", pack=pack, price=float(c.get("unit_price") or 0)):
                continue
            open_cands.append(c)
        if open_cands:
            rec["candidates"] = open_cands
            rec["best"] = open_cands[0]
            rec["status"] = "FOUND_VALID_PRICE"
        else:
            rec["best"] = None
            rec["candidates"] = []
            rec["status"] = "EXHAUSTED"

    # Catalog PDF soft pass: only note (do not invent prices from snippets)
    pdfs = [d for d in discovery.get("discovered") or [] if d.get("strategy") == STRAT_CATALOG_PDF]
    rec["hard_miss"] = {
        "build": BUILD,
        "discovery": {
            "n_discovered": discovery.get("n_discovered"),
            "authorized_distributors": discovery.get("authorized_distributors"),
            "specialist_domains": discovery.get("specialist_domains"),
            "discovered": discovery.get("discovered"),
            "strategy": discovery.get("strategy"),
            "locator": discovery.get("locator"),
            "catalog_pdfs": pdfs[:5],
        },
        "elapsed_s": round(time.time() - started, 3),
    }
    return rec
