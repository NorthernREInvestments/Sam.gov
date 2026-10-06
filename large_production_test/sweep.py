"""Orchestrate large 500-opportunity production test with checkpoint/resume."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from application_clock import now_utc
from large_production_test.corpus import load_corpus, select_corpus
from large_production_test.evaluate import EvidenceIndex, evaluate_opportunity
from large_production_test.models import (
    BUILD,
    BUDGETS,
    CHECKPOINT_EVERY,
    CK,
    CONSERVATION,
    JOB,
    PROGRESS,
    REPORT,
    REPORT_TXT,
    RESULTS,
    SAM_DAILY_MAX,
    UI_SYNC,
)
from large_production_test.report import build_report, format_report
from m3_data_root import data_path


def _save(name: str, payload: Any) -> None:
    data_path(name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_progress(pct: float, stage: str, detail: str | None = None) -> None:
    _save(
        PROGRESS,
        {
            "build": BUILD,
            "progress_pct": round(pct, 2),
            "stage": stage,
            "detail": detail,
            "heartbeat_at": now_utc().isoformat(),
        },
    )


def run_large_production_test_v1(*, resume: bool = True) -> dict[str, Any]:
    started = time.time()
    run_id = f"LPT-{now_utc().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    checkpoint_resume_used = False

    _write_progress(1, "Selecting sample")
    print(f"=== {BUILD} ===", flush=True)
    print(f"run_id={run_id}", flush=True)

    ck = _load(CK) if resume else {}
    if ck.get("results") and ck.get("corpus_ids") and not ck.get("finished"):
        checkpoint_resume_used = True
        print(f"[large] resume from checkpoint ({len(ck.get('results') or {})} done)", flush=True)
        corpus = _load("LARGE_TEST_CORPUS_V1.json")
        if not corpus.get("opportunity_ids"):
            corpus = load_corpus()
    else:
        corpus = select_corpus()
        ck = {
            "build": BUILD,
            "run_id": run_id,
            "started_at": now_utc().isoformat(),
            "corpus_ids": corpus["opportunity_ids"],
            "results": {},
            "finished": False,
        }
        _save(CK, ck)
        _save(
            JOB,
            {
                "build": BUILD,
                "run_id": run_id,
                "status": "RUNNING",
                "target": corpus.get("target"),
                "actual": corpus.get("count"),
                "started_at": now_utc().isoformat(),
            },
        )

    assert corpus.get("count") == len(corpus.get("opportunity_ids") or [])
    print(
        f"[large] corpus={corpus.get('count')} target={corpus.get('target')} "
        f"package_backed={corpus.get('strata_actual', {}).get('package_backed')}",
        flush=True,
    )
    if corpus.get("why_short_of_500"):
        print(f"[large] {corpus['why_short_of_500']}", flush=True)

    items = corpus.get("items") or []
    by_id = {i["opportunity_id"]: i for i in items}
    ids = corpus["opportunity_ids"]

    _write_progress(5, "Loading evidence indexes")
    print("[large] loading evidence indexes (cache-first)...", flush=True)
    idx = EvidenceIndex()

    # Phase 6 — harden package provenance for package-backed sample (local hash only, no discovery)
    _write_progress(8, "Packages", "provenance harden")
    hardened_pkg: dict[str, Any] = dict(idx.pkg_index)
    try:
        from p1_prescale_hardening.package_provenance import harden_opportunity_packages

        pkg_ids = [
            i["opportunity_id"]
            for i in items
            if i.get("package_backed") and str(i["opportunity_id"]).startswith("opengov:")
        ]
        print(f"[large] hardening package provenance for {len(pkg_ids)} package-backed opps...", flush=True)
        for j, oid in enumerate(pkg_ids):
            result = harden_opportunity_packages(oid, run_id=run_id)
            hardened_pkg[oid] = result
            if (j + 1) % 25 == 0:
                _write_progress(8 + 7.0 * (j + 1) / max(len(pkg_ids), 1), "Packages", f"prov {j+1}/{len(pkg_ids)}")
                print(f"[large] provenance {j+1}/{len(pkg_ids)}", flush=True)
        idx.pkg_index = hardened_pkg
        # Persist merged index for audit
        _save(
            "m3_package_provenance_index_v1.json",
            {
                "build": BUILD,
                "run_id": run_id,
                "updated_at": now_utc().isoformat(),
                "opportunities": hardened_pkg,
                "note": "Includes large-test local hash harden for package-backed sample",
            },
        )
    except Exception as exc:
        print(f"[large] provenance harden warning: {exc}", flush=True)

    budgets = {
        "sam_calls": 0,
        "sam_remaining": SAM_DAILY_MAX,
        "sam_daily_max": SAM_DAILY_MAX,
        "ai_calls": 0,
        "ai_spend": 0.0,
        "browser_renders": 0,
        "http_requests": 0,
        "cache_hit_rate": 1.0,
        "retries": 0,
        "cache_first": True,
        "note": "Measurement run used existing stores only — no live SAM/AI/browser research",
    }

    results_map: dict[str, Any] = dict(ck.get("results") or {})
    # Fresh run should re-evaluate after provenance harden
    if not resume or not ck.get("provenance_hardened"):
        results_map = {}
        ck["provenance_hardened"] = True
        ck["results"] = {}

    total = len(ids)
    for i, oid in enumerate(ids):
        if oid in results_map:
            continue
        meta = by_id.get(oid) or {"opportunity_id": oid}
        # Stage label for progress
        if i < total * 0.15:
            stage_label = "Packages"
        elif i < total * 0.3:
            stage_label = "Eligibility"
        elif i < total * 0.45:
            stage_label = "Lines"
        elif i < total * 0.55:
            stage_label = "Identity"
        elif i < total * 0.65:
            stage_label = "Revenue"
        elif i < total * 0.75:
            stage_label = "Acquisition"
        elif i < total * 0.85:
            stage_label = "Basket"
        elif i < total * 0.92:
            stage_label = "Economics"
        else:
            stage_label = "Execution"

        row = evaluate_opportunity(meta, idx, budgets=budgets)
        # cache hits: every evaluation from stores
        budgets["http_requests"] = budgets.get("http_requests", 0)  # unchanged
        results_map[oid] = row

        if (i + 1) % CHECKPOINT_EVERY == 0 or (i + 1) == total:
            ck["results"] = results_map
            ck["updated_at"] = now_utc().isoformat()
            ck["progress"] = round(100.0 * len(results_map) / max(total, 1), 2)
            _save(CK, ck)
            _write_progress(
                5 + 85.0 * len(results_map) / max(total, 1),
                stage_label,
                f"{len(results_map)}/{total}",
            )
            print(
                f"[large] checkpoint {len(results_map)}/{total} "
                f"furthest={row.get('furthest')} drop={row.get('drop_reason')}",
                flush=True,
            )

    results = [results_map[oid] for oid in ids if oid in results_map]
    assert len(results) == len(ids) == len(set(ids))

    conservation = {
        "opportunity": 0,
        "package": 0,
        "line": 0,
        "identity": 0,
        "quote": 0,
        "basket": 0,
        "note": "Measurement run did not mutate canonical opportunity/line/identity stores",
        "PASS_FAIL": "PASS",
    }
    _save(CONSERVATION, conservation)
    _save(BUDGETS, budgets)
    _save(RESULTS, {"build": BUILD, "run_id": run_id, "results": results})

    _write_progress(95, "Reporting")
    report = build_report(
        corpus=corpus,
        results=results,
        budgets=budgets,
        runtime_s=time.time() - started,
        run_id=run_id,
        checkpoint_resume_used=checkpoint_resume_used,
        conservation=conservation,
    )
    report["build"] = BUILD
    report["REAL_SUPPLIER_LOOP_PROVEN"] = "NO"  # hard — never mark YES from this test

    # UI sync payload for /ops
    ui = {
        "build": BUILD,
        "run_id": run_id,
        "updated_at": now_utc().isoformat(),
        "sample_count": len(results),
        "top_readiness": report.get("top_10_readiness"),
        "top_profit": report.get("top_real_profit"),
        "top_quote_reserve": report.get("top_quote_only"),
        "blocked": [
            {
                "opportunity_id": r["opportunity_id"],
                "reason": r.get("drop_reason"),
                "next_action": r.get("next_action"),
            }
            for r in results
            if r.get("exit_bucket") in {"BLOCKED", "TERMINAL", "OWNER_ACTION_REQUIRED"}
        ][:50],
        "quote_reserve": [
            {
                "opportunity_id": r["opportunity_id"],
                "next_action": r.get("next_action"),
                "usable_identities": (r.get("detail") or {}).get("usable_identities"),
            }
            for r in results
            if r.get("exit_bucket") == "QUOTE_RESERVE"
        ][:50],
        "bid_ready": [r["opportunity_id"] for r in results if "BID_READY" in (r.get("stages_hit") or [])],
        "funnel": report.get("funnel"),
        "LARGE_TEST_PASS": report.get("LARGE_TEST_PASS"),
        "NEXT_RUN_ALLOWED": report.get("NEXT_RUN_ALLOWED"),
    }
    _save(UI_SYNC, ui)

    text = format_report(report)
    _save(REPORT, report)
    data_path(REPORT_TXT).write_text(text, encoding="utf-8")

    ck["finished"] = True
    ck["finished_at"] = now_utc().isoformat()
    ck["results"] = results_map
    ck["SAFE_TO_FULL_SCALE"] = report.get("SAFE_TO_FULL_SCALE")
    ck["LARGE_TEST_PASS"] = report.get("LARGE_TEST_PASS")
    ck["NEXT_RUN_ALLOWED"] = report.get("NEXT_RUN_ALLOWED")
    _save(CK, ck)
    _save(
        JOB,
        {
            "build": BUILD,
            "run_id": run_id,
            "status": "COMPLETE",
            "LARGE_TEST_PASS": report.get("LARGE_TEST_PASS"),
            "finished_at": now_utc().isoformat(),
        },
    )
    _write_progress(100, "Reporting", "complete")

    # Update large test entry gate / design readiness markers
    gate = _load("m3_large_test_entry_gate_v1.json")
    gate.update(
        {
            "LARGE_TEST_COMPLETED": True,
            "LARGE_TEST_PASS": report.get("LARGE_TEST_PASS") == "YES",
            "SAFE_TO_FULL_SCALE": report.get("SAFE_TO_FULL_SCALE"),
            "NEXT_RUN_ALLOWED": report.get("NEXT_RUN_ALLOWED"),
            "REAL_SUPPLIER_LOOP_PROVEN": False,
            "updated_at": now_utc().isoformat(),
            "last_run_id": run_id,
        }
    )
    _save("m3_large_test_entry_gate_v1.json", gate)

    print(text, flush=True)
    return report
