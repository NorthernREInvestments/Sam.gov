"""Same-13 authoritative schedule recovery with iteration log (content-first)."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from application_clock import now_utc
from bidnet_engine.money_path import DOWNSTREAM_CHECKPOINT, _merge_checkpoint, process_money_opportunity
from bidnet_engine.package_materialization import BUILD as PKG_BUILD
from bidnet_engine.package_recovery_canary import SAME_13_STABLE_KEYS, _resolve_same_13
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits

BUILD = "20261007-m3-authoritative-schedule-recovery-v1"
STATUS = "m3_schedule_recovery_v1_status.json"
REPORT_JSON = "m3_schedule_recovery_v1_last_report.json"
REPORT_TXT = "m3_schedule_recovery_v1_last_report.txt"
ROWS_JSON = "m3_schedule_recovery_v1_rows.json"
ITER_LOG = "m3_schedule_recovery_v1_iteration_log.json"

BEFORE = {
    "AUTHORITATIVE_PRODUCT_DOC": 2,
    "LINES_READY": 2,
}


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
    _save(STATUS, {"build": BUILD, "updated_at": now_utc().isoformat(), **kwargs})


def _append_iteration(entry: dict[str, Any]) -> list[dict[str, Any]]:
    log = _load(ITER_LOG)
    items = list(log.get("iterations") or [])
    items.append({"at": now_utc().isoformat(), **entry})
    _save(ITER_LOG, {"build": BUILD, "iterations": items})
    return items


def run_schedule_recovery(
    *,
    mode: str = "same_13",
    canary_n: int = 20,
    price_budget: int = 25,
    iteration: int = 1,
    change_made: str = "content-first schedule recognition",
    on_progress: Any | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"ASR-{now_utc().strftime('%Y%m%d%H%M%S')}"
    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}

    if mode == "same_13":
        candidates = _resolve_same_13(rows, store)
    else:
        from bidnet_engine.schedule_selection import select_schedule_backed_candidates
        from bidnet_downstream.models import PRODUCT_CLASSES

        product_rows = [r for r in rows if r.get("classification") in PRODUCT_CLASSES]
        candidates, _excl = select_schedule_backed_candidates(product_rows, store_by_cid, limit=canary_n * 2)
        # Prefer content-evidence when available; exclude same-13
        same = set(SAME_13_STABLE_KEYS)
        candidates = [c for c in candidates if str(c.get("stable_key") or "") not in same][:canary_n]

    write_status(
        phase="SELECTED",
        completed=0,
        remaining=len(candidates),
        run_id=run_id,
        mode=mode,
        selected=len(candidates),
        iteration=iteration,
    )

    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_engine.package_materialization import PATCH as PKG_PATCH, purge_html_document_caches

    # Clear poisoned HTML document_1 caches before re-download / harvest
    purge_stats = purge_html_document_caches(limit=800)
    write_status(
        phase="PURGED_HTML_CACHE",
        completed=0,
        remaining=len(candidates),
        run_id=run_id,
        mode=mode,
        selected=len(candidates),
        iteration=iteration,
        purge=purge_stats,
        patch=PKG_PATCH,
    )

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    # Synchronous processing — text-only content inspection is bounded.
    # Do NOT recreate the BidNet client mid-run (breaks authenticated downloads → HTML).

    for i, item in enumerate(candidates):
        cid = str(item.get("canonical_opportunity_id") or "")
        store_row = store_by_cid.get(cid) or {}
        if not store_row:
            for k, v in store_by_cid.items():
                if str(v.get("stable_key") or "") == str(item.get("stable_key") or ""):
                    store_row = v
                    item = {**item, "canonical_opportunity_id": k}
                    cid = k
                    break
        write_status(
            phase=f"SCHEDULE_RECOVERY_{mode.upper()}_OPP",
            completed=len(results),
            remaining=max(0, len(candidates) - len(results)),
            percent=int(100 * len(results) / max(len(candidates), 1)),
            run_id=run_id,
            iteration=iteration,
            current_stable_key=item.get("stable_key"),
            current_title=str(item.get("title") or "")[:120],
        )
        try:
            r = process_money_opportunity(
                item, store_row, client=client, store=store_by_cid, price_budget=price_budget
            )
        except Exception as exc:
            r = {
                "canonical_opportunity_id": cid,
                "stable_key": item.get("stable_key"),
                "title": item.get("title"),
                "buyer": item.get("buyer"),
                "raw_lines": 0,
                "material_lines": 0,
                "usable_ae": 0,
                "public_prices": 0,
                "package_materialization": {
                    "primary_blocker": "OPP_ERROR",
                    "operator_product_status": f"Package analysis failed: {type(exc).__name__}",
                    "product_classification": "PARSER_DEFECT_REMAINS",
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                },
                "package_primary_blocker": "OPP_ERROR",
                "error": f"{type(exc).__name__}:{exc}"[:200],
            }
        r["live_bidnet"] = True
        r["recovery_cohort"] = mode
        r["schedule_recovery_iteration"] = iteration
        results.append(r)
        if (i + 1) % 2 == 0 or i == len(candidates) - 1:
            write_status(
                phase=f"SCHEDULE_RECOVERY_{mode.upper()}",
                completed=len(results),
                remaining=max(0, len(candidates) - len(results)),
                percent=int(100 * len(results) / max(len(candidates), 1)),
                run_id=run_id,
                iteration=iteration,
            )
            if on_progress:
                try:
                    on_progress(
                        phase=f"SCHEDULE_RECOVERY_{mode}",
                        pct=int(100 * len(results) / max(len(candidates), 1)),
                        completed=len(results),
                    )
                except Exception:
                    pass
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

    client.close()
    report = _build_report(
        results,
        mode=mode,
        run_id=run_id,
        started=started,
        before=BEFORE if mode == "same_13" else None,
        iteration=iteration,
        change_made=change_made,
    )
    iters = _append_iteration(
        {
            "iteration": iteration,
            "mode": mode,
            "AUTHORITATIVE_PRODUCT_DOC_FOUND": (report.get("after") or {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND"),
            "LINES_READY": (report.get("after") or {}).get("LINES_READY"),
            "A_E": (report.get("downstream") or {}).get("A_E_IDENTITY_OPPS"),
            "PUBLIC_PRICE": (report.get("downstream") or {}).get("PUBLIC_PRICE_READY_OPPS"),
            "CHANGE_MADE": change_made,
            "RESULT": report.get("STATUS"),
            "run_id": run_id,
        }
    )
    report["iteration_log"] = iters
    report["ITERATIONS_COMPLETED"] = len(iters)

    gates = report.get("gates") or {}
    same13_pass = bool(
        gates.get("AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8") and gates.get("LINES_READY_GE_8")
    ) or bool(gates.get("ADJUSTED_ITEMIZABLE_PASS"))

    if mode == "same_13" and same13_pass:
        write_status(phase="EXPAND_NEW_20", run_id=run_id, iteration=iteration)
        new20 = run_schedule_recovery(
            mode="new_20",
            canary_n=20,
            price_budget=price_budget,
            iteration=iteration,
            change_made="new-20 after same-13 pass",
            on_progress=on_progress,
        )
        report["NEW_20"] = {
            "RUN": "YES",
            **{k: new20.get(k) for k in ("canary_input", "after", "gates", "downstream", "top_recovered", "STATUS")},
        }
        report["STATUS"] = "PASS" if (new20.get("gates") or {}).get("NEW_20_PASS") else "SAME13_PASS_NEW20_FAIL"
    elif mode == "same_13":
        report["NEW_20"] = {"RUN": "NO", "reason": "same_13_gates_not_met"}

    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_report(report))
    # Also mirror into package materialization filenames for UI fallbacks
    _save("m3_package_materialization_v1_rows.json", {"build": BUILD, "run_id": run_id, "mode": mode, "rows": results})
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "mode": mode, "rows": results, "updated_at": now_utc().isoformat()})
    write_status(
        phase="DONE",
        percent=100,
        completed=len(results),
        STATUS=report.get("STATUS"),
        run_id=run_id,
        iteration=iteration,
        AUTHORITATIVE=report.get("after", {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND"),
        LINES=report.get("after", {}).get("LINES_READY"),
    )
    return report


def _build_report(
    results: list[dict[str, Any]],
    *,
    mode: str,
    run_id: str,
    started: float,
    before: dict[str, Any] | None,
    iteration: int,
    change_made: str,
) -> dict[str, Any]:
    states: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    detail = index_acq = 0
    discovered = downloaded = valid = invalid = 0
    auth_found = lines_ready = material_lines = 0
    expected_lines = extracted_lines = 0
    product_like_docs = 0
    catalog_only = no_product = 0
    itemizable = 0
    ae_opps = pub_opps = rev_opps = 0
    ae_lines = pub_lines = 0
    cov50 = cov75 = 0
    unrecovered: list[dict[str, Any]] = []
    top = []

    for r in results:
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        cr = pm.get("content_recognition") if isinstance(pm.get("content_recognition"), dict) else {}
        completeness = str(pm.get("PACKAGE_COMPLETENESS") or "UNKNOWN")
        states[completeness] += 1
        discovered += int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0)
        downloaded += int(pm.get("PACKAGE_DOCUMENT_COUNT_DOWNLOADED") or 0)
        valid += int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        invalid += len(pm.get("invalid_downloads") or [])
        if int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0) > 0:
            index_acq += 1
        detail += 1
        auth = bool(pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"))
        raw_lines = int(r.get("raw_lines") or 0)
        if auth:
            auth_found += 1
        if raw_lines > 0:
            lines_ready += 1
        material_lines += int(r.get("material_lines") or 0)
        expected_lines += int(pm.get("EXPECTED_PRODUCT_LINES") or cr.get("EXPECTED_PRODUCT_LINES") or 0)
        extracted_lines += int(pm.get("EXTRACTED_PRODUCT_LINES") or raw_lines or 0)
        product_like_docs += int(cr.get("product_like_documents") or 0)
        clf = str(pm.get("product_classification") or cr.get("classification") or "")
        valid_local = int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        # Without valid local docs we cannot claim "no product lines present"
        if valid_local <= 0 and clf in {"NO_PRODUCT_LINES_ACTUALLY_PRESENT", "UNKNOWN", ""}:
            clf = "PRODUCT_SCHEDULE_INACCESSIBLE"
        if clf == "CATALOG_DISCOUNT_ONLY":
            catalog_only += 1
        elif clf in {"NO_PRODUCT_LINES_ACTUALLY_PRESENT"}:
            no_product += 1
        elif clf == "PRODUCT_SCHEDULE_INACCESSIBLE" or valid_local <= 0:
            pass  # neither itemizable nor proven absent
        elif raw_lines > 0 or auth or clf == "LINES_RECOVERED":
            itemizable += 1
        elif clf not in {"SERVICE_SCOPE"}:
            # Product-like opp with package but no lines yet — still counts as itemizable candidate
            itemizable += 1
        if int(r.get("usable_ae") or 0) > 0:
            ae_opps += 1
            ae_lines += int(r.get("usable_ae") or 0)
        if int(r.get("public_prices") or 0) > 0:
            pub_opps += 1
            pub_lines += int(r.get("public_prices") or 0)
        if r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE":
            rev_opps += 1
        cov = r.get("public_price_coverage_pct")
        try:
            cov_f = float(cov) if cov is not None else 0.0
        except (TypeError, ValueError):
            cov_f = 0.0
        if cov_f >= 50:
            cov50 += 1
        if cov_f >= 75:
            cov75 += 1
        b = str(r.get("package_primary_blocker") or pm.get("primary_blocker") or "OTHER")
        if clf == "PARSER_DEFECT_REMAINS":
            b = "PARSER_FAILURE"
        elif clf == "CATALOG_DISCOUNT_ONLY":
            b = "CATALOG_DISCOUNT_ONLY"
        elif clf == "PRODUCT_SCHEDULE_INACCESSIBLE":
            b = "PRODUCT_SCHEDULE_INACCESSIBLE"
        blockers[b] += 1
        auth_doc = pm.get("AUTHORITATIVE_PRODUCT_DOC") or {}
        top.append(
            {
                "opportunity": r.get("stable_key") or r.get("canonical_opportunity_id"),
                "title": r.get("title"),
                "buyer": r.get("buyer"),
                "documents_acquired": pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED"),
                "product_schedule": "Found" if auth else "Missing",
                "schedule_filename": auth_doc.get("filename") if isinstance(auth_doc, dict) else None,
                "role": auth_doc.get("document_role_content") or auth_doc.get("document_role_guess"),
                "pages": auth_doc.get("product_signal_pages") if isinstance(auth_doc, dict) else None,
                "expected": pm.get("EXPECTED_PRODUCT_LINES"),
                "extracted_lines": raw_lines,
                "coverage": pm.get("LINE_EXTRACTION_COVERAGE"),
                "classification": clf,
                "operator_status": pm.get("operator_product_status") or pm.get("operator_message"),
                "identity_handoff": int(r.get("usable_ae") or 0) > 0,
                "public_pricing_handoff": int(r.get("public_prices") or 0) > 0,
            }
        )
        if raw_lines <= 0:
            unrecovered.append(
                {
                    "opportunity": r.get("stable_key") or r.get("title"),
                    "title": r.get("title"),
                    "documents_inspected": [
                        {
                            "filename": (i or {}).get("filename"),
                            "role": (i or {}).get("document_role_content"),
                            "pages": (i or {}).get("product_signal_pages"),
                            "signals": (i or {}).get("signal_hits"),
                            "rows": (i or {}).get("extracted_line_count"),
                            "classification": (i or {}).get("classification"),
                        }
                        for i in (cr.get("inspections") or [])
                    ],
                    "product_signals": clf,
                    "why_no_lines": pm.get("operator_product_status") or b,
                    "classification": clf or b,
                    "blocker_type": (
                        "external"
                        if clf in {"CATALOG_DISCOUNT_ONLY", "NO_PRODUCT_LINES_ACTUALLY_PRESENT", "PRODUCT_SCHEDULE_INACCESSIBLE"}
                        else "software"
                    ),
                }
            )

    top.sort(
        key=lambda x: (
            1 if x.get("product_schedule") == "Found" else 0,
            int(x.get("extracted_lines") or 0),
        ),
        reverse=True,
    )

    n = len(results)
    after = {
        "DETAIL_ACQUIRED": detail,
        "ATTACHMENT_INDEX_ACQUIRED": index_acq,
        "DOCUMENTS_DISCOVERED": discovered,
        "DOCUMENTS_DOWNLOADED": downloaded,
        "VALID_LOCAL_DOCUMENTS": valid,
        "INVALID_DOWNLOADS": invalid,
        "PRODUCT_LIKE_DOCUMENTS_FOUND": product_like_docs,
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": auth_found,
        "ACTUALLY_ITEMIZABLE_PRODUCT_OPPS": itemizable,
        "CATALOG_DISCOUNT_ONLY": catalog_only,
        "NO_PRODUCT_LINES_PRESENT": no_product,
        "LINES_READY": lines_ready,
        "EXPECTED_LINES": expected_lines,
        "EXTRACTED_LINES": extracted_lines,
        "EXTRACTION_COVERAGE": round(extracted_lines / expected_lines, 4) if expected_lines else None,
        "MATERIAL_PRODUCT_LINES": material_lines,
    }

    # Adjusted pass ONLY with document-level proof of non-itemizable majority.
    # PRODUCT_SCHEDULE_NOT_ACQUIRED / PARSER_FAILURE are software blockers — never count as proof.
    import math

    soft_blockers = int(blockers.get("PRODUCT_SCHEDULE_NOT_ACQUIRED") or 0) + int(
        blockers.get("PARSER_FAILURE") or 0
    ) + int(blockers.get("OPP_TIMEOUT") or 0) + int(blockers.get("INVALID_DOWNLOADED_FILE") or 0)
    opps_with_valid = sum(
        1
        for r in results
        if int((r.get("package_materialization") or {}).get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0
    )
    adjusted_pass = False
    if (
        soft_blockers == 0
        and 1 <= itemizable < 8
        and (catalog_only + no_product) >= (n - itemizable)
        and n >= 8
        and opps_with_valid >= n  # every opp had at least one valid local doc inspected
    ):
        needed = max(1, int(math.ceil(0.8 * itemizable)))
        adjusted_pass = lines_ready >= needed and auth_found >= needed

    if mode == "same_13":
        gates = {
            "AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8": auth_found >= 8,
            "LINES_READY_GE_8": lines_ready >= 8,
            "ADJUSTED_ITEMIZABLE_PASS": adjusted_pass,
            # Prefer hard 8/8; adjusted only when soft blockers are gone and absence is proven
            "PACKAGE_RECOVERY_WORKING": (auth_found >= 8 and lines_ready >= 8) or adjusted_pass,
            "A_E_IDENTITY_OPPS_GE_5": ae_opps >= 5,
            "PUBLIC_PRICE_READY_OPPS_GE_3": pub_opps >= 3,
            "PUBLIC_PRICE_COVERAGE_50_GE_1": cov50 >= 1,
        }
        status = "PASS" if gates["PACKAGE_RECOVERY_WORKING"] else "FAIL"
    else:
        valid_pkgs = sum(
            1
            for r in results
            if int((r.get("package_materialization") or {}).get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0
        )
        gates = {
            "VALID_LOCAL_PACKAGE_GE_18": valid_pkgs >= 18,
            "ITEMIZABLE_PRODUCT_GE_15": itemizable >= 15,
            "LINES_READY_GE_12": lines_ready >= 12,
            "A_E_GE_8": ae_opps >= 8,
            "PUBLIC_PRICE_READY_GE_5": pub_opps >= 5,
            "NEW_20_PASS": valid_pkgs >= 18 and lines_ready >= 12 and ae_opps >= 8 and pub_opps >= 5,
        }
        status = "PASS" if gates["NEW_20_PASS"] else "FAIL"

    return {
        "build": BUILD,
        "package_build": PKG_BUILD,
        "run_id": run_id,
        "mode": mode,
        "STATUS": status,
        "runtime_s": round(time.time() - started, 1),
        "canary_input": n,
        "iteration": iteration,
        "change_made": change_made,
        "before": before,
        "after": after,
        "package_states": dict(states),
        "blockers": dict(blockers),
        "gates": gates,
        "downstream": {
            "A_E_IDENTITY_OPPS": ae_opps,
            "A_E_LINES": ae_lines,
            "PUBLIC_PRICE_READY_OPPS": pub_opps,
            "PUBLIC_PRICED_LINES": pub_lines,
            "PUBLIC_PRICE_COVERAGE_50": cov50,
            "PUBLIC_PRICE_COVERAGE_75": cov75,
            "REVENUE_READY_OPPS": rev_opps,
            "DOWNSTREAM_HANDOFF": ae_opps >= 5 and pub_opps >= 3,
        },
        "top_recovered": top[:13],
        "unrecovered_cases": unrecovered,
        "REAL_DATA_ONLY": "YES",
        "EXPAND_TO_100": "NO",
        "updated_at": now_utc().isoformat(),
    }


def format_report(report: dict[str, Any]) -> str:
    a = report.get("after") or {}
    g = report.get("gates") or {}
    d = report.get("downstream") or {}
    lines = [
        "M3 AUTHORITATIVE SCHEDULE RECOVERY SUMMARY",
        f"BUILD: {report.get('build')}",
        f"JOB: {report.get('run_id')}",
        f"STATUS: {report.get('STATUS')}",
        f"ITERATIONS COMPLETED: {report.get('ITERATIONS_COMPLETED') or report.get('iteration')}",
        "",
        "SAME-13",
        f"VALID LOCAL DOCUMENTS: {a.get('VALID_LOCAL_DOCUMENTS')}",
        f"PRODUCT-LIKE DOCUMENTS FOUND: {a.get('PRODUCT_LIKE_DOCUMENTS_FOUND')}",
        f"AUTHORITATIVE PRODUCT DOCS: {a.get('AUTHORITATIVE_PRODUCT_DOC_FOUND')}",
        f"ACTUALLY ITEMIZABLE PRODUCT OPPS: {a.get('ACTUALLY_ITEMIZABLE_PRODUCT_OPPS')}",
        f"CATALOG-DISCOUNT-ONLY: {a.get('CATALOG_DISCOUNT_ONLY')}",
        f"NO PRODUCT LINES PRESENT: {a.get('NO_PRODUCT_LINES_PRESENT')}",
        f"LINES READY: {a.get('LINES_READY')}",
        f"EXPECTED LINES: {a.get('EXPECTED_LINES')}",
        f"EXTRACTED LINES: {a.get('EXTRACTED_LINES')}",
        f"EXTRACTION COVERAGE: {a.get('EXTRACTION_COVERAGE')}",
        "",
        f"A-E IDENTITY OPPS: {d.get('A_E_IDENTITY_OPPS')}",
        f"PUBLIC PRICE READY OPPS: {d.get('PUBLIC_PRICE_READY_OPPS')}",
        f">=50% COVERAGE: {d.get('PUBLIC_PRICE_COVERAGE_50')}",
        "",
        f"AUTHORITATIVE PRODUCT DOC >=8: {'YES' if g.get('AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8') else 'NO'}",
        f"LINES READY >=8: {'YES' if g.get('LINES_READY_GE_8') else 'NO'}",
        f"ADJUSTED ITEMIZABLE DENOMINATOR PASS: {'YES' if g.get('ADJUSTED_ITEMIZABLE_PASS') else 'NO'}",
        f"DOWNSTREAM HANDOFF: {'YES' if d.get('DOWNSTREAM_HANDOFF') else 'NO'}",
        "",
    ]
    return "\n".join(lines)
