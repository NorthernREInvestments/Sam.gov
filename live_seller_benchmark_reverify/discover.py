"""Expanded live seller discovery for unresolved / contradiction items."""

from __future__ import annotations

import time
from typing import Any

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from live_exact_priced_pdp.audit import audit_url
from live_seller_benchmark_reverify.domains import is_demoted, note_domain
from live_seller_benchmark_reverify.models import (
    CATEGORY_EXPANDED_POOLS,
    DISCOVERY_BUDGET_S,
    MAX_CANDIDATES_VALIDATE,
    MAX_DISCOVERY_QUERIES,
    MAX_SELLER_DOMAINS,
    PREFERRED_PRICEABLE,
    STOP_AT_EXTRACTABLE,
)
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from open_seller_expansion.fingerprint import build_fingerprint, enrich_upc_from_html, try_upc_from_identity_url
from open_seller_expansion.pools import seller_pool_for_item
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from open_web_product_discovery.routing import resolve_category
from open_web_product_discovery.transport import search_open_web
from price_adapters.validate import seller_of
from public_price_search.search import fetch_page

from live_exact_priced_pdp.models import EXTRACTABLE_STATES

try:
    from known_pdp_price_extraction.rediscover import shopify_suggest_products
except Exception:  # pragma: no cover
    shopify_suggest_products = None  # type: ignore


def _add(bag: list[dict[str, Any]], seen: set[str], url: str, method: str) -> None:
    if not url or not str(url).startswith("http") or url in seen or is_search_shell(url):
        return
    dom = seller_of(url)
    if is_demoted(dom):
        return
    seen.add(url)
    bag.append({"url": url, "domain": dom, "discovery_method": method})


def build_queries(item: dict[str, Any], fp: dict[str, Any]) -> list[dict[str, str]]:
    mpn = str(item.get("mpn") or "")
    mfr = str(item.get("manufacturer") or "")
    title = fp.get("canonical_title") or f"{mfr} {mpn}".strip()
    upc = fp.get("upc") or fp.get("gtin") or ""
    qs: list[dict[str, str]] = []
    if mfr and mpn:
        qs.extend(
            [
                {"type": "price", "query": f'{mfr} "{mpn}" price'},
                {"type": "buy", "query": f'{mfr} "{mpn}" buy'},
                {"type": "stock", "query": f'{mfr} "{mpn}" "in stock"'},
                {"type": "distributor", "query": f'{mfr} "{mpn}" distributor'},
                {"type": "cart", "query": f'"{mpn}" "Add to Cart"'},
            ]
        )
    if title and len(title) > 8:
        qs.append({"type": "canonical_title", "query": f'"{title[:80]}"'})
    if upc:
        qs.append({"type": "upc", "query": f'"{upc}"'})
        if mfr:
            qs.append({"type": "upc_mfr", "query": f"{mfr} {upc}"})
    return qs[:MAX_DISCOVERY_QUERIES]


def _seller_domains(item: dict[str, Any]) -> list[str]:
    cat = resolve_category(item)
    out: list[str] = []
    seen: set[str] = set()

    def add(d: str) -> None:
        d = (d or "").replace("www.", "").lower()
        if not d or d in seen or is_demoted(d):
            return
        seen.add(d)
        out.append(d)

    for d in PREFERRED_PRICEABLE:
        add(d)
    for d in CATEGORY_EXPANDED_POOLS.get(cat) or CATEGORY_EXPANDED_POOLS["default"]:
        add(d)
    for d in seller_pool_for_item(item):
        add(d)
    try:
        mfr_res = resolve_manufacturer(item)
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or item.get("manufacturer"))[:8]:
            if isinstance(dist, dict):
                add(str(dist.get("distributor_domain") or dist.get("domain") or ""))
            elif isinstance(dist, (list, tuple)):
                add(str(dist[0]))
            else:
                add(str(dist))
    except Exception:
        pass
    return out[:MAX_SELLER_DOMAINS]


def _try_upc_enrich(item: dict[str, Any], fp: dict[str, Any], deadline: float) -> dict[str, Any]:
    if fp.get("upc") or fp.get("gtin") or time.time() > deadline:
        return fp
    # Try known hint / soft PDPs for structured UPC only (identity reference)
    for p in item.get("pdps") or []:
        url = p.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            fr = fetch_page(url, use_budget=True)
            html = fr.get("text") or ""
            if html:
                fp = enrich_upc_from_html(html, fp)
                if fp.get("upc") or fp.get("gtin"):
                    return fp
                fp = try_upc_from_identity_url(url, fp)
                if fp.get("upc") or fp.get("gtin"):
                    return fp
        except Exception:
            continue
        if time.time() > deadline:
            break
    return fp


def discover_live_sellers(item: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
    started = time.time()
    if deadline is None:
        deadline = started + DISCOVERY_BUDGET_S
    fp = build_fingerprint(item)
    fp = _try_upc_enrich(item, fp, deadline)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    authorized_hits: list[dict[str, Any]] = []

    # Preferred / category / authorized internal search
    for dom in _seller_domains(item):
        if time.time() > deadline:
            break
        if shopify_suggest_products and dom in {"lightbulbs.com", "pksafety.com"}:
            for u in shopify_suggest_products(dom, f"{item.get('manufacturer')} {item.get('mpn')}", limit=2):
                _add(candidates, seen, u, f"SHOPIFY:{dom}")
        for row in convert_internal_search(dom, mpn=str(item.get("mpn") or ""), manufacturer=str(item.get("manufacturer") or ""), limit=2):
            _add(candidates, seen, row.get("url") or "", f"INTERNAL:{dom}")

    if time.time() < deadline and not is_demoted("platt.com"):
        for row in platt_suggest_urls(str(item.get("mpn") or ""), manufacturer=str(item.get("manufacturer") or ""), limit=3):
            _add(candidates, seen, row.get("url") or "", "PLATT_GRAPHQL")

    # Authorized distributor bookkeeping
    try:
        mfr_res = resolve_manufacturer(item)
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or item.get("manufacturer"))[:8]:
            if isinstance(dist, dict):
                authorized_hits.append(
                    {
                        "manufacturer": mfr_res.get("manufacturer_key"),
                        "distributor": dist.get("distributor_domain") or dist.get("domain"),
                        "status": dist.get("status"),
                    }
                )
            elif isinstance(dist, (list, tuple)):
                authorized_hits.append(
                    {
                        "manufacturer": mfr_res.get("manufacturer_key"),
                        "distributor": dist[0],
                        "status": dist[1] if len(dist) > 1 else None,
                    }
                )
    except Exception:
        pass

    # Open-web live-priced queries (Bing Playwright when needed)
    for qrow in build_queries(item, fp):
        if time.time() > deadline or len(candidates) >= MAX_CANDIDATES_VALIDATE * 2:
            break
        prefer_pw = qrow.get("type") in {"price", "buy", "cart", "upc", "canonical_title"}
        res = search_open_web(qrow["query"], limit=6, prefer_playwright=prefer_pw)
        for hit in res.get("results") or []:
            _add(candidates, seen, hit.get("url") or "", f"SERP:{qrow.get('type')}")

    # Audit to live extractable states
    audited: list[dict[str, Any]] = []
    extractable: list[dict[str, Any]] = []
    for cand in candidates[:MAX_CANDIDATES_VALIDATE]:
        if time.time() > deadline:
            break
        audit = audit_url(cand["url"], item, allow_browser=True, soft=False)
        audit["discovery_method"] = cand.get("discovery_method")
        audited.append(audit)
        note_domain(
            audit.get("domain") or cand.get("domain") or "",
            state=str(audit.get("state") or ""),
            extractable=bool(audit.get("extractable")),
            priced=False,
            category=str(item.get("category") or ""),
            manufacturer=str(item.get("manufacturer") or ""),
            offer_format=str(audit.get("offer_status") or ""),
            search_route=str(cand.get("discovery_method") or ""),
        )
        if audit.get("state") in EXTRACTABLE_STATES:
            extractable.append(audit)
            if len(extractable) >= STOP_AT_EXTRACTABLE:
                break

    return {
        "fingerprint": {k: fp.get(k) for k in ("canonical_title", "upc", "gtin", "mpn", "manufacturer")},
        "candidates": candidates[:40],
        "audited": audited,
        "extractable": extractable,
        "authorized_distributors": authorized_hits,
        "n_candidates": len(candidates),
        "n_extractable": len(extractable),
        "upc_recovered": bool(fp.get("upc") or fp.get("gtin")),
        "elapsed_s": round(time.time() - started, 2),
    }
