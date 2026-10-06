"""Human-like public price resolver cascade."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from public_price_search.budget import remaining, snapshot
from public_price_search.extract import (
    classify_seller,
    detect_condition,
    extract_candidate_from_page,
    extract_product_links,
    identity_on_page,
)
from public_price_search.models import (
    BUILD,
    CONDITION_MISMATCH,
    CONDITION_NEW,
    CONDITION_RECONDITIONED,
    CONDITION_REMANUFACTURED,
    CONDITION_USED,
    INSUFFICIENT_IDENTITY,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PER_IDENTITY_PAGE_CAP,
    PER_IDENTITY_QUERY_CAP,
    PRICE_SEARCH_BUDGET_EXHAUSTED,
    PRICE_SOURCE_BLOCKED_RETRYABLE,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
    SELLER_DISTRIBUTOR,
    SELLER_MANUFACTURER,
    SELLER_MARKETPLACE,
    SELLER_RESELLER,
    UOM_AMBIGUOUS,
)
from public_price_search.queries import (
    build_query_variants,
    distributor_search_urls,
    known_product_urls,
    manufacturer_direct_urls,
)
from public_price_search.search import (
    fetch_page,
    route_health_snapshot,
    search_web,
    serp_circuit_open,
    snippet_candidates,
    note_route,
)
from public_price_search.circuits import is_open, note as circuit_note

log = logging.getLogger("govtracker.public_price_search.resolver")


def _has_identity(identity: dict[str, Any]) -> bool:
    return bool(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("model")
        or identity.get("sku")
        or (identity.get("manufacturer") and identity.get("model"))
    )


def _required_condition(identity: dict[str, Any]) -> str | None:
    """NEW_ASSUMED unless solicitation explicitly permits reman/recon/used."""
    hint = str(identity.get("required_condition") or identity.get("condition_required") or "").upper()
    if hint in {"REMAN", "REMANUFACTURED", "RECONDITIONED", "RECON", "USED", "ANY"}:
        return None  # explicitly permissive
    if hint in {"NEW", "OEM_NEW"}:
        return CONDITION_NEW
    blob = str(identity.get("raw_description") or identity.get("solicitation_notes") or "").lower()
    if any(x in blob for x in ("reman ok", "reconditioned ok", "used ok", "rebuild ok")):
        return None
    if "new only" in blob or "must be new" in blob:
        return CONDITION_NEW
    # Default policy: NEW_ASSUMED
    return CONDITION_NEW


def _score_candidate(c: dict[str, Any], required_cond: str | None) -> tuple:
    cond = c.get("condition")
    cond_ok = 0
    if required_cond == CONDITION_NEW and cond in {
        CONDITION_RECONDITIONED,
        CONDITION_REMANUFACTURED,
        CONDITION_USED,
    }:
        cond_ok = 2  # worse
    elif required_cond and cond == required_cond:
        cond_ok = 0
    else:
        cond_ok = 1
    seller_rank = {
        SELLER_MANUFACTURER: 0,
        SELLER_DISTRIBUTOR: 1,
        SELLER_RESELLER: 2,
        SELLER_MARKETPLACE: 3,
    }.get(c.get("seller_class"), 4)
    snippet_pen = 1 if c.get("snippet_only") else 0
    price = float(c.get("unit_price") or 1e18)
    return (cond_ok, snippet_pen, seller_rank, price)


def _select_best(
    candidates: list[dict[str, Any]],
    *,
    required_cond: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    for c in candidates:
        if c.get("status") != "PRICE_OK" or not c.get("unit_price"):
            rejected.append({**c, "rejection_reason": "NO_PRICE"})
            continue
        if required_cond == CONDITION_NEW and c.get("condition") in {
            CONDITION_RECONDITIONED,
            CONDITION_REMANUFACTURED,
            CONDITION_USED,
        }:
            rejected.append({**c, "rejection_reason": "CONDITION_NOT_USABLE_FOR_ECONOMICS"})
            continue
        usable.append(c)
    if not usable:
        return None, rejected
    # Drop outlier-low prices when peers disagree by >=1.8x
    # (often incomplete kits / missing core / wrong grade).
    prices = sorted(float(c["unit_price"]) for c in usable)
    if len(prices) >= 2 and prices[-1] >= 1.8 * prices[0]:
        floor = prices[-1] * 0.60
        kept = []
        for c in usable:
            if float(c["unit_price"]) < floor:
                rejected.append({**c, "rejection_reason": "PRICE_OUTLIER_LOW_VS_PEERS"})
            else:
                kept.append(c)
        if kept:
            usable = kept
    usable.sort(key=lambda c: _score_candidate(c, required_cond))
    best = usable[0]
    best["selection_reason"] = (
        f"exact_product; condition={best.get('condition')}; "
        f"seller={best.get('seller_class')}; via={best.get('via')}; "
        f"lowest_defensible_among_ranked"
    )
    return best, rejected


def resolve_public_price(
    identity: dict[str, Any],
    *,
    opportunity_id: str | None = None,
    client: httpx.Client | None = None,
    max_queries: int | None = None,
    max_pages: int | None = None,
    use_budget: bool = True,
    required_condition: str | None = None,
) -> dict[str, Any]:
    """Full human-like cascade for one identity."""
    oid = opportunity_id or identity.get("opportunity_id") or "unknown"
    trace: dict[str, Any] = {
        "build": BUILD,
        "opportunity_id": oid,
        "queries_attempted": [],
        "candidate_urls": [],
        "candidate_sellers": [],
        "blocked_sources": [],
        "prices_found": [],
        "rejected_prices": [],
        "selected_price": None,
        "selection_reason": None,
        "budget": snapshot(),
    }
    result: dict[str, Any] = {
        "status": None,
        "match_type": None,
        "failure_reason": None,
        "stop_reason": None,
        "evidence": None,
        "provenance": [],
        "candidates": [],
        "search_trace": trace,
        "price_search_budget": snapshot(),
    }

    if not _has_identity(identity):
        result["status"] = INSUFFICIENT_IDENTITY
        result["failure_reason"] = INSUFFICIENT_IDENTITY
        result["stop_reason"] = INSUFFICIENT_IDENTITY
        return result

    req_cond = required_condition or _required_condition(identity)
    q_cap = max_queries or PER_IDENTITY_QUERY_CAP
    p_cap = max_pages or PER_IDENTITY_PAGE_CAP
    pages_used = 0
    queries_used_local = 0
    candidates: list[dict[str, Any]] = []
    budget_hit = False
    blocked_any = False
    own = client is None
    c = client or httpx.Client(timeout=8.0, follow_redirects=True)

    try:
        def _ingest_page(url: str, *, via: str, follow_links: bool = True) -> None:
            nonlocal pages_used, blocked_any, budget_hit
            if pages_used >= p_cap or budget_hit:
                return
            if use_budget and remaining() <= 0:
                budget_hit = True
                return
            pages_used += 1
            fr = fetch_page(url, client=c, use_budget=use_budget)
            final_url = fr.get("url") or url
            trace["candidate_urls"].append(final_url)
            if fr.get("blocked") or not fr.get("ok"):
                blocked_any = True
                trace["blocked_sources"].append(
                    {"url": url, "status_code": fr.get("status_code"), "error": fr.get("error")}
                )
                # Do not trust prices on blocked/challenge bodies
                if follow_links:
                    return
            text = fr.get("text") or ""
            host = fr.get("host") or ""
            is_search = "/search" in (final_url or "").lower() or "search?" in (final_url or "").lower()
            # Prefer following product links from search pages before listing-price extraction
            if follow_links and is_search and text:
                for link in extract_product_links(text, identity, base_host=host)[:4]:
                    if link in trace["candidate_urls"]:
                        continue
                    _ingest_page(link, via=f"{via}_link", follow_links=False)
            # Prefer product links from search pages, but also accept a search-page
            # price when identity + structured/product price are present (common on
            # diesel/MRO catalogs that embed offer JSON in results HTML).
            if text and identity_on_page(text, identity) and (not is_search or via.endswith("search") or "distributor" in via or "manufacturer" in via):
                cand = extract_candidate_from_page(text, url=final_url, identity=identity)
                if cand and cand.get("status") == "PRICE_OK":
                    # Guard absurd prices from noisy HTML
                    if float(cand["unit_price"]) > 75000:
                        trace["rejected_prices"].append({**cand, "rejection_reason": "PRICE_ABSURD"})
                    else:
                        # On search pages, only keep if structured signal present
                        if is_search and cand.get("via") not in {"structured_data", "structured_api"}:
                            # still allow single clear productPrice-style hits
                            if len(cand.get("all_prices_seen") or []) > 3:
                                trace["rejected_prices"].append(
                                    {**cand, "rejection_reason": "SEARCH_PAGE_AMBIGUOUS"}
                                )
                                cand = None
                        if cand:
                            candidates.append(cand)
                            if cand.get("via") == "structured_data":
                                circuit_note("STRUCTURED_PRODUCT_DATA", ok=True)
                            note_route(
                                "Distributor" if "distributor" in via else "Direct catalog",
                                ok=True,
                            )
                            circuit_note(
                                "DISTRIBUTOR_SEARCH" if "distributor" in via else "DIRECT_CATALOG",
                                ok=True,
                            )
                            trace["prices_found"].append(
                                {"url": cand["url"], "price": cand["unit_price"], "via": via}
                            )
                elif cand and cand.get("js_render_candidate") and pages_used < p_cap and not is_search:
                    # Static HTML matched identity but price is JS-hidden
                    recovered = False
                    try:
                        from public_price_search.catalog_intel import (
                            browser_render_price,
                            extract_api_hints,
                            fetch_structured_api,
                        )

                        for api_u in extract_api_hints(text, page_url=final_url)[:2]:
                            for row in fetch_structured_api(api_u, client=c, identity=identity):
                                p = row.get("price")
                                if not p:
                                    continue
                                candidates.append(
                                    {
                                        "status": "PRICE_OK",
                                        "url": final_url,
                                        "unit_price": p,
                                        "displayed_price": p,
                                        "condition": detect_condition(text),
                                        "seller_class": classify_seller(
                                            final_url, identity.get("manufacturer")
                                        ),
                                        "seller": host,
                                        "via": "structured_api",
                                        "exact_match": True,
                                    }
                                )
                                circuit_note("STRUCTURED_PRODUCT_DATA", ok=True)
                                trace["prices_found"].append(
                                    {"url": final_url, "price": p, "via": "structured_api"}
                                )
                                recovered = True
                        if not recovered:
                            rendered = browser_render_price(final_url, identity)
                            if rendered and rendered.get("status") == "PRICE_OK":
                                rendered["seller_class"] = classify_seller(
                                    final_url, identity.get("manufacturer")
                                )
                                candidates.append(rendered)
                                circuit_note("DIRECT_CATALOG", ok=True)
                                trace["prices_found"].append(
                                    {
                                        "url": final_url,
                                        "price": rendered["unit_price"],
                                        "via": "js_render",
                                    }
                                )
                                recovered = True
                    except Exception:
                        recovered = False
                    if not recovered:
                        trace["rejected_prices"].append(cand)
                elif cand:
                    trace["rejected_prices"].append(cand)

        # --- 1) Known product/search URLs first (SERP often bot-blocked) ---
        for url in known_product_urls(identity) + manufacturer_direct_urls(identity):
            before = len([c for c in candidates if c.get("status") == "PRICE_OK"])
            _ingest_page(url, via="manufacturer_or_known")
            after = len([c for c in candidates if c.get("status") == "PRICE_OK"])
            note_route("Manufacturer" if "cummins" in url.lower() or "ford" in url.lower() or "dynarex" in url.lower() else "Direct catalog", ok=after > before)
            # Keep collecting — do not stop at first price (best-price policy)
            if after >= 4:
                break

        # --- 2) Independent search providers (A/B) — skip only if BOTH open ---
        # Never let search-provider failure stop manufacturer/distributor routes below.
        queries = build_query_variants(identity)[:q_cap]
        seen_urls: set[str] = set(trace["candidate_urls"])
        if serp_circuit_open():
            trace["queries_attempted"].append("(both_search_providers_circuit_open)")
            trace["serp_status"] = "BOTH_PROVIDERS_CIRCUIT_OPEN"
            queries = []
        elif len([x for x in candidates if x.get("status") == "PRICE_OK"]) >= 3:
            queries = []
            trace["queries_attempted"].append("(skipped_serp_enough_direct_prices)")
        else:
            queries = queries[: min(3, q_cap)]
        for q in queries:
            if use_budget and remaining() <= 0:
                budget_hit = True
                break
            queries_used_local += 1
            sr = search_web(q, limit=6, client=c, use_budget=use_budget)
            trace["queries_attempted"].append(q)
            if sr.get("skipped") == "ALL_SEARCH_PROVIDERS_CIRCUIT_OPEN":
                trace["serp_status"] = "BOTH_PROVIDERS_CIRCUIT_OPEN"
                break
            if sr.get("budget_exhausted"):
                budget_hit = True
                break
            for sc in snippet_candidates(sr.get("results") or [], identity):
                candidates.append(sc)
                trace["prices_found"].append(
                    {"url": sc.get("url"), "price": sc.get("unit_price"), "via": "snippet"}
                )
            for r in sr.get("results") or []:
                url = r.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                trace["candidate_urls"].append(url)
                if pages_used >= p_cap:
                    continue
                if use_budget and remaining() <= 0:
                    budget_hit = True
                    break
                _ingest_page(url, via="serp_page", follow_links=True)
                if any(x.get("url") == url and x.get("status") == "PRICE_OK" for x in candidates):
                    note_route("Reseller", ok=True)

        # --- 3) Distributor / category catalogs — ALWAYS when under-priced ---
        if len([x for x in candidates if x.get("status") == "PRICE_OK"]) < 3:
            if not is_open("DISTRIBUTOR_SEARCH"):
                pn = identity.get("part_number") or identity.get("model") or ""
                mfr = identity.get("manufacturer") or ""
                seed = f"{mfr} {pn}".strip() or str(pn)
                if not seed:
                    seed = str(
                        identity.get("commercial_search_key") or identity.get("raw_description") or ""
                    )[:80]
                for url in distributor_search_urls(seed, identity)[:6]:
                    before = len([x for x in candidates if x.get("status") == "PRICE_OK"])
                    _ingest_page(url, via="distributor_search")
                    after = len([x for x in candidates if x.get("status") == "PRICE_OK"])
                    note_route(
                        "Distributor",
                        ok=after > before,
                        error=None if after > before else "NO_PRICE_ON_PAGE",
                    )
                    circuit_note(
                        "DISTRIBUTOR_SEARCH",
                        ok=after > before,
                        reason=None if after > before else "NO_PRICE_ON_PAGE",
                    )
                    if after >= 3 or pages_used >= p_cap:
                        break

        trace["route_health"] = route_health_snapshot()
        trace["serp_circuit_open"] = serp_circuit_open()

        # Select
        priced = [x for x in candidates if x.get("status") == "PRICE_OK"]
        # Dedupe by url+price
        dedup: list[dict[str, Any]] = []
        seen_k: set[str] = set()
        for p in priced:
            k = f"{p.get('url')}|{p.get('unit_price')}"
            if k in seen_k:
                continue
            seen_k.add(k)
            dedup.append(p)
        best, rejected = _select_best(dedup, required_cond=req_cond)
        trace["rejected_prices"].extend(rejected)
        result["candidates"] = dedup[:8]
        trace["candidate_sellers"] = sorted({str(p.get("seller") or "") for p in dedup if p.get("seller")})
        trace["budget"] = snapshot()
        result["price_search_budget"] = snapshot()
        result["queries_used"] = queries_used_local
        result["searches_remaining"] = remaining()

        if best:
            # Cost basis: do not silently subtract core
            evidence = {
                "unit_price": best["unit_price"],
                "displayed_price": best.get("displayed_price") or best["unit_price"],
                "core_charge": best.get("core_charge"),
                "core_refundable": best.get("core_refundable"),
                "net_cost_if_core_returned": best.get("net_cost_if_core_returned"),
                "gross_cash_required": best.get("gross_cash_required"),
                "core_return_requirement": best.get("core_return_requirement"),
                "condition": best.get("condition"),
                "seller": best.get("seller"),
                "seller_class": best.get("seller_class"),
                "source_url": best.get("url"),
                "shipping": best.get("shipping"),
                "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
                "basis": (
                    "PUBLIC_MANUFACTURER_EXACT"
                    if best.get("seller_class") == SELLER_MANUFACTURER
                    else (
                        "PUBLIC_DISTRIBUTOR_EXACT"
                        if best.get("seller_class") == SELLER_DISTRIBUTOR
                        else "PUBLIC_RESELLER_EXACT"
                    )
                ),
                "exact_match": True,
                "retrieved_via": best.get("via"),
                "snippet_only": bool(best.get("snippet_only")),
                "selection_reason": best.get("selection_reason"),
                "alternate_candidates": [
                    {"url": x.get("url"), "price": x.get("unit_price"), "condition": x.get("condition")}
                    for x in dedup[1:5]
                ],
            }
            # Keep unit_price = displayed part price. Core is explicit side-channel fields only.
            result["status"] = PUBLIC_PRICE_PARTIAL if best.get("snippet_only") else PUBLIC_PRICE_FOUND
            result["match_type"] = evidence["basis"]
            result["evidence"] = evidence
            result["provenance"] = [
                {
                    "route": best.get("via"),
                    "url": best.get("url"),
                    "seller": best.get("seller"),
                    "condition": best.get("condition"),
                }
            ]
            trace["selected_price"] = evidence
            trace["selection_reason"] = best.get("selection_reason")
            return result

        # Condition-only mismatches
        if rejected and all(
            str(r.get("rejection_reason") or "").startswith("CONDITION") for r in rejected
        ):
            result["status"] = CONDITION_MISMATCH
            result["failure_reason"] = CONDITION_MISMATCH
            result["stop_reason"] = CONDITION_MISMATCH
            result["search_trace"] = trace
            return result

        # If we found priced candidates but selection emptied them for non-condition
        # reasons, still prefer returning the best NEW if any usable remain after relax
        if dedup and not best:
            # Prefer NEW/UNKNOWN over reman for reporting; if only reman, CONDITION_MISMATCH
            newish = [
                c
                for c in dedup
                if str(c.get("condition") or "").upper() in {"NEW", "UNKNOWN", ""}
            ]
            if newish:
                newish.sort(key=lambda c: float(c.get("unit_price") or 1e18))
                best = newish[0]
                best["selection_reason"] = (
                    f"fallback_new_assumed; condition={best.get('condition')}; "
                    f"seller={best.get('seller_class')}; via={best.get('via')}"
                )
                evidence = {
                    "unit_price": best["unit_price"],
                    "displayed_price": best.get("displayed_price") or best["unit_price"],
                    "core_charge": best.get("core_charge"),
                    "condition": best.get("condition") or "UNKNOWN",
                    "seller": best.get("seller"),
                    "seller_class": best.get("seller_class"),
                    "source_url": best.get("url"),
                    "shipping": best.get("shipping"),
                    "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
                    "basis": "PUBLIC_DISTRIBUTOR_EXACT",
                    "exact_match": True,
                    "retrieved_via": best.get("via"),
                    "selection_reason": best.get("selection_reason"),
                    "alternate_candidates": [
                        {"url": x.get("url"), "price": x.get("unit_price"), "condition": x.get("condition")}
                        for x in dedup[:5]
                        if x is not best
                    ],
                }
                result["status"] = PUBLIC_PRICE_FOUND
                result["match_type"] = evidence["basis"]
                result["evidence"] = evidence
                result["provenance"] = [{"route": best.get("via"), "url": best.get("url")}]
                result["candidates"] = dedup[:8]
                trace["selected_price"] = evidence
                result["search_trace"] = trace
                return result
            result["status"] = CONDITION_MISMATCH
            result["failure_reason"] = "CONDITION_NOT_USABLE_FOR_ECONOMICS"
            result["stop_reason"] = CONDITION_MISMATCH
            result["candidates"] = dedup[:8]
            result["search_trace"] = trace
            return result

        if budget_hit and not dedup:
            result["status"] = PRICE_SEARCH_BUDGET_EXHAUSTED
            result["failure_reason"] = PRICE_SEARCH_BUDGET_EXHAUSTED
            result["stop_reason"] = PRICE_SEARCH_BUDGET_EXHAUSTED
            result["search_trace"] = trace
            return result

        # If SERP is circuit-open and we also saw blocked catalog pages without any priced
        # candidate, this is retryable route failure — NOT exhaustive no-price.
        if serp_circuit_open() and blocked_any and not dedup:
            result["status"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["failure_reason"] = "PRICE_ROUTE_BLOCKED_RETRYABLE"
            result["stop_reason"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["search_trace"] = trace
            result["note"] = "SERP circuit open; manufacturer/distributor attempted but blocked/empty"
            return result

        if blocked_any and not dedup and queries_used_local < 2:
            result["status"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["failure_reason"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["stop_reason"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["search_trace"] = trace
            return result

        # Only claim exhaustive no-price when direct catalog routes were actually attempted
        routes_ok = bool(trace.get("candidate_urls"))
        if not routes_ok:
            result["status"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["failure_reason"] = "PRICE_SEARCH_NOT_RUN"
            result["stop_reason"] = PRICE_SOURCE_BLOCKED_RETRYABLE
            result["search_trace"] = trace
            return result

        result["status"] = NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH
        result["failure_reason"] = NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH
        result["stop_reason"] = NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH
        result["search_trace"] = trace
        return result
    finally:
        if own:
            c.close()


# Keep import surface tidy
__all__ = ["resolve_public_price", "UOM_AMBIGUOUS", "BUILD"]
