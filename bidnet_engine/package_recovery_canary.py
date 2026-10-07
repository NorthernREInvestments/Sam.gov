"""Same-13 BidNet package materialization recovery (SBC-d5a8f17d0cea cohort)."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from application_clock import now_utc
from bidnet_engine.money_path import DOWNSTREAM_CHECKPOINT, _merge_checkpoint, process_money_opportunity
from bidnet_engine.package_materialization import BUILD as PKG_BUILD
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits

BUILD = "20261007-m3-bidnet-package-materialization-v1"
STATUS = "m3_package_materialization_v1_status.json"
REPORT_JSON = "m3_package_materialization_v1_last_report.json"
REPORT_TXT = "m3_package_materialization_v1_last_report.txt"
ROWS_JSON = "m3_package_materialization_v1_rows.json"

# Exact cohort from SBC-d5a8f17d0cea / live scores (13)
SAME_13_STABLE_KEYS = [
    "id:0000436465",
    "id:0000437119",
    "id:0000437309",
    "id:0000437915",
    "id:0000438141",
    "id:0000438184",
    "id:0000438543",
    "id:0000438575",
    "id:0000438612",
    "id:0000438787",
    "id:0000439029",
    "id:444129821660",
    "id:444173049548",
]

BEFORE = {
    "PACKAGE_READY": 13,
    "AUTHORITATIVE_PRODUCT_DOC": 2,
    "LINES_READY": 1,
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


def _resolve_same_13(ckpt_rows: list[dict[str, Any]], store: dict[str, Any]) -> list[dict[str, Any]]:
    """Load selected IDs from prior SBC file if present; else SAME_13_STABLE_KEYS."""
    selected = _load("m3_schedule_backed_canary_v1_selected_ids.json")
    keys = [str(x) for x in (selected.get("selected_ids") or []) if x]
    if len(keys) < 5:
        keys = list(SAME_13_STABLE_KEYS)
    by_stable = {str(r.get("stable_key") or ""): r for r in ckpt_rows if isinstance(r, dict)}
    by_cid = {str(r.get("canonical_opportunity_id") or ""): r for r in ckpt_rows if isinstance(r, dict)}
    out: list[dict[str, Any]] = []
    for key in keys:
        row = by_stable.get(key)
        if not row:
            # match by trailing numeric id in store
            for cid, srow in store.items():
                if not isinstance(srow, dict):
                    continue
                if key in {str(srow.get("stable_key") or ""), str(srow.get("external_id") or "")} or key.endswith(
                    str(srow.get("external_id") or "")
                ):
                    # build synthetic checkpoint-like item
                    row = {
                        "stable_key": key,
                        "canonical_opportunity_id": cid,
                        "title": srow.get("title"),
                        "buyer": srow.get("buyer"),
                        "deadline": srow.get("deadline"),
                        "classification": srow.get("classification") or "PRODUCT",
                    }
                    break
        if not row:
            # try cid map where stable in store
            for cid, srow in store.items():
                if isinstance(srow, dict) and str(srow.get("stable_key") or "") == key:
                    row = {
                        "stable_key": key,
                        "canonical_opportunity_id": cid,
                        "title": srow.get("title"),
                        "buyer": srow.get("buyer"),
                        "deadline": srow.get("deadline"),
                        "classification": "PRODUCT",
                    }
                    break
        if row:
            item = dict(row)
            item["stable_key"] = item.get("stable_key") or key
            out.append(item)
    # Dedup preserve order
    seen: set[str] = set()
    deduped = []
    for r in out:
        k = str(r.get("stable_key") or r.get("canonical_opportunity_id"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped[:13]


def run_package_recovery(
    *,
    mode: str = "same_13",
    canary_n: int = 20,
    price_budget: int = 25,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"PMR-{now_utc().strftime('%Y%m%d%H%M%S')}"
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
        candidates, _excl = select_schedule_backed_candidates(product_rows, store_by_cid, limit=canary_n)
        # Exclude same-13 keys for new-20
        same = set(SAME_13_STABLE_KEYS)
        candidates = [c for c in candidates if str(c.get("stable_key") or "") not in same][:canary_n]

    write_status(phase="SELECTED", completed=0, remaining=len(candidates), run_id=run_id, mode=mode, selected=len(candidates))

    from bidnet_auth.client import BidNetAuthenticatedClient

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    for i, item in enumerate(candidates):
        cid = str(item.get("canonical_opportunity_id") or "")
        store_row = store_by_cid.get(cid) or {}
        # Also try resolve by stable_key in store
        if not store_row:
            for k, v in store_by_cid.items():
                if str(v.get("stable_key") or "") == str(item.get("stable_key") or ""):
                    store_row = v
                    item = {**item, "canonical_opportunity_id": k}
                    cid = k
                    break
        r = process_money_opportunity(item, store_row, client=client, store=store_by_cid, price_budget=price_budget)
        r["live_bidnet"] = True
        r["recovery_cohort"] = mode
        results.append(r)
        if (i + 1) % 2 == 0 or i == len(candidates) - 1:
            write_status(
                phase=f"PACKAGE_RECOVERY_{mode.upper()}",
                completed=len(results),
                remaining=max(0, len(candidates) - len(results)),
                percent=int(100 * len(results) / max(len(candidates), 1)),
                run_id=run_id,
            )
            if on_progress:
                try:
                    on_progress(phase=f"PACKAGE_RECOVERY_{mode}", pct=int(100 * len(results) / max(len(candidates), 1)), completed=len(results))
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
    report = _build_report(results, mode=mode, run_id=run_id, started=started, before=BEFORE if mode == "same_13" else None)

    # Expansion rule: only if same-13 gates pass, run new-20 in same job
    gates = report.get("gates") or {}
    if mode == "same_13" and gates.get("AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8") and gates.get("LINES_READY_GE_8"):
        write_status(phase="EXPAND_NEW_20", run_id=run_id)
        new20 = run_package_recovery(mode="new_20", canary_n=20, price_budget=price_budget, on_progress=on_progress)
        report["NEW_20"] = {
            "RUN": "YES",
            **{k: new20.get(k) for k in ("canary_input", "after", "gates", "downstream", "top_recovered", "STATUS")},
        }
        report["STATUS"] = "PASS" if (new20.get("gates") or {}).get("NEW_20_PASS") else "SAME13_PASS_NEW20_FAIL"
    else:
        report["NEW_20"] = {"RUN": "NO", "reason": "same_13_gates_not_met"}

    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_report(report))
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "mode": mode, "rows": results, "updated_at": now_utc().isoformat()})
    write_status(
        phase="DONE",
        percent=100,
        completed=len(results),
        STATUS=report.get("STATUS"),
        run_id=run_id,
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
) -> dict[str, Any]:
    states: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    detail = index_acq = 0
    discovered = downloaded = valid = invalid = 0
    auth_found = lines_ready = material_lines = 0
    ae_opps = pub_opps = rev_opps = 0
    top = []

    for r in results:
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        completeness = str(pm.get("PACKAGE_COMPLETENESS") or "UNKNOWN")
        states[completeness] += 1
        # Also count truthful package states
        pst = str(pm.get("package_state_truthful") or r.get("package_state") or "UNKNOWN")
        if "REGISTRATION" in pst:
            states["REGISTRATION_REQUIRED"] += 1
        if "MEMBERSHIP" in pst or "LOCKED" in pst:
            states["MEMBERSHIP_LOCKED"] += 1
        if "EXTERNAL" in pst:
            states["EXTERNAL_PORTAL_REQUIRED"] += 1
        if "DOWNLOAD_FAILED" in pst:
            states["DOWNLOAD_FAILED"] += 1
        if completeness == "COMPLETE" or completeness == "LIKELY_COMPLETE":
            pass
        discovered += int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0)
        downloaded += int(pm.get("PACKAGE_DOCUMENT_COUNT_DOWNLOADED") or 0)
        valid += int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        invalid += len(pm.get("invalid_downloads") or [])
        if int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0) > 0:
            index_acq += 1
        detail += 1  # processed via live pipeline
        if pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"):
            auth_found += 1
        if int(r.get("raw_lines") or 0) > 0:
            lines_ready += 1
        material_lines += int(r.get("material_lines") or 0)
        if int(r.get("usable_ae") or 0) > 0:
            ae_opps += 1
        if int(r.get("public_prices") or 0) > 0:
            pub_opps += 1
        if r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE":
            rev_opps += 1
        b = str(r.get("package_primary_blocker") or pm.get("primary_blocker") or "OTHER")
        # Normalize PRODUCT_SCHEDULE_NOT_ACQUIRED into report buckets
        if b == "PRODUCT_SCHEDULE_NOT_ACQUIRED":
            blockers["SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE"] += 1
        else:
            blockers[b] += 1
        auth = pm.get("AUTHORITATIVE_PRODUCT_DOC") or {}
        top.append(
            {
                "opportunity": r.get("stable_key") or r.get("canonical_opportunity_id"),
                "buyer": r.get("buyer"),
                "title": r.get("title"),
                "package_host": (auth.get("source_system") if isinstance(auth, dict) else None) or "BidNet/official",
                "documents_discovered": pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED"),
                "documents_acquired": pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED"),
                "product_schedule": "Found" if pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND") else "Missing",
                "schedule_filename": auth.get("filename") if isinstance(auth, dict) else None,
                "format": auth.get("extension") if isinstance(auth, dict) else None,
                "extracted_lines": r.get("raw_lines"),
                "material_lines": r.get("material_lines"),
                "identity_handoff": int(r.get("usable_ae") or 0) > 0,
                "public_pricing_handoff": int(r.get("public_prices") or 0) > 0,
                "blocker": b,
                "operator_ui_message": r.get("package_operator_message") or pm.get("operator_message"),
            }
        )

    top.sort(
        key=lambda x: (
            1 if x.get("product_schedule") == "Found" else 0,
            int(x.get("extracted_lines") or 0),
            int(x.get("documents_acquired") or 0),
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
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": auth_found,
        "LINES_READY": lines_ready,
        "MATERIAL_PRODUCT_LINES": material_lines,
    }
    if mode == "same_13":
        gates = {
            "AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8": auth_found >= 8,
            "LINES_READY_GE_8": lines_ready >= 8,
            "PACKAGE_RECOVERY_WORKING": auth_found >= 8 and lines_ready >= 8,
        }
        status = "PASS" if gates["PACKAGE_RECOVERY_WORKING"] else "FAIL"
    else:
        gates = {
            "VALID_LOCAL_PACKAGE_GE_18": sum(1 for r in results if int((r.get("package_materialization") or {}).get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0) >= 18,
            "AUTHORITATIVE_PRODUCT_DOC_GE_15": auth_found >= 15,
            "LINES_READY_GE_12": lines_ready >= 12,
            "NEW_20_PASS": auth_found >= 15 and lines_ready >= 12,
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
        "before": before,
        "after": after,
        "package_states": dict(states),
        "blockers": dict(blockers),
        "gates": gates,
        "downstream": {
            "A_E_IDENTITY_OPPS": ae_opps,
            "PUBLIC_PRICE_READY_OPPS": pub_opps,
            "REVENUE_READY_OPPS": rev_opps,
        },
        "top_recovered": top[:8],
        "REAL_DATA_ONLY": "YES",
        "EXPAND_TO_100": "NO",
        "updated_at": now_utc().isoformat(),
    }


def format_report(report: dict[str, Any]) -> str:
    a = report.get("after") or {}
    g = report.get("gates") or {}
    return "\n".join(
        [
            "M3 BIDNET PACKAGE MATERIALIZATION SUMMARY",
            f"BUILD: {report.get('build')}",
            f"RUN: {report.get('run_id')}",
            f"STATUS: {report.get('STATUS')}",
            f"MODE: {report.get('mode')}",
            f"AUTHORITATIVE DOCS: {a.get('AUTHORITATIVE_PRODUCT_DOC_FOUND')}",
            f"LINES READY: {a.get('LINES_READY')}",
            f"PACKAGE_RECOVERY_WORKING: {g.get('PACKAGE_RECOVERY_WORKING') or g.get('NEW_20_PASS')}",
            f"EXPAND_TO_100: NO",
            "",
        ]
    )
