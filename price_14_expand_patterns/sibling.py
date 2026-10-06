"""Sibling / domain-pattern discovery for remaining unresolved items."""

from __future__ import annotations

import time
from typing import Any

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from exact_product_url_discovery.validate_identity import validate_product_page
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from open_web_product_discovery.result_filter import classify_search_hit, is_allowed_candidate
from open_web_product_discovery.transport import search_open_web
from price_14_expand_patterns.models import ITEM_DEADLINE_S, MAX_SIBLING_CANDIDATES
from price_14_expand_patterns.patterns import preferred_domains_for_item, quill_search_url
from price_14_expand_patterns.price_url import price_exact_url
from price_adapters.validate import seller_of


def _add(bag: list[dict[str, Any]], seen: set[str], row: dict[str, Any]) -> None:
    url = row.get("url") or ""
    if not url.startswith("http") or url in seen or is_search_shell(url):
        return
    seen.add(url)
    bag.append(row)


def discover_sibling_candidates(item: dict[str, Any], *, deadline: float | None = None) -> list[dict[str, Any]]:
    """Prefer proven domain patterns before broad web search."""
    started = time.time()
    if deadline is None:
        deadline = started + ITEM_DEADLINE_S
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    prefs = preferred_domains_for_item(item)
    for pref in prefs:
        if time.time() > deadline or len(candidates) >= MAX_SIBLING_CANDIDATES:
            break
        domain = pref.get("domain") or ""
        route = pref.get("discovery_route") or ""

        # Platt GraphQL
        if domain == "platt.com" and time.time() < deadline:
            for row in platt_suggest_urls(mpn, manufacturer=mfr, limit=3):
                _add(
                    candidates,
                    seen,
                    {
                        **row,
                        "discovery_method": "SIBLING_PLATT_GRAPHQL",
                        "source": "sibling_pattern",
                    },
                )

        # Quill / internal search conversion
        if domain in {"quill.com", "pksafety.com", "homelectrical.com", "activeplumbing.com", "gsistore.com"} and time.time() < deadline:
            for row in convert_internal_search(domain, mpn=mpn, manufacturer=mfr, limit=3):
                _add(
                    candidates,
                    seen,
                    {
                        "url": row["url"],
                        "domain": domain,
                        "discovery_method": "SIBLING_INTERNAL_SEARCH",
                        "confidence": row.get("confidence", 0.55),
                        "source": "sibling_pattern",
                    },
                )

        # Active plumbing slug guess
        if domain == "activeplumbing.com" and mpn:
            slug = mpn.lower().replace("/", "-")
            _add(
                candidates,
                seen,
                {
                    "url": f"https://www.activeplumbing.com/buy/product/{slug}/",
                    "domain": domain,
                    "discovery_method": "SIBLING_ACTIVE_PLUMBING",
                    "confidence": 0.4,
                    "source": "sibling_pattern",
                },
            )

        # GSI / Aprilaire slug
        if domain == "gsistore.com" and mpn:
            _add(
                candidates,
                seen,
                {
                    "url": f"https://www.gsistore.com/products/{mfr.split()[0].lower()}-{mpn.lower()}",
                    "domain": domain,
                    "discovery_method": "SIBLING_GSISTORE",
                    "confidence": 0.35,
                    "source": "sibling_pattern",
                },
            )
        if domain == "shop.aprilaire.com" and mpn:
            _add(
                candidates,
                seen,
                {
                    "url": f"https://shop.aprilaire.com/products/aprilaire-{mpn.lower()}-replacement-filter",
                    "domain": domain,
                    "discovery_method": "SIBLING_APRILAIRE",
                    "confidence": 0.4,
                    "source": "sibling_pattern",
                },
            )

        # R.S. Hughes — rely on open-web search site-targeted later
        if domain == "rshughes.com" and time.time() < deadline:
            res = search_open_web(f'site:rshughes.com {mfr} "{mpn}"', limit=4, prefer_playwright=False)
            for hit in res.get("results") or []:
                cls = classify_search_hit(
                    url=hit.get("url") or "",
                    title=hit.get("title") or "",
                    snippet=hit.get("snippet") or "",
                    mpn=mpn,
                    manufacturer=mfr,
                )
                if is_allowed_candidate(cls):
                    _add(
                        candidates,
                        seen,
                        {
                            "url": cls["url"],
                            "domain": "rshughes.com",
                            "discovery_method": "SIBLING_SITE_SEARCH",
                            "confidence": 0.5,
                            "source": "sibling_pattern",
                        },
                    )

    # Broad Bing Playwright only if sibling patterns weak
    if time.time() < deadline and len(candidates) < 2:
        for q in (
            f'{mfr} "{mpn}"',
            f'"{mpn}" buy',
            f'"{mpn}" product',
        ):
            if time.time() > deadline or len(candidates) >= MAX_SIBLING_CANDIDATES:
                break
            res = search_open_web(q, limit=5, prefer_playwright=True)
            for hit in res.get("results") or []:
                cls = classify_search_hit(
                    url=hit.get("url") or "",
                    title=hit.get("title") or "",
                    snippet=hit.get("snippet") or "",
                    mpn=mpn,
                    manufacturer=mfr,
                )
                if not is_allowed_candidate(cls):
                    continue
                _add(
                    candidates,
                    seen,
                    {
                        "url": cls["url"],
                        "domain": cls.get("domain") or seller_of(cls["url"]),
                        "discovery_method": "BING_PLAYWRIGHT_PDP",
                        "confidence": 0.45,
                        "source": "search",
                        "title": cls.get("title"),
                    },
                )

    return candidates[:MAX_SIBLING_CANDIDATES]


def discover_and_price(
    item: dict[str, Any],
    *,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sibling discovery → identity validate → price in one pass."""
    from price_14_expand_patterns.patterns import learn_success, learn_failure

    stats = stats if stats is not None else {}
    deadline = time.time() + ITEM_DEADLINE_S
    cands = discover_sibling_candidates(item, deadline=deadline)
    stats.setdefault("candidates_seen", 0)
    stats["candidates_seen"] = int(stats["candidates_seen"]) + len(cands)

    failures: list[dict[str, Any]] = []
    for cand in cands:
        if time.time() > deadline:
            break
        url = cand["url"]
        # identity first
        v = validate_product_page(
            url,
            mpn=str(item.get("mpn") or ""),
            manufacturer=str(item.get("manufacturer") or ""),
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=False,
        )
        if not v.get("identity_match"):
            failures.append({"url": url, "reason": v.get("reason"), "stage": "identity"})
            learn_failure(cand.get("domain") or seller_of(url), str(item.get("manufacturer") or ""))
            continue

        priced = price_exact_url(url, item, allow_browser=True, stats=stats, revalidate_identity=False)
        if priced.get("status") == "PASS":
            learn_success(
                manufacturer=str(item.get("manufacturer") or ""),
                category=str(item.get("category") or ""),
                domain=priced.get("domain") or seller_of(url),
                discovery_route=str(cand.get("discovery_method") or ""),
                price_route=str(priced.get("extraction_route") or ""),
                url=url,
                mpn=str(item.get("mpn") or ""),
            )
            return {
                "benchmark_id": item.get("benchmark_id"),
                "status": "EXECUTABLE_PRICE",
                "n_candidates": len(cands),
                "url": url,
                "price_result": priced,
                "discovery_method": cand.get("discovery_method"),
                "failures": failures[:10],
            }
        failures.append({"url": url, "reason": priced.get("rejection"), "stage": "price"})
        learn_failure(cand.get("domain") or seller_of(url), str(item.get("manufacturer") or ""))

    return {
        "benchmark_id": item.get("benchmark_id"),
        "status": "UNRESOLVED",
        "n_candidates": len(cands),
        "url": None,
        "price_result": None,
        "failures": failures[:15],
    }
