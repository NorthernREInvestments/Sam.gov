"""Schedule-backed product canary — selection + extraction recovery (no auto-expand to 100)."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from application_clock import now_utc
from bidnet_downstream.models import PRODUCT_CLASSES
from bidnet_engine.money_path import (
    DOWNSTREAM_CHECKPOINT,
    _merge_checkpoint,
    canonical_pipeline_map,
    inspect_durable_checkpoint,
    process_money_opportunity,
)
from bidnet_engine.schedule_selection import select_schedule_backed_candidates
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits
from channel_fit.engine import queue_buckets, score_money_sprint_rows

BUILD = "20261007-m3-schedule-backed-product-canary-v1"
STATUS = "m3_schedule_backed_canary_v1_status.json"
REPORT_JSON = "m3_schedule_backed_canary_v1_last_report.json"
REPORT_TXT = "m3_schedule_backed_canary_v1_last_report.txt"
ROWS_JSON = "m3_schedule_backed_canary_v1_rows.json"
SCORES_JSON = "m3_channel_fit_live_scores_v1.json"  # Today reads live scores
SELECTED_IDS = "m3_schedule_backed_canary_v1_selected_ids.json"


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


def _grade_letter(line: dict[str, Any]) -> str:
    for key in ("identity_grade", "confidence_grade", "identity_class", "grade"):
        g = str(line.get(key) or "").upper()
        if g[:1] in "ABCDEFG":
            return g[:1]
    return "G"


def _summarize(results: list[dict[str, Any]], excluded: dict[str, int]) -> dict[str, Any]:
    n = len(results)
    package = sum(1 for r in results if "ACQUIRED" in str(r.get("package_state") or ""))
    sched_likely = 0
    pricing = line_sched = bom = bid_form = body = 0
    lines_ready = 0
    cov80 = cov95 = 0
    material_total = 0
    expected_total = 0
    extracted_total = 0
    hard_zero: list[dict[str, Any]] = []
    ae_opps = 0
    grade_counts: Counter[str] = Counter()
    public_ready = 0
    public_lines = 0
    cov50 = cov75 = cov90 = 0
    revenue_ready = hist_only = no_rev = 0
    channel_c: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    signal_c: Counter[str] = Counter()

    scored = score_money_sprint_rows(results)
    for r, s in zip(results, scored):
        r["channel_fit"] = s.get("channel_fit") or s
        r["live_bidnet"] = True
        r["fixture"] = False
        se = r.get("schedule_extraction") if isinstance(r.get("schedule_extraction"), dict) else {}
        gate = r.get("_schedule_gate") if isinstance(r.get("_schedule_gate"), dict) else {}
        if gate.get("LIKELY_PRODUCT_SCHEDULE") or int(se.get("schedule_docs_found") or 0) > 0:
            sched_likely += 1
        roles = set(se.get("schedule_roles_present") or [])
        if "PRICING_SCHEDULE" in roles:
            pricing += 1
        if "LINE_ITEM_SCHEDULE" in roles:
            line_sched += 1
        if "BOM" in roles:
            bom += 1
        if "BID_FORM" in roles:
            bid_form += 1
        if gate.get("SOLICITATION_BODY_HAS_EXPLICIT_PRODUCT_LINES"):
            body += 1
        raw = int(r.get("raw_lines") or 0)
        if raw > 0:
            lines_ready += 1
        exp = se.get("EXPECTED_PRODUCT_LINES")
        ext = se.get("EXTRACTED_PRODUCT_LINES")
        if isinstance(exp, int):
            expected_total += exp
        if isinstance(ext, int):
            extracted_total += ext
        else:
            extracted_total += raw
        material_total += int(r.get("material_lines") or 0)
        cov_class = str(se.get("LINE_EXTRACTION_COVERAGE_CLASS") or "")
        cov_pct = se.get("LINE_EXTRACTION_COVERAGE")
        if cov_pct is None and isinstance(exp, int) and exp > 0:
            cov_pct = 100.0 * raw / exp
        if cov_pct is not None and cov_pct >= 80:
            cov80 += 1
        if cov_pct is not None and cov_pct >= 95:
            cov95 += 1
        for hz in se.get("SCHEDULE_PRESENT_EXTRACTION_ZERO") or []:
            if isinstance(hz, dict):
                hard_zero.append(hz)
        lines = r.get("line_items") if isinstance(r.get("line_items"), list) else []
        ae = 0
        for li in lines:
            if not isinstance(li, dict):
                continue
            g = _grade_letter(li)
            grade_counts[g] += 1
            if g in "ABCDE":
                ae += 1
        if ae > 0 or int(r.get("usable_ae") or 0) > 0:
            ae_opps += 1
        pub = int(r.get("public_prices") or 0)
        public_lines += pub
        if pub > 0:
            public_ready += 1
        cf = r.get("channel_fit") if isinstance(r.get("channel_fit"), dict) else {}
        vc = cf.get("PUBLIC_BASKET_VALUE_COVERAGE") or cf.get("PUBLIC_BASKET_LINE_COVERAGE")
        try:
            vc_f = float(vc) if vc is not None else None
        except (TypeError, ValueError):
            vc_f = None
        if vc_f is not None:
            if vc_f >= 50:
                cov50 += 1
            if vc_f >= 75:
                cov75 += 1
            if vc_f >= 90:
                cov90 += 1
        rev = str(r.get("revenue_state") or "")
        if rev == "ECONOMIC_REVENUE_USABLE":
            revenue_ready += 1
        elif "HIST" in rev.upper():
            hist_only += 1
        else:
            no_rev += 1
        channel_c[str(cf.get("CHANNEL_COMPETITION_CLASS") or "UNKNOWN")] += 1
        decisions[str(cf.get("PRE_QUOTE_DECISION") or "INSUFFICIENT_EVIDENCE")] += 1
        sig = (gate.get("signals") or {}) if isinstance(gate.get("signals"), dict) else {}
        for k, v in sig.items():
            if v:
                signal_c[k] += 1

    lines_ready_gate = lines_ready >= 12
    ae_gate = ae_opps >= 8
    pub_gate = public_ready >= 5
    pkg_gate = package >= 18
    sched_gate = sched_likely >= 15
    primary_pass = lines_ready_gate and ae_gate and pub_gate

    return {
        "selection": {
            "LIVE_CANDIDATES_REVIEWED": excluded.get("REVIEWED", 0),
            "SELECTED": n,
            "EXCLUDED_SERVICE": excluded.get("SERVICE", 0),
            "EXCLUDED_REPAIR": excluded.get("REPAIR", 0),
            "EXCLUDED_CONSTRUCTION": excluded.get("CONSTRUCTION", 0),
            "EXCLUDED_INSTALL": excluded.get("INSTALL", 0),
            "EXCLUDED_CATALOG_DISCOUNT": excluded.get("CATALOG_DISCOUNT", 0),
            "EXCLUDED_NO_SCHEDULE": excluded.get("NO_SCHEDULE", 0),
            "EXCLUDED_OTHER": excluded.get("OTHER", 0),
            "signals": dict(signal_c),
        },
        "package": {"PACKAGE_READY": package, "PACKAGE_FAIL": n - package},
        "schedule_discovery": {
            "LIKELY_PRODUCT_SCHEDULE": sched_likely,
            "PRICING_SCHEDULE": pricing,
            "LINE_ITEM_SCHEDULE": line_sched,
            "BOM": bom,
            "BID_FORM": bid_form,
            "BODY_TABLE": body,
        },
        "line_extraction": {
            "OPPORTUNITIES_WITH_LINES": lines_ready,
            "EXPECTED_PRODUCT_LINES": expected_total,
            "EXTRACTED_PRODUCT_LINES": extracted_total,
            "MATERIAL_PRODUCT_LINES": material_total,
            "EXTRACTION_COVERAGE_80": cov80,
            "EXTRACTION_COVERAGE_95": cov95,
            "SCHEDULE_PRESENT_EXTRACTION_ZERO": hard_zero,
        },
        "identity": {
            "OPPORTUNITIES_WITH_A_E": ae_opps,
            **{k: grade_counts.get(k, 0) for k in "ABCDEFG"},
        },
        "public_pricing": {
            "OPPORTUNITIES_PUBLIC_PRICE_READY": public_ready,
            "PUBLIC_PRICE_LINES": public_lines,
            "COVERAGE_50": cov50,
            "COVERAGE_75": cov75,
            "COVERAGE_90": cov90,
        },
        "revenue": {
            "REVENUE_READY": revenue_ready,
            "HISTORICAL_ONLY": hist_only,
            "NO_USABLE_REVENUE": no_rev,
        },
        "channel": dict(channel_c),
        "pre_quote": dict(decisions),
        "gates": {
            "PACKAGE_READY_GE_18": pkg_gate,
            "EXPECTED_PRODUCT_SCHEDULE_GE_15": sched_gate,
            "LINES_READY_GE_12": lines_ready_gate,
            "A_E_IDENTITY_OPPS_GE_8": ae_gate,
            "PUBLIC_PRICE_READY_OPPS_GE_5": pub_gate,
            "SCHEDULE_BACKED_PRODUCT_CANARY_PASS": primary_pass,
        },
        "scored_rows": scored,
    }


def _top5(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def rank(r: dict[str, Any]) -> tuple:
        cf = r.get("channel_fit") if isinstance(r.get("channel_fit"), dict) else {}
        return (
            int(r.get("public_prices") or 0),
            int(r.get("usable_ae") or 0),
            int(r.get("raw_lines") or 0),
            int(cf.get("QUOTE_PRIORITY_SCORE") or 0),
        )

    ordered = sorted(scored, key=rank, reverse=True)[:5]
    out = []
    for r in ordered:
        cf = r.get("channel_fit") if isinstance(r.get("channel_fit"), dict) else {}
        se = r.get("schedule_extraction") if isinstance(r.get("schedule_extraction"), dict) else {}
        gate = r.get("_schedule_gate") if isinstance(r.get("_schedule_gate"), dict) else {}
        inv = se.get("DOCUMENT_INVENTORY") or []
        sched_src = next(
            (d.get("filename") for d in inv if isinstance(d, dict) and d.get("line_schedule_likely")),
            gate.get("SOURCE_DOCUMENT"),
        )
        blocker = "NONE"
        if int(r.get("raw_lines") or 0) == 0:
            blocker = "LINE_EXTRACTION"
        elif int(r.get("usable_ae") or 0) == 0:
            blocker = "IDENTITY"
        elif int(r.get("public_prices") or 0) == 0:
            blocker = "PUBLIC_PRICING"
        elif r.get("revenue_state") != "ECONOMIC_REVENUE_USABLE":
            blocker = "REVENUE"
        elif cf.get("PRE_QUOTE_DECISION") == "INSUFFICIENT_EVIDENCE":
            blocker = "COVERAGE_OR_HEADROOM"
        out.append(
            {
                "opportunity_id": r.get("stable_key") or r.get("canonical_opportunity_id"),
                "buyer": r.get("buyer") or cf.get("buyer"),
                "title": r.get("title") or cf.get("title"),
                "deadline": r.get("deadline"),
                "days_remaining": r.get("days_remaining") or cf.get("days_remaining"),
                "package_status": r.get("package_state"),
                "schedule_source": sched_src,
                "expected_lines": se.get("EXPECTED_PRODUCT_LINES"),
                "extracted_lines": se.get("EXTRACTED_PRODUCT_LINES") or r.get("raw_lines"),
                "extraction_coverage": se.get("LINE_EXTRACTION_COVERAGE"),
                "material_lines": r.get("material_lines"),
                "ae_identity": r.get("usable_ae"),
                "public_priced_lines": r.get("public_prices"),
                "public_coverage": cf.get("PUBLIC_PRICE_COVERAGE_CLASS"),
                "government_value": cf.get("government_value") or r.get("government_value") or r.get("revenue_value"),
                "historical_value": r.get("historical_value"),
                "public_basket": cf.get("PUBLIC_BASKET_VALUE"),
                "channel_class": cf.get("CHANNEL_COMPETITION_CLASS"),
                "visible_headroom": cf.get("VISIBLE_HEADROOM"),
                "decision": cf.get("PRE_QUOTE_DECISION"),
                "exact_blocker": blocker,
                "next_action": cf.get("next_action"),
            }
        )
    return out


def _next_bottleneck(summary: dict[str, Any]) -> str:
    g = summary.get("gates") or {}
    if not g.get("LINES_READY_GE_12"):
        return "LINE_EXTRACTION"
    if not g.get("A_E_IDENTITY_OPPS_GE_8"):
        return "IDENTITY"
    if not g.get("PUBLIC_PRICE_READY_OPPS_GE_5"):
        return "PUBLIC_PRICING"
    le = summary.get("line_extraction") or {}
    if (summary.get("pre_quote") or {}).get("CALL_TODAY", 0) == 0:
        if (summary.get("public_pricing") or {}).get("COVERAGE_50", 0) == 0:
            return "PUBLIC_PRICING"
        if (summary.get("revenue") or {}).get("REVENUE_READY", 0) == 0:
            return "REVENUE"
        return "NO_HEADROOM"
    return "OTHER"


def run_schedule_backed_canary(
    *,
    canary_n: int = 20,
    price_budget: int = 25,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"SBC-{now_utc().strftime('%Y%m%d%H%M%S')}"
    precheck = inspect_durable_checkpoint()
    cmap = canonical_pipeline_map()

    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}

    # Prefer PRODUCT classes from checkpoint; selection gate does hard filtering
    product_rows = [r for r in rows if r.get("classification") in PRODUCT_CLASSES]
    candidates, excluded = select_schedule_backed_candidates(product_rows, store_by_cid, limit=canary_n)

    # If fewer than canary_n pass gate, widen: allow package-acquired product with schedule filename only after live detail
    if len(candidates) < canary_n:
        # Keep what we have; canary may run short — report honestly
        pass

    selected_ids = [str(c.get("stable_key") or c.get("canonical_opportunity_id")) for c in candidates]
    _save(
        SELECTED_IDS,
        {
            "build": BUILD,
            "run_id": run_id,
            "selected_ids": selected_ids,
            "excluded_counts": excluded,
            "updated_at": now_utc().isoformat(),
        },
    )
    write_status(phase="SELECTED", completed=0, remaining=len(candidates), percent=1, run_id=run_id, selected=len(candidates))

    if not candidates:
        report = {
            "build": BUILD,
            "run_id": run_id,
            "STATUS": "NO_CANDIDATES",
            "EXPAND_TO_100": "NO",
            "selection": excluded,
            "gates": {"SCHEDULE_BACKED_PRODUCT_CANARY_PASS": False},
            "blocker": "SELECTION",
        }
        _save(REPORT_JSON, report)
        write_status(phase="NO_CANDIDATES", percent=100, run_id=run_id)
        return report

    from bidnet_auth.client import BidNetAuthenticatedClient

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    errors = 0
    for i, item in enumerate(candidates):
        cid = str(item.get("canonical_opportunity_id") or item.get("stable_key") or "")
        store_row = store_by_cid.get(cid) or {}
        r = process_money_opportunity(
            item,
            store_row,
            client=client,
            store=store_by_cid,
            price_budget=price_budget,
        )
        r["_schedule_gate"] = item.get("_schedule_gate")
        r["live_bidnet"] = True
        r["fixture"] = False
        results.append(r)
        if r.get("error"):
            errors += 1
        if (i + 1) % 2 == 0 or i == len(candidates) - 1:
            merged = _merge_checkpoint(rows, results)
            _save(
                DOWNSTREAM_CHECKPOINT,
                {
                    "build": BUILD,
                    "engine": BUILD,
                    "updated_at": now_utc().isoformat(),
                    "classified": len(merged),
                    "deep_processed": sum(
                        1 for x in merged if x.get("classification") in PRODUCT_CLASSES and x.get("deep_complete")
                    ),
                    "rows": merged,
                },
            )
            write_status(
                phase="SCHEDULE_BACKED_CANARY_20",
                completed=len(results),
                remaining=max(0, len(candidates) - len(results)),
                percent=int(100 * len(results) / max(len(candidates), 1)),
                errors=errors,
                run_id=run_id,
            )
            if on_progress:
                try:
                    on_progress(phase="SCHEDULE_BACKED_CANARY_20", pct=int(100 * len(results) / max(len(candidates), 1)), completed=len(results))
                except Exception:
                    pass

    client.close()
    summary = _summarize(results, excluded)
    scored = summary.pop("scored_rows")
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "rows": scored, "updated_at": now_utc().isoformat()})
    _save(
        SCORES_JSON,
        {
            "build": BUILD,
            "source": "live_bidnet_canary",
            "fixture_leakage": False,
            "updated_at": now_utc().isoformat(),
            "count": len(scored),
            "rows": scored,
        },
    )
    buckets = queue_buckets(scored)
    call_today = len(buckets.get("CALL_TODAY") or [])
    primary = bool((summary.get("gates") or {}).get("SCHEDULE_BACKED_PRODUCT_CANARY_PASS"))
    bottleneck = None if primary and call_today >= 1 else _next_bottleneck(summary)

    report = {
        "build": BUILD,
        "run_id": run_id,
        "job_kind": "schedule_backed_canary",
        "STATUS": "PASS" if primary else "FAIL",
        "runtime_s": round(time.time() - started, 1),
        "precheck": precheck,
        "pipeline_map": cmap,
        "canary_input": len(candidates),
        "EXPAND_TO_100": "NO",
        **summary,
        "REAL_LIVE_CALL_TODAY": call_today,
        "top_5": _top5(scored),
        "call_today": [
            {
                "opportunity_id": r.get("stable_key"),
                "title": r.get("title"),
                "buyer": r.get("buyer"),
                "decision": (r.get("channel_fit") or {}).get("PRE_QUOTE_DECISION"),
            }
            for r in (buckets.get("CALL_TODAY") or [])[:5]
        ],
        "NEXT_TRUE_BOTTLENECK": bottleneck,
        "FIXTURE_LEAKAGE": "NO",
        "REAL_DATA_ONLY": "YES",
        "updated_at": now_utc().isoformat(),
    }
    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_schedule_canary_report(report))
    write_status(
        phase="DONE" if primary else "FAIL_GATES",
        percent=100,
        completed=len(results),
        SCHEDULE_BACKED_PRODUCT_CANARY_PASS="YES" if primary else "NO",
        REAL_LIVE_CALL_TODAY=call_today,
        blocker=bottleneck,
        run_id=run_id,
    )
    return report


def format_schedule_canary_report(report: dict[str, Any]) -> str:
    g = report.get("gates") or {}
    le = report.get("line_extraction") or {}
    ident = report.get("identity") or {}
    pub = report.get("public_pricing") or {}
    return "\n".join(
        [
            "M3 SCHEDULE-BACKED PRODUCT CANARY SUMMARY",
            f"BUILD: {report.get('build')}",
            f"RUN: {report.get('run_id')}",
            f"STATUS: {report.get('STATUS')}",
            f"SELECTED: {report.get('canary_input')}",
            f"LINES READY: {le.get('OPPORTUNITIES_WITH_LINES')}",
            f"A-E OPPS: {ident.get('OPPORTUNITIES_WITH_A_E')}",
            f"PUBLIC PRICE READY: {pub.get('OPPORTUNITIES_PUBLIC_PRICE_READY')}",
            f"SCHEDULE_BACKED_PRODUCT_CANARY_PASS: {'YES' if g.get('SCHEDULE_BACKED_PRODUCT_CANARY_PASS') else 'NO'}",
            f"REAL_LIVE_CALL_TODAY: {report.get('REAL_LIVE_CALL_TODAY')}",
            f"EXPAND_TO_100: NO",
            f"NEXT BOTTLENECK: {report.get('NEXT_TRUE_BOTTLENECK')}",
            "",
        ]
    )
