"""SAME-20 full pipeline recovery — frozen ITER-23 corpus, iterate until gates pass."""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from typing import Any

from application_clock import now_utc
from bidnet_engine.money_path import DOWNSTREAM_CHECKPOINT, _merge_checkpoint
from bidnet_engine.same20_corpus import (
    BASELINE_ITER23,
    BUILD,
    SAME20_STABLE_KEYS,
    SOURCE_JOB,
    SOURCE_RUN,
    freeze_corpus_from_report,
    load_corpus,
    resolve_same20,
    save_corpus,
)
from bidnet_engine.schedule_recovery_canary import _process_opp_with_guards
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits

STATUS = "m3_same20_full_pipeline_v1_status.json"
REPORT_JSON = "m3_same20_full_pipeline_v1_last_report.json"
REPORT_TXT = "m3_same20_full_pipeline_v1_last_report.txt"
ROWS_JSON = "m3_same20_full_pipeline_v1_rows.json"
ITER_LOG = "m3_same20_full_pipeline_v1_iteration_log.json"
PARTIAL = "m3_same20_full_pipeline_v1_partial_checkpoint.json"
STAGE_CACHE = "m3_same20_stage_cache.json"
CANCEL_FLAG = "m3_same20_cancel.flag"

# Hard operator ceiling: one SAME-20 job must finish (or stop) inside 1 hour.
RUN_WALL_BUDGET_S = 55 * 60
SAME20_OPP_HARD_TIMEOUT_S = 150

# Reuse schedule-recovery status filename aliases for UI that already polls ASR
# — also write dedicated same20 status.


def _load(name: str) -> dict[str, Any]:
    from m3_data_root import data_path

    path = data_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: Any) -> None:
    from m3_data_root import data_path

    path = data_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_status(**kwargs: Any) -> None:
    body = {"build": BUILD, "updated_at": now_utc().isoformat(), **kwargs}
    _save(STATUS, body)
    # Mirror into schedule-recovery status so existing monitors work
    _save("m3_schedule_recovery_v1_status.json", body)


def _cancel_requested() -> bool:
    from m3_data_root import data_path

    return data_path(CANCEL_FLAG).exists()


def _clear_cancel_flag() -> None:
    from m3_data_root import data_path

    try:
        data_path(CANCEL_FLAG).unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        pass


def _budget_remaining(started: float, wall_budget: int = RUN_WALL_BUDGET_S) -> float:
    return wall_budget - (time.time() - started)


def _append_iteration(entry: dict[str, Any]) -> list[dict[str, Any]]:
    log = _load(ITER_LOG)
    items = list(log.get("iterations") or [])
    items.append({"at": now_utc().isoformat(), **entry})
    _save(ITER_LOG, {"build": BUILD, "iterations": items})
    return items


def _stage_cache_get(sk: str) -> dict[str, Any] | None:
    cache = _load(STAGE_CACHE)
    row = (cache.get("by_stable_key") or {}).get(sk)
    return row if isinstance(row, dict) else None


def _stage_cache_put(sk: str, result: dict[str, Any], *, timings: dict[str, Any]) -> None:
    cache = _load(STAGE_CACHE)
    by = dict(cache.get("by_stable_key") or {})
    pm = result.get("package_materialization") if isinstance(result.get("package_materialization"), dict) else {}
    hashes = []
    for e in pm.get("PACKAGE_ATTACHMENT_INDEX") or []:
        if isinstance(e, dict) and e.get("CONTENT_HASH"):
            hashes.append(str(e["CONTENT_HASH"]))
    by[sk] = {
        "updated_at": now_utc().isoformat(),
        "content_hashes": hashes,
        "raw_lines": result.get("raw_lines"),
        "usable_ae": result.get("usable_ae"),
        "public_prices": result.get("public_prices"),
        "revenue_state": result.get("revenue_state"),
        "package_primary_blocker": result.get("package_primary_blocker"),
        "timings": timings,
        "result_slim": {
            "stable_key": result.get("stable_key"),
            "title": result.get("title"),
            "buyer": result.get("buyer"),
            "raw_lines": result.get("raw_lines"),
            "material_lines": result.get("material_lines"),
            "usable_ae": result.get("usable_ae"),
            "public_prices": result.get("public_prices"),
            "public_price_coverage_pct": result.get("public_price_coverage_pct"),
            "revenue_state": result.get("revenue_state"),
            "channel_class": result.get("channel_class"),
            "visible_headroom": result.get("visible_headroom"),
            "decision": result.get("decision"),
            "package_materialization": {
                k: pm.get(k)
                for k in (
                    "PACKAGE_DOCUMENT_COUNT_MATERIALIZED",
                    "PACKAGE_DOCUMENT_COUNT_DISCOVERED",
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND",
                    "product_classification",
                    "operator_product_status",
                    "EXPECTED_PRODUCT_LINES",
                    "EXTRACTED_PRODUCT_LINES",
                    "LINE_EXTRACTION_COVERAGE",
                    "PACKAGE_COMPLETENESS",
                    "primary_blocker",
                    "cache_hits",
                    "cache_misses",
                )
            },
        },
    }
    _save(
        STAGE_CACHE,
        {"build": BUILD, "updated_at": now_utc().isoformat(), "by_stable_key": by},
    )


def _enrich_result(r: dict[str, Any]) -> dict[str, Any]:
    """Derive coverage, channel, headroom, decision, plain blocker."""
    pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
    material = int(r.get("material_lines") or r.get("raw_lines") or 0)
    priced = int(r.get("public_prices") or 0)
    ae = int(r.get("usable_ae") or 0)
    cov = r.get("public_price_coverage_pct")
    if cov is None and material > 0:
        cov = round(100.0 * priced / material, 1)
        r["public_price_coverage_pct"] = cov
    try:
        cov_f = float(cov or 0)
    except (TypeError, ValueError):
        cov_f = 0.0

    # Channel — prefer existing; else UNKNOWN
    ch = r.get("channel_class") or r.get("channel_fit_class")
    if not ch:
        cf = r.get("channel_fit") if isinstance(r.get("channel_fit"), dict) else {}
        ch = cf.get("class") or cf.get("CHANNEL_CLASS") or "UNKNOWN"
    r["channel_class"] = ch
    r["channel_evidence_available"] = bool(r.get("channel_evidence_available") or ch not in {"UNKNOWN", None, ""})

    # Headroom when revenue + public basket exist
    rev_ok = r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE"
    rev_val = r.get("revenue_value") or r.get("government_value")
    pub_basket = r.get("public_basket_value")
    if pub_basket is None and priced > 0:
        # approximate from priced line hits if present
        hits = r.get("public_price_hits") or []
        if isinstance(hits, list):
            try:
                pub_basket = sum(float((h or {}).get("price") or 0) for h in hits if isinstance(h, dict))
            except (TypeError, ValueError):
                pub_basket = None
    headroom = None
    headroom_pct = None
    if rev_ok and rev_val is not None and pub_basket is not None:
        try:
            headroom = float(rev_val) - float(pub_basket)
            if float(rev_val) > 0:
                headroom_pct = round(100.0 * headroom / float(rev_val), 1)
        except (TypeError, ValueError):
            pass
    r["visible_headroom"] = headroom
    r["visible_headroom_percent"] = headroom_pct

    # Decision
    auth = bool(pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"))
    lines = int(r.get("raw_lines") or 0)
    if headroom is not None and headroom > 0 and cov_f >= 50 and ae >= 3:
        decision = "CALL_TODAY"
    elif rev_ok and priced > 0 and ae > 0:
        decision = "QUOTE_IF_CAPACITY"
    elif auth and lines > 0:
        decision = "WATCH"
    elif pm.get("product_classification") == "PRODUCT_SCHEDULE_INACCESSIBLE":
        decision = "INSUFFICIENT_EVIDENCE"
    else:
        decision = "PASS" if lines <= 0 and not auth else "WATCH"
    r["decision"] = decision

    # Plain-language blocker
    clf = str(pm.get("product_classification") or "")
    blocker = str(r.get("package_primary_blocker") or pm.get("primary_blocker") or "")
    if clf == "PRODUCT_SCHEDULE_INACCESSIBLE" or blocker in {
        "PRODUCT_SCHEDULE_INACCESSIBLE",
        "PACKAGE_MEMBERSHIP_LOCKED",
    }:
        plain = (
            "BidNet package documents are not downloadable with the current session "
            "(membership/login wall). No free public product schedule was recovered."
        )
    elif clf == "PARSER_DEFECT_REMAINS":
        plain = (
            f"A product table was detected but line extraction recovered only "
            f"{lines} usable rows — parser needs another pass."
        )
    elif not auth and lines <= 0:
        n_docs = int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        plain = (
            f"Package has {n_docs} local document(s), but no authoritative product schedule "
            f"was recognized yet — need pricing/bid form/item list extraction."
            if n_docs > 0
            else "No local package documents available yet."
        )
    elif lines <= 0 and auth:
        plain = "Authoritative product document found, but no itemized lines were extracted yet."
    elif lines > 0 and ae <= 0:
        plain = f"{lines} product lines extracted, but none reached A–E identity match quality."
    elif ae > 0 and priced <= 0:
        plain = (
            f"{ae} identity-ready lines, but no public/MSRP prices were recovered yet."
        )
    elif priced > 0 and cov_f < 50:
        plain = (
            f"{priced} lines priced publicly ({cov_f:.0f}% coverage) — still below 50% basket coverage."
        )
    elif lines > 0 and not rev_ok:
        plain = "Product lines exist, but usable government contract value is not yet recovered."
    else:
        plain = pm.get("operator_product_status") or "Pipeline stages advancing."
    r["blocker_plain"] = plain
    r["operator_pipeline_status"] = {
        "package": (
            "Complete"
            if int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0
            and str(pm.get("PACKAGE_COMPLETENESS") or "") in {"COMPLETE", "LIKELY_COMPLETE"}
            else (
                "Partial"
                if int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0
                else "Missing"
            )
        ),
        "product_document": "Found" if auth else "Missing",
        "lines": {
            "expected": pm.get("EXPECTED_PRODUCT_LINES"),
            "extracted": lines,
            "coverage": pm.get("LINE_EXTRACTION_COVERAGE"),
        },
        "identity": {"ae_count": ae, "unresolved": max(0, material - ae)},
        "government_value": "Ready" if rev_ok else "Missing",
        "public_pricing": {
            "priced_lines": priced,
            "coverage_pct": cov_f,
            "basket_value": pub_basket,
        },
        "channel": {"class": ch, "evidence": r.get("channel_evidence_available")},
        "visible_headroom": {"amount": headroom, "percent": headroom_pct},
        "decision": decision,
        "blocker_plain": plain,
    }
    return r


def _blocker_bucket(r: dict[str, Any]) -> str:
    pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
    clf = str(pm.get("product_classification") or "")
    lines = int(r.get("raw_lines") or 0)
    ae = int(r.get("usable_ae") or 0)
    priced = int(r.get("public_prices") or 0)
    cov = float(r.get("public_price_coverage_pct") or 0)
    if r.get("stalled") or r.get("exclusion") == "STALLED_OPPORTUNITY":
        return "TIMEOUT"
    if clf == "PRODUCT_SCHEDULE_INACCESSIBLE" or int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) <= 0:
        if clf != "NO_PRODUCT_LINES_ACTUALLY_PRESENT":
            return "PACKAGE_INCOMPLETE"
    if not pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND") and lines <= 0:
        # Local package present but schedule not recognized is still an auth-doc gap
        # (not "OTHER") — keeps operator buckets actionable.
        if clf == "NO_PRODUCT_LINES_ACTUALLY_PRESENT" and int(
            pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0
        ) <= 0:
            return "OTHER"
        return "NO_AUTHORITATIVE_PRODUCT_DOC"
    if lines <= 0:
        return "NO_LINES"
    expected = int(pm.get("EXPECTED_PRODUCT_LINES") or 0)
    if expected > 0 and lines < 0.5 * expected:
        return "PARTIAL_LINES"
    if ae <= 0:
        return "IDENTITY_WEAK"
    if r.get("revenue_state") != "ECONOMIC_REVENUE_USABLE":
        return "NO_REVENUE"
    if priced <= 0:
        return "NO_PUBLIC_PRICE"
    if cov < 50:
        return "PUBLIC_COVERAGE_LOW"
    if str(r.get("channel_class") or "UNKNOWN") == "UNKNOWN":
        return "CHANNEL_UNKNOWN"
    if r.get("visible_headroom") is None:
        return "NO_HEADROOM"
    return "OTHER"


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid_local = auth = lines_ready = material = 0
    ae_opps = ae_lines = rev = pub_opps = pub_lines = 0
    cov50 = cov75 = channel = headroom = call_today = 0
    expected = extracted = 0
    buckets: Counter[str] = Counter()
    timings_total: list[float] = []
    stage_totals: Counter[str] = Counter()

    for r in results:
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        if int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0:
            valid_local += 1
        if pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"):
            auth += 1
        raw = int(r.get("raw_lines") or 0)
        if raw > 0:
            lines_ready += 1
        material += int(r.get("material_lines") or 0)
        expected += int(pm.get("EXPECTED_PRODUCT_LINES") or 0)
        extracted += int(pm.get("EXTRACTED_PRODUCT_LINES") or raw or 0)
        ae = int(r.get("usable_ae") or 0)
        if ae > 0:
            ae_opps += 1
            ae_lines += ae
        if r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE":
            rev += 1
        priced = int(r.get("public_prices") or 0)
        if priced > 0:
            pub_opps += 1
            pub_lines += priced
        try:
            cov = float(r.get("public_price_coverage_pct") or 0)
        except (TypeError, ValueError):
            cov = 0.0
        if cov >= 50:
            cov50 += 1
        if cov >= 75:
            cov75 += 1
        if str(r.get("channel_class") or "UNKNOWN") not in {"UNKNOWN", ""}:
            channel += 1
        if r.get("visible_headroom") is not None:
            headroom += 1
        if r.get("decision") == "CALL_TODAY":
            call_today += 1
        buckets[_blocker_bucket(r)] += 1
        t = r.get("timings") if isinstance(r.get("timings"), dict) else {}
        if t.get("TIME_TOTAL_PER_OPP") is not None:
            timings_total.append(float(t["TIME_TOTAL_PER_OPP"]))
        for k, v in t.items():
            if k.startswith("TIME_") and k != "TIME_TOTAL_PER_OPP" and v is not None:
                try:
                    stage_totals[k] += float(v)
                except (TypeError, ValueError):
                    pass

    gates = {
        "VALID_LOCAL_PACKAGES_GE_18": valid_local >= 18,
        "AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_15": auth >= 15,
        "LINES_READY_GE_12": lines_ready >= 12,
        "A_E_IDENTITY_OPPS_GE_8": ae_opps >= 8,
        "PUBLIC_PRICE_READY_OPPS_GE_5": pub_opps >= 5,
    }
    gates["SAME20_FULL_PIPELINE_RECOVERY_PASS"] = all(gates.values())

    perf: dict[str, Any] = {}
    if timings_total:
        srt = sorted(timings_total)
        perf = {
            "median_s": round(statistics.median(srt), 1),
            "p90_s": round(srt[max(0, int(0.9 * len(srt)) - 1)], 1),
            "slowest_s": round(max(srt), 1),
            "slowest_stage": max(stage_totals, key=stage_totals.get) if stage_totals else None,
        }

    return {
        "after": {
            "VALID_PRODUCT_DOMINANT": len(results),
            "VALID_LOCAL_PACKAGES": valid_local,
            "AUTHORITATIVE_PRODUCT_DOCS": auth,
            "LINES_READY": lines_ready,
            "EXPECTED_PRODUCT_LINES": expected,
            "EXTRACTED_PRODUCT_LINES": extracted,
            "MATERIAL_PRODUCT_LINES": material,
            "A_E_IDENTITY_OPPS": ae_opps,
            "A_E_IDENTITY_LINES": ae_lines,
            "REVENUE_READY": rev,
            "PUBLIC_PRICE_READY": pub_opps,
            "PUBLIC_PRICED_LINES": pub_lines,
            "PUBLIC_COVERAGE_50": cov50,
            "PUBLIC_COVERAGE_75": cov75,
            "CHANNEL_CLASSIFIED": channel,
            "VISIBLE_HEADROOM": headroom,
            "CALL_TODAY": call_today,
        },
        "blockers": dict(buckets),
        "gates": gates,
        "performance": perf,
    }


def ensure_corpus_frozen(report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze / refresh corpus. Always merges SAME20_SEED_META package URLs."""
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    store_by_sk = {}
    for k, v in store.items():
        if isinstance(v, dict) and v.get("stable_key"):
            store_by_sk[str(v["stable_key"])] = {
                **v,
                "canonical_opportunity_id": str(v.get("canonical_opportunity_id") or k),
            }
    if report is None:
        report = _load("m3_iter23_new20_report_snapshot.json") or _load(
            "m3_schedule_recovery_v1_last_report.json"
        )
    # Synthetic minimal report when snapshot missing — seed titles/URLs still applied
    if not (report.get("top_recovered") or report.get("unrecovered_cases")):
        from bidnet_engine.same20_corpus import SAME20_SEED_META

        report = {
            "top_recovered": [
                {
                    "opportunity": sk,
                    "title": (SAME20_SEED_META.get(sk) or {}).get("title"),
                    "buyer": (SAME20_SEED_META.get(sk) or {}).get("buyer"),
                    "attachment_urls": (SAME20_SEED_META.get(sk) or {}).get("attachment_urls") or [],
                }
                for sk in SAME20_STABLE_KEYS
            ]
        }
    payload = freeze_corpus_from_report(report, store_by_sk)
    save_corpus(payload)
    return payload


def run_same20_full_pipeline(
    *,
    iteration: int = 1,
    change_made: str = "same-20 full pipeline recovery",
    price_budget: int = 40,
    warm_cache: bool = False,
    on_progress: Any | None = None,
    stable_keys: list[str] | None = None,
    max_opps: int | None = None,
    run_budget_s: int | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_engine.package_materialization import PATCH as PKG_PATCH, purge_html_document_caches
    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    _clear_cancel_flag()
    wall_budget = int(run_budget_s or RUN_WALL_BUDGET_S)
    run_id = f"S20-{now_utc().strftime('%Y%m%d%H%M%S')}"
    # Always re-freeze from ITER-23 report when present so attachment URLs persist
    prior_report = _load("m3_schedule_recovery_v1_last_report.json")
    # Prefer dedicated iter23 snapshot if saved
    iter23 = _load("m3_iter23_new20_report_snapshot.json")
    corpus = ensure_corpus_frozen(iter23 if iter23.get("top_recovered") else None)
    # If corpus lacks attachment URLs, rebuild from prior report when it still has them
    opps = corpus.get("opportunities") or []
    if opps and not any((o.get("attachment_urls") or o.get("attachments_metadata")) for o in opps if isinstance(o, dict)):
        if prior_report.get("unrecovered_cases") or prior_report.get("top_recovered"):
            # Only rebuild if prior report looks like ITER-23 (has PRODUCT_SCHEDULE_INACCESSIBLE buckets)
            corpus = ensure_corpus_frozen(prior_report)
    store = load_store()
    candidates = resolve_same20(store, corpus)
    if stable_keys:
        want = {str(k) for k in stable_keys}
        candidates = [c for c in candidates if str(c.get("stable_key") or "") in want]
    if max_opps is not None and int(max_opps) > 0:
        candidates = candidates[: int(max_opps)]
    target_n = max(1, len(candidates) or 1)
    store_by_cid: dict[str, dict[str, Any]] = {}
    # Base index from full store
    for k, v in store.items():
        if isinstance(v, dict):
            store_by_cid[str(v.get("canonical_opportunity_id") or k)] = dict(v)
    # Overlay corpus-enriched rows LAST so seeded attachment URLs are not wiped
    for c in candidates:
        cid = str(c.get("canonical_opportunity_id") or "")
        enriched = c.get("_store_row") if isinstance(c.get("_store_row"), dict) else {}
        if not cid:
            continue
        base = dict(store_by_cid.get(cid) or {})
        # Prefer enriched attachments / authoritative URL
        if enriched.get("attachments_metadata"):
            base["attachments_metadata"] = enriched["attachments_metadata"]
        if enriched.get("authoritative_url"):
            base["authoritative_url"] = enriched["authoritative_url"]
        if enriched.get("row_ref"):
            base["row_ref"] = enriched["row_ref"]
        for field in ("title", "buyer", "deadline", "stable_key"):
            if enriched.get(field) and not base.get(field):
                base[field] = enriched[field]
        store_by_cid[cid] = base
        # Also key by stable_key for lookups
        sk = str(c.get("stable_key") or "")
        if sk:
            store_by_cid[sk] = base

    write_status(
        phase="SAME20_SELECTED",
        completed=0,
        remaining=target_n,
        run_id=run_id,
        iteration=iteration,
        corpus=SOURCE_RUN,
        warm_cache=warm_cache,
        selected=len(candidates),
        wall_budget_s=wall_budget,
        canary_keys=[str(c.get("stable_key") or "") for c in candidates],
        heartbeat_at=now_utc().isoformat(),
    )

    # Never mass-purge on SAME-20 — HTML poison is cleared per-file during materialize.
    # A cold purge wiped valid PDFs from ITER-23 and regressed VALID_LOCAL to 0.
    purge_stats = {"skipped": True, "reason": "same20_preserve_document_cache"}

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    cache_hits = cache_misses = redownloads = reparses = 0

    # Resume partial
    partial = _load(PARTIAL)
    done_sk = set()
    if partial.get("rows") and int(partial.get("iteration") or 0) == iteration:
        for r in partial.get("rows") or []:
            if isinstance(r, dict) and r.get("stable_key"):
                results.append(r)
                done_sk.add(str(r["stable_key"]))
        for r in partial.get("quarantined") or []:
            if isinstance(r, dict):
                quarantined.append(r)
                done_sk.add(str(r.get("stable_key") or ""))

    def _process_one(item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cache_hits, cache_misses, redownloads, reparses
        sk = str(item.get("stable_key") or "")
        cid = str(item.get("canonical_opportunity_id") or sk)
        store_row = (
            item.get("_store_row")
            or store_by_cid.get(cid)
            or store_by_cid.get(sk)
            or {}
        )
        t0 = time.perf_counter()
        timings: dict[str, float] = {}

        if warm_cache:
            cached = _stage_cache_get(sk)
            if cached and cached.get("result_slim") and int(cached.get("raw_lines") or 0) >= 0:
                # Warm path: still re-invoke money path but materialize will reuse local files
                cache_hits += 1
            else:
                cache_misses += 1

        write_status(
            phase="SAME20_OPP",
            completed=len(results),
            remaining=max(0, target_n - len(results)),
            percent=int(100 * len(results) / max(target_n, 1)),
            run_id=run_id,
            iteration=iteration,
            current_stable_key=sk,
            current_title=str(item.get("title") or "")[:120],
            current_stage="PROCESS_MONEY",
            warm_cache=warm_cache,
            heartbeat_at=now_utc().isoformat(),
        )

        try:
            r = _process_opp_with_guards(
                item={k: v for k, v in item.items() if k != "_store_row"},
                store_row=store_row,
                client=client,
                store_by_cid=store_by_cid,
                price_budget=price_budget,
                run_id=run_id,
                iteration=iteration,
                mode="same_20",
                valid_n=len(results),
                excl_n=len(quarantined),
                pool_remaining=max(0, target_n - len(results) - 1),
                target_valid=target_n,
                on_progress=on_progress,
                opp_hard_timeout_s=SAME20_OPP_HARD_TIMEOUT_S,
            )
        except Exception as exc:
            r = {
                "stable_key": sk,
                "canonical_opportunity_id": cid,
                "title": item.get("title"),
                "buyer": item.get("buyer"),
                "raw_lines": 0,
                "material_lines": 0,
                "usable_ae": 0,
                "public_prices": 0,
                "error": f"{type(exc).__name__}:{exc}"[:200],
                "package_materialization": {
                    "primary_blocker": "OPP_ERROR",
                    "product_classification": "PARSER_DEFECT_REMAINS",
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                },
            }

        timings["TIME_TOTAL_PER_OPP"] = round(time.perf_counter() - t0, 2)
        # Prefer engine-reported stage timings when present
        eng_t = r.get("timings") if isinstance(r.get("timings"), dict) else {}
        for k in (
            "TIME_PACKAGE",
            "TIME_DOCUMENT_RECOGNITION",
            "TIME_LINE_EXTRACTION",
            "TIME_IDENTITY",
            "TIME_REVENUE",
            "TIME_PUBLIC_PRICING",
            "TIME_CHANNEL",
        ):
            if eng_t.get(k) is not None:
                timings[k] = float(eng_t[k])
        r["timings"] = timings
        r["recovery_cohort"] = "same_20"
        r["schedule_recovery_iteration"] = iteration
        r["corpus_run"] = SOURCE_RUN
        r = _enrich_result(r)

        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        cache_hits += int(pm.get("cache_hits") or 0)
        cache_misses += int(pm.get("cache_misses") or 0)
        redownloads += int(pm.get("redownloads") or 0)
        reparses += int(pm.get("reparses") or 0)

        _stage_cache_put(sk, r, timings=timings)
        return r

    # Pass 1 — all not-yet-done
    pending = [c for c in candidates if str(c.get("stable_key") or "") not in done_sk]
    budget_stopped = False
    for opp_i, item in enumerate(pending):
        if _cancel_requested() or _budget_remaining(started, wall_budget) < SAME20_OPP_HARD_TIMEOUT_S:
            budget_stopped = True
            write_status(
                phase="SAME20_BUDGET_STOP",
                completed=len(results),
                remaining=max(0, target_n - len(results)),
                run_id=run_id,
                iteration=iteration,
                reason="cancel" if _cancel_requested() else "run_wall_budget",
                budget_s=wall_budget,
                elapsed_s=round(time.time() - started, 1),
                heartbeat_at=now_utc().isoformat(),
            )
            for skipped in pending[opp_i:]:
                quarantined.append(
                    {
                        "stable_key": skipped.get("stable_key"),
                        "canonical_opportunity_id": skipped.get("canonical_opportunity_id"),
                        "title": skipped.get("title"),
                        "buyer": skipped.get("buyer"),
                        "raw_lines": 0,
                        "stalled": True,
                        "exclusion": "STALLED_OPPORTUNITY",
                        "stalled_reason": "RUN_WALL_BUDGET_OR_CANCEL",
                        "package_materialization": {
                            "primary_blocker": "RUN_WALL_BUDGET",
                            "product_classification": "BUDGET_SKIPPED",
                            "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                        },
                    }
                )
            break
        # Mid-run session refresh: BidNet sessions die ~30–45m into SAME-20
        if opp_i > 0 and opp_i % 4 == 0:
            try:
                refresh = client.ensure_authenticated()
                write_status(
                    phase="SAME20_REAUTH",
                    completed=len(results),
                    remaining=max(0, target_n - len(results)),
                    run_id=run_id,
                    iteration=iteration,
                    reauth_ok=bool(refresh.authenticated),
                    reauth_status=str(refresh.status),
                    heartbeat_at=now_utc().isoformat(),
                )
            except Exception as exc:
                write_status(
                    phase="SAME20_REAUTH",
                    completed=len(results),
                    remaining=max(0, target_n - len(results)),
                    run_id=run_id,
                    iteration=iteration,
                    reauth_ok=False,
                    reauth_error=f"{type(exc).__name__}:{exc}"[:120],
                    heartbeat_at=now_utc().isoformat(),
                )
        r = _process_one(item)
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        # Immediate re-auth after membership/HTML wall on a package
        if (
            not pm.get("VALID_LOCAL_DOCUMENT_COUNT")
            and str(pm.get("primary_blocker") or "")
            in {"MEMBERSHIP_WALL", "PACKAGE_INCOMPLETE", "NO_AUTHORITATIVE_PRODUCT_DOC", "DOWNLOAD_FAILED"}
        ):
            try:
                client.ensure_authenticated()
            except Exception:
                pass
        if r.get("stalled") or r.get("exclusion") == "STALLED_OPPORTUNITY":
            quarantined.append(r)
        else:
            results.append(r)
        write_status(
            phase="SAME20_HEARTBEAT",
            completed=len(results),
            remaining=max(0, target_n - len(results)),
            percent=int(100 * len(results) / max(target_n, 1)),
            run_id=run_id,
            iteration=iteration,
            last_title=str(r.get("title") or "")[:120],
            quarantined=len(quarantined),
            warm_cache=warm_cache,
            elapsed_s=round(time.time() - started, 1),
            budget_s=wall_budget,
            heartbeat_at=now_utc().isoformat(),
        )
        _save(
            PARTIAL,
            {
                "build": BUILD,
                "run_id": run_id,
                "iteration": iteration,
                "rows": results,
                "quarantined": quarantined,
                "updated_at": now_utc().isoformat(),
            },
        )
        ckpt = _load(DOWNSTREAM_CHECKPOINT)
        rows = [x for x in (ckpt.get("rows") or []) if isinstance(x, dict)]
        merged = _merge_checkpoint(rows, results)
        _save(
            DOWNSTREAM_CHECKPOINT,
            {
                "build": BUILD,
                "engine": BUILD,
                "updated_at": now_utc().isoformat(),
                "classified": len(merged),
                "rows": merged,
            },
        )

    # Pass 2 — revisit quarantined (still in denominator); skip if budget exhausted
    still_q: list[dict[str, Any]] = []
    for q in list(quarantined):
        if budget_stopped or _cancel_requested() or _budget_remaining(started, wall_budget) < SAME20_OPP_HARD_TIMEOUT_S:
            still_q.append(q)
            continue
        sk = str(q.get("stable_key") or "")
        item = next((c for c in candidates if str(c.get("stable_key")) == sk), None)
        if not item:
            still_q.append(q)
            continue
        write_status(
            phase="SAME20_REVISIT_QUARANTINE",
            completed=len(results),
            current_stable_key=sk,
            current_title=str(item.get("title") or "")[:120],
            run_id=run_id,
            iteration=iteration,
            heartbeat_at=now_utc().isoformat(),
        )
        r = _process_one(item)
        if r.get("stalled") or r.get("exclusion") == "STALLED_OPPORTUNITY":
            still_q.append(r)
        else:
            results.append(r)
    quarantined = still_q

    # Ensure denominator stays 20 — append lasting quarantines into results for reporting
    present = {str(r.get("stable_key") or "") for r in results}
    for q in quarantined:
        sk = str(q.get("stable_key") or "")
        if sk and sk not in present:
            results.append(q)
            present.add(sk)

    # Order by frozen corpus
    by_sk = {str(r.get("stable_key") or ""): r for r in results}
    ordered = [by_sk[sk] for sk in SAME20_STABLE_KEYS if sk in by_sk]
    for sk, r in by_sk.items():
        if sk not in SAME20_STABLE_KEYS:
            ordered.append(r)
    results = ordered[:20] if len(ordered) >= 20 else ordered

    client.close()
    agg = _aggregate(results)
    runtime = round(time.time() - started, 1)
    after = agg["after"]
    gates = agg["gates"]
    status = "PASS" if gates.get("SAME20_FULL_PIPELINE_RECOVERY_PASS") else "FAIL"

    report = {
        "build": BUILD,
        "run_id": run_id,
        "mode": "same_20",
        "STATUS": status,
        "iteration": iteration,
        "change_made": change_made,
        "warm_cache": warm_cache,
        "runtime_s": runtime,
        "corpus": {
            "name": "SAME20_REGRESSION_CORPUS",
            "source_job": SOURCE_JOB,
            "source_run": SOURCE_RUN,
            "opportunities": 20,
            "keys_preserved": [str(r.get("stable_key")) for r in results],
        },
        "baseline": BASELINE_ITER23,
        "after": after,
        "blockers": agg["blockers"],
        "gates": gates,
        "performance": {
            **(agg.get("performance") or {}),
            "TOTAL_RUNTIME_S": runtime,
            "CACHE_HITS": cache_hits,
            "CACHE_MISSES": cache_misses,
            "DOCUMENT_REDOWNLOADS": redownloads,
            "DOCUMENT_REPARSES": reparses,
        },
        "quarantined": [
            {"stable_key": q.get("stable_key"), "title": q.get("title"), "reason": q.get("stalled_reason")}
            for q in quarantined
        ],
        "top_recovered": [
            {
                "opportunity": r.get("stable_key"),
                "title": r.get("title"),
                "buyer": r.get("buyer"),
                "discovery": ((r.get("package_materialization") or {}).get("DISCOVERY") or {}),
                "documents_acquired": (r.get("package_materialization") or {}).get(
                    "PACKAGE_DOCUMENT_COUNT_MATERIALIZED"
                ),
                "product_schedule": (
                    "Found"
                    if (r.get("package_materialization") or {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND")
                    else "Missing"
                ),
                "extracted_lines": r.get("raw_lines"),
                "usable_ae": r.get("usable_ae"),
                "public_prices": r.get("public_prices"),
                "coverage": r.get("public_price_coverage_pct"),
                "decision": r.get("decision"),
                "blocker_plain": r.get("blocker_plain"),
                "timings": r.get("timings"),
            }
            for r in sorted(
                results,
                key=lambda x: (
                    1 if (x.get("package_materialization") or {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND") else 0,
                    int(x.get("raw_lines") or 0),
                    int(x.get("usable_ae") or 0),
                ),
                reverse=True,
            )[:20]
        ],
        "UI_SYNCHRONIZED": True,
        "patch": PKG_PATCH,
        "updated_at": now_utc().isoformat(),
    }
    iters = _append_iteration(
        {
            "iteration": iteration,
            "PACKAGES": after.get("VALID_LOCAL_PACKAGES"),
            "AUTH_DOCS": after.get("AUTHORITATIVE_PRODUCT_DOCS"),
            "LINES_READY": after.get("LINES_READY"),
            "A_E_OPPS": after.get("A_E_IDENTITY_OPPS"),
            "PUBLIC_PRICE_READY": after.get("PUBLIC_PRICE_READY"),
            "REVENUE_READY": after.get("REVENUE_READY"),
            "COVERAGE_50": after.get("PUBLIC_COVERAGE_50"),
            "VISIBLE_HEADROOM": after.get("VISIBLE_HEADROOM"),
            "TOTAL_RUNTIME": runtime,
            "CHANGE_MADE": change_made,
            "RESULT": status,
            "warm_cache": warm_cache,
            "run_id": run_id,
        }
    )
    report["iteration_log"] = iters
    report["ITERATIONS_COMPLETED"] = len(iters)

    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_same20_report(report))
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "rows": results, "updated_at": now_utc().isoformat()})
    _save("m3_schedule_recovery_v1_last_report.json", report)
    _save("m3_schedule_recovery_v1_rows.json", {"build": BUILD, "run_id": run_id, "mode": "same_20", "rows": results})
    write_status(
        phase="DONE",
        percent=100,
        completed=len(results),
        STATUS=status,
        run_id=run_id,
        iteration=iteration,
        warm_cache=warm_cache,
        VALID_LOCAL=after.get("VALID_LOCAL_PACKAGES"),
        AUTH=after.get("AUTHORITATIVE_PRODUCT_DOCS"),
        LINES=after.get("LINES_READY"),
        A_E=after.get("A_E_IDENTITY_OPPS"),
        PUBLIC=after.get("PUBLIC_PRICE_READY"),
        PASS=gates.get("SAME20_FULL_PIPELINE_RECOVERY_PASS"),
    )
    return report


def format_same20_report(report: dict[str, Any]) -> str:
    a = report.get("after") or {}
    b = report.get("baseline") or {}
    g = report.get("gates") or {}
    p = report.get("performance") or {}
    lines = [
        "M3 SAME-20 FULL PIPELINE RECOVERY SUMMARY",
        f"BUILD: {report.get('build')}",
        f"JOB: {report.get('run_id')}",
        f"STATUS: {report.get('STATUS')}",
        "",
        "BASELINE",
        f"VALID LOCAL PACKAGES: {b.get('VALID_LOCAL_PACKAGES')}",
        f"AUTHORITATIVE PRODUCT DOCS: {b.get('AUTHORITATIVE_PRODUCT_DOCS')}",
        f"LINES READY: {b.get('LINES_READY')}",
        "",
        "FINAL",
        f"VALID LOCAL PACKAGES: {a.get('VALID_LOCAL_PACKAGES')}",
        f"AUTHORITATIVE PRODUCT DOCS: {a.get('AUTHORITATIVE_PRODUCT_DOCS')}",
        f"LINES READY: {a.get('LINES_READY')}",
        f"A-E IDENTITY OPPS: {a.get('A_E_IDENTITY_OPPS')}",
        f"PUBLIC PRICE READY: {a.get('PUBLIC_PRICE_READY')}",
        f"REVENUE READY: {a.get('REVENUE_READY')}",
        "",
        f"SAME20_FULL_PIPELINE_RECOVERY_PASS: {'YES' if g.get('SAME20_FULL_PIPELINE_RECOVERY_PASS') else 'NO'}",
        f"RUNTIME_S: {report.get('runtime_s')}",
        f"MEDIAN_PER_OPP: {p.get('median_s')}",
        f"P90_PER_OPP: {p.get('p90_s')}",
    ]
    return "\n".join(lines)
