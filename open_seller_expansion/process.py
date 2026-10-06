"""Validate exact PDP + extract price in the same pass."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from exact_product_url_discovery.validate_identity import validate_product_page
from open_seller_expansion.discover import discover_alternate_sellers
from open_seller_expansion.fingerprint import persist_fingerprint
from open_seller_expansion.models import (
    DISCOVERY_BUDGET_S,
    ITEM_DEADLINE_S,
    MAX_PRICES_PER_ITEM,
    PRICE_RESERVE_S,
)
from open_seller_expansion.pools import host_of, is_identity_reference, note_seller_outcome
from price_14_expand_patterns.price_url import price_exact_url


def process_hard_item(item: dict[str, Any], *, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Discover alternate sellers → validate exact PDP → price immediately."""
    stats = stats if stats is not None else {}
    started = time.time()
    deadline = started + ITEM_DEADLINE_S
    discovery_deadline = min(started + DISCOVERY_BUDGET_S, deadline - PRICE_RESERVE_S)
    bid = item.get("benchmark_id")
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    cat = str(item.get("category") or "")

    disc = discover_alternate_sellers(item, deadline=discovery_deadline)
    fp = disc.get("fingerprint") or {}
    persist_fingerprint(fp)

    stats["searches"] = int(stats.get("searches") or 0) + int(disc.get("searches") or 0)
    stats["candidates_seen"] = int(stats.get("candidates_seen") or 0) + len(disc.get("all_candidates") or [])

    prices: list[dict[str, Any]] = []
    exact_pdps: list[dict[str, Any]] = []
    fails: list[dict[str, Any]] = []
    rej = {
        "wrong_product": 0,
        "search_shell": 0,
        "wrong_pack": 0,
        "wrong_variant": 0,
        "other": 0,
    }

    # Prefer open-alt / curated before noisy search hits
    cands = list(disc.get("candidates") or [])
    cands.sort(key=lambda c: (0 if c.get("_curated_open") else 1, -float(c.get("confidence") or 0)))

    for cand in cands:
        if time.time() > deadline:
            break
        if len(prices) >= MAX_PRICES_PER_ITEM:
            break
        url = cand.get("url") or ""
        domain = host_of(cand.get("domain") or url)
        if is_identity_reference(domain):
            continue

        # Identity validation
        soft = bool(cand.get("_curated_open"))
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=cat,
            allow_browser=False,
        )
        if not v.get("identity_match"):
            # one browser retry for JS-heavy but open sellers
            v = validate_product_page(
                url,
                mpn=mpn,
                manufacturer=mfr,
                description=item.get("description"),
                category=cat,
                allow_browser=True,
            )
        if not v.get("identity_match"):
            reason = str(v.get("reason") or "identity_fail")
            # Curated open alts: allow price extract when fetch wall / soft title issues
            # (price_exact_url still enforces pack/condition/sanity; MPN often in JSON-LD)
            soft_ok = soft and reason in {
                "fetch_blocked_or_failed",
                "auth_or_non_product_title",
                "mpn_not_on_page",
            }
            if not soft_ok:
                fails.append({"url": url, "domain": domain, "reason": reason, "stage": "identity"})
                if "mpn" in reason or "wrong" in reason:
                    rej["wrong_product"] += 1
                elif "shell" in reason or "search" in reason:
                    rej["search_shell"] += 1
                else:
                    rej["other"] += 1
                note_seller_outcome(
                    domain=domain,
                    manufacturer=mfr,
                    category=cat,
                    mpn=mpn,
                    exact_pdp=False,
                    priced=False,
                    blocked="fetch" in reason or "auth" in reason,
                )
                continue
            item = {**item, "_ready_verified": True}

        exact_pdps.append(
            {
                "url": url,
                "domain": domain,
                "title": v.get("title"),
                "discovery_method": cand.get("discovery_method"),
                "soft_identity": soft and not v.get("identity_match"),
            }
        )
        stats["exact_pdps"] = int(stats.get("exact_pdps") or 0) + 1

        # Price immediately
        priced = price_exact_url(
            url,
            {**item, "_ready_verified": soft or bool(item.get("_ready_verified"))},
            allow_browser=True,
            stats=stats,
            revalidate_identity=not soft,
        )
        if priced.get("status") == "PASS" and priced.get("price"):
            prices.append(priced)
            note_seller_outcome(
                domain=domain,
                manufacturer=mfr,
                category=cat,
                mpn=mpn,
                exact_pdp=True,
                priced=True,
                url_pattern=urlparse_path(url),
                price_route=str(priced.get("extraction_route") or ""),
            )
            print(
                f"[ose] PRICE {bid} ${priced.get('price')} {domain} via {priced.get('extraction_route')}",
                flush=True,
            )
        else:
            reason = str(priced.get("rejection") or "no_price")
            fails.append({"url": url, "domain": domain, "reason": reason, "stage": "price"})
            if "pack" in reason:
                rej["wrong_pack"] += 1
            elif "variant" in reason:
                rej["wrong_variant"] += 1
            else:
                rej["other"] += 1
            note_seller_outcome(
                domain=domain,
                manufacturer=mfr,
                category=cat,
                mpn=mpn,
                exact_pdp=True,
                priced=False,
            )

    best = None
    if prices:
        best = min(prices, key=lambda p: float(p.get("price") or 1e18))

    status = "EXECUTABLE_PRICE" if best else ("EXACT_PDP_NO_PRICE" if exact_pdps else "UNRESOLVED")
    return {
        "benchmark_id": bid,
        "status": status,
        "fingerprint": fp,
        "n_candidates": len(disc.get("all_candidates") or []),
        "n_unique_domains": disc.get("n_unique_domains") or 0,
        "unique_domains": disc.get("unique_domains") or [],
        "searches": disc.get("searches") or 0,
        "identity_refs": len(disc.get("identity_refs") or []),
        "upc_recovered": disc.get("upc_recovered"),
        "upc_seller_hit": disc.get("upc_seller_hit"),
        "exact_pdps": exact_pdps,
        "prices": prices,
        "best": best,
        "price": best.get("price") if best else None,
        "seller": best.get("domain") if best else None,
        "url": best.get("url") if best else None,
        "fails": fails[:20],
        "rejections": rej,
        "elapsed_s": round(time.time() - started, 2),
    }


def urlparse_path(url: str) -> str:
    try:
        p = urlparse(url).path
        parts = [x for x in p.split("/") if x]
        if not parts:
            return "/"
        # keep pattern-ish: first + last segment
        if len(parts) == 1:
            return f"/{parts[0]}"
        return f"/{parts[0]}/…/{parts[-1]}"
    except Exception:
        return "/"
