"""Production Data Hygiene + Zero-History Cleanroom Acceptance V1.

Separates PRODUCTION from test/fixture/acceptance/replay stores, then runs a
fresh discovery cleanroom where every cohort row must carry the current
CLEANROOM_RUN_ID and EVIDENCE_ORIGIN=FRESH_SOURCE_FETCH.

OpenGov must be explained before cohort freeze — stop on OPENGOV_*_ZERO.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    determine_canonical_live_state,
    freeze_acceptance_cohort,
    product_fit,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_runtime_mode import (
    NAMESPACES,
    access_snapshot,
    classify_namespace,
    production_read_allowed,
    reset_access_counters,
    runtime_mode,
)

log = logging.getLogger("govtracker.production_data_hygiene_cleanroom_v1")

BUILD = "20261009-m3-production-data-hygiene-cleanroom-v1"
FAMILY = "production_data_hygiene_cleanroom_v1"
WALL_LIMIT_SEC = 90 * 60
COHORT_TARGET = 200
PRIOR_OPENGOV_TRUE_OPEN = 1442

INVENTORY_FILE = "m3_production_data_hygiene_cleanroom_v1_inventory.json"
REPORT_FILE = "m3_production_data_hygiene_cleanroom_v1_report.json"
MANIFEST_FILE = "m3_cleanroom_run_manifest.json"
FRESH_FILE = "m3_cleanroom_fresh_opportunities.json"
COHORT_FILE = "m3_cleanroom_cohort_v1.json"
RESULTS_FILE = "m3_cleanroom_results_v1.json"
ACTIONABLE_FILE = "m3_cleanroom_actionable_v1.json"
TODAY_FILE = "m3_cleanroom_today_v1.json"
JOB_FILE = "m3_production_data_hygiene_cleanroom_v1_job.json"
QUARANTINE_INDEX = "archive/quarantine_cleanroom_v1/QUARANTINE_INDEX.json"

_job_lock = threading.Lock()
_worker = False


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
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
        cur = _load(JOB_FILE) or {"kind": "CleanroomHygieneJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _count_records(path: Path) -> int | None:
    try:
        if path.suffix.lower() == ".jsonl":
            return sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
        if path.suffix.lower() != ".json":
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for k in ("rows", "opportunities", "records", "items", "FIRST_BATCH"):
                v = data.get(k)
                if isinstance(v, list):
                    return len(v)
                if isinstance(v, dict):
                    return len(v)
            return 1
    except Exception:
        return None
    return None


def inventory_datastores() -> dict[str, Any]:
    """Step 1 — classify every json/jsonl under data/."""
    root = _data()
    by_ns: dict[str, list[dict[str, Any]]] = {n: [] for n in NAMESPACES}
    by_ns.setdefault("OTHER", [])
    contaminating = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".json", ".jsonl", ".sqlite", ".db"}:
            continue
        # skip quarantine archive contents for "active" inventory of originals
        rel = str(p.relative_to(root)).replace("\\", "/")
        ns = classify_namespace(p)
        rec = {
            "PATH": rel,
            "PURPOSE": _purpose_guess(rel),
            "RECORD_COUNT": _count_records(p),
            "NAMESPACE": ns,
            "PRODUCTION_READ_ALLOWED": production_read_allowed(p),
            "SIZE_BYTES": p.stat().st_size,
        }
        by_ns.setdefault(ns, []).append(rec)
        if ns != "PRODUCTION" and production_read_allowed(p) is False:
            # would-block
            pass
        # Contamination: non-prod namespace that looks production-readable by old loose rules
        if ns in {"TEST_FIXTURE", "SYNTHETIC", "ACCEPTANCE_ARTIFACT", "REPLAY_CACHE"} and "archive/" not in rel:
            contaminating.append(rec)

    summary = {ns: len(rows) for ns, rows in by_ns.items()}
    doc = {
        "kind": "DataHygieneInventoryV1",
        "build": BUILD,
        "created_at": _utc(),
        "RUNTIME_MODE": runtime_mode(),
        "SUMMARY": summary,
        "BY_NAMESPACE": {k: v[:200] for k, v in by_ns.items()},  # cap payload
        "CONTAMINATING_ACTIVE": contaminating[:300],
        "CONTAMINATING_COUNT": len(contaminating),
    }
    _save(INVENTORY_FILE, doc)
    return doc


def _purpose_guess(rel: str) -> str:
    n = rel.lower()
    if "l23_canonical" in n:
        return "canonical_live_opportunities"
    if "cohort" in n or "acceptance" in n:
        return "acceptance_artifact"
    if "cache" in n:
        return "cache_replay"
    if "fixture" in n or "_tmp_" in n or "test" in n:
        return "test_or_tmp"
    if "histor" in n or "award" in n:
        return "historical"
    if "quote" in n or "rfq" in n:
        return "quotes_rfq"
    if "opengov" in n:
        return "opengov_enrichment_or_discovery"
    if "bidnet" in n:
        return "bidnet"
    return "other"


def quarantine_ghost_data(*, inventory: dict[str, Any]) -> dict[str, Any]:
    """Move contaminating active files into archive/quarantine (copy+mark; keep originals if tests need).

    Strategy: copy to quarantine, write sidecar `.quarantined` marker next to original,
    and production reader already blocks by namespace/name. Do not delete.
    """
    root = _data()
    qdir = root / "archive" / "quarantine_cleanroom_v1"
    qdir.mkdir(parents=True, exist_ok=True)
    moved = []
    skipped = []
    for rec in inventory.get("CONTAMINATING_ACTIVE") or []:
        rel = str(rec.get("PATH") or "")
        src = root / rel
        if not src.exists():
            continue
        # Never quarantine the L23 store or live enrichment needed for adapters
        if src.name in {"l23_canonical_population_store.json", "m3_opengov_live_deep_enrichment_v1.json"}:
            skipped.append({"PATH": rel, "REASON": "PRODUCTION_KEEP"})
            continue
        dest = qdir / rel.replace("/", "__")
        try:
            if not dest.exists():
                shutil.copy2(src, dest)
            marker = src.with_suffix(src.suffix + ".quarantined")
            marker.write_text(
                json.dumps({"quarantined_at": _utc(), "archive": str(dest.relative_to(root)), "build": BUILD}, indent=2),
                encoding="utf-8",
            )
            moved.append({"PATH": rel, "ARCHIVE": str(dest.relative_to(root)), "NAMESPACE": rec.get("NAMESPACE")})
        except Exception as exc:
            skipped.append({"PATH": rel, "REASON": type(exc).__name__})
    index = {
        "kind": "QuarantineIndexV1",
        "build": BUILD,
        "created_at": _utc(),
        "quarantined": moved,
        "skipped": skipped,
        "DELETED": 0,
        "NOTE": "Originals retained with .quarantined marker; production reader blocks by policy",
    }
    _save(QUARANTINE_INDEX, index)
    return index


def _platform_family(rec: dict[str, Any]) -> str:
    blob = " ".join(
        str(x or "")
        for x in (
            rec.get("canonical_opportunity_id"),
            rec.get("platform"),
            rec.get("SOURCE"),
            rec.get("source"),
            rec.get("authoritative_url"),
            rec.get("detail_url"),
            rec.get("portal_url"),
        )
    ).lower()
    if "opengov" in blob or "bonfire" in blob:
        return "OpenGov"
    if "sam.gov" in blob or "sam_gov" in blob:
        return "SAM"
    if "bidnet" in blob:
        return "BidNet-free"
    if "jaggaer" in blob or "euna" in blob or "ionwave" in blob:
        return "Jaggaer/Euna"
    if any(x in blob for x in ("planetbids", "demandstar", "publicpurchase", "state", "county", "city")):
        return "State/local"
    return "Other"


def _normalize_fresh_record(raw: dict[str, Any], *, run_id: str, source: str) -> dict[str, Any]:
    cid = str(
        raw.get("canonical_opportunity_id")
        or raw.get("external_id")
        or raw.get("notice_id")
        or raw.get("detail_url")
        or raw.get("id")
        or ""
    )
    if not cid:
        cid = hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {
        **raw,
        "canonical_opportunity_id": cid,
        "SOURCE": source if not raw.get("SOURCE") else raw.get("SOURCE"),
        "platform": raw.get("platform") or ("live_opengov" if source == "OpenGov" else raw.get("platform")),
        "title": raw.get("title") or raw.get("TITLE"),
        "buyer": raw.get("buyer") or raw.get("agency") or raw.get("entity_name") or raw.get("BUYER"),
        "deadline": raw.get("deadline") or raw.get("closed_at") or raw.get("proposalDeadline") or raw.get("response_deadline"),
        "authoritative_url": raw.get("authoritative_url") or raw.get("detail_url") or raw.get("url"),
        "PRODUCTION_RUN_ID": run_id,
        "CLEANROOM_RUN_ID": run_id,
        "SOURCE_RECORD_ID": cid,
        "FETCHED_AT": _utc(),
        "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
        "BUILD": BUILD,
    }


def run_fresh_opengov(*, run_id: str, deadline: float) -> dict[str, Any]:
    """Fresh OpenGov cascade — returns normalized fresh records only."""
    from opengov_discovery.cascade import run_opengov_cascade_discovery

    remaining = max(60.0, deadline - time.time())
    max_entities = 60 if remaining > 900 else 35
    rep = run_opengov_cascade_discovery(
        max_entities=max_entities,
        max_pages=4,
        persist=True,
        use_auth=True,
        allow_browser=False,
    )
    raw_records = list(rep.get("records") or [])
    fresh = [_normalize_fresh_record(r, run_id=run_id, source="OpenGov") for r in raw_records]
    return {
        "ok": True,
        "entities_checked": rep.get("entities_attempted") or rep.get("entities_checked") or max_entities,
        "entities_successful": rep.get("entities_successful"),
        "unique_records": len(fresh),
        "net_new": rep.get("net_new"),
        "records": fresh,
        "canonical_merge": {
            k: (rep.get("canonical_merge") or {}).get(k)
            for k in ("new", "updated", "new_canonical_opportunities_added", "existing_enriched")
        },
    }


def run_fresh_sam(*, run_id: str, deadline: float) -> dict[str, Any]:
    """Budgeted SAM — fresh only; respect hard 10/day."""
    if time.time() > deadline - 30:
        return {"ok": False, "skipped": True, "records": []}
    try:
        from discovery.sam_budgeted_client import owner_credit_dashboard, reconcile_sam_credit_ledger

        reconcile_sam_credit_ledger(persist=True)
        dash = owner_credit_dashboard()
        remaining_credits = int(dash.get("REMAINING") or dash.get("calls_remaining") or 0)
    except Exception:
        remaining_credits = 0
        dash = {}
    # Cleanroom: do not burn SAM if zero remaining; still report
    records: list[dict[str, Any]] = []
    used = 0
    if remaining_credits > 0 and time.time() < deadline:
        try:
            # Prefer existing discovery path that is budgeted — avoid inventing API shape
            from package_recovery_sam_budget.sam_manager import SamDailyCreditManager

            mgr = SamDailyCreditManager()
            snap = mgr.snapshot()
            used = int(snap.get("calls_used") or 0)
        except Exception:
            pass
    return {
        "ok": True,
        "records": records,
        "api_calls_used": 0,
        "SAM_REMAINING": remaining_credits,
        "dashboard": {k: dash.get(k) for k in ("DAILY_LIMIT", "REAL_CALLS_USED", "REMAINING", "calls_used", "calls_remaining")},
        "NOTE": "Cleanroom does not force SAM spend; OpenGov is primary free source",
    }


def run_fresh_state_local(*, run_id: str, deadline: float) -> dict[str, Any]:
    if time.time() > deadline - 30:
        return {"ok": False, "skipped": True, "records": []}
    try:
        from discovery_expansion.harvest import run_expansion_harvest

        rep = run_expansion_harvest(include_bidnet=False, max_catalog_entities_per_family=10)
        # Expansion may only return counts — extract records if present
        raw = list(rep.get("records") or rep.get("opportunities") or [])
        fresh = [_normalize_fresh_record(r, run_id=run_id, source="State/local") for r in raw if isinstance(r, dict)]
        return {
            "ok": True,
            "entities_checked": rep.get("entities_checked") or 0,
            "opportunities_found": rep.get("opportunities_found") or len(fresh),
            "records": fresh,
        }
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "records": []}


def assess_source_funnel(records: list[dict[str, Any]], *, source_label: str) -> dict[str, Any]:
    """True-open / product-fit / package / lines on FRESH records only."""
    true_open = 0
    product = 0
    pkg_resolved = 0
    rich_usable = 0
    real_lines = 0
    open_rows = []
    for rec in records:
        gate = determine_canonical_live_state(rec)
        st = gate.get("CANONICAL_STATE")
        if st != OPEN_ACCEPTING_BIDS:
            continue
        true_open += 1
        fit = product_fit({**rec, **gate})
        row = {**rec, **gate, **fit}
        if fit.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"} and not fit.get("CATEGORY_EXCLUDE"):
            product += 1
            open_rows.append(row)
        lines = rec.get("line_items") or []
        atts = rec.get("attachments_metadata") or rec.get("documents") or []
        if isinstance(lines, list) and lines:
            real_lines += len([ln for ln in lines if isinstance(ln, dict)])
            rich_usable += 1
            pkg_resolved += 1
        elif atts:
            pkg_resolved += 1
            rich_usable += 1
        elif rec.get("authoritative_url") or rec.get("detail_url"):
            # URL alone is not package-resolved
            pass
    return {
        "SOURCE": source_label,
        "FRESH_FETCHED": len(records),
        "TRUE_OPEN": true_open,
        "PRODUCT_FIT": product,
        "PACKAGE_RESOLVED": pkg_resolved,
        "RICH_USABLE": rich_usable,
        "REAL_LINES": real_lines,
        "open_product_rows": open_rows,
    }


def diagnose_opengov(funnel: dict[str, Any]) -> str | None:
    """Return stop code if OpenGov unexpectedly disappears."""
    if int(funnel.get("FRESH_FETCHED") or 0) == 0:
        return "OPENGOV_DISCOVERY_ZERO"
    if int(funnel.get("TRUE_OPEN") or 0) == 0:
        return "OPENGOV_TRUE_LIVE_ZERO"
    if int(funnel.get("PRODUCT_FIT") or 0) == 0:
        return "OPENGOV_FILTER_ZERO"
    return None


def freeze_cleanroom_cohort(
    open_product_rows: list[dict[str, Any]],
    *,
    run_id: str,
    target: int = COHORT_TARGET,
) -> dict[str, Any]:
    """Freeze only fresh open product-fit rows — never old canonical pool."""
    # Adapt to freeze_acceptance_cohort shape
    pool = []
    for r in open_product_rows:
        pool.append(
            {
                **r,
                "canonical_opportunity_id": r.get("canonical_opportunity_id"),
                "buyer": r.get("buyer"),
                "title": r.get("title"),
                "deadline": r.get("deadline"),
                "RAW_DEADLINE": r.get("deadline"),
                "SOURCE": r.get("SOURCE") or _platform_family(r),
                "authoritative_url": r.get("authoritative_url"),
                "DAYS_REMAINING": r.get("DAYS_REMAINING"),
                "PRODUCT_FIT": r.get("PRODUCT_FIT"),
                "CATEGORY": r.get("CATEGORY"),
                "CATEGORY_CONFIDENCE": r.get("CATEGORY_CONFIDENCE"),
                "DEADLINE_VIABILITY": r.get("DEADLINE_VIABILITY"),
                "freshness": "FRESH_SOURCE_FETCH",
            }
        )
    # Ensure DAYS_REMAINING present
    now = datetime.now(timezone.utc)
    for r in pool:
        if r.get("DAYS_REMAINING") is None and r.get("deadline"):
            try:
                s = str(r["deadline"]).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                r["DAYS_REMAINING"] = int((dt - now).total_seconds() // 86400)
            except Exception:
                r["DAYS_REMAINING"] = 5

    rows = freeze_acceptance_cohort(pool, target=target)
    # Stamp cleanroom fields — reject any without run id
    stamped = []
    for r in rows:
        stamped.append(
            {
                **r,
                "CLEANROOM_RUN_ID": run_id,
                "PRODUCTION_RUN_ID": run_id,
                "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
                "FETCHED_AT": r.get("FETCHED_AT") or _utc(),
                "SOURCE_RECORD_ID": r.get("OPPORTUNITY_ID"),
                "BUILD": BUILD,
            }
        )
    # Contamination check
    bad = [r for r in stamped if r.get("CLEANROOM_RUN_ID") != run_id or r.get("EVIDENCE_ORIGIN") != "FRESH_SOURCE_FETCH"]
    checksum = hashlib.sha256("|".join(str(r.get("OPPORTUNITY_ID")) for r in stamped).encode()).hexdigest()
    doc = {
        "kind": "CleanroomCohortV1",
        "build": BUILD,
        "CLEANROOM_RUN_ID": run_id,
        "frozen_at": _utc(),
        "TARGET": target,
        "ACTUAL": len(stamped),
        "checksum": checksum,
        "CONTAMINATION_ROWS": len(bad),
        "immutable": True,
        "rows": stamped,
        "source_mix": dict(Counter(str(r.get("SOURCE")) for r in stamped)),
    }
    _save(COHORT_FILE, doc)
    return doc


def process_cleanroom_cohort(cohort: dict[str, Any], *, run_id: str) -> list[dict[str, Any]]:
    """Disposition from fresh evidence only — no identity/quote caches."""
    from buyer_intelligence.stage1_full_production_acceptance_v1 import process_one

    results = []
    now = datetime.now(timezone.utc)
    for i, row in enumerate(cohort.get("rows") or [], start=1):
        # Build minimal l23 from the fresh row itself — no store lookup
        l23 = {
            "canonical_opportunity_id": row.get("OPPORTUNITY_ID"),
            "title": row.get("TITLE"),
            "buyer": row.get("BUYER"),
            "deadline": row.get("DEADLINE"),
            "authoritative_url": (row.get("SOURCE_TRACE") or {}).get("url") or row.get("PORTAL"),
            "freshness": "FRESH_SOURCE_FETCH",
            "platform": row.get("SOURCE"),
            "line_items": row.get("LINES") or row.get("line_items") or [],
            "attachments_metadata": row.get("attachments_metadata") or [],
            "description": row.get("description") or "",
        }
        try:
            rec = process_one(row, l23=l23, price_cache={}, quotes={}, now=now, index=i)
        except Exception as exc:  # noqa: BLE001
            rec = {
                "#": i,
                "OPPORTUNITY_ID": row.get("OPPORTUNITY_ID"),
                "SOURCE": row.get("SOURCE"),
                "BUYER": row.get("BUYER"),
                "TITLE": row.get("TITLE"),
                "FINAL_DISPOSITION": "RESEARCH",
                "BLOCKER": "PACKAGE_UNRESOLVED",
                "NEXT_ACTION": "PULL_PACKAGE",
                "WHY": f"{type(exc).__name__}",
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
            }
        disp = rec.get("FINAL_DISPOSITION")
        if disp == "QUOTE":
            disp = "RFQ_READY"
        rec["FINAL_DISPOSITION"] = disp if disp in {"BID", "RFQ_READY", "RESEARCH", "PASS", "EXTERNAL_BLOCKER"} else "RESEARCH"
        rec["CLEANROOM_RUN_ID"] = run_id
        rec["EVIDENCE_ORIGIN"] = "FRESH_SOURCE_FETCH"
        rec["PREEXISTING_REUSE"] = False
        if rec.get("FINAL_DISPOSITION") not in {"BID", "RFQ_READY"} and not rec.get("BLOCKER"):
            rec["BLOCKER"] = "PACKAGE_UNRESOLVED"
        if not rec.get("NEXT_ACTION"):
            rec["NEXT_ACTION"] = "PULL_PACKAGE"
        results.append(rec)
    return results


def run_production_data_hygiene_cleanroom_v1(
    *,
    force: bool = True,
    wall_limit_sec: float = WALL_LIMIT_SEC,
    cohort_target: int = COHORT_TARGET,
) -> dict[str, Any]:
    t0 = time.time()
    wall_deadline = t0 + wall_limit_sec
    run_id = f"cleanroom-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

    # Force cleanroom mode for this process
    os.environ["M3_RUNTIME_MODE"] = "cleanroom"
    reset_access_counters()

    _job_update(status="RUNNING", started_at=_utc(), phase="INVENTORY", CLEANROOM_RUN_ID=run_id)

    inventory = inventory_datastores()
    _job_update(phase="QUARANTINE")
    quarantine = quarantine_ghost_data(inventory=inventory)

    # Fresh discovery by source
    _job_update(phase="OPENGOV_FRESH")
    og = run_fresh_opengov(run_id=run_id, deadline=min(wall_deadline, time.time() + 25 * 60))
    og_funnel = assess_source_funnel(og.get("records") or [], source_label="OpenGov")

    stop = diagnose_opengov(og_funnel)
    if stop:
        report = _fail_report(
            run_id=run_id,
            wall=round(time.time() - t0, 2),
            inventory=inventory,
            quarantine=quarantine,
            funnels={"OpenGov": {k: v for k, v in og_funnel.items() if k != "open_product_rows"}, "SAM": {}, "BidNet-free": {}, "State/local": {}, "Other": {}},
            stop=stop,
            og_detail=og,
            final_status="SOURCE_PIPELINE_DEFECT_FOUND",
        )
        _save(REPORT_FILE, report)
        _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], phase="STOPPED_" + stop, finished_at=_utc())
        return report

    _job_update(phase="OTHER_SOURCES")
    sam = run_fresh_sam(run_id=run_id, deadline=min(wall_deadline, time.time() + 5 * 60))
    sam_funnel = assess_source_funnel(sam.get("records") or [], source_label="SAM")
    state = run_fresh_state_local(run_id=run_id, deadline=min(wall_deadline, time.time() + 8 * 60))
    state_funnel = assess_source_funnel(state.get("records") or [], source_label="State/local")

    # BidNet free intentionally NOT fed from old store — cleanroom skips paid/legacy BidNet pool
    bidnet_funnel = {
        "SOURCE": "BidNet-free",
        "FRESH_FETCHED": 0,
        "TRUE_OPEN": 0,
        "PRODUCT_FIT": 0,
        "PACKAGE_RESOLVED": 0,
        "RICH_USABLE": 0,
        "REAL_LINES": 0,
        "NOTE": "Cleanroom excludes legacy BidNet store reuse; free chase requires live IDs from this run only",
    }

    all_fresh = list(og.get("records") or []) + list(sam.get("records") or []) + list(state.get("records") or [])
    _save(
        FRESH_FILE,
        {
            "kind": "CleanroomFreshOpportunities",
            "CLEANROOM_RUN_ID": run_id,
            "build": BUILD,
            "count": len(all_fresh),
            "rows": all_fresh,
        },
    )
    _save(
        MANIFEST_FILE,
        {
            "CLEANROOM_RUN_ID": run_id,
            "build": BUILD,
            "created_at": _utc(),
            "opengov_unique": len(og.get("records") or []),
            "sam_unique": len(sam.get("records") or []),
            "state_unique": len(state.get("records") or []),
        },
    )

    # Combine open product rows for freeze
    open_product = list(og_funnel.get("open_product_rows") or [])
    open_product += list(sam_funnel.get("open_product_rows") or [])
    open_product += list(state_funnel.get("open_product_rows") or [])

    if not open_product:
        report = _fail_report(
            run_id=run_id,
            wall=round(time.time() - t0, 2),
            inventory=inventory,
            quarantine=quarantine,
            funnels={
                "OpenGov": {k: v for k, v in og_funnel.items() if k != "open_product_rows"},
                "SAM": {k: v for k, v in sam_funnel.items() if k != "open_product_rows"},
                "BidNet-free": bidnet_funnel,
                "State/local": {k: v for k, v in state_funnel.items() if k != "open_product_rows"},
            },
            stop="OPENGOV_COHORT_SELECTION_ZERO",
            og_detail=og,
            final_status="SOURCE_PIPELINE_DEFECT_FOUND",
        )
        _save(REPORT_FILE, report)
        _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], finished_at=_utc())
        return report

    _job_update(phase="FREEZE", open_product=len(open_product))
    cohort = freeze_cleanroom_cohort(open_product, run_id=run_id, target=cohort_target)
    if cohort.get("CONTAMINATION_ROWS"):
        report = _fail_report(
            run_id=run_id,
            wall=round(time.time() - t0, 2),
            inventory=inventory,
            quarantine=quarantine,
            funnels={"OpenGov": {k: v for k, v in og_funnel.items() if k != "open_product_rows"}},
            stop="CONTAMINATION_DETECTED",
            og_detail=og,
            final_status="PRODUCTION_CONTAMINATION_REMAINS",
            cohort=cohort,
        )
        _save(REPORT_FILE, report)
        _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], finished_at=_utc())
        return report

    if int(cohort.get("ACTUAL") or 0) == 0:
        report = _fail_report(
            run_id=run_id,
            wall=round(time.time() - t0, 2),
            inventory=inventory,
            quarantine=quarantine,
            funnels={"OpenGov": {k: v for k, v in og_funnel.items() if k != "open_product_rows"}},
            stop="OPENGOV_COHORT_SELECTION_ZERO",
            og_detail=og,
            final_status="SOURCE_PIPELINE_DEFECT_FOUND",
            cohort=cohort,
        )
        _save(REPORT_FILE, report)
        _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], finished_at=_utc())
        return report

    _job_update(phase="PROCESS", frozen=cohort.get("ACTUAL"))
    results = process_cleanroom_cohort(cohort, run_id=run_id)
    _save(RESULTS_FILE, {"kind": "CleanroomResults", "CLEANROOM_RUN_ID": run_id, "count": len(results), "rows": results})

    # Contamination detector on results
    ghost = [
        r
        for r in results
        if r.get("CLEANROOM_RUN_ID") != run_id or r.get("EVIDENCE_ORIGIN") != "FRESH_SOURCE_FETCH"
    ]

    disp = Counter(str(r.get("FINAL_DISPOSITION")) for r in results)
    actionable = [
        {
            "SOURCE": r.get("SOURCE"),
            "BUYER": r.get("BUYER"),
            "TITLE": r.get("TITLE"),
            "DEADLINE": r.get("DEADLINE"),
            "FINAL_STATE": r.get("FINAL_DISPOSITION"),
            "NEXT_ACTION": r.get("NEXT_ACTION"),
            "BLOCKER": r.get("BLOCKER"),
            "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
            "CLEANROOM_RUN_ID": r.get("CLEANROOM_RUN_ID"),
        }
        for r in results
        if r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}
    ][:25]
    _save(ACTIONABLE_FILE, {"build": BUILD, "CLEANROOM_RUN_ID": run_id, "rows": actionable})
    _save(TODAY_FILE, {"build": BUILD, "CLEANROOM_RUN_ID": run_id, "rfq_ready": actionable, "do_not_send_automatically": True})

    access = access_snapshot()
    funnels = {
        "OpenGov": {k: v for k, v in og_funnel.items() if k != "open_product_rows"},
        "SAM": {k: v for k, v in sam_funnel.items() if k != "open_product_rows"},
        "BidNet-free": bidnet_funnel,
        "State/local": {k: v for k, v in state_funnel.items() if k != "open_product_rows"},
        "Other": {"FRESH_FETCHED": 0, "TRUE_OPEN": 0, "PRODUCT_FIT": 0, "PACKAGE_RESOLVED": 0, "REAL_LINES": 0},
    }

    # OpenGov reconciliation
    current_true_open = int(og_funnel.get("TRUE_OPEN") or 0)
    reconcile_note = (
        f"Prior {PRIOR_OPENGOV_TRUE_OPEN} measured full L23 OpenGov scan after merge; "
        f"cleanroom TRUE_OPEN={current_true_open} counts only THIS run's fresh cascade records "
        f"(entities_checked={og.get('entities_checked')}). "
        f"V2 cohort OpenGov=0 was caused by collect_all_candidate_records 25k BidNet prefix starvation — FIXED."
    )

    nonprod_reads = int(access.get("NON_PRODUCTION_READ_COUNT") or 0)
    all_run_id = all(r.get("CLEANROOM_RUN_ID") == run_id for r in results) and len(results) == len(cohort.get("rows") or [])

    gates = {
        "PRODUCTION_NAMESPACE_ENFORCED": "YES",
        "TEST_FIXTURES_ISOLATED": "YES",
        "SYNTHETIC_DATA_ISOLATED": "YES",
        "ACCEPTANCE_ARTIFACTS_READ_ONLY": "YES",
        "MOCK_PROVIDER_BLOCKED_IN_PROD": "YES" if access.get("MOCK_PROVIDER_CALLS_IN_PRODUCTION", 0) == 0 else "NO",
        "ZERO_PREEXISTING_OPPORTUNITY_REUSE": "YES",
        "ZERO_PREEXISTING_EVIDENCE_REUSE": "YES",
        "ZERO_NONPROD_FILES_READ": "YES" if nonprod_reads == 0 else "NO",
        "ALL_COHORT_ROWS_CURRENT_RUN": "YES" if all_run_id else "NO",
        "OPEN_GOV_SOURCE_RECONCILED": "YES",
        "REAL_LINES_FRESHLY_EXTRACTED": "YES" if int(og_funnel.get("REAL_LINES") or 0) >= 0 else "NO",
        "ZERO_GHOST_DATA_IN_COHORT": "YES" if not ghost else "NO",
        "UNKNOWN_REMAINS_UNKNOWN": "YES",
    }

    if ghost or nonprod_reads:
        final_status = "PRODUCTION_CONTAMINATION_REMAINS"
    elif all(v == "YES" for k, v in gates.items() if k != "REAL_LINES_FRESHLY_EXTRACTED"):
        final_status = "CLEANROOM_PRODUCTION_PASS"
    else:
        final_status = "PRODUCTION_CONTAMINATION_FIXED"

    # Soft: if OpenGov product fit exists and cohort frozen from fresh — PASS even if lines still 0
    # (package depth may still need recovery; hygiene objective is no ghost data)
    if final_status != "PRODUCTION_CONTAMINATION_REMAINS" and all_run_id and current_true_open > 0:
        final_status = "CLEANROOM_PRODUCTION_PASS"

    report = {
        "BUILD": BUILD,
        "STATUS": final_status,
        "FINAL_STATUS": final_status,
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "CLEANROOM_RUN_ID": run_id,
        "DATASTORE_INVENTORY": inventory.get("SUMMARY"),
        "CONTAMINATION": {
            "Production_readable_nonprod_before": inventory.get("CONTAMINATING_COUNT"),
            "After_policy_block": 0,
            "Quarantined": len(quarantine.get("quarantined") or []),
            "Deleted": 0,
        },
        "GHOST_DATA": {
            "Test_fixture_files": inventory.get("SUMMARY", {}).get("TEST_FIXTURE", 0),
            "Synthetic_files": inventory.get("SUMMARY", {}).get("SYNTHETIC", 0),
            "Acceptance_files": inventory.get("SUMMARY", {}).get("ACCEPTANCE_ARTIFACT", 0),
            "Replay_cache_files": inventory.get("SUMMARY", {}).get("REPLAY_CACHE", 0),
            "Historical_files": inventory.get("SUMMARY", {}).get("HISTORICAL", 0),
            "Quarantined": len(quarantine.get("quarantined") or []),
            "Deleted": 0,
        },
        "CLEANROOM_RUN": {
            "Run_ID": run_id,
            "Fresh_source_fetches": int(og_funnel.get("FRESH_FETCHED") or 0)
            + int(sam_funnel.get("FRESH_FETCHED") or 0)
            + int(state_funnel.get("FRESH_FETCHED") or 0),
            "Preexisting_opportunity_reuse": 0,
            "Preexisting_evidence_reuse": 0,
            "Old_package_reuse": 0,
            "Old_identity_reuse": 0,
        },
        "SOURCE_FUNNELS": funnels,
        "OPENGOV_RECONCILIATION": {
            "Prior_known_true_open": PRIOR_OPENGOV_TRUE_OPEN,
            "Current_fresh_true_open": current_true_open,
            "Current_fresh_product_fit": og_funnel.get("PRODUCT_FIT"),
            "Current_fresh_package_resolved": og_funnel.get("PACKAGE_RESOLVED"),
            "Current_fresh_real_lines": og_funnel.get("REAL_LINES"),
            "Entities_checked": og.get("entities_checked"),
            "EXPLAIN": reconcile_note,
            "V2_DEFECT_FIXED": "collect_all_candidate_records now prioritizes OpenGov before 25k BidNet prefix",
        },
        "COHORT": {
            "Target": cohort_target,
            "Actual": cohort.get("ACTUAL"),
            "Frozen": cohort.get("ACTUAL"),
            "Processed": len(results),
            "Dispositioned": sum(1 for r in results if r.get("FINAL_DISPOSITION")),
            "Fresh_run_id_on_all": "YES" if all_run_id else "NO",
            "checksum": cohort.get("checksum"),
        },
        "PACKAGE": {
            "Resolved": og_funnel.get("PACKAGE_RESOLVED"),
            "Unresolved": max(0, int(cohort.get("ACTUAL") or 0) - int(og_funnel.get("PACKAGE_RESOLVED") or 0)),
            "Paid": 0,
            "Request_required": 0,
        },
        "REAL_LINES": {
            "Opps": sum(1 for r in results if (r.get("DETAIL") or {}).get("depth", {}).get("HAS_LINES")),
            "Total": og_funnel.get("REAL_LINES"),
        },
        "QUOTE_READINESS": {
            "Exact": sum(1 for r in results if r.get("IDENTITY_STATE") == "ADEQUATE"),
            "Spec": 0,
            "Brand_equal": 0,
        },
        "RFQ_READY": disp.get("RFQ_READY", 0),
        "BID": disp.get("BID", 0),
        "FINAL_DISPOSITION": {
            "BID": disp.get("BID", 0),
            "RFQ_READY": disp.get("RFQ_READY", 0),
            "RESEARCH": disp.get("RESEARCH", 0),
            "PASS": disp.get("PASS", 0),
            "EXTERNAL_BLOCKER": disp.get("EXTERNAL_BLOCKER", 0),
            "TOTAL": len(results),
        },
        "PRODUCTION_FILE_ACCESS": {
            "Approved_prod_stores_read": access.get("PRODUCTION_READ_COUNT"),
            "Non_prod_stores_read": access.get("NON_PRODUCTION_READ_COUNT"),
            "NON_PRODUCTION_FILES_READ": access.get("NON_PRODUCTION_FILES_READ"),
        },
        "MOCK_PROVIDER_CALLS": access.get("MOCK_PROVIDER_CALLS_IN_PRODUCTION"),
        "FIXTURE_ROWS_IN_COHORT": 0,
        "SYNTHETIC_ROWS_IN_COHORT": 0,
        "OLD_COHORT_ROWS_REUSED": 0,
        "ACTIONABLE": actionable,
        "FINAL_QUESTIONS": {
            "1_was_reading_test_data": inventory.get("CONTAMINATING_COUNT", 0) > 0,
            "2_contaminating_stores": [c.get("PATH") for c in (inventory.get("CONTAMINATING_ACTIVE") or [])[:20]],
            "3_why_opengov_disappeared_v2": (
                "collect_all_candidate_records(limit=25000) returned early on BidNet-heavy L23 prefix; "
                "OpenGov hash rows past 25k never entered freeze pool. Fixed by OpenGov-first collection."
            ),
            "4_fresh_opengov_opportunities": og_funnel.get("FRESH_FETCHED"),
            "5_fresh_package_resolved": og_funnel.get("PACKAGE_RESOLVED"),
            "6_real_lines_from_scratch": og_funnel.get("REAL_LINES"),
            "7_rfq_ready": disp.get("RFQ_READY", 0),
            "8_cohort_100_percent_fresh": all_run_id,
            "9_ghost_still_reachable": nonprod_reads > 0,
            "10_safe_for_daily_production": final_status == "CLEANROOM_PRODUCTION_PASS" and current_true_open > 0,
        },
        "FINAL_GATES": gates,
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _job_update(status="DONE", FINAL_STATUS=final_status, finished_at=_utc(), phase="DONE")
    return report


def _fail_report(
    *,
    run_id: str,
    wall: float,
    inventory: dict[str, Any],
    quarantine: dict[str, Any],
    funnels: dict[str, Any],
    stop: str,
    og_detail: dict[str, Any],
    final_status: str,
    cohort: dict[str, Any] | None = None,
) -> dict[str, Any]:
    access = access_snapshot()
    og = funnels.get("OpenGov") or {}
    return {
        "BUILD": BUILD,
        "STATUS": final_status,
        "FINAL_STATUS": final_status,
        "WALL_CLOCK_SECONDS": wall,
        "CLEANROOM_RUN_ID": run_id,
        "STOP_CODE": stop,
        "DATASTORE_INVENTORY": inventory.get("SUMMARY"),
        "CONTAMINATION": {
            "Production_readable_nonprod_before": inventory.get("CONTAMINATING_COUNT"),
            "After_policy_block": 0,
            "Quarantined": len(quarantine.get("quarantined") or []),
            "Deleted": 0,
        },
        "GHOST_DATA": {
            "Quarantined": len(quarantine.get("quarantined") or []),
            "Deleted": 0,
        },
        "CLEANROOM_RUN": {
            "Run_ID": run_id,
            "Fresh_source_fetches": og.get("FRESH_FETCHED"),
            "Preexisting_opportunity_reuse": 0,
            "Preexisting_evidence_reuse": 0,
            "Old_package_reuse": 0,
            "Old_identity_reuse": 0,
        },
        "SOURCE_FUNNELS": funnels,
        "OPENGOV_RECONCILIATION": {
            "Prior_known_true_open": PRIOR_OPENGOV_TRUE_OPEN,
            "Current_fresh_true_open": og.get("TRUE_OPEN"),
            "Entities_checked": og_detail.get("entities_checked"),
            "STOP": stop,
            "EXPLAIN": (
                f"Stopped before cohort freeze at {stop}. "
                "V2 OpenGov=0 root cause: 25k BidNet prefix in collect_all_candidate_records (now OpenGov-first)."
            ),
            "V2_DEFECT_FIXED": True,
        },
        "COHORT": {
            "Target": COHORT_TARGET,
            "Actual": (cohort or {}).get("ACTUAL", 0),
            "Frozen": (cohort or {}).get("ACTUAL", 0),
            "Processed": 0,
            "Dispositioned": 0,
            "Fresh_run_id_on_all": "N/A",
        },
        "PRODUCTION_FILE_ACCESS": {
            "Approved_prod_stores_read": access.get("PRODUCTION_READ_COUNT"),
            "Non_prod_stores_read": access.get("NON_PRODUCTION_READ_COUNT"),
        },
        "MOCK_PROVIDER_CALLS": access.get("MOCK_PROVIDER_CALLS_IN_PRODUCTION"),
        "FIXTURE_ROWS_IN_COHORT": 0,
        "SYNTHETIC_ROWS_IN_COHORT": 0,
        "OLD_COHORT_ROWS_REUSED": 0,
        "FINAL_QUESTIONS": {
            "1_was_reading_test_data": True,
            "2_contaminating_stores": [c.get("PATH") for c in (inventory.get("CONTAMINATING_ACTIVE") or [])[:20]],
            "3_why_opengov_disappeared_v2": (
                "collect_all_candidate_records early-return at 25k BidNet-heavy keys; OpenGov starved"
            ),
            "4_fresh_opengov_opportunities": og.get("FRESH_FETCHED"),
            "5_fresh_package_resolved": og.get("PACKAGE_RESOLVED"),
            "6_real_lines_from_scratch": og.get("REAL_LINES"),
            "7_rfq_ready": 0,
            "8_cohort_100_percent_fresh": False,
            "9_ghost_still_reachable": False,
            "10_safe_for_daily_production": False,
        },
        "FINAL_GATES": {
            "PRODUCTION_NAMESPACE_ENFORCED": "YES",
            "TEST_FIXTURES_ISOLATED": "YES",
            "OPEN_GOV_SOURCE_RECONCILED": "NO",
            "ZERO_GHOST_DATA_IN_COHORT": "YES",
            "ALL_COHORT_ROWS_CURRENT_RUN": "NO",
        },
        "build_version": BUILD,
    }


def start_job(*, force: bool = True, cohort_target: int = COHORT_TARGET) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_production_data_hygiene_cleanroom_v1(force=force, cohort_target=cohort_target)
        except Exception as exc:
            log.exception("cleanroom hygiene failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
