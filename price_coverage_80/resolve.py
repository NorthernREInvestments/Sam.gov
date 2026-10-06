"""Multi-route accurate public NEW price resolve for coverage-80.

Build: 20261004-m3-price-coverage-80-v1
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from price_coverage_80.adapters import (
    ADAPTERS,
    classify_block_cause,
    load_domain_intel,
    note_domain_result,
    try_adapter_fetch,
)
from price_coverage_80.guards import check_condition, check_uom_pack, is_credible_seller
from price_coverage_80.models import MIN_CREDIBLE_PRICE
from public_price_search.domain_map import category_search_urls
from public_price_search.extract import detect_condition
from public_price_search.models import CONDITION_NEW
from public_price_search.queries import manufacturer_sites
from public_price_search.resolver import resolve_public_price
from public_price_search.search import fetch_page


def _identity_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_number": item.get("mpn") or item.get("part_number"),
        "manufacturer": item.get("manufacturer"),
        "brand": item.get("manufacturer"),
        "raw_description": item.get("description") or item.get("raw_description"),
        "model": item.get("model") or item.get("mpn"),
        "category": item.get("category"),
    }


def _domains_for_item(item: dict[str, Any], identity: dict[str, Any]) -> list[str]:
    preferred: list[str] = []
    seller = (item.get("known_public_seller") or "").lower().replace("www.", "")
    if seller:
        preferred.append(seller)
    # Rank by domain intel success rate
    intel = load_domain_intel().get("domains") or {}
    ranked = sorted(
        ADAPTERS.keys(),
        key=lambda d: -float((intel.get(d) or {}).get("success_rate") or 0),
    )
    cat = item.get("category") or "mro"
    cat_priority = {
        "lighting": ["1000bulbs.com", "grainger.com", "zoro.com"],
        "plumbing": ["supplyhouse.com", "grainger.com", "zoro.com"],
        "hvac": ["supplyhouse.com", "grainger.com", "zoro.com"],
        "office": ["staples.com", "grainger.com"],
        "automotive_heavy": ["finditparts.com", "fleetpride.com", "grainger.com"],
        "ppe": ["grainger.com", "zoro.com"],
        "tools": ["grainger.com", "zoro.com", "mscdirect.com"],
        "electrical": ["grainger.com", "zoro.com"],
        "industrial": ["globalindustrial.com", "grainger.com", "zoro.com"],
        "mro": ["grainger.com", "zoro.com", "mscdirect.com"],
        "furniture": ["globalindustrial.com", "staples.com"],
    }
    for d in cat_priority.get(cat, []) + ranked:
        if d not in preferred:
            preferred.append(d)
    for site in manufacturer_sites(identity.get("manufacturer")):
        if site not in preferred:
            preferred.append(site)
    return preferred[:8]


def resolve_accurate_price(
    item: dict[str, Any],
    *,
    use_budget: bool = True,
    max_sellers: int = 3,
    max_queries: int = 2,
    max_pages: int = 3,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Independent multi-route resolve — fail closed on condition/UOM/credibility."""
    t0 = time.time()
    identity = _identity_from_item(item)
    stats = stats if stats is not None else {}
    stats.setdefault("http_requests", 0)
    stats.setdefault("browser_renders", 0)
    stats.setdefault("ai_calls", 0)
    stats.setdefault("tokens", 0)
    stats.setdefault("block_causes", {})

    candidates: list[dict[str, Any]] = []
    sellers_discovered: list[str] = []
    sellers_attempted: list[str] = []
    sellers_blocked: list[dict[str, Any]] = []
    queries: list[str] = []
    miss_notes: list[str] = []

    # Route 1 — seller adapters (direct site search), prioritize known seller first
    domains = _domains_for_item(item, identity)
    # Cap sellers; stop early once we have 2+ valid raw candidates
    for domain in domains[:max_sellers]:
        sellers_attempted.append(domain)
        sellers_discovered.append(domain)
        res = try_adapter_fetch(identity, domain=domain, use_budget=use_budget)
        stats["http_requests"] = int(stats["http_requests"]) + len(res.get("urls") or [])
        for b in res.get("blocks") or []:
            cause = b.get("cause") or "unknown"
            stats["block_causes"][cause] = int(stats["block_causes"].get(cause) or 0) + 1
            sellers_blocked.append(b)
            note_domain_result(domain, success=False)
        for c in res.get("candidates") or []:
            note_domain_result(domain, success=True, method=c.get("via") or "adapter")
            candidates.append(c)
        if len(candidates) >= 2:
            break

    # Route 2 — category URL map direct (only if still empty)
    if not candidates:
        for url in category_search_urls(identity, limit=4):
            stats["http_requests"] = int(stats["http_requests"]) + 1
            fr = fetch_page(url, use_budget=use_budget)
            cause = classify_block_cause(fr)
            if cause:
                stats["block_causes"][cause] = int(stats["block_causes"].get(cause) or 0) + 1
                sellers_blocked.append({"url": url, "cause": cause})
                continue
            from price_coverage_80.adapters import _extract_generic

            cand = _extract_generic(identity, url, fr)
            if cand:
                candidates.append(cand)

    # Route 3 — existing resolver only if adapters yielded nothing usable yet
    first_hit_price = min((float(c["unit_price"]) for c in candidates), default=None)
    if not candidates:
        try:
            resolved = resolve_public_price(
                identity,
                use_budget=use_budget,
                max_queries=min(2, max_queries),
                max_pages=min(3, max_pages),
            )
            queries = list(resolved.get("queries_attempted") or resolved.get("queries") or [])[:12]
            stats["http_requests"] = int(stats["http_requests"]) + int(
                resolved.get("pages_fetched") or resolved.get("queries_used") or 0
            )
            ev = resolved.get("evidence")
            if ev and ev.get("unit_price"):
                candidates.append(
                    {
                        "unit_price": ev["unit_price"],
                        "condition": ev.get("condition"),
                        "url": ev.get("source_url"),
                        "via": ev.get("via") or "resolver",
                        "seller": ev.get("seller") or (urlparse(ev.get("source_url") or "").netloc),
                        "status": "PRICE_OK",
                        "selection_reason": ev.get("selection_reason"),
                    }
                )
            for alt in resolved.get("alternate_candidates") or []:
                if alt.get("unit_price"):
                    candidates.append(
                        {
                            "unit_price": alt["unit_price"],
                            "condition": alt.get("condition"),
                            "url": alt.get("url") or alt.get("source_url"),
                            "via": alt.get("via") or "resolver_alt",
                            "seller": alt.get("seller"),
                            "status": "PRICE_OK",
                        }
                    )
            if resolved.get("status") == "PRICE_SOURCE_BLOCKED_RETRYABLE":
                miss_notes.append("resolver_blocked")
        except Exception as exc:
            miss_notes.append(f"resolver_error:{exc}")
    else:
        miss_notes.append("resolver_skipped_adapters_hit")

    # Validate fail-closed
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for c in candidates:
        price = float(c.get("unit_price") or 0)
        seller = str(c.get("seller") or "")
        if not is_credible_seller(seller, price):
            rejected.append({**c, "reject": "NONCREDIBLE_SELLER_OR_PRICE"})
            continue
        cond = c.get("condition") or detect_condition(str(c.get("url") or ""))
        # If condition unknown, treat as NEW_ASSUMED
        if not cond or cond == "UNKNOWN":
            cond = CONDITION_NEW
            c["condition"] = cond
        cc = check_condition(
            expected=item.get("expected_condition") or "NEW",
            found=cond,
            new_assumed=(item.get("expected_condition") or "NEW") == "NEW",
        )
        if not cc["valid"]:
            rejected.append({**c, "reject": cc["status"], "reject_reason": cc["reason"]})
            continue
        # Skip reman for NEW coverage
        if (item.get("expected_condition") or "NEW") == "NEW" and cond in {
            "REMANUFACTURED",
            "RECONDITIONED",
            "USED",
        }:
            rejected.append({**c, "reject": "CONDITION_MISMATCH"})
            continue
        uom = check_uom_pack(
            expected_uom=item.get("expected_uom"),
            expected_pack=item.get("expected_pack"),
            page_text="",
            candidate_uom=c.get("uom"),
            candidate_pack=c.get("pack"),
        )
        if not uom["valid"]:
            rejected.append({**c, "reject": uom["status"], "reject_reason": uom["reason"]})
            continue
        # MPN presence soft check via URL/seller path
        pn = str(identity.get("part_number") or "").upper()
        blob = f"{c.get('url') or ''} {c.get('via') or ''}".upper()
        if pn and len(pn) >= 5 and pn not in blob and c.get("via") == "adapter_dollar":
            # weak dollar scrape without PN in URL — reject
            rejected.append({**c, "reject": "WRONG_MPN_WEAK"})
            continue
        # Category–seller affinity (fail closed)
        seller_l = seller.lower()
        cat = str(item.get("category") or "").lower()
        dieselish = any(x in seller_l for x in ("diesel", "fleetpride", "truckparts", "alliant"))
        if dieselish and cat in {"hvac", "office", "lighting", "furniture", "ppe", "plumbing"}:
            rejected.append({**c, "reject": "WRONG_CATEGORY_SELLER"})
            continue
        c["landed_estimate"] = price  # shipping unknown — labeled
        c["freight_status"] = "FREIGHT_UNKNOWN"
        c["core_charge"] = None
        valid.append(c)

    best = None
    best_price_improvement = False
    if valid:
        valid.sort(key=lambda x: float(x["unit_price"]))
        best = valid[0]
        if first_hit_price and float(best["unit_price"]) < float(first_hit_price) * 0.98:
            best_price_improvement = True
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(valid)} valid; "
            f"seller={best.get('seller')}; via={best.get('via')}; "
            f"freight={best.get('freight_status')}"
        )

    recovered_after_block = bool(valid) and bool(sellers_blocked)

    return {
        "benchmark_id": item.get("benchmark_id"),
        "identity": identity,
        "usable": best is not None,
        "unit_price": best.get("unit_price") if best else None,
        "condition": best.get("condition") if best else None,
        "seller": best.get("seller") if best else None,
        "source_url": best.get("url") if best else None,
        "via": best.get("via") if best else None,
        "selection_reason": best.get("selection_reason") if best else None,
        "n_valid_candidates": len(valid),
        "n_rejected": len(rejected),
        "candidates": valid[:6],
        "rejected": rejected[:6],
        "sellers_discovered": sellers_discovered,
        "sellers_attempted": sellers_attempted,
        "sellers_blocked": sellers_blocked[:8],
        "queries": queries,
        "best_price_improvement": best_price_improvement,
        "first_hit_price": first_hit_price,
        "recovered_after_block": recovered_after_block,
        "miss_notes": miss_notes,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "used_history_as_cost": False,
    }
