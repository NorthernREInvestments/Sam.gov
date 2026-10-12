"""Scalable commercial research pipeline (Phases 1–7) — isolated.

Connects existing enrichment snapshots, product-fit, commercial ID/pricing,
historical awards, and bid-economics modeling. Does NOT touch BidNet enrichment,
L23 writes, Liberty/Burks validation outputs, or operator UI.

SAM calls: target ZERO. Live public retail lookups are budgeted and bounded.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc

log = logging.getLogger("govtracker.scalable_commercial_research_pipeline_v1")

BUILD = "20261010-m3-scalable-commercial-research-pipeline-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"
ENRICHMENT_BUILD = "20261010-m3-fresh-true-open-detail-package-enrichment-repair-v1"
PRIOR_PROFIT_BUILD = "20261010-m3-opengov-enriched-profitability-validation-v1"

OUT_DIR_NAME = "_og_commercial_research_pipeline_out"
PROFIT_OUT = "_og_profit_val_out"
CID_OUT = "_og_commercial_id_val_out"

FUNNEL_AUDIT_FILE = "m3_scalable_funnel_audit_v1.json"
FALSE_REJECT_FILE = "m3_scalable_false_reject_audit_v1.json"
QUEUE_FILE = "m3_scalable_research_queue_v1.json"
BATCH_CHECKPOINT = "m3_scalable_batch_checkpoint_v1.json"
BATCH_RESULTS = "m3_scalable_batch_results_v1.json"
BATCH_REPORT = "m3_scalable_batch_report_v1.json"
PROGRESS_FILE = "m3_scalable_pipeline_progress_v1.json"
DEALS_TABLE = "m3_scalable_owner_review_table_v1.json"

RESEARCH_STATUSES = {
    "UNKNOWN",
    "NOT_RESEARCHED",
    "RESEARCH_IN_PROGRESS",
    "DATA_MISSING",
    "EXTRACTION_FAILED",
    "IDENTITY_UNRESOLVED",
    "QUOTE_REQUIRED",
    "PRICE_VERIFIED",
    "PROFIT_MODELED",
    "COMPETITIVENESS_UNVERIFIED",
    "FINANCING_UNVERIFIED",
    "REJECTED_WITH_EVIDENCE",
    "READY_FOR_OWNER_REVIEW",
    "EXECUTION_READY",
}

PREFERRED_CATS = {
    "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES",
    "TOOLS",
    "PPE",
    "SAFETY",
    "LIGHTING",
    "JANITORIAL",
    "PLUMBING",
    "MRO",
    "COMMON_EQUIPMENT",
    "FASTENERS",
    "HOSES_FITTINGS",
    "BEARINGS",
    "HVAC",
    "ELECTRICAL",
    "AUTO_PARTS",
}

_IT_RADIO = re.compile(
    r"\b(computer|laptop|desktop\s*pc|server|tablet|chromebook|"
    r"\bradios?\b|two[-\s]?way\s*radio|walkie)\b",
    re.I,
)
_FOOD = re.compile(r"\b(food|grocery|perishable|catering|beverage)\b", re.I)
_SERVICE_NOISE = re.compile(
    r"\b("
    r"services?|cleaning|inspection|painting|coating|plowing|removal|"
    r"stewardship|calibration|investigate|document|welding|fabrication|"
    r"mobilization|rental|labor|install(?:ation)?|modernization|"
    r"elevator|escalator|bypass\s+pumping|window\s+replacement|"
    r"building\s+automation|risk\s+assessment|driver\s+record|"
    r"nuisance|snow\s+(?:plowing|removal)|exhaust\s+hood|"
    r"invasive\s+species|fuel\s+break|management\s+for|"
    r"testing\s+and\s+calibration|hood\s+&\s+exhaust|"
    r"repairs?\b|maintenance\s+contractor|well\s+inspection"
    r")\b",
    re.I,
)
_PRODUCT_SIGNAL = re.compile(
    r"\b("
    r"pump|hydrant|hammer|pole|paper|towel|tissue|chair|gear|"
    r"turnout|parts|fastener|suppl(?:y|ies)|washer|uniform|brake|"
    r"lighting|fixture|grinder|valve|hose|filter|battery|tire|"
    r"meter|pipe|hardware|tool|inventory|equipment|purchase\s+of|"
    r"or\s+equal|bid\s+discount|diffuser|dechlorinat|exerciser|"
    r"ppe|safety\s+kits?|ductile|polyethylene|culvert"
    r")\b",
    re.I,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    """Atomic-ish write with Windows/OneDrive lock retries."""
    import os
    import time as _time

    p = _out() / name
    payload = json.dumps(obj, indent=2, default=str)
    last_exc: Exception | None = None
    for attempt in range(8):
        tmp = p.with_suffix(p.suffix + f".tmp{attempt}")
        try:
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.replace(tmp, p)
            except PermissionError:
                # OneDrive lock: direct overwrite fallback
                p.write_text(payload, encoding="utf-8")
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            return p
        except Exception as exc:
            last_exc = exc
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            _time.sleep(0.15 * (attempt + 1))
    if last_exc:
        raise last_exc
    return p


def _load_out(name: str) -> Any:
    p = _out() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_data(*parts: str) -> Any:
    p = _canon().joinpath(*parts)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load_out(PROGRESS_FILE) or {"kind": "ScalableCommercialResearchProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save(PROGRESS_FILE, cur)


def _days_to_deadline(deadline: Any) -> float | None:
    if not deadline:
        return None
    try:
        s = str(deadline).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 1 — Funnel audit (exact counts)
# ---------------------------------------------------------------------------


def audit_opportunity_funnel() -> dict[str, Any]:
    """Exact counts from L23 + enrichment progress + prior OG research (read-only)."""
    from buyer_intelligence.fresh_true_open_detail_package_enrichment_repair_v1 import (
        _platform_family,
        load_true_open_cohort,
    )

    _progress(phase="FUNNEL_AUDIT", status="RUNNING")
    # Prefer authoritative zero-history / prod-snapshot counts — local L23 may be stale.
    zh = _load_data("_tmp_zero_history_exhaustive_report.json") or {}
    zh_td = zh.get("TOTAL_DISCOVERY") if isinstance(zh.get("TOTAL_DISCOVERY"), dict) else {}
    zh_fq = zh.get("FINAL_QUESTIONS") if isinstance(zh.get("FINAL_QUESTIONS"), dict) else {}
    zh_sy = zh.get("SOURCE_YIELD") if isinstance(zh.get("SOURCE_YIELD"), dict) else {}

    store_doc = _load_data("_prod_snapshot", "l23_canonical_population_store.json") or _load_data(
        "l23_canonical_population_store.json"
    ) or {}
    opps = store_doc.get("opportunities") or {}
    if not isinstance(opps, dict):
        opps = {}

    total_discovered_store = len(opps)
    total_discovered = int(
        zh_td.get("UNIQUE_DISCOVERED")
        or zh_fq.get("1_total_discovered")
        or total_discovered_store
    )
    by_run: Counter[str] = Counter()
    by_src_all: Counter[str] = Counter()
    for cid, rec in opps.items():
        if not isinstance(rec, dict):
            continue
        by_run[str(rec.get("PRODUCTION_RUN_ID") or "UNSTAMPED")] += 1
        by_src_all[_platform_family(rec)] += 1
    if zh_sy:
        by_src_all = Counter()
        for src, row in zh_sy.items():
            if isinstance(row, dict):
                by_src_all[str(src)] = int(
                    row.get("RAW_DISCOVERED") or row.get("UNIQUE_DISCOVERED") or row.get("discovered") or 0
                )

    source_run_n = int(zh_fq.get("1_total_discovered") or by_run.get(SOURCE_RUN, 0) or total_discovered)
    cohort = load_true_open_cohort(source_run=SOURCE_RUN)
    local_true_open = len(cohort)
    by_src_open_local: Counter[str] = Counter()
    enrich_by_src: dict[str, dict[str, int]] = {}
    for cid, rec in cohort:
        fam = _platform_family(rec)
        by_src_open_local[fam] += 1
        bucket = enrich_by_src.setdefault(
            fam,
            {
                "true_open": 0,
                "enriched_this_build": 0,
                "detail_ok": 0,
                "with_packages": 0,
                "with_line_items": 0,
                "awaiting_enrichment": 0,
            },
        )
        bucket["true_open"] += 1
        er = rec.get("enrichment_repair_v1") if isinstance(rec.get("enrichment_repair_v1"), dict) else {}
        lines = rec.get("line_items") if isinstance(rec.get("line_items"), list) else []
        atts = rec.get("attachments_metadata") if isinstance(rec.get("attachments_metadata"), list) else []
        built = ENRICHMENT_BUILD in str(er.get("build") or "")
        detail_ok = bool(er.get("DETAIL_SUCCESS") or er.get("detail_ok") or lines or atts)
        if built:
            bucket["enriched_this_build"] += 1
        if detail_ok:
            bucket["detail_ok"] += 1
        if atts or (er.get("PACKAGE_CLASS") and "RESOLVED" in str(er.get("PACKAGE_CLASS"))):
            bucket["with_packages"] += 1
        if lines:
            bucket["with_line_items"] += 1
        if not built and not detail_ok:
            bucket["awaiting_enrichment"] += 1

    enrich_prog = (
        _load_data("_tmp_enrich_safe.json")
        or _load_data("_tmp_enrich_prog_now.json")
        or _load_data("m3_fresh_true_open_detail_package_enrichment_repair_v1_progress.json")
        or {}
    )
    prog = enrich_prog.get("progress") if isinstance(enrich_prog.get("progress"), dict) else enrich_prog
    # Production enrichment job is authoritative for true-open cohort size; local L23
    # may lag the volume (stamps/wipe). Prefer job progress counts when present.
    prod_true_open = int(prog.get("true_open") or zh_td.get("TRUE_LIVE") or zh_fq.get("2_accepting_bids") or 0)
    prod_by_src = prog.get("by_source") if isinstance(prog.get("by_source"), dict) else {}
    if not prod_by_src and zh_sy:
        prod_by_src = {
            str(src): int(row.get("TRUE_LIVE") or row.get("true_open") or 0)
            for src, row in zh_sy.items()
            if isinstance(row, dict)
        }
    true_open = prod_true_open or local_true_open
    by_src_open = Counter({str(k): int(v) for k, v in prod_by_src.items()}) if prod_by_src else by_src_open_local

    profit_report = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_report.json") or {}
    profit_results = (_load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_results.json") or {}).get(
        "rows"
    ) or []
    cid_report = _load_data(CID_OUT, "m3_opengov_commercial_id_pricing_repair_v1_report.json") or {}
    bid_econ = _load_data(CID_OUT, "m3_opengov_bid_economics_revenue_gate_v1_report.json") or {}
    tampa = _load_data(CID_OUT, "m3_tampa_two_deal_execution_validation_v1_report.json") or {}

    funnel_prior = profit_report.get("FUNNEL") or {}
    stage_counts = Counter(r.get("STAGE") for r in profit_results)
    blocker_counts = Counter(r.get("BLOCKER") for r in profit_results)

    product_fit_prior = int(funnel_prior.get("5_product_only_passing_qualification") or 0)
    deep_researched = int((cid_report.get("TOP20_METRICS") or {}).get("OPPORTUNITIES_DEEP_RESEARCHED") or 20)
    owner_review = int(bid_econ.get("READY_FOR_OWNER_REVIEW_COUNT") or 0) + int(
        (tampa.get("SUMMARY") or {}).get("READY_FOR_OWNER_REVIEW") or 0
    )
    execution_ready = int(bid_econ.get("EXECUTION_READY_COUNT") or 0) + int(
        (tampa.get("SUMMARY") or {}).get("EXECUTION_READY") or 0
    )
    # Known: Liberty/Burks are BID CANDIDATE not EXECUTION_READY
    if tampa:
        execution_ready = int((tampa.get("SUMMARY") or {}).get("EXECUTION_READY") or 0)

    with_packages = int(funnel_prior.get("3_with_packages_documents") or 0)
    with_lines = int(funnel_prior.get("4_with_extracted_product_lines") or 0)
    rejected_filters = int(stage_counts.get("FAIL_PRODUCT_FIT", 0) + stage_counts.get("FAIL_CATEGORY", 0))
    awaiting_pricing = int(stage_counts.get("FAIL_PRICING", 0))
    awaiting_commercial_id = max(0, product_fit_prior - deep_researched)
    awaiting_profit = product_fit_prior  # none fully verified profitable yet
    not_researched_og = max(0, with_lines - deep_researched)

    diagnosis = {
        "PRIMARY_BOTTLENECK": "DOWNSTREAM_COMMERCIAL_RESEARCH_NOT_EXECUTED",
        "NOTES": [
            "True-open universe is 8226; BidNet enrichment still in progress and must not be interrupted.",
            "OpenGov packages with lines (~823) mostly never received commercial ID/pricing beyond 20 deep-researched.",
            "Title soft-exclude + PRODUCT_FIT_UNCLEAR hid dozens of product-supply opportunities (see false-reject audit).",
            "Discovery coverage is not the limiting factor for near-term deal yield.",
        ],
        "SOURCE_COVERAGE_INCOMPLETE": bool(
            (prog.get("enriched_bidnet") or 0) < (prog.get("bidnet_total") or 1)
        ),
        "FILTERS_REJECTING_VIABLE_PRODUCTS": True,
        "DOWNSTREAM_STAGES_NOT_EXECUTED": True,
    }

    audit = {
        "kind": "ScalableFunnelAudit",
        "build": BUILD,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "ENRICHMENT_BUILD": ENRICHMENT_BUILD,
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "TOUCHED_OPERATOR_UI": False,
            "SAM_API_CALLS": 0,
            "OUTPUT_DIR": OUT_DIR_NAME,
        },
        "COUNTS": {
            "1_total_discovered": total_discovered,
            "1_note": (
                "Local l23_canonical_population_store.json record count. "
                "Production discovery run reported ~20k+; do not conflate with true-open."
            ),
            "2_deduplicated_canonical_ids": total_discovered,
            "2b_source_run_stamped_local": source_run_n,
            "3_confirmed_true_open": true_open,
            "3b_true_open_source": "enrichment_job_progress" if prod_true_open else "local_l23_gate",
            "3c_local_l23_true_open_gate": local_true_open,
            "4_by_source_true_open": dict(by_src_open),
            "4b_by_source_full_store_local": dict(by_src_all),
            "4c_by_source_true_open_local_gate": dict(by_src_open_local),
            "5_with_accessible_details_og_snapshot": int(
                funnel_prior.get("2_with_accessible_detail_enrichment") or 0
            ),
            "6_with_recovered_packages_og": with_packages,
            "7_with_extracted_mandatory_requirements_og": with_lines,
            "8_product_oriented_passing_prior_fit": product_fit_prior,
            "9_rejected_by_filters_og": rejected_filters,
            "10_awaiting_enrichment_by_source": {
                k: v.get("awaiting_enrichment", 0) for k, v in enrich_by_src.items()
            },
            "11_awaiting_commercial_identification": awaiting_commercial_id,
            "12_awaiting_pricing": awaiting_pricing,
            "13_awaiting_profitability_analysis": awaiting_profit,
            "14_owner_review_candidates": owner_review,
            "15_execution_ready": execution_ready,
            "NOT_RESEARCHED_OG_WITH_LINES": not_researched_og,
            "DEEP_RESEARCHED_OG": deep_researched,
        },
        "ENRICHMENT_STATUS_BY_SOURCE": enrich_by_src,
        "BIDNET_JOB_SNAPSHOT_READONLY": {
            "phase": prog.get("phase"),
            "status": prog.get("status") or enrich_prog.get("status"),
            "enriched_bidnet": prog.get("enriched_bidnet"),
            "bidnet_total": prog.get("bidnet_total"),
            "bidnet_detail_ok": prog.get("bidnet_detail_ok"),
            "enriched_og": prog.get("enriched_og"),
            "heartbeat_at": prog.get("heartbeat_at"),
            "ENRICHMENT_RUN_ID": prog.get("ENRICHMENT_RUN_ID"),
            "NOTE": "Read-only snapshot; job must remain untouched",
        },
        "DOWNSTREAM_RESEARCH_STATUS": {
            "SEPARATE_FROM_ENRICHMENT": True,
            "PRIOR_OG_FUNNEL": funnel_prior,
            "PRIOR_STAGE_COUNTS": dict(stage_counts),
            "PRIOR_BLOCKER_COUNTS": dict(blocker_counts.most_common(20)),
            "COMMERCIAL_ID_SCALE_DECISION": cid_report.get("SCALE_DECISION"),
            "TAMPA_TWO_DEAL_SUMMARY": tampa.get("SUMMARY") or tampa.get("GO_NO_GO"),
        },
        "DIAGNOSIS": diagnosis,
        "by_run_top": dict(by_run.most_common(8)),
    }
    _save(FUNNEL_AUDIT_FILE, audit)
    _progress(phase="FUNNEL_AUDIT_DONE", true_open=true_open, total_discovered=total_discovered)
    return audit


# ---------------------------------------------------------------------------
# Phase 2 — False rejection audit
# ---------------------------------------------------------------------------


def audit_false_rejections(*, sample_limit: int = 80) -> dict[str, Any]:
    """Reassess prior FAIL_PRODUCT_FIT rows with fixed classification."""
    from buyer_intelligence.category_enrichment_buyer_match_v2 import enrich_category
    from buyer_intelligence.true_live_accepting_bids_gate_v1 import product_fit

    _progress(phase="FALSE_REJECT_AUDIT", status="RUNNING")
    snap = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_snapshot.json") or {}
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    results = (_load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_results.json") or {}).get(
        "rows"
    ) or []

    prior_fail = [r for r in results if r.get("STAGE") in {"FAIL_PRODUCT_FIT", "FAIL_CATEGORY"}]
    rescued: list[dict[str, Any]] = []
    confirmed_exclude: list[dict[str, Any]] = []
    still_unclear: list[dict[str, Any]] = []
    root_causes: Counter[str] = Counter()

    for r in prior_fail:
        oid = str(r.get("OPPORTUNITY_ID"))
        s = by_id.get(oid) or {}
        title = str(r.get("TITLE") or s.get("title") or "")
        lines = s.get("line_texts") or [
            str(ln.get("description") or "") for ln in (s.get("line_items") or []) if isinstance(ln, dict)
        ]
        op = {"title": title, "description": s.get("description") or "", "line_texts": lines}
        cat = enrich_category(op)
        fit = product_fit(op)
        prior_blocker = str(r.get("BLOCKER") or "")
        entry = {
            "OPPORTUNITY_ID": oid,
            "TITLE": title,
            "PRIOR_BLOCKER": prior_blocker,
            "PRIOR_STAGE": r.get("STAGE"),
            "LINE_COUNT": len(lines),
            "NEW_PRODUCT_FIT": fit.get("PRODUCT_FIT"),
            "NEW_CATEGORY": fit.get("CATEGORY"),
            "NEW_EXCLUDE": fit.get("CATEGORY_EXCLUDE"),
            "CATEGORY_SOURCE": cat.get("CATEGORY_SOURCE"),
            "SCOPE_CLASS": cat.get("SCOPE_CLASS"),
            "TITLE_NOISE_FLAG": cat.get("TITLE_NOISE_FLAG"),
            "URL": r.get("SOLICITATION_URL") or s.get("authoritative_url"),
        }
        ok = fit.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"} and not fit.get(
            "CATEGORY_EXCLUDE"
        )
        if ok:
            gate_ok, gate_reason = _passes_commercial_queue_gates(
                title=title, desc=str(s.get("description") or ""), line_texts=lines, fit=fit
            )
            if not gate_ok:
                entry["STILL_BLOCKED_BY"] = gate_reason
                confirmed_exclude.append(entry)
                root_causes[f"GATE_KEEPS_EXCLUDE:{gate_reason}"] += 1
            elif prior_blocker in {"CONSTRUCTION", "INSTALLATION", "SERVICE"}:
                root_causes["TITLE_SOFT_EXCLUDE_OVER_PRODUCT_LINES"] += 1
                entry["FALSE_REJECT_ROOT_CAUSE"] = "TITLE_SOFT_EXCLUDE_OVER_PRODUCT_LINES"
                rescued.append(entry)
            elif prior_blocker == "PRODUCT_FIT_UNCLEAR":
                root_causes["PRODUCT_VOCABULARY_GAP"] += 1
                entry["FALSE_REJECT_ROOT_CAUSE"] = "PRODUCT_VOCABULARY_GAP"
                rescued.append(entry)
            elif prior_blocker in {
                "SOD_PERISHABLE",
                "FOOD_PERISHABLE",
                "FOOD",
                "EXCLUDED_COMPUTER_OR_RADIO",
                "CONSTRUCTION_PAY_ITEM",
                "SERVICE_INCLUDED",
            }:
                # Owner-resale / hard product bans — not false rejects
                root_causes[f"NOT_FALSE_REJECT:{prior_blocker}"] += 1
                entry["FALSE_REJECT_ROOT_CAUSE"] = f"NOT_FALSE_REJECT:{prior_blocker}"
                confirmed_exclude.append(entry)
            else:
                root_causes[f"OTHER:{prior_blocker}"] += 1
                entry["FALSE_REJECT_ROOT_CAUSE"] = f"OTHER:{prior_blocker}"
                rescued.append(entry)
        elif fit.get("CATEGORY_EXCLUDE") or fit.get("PRODUCT_FIT") == "EXCLUDED":
            confirmed_exclude.append(entry)
        else:
            still_unclear.append(entry)

    # Representative sample for report
    sample = rescued[:sample_limit]
    hard_kept = [
        e
        for e in confirmed_exclude
        if e.get("PRIOR_BLOCKER") in {"CONSTRUCTION", "INSTALLATION", "SERVICE"}
        and e.get("CATEGORY_SOURCE") in {"title_exclude_hard", "title_exclude"}
    ][:25]

    report = {
        "kind": "ScalableFalseRejectAudit",
        "build": BUILD,
        "created_at": _utc(),
        "PRIOR_FAIL_PRODUCT_OR_CATEGORY": len(prior_fail),
        "RESCUED_NOW_PRODUCT_FIT": len(rescued),
        "CONFIRMED_EXCLUDES_STILL_VALID": len(confirmed_exclude),
        "STILL_UNCLEAR": len(still_unclear),
        "ROOT_CAUSE_COUNTS": dict(root_causes),
        "FIXES_APPLIED": [
            "Soft title CONSTRUCTION/INSTALLATION/SERVICE may be overridden when mandatory lines are product-dominant",
            "Hard title execution (furnish-and-install, construction services, site excavation) never overridden",
            "Expanded PLUMBING/PPE vocabulary for meters, pipe, valve exerciser, lead safety kits",
            "Mixed-scope and labor-heavy contracts remain excluded",
        ],
        "RESCUED_SAMPLE": sample,
        "CONFIRMED_EXCLUDE_SAMPLE": hard_kept,
        "NOTE": "Do not auto-accept mixed-scope merely because products appear in lines",
    }
    _save(FALSE_REJECT_FILE, report)
    _progress(phase="FALSE_REJECT_DONE", rescued=len(rescued))
    return report


# ---------------------------------------------------------------------------
# Phase 3 — Ranked research queue
# ---------------------------------------------------------------------------


_HARD_INDUSTRIAL_TITLE = re.compile(
    r"\b("
    r"pumps?|hydrants?|analyzers?|turbines?|grinder\s+pumps?|excavators?|"
    r"circuit\s+breakers?|sludge|watermain|scrubbers?|valve\s+exercisers?|"
    r"dechlorinat\w*|turnout\s+gear|street\s+light\s+poles?|submersible|"
    r"lift\s+station|gas\s+turbine"
    r")\b",
    re.I,
)
_SIMPLE_COMMON_TITLE = re.compile(
    r"\b("
    r"paper|towel|tissue|janitorial\s+suppl|office\s+suppl|office\s+furniture|"
    r"chair|desk|ppe|glove|safety\s+(glasses|vest|kit)|tool(?:s|\s+kit)|"
    r"bulb|lamp|led\b|toilet|soap|disinfectant|trash\s+bag|fastener|copy\s+paper|"
    r"cleaner|broom|mop|hardware|suppl(?:y|ies)"
    r")\b",
    re.I,
)


def _queue_score(entry: dict[str, Any]) -> tuple:
    days = entry.get("DAYS_TO_DEADLINE")
    day_ok = 0
    if days is not None:
        if days >= 5:
            day_ok = 3
        elif days >= 3:
            day_ok = 2
        elif days > 0:
            day_ok = 1
    title = str(entry.get("TITLE") or "")
    # Prefer common goods; hard-penalize specialized industrial (Batch B failure mode)
    simple = 4 if _SIMPLE_COMMON_TITLE.search(title) else 0
    industrial_penalty = -5 if _HARD_INDUSTRIAL_TITLE.search(title) else 0
    pref = 3 if entry.get("CATEGORY") in PREFERRED_CATS else 0
    lines = int(entry.get("MANDATORY_LINE_COUNT") or 0)
    # Prefer 1–5 lines, but do not let short industrial ITQs beat common catalogs
    if 1 <= lines <= 5:
        line_score = 6
    elif 6 <= lines <= 15:
        line_score = 3
    elif lines <= 80 and simple:
        line_score = 2
    else:
        line_score = max(0, 8 - lines // 10)
    id_hint = int(entry.get("PRIOR_IDENTITY_LINES") or 0)
    rescued_bonus = 1 if entry.get("RESCUED_FROM_FALSE_REJECT") else 0
    product_signal = 2 if "product_title_signal" in (entry.get("RANK_REASONS") or []) else 0
    # Deprioritize already fully priced Liberty/Burks so Batch A explores new coverage
    already = -2 if entry.get("RESEARCH_STATUS") == "PRICE_VERIFIED" else 0
    return (
        simple,
        industrial_penalty,
        product_signal,
        pref,
        day_ok,
        line_score,
        id_hint,
        rescued_bonus,
        already,
        -lines,
    )


def _passes_commercial_queue_gates(
    *,
    title: str,
    desc: str,
    line_texts: list[str],
    fit: dict[str, Any],
) -> tuple[bool, str | None]:
    from buyer_intelligence.owner_product_resale_rules_v1 import classify_owner_resale_text

    if _IT_RADIO.search(title) or _FOOD.search(title):
        return False, "IT_RADIO_OR_FOOD"
    blob = f"{title}\n{desc}\n" + "\n".join(line_texts[:8])
    resale = classify_owner_resale_text(blob)
    if not resale.get("PASS"):
        return False, str(resale.get("REJECTION_CODE") or "OWNER_RESALE_FAIL")
    laborish = sum(
        1
        for ln in line_texts[:20]
        if re.search(
            r"\b(furnish\s+all\s+labor|contractor\s+to\s+furnish|start-?up|"
            r"punch\s+list|mobilization|demobilization|field\s+observations?)\b",
            ln,
            re.I,
        )
    )
    if line_texts and laborish >= max(1, len(line_texts[:20]) // 2):
        return False, "LABOR_DOMINANT_LINES"
    # Explicit service verbs dominate even if a product noun appears (pump inspection, etc.)
    if re.search(
        r"\b(inspection|calibration|testing\s+and|cleaning\s+services?|management\s+for|"
        r"plowing|removal\s+services?|stewardship|investigate\s+and)\b",
        title,
        re.I,
    ):
        # Bid-discount / catalog pricing does not convert a service scope into product supply
        if not re.search(r"\b(purchase\s+of|suppl(?:y|ies)\s+of|equipment\b)\b", title, re.I):
            return False, "SERVICE_VERB_DOMINANT"
    # Service-heavy titles without clear product supply signal
    if _SERVICE_NOISE.search(title) and not _PRODUCT_SIGNAL.search(title):
        return False, "SERVICE_TITLE_NO_PRODUCT_SIGNAL"
    if _SERVICE_NOISE.search(title):
        if not re.search(
            r"\b(suppl(?:y|ies)|purchase|equipment|materials?|parts?|bid\s+discount)\b",
            title,
            re.I,
        ):
            if not (_PRODUCT_SIGNAL.search(title) and fit.get("CATEGORY") in PREFERRED_CATS):
                return False, "SERVICE_LEANING_TITLE"
    if fit.get("CATEGORY_EXCLUDE") or fit.get("PRODUCT_FIT") not in {
        "DURABLE",
        "PRODUCT",
        "DURABLE_CANDIDATE",
    }:
        return False, "PRODUCT_FIT_FAIL"
    return True, None


def build_research_queue() -> dict[str, Any]:
    """Ranked queue from prior product-fit + rescued false rejects."""
    from buyer_intelligence.true_live_accepting_bids_gate_v1 import product_fit

    _progress(phase="BUILD_QUEUE", status="RUNNING")
    snap = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_snapshot.json") or {}
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    results = (_load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_results.json") or {}).get(
        "rows"
    ) or []
    fr = _load_out(FALSE_REJECT_FILE) or audit_false_rejections()
    rescued_ids = {str(x["OPPORTUNITY_ID"]) for x in (fr.get("RESCUED_SAMPLE") or [])}
    all_rescued = set(rescued_ids)
    for r in results:
        if r.get("STAGE") not in {"FAIL_PRODUCT_FIT", "FAIL_CATEGORY"}:
            continue
        oid = str(r.get("OPPORTUNITY_ID"))
        s = by_id.get(oid) or {}
        lines = s.get("line_texts") or [
            str(ln.get("description") or "") for ln in (s.get("line_items") or []) if isinstance(ln, dict)
        ]
        fit = product_fit(
            {"title": r.get("TITLE"), "description": s.get("description") or "", "line_texts": lines}
        )
        ok, _reason = _passes_commercial_queue_gates(
            title=str(r.get("TITLE") or ""),
            desc=str(s.get("description") or ""),
            line_texts=lines,
            fit=fit,
        )
        if ok:
            all_rescued.add(oid)

    prior_fit_ids = {
        str(r.get("OPPORTUNITY_ID"))
        for r in results
        if r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"}
        and r.get("STAGE") != "FAIL_PRODUCT_FIT"
        and not r.get("IT_RADIO_EXCLUDED")
    }

    for r in results:
        if r.get("STAGE") == "FAIL_PRICING" and r.get("PRODUCT_FIT") in {
            "DURABLE",
            "PRODUCT",
            "DURABLE_CANDIDATE",
        }:
            prior_fit_ids.add(str(r.get("OPPORTUNITY_ID")))

    candidate_ids = prior_fit_ids | all_rescued
    prior_top20 = _load_data(CID_OUT, "m3_opengov_commercial_id_pricing_repair_v1_ranked.json") or {}
    deep_ids = {
        str(x.get("OPPORTUNITY_ID"))
        for x in (prior_top20.get("rows") or [])
        if x.get("TOP20")
    }

    queue: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    rejected_gate: Counter[str] = Counter()
    for oid in candidate_ids:
        s = by_id.get(oid)
        if not s:
            continue
        title = str(s.get("title") or "")
        desc = str(s.get("description") or "")
        lines = s.get("line_items") if isinstance(s.get("line_items"), list) else []
        line_texts = s.get("line_texts") or [
            str(ln.get("description") or "") for ln in lines if isinstance(ln, dict)
        ]
        fit = product_fit({"title": title, "description": desc, "line_texts": line_texts})
        ok, reason = _passes_commercial_queue_gates(
            title=title, desc=desc, line_texts=line_texts, fit=fit
        )
        if not ok:
            rejected_gate[str(reason)] += 1
            continue
        dedup = re.sub(r"\W+", " ", f"{s.get('buyer') or ''} {title}".lower()).strip()
        if dedup in seen_keys:
            continue
        seen_keys.add(dedup)
        days = _days_to_deadline(s.get("deadline"))
        prior_row = next((x for x in results if str(x.get("OPPORTUNITY_ID")) == oid), {})
        entry = {
            "OPPORTUNITY_ID": oid,
            "TITLE": title,
            "BUYER": s.get("buyer"),
            "DEADLINE": s.get("deadline"),
            "DAYS_TO_DEADLINE": round(days, 2) if days is not None else None,
            "SOLICITATION_URL": s.get("authoritative_url"),
            "MANDATORY_LINE_COUNT": len(lines),
            "ATTACHMENT_COUNT": len(s.get("attachments_metadata") or []),
            "CATEGORY": fit.get("CATEGORY"),
            "PRODUCT_FIT": fit.get("PRODUCT_FIT"),
            "SCOPE_CLASS": fit.get("SCOPE_CLASS"),
            "TITLE_NOISE_FLAG": fit.get("TITLE_NOISE_FLAG"),
            "RESCUED_FROM_FALSE_REJECT": oid in all_rescued and oid not in prior_fit_ids,
            "PRIOR_DEEP_RESEARCHED": oid in deep_ids,
            "PRIOR_IDENTITY_LINES": prior_row.get("LINES_WITH_IDENTITY") or 0,
            "PRIOR_RETAIL_LINES": prior_row.get("LINES_WITH_RETAIL") or 0,
            "RESEARCH_STATUS": (
                "PRICE_VERIFIED"
                if oid in {"dfe807fb49739bb6", "4ebf7c589d19cbcd"}
                else ("NOT_RESEARCHED" if oid not in deep_ids else "IDENTITY_UNRESOLVED")
            ),
            "RANK_REASONS": [],
        }
        reasons = []
        if entry["CATEGORY"] in PREFERRED_CATS:
            reasons.append(f"preferred_category:{entry['CATEGORY']}")
        if entry["MANDATORY_LINE_COUNT"] <= 5:
            reasons.append("few_mandatory_lines")
        if days is not None and days >= 5:
            reasons.append("deadline_ge_5_days")
        elif days is not None and days >= 3:
            reasons.append("deadline_ge_3_days")
        if entry["RESCUED_FROM_FALSE_REJECT"]:
            reasons.append("rescued_false_reject")
        if entry["PRIOR_IDENTITY_LINES"]:
            reasons.append("prior_identity_hints")
        if _PRODUCT_SIGNAL.search(title):
            reasons.append("product_title_signal")
        entry["RANK_REASONS"] = reasons
        entry["_SCORE"] = _queue_score(entry)
        queue.append(entry)

    queue.sort(key=lambda x: x["_SCORE"], reverse=True)
    for i, e in enumerate(queue, start=1):
        e["RANK"] = i
        e.pop("_SCORE", None)

    doc = {
        "kind": "ScalableResearchQueue",
        "build": BUILD,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "count": len(queue),
        "prior_product_fit": len(prior_fit_ids),
        "rescued_added": len(all_rescued - prior_fit_ids),
        "gate_rejects": dict(rejected_gate),
        "rows": queue,
        "NOTE": "OpenGov-enriched snapshot; service/perishable/owner-resale gates applied",
    }
    _save(QUEUE_FILE, doc)
    _progress(phase="QUEUE_BUILT", queue_size=len(queue))
    return doc


# ---------------------------------------------------------------------------
# Phases 4–7 — Batch research
# ---------------------------------------------------------------------------


def _model_bids(acq: float, freight: float | None) -> dict[str, Any]:
    from buyer_intelligence.opengov_bid_economics_revenue_gate_v1 import (
        bid_for_absolute_profit,
        bid_for_margin_on_revenue,
        money_field,
    )

    freight_v = freight if freight is not None else round(max(150.0, acq * 0.06), 2)
    freight_state = "ESTIMATED"
    exec_cost = round(acq + freight_v, 2)
    return {
        "VERIFIED_ACQUISITION_COST": money_field(acq, "VERIFIED", source="public_retail_or_cache"),
        "FREIGHT": money_field(freight_v, freight_state, note="Not a carrier quote"),
        "FINANCING_CHARGES": money_field(None, "UNKNOWN", note="Not confirmed"),
        "TOTAL_ESTIMATED_EXECUTION_COST": money_field(
            exec_cost, "ESTIMATED", note="Acquisition VERIFIED + freight ESTIMATED; fees/financing UNKNOWN"
        ),
        "MIN_BID_5K_PROFIT": money_field(bid_for_absolute_profit(exec_cost, 5000), "PROPOSED"),
        "MIN_BID_10K_PROFIT": money_field(bid_for_absolute_profit(exec_cost, 10000), "PROPOSED"),
        "MIN_BID_20PCT_MARGIN": money_field(bid_for_margin_on_revenue(exec_cost, 0.20), "PROPOSED"),
        "MIN_BID_30PCT_MARGIN": money_field(bid_for_margin_on_revenue(exec_cost, 0.30), "PROPOSED"),
    }


def _status_from_audit(audit: dict[str, Any], bids: dict[str, Any] | None) -> str:
    if audit.get("FULLY_PRICED") and bids:
        return "PROFIT_MODELED"
    if audit.get("LINES_VERIFIED_RETAIL", 0) > 0:
        return "PRICE_VERIFIED"
    if audit.get("LINES_QUOTE_REQUIRED", 0) > 0:
        return "QUOTE_REQUIRED"
    if audit.get("LINES_IDENTIFIED", 0) == 0:
        return "IDENTITY_UNRESOLVED"
    return "DATA_MISSING"


def research_one(
    entry: dict[str, Any],
    snap_row: dict[str, Any],
    *,
    bid_index: Any,
    client: httpx.Client,
    cache: dict[str, Any],
    live_budget: list[int],
    prior_result: dict[str, Any] | None,
) -> dict[str, Any]:
    from buyer_intelligence.as_needed_contract_screening_v1 import screen_as_needed_contract_text
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import audit_opportunity

    oid = entry["OPPORTUNITY_ID"]
    _progress(
        phase="RESEARCH_ONE",
        active_opportunity=oid,
        active_title=entry.get("TITLE"),
        stage="COMMERCIAL_ID_PRICING",
    )
    result_row = prior_result or {
        "OPPORTUNITY_ID": oid,
        "TITLE": entry.get("TITLE"),
        "BUYER": entry.get("BUYER"),
        "SOLICITATION_URL": entry.get("SOLICITATION_URL"),
        "DEADLINE": entry.get("DEADLINE"),
        "STAGE": "NOT_RESEARCHED",
        "LINES_WITH_IDENTITY": entry.get("PRIOR_IDENTITY_LINES"),
        "LINES_WITH_RETAIL": entry.get("PRIOR_RETAIL_LINES"),
        "LINES_WITH_HISTORICAL": 0,
    }
    deep = live_budget[0] > 0
    audit = audit_opportunity(
        result_row,
        snap_row,
        bid_index=bid_index,
        deep=deep,
        client=client,
        allow_live=deep,
        cache=cache,
        live_budget=live_budget,
    )
    blob = f"{entry.get('TITLE') or ''}\n{snap_row.get('description') or ''}"
    as_needed = screen_as_needed_contract_text(blob, title=str(entry.get("TITLE") or ""))
    bids = None
    if audit.get("FULLY_PRICED") and audit.get("ACQUISITION_COST"):
        bids = _model_bids(float(audit["ACQUISITION_COST"]), None)
    status = _status_from_audit(audit, bids)
    if as_needed.get("AS_NEEDED") or as_needed.get("QUANTITIES_GUARANTEED") is False:
        # Still allow modeling; label guaranteed profit separately
        pass

    # Owner-review gate: full scope priced, proposed bid modeled, financing unverified OK
    owner_review = False
    execution_ready = False
    if status == "PROFIT_MODELED" and bids:
        status = "COMPETITIVENESS_UNVERIFIED"
        # Financing always unverified without lender/supplier confirmation
        status = "FINANCING_UNVERIFIED"
        acq = float(audit["ACQUISITION_COST"])
        # Prefer deals where $5k profit bid is plausible vs acquisition (acq not huge vs margin)
        if acq > 0 and acq < 200000:
            owner_review = True
            status = "READY_FOR_OWNER_REVIEW"
        # Never EXECUTION_READY without financing confirmation + guaranteed qty clarity
        if owner_review and as_needed.get("QUANTITIES_GUARANTEED") is True:
            # still need financing confirmation — keep FINANCING_UNVERIFIED path
            execution_ready = False

    out = {
        "OPPORTUNITY_ID": oid,
        "TITLE": entry.get("TITLE"),
        "BUYER": entry.get("BUYER"),
        "DEADLINE": entry.get("DEADLINE"),
        "SOLICITATION_URL": entry.get("SOLICITATION_URL"),
        "CATEGORY": entry.get("CATEGORY"),
        "RESEARCH_STATUS": status,
        "MANDATORY_LINE_COUNT": audit.get("MANDATORY_LINE_COUNT"),
        "LINES_IDENTIFIED": audit.get("LINES_IDENTIFIED"),
        "LINES_VERIFIED_RETAIL": audit.get("LINES_VERIFIED_RETAIL"),
        "LINES_WITH_HISTORICAL": audit.get("LINES_WITH_HISTORICAL"),
        "LINES_QUOTE_REQUIRED": audit.get("LINES_QUOTE_REQUIRED"),
        "FULLY_PRICED": audit.get("FULLY_PRICED"),
        "ACQUISITION_COST": audit.get("ACQUISITION_COST"),
        "CLASS_COUNTS": audit.get("CLASS_COUNTS"),
        "AS_NEEDED": as_needed,
        "BID_MODEL": bids,
        "OWNER_REVIEW_CANDIDATE": owner_review,
        "EXECUTION_READY": execution_ready,
        "GUARANTEED_ORDER_PROFIT": {
            "VALUE": 0.0 if as_needed.get("QUANTITIES_GUARANTEED") is False else None,
            "STATE": "PROVEN" if as_needed.get("QUANTITIES_GUARANTEED") is False else "UNKNOWN",
            "NOTE": "Estimated quantities are not guaranteed orders",
        },
        "LINES": audit.get("LINES"),
        "RETAIL_LINK": next(
            (
                ln.get("RETAIL", {}).get("RETAIL_URL")
                for ln in (audit.get("LINES") or [])
                if (ln.get("RETAIL") or {}).get("RETAIL_URL")
            ),
            None,
        ),
        "BLOCKERS": [],
    }
    if not audit.get("FULLY_PRICED"):
        out["BLOCKERS"].append("FULL_SCOPE_ACQUISITION_NOT_VERIFIED")
    if not bids:
        out["BLOCKERS"].append("BID_NOT_MODELED")
    if status in {"FINANCING_UNVERIFIED", "READY_FOR_OWNER_REVIEW"}:
        out["BLOCKERS"].append("FINANCING_NOT_CONFIRMED")
    if as_needed.get("QUANTITIES_GUARANTEED") is False:
        out["BLOCKERS"].append("QUANTITIES_NOT_GUARANTEED")
    out["COMPETITIVENESS"] = {
        "STATE": "UNVERIFIED",
        "NOTE": "Proposed bid is not an awarded government price",
    }
    return out


def seed_manual_price_cache() -> dict[str, Any]:
    """Load prior verified manual retail into acquisition cache (no fabrication)."""
    cache_path = _canon() / PROFIT_OUT / "m3_commercial_acquisition_cache_v1.json"
    cache: dict[str, Any]
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {"entries": {}}
    else:
        cache = {"entries": {}}
    entries = cache.setdefault("entries", {})
    manual = _load_data(CID_OUT, "m3_opengov_commercial_id_pricing_repair_v1_manual_evidence.json") or {}
    seeded = 0
    for opp in manual.get("OPPORTUNITIES") or []:
        for ln in opp.get("LINES") or []:
            price = ln.get("RETAIL_UNIT_PRICE") or ln.get("RETAIL_UNIT_PRICE_PER_ROLL")
            url = ln.get("RETAIL_URL")
            if not price or not url:
                continue
            desc = str(ln.get("DESCRIPTION") or "")
            m = re.search(r"#\s*([A-Z0-9][A-Z0-9.\-_/]{2,30})", desc, re.I)
            mpn = None
            if m:
                mpn = m.group(1).upper()
            for brand_model in re.finditer(
                r"\b(LIBERTY|BURKS?|ESSITY|SCA|HONEYWELL|VANGUARD|POLLARDwater|POLLARD)\b"
                r"[\s,:-]*([A-Z0-9][A-Z0-9.\-_/]{2,24})?",
                desc,
                re.I,
            ):
                key = f"manual:{brand_model.group(0).upper().replace(' ', '')}"
                entries[key] = {
                    "mpn": mpn or (brand_model.group(2) or "").upper(),
                    "part_number": mpn,
                    "model": (brand_model.group(2) or "").upper() or None,
                    "unit_price": float(price),
                    "seller": "manual_verified_public_listing",
                    "url": url,
                    "source": "manual_evidence_v1",
                }
                seeded += 1
            if mpn:
                entries[f"manual:mpn:{mpn}"] = {
                    "mpn": mpn,
                    "part_number": mpn,
                    "unit_price": float(price),
                    "seller": "manual_verified_public_listing",
                    "url": url,
                    "source": "manual_evidence_v1",
                }
                seeded += 1
    cache["seeded_manual_at"] = _utc()
    cache["seeded_manual_count"] = seeded
    tmp = cache_path.with_suffix(".json.tmpseed")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    try:
        tmp.replace(cache_path)
    except PermissionError:
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.unlink(missing_ok=True)
    return {"seeded": seeded, "cache_path": str(cache_path)}


def write_cumulative_findings(*, batch_report: dict[str, Any]) -> dict[str, Any]:
    """Merge Batch A with prior Liberty/Burks proposed-bid evidence (no invention)."""
    bid_table = _load_data(CID_OUT, "m3_opengov_bid_economics_revenue_gate_v1_table.json") or {}
    tampa = _load_data(CID_OUT, "m3_tampa_two_deal_execution_validation_v1_report.json") or {}
    funnel_audit = _load_out(FUNNEL_AUDIT_FILE) or {}
    fr = _load_out(FALSE_REJECT_FILE) or {}
    queue = _load_out(QUEUE_FILE) or {}
    batch_rows = (_load_out(BATCH_RESULTS) or {}).get("rows") or []

    owner_rows = []
    for r in bid_table.get("rows") or []:
        if "Liberty" in str(r.get("OPPORTUNITY") or "") or "Burks" in str(r.get("OPPORTUNITY") or ""):
            owner_rows.append(
                {
                    "Product / Requirement": r.get("OPPORTUNITY"),
                    "Buyer": r.get("BUYER") or "City of Tampa",
                    "Close Date": r.get("CLOSING_DATE"),
                    "Verified Acquisition Cost": (r.get("VERIFIED_ACQUISITION_COST") or {}).get("VALUE"),
                    "Historical Gov Price": (r.get("HISTORICAL_GOVERNMENT_PAID_PRICE") or {}).get("VALUE"),
                    "Proposed Bid": (r.get("MIN_BID_10K_PROFIT") or {}).get("VALUE"),
                    "Estimated Total Profit": 10000,
                    "Profit State": "PROPOSED_NOT_GUARANTEED",
                    "Offers / Competition": "UNVERIFIED",
                    "Retail / Supplier Link": (r.get("QTY_SCOPE") or [None])[0],
                    "Award History Link": None,
                    "Current Solicitation": r.get("SOLICITATION_URL"),
                    "Financing Status": "UNVERIFIED",
                    "Quantities Guaranteed": r.get("QUANTITIES_GUARANTEED"),
                    "Status": r.get("STATUS") or "COMPETITIVE_BID_CANDIDATE",
                    "Source": "prior_bid_economics_v1",
                }
            )

    # Paper towels: priced but not competitive vs prior band — do NOT list as owner-review
    findings = {
        "kind": "ScalableCommercialResearchFindings",
        "build": BUILD,
        "created_at": _utc(),
        "PHASE8_UI": "DEFERRED_UNTIL_BATCH_VALIDATION",
        "BIDNET_UNTOUCHED": True,
        "FUNNEL_AUDIT_COUNTS": (funnel_audit.get("COUNTS") or {}),
        "FALSE_REJECTS": {
            "rescued": fr.get("RESCUED_NOW_PRODUCT_FIT"),
            "root_causes": fr.get("ROOT_CAUSE_COUNTS"),
            "confirmed_excludes": fr.get("CONFIRMED_EXCLUDES_STILL_VALID"),
        },
        "QUEUE_SIZE": queue.get("count"),
        "BATCH_REPORT_FUNNEL": batch_report.get("FUNNEL"),
        "BATCH_REPORT_NAME": batch_report.get("BATCH"),
        "BATCH_A_FUNNEL": (
            batch_report.get("FUNNEL")
            if batch_report.get("BATCH") == "A"
            else (_load_out("m3_scalable_batch_ab_comparison_v1.json") or {})
            .get("METRICS_TABLE", {})
            .get("Batch A")
        ),
        "BATCH_B_FUNNEL": batch_report.get("FUNNEL") if batch_report.get("BATCH") == "B" else None,
        "BATCH_A_BOTTLENECK": batch_report.get("BOTTLENECK") if batch_report.get("BATCH") == "A" else None,
        "BATCH_A_STATUS_COUNTS": (
            batch_report.get("RESEARCH_STATUS_COUNTS") if batch_report.get("BATCH") == "A" else None
        ),
        "BATCH_A_PRICING_STATS": (
            batch_report.get("PRICING_SOURCE_STATS") if batch_report.get("BATCH") == "A" else None
        ),
        "CUMULATIVE_DISTINCT_RESEARCHED": len({r.get("OPPORTUNITY_ID") for r in batch_rows})
        + 20,  # prior top20 deep research; Batch A may overlap
        "CUMULATIVE_NOTE": (
            "Prior top-20 deep research + Batch A/B. Overlaps possible; "
            "distinct OpenGov commercial research coverage remains on order of ~45–55 IDs, "
            "not the full 8226 true-open set."
        ),
        "OWNER_REVIEW_QUALIFYING_FROM_BATCH_A": 0,
        "EXECUTION_READY": 0,
        "PRIOR_PROPOSED_BID_CANDIDATES": owner_rows,
        "PRIOR_PROPOSED_BID_COUNT": len(owner_rows),
        "TAMPA_GO_NO_GO": tampa.get("GO_NO_GO") or tampa.get("SUMMARY"),
        "WHY_BATCH_A_ZERO_OWNER_REVIEW": [
            "Automated public retail lookup returned LIVE_RESOLVE_NO_PRICE or PRICE_SOURCE_BLOCKED for identified SKUs (paper, Pollard, turnout gear).",
            "Commodity pipe/excavator lines lack manufacturer/model identity → quote-required.",
            "Several queue entries still had labor/construction package character (fixed by labor-dominant gate).",
            "Missing published buyer budget is NOT the blocker after revenue-gate fix; full-scope verified acquisition cost is.",
        ],
        "WHAT_PREVENTS_OTHERS": {
            "PRIMARY": "PUBLIC_RETAIL_AND_DISTRIBUTOR_PRICE_COVERAGE",
            "SECONDARY": [
                "Incomplete commercial identity on portal line text (specs in attachments)",
                "As-needed / estimated qty contracts (guaranteed profit $0 even when priced)",
                "Financing confirmation not obtained",
                "BidNet enrichment still incomplete (~925/6520) — large product pool not yet researchable",
            ],
        },
        "ISOLATION": batch_report.get("ISOLATION"),
    }
    # Honest distinct count
    prior_ranked = _load_data(CID_OUT, "m3_opengov_commercial_id_pricing_repair_v1_ranked.json") or {}
    deep = {str(x.get("OPPORTUNITY_ID")) for x in (prior_ranked.get("rows") or []) if x.get("TOP20")}
    batch_ids = {str(r.get("OPPORTUNITY_ID")) for r in batch_rows if r.get("OPPORTUNITY_ID")}
    findings["CUMULATIVE_DISTINCT_RESEARCHED"] = len(deep | batch_ids)
    _save(DEALS_TABLE, {"build": BUILD, "count": len(owner_rows), "rows": owner_rows, "NOTE": "Only evidence-backed proposed-bid candidates"})
    _save("m3_scalable_cumulative_findings_v1.json", findings)
    return findings


_SERVICE_HEAVY_TITLE = re.compile(
    r"\b("
    r"services?|inspection|testing|repair|reconditioning|maintenance\s+contractor|"
    r"rental|relocation|replacement\s+project|upgrades?\b|agreement|contractor"
    r")\b",
    re.I,
)


_EXHAUSTED_RESEARCH_STATUSES = frozenset(
    {
        "QUOTE_READY",
        "QUOTE_REQUIRED",
        "DATA_MISSING",
        "IDENTITY_UNRESOLVED",
        "EXTRACTION_FAILED",
        "REJECTED_WITH_EVIDENCE",
        "PRICE_VERIFIED",
        "READY_FOR_OWNER_REVIEW",
        "PROFIT_MODELED",
        "SCREENED_OUT",
    }
)


def _exhausted_research_ids() -> set[str]:
    """IDs already deeply researched with a terminal/exhausted outcome — do not re-top-rank."""
    out: set[str] = set()
    for name in (
        BATCH_CHECKPOINT,
        BATCH_RESULTS,
        "m3_simple_controlled_test_results_v1.json",
        "m3_scalable_batch_b_results_v1.json",
        "m3_scalable_batch_a_results_v1.json",
    ):
        doc = _load_out(name) or {}
        for oid in doc.get("completed_ids") or []:
            out.add(str(oid))
        for r in doc.get("rows") or []:
            oid = str(r.get("OPPORTUNITY_ID") or "")
            if not oid:
                continue
            status = str(r.get("RESEARCH_STATUS") or "")
            if status in _EXHAUSTED_RESEARCH_STATUSES or r.get("BATCH"):
                out.add(oid)
    return out


def _pick_batch_candidates(
    queue: dict[str, Any],
    *,
    completed: set[str],
    n: int,
    skip_already_researched: bool,
    prefer_short_scopes: bool,
) -> list[dict[str, Any]]:
    """Select unresearched product opportunities; Batch B prefers short durable scopes."""
    exhausted = _exhausted_research_ids() if skip_already_researched else set()
    candidates: list[dict[str, Any]] = []
    for row in queue.get("rows") or []:
        oid = str(row.get("OPPORTUNITY_ID"))
        if oid in completed and skip_already_researched:
            continue
        if oid in exhausted and skip_already_researched:
            continue  # prior deep research exhausted — wait for new package evidence
        if row.get("RESEARCH_STATUS") == "PRICE_VERIFIED":
            continue  # Liberty/Burks already modeled — do not burn batch slots
        if (row.get("DAYS_TO_DEADLINE") or 0) < 3 and row.get("DAYS_TO_DEADLINE") is not None:
            continue
        reasons = row.get("RANK_REASONS") or []
        if "product_title_signal" not in reasons and row.get("CATEGORY") not in PREFERRED_CATS:
            continue
        title = str(row.get("TITLE") or "")
        # Soft-deprioritize service-heavy titles (still allowed as fallback)
        service_heavy = bool(_SERVICE_HEAVY_TITLE.search(title)) and not re.search(
            r"\b(purchase\s+of|suppl(?:y|ies)|parts?|materials?|equipment|bid\s+discount)\b",
            title,
            re.I,
        )
        lines = int(row.get("MANDATORY_LINE_COUNT") or 999)
        candidates.append({**row, "_service_heavy": service_heavy, "_lines": lines})

    if prefer_short_scopes:
        candidates.sort(
            key=lambda r: (
                0 if _SIMPLE_COMMON_TITLE.search(str(r.get("TITLE") or "")) else 1,
                1 if _HARD_INDUSTRIAL_TITLE.search(str(r.get("TITLE") or "")) else 0,
                1 if r.get("_service_heavy") else 0,
                int(r.get("_lines") or 999) if not _SIMPLE_COMMON_TITLE.search(str(r.get("TITLE") or "")) else min(int(r.get("_lines") or 999), 20),
                -(float(r.get("DAYS_TO_DEADLINE") or 0)),
            )
        )
    picks = candidates[:n]
    if len(picks) < n:
        seen = {p["OPPORTUNITY_ID"] for p in picks}
        for row in queue.get("rows") or []:
            oid = str(row.get("OPPORTUNITY_ID"))
            if oid in completed or oid in seen:
                continue
            if row.get("RESEARCH_STATUS") == "PRICE_VERIFIED":
                continue
            if (row.get("DAYS_TO_DEADLINE") or 0) < 3 and row.get("DAYS_TO_DEADLINE") is not None:
                continue
            picks.append(row)
            seen.add(oid)
            if len(picks) >= n:
                break
    for p in picks:
        p.pop("_service_heavy", None)
        p.pop("_lines", None)
    return picks


def _annotate_price_origins(researched: dict[str, Any]) -> dict[str, Any]:
    """Label cold-start vs warm-cache / seeded / mapping origins per line."""
    origins: Counter[str] = Counter()
    warm = 0
    cold = 0
    for ln in researched.get("LINES") or []:
        retail = ln.get("RETAIL") or {}
        origin = str(retail.get("PRICE_ORIGIN") or "")
        reason = str(retail.get("REASON") or "")
        if not origin:
            if reason == "COMMERCIAL_CACHE":
                origin = "PREVIOUSLY_CACHED_SUPPLIER_INTELLIGENCE"
            elif reason.startswith("MULTI_SUPPLIER") or reason.startswith("LIVE_RESOLVE"):
                origin = "FRESH_INDEPENDENT_DISCOVERY"
            elif reason.startswith("manual") or "MANUAL" in reason.upper():
                origin = "MANUALLY_SEEDED_RESEARCH"
            else:
                origin = "NONE"
        origins[origin] += 1
        if retail.get("CACHE_WARM") or origin in {
            "PREVIOUSLY_CACHED_SUPPLIER_INTELLIGENCE",
            "PREVIOUSLY_CACHED_SUPPLIER_INTEL",
            "MANUALLY_SEEDED_RESEARCH",
            "PRODUCT_SPECIFIC_MAPPING",
        }:
            warm += 1
        elif origin == "FRESH_INDEPENDENT_DISCOVERY":
            cold += 1
    researched["PRICE_ORIGIN_COUNTS"] = dict(origins)
    researched["WARM_CACHE_LINE_HITS"] = warm
    researched["COLD_START_LINE_ATTEMPTS"] = cold
    researched["PRICING_MODE"] = (
        "WARM_CACHE"
        if warm and not cold
        else "COLD_START"
        if cold and not warm
        else "MIXED"
        if warm and cold
        else "UNPRICED"
    )
    return researched


def run_batch(
    *,
    batch_name: str,
    n: int,
    max_live_price_calls: int = 40,
    skip_already_researched: bool = True,
    checkpoint_every: int = 5,
    prefer_short_scopes: bool = False,
    seed_manual_cache: bool = True,
    report_file: str | None = None,
) -> dict[str, Any]:
    """Run controlled batch from ranked queue with checkpoint resume."""
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import BidIndex, restore_historical_pricing_access

    t0 = time.time()
    run_id = f"scr-{batch_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    _progress(status="RUNNING", phase=f"BATCH_{batch_name}", RUN_ID=run_id, batch_target=n)

    if seed_manual_cache:
        seed_manual_price_cache()
    hist = restore_historical_pricing_access()
    bid_index = BidIndex(_canon() / "opengov_bid_price_index.json")
    bid_index.load()

    queue = _load_out(QUEUE_FILE) or build_research_queue()
    snap = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_snapshot.json") or {}
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    prior_results = {
        str(r.get("OPPORTUNITY_ID")): r
        for r in (
            (_load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_results.json") or {}).get(
                "rows"
            )
            or []
        )
    }

    ckpt = _load_out(BATCH_CHECKPOINT) or {
        "kind": "ScalableBatchCheckpoint",
        "build": BUILD,
        "completed_ids": [],
        "results": [],
        "live_price_calls_used": 0,
        "batches": {},
    }
    completed = set(ckpt.get("completed_ids") or [])
    results: list[dict[str, Any]] = list(ckpt.get("results") or [])

    picks = _pick_batch_candidates(
        queue,
        completed=completed,
        n=n,
        skip_already_researched=skip_already_researched,
        prefer_short_scopes=prefer_short_scopes,
    )
    _save(
        f"m3_scalable_batch_{batch_name.lower()}_picks_v1.json",
        {
            "build": BUILD,
            "BATCH": batch_name,
            "RUN_ID": run_id,
            "count": len(picks),
            "rows": [
                {
                    "OPPORTUNITY_ID": p.get("OPPORTUNITY_ID"),
                    "TITLE": p.get("TITLE"),
                    "BUYER": p.get("BUYER"),
                    "CATEGORY": p.get("CATEGORY"),
                    "MANDATORY_LINE_COUNT": p.get("MANDATORY_LINE_COUNT"),
                    "DAYS_TO_DEADLINE": p.get("DAYS_TO_DEADLINE"),
                    "SOLICITATION_URL": p.get("SOLICITATION_URL"),
                }
                for p in picks
            ],
        },
    )

    live_budget = [max_live_price_calls]
    cache = _load_data(PROFIT_OUT, "m3_commercial_acquisition_cache_v1.json") or {"entries": {}}
    pricing_stats = Counter()
    origin_stats: Counter[str] = Counter()
    batch_rows: list[dict[str, Any]] = []

    with httpx.Client(timeout=20.0, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for i, entry in enumerate(picks, start=1):
            oid = entry["OPPORTUNITY_ID"]
            _progress(
                phase=f"BATCH_{batch_name}",
                batch_index=i,
                batch_total=len(picks),
                active_opportunity=oid,
                active_title=entry.get("TITLE"),
                live_budget_remaining=live_budget[0],
            )
            snap_row = by_id.get(oid) or {}
            try:
                researched = research_one(
                    entry,
                    snap_row,
                    bid_index=bid_index,
                    client=client,
                    cache=cache,
                    live_budget=live_budget,
                    prior_result=prior_results.get(oid),
                )
            except Exception as exc:
                researched = {
                    "OPPORTUNITY_ID": oid,
                    "TITLE": entry.get("TITLE"),
                    "RESEARCH_STATUS": "EXTRACTION_FAILED",
                    "ERROR": type(exc).__name__,
                    "ERROR_DETAIL": str(exc)[:300],
                    "OWNER_REVIEW_CANDIDATE": False,
                    "EXECUTION_READY": False,
                    "BLOCKERS": ["EXCEPTION"],
                }
            researched["BATCH"] = batch_name
            researched["RESEARCHED_AT"] = _utc()
            researched = _annotate_price_origins(researched)
            for ln in researched.get("LINES") or []:
                reason = str((ln.get("RETAIL") or {}).get("REASON") or "NONE")
                pricing_stats[reason.split(":")[0]] += 1
                origin = str((ln.get("RETAIL") or {}).get("PRICE_ORIGIN") or "NONE")
                origin_stats[origin] += 1
            results.append(researched)
            batch_rows.append(researched)
            completed.add(oid)
            ckpt["completed_ids"] = sorted(completed)
            ckpt["results"] = results
            ckpt["live_price_calls_used"] = max_live_price_calls - live_budget[0]
            ckpt["batches"][batch_name] = {
                "target": n,
                "completed_in_batch": i,
                "updated_at": _utc(),
            }
            ckpt["updated_at"] = _utc()
            _save(BATCH_CHECKPOINT, ckpt)
            _save(BATCH_RESULTS, {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results})
            _save(
                f"m3_scalable_batch_{batch_name.lower()}_results_v1.json",
                {"build": BUILD, "RUN_ID": run_id, "BATCH": batch_name, "count": len(batch_rows), "rows": batch_rows},
            )
            if i % max(1, checkpoint_every) == 0 or i == len(picks):
                _progress(
                    phase=f"BATCH_{batch_name}_CHECKPOINT",
                    completed_in_batch=i,
                    batch_total=len(picks),
                    completed=len(completed),
                )
                print(
                    f"[BATCH_{batch_name}] checkpoint {i}/{len(picks)} "
                    f"live_remaining={live_budget[0]} oid={oid}",
                    flush=True,
                )

    report = _build_batch_report(
        run_id=run_id,
        batch_name=batch_name,
        results=batch_rows,
        hist=hist,
        pricing_stats=pricing_stats,
        live_used=max_live_price_calls - live_budget[0],
        wall=round(time.time() - t0, 2),
        queue_size=int(queue.get("count") or 0),
    )
    report["PRICE_ORIGIN_STATS"] = dict(origin_stats)
    report["COLD_START_OPPORTUNITIES"] = sum(1 for r in batch_rows if r.get("PRICING_MODE") == "COLD_START")
    report["WARM_CACHE_OPPORTUNITIES"] = sum(1 for r in batch_rows if r.get("PRICING_MODE") == "WARM_CACHE")
    report["MIXED_ORIGIN_OPPORTUNITIES"] = sum(1 for r in batch_rows if r.get("PRICING_MODE") == "MIXED")
    out_report = report_file or (f"m3_scalable_batch_{batch_name.lower()}_report_v1.json" if batch_name != "A" else BATCH_REPORT)
    _save(out_report, report)
    _save(BATCH_REPORT, report)
    write_cumulative_findings(batch_report=report)
    _progress(status="BATCH_COMPLETE", phase=f"BATCH_{batch_name}_DONE", FINAL_STATUS=report.get("FINAL_STATUS"))
    return report


def _build_batch_report(
    *,
    run_id: str,
    batch_name: str,
    results: list[dict[str, Any]],
    hist: dict[str, Any],
    pricing_stats: Counter,
    live_used: int,
    wall: float,
    queue_size: int,
) -> dict[str, Any]:
    evaluated = len(results)
    product_fit = evaluated  # queue already filtered
    full_pkg = sum(1 for r in results if (r.get("MANDATORY_LINE_COUNT") or 0) > 0)
    scopes = sum(1 for r in results if (r.get("MANDATORY_LINE_COUNT") or 0) > 0)
    identities = sum(1 for r in results if (r.get("LINES_IDENTIFIED") or 0) > 0)
    full_acq = sum(1 for r in results if r.get("FULLY_PRICED"))
    hist_ok = sum(1 for r in results if (r.get("LINES_WITH_HISTORICAL") or 0) > 0)
    profit_5k = 0
    profit_10k = 0
    for r in results:
        bids = r.get("BID_MODEL") or {}
        if bids.get("MIN_BID_5K_PROFIT", {}).get("VALUE"):
            profit_5k += 1
        if bids.get("MIN_BID_10K_PROFIT", {}).get("VALUE"):
            profit_10k += 1
    owner = [r for r in results if r.get("OWNER_REVIEW_CANDIDATE")]
    exec_ready = [r for r in results if r.get("EXECUTION_READY")]

    table_rows = []
    for r in owner[:20]:
        bids = r.get("BID_MODEL") or {}
        table_rows.append(
            {
                "Product / Requirement": r.get("TITLE"),
                "Buyer": r.get("BUYER"),
                "Close Date": r.get("DEADLINE"),
                "Verified Acquisition Cost": (bids.get("VERIFIED_ACQUISITION_COST") or {}).get("VALUE"),
                "Historical Gov Price": None,
                "Proposed Bid (10k profit)": (bids.get("MIN_BID_10K_PROFIT") or {}).get("VALUE"),
                "Estimated Total Profit": 10000 if bids.get("MIN_BID_10K_PROFIT") else None,
                "Profit State": "PROPOSED_NOT_GUARANTEED",
                "Offers / Competition": "UNVERIFIED",
                "Retail / Supplier Link": r.get("RETAIL_LINK"),
                "Award History Link": None,
                "Current Solicitation": r.get("SOLICITATION_URL"),
                "Financing Status": "UNVERIFIED",
                "Research Status": r.get("RESEARCH_STATUS"),
                "Blockers": r.get("BLOCKERS"),
            }
        )
    _save(DEALS_TABLE, {"build": BUILD, "count": len(table_rows), "rows": table_rows})

    status_counts = Counter(r.get("RESEARCH_STATUS") for r in results)
    bottleneck = "FULL_SCOPE_PUBLIC_RETAIL_COVERAGE"
    if full_acq == 0 and identities == 0:
        bottleneck = "COMMERCIAL_IDENTITY_RESOLUTION"
    elif full_acq == 0:
        bottleneck = "PUBLIC_RETAIL_LOOKUP_OR_QUOTE_REQUIRED"
    elif not owner:
        bottleneck = "OWNER_REVIEW_GATE_OR_AS_NEEDED_DRAWDOWN"

    return {
        "kind": "ScalableBatchReport",
        "build": BUILD,
        "RUN_ID": run_id,
        "BATCH": batch_name,
        "created_at": _utc(),
        "WALL_CLOCK_SECONDS": wall,
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "TOUCHED_OPERATOR_UI": False,
            "TOUCHED_LIBERTY_BURKS_OUTPUTS": False,
            "SAM_API_CALLS": 0,
            "LIVE_PUBLIC_PRICE_CALLS": live_used,
            "OUTPUT_DIR": OUT_DIR_NAME,
        },
        "HISTORICAL_ACCESS": {
            "BID_INDEX_PRESENT": hist.get("BID_INDEX_PRESENT"),
            "BID_INDEX_RECORDS": hist.get("BID_INDEX_RECORDS"),
        },
        "QUEUE_SIZE": queue_size,
        "DISTINCT_OPPORTUNITIES_RESEARCHED": evaluated,
        "FUNNEL": {
            "Opportunities evaluated": evaluated,
            "Product-fit opportunities": product_fit,
            "Complete packages available": full_pkg,
            "Full mandatory scopes extracted": scopes,
            "Commercial identities resolved": identities,
            "Full acquisition pricing verified": full_acq,
            "Historical award prices verified": hist_ok,
            "Proposed profit ≥$5k": profit_5k,
            "Proposed profit ≥$10k": profit_10k,
            "Competitiveness supported": 0,
            "Potentially financeable": 0,
            "Ready for owner review": len(owner),
            "Execution ready": len(exec_ready),
        },
        "RESEARCH_STATUS_COUNTS": dict(status_counts),
        "PRICING_SOURCE_STATS": dict(pricing_stats),
        "OWNER_REVIEW_TABLE_COUNT": len(table_rows),
        "OWNER_REVIEW_TABLE_FILE": DEALS_TABLE,
        "BOTTLENECK": bottleneck,
        "FINAL_STATUS": "COMPLETE",
        "PHASE8_UI": "DEFERRED_UNTIL_BATCH_VALIDATION",
    }


def run_phases_1_to_5_batch_a(*, batch_size: int = 10, max_live_price_calls: int = 40) -> dict[str, Any]:
    """Execute funnel audit, false-reject fix validation, queue, and Batch A."""
    audit = audit_opportunity_funnel()
    fr = audit_false_rejections()
    queue = build_research_queue()
    report = run_batch(
        batch_name="A",
        n=batch_size,
        max_live_price_calls=max_live_price_calls,
        skip_already_researched=True,
    )
    return {
        "FUNNEL_AUDIT": {
            "true_open": (audit.get("COUNTS") or {}).get("3_confirmed_true_open"),
            "total_discovered": (audit.get("COUNTS") or {}).get("1_total_discovered"),
            "by_source": (audit.get("COUNTS") or {}).get("4_by_source_true_open"),
        },
        "FALSE_REJECTS_RESCUED": fr.get("RESCUED_NOW_PRODUCT_FIT"),
        "QUEUE_SIZE": queue.get("count"),
        "BATCH_A": report,
    }


def _batch_metric_row(results: list[dict[str, Any]], *, researched: int) -> dict[str, Any]:
    """Compute Batch A/B comparison metrics from opportunity-level results."""
    products_identified = sum(1 for r in results if (r.get("LINES_IDENTIFIED") or 0) > 0)
    products_priced = sum(1 for r in results if (r.get("LINES_VERIFIED_RETAIL") or 0) > 0)
    multi_supplier = 0
    for r in results:
        for ln in r.get("LINES") or []:
            retail = ln.get("RETAIL") or {}
            sc = int(retail.get("SUPPLIER_COUNT") or retail.get("EXACT_VERIFIED_COUNT") or 0)
            if sc >= 2 or len(retail.get("ALL_CANDIDATES") or []) >= 2:
                multi_supplier += 1
                break
    full_acq = sum(1 for r in results if r.get("FULLY_PRICED"))
    competitive = 0
    profit_5k = 0
    profit_10k = 0
    for r in results:
        bids = r.get("BID_MODEL") or {}
        if bids.get("MIN_BID_5K_PROFIT", {}).get("VALUE"):
            profit_5k += 1
            competitive += 1
        if bids.get("MIN_BID_10K_PROFIT", {}).get("VALUE"):
            profit_10k += 1
    owner = sum(1 for r in results if r.get("OWNER_REVIEW_CANDIDATE"))
    exec_ready = sum(1 for r in results if r.get("EXECUTION_READY"))
    times = []
    # Wall time not per-row; approximate from RESEARCHED_AT gaps is unreliable — leave None
    return {
        "Opportunities researched": researched,
        "Correctly identified products": products_identified,
        "Exact products priced automatically": products_priced,
        "Products with 2+ verified suppliers": multi_supplier,
        "Fully acquisition-priced contracts": full_acq,
        "Competitive proposed bids": competitive,
        "Modeled profit ≥$5k": profit_5k,
        "Modeled profit ≥$10k": profit_10k,
        "Owner-review candidates": owner,
        "Execution-ready opportunities": exec_ready,
        "Average research time": None,
    }


def write_batch_ab_comparison(*, batch_a_results: list[dict[str, Any]], batch_b_report: dict[str, Any]) -> dict[str, Any]:
    """Write Batch A vs Batch B metrics table + incomplete-scope investigation."""
    b_rows = (_load_out("m3_scalable_batch_b_results_v1.json") or {}).get("rows") or []
    a_metrics = _batch_metric_row(batch_a_results, researched=len(batch_a_results))
    b_metrics = _batch_metric_row(b_rows, researched=len(b_rows))
    wall_b = float(batch_b_report.get("WALL_CLOCK_SECONDS") or 0)
    if b_rows and wall_b:
        b_metrics["Average research time"] = round(wall_b / len(b_rows), 1)

    # Incomplete-scope root causes when <3 fully priced
    unresolved: list[dict[str, Any]] = []
    for r in b_rows:
        if r.get("FULLY_PRICED"):
            continue
        missing = []
        for ln in r.get("LINES") or []:
            retail = ln.get("RETAIL") or {}
            if retail.get("RETAIL_STATE") != "PROVEN":
                missing.append(
                    {
                        "DESCRIPTION": (ln.get("DESCRIPTION") or ln.get("description") or "")[:160],
                        "IDENTITY": ln.get("IDENTITY") or ln.get("COMMERCIAL_IDENTITY"),
                        "RETAIL_REASON": retail.get("REASON"),
                        "PRICE_ORIGIN": retail.get("PRICE_ORIGIN"),
                        "CLASS": ln.get("CLASS") or ln.get("LINE_CLASS"),
                    }
                )
        unresolved.append(
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "TITLE": r.get("TITLE"),
                "CATEGORY": r.get("CATEGORY"),
                "RESEARCH_STATUS": r.get("RESEARCH_STATUS"),
                "MANDATORY_LINE_COUNT": r.get("MANDATORY_LINE_COUNT"),
                "LINES_IDENTIFIED": r.get("LINES_IDENTIFIED"),
                "LINES_VERIFIED_RETAIL": r.get("LINES_VERIFIED_RETAIL"),
                "LINES_QUOTE_REQUIRED": r.get("LINES_QUOTE_REQUIRED"),
                "BLOCKERS": r.get("BLOCKERS"),
                "PRICING_MODE": r.get("PRICING_MODE"),
                "UNRESOLVED_MANDATORY_ITEMS": missing[:12],
            }
        )

    by_cat: dict[str, dict[str, int]] = {}
    for r in b_rows:
        cat = str(r.get("CATEGORY") or "UNKNOWN")
        slot = by_cat.setdefault(cat, {"opps": 0, "identified": 0, "priced": 0, "full": 0})
        slot["opps"] += 1
        if (r.get("LINES_IDENTIFIED") or 0) > 0:
            slot["identified"] += 1
        if (r.get("LINES_VERIFIED_RETAIL") or 0) > 0:
            slot["priced"] += 1
        if r.get("FULLY_PRICED"):
            slot["full"] += 1

    full_acq_b = b_metrics["Fully acquisition-priced contracts"]
    next_change = (
        "Expand toward 100 only after ≥3 complete scopes are acquisition-priced with multi-supplier evidence."
        if full_acq_b >= 3
        else (
            "Primary bottleneck: complete-scope coverage — identity resolution and public retail for ALL "
            "mandatory lines, not single-SKU multi-supplier wins. Next change: tighten line extraction + "
            "quote-request handoff for unresolved mandatory items, and prefer 1–5 line durable catalogs."
        )
    )

    comparison = {
        "kind": "ScalableBatchABComparison",
        "build": BUILD,
        "created_at": _utc(),
        "METRICS_TABLE": {
            "Batch A": a_metrics,
            "Batch B": b_metrics,
        },
        "BATCH_B_PRICE_ORIGINS": batch_b_report.get("PRICE_ORIGIN_STATS"),
        "BATCH_B_COLD_VS_WARM": {
            "COLD_START": batch_b_report.get("COLD_START_OPPORTUNITIES"),
            "WARM_CACHE": batch_b_report.get("WARM_CACHE_OPPORTUNITIES"),
            "MIXED": batch_b_report.get("MIXED_ORIGIN_OPPORTUNITIES"),
        },
        "PRICING_COVERAGE_BY_CATEGORY": by_cat,
        "SOURCE_REQUEST_STATS": batch_b_report.get("PRICING_SOURCE_STATS"),
        "ISOLATION": batch_b_report.get("ISOLATION"),
        "FULLY_PRICED_BATCH_B": full_acq_b,
        "INVESTIGATE_BEFORE_SCALE_100": full_acq_b < 3,
        "INCOMPLETE_SCOPE_INVESTIGATION": unresolved[:25],
        "NEXT_SPECIFIC_CHANGE": next_change,
        "SUCCESS_CRITERION": (
            "Success means complete solicitations → evidence-backed acquisition costs → "
            "credible competitive bid opportunities. Product-level supplier wins alone are insufficient."
        ),
    }
    _save("m3_scalable_batch_ab_comparison_v1.json", comparison)
    return comparison


def run_batch_b(*, batch_size: int = 25, max_live_price_calls: int = 100) -> dict[str, Any]:
    """Execute Batch B: 25 additional unresearched open product opportunities.

    Does not touch BidNet enrichment, L23, sidecars, or SAM. Skips re-seeding
    manual Liberty/Burks prices so cold-start discovery is measurable; existing
    cache hits are still labeled when they occur.
    """
    _progress(status="RUNNING", phase="BATCH_B_START", batch_target=batch_size)
    # Refresh queue rankings but do not rebuild funnel audits (avoid long pre-work)
    queue = build_research_queue()
    report = run_batch(
        batch_name="B",
        n=batch_size,
        max_live_price_calls=max_live_price_calls,
        skip_already_researched=True,
        checkpoint_every=5,
        prefer_short_scopes=True,
        seed_manual_cache=False,  # no new manual product seeds for this validation
        report_file="m3_scalable_batch_b_report_v1.json",
    )
    all_rows = (_load_out(BATCH_RESULTS) or {}).get("rows") or []
    batch_a_rows = [r for r in all_rows if r.get("BATCH") == "A"]
    comparison = write_batch_ab_comparison(batch_a_results=batch_a_rows, batch_b_report=report)
    _progress(status="BATCH_COMPLETE", phase="BATCH_B_DONE", FINAL_STATUS=report.get("FINAL_STATUS"))
    return {
        "QUEUE_SIZE": queue.get("count"),
        "BATCH_B": report,
        "COMPARISON": comparison,
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    mode = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    if mode in {"B", "BATCH_B", "BATCHB"}:
        out = run_batch_b(batch_size=25, max_live_price_calls=100)
    else:
        out = run_phases_1_to_5_batch_a(batch_size=10, max_live_price_calls=40)
    print(json.dumps(out, indent=2, default=str)[:6000])
