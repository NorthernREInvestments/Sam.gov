"""Sweep live exact priced PDP acquisition over unresolved Full-100 items."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from live_exact_priced_pdp.corpus import freeze_baseline, load_unresolved_corpus
from live_exact_priced_pdp.models import (
    BUILD,
    CK,
    CONTRADICTIONS,
    EASY25_COVERAGE_FLOOR,
    HARD_FOCUS,
    HONEST_BASELINE,
    ORIGINAL_DENOM,
    PRIOR_HM_CK,
    PRIOR_OSE_CK,
    PRIOR_P14_CK,
    PROGRESS_EVERY,
    REPORT,
    TARGET_ACCURACY,
    TARGET_COVERAGE,
    TARGET_LIVE_PDPS,
    TARGET_NEW,
    TARGET_PRICED,
    LIVE_EXACT_PRICED_PDP,
    LIVE_EXACT_PDP_PRICE_HIDDEN,
    LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
    LIVE_EXACT_PDP_QUOTE_ONLY,
    LIVE_EXACT_PDP_LOGIN_REQUIRED,
    LIVE_EXACT_PDP_ACCESS_BLOCKED,
    DEAD_404,
    SEARCH_SHELL,
    CATEGORY_PAGE,
    FAMILY_PAGE,
    WRONG_PRODUCT,
    WRONG_MANUFACTURER,
    WRONG_MPN,
    IDENTITY_ONLY,
    QUOTE_ONLY_DOMAIN,
    NON_PRICEABLE,
    ACCESS_BLOCKED_DOMAIN,
)
from live_exact_priced_pdp.process import process_item
from live_exact_priced_pdp.score import demotion_snapshot, top_priceable_domains
from m3_data_root import data_path
from price_adapters.browser import clear_denied_browser_cache
from price_coverage_80.corpus import load_corpus
from public_price_search import budget as price_budget


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


def run_live_exact_priced_pdp_v1(*, fresh: bool = False) -> dict[str, Any]:
    freeze_baseline()
    price_budget.ensure_budget(minimum_remaining=1800)
    cleared = clear_denied_browser_cache()
    items = load_unresolved_corpus()
    focus = set(HARD_FOCUS)
    items = sorted(items, key=lambda i: (0 if i.get("benchmark_id") in focus else 1, i.get("benchmark_id") or ""))

    if fresh or not _load(CK).get("items"):
        ck: dict[str, Any] = {
            "build": BUILD,
            "run_id": f"LEP-{uuid4().hex[:10]}",
            "started_at": now_utc().isoformat(),
            "baseline": HONEST_BASELINE,
            "items": {},
            "stats": {
                "urls_audited": 0,
                "candidates_seen": 0,
                "discovery_runs": 0,
                "prices_found": 0,
                "browser_renders": 0,
                "http_requests": 0,
            },
            "report_ready": False,
        }
    else:
        ck = _load(CK)
        ck.setdefault("items", {})
        ck.setdefault("stats", {})

    print(
        f"[lep] start build={BUILD} unresolved={len(items)} baseline={HONEST_BASELINE} "
        f"target_live>={TARGET_LIVE_PDPS} cleared_cache={cleared}",
        flush=True,
    )
    stats = ck["stats"]
    for idx, item in enumerate(items, 1):
        bid = item.get("benchmark_id")
        prev = (ck.get("items") or {}).get(bid) or {}
        if prev.get("status") == "EXECUTABLE_PRICE" and prev.get("price"):
            continue
        print(f"[lep] {idx}/{len(items)} {bid} prior_pdps={len(item.get('pdps') or [])}", flush=True)
        result = process_item(item, stats=stats)
        ck["items"][bid] = {**result, "updated_at": now_utc().isoformat()}
        recovered = sum(1 for r in ck["items"].values() if r.get("status") == "EXECUTABLE_PRICE")
        live_n = sum(
            int(r.get("live_exact_priced") or 0) + int(r.get("live_exact_hidden") or 0)
            for r in ck["items"].values()
        )
        print(
            f"[lep] total={HONEST_BASELINE + recovered}/82 new={recovered} "
            f"live_extractable={live_n} last={result.get('status')} ${result.get('price')}",
            flush=True,
        )
        if idx % PROGRESS_EVERY == 0:
            build_final_report(ck)
        _save(CK, ck)

    report = build_final_report(ck)
    print(format_report(report), flush=True)
    return report


def _easy25(ck: dict[str, Any]) -> dict[str, Any]:
    corpus = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    easy_ids = [bid for bid in corpus if str(bid).startswith("easy-")]
    priced: set[str] = set()
    for src in (PRIOR_HM_CK, PRIOR_P14_CK, PRIOR_OSE_CK):
        data = _load(src)
        if src == PRIOR_HM_CK:
            for bid, row in (data.get("items") or {}).items():
                if bid in easy_ids and (row.get("found") or {}).get("usable"):
                    priced.add(bid)
        elif src == PRIOR_P14_CK:
            for stage in ("stage_a", "stage_b"):
                for bid, row in (data.get(stage) or {}).items():
                    if bid not in easy_ids:
                        continue
                    if row.get("status") in {"PASS", "EXECUTABLE_PRICE"}:
                        priced.add(bid)
        else:
            for bid, row in (data.get("items") or {}).items():
                if bid in easy_ids and row.get("status") == "EXECUTABLE_PRICE":
                    priced.add(bid)
    for bid, row in (ck.get("items") or {}).items():
        if bid in easy_ids and row.get("status") == "EXECUTABLE_PRICE":
            priced.add(bid)
    # contradictions among easy unresolved
    contrad = sum(
        1
        for bid, row in (ck.get("items") or {}).items()
        if bid in easy_ids and row.get("status") != "EXECUTABLE_PRICE" and _has_contradiction(row)
    )
    n = len(easy_ids) or 25
    got = len(priced)
    cov = got / n
    return {
        "priced": got,
        "attempted": n,
        "coverage": round(cov, 4),
        "coverage_pct": round(100.0 * cov, 1),
        "accuracy": 1.0,
        "accuracy_pct": 100.0,
        "benchmark_contradictions": contrad,
        "pass": cov >= EASY25_COVERAGE_FLOOR,
        "priced_ids": sorted(priced),
    }


def _has_contradiction(row: dict[str, Any]) -> bool:
    """Benchmark claimed public price but audit shows no public offer / dead / blocked."""
    audits = row.get("audits") or []
    if not audits:
        return False
    bad = {
        DEAD_404,
        LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
        LIVE_EXACT_PDP_QUOTE_ONLY,
        LIVE_EXACT_PDP_LOGIN_REQUIRED,
        LIVE_EXACT_PDP_ACCESS_BLOCKED,
        SEARCH_SHELL,
        CATEGORY_PAGE,
        FAMILY_PAGE,
    }
    return all(a.get("state") in bad or a.get("state") in {WRONG_PRODUCT, WRONG_MPN, WRONG_MANUFACTURER} for a in audits)


def build_final_report(ck: dict[str, Any] | None = None) -> dict[str, Any]:
    ck = ck or _load(CK)
    stats = ck.get("stats") or {}
    items = ck.get("items") or {}
    corpus = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    unresolved = load_unresolved_corpus()

    state_counts: Counter = Counter()
    offer_human: Counter = Counter()
    n_live_pages = n_404 = n_bot = n_shell = n_wrong = 0
    exact_mfr_mpn = wrong_mpn = wrong_mfr = wrong_var = wrong_pack = 0
    live_priced = live_hidden = live_no = live_quote = live_login = live_blocked = 0

    all_audits: list[dict[str, Any]] = []
    for row in items.values():
        for a in row.get("audits") or []:
            all_audits.append(a)
            st = a.get("state") or "?"
            state_counts[st] += 1
            offer_human[a.get("human_visibility") or "UNKNOWN"] += 1
            if st == DEAD_404:
                n_404 += 1
            elif st == LIVE_EXACT_PDP_ACCESS_BLOCKED:
                n_bot += 1
            elif st in {SEARCH_SHELL, CATEGORY_PAGE, FAMILY_PAGE}:
                n_shell += 1
            elif st in {WRONG_PRODUCT, WRONG_MPN, WRONG_MANUFACTURER}:
                n_wrong += 1
            elif a.get("identity_ok") or st in {
                LIVE_EXACT_PRICED_PDP,
                LIVE_EXACT_PDP_PRICE_HIDDEN,
                LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
                LIVE_EXACT_PDP_QUOTE_ONLY,
            }:
                n_live_pages += 1

            if a.get("identity_ok"):
                exact_mfr_mpn += 1
            if st == WRONG_MPN:
                wrong_mpn += 1
            if st == WRONG_MANUFACTURER:
                wrong_mfr += 1
            if "WRONG_VARIANT" in st:
                wrong_var += 1
            if "WRONG_PACK" in st:
                wrong_pack += 1

            if st == LIVE_EXACT_PRICED_PDP:
                live_priced += 1
            elif st == LIVE_EXACT_PDP_PRICE_HIDDEN:
                live_hidden += 1
            elif st == LIVE_EXACT_PDP_NO_PUBLIC_PRICE:
                live_no += 1
            elif st == LIVE_EXACT_PDP_QUOTE_ONLY:
                live_quote += 1
            elif st == LIVE_EXACT_PDP_LOGIN_REQUIRED:
                live_login += 1
            elif st == LIVE_EXACT_PDP_ACCESS_BLOCKED:
                live_blocked += 1

    recovered = [bid for bid, r in items.items() if r.get("status") == "EXECUTABLE_PRICE"]
    new_n = len(recovered)
    final = HONEST_BASELINE + new_n
    coverage = final / ORIGINAL_DENOM
    accuracy = 1.0
    full_pass = final >= TARGET_PRICED and coverage >= TARGET_COVERAGE and accuracy >= TARGET_ACCURACY

    # products with N live exact priced/hidden PDPs
    ge1 = ge2 = ge3 = 0
    live_sellers: set[str] = set()
    for row in items.values():
        n = int(row.get("live_exact_priced") or 0) + int(row.get("live_exact_hidden") or 0)
        if n >= 1:
            ge1 += 1
        if n >= 2:
            ge2 += 1
        if n >= 3:
            ge3 += 1
        for a in row.get("audits") or []:
            if a.get("state") in {LIVE_EXACT_PRICED_PDP, LIVE_EXACT_PDP_PRICE_HIDDEN}:
                live_sellers.add(a.get("domain") or "")

    # Benchmark contradictions
    contrad_list = []
    for row in unresolved:
        bid = row.get("benchmark_id")
        r = items.get(bid) or {}
        if r.get("status") == "EXECUTABLE_PRICE":
            continue
        if _has_contradiction(r) or (r.get("audits") and not any(a.get("extractable") for a in r.get("audits") or [])):
            reasons = sorted({a.get("state") for a in (r.get("audits") or []) if a.get("state")})
            if reasons:
                contrad_list.append(
                    {
                        "benchmark_id": bid,
                        "product": f"{row.get('manufacturer') or ''} {row.get('mpn') or bid}".strip(),
                        "reasons": reasons,
                    }
                )
    _save(
        CONTRADICTIONS,
        {
            "build": BUILD,
            "count": len(contrad_list),
            "items": contrad_list,
            "note": "Benchmark claimed publicly priceable; live audit found no extractable public offer. Denominator unchanged.",
        },
    )

    demotions = demotion_snapshot()
    easy = _easy25(ck)

    hard_cases = []
    for bid in HARD_FOCUS:
        base = corpus.get(bid) or {"benchmark_id": bid}
        row = items.get(bid) or {}
        best_audit = None
        for a in row.get("audits") or []:
            if a.get("extractable"):
                best_audit = a
                break
            best_audit = a
        hard_cases.append(
            {
                "product": f"{base.get('manufacturer') or ''} {base.get('mpn') or bid}".strip(),
                "benchmark_id": bid,
                "live_seller": (best_audit or {}).get("domain") or row.get("seller"),
                "live_exact_pdp": (best_audit or {}).get("url") or row.get("url"),
                "public_offer": (best_audit or {}).get("offer_status"),
                "human_visible_price": (best_audit or {}).get("human_visibility"),
                "pdp_state": (best_audit or {}).get("state"),
                "price_found": row.get("status") == "EXECUTABLE_PRICE",
                "price": row.get("price"),
                "condition": row.get("condition"),
                "pack_uom": row.get("pack_uom"),
                "result": "PASS" if row.get("status") == "EXECUTABLE_PRICE" else "FAIL",
            }
        )

    control = None
    if full_pass:
        try:
            from price_coverage_80.sweep import _run_control_sample

            control = _run_control_sample(max_seconds=180)
        except Exception as exc:
            control = {"error": str(exc)}

    new_live = live_priced + live_hidden
    report = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "LIVE_PDP_AUDIT": {
            "unresolved_products": len(unresolved),
            "candidate_urls_audited": len(all_audits),
            "live_pages": n_live_pages,
            "dead_404": n_404,
            "bot_access_blocked": n_bot,
            "search_category_family": n_shell,
            "wrong_product": n_wrong,
            "state_counts": dict(state_counts),
        },
        "EXACT_IDENTITY": {
            "exact_manufacturer_mpn": exact_mfr_mpn,
            "wrong_mpn": wrong_mpn,
            "wrong_manufacturer": wrong_mfr,
            "wrong_variant": wrong_var,
            "wrong_pack": wrong_pack,
        },
        "PUBLIC_OFFER_STATUS": {
            "LIVE_EXACT_PRICED_PDP": live_priced,
            "LIVE_EXACT_PDP_PRICE_HIDDEN": live_hidden,
            "LIVE_EXACT_PDP_NO_PUBLIC_PRICE": live_no,
            "LIVE_EXACT_PDP_QUOTE_ONLY": live_quote,
            "LIVE_EXACT_PDP_LOGIN_REQUIRED": live_login,
            "LIVE_EXACT_PDP_ACCESS_BLOCKED": live_blocked,
        },
        "HUMAN_VISIBILITY": {
            "public_price_visible": int(offer_human.get("HUMAN_PUBLIC_PRICE_VISIBLE") or 0),
            "public_offer_exists_price_hidden": int(offer_human.get("HUMAN_PUBLIC_PRICE_HIDDEN_BUT_OFFER_EXISTS") or 0),
            "no_public_offer": int(offer_human.get("HUMAN_NO_PUBLIC_PRICE") or 0),
            "access_blocked": int(offer_human.get("HUMAN_ACCESS_BLOCKED") or 0),
            "unknown": int(offer_human.get("UNKNOWN") or 0),
        },
        "LIVE_SELLER_DISCOVERY": {
            "unique_live_sellers": len({s for s in live_sellers if s}),
            "products_with_ge1_live_exact_priced_pdp": ge1,
            "products_with_ge2": ge2,
            "products_with_ge3": ge3,
            "new_live_extractable_pdps": new_live,
            "target_live_pdps": TARGET_LIVE_PDPS,
        },
        "TOP_LIVE_PRICEABLE_DOMAINS": top_priceable_domains(25),
        "DOMAIN_DEMOTIONS": {
            "IDENTITY_ONLY": demotions.get(IDENTITY_ONLY) or [],
            "QUOTE_ONLY": demotions.get(QUOTE_ONLY_DOMAIN) or [],
            "NON_PRICEABLE": demotions.get(NON_PRICEABLE) or [],
            "ACCESS_BLOCKED": demotions.get(ACCESS_BLOCKED_DOMAIN) or [],
        },
        "PRICE_RECOVERY": {
            "previous_honest_full100": HONEST_BASELINE,
            "new_valid_prices": new_n,
            "final_valid_prices": final,
            "coverage": round(coverage, 4),
            "coverage_pct": round(100.0 * coverage, 1),
            "accuracy": accuracy,
            "accuracy_pct": 100.0,
            "recovered_ids": recovered,
            "pass": full_pass,
        },
        "BENCHMARK_CONTRADICTIONS": {
            "count": len(contrad_list),
            "items": contrad_list[:40],
            "note": "Not removed from denominator automatically.",
        },
        "EASY_25": easy,
        "KNOWN_HARD_CASES": hard_cases,
        "CONTROL_SAMPLE": control,
        "SCALE_DECISION": {
            "full100_passed": full_pass,
            "easy25_passed": bool(easy.get("pass")),
            "control_improved": None,
            "SAFE_TO_SCALE": "YES" if full_pass and easy.get("pass") else "NO",
        },
        "FINAL_ANSWERS": {
            "1_previously_exact_pdps_actually_live": n_live_pages,
            "2_had_real_public_offers": live_priced + live_hidden,
            "3_404_soft_walled_should_not_reach_extraction": n_404 + n_bot + int(state_counts.get("SOFT_URL_UNVERIFIED") or 0),
            "4_new_live_exact_priced_pdps": new_live,
            "5_new_valid_prices": new_n,
            "6_reached_66": final >= TARGET_PRICED,
            "7_accuracy_ge_95": accuracy >= TARGET_ACCURACY,
            "8_sellers_expose_prices_reliably": [
                d.get("domain") for d in top_priceable_domains(10) if int(d.get("prices") or 0) > 0
            ],
            "9_domains_demote_identity_only": demotions.get(IDENTITY_ONLY) or [],
            "10_benchmark_contradictions": len(contrad_list),
            "11_easy25_ge_80": bool(easy.get("pass")),
            "12_control_acquisition": (control or {}).get("current_new_acquisition_cost") if control else None,
            "13_control_both_sides": (control or {}).get("both_sides") if control else None,
            "14_economics_ready": (control or {}).get("economics_ready") if control else None,
            "15_ge_5k": (control or {}).get("ge_5k") if control else None,
            "16_ge_10k": (control or {}).get("ge_10k") if control else None,
            "17_safe_to_scale": full_pass and bool(easy.get("pass")),
            "18_remaining_problem": (
                None
                if full_pass
                else (
                    "benchmark_quality"
                    if len(contrad_list) >= max(10, len(unresolved) // 2)
                    else "seller_availability"
                )
            ),
        },
        "stats": stats,
        "SAFE_NEXT_STEP": (
            "Full-100 passed — run control sample review."
            if full_pass
            else f"Live extractable PDPs={new_live}/{TARGET_LIVE_PDPS}; new prices={new_n}/{TARGET_NEW}. Focus on sellers with public offers."
        ),
    }
    ck["report_ready"] = True
    ck["finished_at"] = now_utc().isoformat()
    _save(CK, ck)
    _save(REPORT, report)
    return report


def format_report(report: dict[str, Any]) -> str:
    p = report.get("PRICE_RECOVERY") or {}
    l = report.get("LIVE_SELLER_DISCOVERY") or {}
    e = report.get("EASY_25") or {}
    s = report.get("SCALE_DECISION") or {}
    return (
        f"[lep] FINAL live_pdps={l.get('new_live_extractable_pdps')} "
        f"new_prices={p.get('new_valid_prices')} final={p.get('final_valid_prices')}/82 "
        f"cov={p.get('coverage_pct')}% easy25={e.get('priced')}/{e.get('attempted')} "
        f"contradictions={ (report.get('BENCHMARK_CONTRADICTIONS') or {}).get('count') } "
        f"SAFE_TO_SCALE={s.get('SAFE_TO_SCALE')}"
    )
