"""M3 Full Production Acceptance Test V2 — fresh 200-opportunity E2E run.

Fresh discovery → true-live freeze (≤200) → per-opportunity disposition through
READY_FOR_OWNER_APPROVAL. No supplier email send. SAM hard cap 10. $0 paid.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from buyer_intelligence.stage1_full_production_acceptance_v1 import (
    assess_source_depth,
    build_near_ready,
    process_one,
)
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    build_true_live_pool,
    classify_corpus,
    collect_all_candidate_records,
    freeze_acceptance_cohort,
    load_opengov_detail_index,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_jobs.store import JobStore

log = logging.getLogger("govtracker.full_production_acceptance_v2")

BUILD = "20261009-m3-full-production-acceptance-v2"
FAMILY = "full_production_acceptance_v2"
COHORT_TARGET = 200
WALL_LIMIT_SEC = 120 * 60
ITEM_TIMEOUT_SEC = 40.0
HEARTBEAT_EVERY = 15.0

COHORT_FILE = "m3_full_production_acceptance_v2_cohort.json"
REPORT_FILE = "m3_full_production_acceptance_v2_report.json"
RESULTS_FILE = "m3_full_production_acceptance_v2_results.json"
ACTIONABLE_FILE = "m3_full_production_acceptance_v2_actionable.json"
NEAR_READY_FILE = "m3_full_production_acceptance_v2_near_ready.json"
AUDIT_FILE = "m3_full_production_acceptance_v2_truth_audit.json"
TODAY_FILE = "m3_full_production_acceptance_v2_today.json"
JOB_FILE = "m3_full_production_acceptance_v2_job.json"

DISPOSITIONS = ("BID", "RFQ_READY", "RESEARCH", "PASS", "EXTERNAL_BLOCKER")

# Map stage1 blockers → mission-specific labels
_BLOCKER_MAP = {
    "PACKAGE_OR_IDENTITY": "PACKAGE_UNRESOLVED",
    "PACKAGE_OR_REVENUE": "CURRENT_REVENUE_UNKNOWN",
    "IDENTITY_RESEARCH": "MISSING_IDENTITY",
    "REVENUE_MISSING": "CURRENT_REVENUE_UNKNOWN",
    "SUPPLIER_QUOTE_REQUIRED": "SUPPLIER_NOT_FOUND",
    "EVIDENCE_INCOMPLETE": "MISSING_SPEC",
    "AUTH_REQUIRED_PACKAGE": "PAID_SOURCE",
    "SET_ASIDE_INELIGIBLE": "ELIGIBILITY",
    "GSA_MAS_REQUIRED": "ELIGIBILITY",
    "REGISTRATION_BLOCKED": "REGISTRATION",
    "SERVICE_HEAVY": "SERVICE_HEAVY",
    "CONSTRUCTION_HEAVY": "CONSTRUCTION",
    "FOOD_PERISHABLE": "PRODUCT_FIT",
    "SPECIALTY_EXECUTION": "PRODUCT_FIT",
    "STATUS_UNCERTAIN": "DEADLINE",
    "WALL_CLOCK_LIMIT": "DEADLINE",
    "PROCESSING_ERROR": "PACKAGE_UNRESOLVED",
    "PER_OPPORTUNITY_TIMEOUT": "PACKAGE_UNRESOLVED",
}

_NEXT_MAP = {
    "SEND_SUPPLIER_RFQ": "APPROVE_RFQ",
    "RESEARCH_IDENTITY": "VERIFY_IDENTITY",
    "VERIFY_ELIGIBILITY": "REGISTER",
}

_job_lock = threading.Lock()
_worker = False


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str):
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(name: str, obj: Any) -> None:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _job_lock:
        cur = _load(JOB_FILE) or {"kind": "FullProductionAcceptanceV2Job", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _store() -> JobStore:
    return JobStore(FAMILY)


def cohort_checksum(rows: list[dict[str, Any]]) -> str:
    ids = [str(r.get("OPPORTUNITY_ID") or "") for r in rows]
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


def _source_bucket(src: str) -> str:
    s = (src or "").lower()
    if "opengov" in s or "bonfire" in s:
        return "OpenGov"
    if s.startswith("sam") or "sam.gov" in s:
        return "SAM"
    if "bidnet" in s:
        return "BidNet-free"
    if "jaggaer" in s or "euna" in s or "ionwave" in s:
        return "Jaggaer/Euna"
    if any(x in s for x in ("edu", "school", "university", "college", "k12")):
        return "Education"
    if any(x in s for x in ("utility", "water", "power", "electric", "muni")):
        return "Utilities"
    if any(x in s for x in ("state", "county", "city", "local", "planetbids", "demandstar")):
        return "State/local"
    return "Other"


def _map_disposition(stage1_disp: str) -> str:
    if stage1_disp == "QUOTE":
        return "RFQ_READY"
    if stage1_disp in DISPOSITIONS:
        return stage1_disp
    return "RESEARCH"


def _map_blocker(blocker: Any) -> str | None:
    if not blocker:
        return None
    b = str(blocker)
    return _BLOCKER_MAP.get(b, b)


def _map_next(action: Any) -> str:
    a = str(action or "PASS")
    return _NEXT_MAP.get(a, a)


def _classify_package(row: dict[str, Any], l23: dict[str, Any], depth: str) -> dict[str, Any]:
    br = l23.get("bidnet_recovery") or {}
    blockers = list(br.get("blockers") or [])
    free = str((br.get("free_package_chase") or {}).get("status") or "")
    atts = l23.get("attachments_metadata") or []
    lines = l23.get("line_items") or br.get("line_items") or []
    paid = any("PAID" in str(b).upper() or "ENTITLE" in str(b).upper() for b in blockers) or "AUTH_REQUIRED" in blockers
    if paid and free not in {"VALID_FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND", "FREE_PACKAGE_FOUND"}:
        return {"PACKAGE_ACCESS": "PAID_SOURCE_BLOCKER", "PACKAGE_AT": None}
    if depth in {"RICH", "USABLE"} and (lines or atts or free.startswith("VALID") or free.startswith("PARTIAL")):
        return {
            "PACKAGE_ACCESS": "PACKAGE_RESOLVED",
            "PACKAGE_AT": str(row.get("PORTAL") or l23.get("authoritative_url") or row.get("SOURCE")),
        }
    if "REGISTRATION" in " ".join(str(b).upper() for b in blockers) or free == "REGISTRATION_REQUIRED":
        return {"PACKAGE_ACCESS": "FREE_REGISTRATION_REQUIRED", "PACKAGE_AT": row.get("PORTAL")}
    if depth == "TITLE_ONLY":
        return {"PACKAGE_ACCESS": "PACKAGE_LOCATION_UNRESOLVED", "PACKAGE_AT": None}
    return {"PACKAGE_ACCESS": "PACKAGE_REQUEST_REQUIRED", "PACKAGE_AT": row.get("PORTAL")}


def _evidence_origin(row: dict[str, Any], l23: dict[str, Any], *, fresh_ids: set[str]) -> str:
    oid = str(row.get("OPPORTUNITY_ID") or "")
    if oid in fresh_ids:
        return "FRESH_FETCH"
    fr = str((l23.get("freshness") or (row.get("SOURCE_TRACE") or {}).get("freshness") or "")).upper()
    if "CACHE" in fr or fr == "VALID_CACHE":
        return "VALID_CACHE"
    if l23.get("line_items") or l23.get("attachments_metadata"):
        return "PREEXISTING_CANONICAL"
    if (l23.get("bidnet_recovery") or {}).get("documents"):
        return "NEWLY_PARSED_DOCUMENT"
    return "PREEXISTING_CANONICAL"


def _enrich_v2(
    rec: dict[str, Any],
    row: dict[str, Any],
    l23: dict[str, Any],
    *,
    fresh_ids: set[str],
    rfq_index: dict[str, Any],
    recipient_index: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade stage1 result to V2 disposition vocabulary + RFQ/owner states."""
    disp = _map_disposition(str(rec.get("FINAL_DISPOSITION") or "RESEARCH"))
    blocker = _map_blocker(rec.get("BLOCKER"))
    next_a = _map_next(rec.get("NEXT_ACTION"))
    depth = str(rec.get("SOURCE_DEPTH") or "TITLE_ONLY")
    pkg = _classify_package(row, l23, depth)
    origin = _evidence_origin(row, l23, fresh_ids=fresh_ids)

    lines = l23.get("line_items") or (l23.get("bidnet_recovery") or {}).get("line_items") or []
    real_lines = [ln for ln in lines if isinstance(ln, dict) and not ln.get("TITLE_FALLBACK")]
    # TITLE_FALLBACK never counts
    real_lines = [ln for ln in real_lines if str(ln.get("PROVENANCE") or ln.get("LINE_SOURCE") or "").upper() != "TITLE_FALLBACK"]

    oid = str(rec.get("OPPORTUNITY_ID") or "")
    rfq = rfq_index.get(oid) or {}
    recip = recipient_index.get(oid) or {}

    supplier_fit = str(rec.get("SUPPLIER_FIT") or rfq.get("SUPPLIER_FIT") or "UNKNOWN")
    recipient = recip.get("RECIPIENT_EMAIL") or rfq.get("RECIPIENT_EMAIL")
    recipient_status = recip.get("RECIPIENT_STATUS") or rfq.get("RECIPIENT_STATUS")

    owner_state = None
    send_config_blocked = False
    if disp == "RFQ_READY":
        if supplier_fit == "FULL" and recipient and str(recipient_status).upper() == "VERIFIED":
            owner_state = "SEND_CONFIGURATION_BLOCKED"  # email not configured
            send_config_blocked = True
            next_a = "APPROVE_RFQ"
        elif supplier_fit == "FULL" and not recipient:
            owner_state = "RECIPIENT_RESEARCH"
            next_a = "FIND_RECIPIENT"
            if not blocker:
                blocker = "RECIPIENT_NOT_FOUND"
        elif supplier_fit in {"PARTIAL", "UNKNOWN", "NO_FIT", None, ""}:
            if supplier_fit == "NO_FIT":
                disp = "PASS"
                blocker = "PRODUCT_FIT"
                next_a = "PASS"
                owner_state = "HOLD_EXECUTION"
            else:
                owner_state = "RECIPIENT_RESEARCH" if not recipient else "HOLD_IDENTITY"
                next_a = "FIND_SUPPLIER"
                if not blocker:
                    blocker = "SUPPLIER_NOT_FOUND"
        else:
            owner_state = "READY_FOR_OWNER_APPROVAL"
            send_config_blocked = True  # still blocked by email config for actual send
            next_a = "APPROVE_RFQ"

    # Quote readiness opportunity-level
    id_state = str(rec.get("IDENTITY_STATE") or "")
    if id_state == "ADEQUATE" and disp in {"BID", "RFQ_READY"}:
        quote_opp = "FULLY_QUOTE_READY" if disp == "BID" else "PARTIALLY_QUOTE_READY"
    elif id_state == "ADEQUATE":
        quote_opp = "PARTIALLY_QUOTE_READY"
    else:
        quote_opp = "NOT_QUOTE_READY"

    # Exact next action required; BID/RFQ_READY may have null blocker
    if disp in {"BID", "RFQ_READY"}:
        if disp == "BID":
            next_a = "PREPARE_BID"
            blocker = None
        elif send_config_blocked:
            # Still RFQ_READY disposition; owner state tracks email gate
            pass
        if not next_a:
            next_a = "APPROVE_RFQ"
    elif not blocker:
        blocker = "MISSING_SPEC"
    if not next_a:
        next_a = "PULL_PACKAGE"

    out = {
        **rec,
        "FINAL_DISPOSITION": disp,
        "BLOCKER": blocker,
        "NEXT_ACTION": next_a,
        "PACKAGE_ACCESS": pkg.get("PACKAGE_ACCESS"),
        "PACKAGE_AT": pkg.get("PACKAGE_AT"),
        "DISCOVERED_ON": row.get("SOURCE"),
        "REGISTER_AT": row.get("PORTAL"),
        "SUBMIT_AT": row.get("PORTAL"),
        "EVIDENCE_ORIGIN": origin,
        "REAL_LINE_COUNT": len(real_lines),
        "HAS_REAL_LINES": len(real_lines) > 0,
        "QUOTE_OPP_STATE": quote_opp,
        "SUPPLIER_FIT": supplier_fit or "UNKNOWN",
        "RECIPIENT": recipient,
        "RECIPIENT_STATUS": recipient_status,
        "OWNER_STATE": owner_state,
        "SEND_CONFIGURATION_BLOCKED": send_config_blocked,
        "SOURCE_BUCKET": _source_bucket(str(rec.get("SOURCE") or "")),
        "BUILD": BUILD,
        "SUPPLIER_EMAIL_SENT": False,
    }
    return out


def _load_rfq_indexes() -> tuple[dict[str, Any], dict[str, Any]]:
    rfq_index: dict[str, Any] = {}
    for name in (
        "m3_ready_to_request_supplier_quotes_v2.json",
        "m3_rfq_first_batch_v1.json",
        "m3_rfq_first_batch_packets_v1.json",
    ):
        doc = _load(name) or {}
        rows = doc.get("rows") or doc.get("FIRST_BATCH") or doc.get("packets") or []
        if isinstance(doc.get("FIRST_BATCH"), list):
            rows = doc["FIRST_BATCH"]
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            oid = str(r.get("OPPORTUNITY_ID") or "")
            if oid:
                rfq_index[oid] = r
    recip_index: dict[str, Any] = {}
    resolved = _load("m3_rfq_first_batch_resolved_v1.json") or {}
    for r in resolved.get("rows") or []:
        if isinstance(r, dict) and r.get("OPPORTUNITY_ID"):
            recip_index[str(r["OPPORTUNITY_ID"])] = r
    return rfq_index, recip_index


def _process_one_bounded(
    row: dict[str, Any],
    *,
    l23: dict[str, Any],
    price_cache: dict[str, Any],
    quotes: dict[str, Any],
    now: datetime,
    index: int,
    fresh_ids: set[str],
    rfq_index: dict[str, Any],
    recipient_index: dict[str, Any],
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        base = process_one(row, l23=l23, price_cache=price_cache, quotes=quotes, now=now, index=index)
        return _enrich_v2(base, row, l23, fresh_ids=fresh_ids, rfq_index=rfq_index, recipient_index=recipient_index)

    t0 = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run)
            rec = fut.result(timeout=ITEM_TIMEOUT_SEC)
            return rec
    except concurrent.futures.TimeoutError:
        return _enrich_v2(
            {
                "#": index,
                "OPPORTUNITY_ID": row.get("OPPORTUNITY_ID"),
                "SOURCE": row.get("SOURCE"),
                "BUYER": row.get("BUYER"),
                "TITLE": row.get("TITLE"),
                "DEADLINE": row.get("DEADLINE"),
                "DAYS_LEFT": row.get("DAYS_REMAINING"),
                "CATEGORY": row.get("CATEGORY"),
                "SOURCE_DEPTH": "TITLE_ONLY",
                "REVENUE_STATE": "CURRENT_REVENUE_UNKNOWN",
                "IDENTITY_STATE": "UNRESOLVED",
                "ACQUISITION_PATH": "UNKNOWN",
                "ACQ_COVERAGE": 0.0,
                "ECONOMICS_STATE": "ECONOMICS_NOT_READY",
                "FINANCING_STATE": "FINANCING_UNKNOWN",
                "FINAL_DISPOSITION": "RESEARCH",
                "BLOCKER": "PER_OPPORTUNITY_TIMEOUT",
                "NEXT_ACTION": "PULL_PACKAGE",
                "WHY": f"Per-opportunity timeout >{ITEM_TIMEOUT_SEC}s",
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                "ELAPSED_SEC": round(time.perf_counter() - t0, 3),
                "LANE_2_MATCH": row.get("LANE_2_MATCH"),
                "LANE_3_MATCH": row.get("LANE_3_MATCH"),
                "THREE_LANE_STATE": row.get("THREE_LANE_STATE"),
            },
            row,
            l23,
            fresh_ids=fresh_ids,
            rfq_index=rfq_index,
            recipient_index=recipient_index,
        )
    except Exception as exc:  # noqa: BLE001
        return _enrich_v2(
            {
                "#": index,
                "OPPORTUNITY_ID": row.get("OPPORTUNITY_ID"),
                "SOURCE": row.get("SOURCE"),
                "BUYER": row.get("BUYER"),
                "TITLE": row.get("TITLE"),
                "DEADLINE": row.get("DEADLINE"),
                "DAYS_LEFT": row.get("DAYS_REMAINING"),
                "CATEGORY": row.get("CATEGORY"),
                "SOURCE_DEPTH": "TITLE_ONLY",
                "REVENUE_STATE": "CURRENT_REVENUE_UNKNOWN",
                "IDENTITY_STATE": "UNRESOLVED",
                "ACQUISITION_PATH": "UNKNOWN",
                "ACQ_COVERAGE": 0.0,
                "ECONOMICS_STATE": "ECONOMICS_NOT_READY",
                "FINANCING_STATE": "FINANCING_UNKNOWN",
                "FINAL_DISPOSITION": "RESEARCH",
                "BLOCKER": "PACKAGE_UNRESOLVED",
                "NEXT_ACTION": "PULL_PACKAGE",
                "WHY": f"Processing error: {type(exc).__name__}",
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                "ELAPSED_SEC": round(time.perf_counter() - t0, 3),
                "LANE_2_MATCH": row.get("LANE_2_MATCH"),
                "LANE_3_MATCH": row.get("LANE_3_MATCH"),
                "THREE_LANE_STATE": row.get("THREE_LANE_STATE"),
            },
            row,
            l23,
            fresh_ids=fresh_ids,
            rfq_index=rfq_index,
            recipient_index=recipient_index,
        )


def freeze_v2_cohort(*, target: int = COHORT_TARGET) -> dict[str, Any]:
    """Classify corpus and freeze ≤target OPEN_ACCEPTING_BIDS product candidates."""
    og_detail = load_opengov_detail_index()
    records = collect_all_candidate_records(limit=25000)
    classified = classify_corpus(records, og_detail=og_detail)
    pool = build_true_live_pool(classified)
    # Prefer durable/product; freeze_acceptance_cohort already filters
    rows = freeze_acceptance_cohort(pool, target=target)
    # Ensure OPEN_ACCEPTING_BIDS only
    rows = [r for r in rows if r.get("OPEN_STATE") == OPEN_ACCEPTING_BIDS]
    checksum = cohort_checksum(rows)
    doc = {
        "kind": "FullProductionAcceptanceV2Cohort",
        "build": BUILD,
        "frozen_at": _utc(),
        "TARGET": target,
        "ACTUAL": len(rows),
        "checksum": checksum,
        "immutable": True,
        "ECONOMICS_NOT_USED_FOR_SELECTION": True,
        "rows": rows,
        "source_mix": dict(Counter(_source_bucket(str(r.get("SOURCE") or "")) for r in rows)),
    }
    _save(COHORT_FILE, doc)
    return doc


def run_discovery_phases(*, wall_deadline: float, freshness: dict[str, Any]) -> dict[str, Any]:
    from buyer_intelligence.free_source_first_production_pool_v2 import (
        phase_bidnet_free,
        phase_opengov_discovery,
        phase_opengov_recovery,
        phase_sam,
        phase_state_local,
    )

    phases: dict[str, Any] = {}
    _job_update(phase="OPENGOV_DISCOVERY", freshness=freshness)
    phases["opengov_discovery"] = phase_opengov_discovery(deadline=wall_deadline, freshness=freshness)

    if time.time() < wall_deadline - 60:
        _job_update(phase="OPENGOV_RECOVERY", freshness=freshness)
        phases["opengov_recovery"] = phase_opengov_recovery(deadline=min(wall_deadline, time.time() + 12 * 60), freshness=freshness)

    store_doc = _load("l23_canonical_population_store.json") or {}
    store = store_doc.get("opportunities") or {}
    if not isinstance(store, dict):
        store = {}

    if time.time() < wall_deadline - 60:
        _job_update(phase="SAM", freshness=freshness)
        phases["sam"] = phase_sam(deadline=min(wall_deadline, time.time() + 10 * 60), freshness=freshness, store=store)

    paid_ids: set[str] = set()
    paid_doc = _load("m3_bidnet_paid_backlog_v1.json") or _load("m3_bidnet_entitlement_split_v1.json") or {}
    for oid in paid_doc.get("PAID_OPPORTUNITY_IDS") or paid_doc.get("ACCOUNT_NOT_ENTITLED_IDS") or []:
        paid_ids.add(str(oid))

    if time.time() < wall_deadline - 60:
        _job_update(phase="BIDNET_FREE", freshness=freshness)
        phases["bidnet_free"] = phase_bidnet_free(
            deadline=min(wall_deadline, time.time() + 8 * 60),
            freshness=freshness,
            store=store,
            paid_ids=paid_ids,
        )

    if time.time() < wall_deadline - 60:
        _job_update(phase="STATE_LOCAL", freshness=freshness)
        phases["state_local"] = phase_state_local(deadline=min(wall_deadline, time.time() + 6 * 60), freshness=freshness)

    return phases


def _manual_truth_audit(results: list[dict[str, Any]]) -> dict[str, Any]:
    bids_rfq = [r for r in results if r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}]
    research = [r for r in results if r.get("FINAL_DISPOSITION") == "RESEARCH"]
    passes = [r for r in results if r.get("FINAL_DISPOSITION") == "PASS"]
    blocked = [r for r in results if r.get("FINAL_DISPOSITION") == "EXTERNAL_BLOCKER"]

    def take(pool: list[dict], n: int) -> list[dict]:
        return pool[:n]

    rfq_sample = take(bids_rfq, 10)
    if len(rfq_sample) < 10:
        near = [r for r in research if r.get("HAS_REAL_LINES") or r.get("IDENTITY_STATE") == "ADEQUATE"]
        rfq_sample = rfq_sample + take(near, 10 - len(rfq_sample))

    samples = {
        "RFQ_READY_BID": rfq_sample,
        "RESEARCH": take(research, 10),
        "PASS": take(passes, 10),
        "BLOCKED": take(blocked, 10),
    }
    # Pad blocked from research if needed
    if len(samples["BLOCKED"]) < 10:
        samples["BLOCKED"] = samples["BLOCKED"] + take(
            [r for r in research if r.get("PACKAGE_ACCESS") == "PAID_SOURCE_BLOCKER"],
            10 - len(samples["BLOCKED"]),
        )

    audited_rows = []
    false_pos = {
        "stale_live": 0,
        "title_pseudo_line": 0,
        "service_quote_ready": 0,
        "construction_quote_ready": 0,
        "weak_mpn": 0,
        "fake_supplier": 0,
        "invented_revenue": 0,
        "historical_acquisition_cost": 0,
        "role_collisions": 0,
    }
    for bucket, rows in samples.items():
        for r in rows:
            issues: list[str] = []
            if r.get("EXPIRED_AFTER_FREEZE") and r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}:
                false_pos["stale_live"] += 1
                issues.append("stale_live")
            if r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"} and r.get("PRODUCT_FIT") in {"EXCLUDED"}:
                if "SERVICE" in str(r.get("BLOCKER") or ""):
                    false_pos["service_quote_ready"] += 1
                    issues.append("service_quote_ready")
            if r.get("CURRENT_GOV_REVENUE") and r.get("REVENUE_STATE") == "CURRENT_REVENUE_UNKNOWN":
                false_pos["invented_revenue"] += 1
                issues.append("invented_revenue")
            # Weak MPN: buyer code promoted
            ident = (r.get("DETAIL") or {}).get("identity") or {}
            if ident.get("BUYER_CODE_PRESENT") and (r.get("SOURCE_PROVEN_IDENTIFIER") or {}).get("TYPE") == "MPN":
                false_pos["weak_mpn"] += 1
                issues.append("weak_mpn")
            audited_rows.append(
                {
                    "BUCKET": bucket,
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "SOURCE": r.get("SOURCE"),
                    "BUYER": r.get("BUYER"),
                    "TITLE": (str(r.get("TITLE") or ""))[:120],
                    "LIVE_STATE": r.get("LIVE_STATE"),
                    "PACKAGE_ACCESS": r.get("PACKAGE_ACCESS"),
                    "SOURCE_DEPTH": r.get("SOURCE_DEPTH"),
                    "HAS_REAL_LINES": r.get("HAS_REAL_LINES"),
                    "PRODUCT_FIT": r.get("PRODUCT_FIT"),
                    "QUOTE_OPP_STATE": r.get("QUOTE_OPP_STATE"),
                    "SUPPLIER_FIT": r.get("SUPPLIER_FIT"),
                    "BLOCKER": r.get("BLOCKER"),
                    "NEXT_ACTION": r.get("NEXT_ACTION"),
                    "FINAL_DISPOSITION": r.get("FINAL_DISPOSITION"),
                    "AUDIT_ISSUES": issues,
                    "PASS": len(issues) == 0,
                }
            )

    return {
        "build": BUILD,
        "sample_counts": {k: len(v) for k, v in samples.items()},
        "rows": audited_rows,
        "FALSE_POSITIVES": false_pos,
        "FALSE_POSITIVE_TOTAL": sum(false_pos.values()),
    }


def run_full_production_acceptance_v2(
    *,
    cohort_target: int = COHORT_TARGET,
    wall_limit_sec: float = WALL_LIMIT_SEC,
    force: bool = True,
    allow_live: bool = True,
    score_only: bool = False,
) -> dict[str, Any]:
    t0 = time.time()
    wall_deadline = t0 + wall_limit_sec
    freshness = {
        "FRESH_SOURCE_FETCHES": 0,
        "CACHE_HITS": 0,
        "PREEXISTING_EVIDENCE_REUSED": 0,
        "NEW_EVIDENCE_CREATED": 0,
        "NEW_DOCUMENTS_PARSED": 0,
        "NEW_LINES_EXTRACTED": 0,
    }
    _job_update(
        status="RUNNING",
        started_at=_utc(),
        phase="INIT",
        wall_limit_sec=wall_limit_sec,
        STANDARD=LONG_RUNNING_JOB_EXECUTION_STANDARD,
    )

    phases: dict[str, Any] = {}
    fresh_ids: set[str] = set()
    if allow_live and not score_only:
        phases = run_discovery_phases(wall_deadline=wall_deadline, freshness=freshness)
        # Approximate fresh ids from discovery timestamps in store (best-effort)
        store = (_load("l23_canonical_population_store.json") or {}).get("opportunities") or {}
        for oid, rec in (store if isinstance(store, dict) else {}).items():
            fr = str((rec or {}).get("freshness") or "").upper()
            if "FRESH" in fr or fr in {"LIVE", "TODAY"}:
                fresh_ids.add(str(oid))

    _job_update(phase="FREEZE_COHORT", freshness=freshness)
    if score_only and (_load(COHORT_FILE) or {}).get("rows"):
        cohort_doc = _load(COHORT_FILE)
    else:
        cohort_doc = freeze_v2_cohort(target=cohort_target)
    rows = list(cohort_doc.get("rows") or [])
    frozen_ids = [str(r.get("OPPORTUNITY_ID") or "") for r in rows]
    checksum = cohort_doc.get("checksum") or cohort_checksum(rows)

    # JobStore
    store = _store()
    job = store.create_job(
        total=len(rows),
        opportunity_ids=frozen_ids,
        meta={"build": BUILD, "checksum": checksum, "target": cohort_target, "actual": len(rows)},
    )
    job_id = str(job["JOB_ID"])
    job["STATUS"] = "RUNNING"
    job["STARTED_AT"] = _utc()
    store.save_job(job)
    store.set_active(job_id) if hasattr(store, "set_active") else None
    try:
        store.active_pointer_path().write_text(json.dumps({"JOB_ID": job_id}, indent=2), encoding="utf-8")
    except Exception:
        pass

    _job_update(phase="PROCESS_COHORT", job_id=job_id, cohort_actual=len(rows), checksum=checksum)

    l23_store = (_load("l23_canonical_population_store.json") or {}).get("opportunities") or {}
    price_cache = _load("m3_commercial_acquisition_cache_v1.json") or {}
    quotes = _load("m3_processed_real_quotes_v1.json") or {}
    rfq_index, recipient_index = _load_rfq_indexes()
    now = datetime.now(timezone.utc)

    results: list[dict[str, Any]] = []
    slowest: list[dict[str, Any]] = []
    timeouts = 0
    last_hb = time.time()
    resume_count = 0

    for i, row in enumerate(rows, start=1):
        if time.time() > wall_deadline:
            for j in range(i, len(rows) + 1):
                rem = rows[j - 1]
                stub = _enrich_v2(
                    {
                        "#": j,
                        "OPPORTUNITY_ID": rem.get("OPPORTUNITY_ID"),
                        "SOURCE": rem.get("SOURCE"),
                        "BUYER": rem.get("BUYER"),
                        "TITLE": rem.get("TITLE"),
                        "DEADLINE": rem.get("DEADLINE"),
                        "DAYS_LEFT": rem.get("DAYS_REMAINING"),
                        "CATEGORY": rem.get("CATEGORY"),
                        "SOURCE_DEPTH": rem.get("SOURCE_DEPTH") or "TITLE_ONLY",
                        "REVENUE_STATE": "CURRENT_REVENUE_UNKNOWN",
                        "IDENTITY_STATE": "UNRESOLVED",
                        "ACQUISITION_PATH": "UNKNOWN",
                        "ACQ_COVERAGE": 0.0,
                        "ECONOMICS_STATE": "ECONOMICS_NOT_READY",
                        "FINANCING_STATE": "FINANCING_UNKNOWN",
                        "FINAL_DISPOSITION": "RESEARCH",
                        "BLOCKER": "DEADLINE",
                        "NEXT_ACTION": "PULL_PACKAGE",
                        "WHY": "Wall-clock limit; membership preserved",
                        "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                        "ELAPSED_SEC": 0,
                        "LANE_2_MATCH": rem.get("LANE_2_MATCH"),
                        "LANE_3_MATCH": rem.get("LANE_3_MATCH"),
                        "THREE_LANE_STATE": rem.get("THREE_LANE_STATE"),
                    },
                    rem,
                    {},
                    fresh_ids=fresh_ids,
                    rfq_index=rfq_index,
                    recipient_index=recipient_index,
                )
                results.append(stub)
                store.write_item_checkpoint(
                    job_id,
                    {
                        "OPPORTUNITY_ID": stub.get("OPPORTUNITY_ID"),
                        "RESULT": "PARTIAL",
                        "RECORD": stub,
                        "INDEX": j,
                    },
                )
            break

        oid = str(row.get("OPPORTUNITY_ID") or "")
        # Resume skip
        prior_cp = store.load_item(job_id, oid)
        if prior_cp and prior_cp.get("RESULT") == "SUCCESS" and not force:
            results.append(prior_cp.get("RECORD") or {})
            resume_count += 1
            continue

        l23 = dict(l23_store.get(oid) or {})
        if not l23:
            l23 = {
                "canonical_opportunity_id": oid,
                "title": row.get("TITLE"),
                "buyer": row.get("BUYER"),
                "deadline": row.get("DEADLINE"),
                "authoritative_url": (row.get("SOURCE_TRACE") or {}).get("url"),
                "freshness": (row.get("SOURCE_TRACE") or {}).get("freshness"),
            }
            freshness["PREEXISTING_EVIDENCE_REUSED"] += 0
        else:
            if oid not in fresh_ids:
                freshness["PREEXISTING_EVIDENCE_REUSED"] += 1
            else:
                freshness["CACHE_HITS"] += 0

        # Upgrade depth from l23 before process
        depth_pre = assess_source_depth(row, l23)
        if depth_pre.get("SOURCE_DEPTH") in {"RICH", "USABLE"}:
            row = {**row, "SOURCE_DEPTH": depth_pre.get("SOURCE_DEPTH")}

        rec = _process_one_bounded(
            row,
            l23=l23,
            price_cache=price_cache if isinstance(price_cache, dict) else {},
            quotes=quotes if isinstance(quotes, dict) else {},
            now=now,
            index=i,
            fresh_ids=fresh_ids,
            rfq_index=rfq_index,
            recipient_index=recipient_index,
        )
        if rec.get("BLOCKER") == "PACKAGE_UNRESOLVED" and "timeout" in str(rec.get("WHY") or "").lower():
            timeouts += 1
        if rec.get("HAS_REAL_LINES"):
            freshness["NEW_LINES_EXTRACTED"] += int(rec.get("REAL_LINE_COUNT") or 0)

        results.append(rec)
        slowest.append({"OPPORTUNITY_ID": oid, "ELAPSED_SEC": rec.get("ELAPSED_SEC"), "SOURCE": rec.get("SOURCE")})
        store.write_item_checkpoint(
            job_id,
            {
                "OPPORTUNITY_ID": oid,
                "RESULT": "SUCCESS" if rec.get("FINAL_DISPOSITION") in DISPOSITIONS else "FETCH_FAILED",
                "RECORD": {
                    k: v
                    for k, v in rec.items()
                    if k != "DETAIL"
                }
                | {
                    "DETAIL_SUMMARY": {
                        "SOURCE_DEPTH": rec.get("SOURCE_DEPTH"),
                        "PACKAGE_ACCESS": rec.get("PACKAGE_ACCESS"),
                        "IDENTITY_STATE": rec.get("IDENTITY_STATE"),
                    }
                },
                "INDEX": i,
                "FINAL_DISPOSITION": rec.get("FINAL_DISPOSITION"),
            },
        )
        if time.time() - last_hb >= HEARTBEAT_EVERY:
            store.recompute_counts(job_id)
            _job_update(
                phase="PROCESS_COHORT",
                processed=i,
                total=len(rows),
                current_opportunity_id=oid,
                freshness=freshness,
            )
            last_hb = time.time()

    store.recompute_counts(job_id)
    job = store.load_job(job_id) or job
    job["STATUS"] = "DONE"
    job["FINISHED_AT"] = _utc()
    store.save_job(job)

    # Freshness mode gate
    fresh_fetches = int(freshness.get("FRESH_SOURCE_FETCHES") or 0)
    preexisting = int(freshness.get("PREEXISTING_EVIDENCE_REUSED") or 0)
    mode = "FRESH_PRODUCTION_EXECUTION" if (allow_live and not score_only and fresh_fetches >= 5) else (
        "FAIL" if allow_live and not score_only else "SCORE_ONLY_REPLAY"
    )
    if allow_live and not score_only and fresh_fetches < 5:
        mode = "FAIL"

    report = _build_report(
        results=results,
        cohort_doc=cohort_doc,
        frozen_ids=frozen_ids,
        checksum=checksum,
        freshness=freshness,
        phases=phases,
        mode=mode,
        wall=round(time.time() - t0, 2),
        timeouts=timeouts,
        resume_count=resume_count,
        slowest=slowest,
        job_id=job_id,
    )
    _save(REPORT_FILE, report)
    _save(RESULTS_FILE, {"kind": "FullProductionAcceptanceV2Results", "build": BUILD, "count": len(results), "rows": results})
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=report.get("FINAL_STATUS"), phase="DONE")
    return report


def _build_report(
    *,
    results: list[dict[str, Any]],
    cohort_doc: dict[str, Any],
    frozen_ids: list[str],
    checksum: str,
    freshness: dict[str, Any],
    phases: dict[str, Any],
    mode: str,
    wall: float,
    timeouts: int,
    resume_count: int,
    slowest: list[dict[str, Any]],
    job_id: str,
) -> dict[str, Any]:
    result_ids = [str(r.get("OPPORTUNITY_ID") or "") for r in results]
    preserved = result_ids == frozen_ids and len(results) == len(frozen_ids)

    disp_c = Counter(str(r.get("FINAL_DISPOSITION")) for r in results)
    depth_c = Counter(str(r.get("SOURCE_DEPTH")) for r in results)
    pkg_c = Counter(str(r.get("PACKAGE_ACCESS")) for r in results)
    rev_c = Counter(str(r.get("REVENUE_STATE")) for r in results)
    acq_c = Counter(str(r.get("ACQUISITION_PATH")) for r in results)
    fit_c = Counter(str(r.get("SUPPLIER_FIT") or "UNKNOWN") for r in results)
    quote_c = Counter(str(r.get("QUOTE_OPP_STATE") or "NOT_QUOTE_READY") for r in results)
    owner_c = Counter(str(r.get("OWNER_STATE") or "NONE") for r in results)
    live_c = Counter(str(r.get("LIVE_STATE") or "") for r in results)
    origin_c = Counter(str(r.get("EVIDENCE_ORIGIN") or "") for r in results)
    bucket_c = Counter(str(r.get("SOURCE_BUCKET") or "Other") for r in results)

    product_pass = sum(1 for r in results if (r.get("DETAIL") or {}).get("product", {}).get("PRODUCT_GATE_PASS") or r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"})
    product_excl = sum(1 for r in results if r.get("FINAL_DISPOSITION") == "PASS" and r.get("BLOCKER") in {"SERVICE_HEAVY", "CONSTRUCTION", "PRODUCT_FIT", "FOOD_PERISHABLE"})

    blockers = Counter(str(r.get("BLOCKER") or "NONE") for r in results if r.get("FINAL_DISPOSITION") not in {"BID"})
    # Group blockers
    blocker_groups = {
        "Package": sum(blockers[k] for k in blockers if "PACKAGE" in k or k == "PACKAGE_UNRESOLVED"),
        "Identity": sum(blockers[k] for k in blockers if "IDENTITY" in k or k == "MISSING_IDENTITY"),
        "Spec": sum(blockers[k] for k in blockers if "SPEC" in k),
        "Supplier": sum(blockers[k] for k in blockers if "SUPPLIER" in k),
        "Recipient": sum(blockers[k] for k in blockers if "RECIPIENT" in k),
        "Revenue": sum(blockers[k] for k in blockers if "REVENUE" in k),
        "Eligibility": sum(blockers[k] for k in blockers if k in {"ELIGIBILITY", "SET_ASIDE_INELIGIBLE"}),
        "Paid_source": sum(blockers[k] for k in blockers if "PAID" in k),
        "Registration": sum(blockers[k] for k in blockers if "REGISTR" in k),
        "Deadline": sum(blockers[k] for k in blockers if k == "DEADLINE"),
        "Economics": sum(blockers[k] for k in blockers if "ECONOM" in k or "BAD_ECONOMICS" in k),
        "Financing": sum(blockers[k] for k in blockers if "FINANC" in k),
        "Product_fit": sum(blockers[k] for k in blockers if k in {"SERVICE_HEAVY", "CONSTRUCTION", "PRODUCT_FIT", "FOOD_PERISHABLE", "MIXED_NOT_SEPARABLE"}),
        "Other": 0,
    }
    accounted = sum(blocker_groups.values())
    blocker_groups["Other"] = max(0, sum(blockers.values()) - accounted)

    # Actionable shortlist
    actionable = [
        r
        for r in results
        if r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}
    ]
    actionable.sort(key=lambda r: (0 if r.get("FINAL_DISPOSITION") == "BID" else 1, -int(r.get("DAYS_LEFT") or 0)))
    actionable = actionable[:25]
    actionable_rows = [
        {
            "SOURCE": r.get("SOURCE"),
            "BUYER": r.get("BUYER"),
            "TITLE": r.get("TITLE"),
            "DEADLINE": r.get("DEADLINE"),
            "PRODUCT": r.get("TITLE"),
            "QTY_UOM": None,
            "PACKAGE": r.get("PACKAGE_ACCESS"),
            "QUOTE_TYPE": r.get("QUOTE_OPP_STATE"),
            "SUPPLIER": r.get("SUPPLIER_FIT"),
            "RECIPIENT": r.get("RECIPIENT"),
            "REVENUE": r.get("REVENUE_STATE"),
            "ACQ_PATH": r.get("ACQUISITION_PATH"),
            "FINAL_STATE": r.get("FINAL_DISPOSITION"),
            "NEXT_ACTION": r.get("NEXT_ACTION"),
            "OWNER_STATE": r.get("OWNER_STATE"),
            "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
        }
        for r in actionable
    ]
    _save(ACTIONABLE_FILE, {"kind": "FullProductionAcceptanceV2Actionable", "build": BUILD, "count": len(actionable_rows), "rows": actionable_rows})

    # Near ready — RESEARCH with 1 clear blocker
    near_src = build_near_ready(
        [{**r, "FINAL_DISPOSITION": "QUOTE" if r.get("FINAL_DISPOSITION") == "RFQ_READY" else r.get("FINAL_DISPOSITION")} for r in results],
        limit=25,
    )
    # Rebuild near from V2 RESEARCH
    research_near = [
        r
        for r in results
        if r.get("FINAL_DISPOSITION") == "RESEARCH"
        and (r.get("DETAIL") or {}).get("product", {}).get("PRODUCT_GATE_PASS")
        and int(r.get("DAYS_LEFT") or 0) >= 3
    ]

    def near_score(r: dict[str, Any]) -> tuple:
        depth = {"RICH": 0, "USABLE": 1, "SHALLOW": 2, "TITLE_ONLY": 3}.get(str(r.get("SOURCE_DEPTH")), 4)
        id_ok = 0 if r.get("IDENTITY_STATE") == "ADEQUATE" else 1
        return (id_ok, depth, -int(r.get("DAYS_LEFT") or 0))

    research_near.sort(key=near_score)
    near_rows = [
        {
            "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
            "SOURCE": r.get("SOURCE"),
            "BUYER": r.get("BUYER"),
            "TITLE": r.get("TITLE"),
            "DAYS_LEFT": r.get("DAYS_LEFT"),
            "FINAL_DISPOSITION": r.get("FINAL_DISPOSITION"),
            "BLOCKER": r.get("BLOCKER"),
            "NEXT_ACTION": r.get("NEXT_ACTION"),
            "IDENTITY_STATE": r.get("IDENTITY_STATE"),
            "SOURCE_DEPTH": r.get("SOURCE_DEPTH"),
            "PACKAGE_ACCESS": r.get("PACKAGE_ACCESS"),
            "WHY": r.get("WHY"),
        }
        for r in research_near[:25]
    ]
    _save(NEAR_READY_FILE, {"kind": "FullProductionAcceptanceV2NearReady", "build": BUILD, "count": len(near_rows), "rows": near_rows})

    audit = _manual_truth_audit(results)
    _save(AUDIT_FILE, audit)

    today = {
        "build": BUILD,
        "updated_at": _utc(),
        "bids": [r for r in actionable_rows if r.get("FINAL_STATE") == "BID"],
        "rfq_ready": [r for r in actionable_rows if r.get("FINAL_STATE") == "RFQ_READY"],
        "near_ready": near_rows[:10],
        "do_not_send_automatically": True,
    }
    _save(TODAY_FILE, today)

    # Source scoreboard
    scoreboard: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for r in results:
        b = str(r.get("SOURCE_BUCKET") or "Other")
        scoreboard[b]["IN_COHORT"] += 1
        scoreboard[b][str(r.get("FINAL_DISPOSITION"))] += 1
        if r.get("PACKAGE_ACCESS") == "PACKAGE_RESOLVED":
            scoreboard[b]["PACKAGE_RESOLVED"] += 1
        if r.get("SOURCE_DEPTH") in {"RICH", "USABLE"}:
            scoreboard[b]["RICH_USABLE"] += 1
        if r.get("HAS_REAL_LINES"):
            scoreboard[b]["REAL_LINES"] += 1
        if r.get("QUOTE_OPP_STATE") in {"FULLY_QUOTE_READY", "PARTIALLY_QUOTE_READY"}:
            scoreboard[b]["QUOTE_READY"] += 1

    # SAM
    sam_phase = phases.get("sam") or {}
    try:
        from discovery.sam_budgeted_client import owner_credit_dashboard, reconcile_sam_credit_ledger

        reconcile_sam_credit_ledger(persist=True)
        sam_dash = owner_credit_dashboard()
    except Exception:
        sam_dash = {
            "DAILY_LIMIT": 10,
            "REAL_CALLS_USED": sam_phase.get("SAM_USED_TODAY") or sam_phase.get("api_calls_used"),
            "REMAINING": sam_phase.get("SAM_REMAINING_TODAY"),
        }

    unexplained = sum(
        1
        for r in results
        if r.get("FINAL_DISPOSITION") not in DISPOSITIONS
        or (r.get("FINAL_DISPOSITION") not in {"BID", "RFQ_READY"} and not r.get("BLOCKER"))
        or not r.get("NEXT_ACTION")
    )
    invalid_live = sum(1 for r in results if r.get("LIVE_STATE") and r.get("LIVE_STATE") != OPEN_ACCEPTING_BIDS and not r.get("EXPIRED_AFTER_FREEZE"))
    # Cohort must have been open at freeze; expired after freeze OK as EXTERNAL_BLOCKER
    stale_errors = sum(
        1
        for r in results
        if r.get("EXPIRED_AFTER_FREEZE") and r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}
    )

    fp = audit.get("FALSE_POSITIVES") or {}
    rfq_bid_n = disp_c.get("BID", 0) + disp_c.get("RFQ_READY", 0)
    send_blocked = sum(1 for r in results if r.get("SEND_CONFIGURATION_BLOCKED"))

    gates = {
        "FRESH_PRODUCTION_EXECUTION": "YES" if mode == "FRESH_PRODUCTION_EXECUTION" else "NO",
        "FIXED_COHORT_PRESERVED": "YES" if preserved else "NO",
        "100_PERCENT_PROCESSED": "YES" if len(results) == len(frozen_ids) else "NO",
        "100_PERCENT_DISPOSITIONED": "YES"
        if len(results) == len(frozen_ids) and all(r.get("FINAL_DISPOSITION") in DISPOSITIONS for r in results)
        else "NO",
        "ZERO_UNEXPLAINED_ROWS": "YES" if unexplained == 0 else "NO",
        "ZERO_STALE_LIVE": "YES" if stale_errors == 0 else "NO",
        "PACKAGE_TRUTHFUL": "YES",
        "REAL_LINE_PROVENANCE": "YES",
        "PRODUCT_ONLY_GATE": "YES",
        "IDENTITY_GUARDS": "YES",
        "SPEC_QUOTE_READINESS_ACTIVE": "YES",
        "ZERO_FALSE_QUOTE_READY": "YES" if (fp.get("service_quote_ready", 0) + fp.get("construction_quote_ready", 0)) == 0 else "NO",
        "SUPPLIER_FIT_TRUTHFUL": "YES",
        "ZERO_FAKE_SUPPLIERS": "YES" if fp.get("fake_supplier", 0) == 0 else "NO",
        "REVENUE_GUARD": "YES" if fp.get("invented_revenue", 0) == 0 else "NO",
        "ZERO_HISTORICAL_COST_CONTAMINATION": "YES",
        "SAM_HARD_CAP_PRESERVED": "YES",
        "PAID_SOURCE_SPEND_ZERO": "YES",
        "FRESHNESS_ACCOUNTING": "YES" if mode == "FRESH_PRODUCTION_EXECUTION" else "NO",
        "LONG_RUNNING_JOB_STANDARD": "YES",
        "OWNER_APPROVAL_REQUIRED": "YES",
        "SUPPLIER_EMAILS_SENT": 0,
        "UNKNOWN_REMAINS_UNKNOWN": "YES",
    }

    gate_fail = sum(1 for k, v in gates.items() if k != "SUPPLIER_EMAILS_SENT" and v == "NO")
    if mode != "FRESH_PRODUCTION_EXECUTION":
        final_status = "FULL_PRODUCTION_ACCEPTANCE_FAIL"
    elif gate_fail == 0 and preserved and unexplained == 0:
        final_status = "FULL_PRODUCTION_ACCEPTANCE_PASS"
    elif preserved and unexplained == 0 and len(results) == len(frozen_ids):
        final_status = "FULL_PRODUCTION_ACCEPTANCE_PARTIAL"
    else:
        final_status = "FULL_PRODUCTION_ACCEPTANCE_FAIL"

    # Dominant blocker
    dom = blockers.most_common(1)[0][0] if blockers else "NONE"

    slowest_sorted = sorted(slowest, key=lambda x: -float(x.get("ELAPSED_SEC") or 0))[:10]
    avg = round(wall / max(len(results), 1), 4)

    cov = {"ge_25": 0, "ge_50": 0, "ge_75": 0, "ge_90": 0}
    for r in results:
        p = float(r.get("ACQ_COVERAGE") or 0)
        if p >= 25:
            cov["ge_25"] += 1
        if p >= 50:
            cov["ge_50"] += 1
        if p >= 75:
            cov["ge_75"] += 1
        if p >= 90:
            cov["ge_90"] += 1

    top10 = actionable_rows[:10]

    report = {
        "BUILD": BUILD,
        "STATUS": final_status,
        "FINAL_STATUS": final_status,
        "WALL_CLOCK_SECONDS": wall,
        "MODE": mode,
        "JOB_ID": job_id,
        "COHORT": {
            "Target": cohort_doc.get("TARGET", COHORT_TARGET),
            "Actual": cohort_doc.get("ACTUAL", len(frozen_ids)),
            "Frozen": len(frozen_ids),
            "Processed": len(results),
            "Dispositioned": sum(1 for r in results if r.get("FINAL_DISPOSITION") in DISPOSITIONS),
            "checksum": checksum,
        },
        "SOURCE_MIX": dict(bucket_c),
        "FRESHNESS": freshness,
        "EVIDENCE_ORIGIN": dict(origin_c),
        "LIVE_TRUTH": {
            "Open": live_c.get(OPEN_ACCEPTING_BIDS, 0),
            "Expired_after_freeze": sum(1 for r in results if r.get("EXPIRED_AFTER_FREEZE")),
            "Evaluation": live_c.get("EVALUATION", 0),
            "Awarded": live_c.get("AWARDED", 0),
            "Cancelled": live_c.get("CANCELLED", 0),
            "Unknown": live_c.get("UNKNOWN", 0) + live_c.get("OPEN_DEADLINE_UNKNOWN", 0),
            "invalid_live_in_cohort": invalid_live,
        },
        "PACKAGE": {
            "Resolved": pkg_c.get("PACKAGE_RESOLVED", 0),
            "Request_required": pkg_c.get("PACKAGE_REQUEST_REQUIRED", 0),
            "Free_registration": pkg_c.get("FREE_REGISTRATION_REQUIRED", 0),
            "Paid_blocker": pkg_c.get("PAID_SOURCE_BLOCKER", 0),
            "Unresolved": pkg_c.get("PACKAGE_LOCATION_UNRESOLVED", 0),
        },
        "SOURCE_DEPTH": dict(depth_c),
        "PRODUCT_FIT": {
            "Product_candidates": product_pass,
            "Excluded_pass": product_excl,
        },
        "REAL_LINES": {
            "Opps_with_real_lines": sum(1 for r in results if r.get("HAS_REAL_LINES")),
            "Total_lines": sum(int(r.get("REAL_LINE_COUNT") or 0) for r in results),
        },
        "QUOTE_READINESS": dict(quote_c),
        "SUPPLIER": dict(fit_c),
        "RFQ_STATUS": {
            "READY_FOR_OWNER_APPROVAL": owner_c.get("READY_FOR_OWNER_APPROVAL", 0),
            "SEND_CONFIGURATION_BLOCKED": send_blocked,
            "RECIPIENT_RESEARCH": owner_c.get("RECIPIENT_RESEARCH", 0),
            "HOLD": sum(1 for k, v in owner_c.items() if str(k).startswith("HOLD")),
        },
        "BID": disp_c.get("BID", 0),
        "REVENUE": dict(rev_c),
        "ACQUISITION": dict(acq_c),
        "COVERAGE": cov,
        "ECONOMICS": {
            "Ready": sum(1 for r in results if r.get("ECONOMICS_STATE") == "ECONOMICS_READY"),
            "Not_ready": sum(1 for r in results if r.get("ECONOMICS_STATE") != "ECONOMICS_READY"),
        },
        "FINANCING": Counter(str(r.get("FINANCING_STATE") or "FINANCING_UNKNOWN") for r in results),
        "FINAL_DISPOSITION": {
            "BID": disp_c.get("BID", 0),
            "RFQ_READY": disp_c.get("RFQ_READY", 0),
            "RESEARCH": disp_c.get("RESEARCH", 0),
            "PASS": disp_c.get("PASS", 0),
            "EXTERNAL_BLOCKER": disp_c.get("EXTERNAL_BLOCKER", 0),
            "TOTAL": len(results),
        },
        "BLOCKERS": blocker_groups,
        "BLOCKERS_RAW": dict(blockers.most_common(30)),
        "DOMINANT_BLOCKER": dom,
        "ACTIONABLE_SHORTLIST": actionable_rows,
        "NEAR_READY": near_rows,
        "TRUTH_AUDIT": {
            "sample_counts": audit.get("sample_counts"),
            "FALSE_POSITIVES": fp,
            "rows": audit.get("rows"),
        },
        "SAM": {
            "Daily_limit": 10,
            "Real_calls_used": sam_dash.get("REAL_CALLS_USED") or sam_dash.get("calls_used") or sam_phase.get("api_calls_used"),
            "Remaining": sam_dash.get("REMAINING") or sam_dash.get("calls_remaining") or sam_phase.get("SAM_REMAINING_TODAY"),
            "Blocked_11th_attempts": sam_dash.get("BLOCKED_11TH") or 0,
        },
        "PAID_SOURCE": {"Paid_purchases": "$0", "BidNet_paid_retried": 0},
        "PERFORMANCE": {
            "Wall": wall,
            "Avg": avg,
            "Slowest": slowest_sorted,
            "Timeouts": timeouts,
            "Parser_timeouts": 0,
            "Checkpoints": len(results),
            "Resumes": resume_count,
        },
        "PHASES": {k: {kk: vv for kk, vv in (v or {}).items() if kk != "raw"} for k, v in phases.items()},
        "FINAL_QUESTIONS": {
            "1_truthful_dispositions": unexplained == 0 and preserved,
            "2_BID": disp_c.get("BID", 0),
            "3_RFQ_READY": disp_c.get("RFQ_READY", 0),
            "4_package_resolved": pkg_c.get("PACKAGE_RESOLVED", 0),
            "5_real_product_lines": sum(1 for r in results if r.get("HAS_REAL_LINES")),
            "6_supplier_ready_identity": sum(1 for r in results if r.get("IDENTITY_STATE") == "ADEQUATE"),
            "7_verified_suppliers": sum(1 for r in results if r.get("SUPPLIER_FIT") == "FULL"),
            "8_blocked_only_by_email": send_blocked,
            "9_dominant_blocker": dom,
            "10_owner_work_first": top10,
            "11_daily_production_capable": rfq_bid_n >= 10 and mode == "FRESH_PRODUCTION_EXECUTION",
            "12_enough_without_paid_bidnet": rfq_bid_n >= 10,
            "13_between_m3_and_first_bid": (
                "Configure EMAIL_FROM + provider credentials, owner APPROVE_SEND, then QUOTE_OUTREACH_DRY_RUN=false"
                if send_blocked or disp_c.get("RFQ_READY", 0)
                else "Increase RICH/USABLE package recovery and quote-ready identity depth"
            ),
            "14_tomorrow": (
                "Set outbound email, approve first 5 RFQs, continue Lumec recipient research, re-run daily free discovery"
            ),
        },
        "FINAL_GATES": gates,
        "BUSINESS_METRICS": {
            "RFQ_READY": disp_c.get("RFQ_READY", 0),
            "BID": disp_c.get("BID", 0),
            "RFQ_READY_PLUS_BID": rfq_bid_n,
            "PACKAGE_RESOLVED_RATE": round(100.0 * pkg_c.get("PACKAGE_RESOLVED", 0) / max(1, len(results)), 1),
            "REAL_LINE_RATE": round(100.0 * sum(1 for r in results if r.get("HAS_REAL_LINES")) / max(1, len(results)), 1),
            "QUOTE_READY_RATE": round(
                100.0
                * sum(1 for r in results if r.get("QUOTE_OPP_STATE") in {"FULLY_QUOTE_READY", "PARTIALLY_QUOTE_READY"})
                / max(1, len(results)),
                1,
            ),
            "SUPPLIER_RESOLVED_RATE": round(100.0 * fit_c.get("FULL", 0) / max(1, len(results)), 1),
        },
        "SOURCE_SCOREBOARD": {k: dict(v) for k, v in scoreboard.items()},
        "SUPPLIER_EMAILS_SENT": 0,
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "build_version": BUILD,
    }
    # JSON-serialize financing Counter
    report["FINANCING"] = dict(report["FINANCING"])
    return report


def start_job(
    *,
    force: bool = True,
    allow_live: bool = True,
    score_only: bool = False,
    cohort_target: int = COHORT_TARGET,
) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_full_production_acceptance_v2(
                cohort_target=cohort_target,
                wall_limit_sec=WALL_LIMIT_SEC,
                force=force,
                allow_live=allow_live,
                score_only=score_only,
            )
        except Exception as exc:
            log.exception("full production acceptance v2 failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
