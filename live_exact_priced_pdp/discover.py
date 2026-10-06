"""Discover LIVE exact priced PDPs (not soft URL guesses)."""

from __future__ import annotations

import time
from typing import Any

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from live_exact_priced_pdp.audit import audit_url
from live_exact_priced_pdp.models import (
    EXTRACTABLE_STATES,
    ITEM_DEADLINE_S,
    MAX_CANDIDATES_VALIDATE,
    MAX_DISCOVERY_QUERIES,
    PREFERRED_LIVE_SELLERS,
    STOP_DISCOVERY_AT_EXTRACTABLE,
)
from live_exact_priced_pdp.score import is_demoted, note_seller_outcome
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from open_web_product_discovery.transport import search_open_web
from price_adapters.validate import seller_of

try:
    from known_pdp_price_extraction.rediscover import shopify_suggest_products
except Exception:  # pragma: no cover
    shopify_suggest_products = None  # type: ignore


def _add(bag: list[dict[str, Any]], seen: set[str], url: str, method: str) -> None:
    if not url.startswith("http") or url in seen or is_search_shell(url):
        return
    if is_demoted(seller_of(url)):
        return
    seen.add(url)
    bag.append({"url": url, "domain": seller_of(url), "discovery_method": method})


def build_live_queries(item: dict[str, Any]) -> list[str]:
    mpn = str(item.get("mpn") or "").strip()
    mfr = str(item.get("manufacturer") or "").strip()
    qs = []
    if mfr and mpn:
        qs.extend(
            [
                f'{mfr} "{mpn}" price',
                f'{mfr} "{mpn}" buy',
                f'{mfr} "{mpn}" "in stock"',
                f'{mfr} "{mpn}" distributor',
                f'{mfr} "{mpn}" supplier',
                f'"{mpn}" "Add to Cart"',
                f'{mfr} "{mpn}" "Add to Cart"',
            ]
        )
    return qs[:MAX_DISCOVERY_QUERIES]


def discover_live_priced_candidates(item: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
    """Find candidate URLs likely to be live exact priced PDPs."""
    started = time.time()
    if deadline is None:
        deadline = started + ITEM_DEADLINE_S
    mpn = str(item.get("mpn") or "")
    mfr = str(item.get("manufacturer") or "")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Prefer historically priceable sellers first
    for dom in PREFERRED_LIVE_SELLERS:
        if time.time() > deadline:
            break
        if is_demoted(dom):
            continue
        if shopify_suggest_products and dom in {"lightbulbs.com", "pksafety.com"}:
            for u in shopify_suggest_products(dom, f"{mfr} {mpn}", limit=3):
                _add(candidates, seen, u, "SHOPIFY_SUGGEST")
        for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
            _add(candidates, seen, row.get("url") or "", "INTERNAL_SEARCH")

    # Platt GraphQL (known live priced)
    if time.time() < deadline and not is_demoted("platt.com"):
        for row in platt_suggest_urls(mpn, manufacturer=mfr, limit=3):
            _add(candidates, seen, row.get("url") or "", "PLATT_GRAPHQL")

    # Authorized distributors (skip demoted)
    try:
        mfr_res = resolve_manufacturer(item)
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)[:4]:
            dom = str((dist or {}).get("distributor_domain") or "")
            if not dom or is_demoted(dom):
                continue
            for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=1):
                _add(candidates, seen, row.get("url") or "", "AUTHORIZED_DIST")
    except Exception:
        pass

    # Curated exact URLs — still only candidates until live audit
    for u in curated_exact_urls(mpn) or []:
        _add(candidates, seen, u, "CURATED")

    # Open-web live-priced queries
    for q in build_live_queries(item):
        if time.time() > deadline or len(candidates) >= MAX_CANDIDATES_VALIDATE * 2:
            break
        res = search_open_web(q, limit=6, prefer_playwright=True)
        for hit in res.get("results") or []:
            _add(candidates, seen, hit.get("url") or "", f"LIVE_SEARCH:{q[:40]}")

    # Audit each candidate to LIVE state; stop early once extractable PDPs found
    # so the reserved extract budget is not consumed by more SERP verification.
    audited: list[dict[str, Any]] = []
    extractable: list[dict[str, Any]] = []
    for cand in candidates[:MAX_CANDIDATES_VALIDATE]:
        if time.time() > deadline:
            break
        audit = audit_url(cand["url"], item, allow_browser=True, soft=False)
        audit["discovery_method"] = cand.get("discovery_method")
        audited.append(audit)
        note_seller_outcome(
            audit.get("domain") or cand.get("domain") or "",
            state=str(audit.get("state") or ""),
            extractable=bool(audit.get("extractable")),
            priced=False,
        )
        if audit.get("state") in EXTRACTABLE_STATES:
            extractable.append(audit)
            if len(extractable) >= STOP_DISCOVERY_AT_EXTRACTABLE:
                break

    return {
        "candidates": candidates[:40],
        "audited": audited,
        "extractable": extractable,
        "n_candidates": len(candidates),
        "n_extractable": len(extractable),
        "elapsed_s": round(time.time() - started, 2),
    }
