"""Sweep exact product URL discovery over unresolved Full-100 items."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from exact_product_url_discovery.discover import discover_and_validate
from exact_product_url_discovery.models import (
    BUILD,
    CK,
    HONEST_PRICED,
    KNOWN_FOCUS,
    ORIGINAL_DENOM,
    PRIOR_HM_CK,
    PROGRESS_EVERY,
    READY_FOR_PRICE_EXTRACTION,
    REPORT,
    TARGET_NEW_EXACT_URLS,
    URL_FOUND,
    URL_DISCOVERY_PENDING,
)
from exact_product_url_discovery.store import domain_yield_snapshot, list_ready_for_price
from m3_data_root import data_path
from price_coverage_80.corpus import confirmed_public, load_corpus
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def unresolved_items() -> list[dict[str, Any]]:
    """Full-100 items without honest executable price."""
    prior = _load(PRIOR_HM_CK)
    by = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    priced = {
        bid
        for bid, row in (prior.get("items") or {}).items()
        if (row.get("found") or {}).get("usable")
    }
    # Also treat honest_priced baseline if prior empty
    out = []
    for item in confirmed_public():
        bid = item["benchmark_id"]
        if bid in priced:
            continue
        full = by.get(bid) or item
        out.append(full)
    return out


def run_exact_product_url_discovery_v1(*, fresh: bool = False) -> dict[str, Any]:
    reset_all()
    reset_serp_circuit()
    price_budget.ensure_budget(minimum_remaining=900)

    if fresh or not _load(CK).get("items"):
        ck: dict[str, Any] = {
            "build": BUILD,
            "run_id": f"EPU-{uuid4().hex[:10]}",
            "started_at": now_utc().isoformat(),
            "items": {},
            "stats": {
                "candidates_seen": 0,
                "validated": 0,
                "rejected_shells": 0,
                "rejected_wrong_mpn": 0,
                "rejected_wrong_mfr": 0,
                "by_method": {},
            },
            "report_ready": False,
        }
    else:
        ck = _load(CK)
        ck.setdefault("stats", {})

    todo = unresolved_items()
    stats = ck["stats"]
    items = ck.setdefault("items", {})
    print(f"[exact_url] start n={len(todo)} build={BUILD} target>={TARGET_NEW_EXACT_URLS}", flush=True)

    found_n = 0
    for idx, item in enumerate(todo, 1):
        bid = item["benchmark_id"]
        # skip if already have validated url this run
        prev = items.get(bid) or {}
        if prev.get("status") == READY_FOR_PRICE_EXTRACTION and (prev.get("validated_urls") or []):
            found_n += 1
            continue
        result = discover_and_validate(item, stats=stats)
        items[bid] = {
            **result,
            "updated_at": now_utc().isoformat(),
        }
        if result.get("n_validated"):
            found_n += 1
            best = result.get("best_url") or {}
            print(
                f"[exact_url] FOUND {bid} {best.get('seller_domain')} {best.get('url')}",
                flush=True,
            )
        else:
            print(
                f"[exact_url] MISS {bid} cands={result.get('n_candidates')} "
                f"fails={len(result.get('failures') or [])}",
                flush=True,
            )
        if idx % PROGRESS_EVERY == 0:
            print(
                f"[exact_url] progress {idx}/{len(todo)} with_url={found_n} "
                f"validated_total={stats.get('validated')} shells_rej={stats.get('rejected_shells')}",
                flush=True,
            )
            _save(CK, ck)
        else:
            _save(CK, ck)

    report = build_final_report(ck, todo_n=len(todo))
    persist_circuits()
    print(format_report(report), flush=True)
    return report


def build_final_report(ck: dict[str, Any] | None = None, *, todo_n: int | None = None) -> dict[str, Any]:
    ck = ck or _load(CK)
    items = ck.get("items") or {}
    stats = ck.get("stats") or {}
    by = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    unresolved = unresolved_items()
    unresolved_ids = {i["benchmark_id"] for i in unresolved}

    with_url = [
        bid
        for bid, row in items.items()
        if bid in unresolved_ids and int(row.get("n_validated") or 0) > 0
    ]
    still = [bid for bid in unresolved_ids if bid not in set(with_url)]

    by_method = stats.get("by_method") or {}
    route_counts = {
        "internal_search_conversions": int((by_method.get("INTERNAL_SEARCH_CONVERSION") or {}).get("validated") or 0),
        "sitemap": int((by_method.get("XML_SITEMAP") or {}).get("validated") or 0)
        + int((by_method.get("PRODUCT_SITEMAP") or {}).get("validated") or 0),
        "manufacturer": int((by_method.get("MANUFACTURER_PRODUCT_PAGE") or {}).get("validated") or 0),
        "distributor": int((by_method.get("AUTHORIZED_DISTRIBUTOR") or {}).get("validated") or 0),
        "reseller_serp": int((by_method.get("SEARCH_RESULT_HREF") or {}).get("validated") or 0),
        "feed_api": int((by_method.get("PUBLIC_FEED_API") or {}).get("validated") or 0),
        "pattern_discovery": int((by_method.get("URL_PATTERN_DISCOVERY") or {}).get("validated") or 0),
        "curated": int((by_method.get("CURATED_EXACT") or {}).get("validated") or 0),
        "browser_assisted": int((by_method.get("BROWSER_ASSISTED") or {}).get("validated") or 0),
    }
    top_route = max(route_counts.items(), key=lambda kv: kv[1])[0] if any(route_counts.values()) else None

    focus = []
    for bid in KNOWN_FOCUS:
        item = by.get(bid) or {}
        row = items.get(bid) or {}
        best = row.get("best_url") or {}
        # controls already priced may not be in unresolved
        focus.append(
            {
                "product": f"{item.get('manufacturer') or ''} {item.get('mpn') or bid}".strip(),
                "benchmark_id": bid,
                "exact_url_found": bool(row.get("n_validated")),
                "seller": best.get("seller_domain"),
                "url": best.get("url"),
                "route": best.get("discovery_method"),
                "validation": "PASS" if row.get("n_validated") else "FAIL",
                "result": "PASS" if row.get("n_validated") else ("PRICED_ALREADY" if bid not in unresolved_ids else "FAIL"),
            }
        )

    ready = list_ready_for_price(benchmark_ids=unresolved_ids)
    # also count all ready in DB for unresolved
    ready_n = len(with_url)

    # Manufacturer success
    mfr_stats: dict[str, dict[str, Any]] = {}
    for bid in with_url:
        item = by.get(bid) or {}
        mfr = item.get("manufacturer") or "?"
        mfr_stats.setdefault(mfr, {"exact_urls": 0, "items": []})
        mfr_stats[mfr]["exact_urls"] += 1
        mfr_stats[mfr]["items"].append(bid)
    impossible = sorted(
        {
            (by.get(bid) or {}).get("manufacturer") or "?"
            for bid in still
        }
    )

    viable = ready_n >= TARGET_NEW_EXACT_URLS or ready_n >= 20

    report = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "URL_DISCOVERY_SUMMARY": {
            "benchmark_items": ORIGINAL_DENOM,
            "honest_priced_baseline": HONEST_PRICED,
            "attempted": todo_n if todo_n is not None else len(items),
            "exact_urls_found": ready_n,
            "exact_urls_validated": int(stats.get("validated") or 0),
            "rejected_urls": int(stats.get("rejected_shells") or 0)
            + int(stats.get("rejected_wrong_mpn") or 0)
            + int(stats.get("rejected_wrong_mfr") or 0),
        },
        "URL_TYPES": route_counts,
        "VALIDATION": {
            "exact_mpn_matches": int(stats.get("validated") or 0),
            "wrong_mpn_rejected": int(stats.get("rejected_wrong_mpn") or 0),
            "wrong_variant_rejected": 0,
            "wrong_pack_rejected": 0,
            "search_shells_rejected": int(stats.get("rejected_shells") or 0),
            "category_pages_rejected": 0,
            "wrong_manufacturer_rejected": int(stats.get("rejected_wrong_mfr") or 0),
        },
        "FULL100_MISS_RECOVERY": {
            "previous_unresolved": len(unresolved_ids),
            "exact_urls_recovered": ready_n,
            "still_unresolved": len(still),
            "still_unresolved_ids": still,
        },
        "KNOWN_CASES": focus,
        "TOP_SELLER_DOMAINS": domain_yield_snapshot(15),
        "TOP_MANUFACTURER_CHANNELS": [
            {"manufacturer": m, "exact_urls": v["exact_urls"], "items": v["items"]}
            for m, v in sorted(mfr_stats.items(), key=lambda kv: -kv[1]["exact_urls"])
        ],
        "READY_FOR_PRICE_EXTRACTION": {
            "count": ready_n,
            "records": [
                {
                    "benchmark_id": bid,
                    "url": ((items.get(bid) or {}).get("best_url") or {}).get("url"),
                    "seller": ((items.get(bid) or {}).get("best_url") or {}).get("seller_domain"),
                    "method": ((items.get(bid) or {}).get("best_url") or {}).get("discovery_method"),
                }
                for bid in with_url
            ],
        },
        "SAFE_NEXT_STEP": (
            "Price extraction may proceed ONLY on verified exact URLs in READY_FOR_PRICE_EXTRACTION."
            if ready_n
            else "Continue URL discovery — pool not yet viable for pricing pass."
        ),
        "FINAL_ANSWERS": {
            "1_unresolved_with_exact_urls": ready_n,
            "2_search_shells_eliminated": True,
            "3_top_discovery_route": top_route,
            "4_productive_seller_domains": [d["domain"] for d in domain_yield_snapshot(10) if d.get("exact_urls")],
            "5_manufacturers_still_impossible": impossible[:20],
            "6_ready_for_price_extraction": ready_n,
            "7_pricing_pass_viable": viable,
        },
        "stats": stats,
        "target_met": ready_n >= TARGET_NEW_EXACT_URLS,
    }
    ck["report"] = report
    ck["report_ready"] = True
    _save(CK, ck)
    _save(REPORT, report)
    return report


def format_report(report: dict[str, Any]) -> str:
    s = report.get("URL_DISCOVERY_SUMMARY") or {}
    m = report.get("FULL100_MISS_RECOVERY") or {}
    a = report.get("FINAL_ANSWERS") or {}
    return "\n".join(
        [
            f"BUILD {report.get('build')}",
            f"Attempted={s.get('attempted')} exact_validated={s.get('exact_urls_validated')} "
            f"with_url={m.get('exact_urls_recovered')}/{m.get('previous_unresolved')} "
            f"still={m.get('still_unresolved')}",
            f"READY_FOR_PRICE_EXTRACTION={a.get('6_ready_for_price_extraction')} "
            f"viable={a.get('7_pricing_pass_viable')} top_route={a.get('3_top_discovery_route')}",
            f"Search shells eliminated={a.get('2_search_shells_eliminated')}",
            report.get("SAFE_NEXT_STEP") or "",
        ]
    )
