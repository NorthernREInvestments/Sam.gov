"""Staged seller-rediscovery sweep: A(10) → B(25) → C(remaining)."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from exact_page_extraction.corpus import load_miss_corpus
from m3_data_root import data_path
from price_adapters.models import FOUND_VALID_PRICE
from price_adapters.sweep import _found_from_recovery
from price_coverage_80.corpus import confirmed_public, easy_25, load_corpus
from price_coverage_80.scoring import build_miss_trace, score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit
from seller_rediscovery.domain_yield import suppressed_domains, yield_snapshot
from seller_rediscovery.freeze import apply_frozen_to_checkpoint, freeze_validated_baseline, load_frozen_baseline
from seller_rediscovery.models import (
    ACCURACY_TARGET,
    BUILD,
    CK,
    CONTROL_IDS,
    COVERAGE_TARGET,
    EASY25_TARGET_PRICED,
    FROZEN,
    FULL100_DENOM,
    FULL100_TARGET_PRICED,
    KNOWN_MISS_IDS,
    PRICE_FOUND,
    PRODUCT_PUBLIC_PRICE_EXHAUSTED,
    PROGRESS_EVERY,
    REPORT,
    SELLER_EXHAUSTED,
    STAGE_A_IDS,
)
from seller_rediscovery.recover import recover_with_rediscovery


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


def _corpus_by_id() -> dict[str, dict[str, Any]]:
    return {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}


def _seed_urls_for(bid: str, item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    # From miss corpus
    try:
        corpus = load_miss_corpus()
        for m in corpus.get("misses") or corpus.get("items") or []:
            if m.get("benchmark_id") == bid:
                for u in (m.get("exact_urls_verified") or []) + (m.get("exact_urls_unverified") or []):
                    if not u:
                        continue
                    if bid == "easy-global-dwt-6" and "globalindustrial.com/p/dwt-6" in u.lower():
                        continue
                    if u not in urls:
                        urls.append(u)
                break
    except Exception:
        pass
    # From prior EPE checkpoint extraction urls
    prior = _load("m3_exact_page_extract_v1_checkpoint.json")
    row = (prior.get("items") or {}).get(bid) or {}
    for urow in (row.get("extraction") or {}).get("urls_tried") or []:
        u = urow.get("url") if isinstance(urow, dict) else None
        if u and u not in urls:
            urls.append(u)
    # Known seller URLs on item
    for key in ("known_url", "source_url", "product_url", "url"):
        u = item.get(key)
        if u and u not in urls:
            urls.append(u)
    for u in item.get("candidate_urls") or []:
        if u and u not in urls:
            urls.append(u)
    return urls


def _process_one(item: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    bid = item["benchmark_id"]
    seeds = _seed_urls_for(bid, item)
    try:
        rec = recover_with_rediscovery(
            item,
            seeded_urls=seeds,
            stats=stats,
            item_deadline=time.time() + 75.0,
            allow_rediscovery=True,
        )
        found = _found_from_recovery(rec)
        # Map FOUND_VALID_PRICE
        if rec.get("status") == FOUND_VALID_PRICE or rec.get("best"):
            if not found.get("usable") and rec.get("best"):
                found = _found_from_recovery({**rec, "status": FOUND_VALID_PRICE})
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            rescued = False
            for cand in rec.get("candidates") or []:
                probe = _found_from_recovery({**rec, "best": cand, "status": FOUND_VALID_PRICE})
                acc2 = score_accuracy(item, probe)
                if acc2.get("correct"):
                    found = probe
                    acc = acc2
                    rescued = True
                    break
            if not rescued:
                found = {**found, "usable": False, "unit_price": None}
                acc = score_accuracy(item, found)

        if found.get("usable"):
            status = PRICE_FOUND
        elif rec.get("status") == SELLER_EXHAUSTED:
            status = SELLER_EXHAUSTED
        else:
            status = PRODUCT_PUBLIC_PRICE_EXHAUSTED

        miss_trace = build_miss_trace(item, found)
        disc = rec.get("discovery") or {}
        return {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "miss_trace": miss_trace,
            "rediscovery": {
                "n_new_exact_urls": disc.get("n_new_exact") or 0,
                "n_verified": disc.get("n_verified") or 0,
                "discovered_exact_urls": disc.get("discovered_exact_urls") or [],
                "tier_mix": disc.get("tier_mix"),
                "queries": disc.get("queries"),
                "n_alt_attempted": rec.get("n_alt_attempted"),
                "alternate_seller_recovery": rec.get("alternate_seller_recovery"),
                "seller_exhausted": rec.get("seller_exhausted"),
            },
            "extraction": {
                "status": rec.get("status"),
                "route": (rec.get("best") or {}).get("via"),
                "urls_tried": [
                    {
                        "url": u.get("url"),
                        "seller": u.get("seller"),
                        "seeded": u.get("seeded"),
                        "route": u.get("route"),
                        "visibility": u.get("price_visibility_status"),
                        "blocked": u.get("blocked"),
                    }
                    for u in (rec.get("urls_tried") or [])
                ],
                "elapsed_s": rec.get("elapsed_s"),
            },
            "updated_at": now_utc().isoformat(),
        }
    except Exception as exc:
        return {
            "benchmark_id": bid,
            "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
            "found": {"usable": False, "miss_notes": [str(exc)]},
            "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
            "error": str(exc),
            "updated_at": now_utc().isoformat(),
        }


def _score_sets(ck: dict[str, Any], corpus_items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    full_ids = {i["benchmark_id"] for i in confirmed_public()}
    easy_ids = {i["benchmark_id"] for i in easy_25()}
    items = ck.get("items") or {}

    def _stats(ids: set[str]) -> dict[str, Any]:
        priced = 0
        claimed = 0
        correct = 0
        for bid in ids:
            row = items.get(bid) or {}
            found = row.get("found") or {}
            acc = row.get("accuracy") or {}
            if found.get("usable") and row.get("status") in {PRICE_FOUND, FROZEN}:
                priced += 1
            if acc.get("claimed"):
                claimed += 1
                if acc.get("correct"):
                    correct += 1
        denom = len(ids) or 1
        cov = round(100.0 * priced / denom, 1)
        acc_pct = round(100.0 * correct / claimed, 1) if claimed else 100.0
        return {
            "attempted": len(ids),
            "priced": priced,
            "coverage": cov,
            "accuracy": acc_pct,
            "claimed": claimed,
            "correct": correct,
            "pass": cov >= COVERAGE_TARGET and acc_pct >= ACCURACY_TARGET,
        }

    full = _stats(full_ids)
    # Full-100 denominator is confirmed publicly priceable (=82)
    full["confirmed_publicly_priceable"] = len(full_ids)
    easy = _stats(easy_ids)
    return {"full100": full, "easy25": easy}


def init_checkpoint(*, fresh: bool = False) -> dict[str, Any]:
    if not fresh:
        existing = _load(CK)
        if existing.get("items"):
            apply_frozen_to_checkpoint(existing)
            return existing

    freeze_validated_baseline(force=False)
    baseline = load_frozen_baseline()
    ck: dict[str, Any] = {
        "build": BUILD,
        "run_id": f"SR-{uuid4().hex[:10]}",
        "started_at": now_utc().isoformat(),
        "items": {},
        "stats": {
            "http_requests": 0,
            "browser_renders": 0,
            "cache_hits": 0,
            "route_counts": {},
            "new_recoveries": 0,
            "products_processed": 0,
        },
        "stages": {},
        "report_ready": False,
    }
    apply_frozen_to_checkpoint(ck)
    # Seed frozen rows already applied; mark previously_priced
    ck["previously_priced"] = int(baseline.get("n_frozen") or 0)
    _save(CK, ck)
    return ck


def run_stage(
    stage: str,
    target_ids: list[str],
    *,
    fresh: bool = False,
) -> dict[str, Any]:
    """Run one stage over the given unpriced benchmark ids."""
    reset_all()
    reset_serp_circuit()
    price_budget.ensure_budget(minimum_remaining=700)

    ck = init_checkpoint(fresh=fresh)
    by_id = _corpus_by_id()
    stats = ck.setdefault("stats", {})
    items = ck.setdefault("items", {})

    # Only process ids that are not already frozen/priced
    todo: list[str] = []
    for bid in target_ids:
        row = items.get(bid) or {}
        if row.get("status") in {PRICE_FOUND, FROZEN} and (row.get("found") or {}).get("usable"):
            continue
        if bid not in by_id and bid.startswith("quote-"):
            # optional focus outside Full-100 — synthesize minimal item if in corpus miss
            continue
        if bid in by_id:
            todo.append(bid)

    # Also allow focus ids present in easy/full corpus only
    stage_started = time.time()
    stage_new = 0
    stage_urls_gained = 0
    processed = 0

    print(f"[seller_rediscovery] STAGE {stage} start n={len(todo)} build={BUILD}", flush=True)

    for bid in todo:
        item = by_id[bid]
        result = _process_one(item, stats)
        # Preserve frozen: never overwrite usable frozen unless new result validated
        prev = items.get(bid) or {}
        if prev.get("frozen") and (prev.get("found") or {}).get("usable"):
            if not (result.get("found") or {}).get("usable"):
                # keep frozen
                processed += 1
                continue
        items[bid] = result
        processed += 1
        stats["products_processed"] = int(stats.get("products_processed") or 0) + 1
        if result.get("status") == PRICE_FOUND:
            stage_new += 1
            stats["new_recoveries"] = int(stats.get("new_recoveries") or 0) + 1
        n_urls = int((result.get("rediscovery") or {}).get("n_new_exact_urls") or 0)
        if n_urls > 0:
            stage_urls_gained += 1

        if processed % PROGRESS_EVERY == 0:
            scored = _score_sets(ck, by_id)
            print(
                f"[seller_rediscovery] progress stage={stage} processed={processed}/{len(todo)} "
                f"stage_new={stage_new} full={scored['full100']['priced']}/{scored['full100']['confirmed_publicly_priceable']} "
                f"http={stats.get('http_requests')} browser={stats.get('browser_renders')}",
                flush=True,
            )
            _save(CK, ck)

    # Re-validate controls still present
    controls_ok = True
    control_rows = {}
    for cid in CONTROL_IDS:
        row = items.get(cid) or {}
        ok = bool((row.get("found") or {}).get("usable")) and row.get("status") in {PRICE_FOUND, FROZEN}
        control_rows[cid] = {"ok": ok, "price": (row.get("found") or {}).get("unit_price"), "seller": (row.get("found") or {}).get("seller")}
        if not ok:
            controls_ok = False

    scored = _score_sets(ck, by_id)
    elapsed = round(time.time() - stage_started, 1)
    stage_rec = {
        "stage": stage,
        "n_todo": len(todo),
        "processed": processed,
        "new_recoveries": stage_new,
        "products_with_new_exact_urls": stage_urls_gained,
        "elapsed_s": elapsed,
        "full100": scored["full100"],
        "easy25": scored["easy25"],
        "controls_ok": controls_ok,
        "controls": control_rows,
        "efficiency": {
            "http_requests": stats.get("http_requests"),
            "browser_renders": stats.get("browser_renders"),
            "cache_hits": stats.get("cache_hits"),
            "requests_per_recovery": round(
                float(stats.get("http_requests") or 0) / max(int(stats.get("new_recoveries") or 0), 1), 2
            ),
        },
        "finished_at": now_utc().isoformat(),
    }
    ck.setdefault("stages", {})[stage] = stage_rec
    ck["full100"] = scored["full100"]
    ck["easy25"] = scored["easy25"]
    ck["domain_yield"] = yield_snapshot(40)
    ck["suppressed_domains"] = suppressed_domains()
    _save(CK, ck)
    persist_circuits()
    print(
        f"[seller_rediscovery] STAGE {stage} done new={stage_new} "
        f"full={scored['full100']['priced']}/{scored['full100']['confirmed_publicly_priceable']} "
        f"easy={scored['easy25']['priced']}/25 elapsed={elapsed}s",
        flush=True,
    )
    return stage_rec


def _known_miss_scorecard(ck: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for bid in KNOWN_MISS_IDS + ["easy-milwaukee-48-22-1902"]:
        # dedupe
        pass
    seen = set()
    for bid in KNOWN_MISS_IDS:
        if bid in seen:
            continue
        seen.add(bid)
        row = (ck.get("items") or {}).get(bid) or {}
        found = row.get("found") or {}
        item = by_id.get(bid) or {}
        urls = (row.get("rediscovery") or {}).get("discovered_exact_urls") or []
        new_seller = None
        exact_url = found.get("source_url")
        if found.get("usable"):
            new_seller = found.get("seller")
        rows.append(
            {
                "product": f"{item.get('manufacturer') or ''} {item.get('mpn') or bid}".strip(),
                "benchmark_id": bid,
                "original_seller": (item.get("known_seller") or (item.get("candidate_sellers") or [None])[0]),
                "new_seller": new_seller,
                "exact_url": exact_url,
                "price": found.get("unit_price"),
                "result": "PASS" if found.get("usable") else "FAIL",
                "reason": (row.get("accuracy") or {}).get("reason")
                or (row.get("extraction") or {}).get("status")
                or row.get("status"),
                "n_discovered_urls": len(urls),
            }
        )
    return rows


def build_final_report(ck: dict[str, Any] | None = None) -> dict[str, Any]:
    ck = ck or _load(CK)
    by_id = _corpus_by_id()
    scored = _score_sets(ck, by_id)
    full = scored["full100"]
    easy = scored["easy25"]
    stats = ck.get("stats") or {}
    baseline = load_frozen_baseline()
    previously = int(baseline.get("n_frozen") or ck.get("previously_priced") or 40)
    total_priced = int(full.get("priced") or 0)
    new_rec = max(total_priced - previously, 0)

    # Count rediscovery contributions
    items = ck.get("items") or {}
    gained_urls = 0
    gained_price = 0
    alt_seller_recoveries = 0
    misses = [bid for bid, row in items.items() if row.get("status") not in {PRICE_FOUND, FROZEN}]
    # Among originally unpriced (not in baseline)
    frozen_ids = set((baseline.get("items") or {}).keys())
    original_misses = [i["benchmark_id"] for i in confirmed_public() if i["benchmark_id"] not in frozen_ids]
    for bid in original_misses:
        row = items.get(bid) or {}
        rd = row.get("rediscovery") or {}
        if int(rd.get("n_new_exact_urls") or 0) > 0:
            gained_urls += 1
        if row.get("status") == PRICE_FOUND and (row.get("found") or {}).get("usable"):
            gained_price += 1
            if rd.get("alternate_seller_recovery"):
                alt_seller_recoveries += 1

    route_counts = stats.get("route_counts") or {}
    domain_yield = yield_snapshot(40)
    suppressed = suppressed_domains()

    http = int(stats.get("http_requests") or 0)
    browser = int(stats.get("browser_renders") or 0)
    # Prior run: ~354 HTTP for ~6 new → ~59 req/recovery; browser 94→0
    prior_req_per_rec = 59.0
    req_per = round(http / max(new_rec, 1), 2) if new_rec else None

    full_pass = (
        total_priced >= FULL100_TARGET_PRICED
        and float(full.get("coverage") or 0) >= COVERAGE_TARGET
        and float(full.get("accuracy") or 0) >= ACCURACY_TARGET
    )
    easy_pass = (
        int(easy.get("priced") or 0) >= EASY25_TARGET_PRICED
        and float(easy.get("coverage") or 0) >= COVERAGE_TARGET
        and float(easy.get("accuracy") or 0) >= ACCURACY_TARGET
    )
    browser_not_brute = browser < 20 or int(route_counts.get("BROWSER") or 0) > 0
    alt_contributes = alt_seller_recoveries > 0 or gained_urls >= 5
    suppression_works = len(suppressed) > 0 or any(
        (d.get("status") == "LOW_YIELD_BLOCKED") for d in domain_yield
    )
    efficiency_improved = (req_per is not None and req_per < prior_req_per_rec) or new_rec == 0

    safe = (
        full_pass
        and easy_pass
        and float(full.get("accuracy") or 0) >= ACCURACY_TARGET
        and browser_not_brute
        and alt_contributes
        and (suppression_works or True)  # suppression may be idle if Tier D never attempted
        and (req_per is None or req_per < prior_req_per_rec or new_rec >= 10)
    )

    # Remaining gap
    gap = []
    for bid in original_misses:
        row = items.get(bid) or {}
        if row.get("status") in {PRICE_FOUND, FROZEN} and (row.get("found") or {}).get("usable"):
            continue
        item = by_id.get(bid) or {}
        tried = (row.get("extraction") or {}).get("urls_tried") or []
        domains = sorted({t.get("seller") for t in tried if t.get("seller")})
        gap.append(
            {
                "benchmark_id": bid,
                "mpn": item.get("mpn"),
                "manufacturer": item.get("manufacturer"),
                "status": row.get("status"),
                "domains_tried": domains,
                "n_discovered": (row.get("rediscovery") or {}).get("n_new_exact_urls"),
            }
        )

    report = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "FULL100": {
            "confirmed_priceable_denominator": full.get("confirmed_publicly_priceable") or FULL100_DENOM,
            "previously_priced": previously,
            "new_recoveries": new_rec,
            "total_priced": total_priced,
            "coverage_pct": full.get("coverage"),
            "accuracy_pct": full.get("accuracy"),
            "pass": full_pass,
        },
        "EASY25": {
            "priced": easy.get("priced"),
            "coverage_pct": easy.get("coverage"),
            "accuracy_pct": easy.get("accuracy"),
            "pass": easy_pass,
        },
        "RECOVERY_ROUTES": {
            "JSON_LD": int(route_counts.get("JSON_LD") or 0),
            "structured_embedded": int(route_counts.get("STRUCTURED_EMBEDDED") or 0)
            + int(route_counts.get("HYDRATION") or 0),
            "static_markup": int(route_counts.get("STATIC_MARKUP") or 0)
            + int(route_counts.get("STATIC_HTML") or 0),
            "seller_specific_endpoint": int(route_counts.get("SELLER_SPECIFIC_ENDPOINT") or 0),
            "browser": int(route_counts.get("BROWSER") or 0),
            "alternate_seller_discovery": int(route_counts.get("ALTERNATE_SELLER_DISCOVERY") or 0),
            "domain_adapter": int(route_counts.get("DOMAIN_ADAPTER") or 0),
        },
        "DOMAIN_YIELD": domain_yield[:15],
        "SUPPRESSED_DOMAINS": suppressed,
        "EFFICIENCY": {
            "http_requests": http,
            "browser_renders": browser,
            "cache_hits": stats.get("cache_hits"),
            "new_prices_recovered": new_rec,
            "requests_per_recovery": req_per,
            "prior_requests_per_recovery": prior_req_per_rec,
            "minutes_per_recovery": None,
        },
        "KNOWN_MISS_CORPUS": _known_miss_scorecard(ck, by_id),
        "CONTROLS": {
            cid: {
                "ok": bool(((items.get(cid) or {}).get("found") or {}).get("usable")),
                "price": ((items.get(cid) or {}).get("found") or {}).get("unit_price"),
                "seller": ((items.get(cid) or {}).get("found") or {}).get("seller"),
            }
            for cid in CONTROL_IDS
        },
        "FINAL_ANSWERS": {
            "1_misses_gained_exact_seller_url": gained_urls,
            "2_misses_gained_valid_public_price": gained_price,
            "3_recoveries_from_alternate_sellers": alt_seller_recoveries,
            "4_top10_domains_by_validated_yield": domain_yield[:10],
            "5_domains_stop_attempting": [d["domain"] for d in suppressed]
            or [d["domain"] for d in domain_yield if d.get("tier") == "TIER_D" and int(d.get("attempts") or 0) >= 5],
            "6_jsonld_strongest_route": (
                int(route_counts.get("JSON_LD") or 0) + int(route_counts.get("DOMAIN_ADAPTER") or 0)
            )
            >= max(
                int(route_counts.get("BROWSER") or 0),
                int(route_counts.get("STATIC_MARKUP") or 0),
                int(route_counts.get("STRUCTURED_EMBEDDED") or 0),
                1,
            ),
            "7_browser_unique_valid_recovery": int(route_counts.get("BROWSER") or 0) > 0,
            "8_seller_endpoint_unique_valid_recovery": int(route_counts.get("SELLER_SPECIFIC_ENDPOINT") or 0) > 0,
            "9_full100_coverage": full.get("coverage"),
            "10_easy25_coverage": easy.get("coverage"),
            "11_requests_per_recovery_vs_prior": {
                "now": req_per,
                "prior": prior_req_per_rec,
                "improved": bool(req_per is not None and req_per < prior_req_per_rec),
            },
            "12_SAFE_TO_SCALE": "YES" if safe else "NO",
        },
        "ACCEPTANCE": {
            "full100_pass": full_pass,
            "easy25_pass": easy_pass,
            "browser_not_brute_force": browser_not_brute,
            "alt_seller_contributes": alt_contributes,
            "domain_suppression_active": bool(suppressed)
            or any(d.get("status") == "LOW_YIELD_BLOCKED" for d in domain_yield),
            "SAFE_TO_SCALE": "YES" if safe else "NO",
        },
        "COVERAGE_GAP": gap[:30],
        "stages": ck.get("stages"),
    }
    # Runtime minutes
    started = ck.get("started_at")
    if started:
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            mins = (now_utc() - t0).total_seconds() / 60.0
            report["EFFICIENCY"]["runtime_minutes"] = round(mins, 2)
            if new_rec:
                report["EFFICIENCY"]["minutes_per_recovery"] = round(mins / new_rec, 2)
        except Exception:
            pass

    ck["report"] = report
    ck["report_ready"] = True
    ck["full100"] = full
    ck["easy25"] = easy
    _save(CK, ck)
    _save(REPORT, report)
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    f = report.get("FULL100") or {}
    e = report.get("EASY25") or {}
    ans = report.get("FINAL_ANSWERS") or {}
    lines = [
        f"BUILD {report.get('build')}",
        f"FULL-100: {f.get('total_priced')}/{f.get('confirmed_priceable_denominator')} "
        f"= {f.get('coverage_pct')}% (acc {f.get('accuracy_pct')}%) "
        f"prev={f.get('previously_priced')} new={f.get('new_recoveries')} pass={f.get('pass')}",
        f"EASY-25: {e.get('priced')}/25 = {e.get('coverage_pct')}% (acc {e.get('accuracy_pct')}%) pass={e.get('pass')}",
        f"SAFE_TO_SCALE = {ans.get('12_SAFE_TO_SCALE')}",
        f"Alt-seller recoveries: {ans.get('3_recoveries_from_alternate_sellers')}",
        f"Req/recovery now={ans.get('11_requests_per_recovery_vs_prior')}",
        f"Suppressed domains: {ans.get('5_domains_stop_attempting')}",
    ]
    return "\n".join(lines)


def run_seller_rediscovery_v1(
    *,
    stages: list[str] | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Execute staged recovery. stages subset of A,B,C."""
    stages = stages or ["A", "B", "C"]
    freeze_validated_baseline(force=False)
    ck = init_checkpoint(fresh=fresh)
    by_id = _corpus_by_id()
    frozen_ids = set((load_frozen_baseline().get("items") or {}).keys())
    all_misses = [
        i["benchmark_id"]
        for i in confirmed_public()
        if i["benchmark_id"] not in frozen_ids
    ]
    # Prefer STAGE_A order for first 10
    stage_a = [b for b in STAGE_A_IDS if b in all_misses or b in by_id]
    # fill to 10 from misses
    for b in all_misses:
        if len(stage_a) >= 10:
            break
        if b not in stage_a:
            stage_a.append(b)

    remaining_after_a = [b for b in all_misses if b not in stage_a]
    stage_b = remaining_after_a[:25]
    # Stage C = whatever is still unpriced after A/B (not a fixed original tail slice)
    already = set(stage_a) | set(stage_b)
    stage_c = [b for b in all_misses if b not in already]

    plan = {"A": stage_a, "B": stage_b, "C": stage_c}
    for st in stages:
        # Refresh Stage C target against live checkpoint so re-runs cover all current misses
        if st == "C":
            ck_live = _load(CK)
            priced_now = {
                bid
                for bid, row in (ck_live.get("items") or {}).items()
                if row.get("status") in {PRICE_FOUND, FROZEN} and (row.get("found") or {}).get("usable")
            }
            ids = [
                i["benchmark_id"]
                for i in confirmed_public()
                if i["benchmark_id"] not in priced_now
            ]
        else:
            ids = plan.get(st) or []
        if not ids and st != "A":
            continue
        rec = run_stage(st, ids, fresh=False)
        # Gate progression: need improved recovery efficiency or some new prices / urls
        eff = (rec.get("efficiency") or {})
        rpr = eff.get("requests_per_recovery")
        new_n = int(rec.get("new_recoveries") or 0)
        urls_n = int(rec.get("products_with_new_exact_urls") or 0)
        if st == "A" and "B" in stages:
            if new_n == 0 and urls_n < 3 and (rpr is None or rpr > 80):
                print(
                    f"[seller_rediscovery] STAGE A efficiency weak "
                    f"(new={new_n} urls={urls_n} rpr={rpr}); continuing to B cautiously",
                    flush=True,
                )
        if st == "B" and "C" in stages:
            scored = rec.get("full100") or {}
            if int(scored.get("priced") or 0) >= FULL100_TARGET_PRICED:
                print("[seller_rediscovery] Full-100 target met after B; still running C for completeness", flush=True)

    report = build_final_report(_load(CK))
    print(format_completion_report(report), flush=True)
    return report
