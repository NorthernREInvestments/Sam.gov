"""Per-item: expanded discovery → live PDP audit → immediate extract → classify."""

from __future__ import annotations

import time
from typing import Any

from known_pdp_price_extraction.extract_pdp import extract_known_pdp
from live_exact_priced_pdp.audit import audit_url
from live_exact_priced_pdp.models import EXTRACTABLE_STATES
from live_seller_benchmark_reverify.discover import build_queries, discover_live_sellers
from live_seller_benchmark_reverify.domains import note_domain
from live_seller_benchmark_reverify.models import (
    DISCOVERY_BUDGET_S,
    EXTRACT_RESERVE_S,
    ITEM_DEADLINE_S,
)
from live_seller_benchmark_reverify.reverify import classify_contradiction_outcome, human_check_record
from open_seller_expansion.fingerprint import build_fingerprint


def _try_extract(
    audits: list[dict[str, Any]],
    item: dict[str, Any],
    *,
    stats: dict[str, Any],
    prices: list[dict[str, Any]],
    hard_deadline: float,
    label: str,
) -> None:
    seen = {p.get("url") for p in prices}
    for a in audits:
        if time.time() > hard_deadline or len(prices) >= 3:
            break
        if a.get("state") not in EXTRACTABLE_STATES:
            continue
        url = a.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        print(f"[reverify] {label} {item.get('benchmark_id')} {a.get('state')} {url[:90]}", flush=True)
        priced = extract_known_pdp(
            url,
            item,
            stats=stats,
            allow_browser=True,
            allow_same_domain_rediscover=False,
        )
        if priced.get("status") == "PASS" and priced.get("price"):
            prices.append({**priced, "pdp_state": a.get("state")})
            note_domain(
                a.get("domain") or "",
                state=str(a.get("state") or ""),
                extractable=True,
                priced=True,
                category=str(item.get("category") or ""),
                manufacturer=str(item.get("manufacturer") or ""),
                offer_format=str(a.get("offer_status") or ""),
                extraction_method=str(priced.get("extraction_route") or ""),
                search_route=str(a.get("discovery_method") or ""),
            )
            print(
                f"[reverify] PRICE {item.get('benchmark_id')} ${priced.get('price')} {priced.get('domain')}",
                flush=True,
            )


def process_item(
    item: dict[str, Any],
    *,
    stats: dict[str, Any] | None = None,
    human_check: bool = False,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    discovery_deadline = started + DISCOVERY_BUDGET_S
    hard_deadline = started + ITEM_DEADLINE_S
    # Ensure extract reserve after discovery
    if hard_deadline < discovery_deadline + EXTRACT_RESERVE_S:
        hard_deadline = discovery_deadline + EXTRACT_RESERVE_S
    bid = item.get("benchmark_id")

    audits: list[dict[str, Any]] = []
    extractable: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []

    # Fast reaudit of prior candidates (no browser on soft)
    for p in item.get("pdps") or item.get("candidate_urls") or []:
        if time.time() > discovery_deadline:
            break
        url = p.get("url") or ""
        soft = bool(p.get("soft_identity"))
        print(f"[reverify] audit {bid} {(p.get('domain') or '')[:40]} soft={soft}", flush=True)
        a = audit_url(url, item, allow_browser=not soft, soft=soft)
        a["source"] = "prior_known"
        a["discovery_method"] = p.get("discovery_method") or "prior"
        audits.append(a)
        stats["urls_audited"] = int(stats.get("urls_audited") or 0) + 1
        note_domain(
            a.get("domain") or "",
            state=str(a.get("state") or ""),
            extractable=bool(a.get("extractable")),
            priced=False,
            category=str(item.get("category") or ""),
            manufacturer=str(item.get("manufacturer") or ""),
        )
        if a.get("state") in EXTRACTABLE_STATES:
            extractable.append(a)

    _try_extract(extractable, item, stats=stats, prices=prices, hard_deadline=hard_deadline, label="extract")

    disc: dict[str, Any] = {
        "extractable": [],
        "audited": [],
        "n_candidates": 0,
        "authorized_distributors": [],
        "fingerprint": {},
        "upc_recovered": False,
    }
    if not prices and time.time() < discovery_deadline:
        print(f"[reverify] discover {bid}", flush=True)
        disc = discover_live_sellers(item, deadline=discovery_deadline)
        stats["discovery_runs"] = int(stats.get("discovery_runs") or 0) + 1
        stats["candidates_seen"] = int(stats.get("candidates_seen") or 0) + int(disc.get("n_candidates") or 0)
        if disc.get("upc_recovered"):
            stats["upc_recovered"] = int(stats.get("upc_recovered") or 0) + 1
        for a in disc.get("audited") or []:
            audits.append({**a, "source": "live_discovery"})
            stats["urls_audited"] = int(stats.get("urls_audited") or 0) + 1
        _try_extract(
            list(disc.get("extractable") or []),
            item,
            stats=stats,
            prices=prices,
            hard_deadline=hard_deadline,
            label="extract-new",
        )

    classification = classify_contradiction_outcome(audits=audits, prices=prices, item=item)
    best = min(prices, key=lambda x: float(x.get("price") or 1e18)) if prices else None
    live_priced = sum(1 for a in audits if a.get("state") == "LIVE_EXACT_PRICED_PDP")
    live_hidden = sum(1 for a in audits if a.get("state") == "LIVE_EXACT_PDP_PRICE_HIDDEN")

    fp = disc.get("fingerprint") or build_fingerprint(item)
    queries = [q["query"] for q in build_queries(item, fp)]
    human = None
    if human_check or item.get("is_contradiction"):
        human = human_check_record(
            item,
            queries=queries,
            audits=audits,
            classification=classification,
            price=best,
        )

    return {
        "benchmark_id": bid,
        "manufacturer": item.get("manufacturer"),
        "mpn": item.get("mpn"),
        "is_contradiction": bool(item.get("is_contradiction")),
        "status": "EXECUTABLE_PRICE" if best else "UNRESOLVED",
        "price": best.get("price") if best else None,
        "seller": best.get("domain") if best else None,
        "url": best.get("url") if best else None,
        "extraction_route": best.get("extraction_route") if best else None,
        "pdp_state": best.get("pdp_state") if best else None,
        "condition": best.get("condition") if best else None,
        "pack_uom": best.get("pack_uom") if best else None,
        "classification": classification,
        "outcome": classification.get("outcome"),
        "retain_in_denom": classification.get("retain_in_denom"),
        "remove_from_denom": classification.get("remove_from_denom"),
        "audits": audits,
        "n_audits": len(audits),
        "live_exact_priced": live_priced,
        "live_exact_hidden": live_hidden,
        "n_extractable": live_priced + live_hidden,
        "discovery": {
            "n_candidates": disc.get("n_candidates"),
            "n_extractable": disc.get("n_extractable"),
            "upc_recovered": disc.get("upc_recovered"),
            "authorized_distributors": disc.get("authorized_distributors") or [],
        },
        "human_check": human,
        "prices": prices,
        "elapsed_s": round(time.time() - started, 2),
    }
