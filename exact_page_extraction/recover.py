"""Recover price from verified exact pages only — no broad discovery."""

from __future__ import annotations

import time
from typing import Any

from exact_page_extraction.extract import extract_exact_page
from exact_page_extraction.models import (
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    ROUTE_ALT_URL,
)
from exact_page_extraction.url_classify import classify_url
from price_adapters.models import FOUND_VALID_PRICE
from price_adapters.orchestrate import _identity


def recover_exact_pages(
    item: dict[str, Any],
    *,
    urls: list[str] | None = None,
    verified_urls: list[str] | None = None,
    unverified_urls: list[str] | None = None,
    allow_browser: bool = True,
    stats: dict[str, Any] | None = None,
    item_deadline: float | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    if item_deadline is None:
        item_deadline = started + 40.0

    identity = _identity(item)
    mpn = str(identity.get("part_number") or "")
    verified = list(verified_urls or [])
    unverified = list(unverified_urls or [])
    if urls:
        for u in urls:
            cls = classify_url(u, mpn=mpn)
            if cls == EXACT_PRODUCT_VERIFIED and u not in verified:
                verified.append(u)
            elif cls == EXACT_PRODUCT_UNVERIFIED and u not in unverified:
                unverified.append(u)

    # Prefer verified, then unverified (still exact-ish product pages)
    queue = [(u, True) for u in verified] + [(u, False) for u in unverified]

    diag: dict[str, Any] = {
        "build": "20261004-m3-exact-page-price-extraction-v1",
        "identity": identity,
        "urls_tried": [],
        "candidates": [],
        "best": None,
        "status": "EXHAUSTED",
        "route": None,
        "n_verified_urls": len(verified),
        "n_unverified_urls": len(unverified),
        "page_diags": [],
    }

    for idx, (url, is_verified) in enumerate(queue[:8]):
        if time.time() > item_deadline:
            diag["status"] = "ROUTE_TIMEOUT"
            break
        page = extract_exact_page(
            url,
            item,
            allow_browser=allow_browser,
            allow_api=True,
            allow_cart=True,
            stats=stats,
        )
        diag["urls_tried"].append({"url": url, "verified": is_verified, "status": page.get("status"), "route": page.get("route")})
        diag["page_diags"].append(page)
        if page.get("best"):
            cand = page["best"]
            if idx > 0:
                cand = {**cand, "via": (cand.get("via") or "") + "+alt_url"}
                stats.setdefault("route_counts", {})
                stats["route_counts"][ROUTE_ALT_URL] = int(stats["route_counts"].get(ROUTE_ALT_URL) or 0) + 1
            diag["candidates"].append(cand)
            # Keep going briefly to allow multi-seller if time remains and < 2 candidates
            if len(diag["candidates"]) >= 2 or time.time() > item_deadline - 5:
                break

    if diag["candidates"]:
        # Prefer trusted industrial sellers / known seller, then lowest price
        from price_coverage_80.scoring import score_accuracy

        def _rank(c: dict[str, Any]) -> tuple:
            probe = {
                "usable": True,
                "unit_price": c.get("unit_price"),
                "condition": c.get("condition") or "NEW",
                "seller": c.get("seller"),
                "source_url": c.get("url"),
            }
            acc = score_accuracy(item, probe)
            trusted = 0 if acc.get("correct") else 1
            return (trusted, float(c["unit_price"]))

        diag["candidates"].sort(key=_rank)
        best = diag["candidates"][0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(diag['candidates'])} exact-page valid; "
            f"seller={best.get('seller')}; via={best.get('via')}"
        )
        diag["best"] = best
        diag["status"] = FOUND_VALID_PRICE
        diag["route"] = best.get("via")
    diag["elapsed_s"] = round(time.time() - started, 3)
    return diag
