"""Process one hard corpus product across its known PDPs (domain-first friendly)."""

from __future__ import annotations

import time
from typing import Any

from known_pdp_price_extraction.extract_pdp import extract_known_pdp
from known_pdp_price_extraction.models import ITEM_DEADLINE_S
from known_pdp_price_extraction.rediscover import rediscover_live_pdps
from price_adapters.validate import seller_of


def process_product(item: dict[str, Any], *, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Try known PDPs; if all invalid/no-price, rediscover live PDPs on productive domains."""
    stats = stats if stats is not None else {}
    started = time.time()
    deadline = started + ITEM_DEADLINE_S
    bid = item.get("benchmark_id")
    pdps = list(item.get("pdps") or [])
    pdps.sort(key=lambda p: (1 if p.get("soft_identity") else 0, p.get("domain") or ""))

    attempts: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []

    def _try_url(url: str, *, prior_soft: bool | None = None, prior_failure: str | None = None) -> None:
        if time.time() > deadline or len(prices) >= 3:
            return
        print(f"[kpe] try {bid} {seller_of(url)} {url[:90]}", flush=True)
        r = extract_known_pdp(
            url,
            item,
            stats=stats,
            allow_browser=True,
            allow_same_domain_rediscover=True,
        )
        r["prior_soft"] = prior_soft
        r["prior_failure"] = prior_failure
        attempts.append(r)
        if r.get("status") == "PASS" and r.get("price"):
            prices.append(r)
            print(
                f"[kpe] PRICE {bid} ${r.get('price')} {r.get('domain')} via {r.get('extraction_route')}/{r.get('via')}",
                flush=True,
            )

    for p in pdps:
        _try_url(p.get("url") or "", prior_soft=p.get("soft_identity"), prior_failure=p.get("prior_failure"))
        if prices:
            break

    # If nothing priced, rediscover live PDPs on historically productive domains
    if not prices and time.time() < deadline:
        stats["rediscover_runs"] = int(stats.get("rediscover_runs") or 0) + 1
        print(f"[kpe] rediscover {bid}", flush=True)
        for row in rediscover_live_pdps(item, limit=4):
            if time.time() > deadline or prices:
                break
            # skip URLs already tried
            if any(a.get("url") == row.get("url") for a in attempts):
                continue
            _try_url(row.get("url") or "", prior_soft=False, prior_failure="rediscovered")

    best = min(prices, key=lambda x: float(x.get("price") or 1e18)) if prices else None
    return {
        "benchmark_id": bid,
        "manufacturer": item.get("manufacturer"),
        "mpn": item.get("mpn"),
        "status": "EXECUTABLE_PRICE" if best else "NO_PRICE",
        "price": best.get("price") if best else None,
        "seller": best.get("domain") if best else None,
        "url": best.get("url") if best else None,
        "extraction_route": best.get("extraction_route") if best else None,
        "via": best.get("via") if best else None,
        "condition": best.get("condition") if best else None,
        "pack_uom": best.get("pack_uom") if best else None,
        "price_class": best.get("price_class") if best else None,
        "attempts": attempts,
        "n_attempts": len(attempts),
        "prices": prices,
        "elapsed_s": round(time.time() - started, 2),
    }
