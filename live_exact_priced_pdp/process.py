"""Per-product: reaudit known URLs → discover live priced PDPs → extract immediately."""

from __future__ import annotations

import time
from typing import Any

from live_exact_priced_pdp.audit import audit_url
from live_exact_priced_pdp.discover import discover_live_priced_candidates
from live_exact_priced_pdp.models import EXTRACT_RESERVE_S, EXTRACTABLE_STATES, ITEM_DEADLINE_S
from live_exact_priced_pdp.score import note_seller_outcome
from known_pdp_price_extraction.extract_pdp import extract_known_pdp


def _try_extract(
    audits: list[dict[str, Any]],
    item: dict[str, Any],
    *,
    stats: dict[str, Any],
    prices: list[dict[str, Any]],
    hard_deadline: float,
    label: str,
) -> None:
    seen_urls = {p.get("url") for p in prices}
    for a in audits:
        if time.time() > hard_deadline or len(prices) >= 3:
            break
        if a.get("state") not in EXTRACTABLE_STATES:
            continue
        url = a.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        print(f"[lep] {label} {item.get('benchmark_id')} {a.get('state')} {url[:90]}", flush=True)
        priced = extract_known_pdp(
            url,
            item,
            stats=stats,
            allow_browser=True,
            allow_same_domain_rediscover=False,
        )
        if priced.get("status") == "PASS" and priced.get("price"):
            prices.append({**priced, "pdp_state": a.get("state")})
            note_seller_outcome(
                a.get("domain") or "",
                state=str(a.get("state") or ""),
                extractable=True,
                priced=True,
            )
            print(
                f"[lep] PRICE {item.get('benchmark_id')} ${priced.get('price')} {priced.get('domain')}",
                flush=True,
            )


def process_item(item: dict[str, Any], *, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    soft_deadline = started + ITEM_DEADLINE_S
    # Hard deadline keeps room to extract after discovery finds a live PDP.
    hard_deadline = started + ITEM_DEADLINE_S + EXTRACT_RESERVE_S
    discovery_deadline = started + max(25.0, ITEM_DEADLINE_S - EXTRACT_RESERVE_S)
    bid = item.get("benchmark_id")

    audits: list[dict[str, Any]] = []
    extractable: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []

    # Phase 2 — reaudit previously known/candidate URLs (fast path for soft)
    for p in item.get("pdps") or []:
        if time.time() > discovery_deadline:
            break
        url = p.get("url") or ""
        soft = bool(p.get("soft_identity"))
        print(f"[lep] audit {bid} {p.get('domain')} soft={soft}", flush=True)
        # Soft prior URLs: no browser — they were never hard-proven; save budget for discovery.
        a = audit_url(url, item, allow_browser=not soft, soft=soft)
        a["source"] = "prior_known"
        audits.append(a)
        stats["urls_audited"] = int(stats.get("urls_audited") or 0) + 1
        note_seller_outcome(
            a.get("domain") or "",
            state=str(a.get("state") or ""),
            extractable=bool(a.get("extractable")),
            priced=False,
        )
        if a.get("state") in EXTRACTABLE_STATES:
            extractable.append(a)

    _try_extract(extractable, item, stats=stats, prices=prices, hard_deadline=hard_deadline, label="extract")

    # Phase 8–14 — discover new live priced PDPs if needed
    disc: dict[str, Any] = {"extractable": [], "audited": [], "n_candidates": 0}
    if not prices and time.time() < discovery_deadline:
        print(f"[lep] discover {bid}", flush=True)
        disc = discover_live_priced_candidates(item, deadline=discovery_deadline)
        stats["discovery_runs"] = int(stats.get("discovery_runs") or 0) + 1
        stats["candidates_seen"] = int(stats.get("candidates_seen") or 0) + int(disc.get("n_candidates") or 0)
        for a in disc.get("audited") or []:
            audits.append({**a, "source": "live_discovery"})
            stats["urls_audited"] = int(stats.get("urls_audited") or 0) + 1
        # Always attempt extract on newly found live PDPs — reserved budget.
        _try_extract(
            list(disc.get("extractable") or []),
            item,
            stats=stats,
            prices=prices,
            hard_deadline=hard_deadline,
            label="extract-new",
        )

    best = min(prices, key=lambda x: float(x.get("price") or 1e18)) if prices else None
    live_priced = sum(1 for a in audits if a.get("state") == "LIVE_EXACT_PRICED_PDP")
    live_hidden = sum(1 for a in audits if a.get("state") == "LIVE_EXACT_PDP_PRICE_HIDDEN")

    return {
        "benchmark_id": bid,
        "manufacturer": item.get("manufacturer"),
        "mpn": item.get("mpn"),
        "status": "EXECUTABLE_PRICE" if best else "UNRESOLVED",
        "price": best.get("price") if best else None,
        "seller": best.get("domain") if best else None,
        "url": best.get("url") if best else None,
        "extraction_route": best.get("extraction_route") if best else None,
        "pdp_state": best.get("pdp_state") if best else None,
        "condition": best.get("condition") if best else None,
        "pack_uom": best.get("pack_uom") if best else None,
        "audits": audits,
        "n_audits": len(audits),
        "live_exact_priced": live_priced,
        "live_exact_hidden": live_hidden,
        "n_extractable": live_priced + live_hidden,
        "discovery": {
            "n_candidates": disc.get("n_candidates"),
            "n_extractable": disc.get("n_extractable"),
        },
        "prices": prices,
        "elapsed_s": round(time.time() - started, 2),
        "soft_deadline_s": ITEM_DEADLINE_S,
        "hard_deadline_s": ITEM_DEADLINE_S + EXTRACT_RESERVE_S,
    }
