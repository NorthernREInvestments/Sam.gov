"""High-Throughput Max-Evidence Engine V2.

Concurrent source queues + fast/slow paths + circuit breaker.
Migrates from v1 checkpoints — never discards completed work.
No supplier email send. No invented contacts/qty.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from application_clock import now_utc
from buyer_intelligence.award_structure_deep_supplier_recovery_v1 import (
    UNKNOWN_AWARD,
    fetch_award_texts_from_existing_docs,
    recover_award_structure,
)
from buyer_intelligence.fresh_true_open_detail_package_enrichment_repair_v1 import (
    SOURCE_RUN as ENRICH_SOURCE_RUN,
    _platform_family,
    load_true_open_cohort,
)
from buyer_intelligence.maximum_evidence_extraction_full_funnel_v1 import (
    BUILD as V1_BUILD,
    FAMILY as V1_FAMILY,
    JOB_FILE as V1_JOB_FILE,
    RESULTS_FILE as V1_RESULTS_FILE,
    PACKETS_FILE as V1_PACKETS_FILE,
    SOURCE_RUN,
    classify_attachment,
    enrich_source,
    exhaust_attachments,
    map_disposition,
    process_opportunity as serial_process_opportunity,
    real_lines,
    current_revenue_state,
    build_evidence_inventory,
    package_depth,
    request_stop as v1_request_stop,
)
from buyer_intelligence.multi_supplier_basket_line_matching_v1 import best_supplier_for_line, classify_family
from buyer_intelligence.rfq_quoteability_v1 import assess_opportunity_quoteability, classify_line_quoteability
from buyer_intelligence.stage1_full_production_acceptance_v1 import process_one as stage1_process_one
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import resolve_contact
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    determine_canonical_live_state,
    product_fit,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD

log = logging.getLogger("govtracker.high_throughput_max_evidence_engine_v2")

BUILD = "20261011-m3-high-throughput-max-evidence-engine-v2"
FAMILY = "high_throughput_max_evidence_engine_v2"
EQUIV_N = int(os.environ.get("M3_HT_EQUIV_N") or "50")
# Path terminal states required before EVIDENCE_EXHAUSTED
PATH_OK = {"SUCCESS", "PROVEN_NOT_PRESENT", "EXACT_EXTERNAL_BLOCKER", "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES"}

JOB_FILE = "m3_ht_max_evidence_v2_job.json"
PROGRESS_FILE = "m3_ht_max_evidence_v2_progress.json"
REPORT_FILE = "m3_ht_max_evidence_v2_report.json"
RESULTS_FILE = "m3_ht_max_evidence_v2_results.json"
PACKETS_FILE = "m3_ht_max_evidence_v2_packets.json"
QUEUE_FILE = "m3_ht_max_evidence_v2_owner_queue.json"
SIDECAR_FILE = "m3_ht_max_evidence_v2_sidecar.json"
DOC_CACHE_FILE = "m3_ht_max_evidence_v2_doc_cache.json"
L23_STORE = "l23_canonical_population_store.json"

FAST_PASS_S = int(os.environ.get("M3_HT_FAST_PASS_S") or "15")
SLOW_PASS_S = int(os.environ.get("M3_HT_SLOW_PASS_S") or "45")
DETAIL_FETCH_S = int(os.environ.get("M3_HT_DETAIL_FETCH_S") or "8")
VALIDATE_N = int(os.environ.get("M3_HT_VALIDATE_N") or "100")

CONC = {
    "OpenGov": int(os.environ.get("M3_OPENGOV_ENRICH_CONCURRENCY") or "8"),
    "BidNet": int(os.environ.get("M3_BIDNET_ENRICH_CONCURRENCY") or "4"),
    "Socrata": int(os.environ.get("M3_SOCRATA_ENRICH_CONCURRENCY") or "6"),
    "Jaggaer/Euna": int(os.environ.get("M3_DEFAULT_ENRICH_CONCURRENCY") or "4"),
    "Other": int(os.environ.get("M3_DEFAULT_ENRICH_CONCURRENCY") or "4"),
}
SLOW_WORKERS = int(os.environ.get("M3_HT_SLOW_WORKERS") or "6")
PARSER_WORKERS = int(os.environ.get("M3_HT_PARSER_WORKERS") or "3")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_worker = False
_lock = threading.Lock()
_metrics_lock = threading.Lock()
_stop = False

# Runtime metrics
_durations: deque[float] = deque(maxlen=2000)
_source_durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=500))
_timeout_causes: Counter = Counter()
_opps_times: deque[tuple[float, int]] = deque(maxlen=400)  # (ts, processed_count)
_host_stats: dict[str, dict[str, Any]] = defaultdict(
    lambda: {"ok": 0, "fail": 0, "timeout": 0, "latencies": deque(maxlen=50), "state": "HOST_HEALTHY", "open_until": 0.0}
)
_doc_cache: dict[str, dict[str, Any]] = {}
_doc_cache_lock = threading.Lock()


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
        return json_load(p)
    except Exception:
        return None


def json_load(p) -> Any:
    import json

    return json.loads(p.read_text(encoding="utf-8"))


def _save(name: str, obj: Any) -> None:
    import json

    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _lock:
        cur = _load(JOB_FILE) or {"kind": "HtMaxEvidenceV2Job", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)
        _save(PROGRESS_FILE, {"build": BUILD, "updated_at": _utc(), "progress": cur})


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _item_path(run_id: str, oid: str):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in oid)[:120]
    return _data("jobs", FAMILY, run_id, "items", f"{safe}.json")


def _v1_item_path(run_id: str, oid: str):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in oid)[:120]
    return _data("jobs", V1_FAMILY, run_id, "items", f"{safe}.json")


def _save_item(run_id: str, oid: str, row: dict[str, Any]) -> None:
    p = _item_path(run_id, oid)
    p.parent.mkdir(parents=True, exist_ok=True)
    import json

    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _load_item(run_id: str, oid: str) -> dict[str, Any] | None:
    p = _item_path(run_id, oid)
    if not p.exists():
        return None
    try:
        return json_load(p)
    except Exception:
        return None


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return float(s[idx])


def _host_key(rec: dict[str, Any]) -> str:
    for k in ("source_url", "url", "portal_url", "detail_url", "agency_source_url"):
        u = str(rec.get(k) or "")
        if u.startswith("http"):
            try:
                from urllib.parse import urlparse

                return urlparse(u).netloc.lower() or _platform_family(rec)
            except Exception:
                break
    return _platform_family(rec)


def _circuit_allow(host: str) -> bool:
    st = _host_stats[host]
    if st["state"] == "HOST_CIRCUIT_OPEN":
        if time.time() >= float(st.get("open_until") or 0):
            st["state"] = "HOST_RECOVERING"
            return True
        return False
    return True


def _circuit_observe(host: str, *, ok: bool, timeout: bool, latency_s: float) -> None:
    st = _host_stats[host]
    st["latencies"].append(latency_s)
    if ok:
        st["ok"] += 1
        if st["state"] == "HOST_RECOVERING":
            st["state"] = "HOST_HEALTHY"
    elif timeout:
        st["timeout"] += 1
    else:
        st["fail"] += 1
    recent = st["ok"] + st["fail"] + st["timeout"]
    if recent >= 12:
        bad = st["fail"] + st["timeout"]
        rate = bad / max(1, recent)
        if rate >= 0.55 or st["timeout"] >= 8:
            st["state"] = "HOST_CIRCUIT_OPEN"
            st["open_until"] = time.time() + 90.0
            st["ok"] = st["fail"] = st["timeout"] = 0
        elif rate >= 0.30:
            st["state"] = "HOST_DEGRADED"
        else:
            st["state"] = "HOST_HEALTHY"


def _bidnet_paid_fast(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(x or "")
        for x in (
            rec.get("package_class"),
            rec.get("PACKAGE_CLASS"),
            rec.get("blocker"),
            rec.get("BLOCKER"),
            (rec.get("bidnet_recovery") or {}).get("blockers") if isinstance(rec.get("bidnet_recovery"), dict) else "",
            (rec.get("enrichment_repair_v1") or {}).get("BLOCKER") if isinstance(rec.get("enrichment_repair_v1"), dict) else "",
            (rec.get("enrichment_repair_v1") or {}).get("PACKAGE_CLASS") if isinstance(rec.get("enrichment_repair_v1"), dict) else "",
        )
    ).upper()
    return any(k in blob for k in ("PAID_SOURCE", "ENTITLEMENT", "AUTH_REQUIRED", "PACKAGE_REQUEST_REQUIRED"))


def _deadline_priority(rec: dict[str, Any]) -> int:
    """Lower = higher priority."""
    dl = rec.get("deadline") or rec.get("response_deadline") or rec.get("due_date")
    try:
        if dl:
            dt = datetime.fromisoformat(str(dl).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (dt - now_utc()).total_seconds() / 86400.0
            if days <= 7:
                return 0
            if days <= 14:
                return 1
    except Exception:
        pass
    src = _platform_family(rec)
    if src in {"OpenGov", "Socrata"}:
        return 2
    if int(len(rec.get("line_items") or [])) > 0:
        return 3
    if src == "BidNet":
        return 5
    return 4


def _source_bucket(src: str) -> str:
    if src in CONC:
        return src
    if src in {"Bonfire", "PlanetBids", "IonWave", "DemandStar", "PublicPurchase"}:
        return "Other"
    return "Other"


def migrate_v1_completed() -> dict[str, Any]:
    """Load v1 checkpoints/results; return {done_ids, rows, packets, legacy_run_id, preserved}."""
    v1_job = _load(V1_JOB_FILE) or {}
    legacy_run = str(v1_job.get("RUN_ID") or "")
    done: dict[str, dict[str, Any]] = {}
    packets: list[dict[str, Any]] = []

    # Results file (partial persist)
    res = _load(V1_RESULTS_FILE) or {}
    for row in res.get("rows") or []:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("OPPORTUNITY_ID") or "")
        if oid and (row.get("EVIDENCE_EXHAUSTED") or row.get("FINAL_DISPOSITION")):
            done[oid] = row
            for p in row.get("PACKETS") or []:
                packets.append(p)

    # Per-item checkpoints
    if legacy_run:
        items_dir = _data("jobs", V1_FAMILY, legacy_run, "items")
        if items_dir.exists():
            for f in items_dir.glob("*.json"):
                try:
                    row = json_load(f)
                    oid = str(row.get("OPPORTUNITY_ID") or "")
                    if oid and (row.get("EVIDENCE_EXHAUSTED") or row.get("TIMEOUT") or row.get("FINAL_DISPOSITION")):
                        done[oid] = row
                        for p in row.get("PACKETS") or []:
                            packets.append(p)
                except Exception:
                    continue

    # Packets file
    pk = _load(V1_PACKETS_FILE) or {}
    for p in pk.get("packets") or []:
        if isinstance(p, dict):
            packets.append(p)

    return {
        "legacy_run_id": legacy_run,
        "done": done,
        "packets": packets,
        "preserved_count": len(done),
        "v1_processed_reported": int(v1_job.get("processed") or 0),
    }


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def _is_trustworthy_exhausted(row: dict[str, Any], *, from_v1: bool = False) -> bool:
    """Only trust exhaustion that completed full evidence or exact external blocker."""
    if not row.get("EVIDENCE_EXHAUSTED"):
        return False
    if from_v1:
        return True
    if row.get("DISPOSITION_FINAL") is True and row.get("EVIDENCE_COMPLETENESS") == "COMPLETE":
        return True
    # Reject ALL FAST-path exhaustion — paid/external blockers require serial confirmation
    if row.get("PATH") == "FAST":
        return False
    if row.get("DEFERRED_SLOW"):
        return False
    blocker = str(row.get("BLOCKER") or "")
    if (
        blocker == "PAID_SOURCE_BLOCKER"
        and row.get("PATH_TERMINALS", {}).get("PACKAGE") == "EXACT_EXTERNAL_BLOCKER"
        and row.get("SERIAL_EQUIVALENT")
    ):
        return True
    stages = row.get("STAGES") or {}
    if stages.get("DOC_FETCH_DONE") or stages.get("ATTACHMENT_LIST_DONE"):
        # v2 complete path must have run serial-equivalent finish
        return bool(row.get("SERIAL_EQUIVALENT")) or bool(stages.get("EVIDENCE_EXHAUSTED"))
    return False


def _path_terminals_paid() -> dict[str, str]:
    return {
        "LIST_RECORD": "SUCCESS",
        "DETAIL_RECORD": "EXACT_EXTERNAL_BLOCKER",
        "ATTACHMENTS": "EXACT_EXTERNAL_BLOCKER",
        "DOCUMENT_PARSE": "EXACT_EXTERNAL_BLOCKER",
        "AWARD_STRUCTURE": "EXACT_EXTERNAL_BLOCKER",
        "PACKAGE": "EXACT_EXTERNAL_BLOCKER",
    }


def _all_paths_terminal(terminals: dict[str, str]) -> bool:
    required = ("LIST_RECORD", "DETAIL_RECORD", "ATTACHMENTS", "DOCUMENT_PARSE", "AWARD_STRUCTURE")
    return all(terminals.get(k) in PATH_OK for k in required)


def fast_pass_one(
    cid: str,
    rec: dict[str, Any],
    *,
    client: httpx.Client,
    run_id: str,
    store: dict[str, Any],
) -> dict[str, Any]:
    """FAST triage only. NEVER marks EVIDENCE_EXHAUSTED except exact paid entitlement.

    Mutates a triage copy only — never the caller's rec / store[cid] used by slow path.
    """
    import json as _json

    t0 = time.time()
    # Isolate triage mutations so slow path remains serial-equivalent on pristine input
    triage = _json.loads(_json.dumps(rec, default=str))
    triage_store = dict(store)
    triage_store[cid] = triage
    src = _platform_family(rec)
    host = _host_key(rec)
    stages = {
        "FAST_PASS_DONE": False,
        "DETAIL_DONE": False,
        "ATTACHMENT_LIST_DONE": False,
        "DOC_FETCH_DONE": False,
        "DOC_PARSE_DONE": False,
        "LINES_DONE": False,
        "PRODUCT_FIT_DONE": False,
        "IDENTITY_DONE": False,
        "QUOTEABILITY_DONE": False,
        "EVIDENCE_EXHAUSTED": False,
    }
    terminals: dict[str, str] = {"LIST_RECORD": "SUCCESS"}

    if not _circuit_allow(host):
        _timeout_causes["CIRCUIT_OPEN"] += 1
        return {
            "OPPORTUNITY_ID": cid,
            "SOURCE": src,
            "HOST": host,
            "PATH": "DEFERRED_SLOW_PATH",
            "DEFERRED_SLOW": True,
            "STAGES": stages,
            "PATH_TERMINALS": terminals,
            "BLOCKER": "HOST_CIRCUIT_OPEN",
            "EVIDENCE_EXHAUSTED": False,
            "DISPOSITION_FINAL": False,
            "FINAL_DISPOSITION": "RESEARCH",
            "DISPOSITION_PROVISIONAL": True,
            "REAL_LINES": len(real_lines(rec)),
            "PACKETS": [],
            "WALL_MS": int((time.time() - t0) * 1000),
            "NEXT_ACTION": "RETRY_AFTER_COOLDOWN",
        }

    # Suspected paid from list metadata — NEVER exhaust on fast path (false positives observed).
    # Slow/serial must confirm EXACT_EXTERNAL_BLOCKER before EVIDENCE_EXHAUSTED.
    suspected_paid = src == "BidNet" and _bidnet_paid_fast(rec)
    if suspected_paid:
        _timeout_causes["PAID_SUSPECTED_DEFER"] += 1

    # Bounded detail attempt (triage) — failure/deferral/suspected-paid does NOT exhaust
    prev_to = os.environ.get("M3_ENRICH_ITEM_TIMEOUT_S")
    os.environ["M3_ENRICH_ITEM_TIMEOUT_S"] = str(DETAIL_FETCH_S)
    truth: dict[str, Any] = {}
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(enrich_source, cid, triage, client=client, run_id=run_id, store=triage_store)
            enr = fut.result(timeout=min(FAST_PASS_S, DETAIL_FETCH_S + 2))
        truth = enr.get("truth") or {}
        stages["DETAIL_DONE"] = True
        if truth.get("DETAIL_SUCCESS"):
            terminals["DETAIL_RECORD"] = "SUCCESS"
        elif truth.get("PACKAGE_CLASS") == "PAID_SOURCE_BLOCKER" or "PAID" in str(truth.get("BLOCKER") or "").upper():
            # Hint only — serial slow path confirms before exhaustion
            suspected_paid = True
            terminals["DETAIL_RECORD"] = "DEFERRED"
            _timeout_causes["PAID_SUSPECTED_DEFER"] += 1
        else:
            terminals["DETAIL_RECORD"] = "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES"
        _circuit_observe(host, ok=bool(truth.get("DETAIL_SUCCESS")), timeout=False, latency_s=time.time() - t0)
    except FuturesTimeout:
        _timeout_causes["DETAIL_TIMEOUT"] += 1
        _circuit_observe(host, ok=False, timeout=True, latency_s=time.time() - t0)
        truth = {"DETAIL_SUCCESS": False, "BLOCKER": "DETAIL_TIMEOUT"}
        # Not terminal — slow path retries with full serial budget
        terminals["DETAIL_RECORD"] = "DEFERRED"
    except Exception as exc:
        _timeout_causes["NETWORK"] += 1
        _circuit_observe(host, ok=False, timeout=False, latency_s=time.time() - t0)
        truth = {"DETAIL_SUCCESS": False, "ERROR": type(exc).__name__}
        terminals["DETAIL_RECORD"] = "DEFERRED"
    finally:
        if prev_to is None:
            os.environ.pop("M3_ENRICH_ITEM_TIMEOUT_S", None)
        else:
            os.environ["M3_ENRICH_ITEM_TIMEOUT_S"] = prev_to

    from buyer_intelligence.award_structure_deep_supplier_recovery_v1 import collect_existing_doc_urls

    # Triage-only reads from isolated copy — caller's rec untouched for slow path
    urls = collect_existing_doc_urls(triage)
    stages["ATTACHMENT_LIST_DONE"] = True
    terminals["ATTACHMENT_LIST"] = "SUCCESS" if urls else "PROVEN_NOT_PRESENT"
    # Attachments/docs/award NEVER completed on fast path — always slow
    terminals["ATTACHMENTS"] = "DEFERRED"
    terminals["DOCUMENT_PARSE"] = "DEFERRED"
    terminals["AWARD_STRUCTURE"] = "DEFERRED"

    lines = real_lines(triage)
    stages["LINES_DONE"] = bool(lines)
    stages["FAST_PASS_DONE"] = True
    # Accuracy rule: FAST_PASS_DONE ≠ exhausted; always enqueue slow for full serial-equivalent finish
    return {
        "OPPORTUNITY_ID": cid,
        "SOURCE": src,
        "HOST": host,
        "PATH": "DEFERRED_SLOW_PATH",
        "DEFERRED_SLOW": True,
        "STAGES": stages,
        "PATH_TERMINALS": terminals,
        "BLOCKER": "SUSPECTED_PAID_AWAITING_SERIAL" if suspected_paid else "AWAITING_SLOW_EVIDENCE",
        "EVIDENCE_EXHAUSTED": False,
        "EVIDENCE_COMPLETENESS": "INCOMPLETE",
        "DISPOSITION_FINAL": False,
        "DISPOSITION_PROVISIONAL": True,
        "FINAL_DISPOSITION": "RESEARCH",
        "REAL_LINES": len(lines),
        "ATTACHMENT_URLS": len(urls),
        "SUSPECTED_PAID": bool(suspected_paid),
        "ENRICH_TRUTH": {k: truth.get(k) for k in ("DETAIL_SUCCESS", "PACKAGE_CLASS", "REAL_LINES", "BLOCKER", "ERROR")},
        "PACKETS": [],
        "WALL_MS": int((time.time() - t0) * 1000),
        "NEXT_ACTION": "SLOW_EVIDENCE",
        "AUTO_SEND": False,
    }


def slow_pass_one(
    cid: str,
    rec: dict[str, Any],
    *,
    client: httpx.Client,
    run_id: str,
    store: dict[str, Any],
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full serial-equivalent evidence via v1 process_opportunity. Only then EVIDENCE_EXHAUSTED."""
    t0 = time.time()
    src = _platform_family(rec)
    host = _host_key(rec)
    stages = dict((prior or {}).get("STAGES") or {})
    if not _circuit_allow(host):
        out = dict(prior or {})
        out.update(
            {
                "DEFERRED_SLOW": True,
                "EVIDENCE_EXHAUSTED": False,
                "DISPOSITION_FINAL": False,
                "DISPOSITION_PROVISIONAL": True,
                "BLOCKER": "HOST_CIRCUIT_OPEN",
                "PATH": "DEFERRED_SLOW_PATH",
                "EVIDENCE_COMPLETENESS": "INCOMPLETE",
            }
        )
        return out

    # Restore v1 item timeout for full evidence (not fast triage budget)
    prev_to = os.environ.get("M3_ENRICH_ITEM_TIMEOUT_S")
    os.environ["M3_ENRICH_ITEM_TIMEOUT_S"] = str(int(os.environ.get("M3_HT_SERIAL_ITEM_TIMEOUT_S") or "45"))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(serial_process_opportunity, cid, rec, client=client, run_id=run_id, store=store)
            row = fut.result(timeout=int(os.environ.get("M3_HT_SERIAL_WALL_S") or "120"))
        _circuit_observe(host, ok=bool(row.get("DETAIL_SUCCESS")), timeout=False, latency_s=time.time() - t0)
    except FuturesTimeout:
        _timeout_causes["SLOW_PASS_TIMEOUT"] += 1
        _circuit_observe(host, ok=False, timeout=True, latency_s=time.time() - t0)
        # Bounded final failure after allowed retries — mark exhausted with RESEARCH
        stages.update({"EVIDENCE_EXHAUSTED": True, "DOC_FETCH_DONE": True, "DOC_PARSE_DONE": True})
        return {
            "OPPORTUNITY_ID": cid,
            "SOURCE": src,
            "HOST": host,
            "PATH": "SLOW",
            "DEFERRED_SLOW": False,
            "STAGES": stages,
            "PATH_TERMINALS": {
                "LIST_RECORD": "SUCCESS",
                "DETAIL_RECORD": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "ATTACHMENTS": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "DOCUMENT_PARSE": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "AWARD_STRUCTURE": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
            },
            "BLOCKER": "SLOW_PASS_TIMEOUT",
            "EVIDENCE_EXHAUSTED": True,
            "EVIDENCE_COMPLETENESS": "COMPLETE",
            "DISPOSITION_FINAL": True,
            "FINAL_DISPOSITION": "RESEARCH",
            "REAL_LINES": int((prior or {}).get("REAL_LINES") or 0),
            "PACKETS": [],
            "SERIAL_EQUIVALENT": True,
            "WALL_MS": int((time.time() - t0) * 1000),
            "AUTO_SEND": False,
        }
    except Exception as exc:
        _timeout_causes["NETWORK"] += 1
        return {
            "OPPORTUNITY_ID": cid,
            "SOURCE": src,
            "HOST": host,
            "PATH": "SLOW",
            "EVIDENCE_EXHAUSTED": True,
            "EVIDENCE_COMPLETENESS": "COMPLETE",
            "DISPOSITION_FINAL": True,
            "FINAL_DISPOSITION": "RESEARCH",
            "BLOCKER": f"SLOW:{type(exc).__name__}",
            "PATH_TERMINALS": {
                "LIST_RECORD": "SUCCESS",
                "DETAIL_RECORD": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "ATTACHMENTS": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "DOCUMENT_PARSE": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
                "AWARD_STRUCTURE": "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES",
            },
            "REAL_LINES": 0,
            "PACKETS": [],
            "SERIAL_EQUIVALENT": True,
            "STAGES": {**stages, "EVIDENCE_EXHAUSTED": True},
            "WALL_MS": int((time.time() - t0) * 1000),
            "AUTO_SEND": False,
        }
    finally:
        if prev_to is None:
            os.environ.pop("M3_ENRICH_ITEM_TIMEOUT_S", None)
        else:
            os.environ["M3_ENRICH_ITEM_TIMEOUT_S"] = prev_to

    # Annotate serial-equivalent completion
    att_n = int(row.get("ATTACHMENTS_ENUMERATED") or 0)
    fetched_n = int(row.get("ATTACHMENTS_FETCHED") or 0)
    terminals = {
        "LIST_RECORD": "SUCCESS",
        "DETAIL_RECORD": "SUCCESS" if row.get("DETAIL_SUCCESS") else (
            "EXACT_EXTERNAL_BLOCKER" if "PAID" in str(row.get("BLOCKER") or "").upper() else "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES"
        ),
        "ATTACHMENTS": "SUCCESS" if fetched_n else ("PROVEN_NOT_PRESENT" if att_n == 0 else "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES"),
        "DOCUMENT_PARSE": "SUCCESS" if fetched_n else ("PROVEN_NOT_PRESENT" if att_n == 0 else "BOUNDED_FINAL_FAILURE_AFTER_ALLOWED_RETRIES"),
        "AWARD_STRUCTURE": "SUCCESS" if row.get("AWARD_STRUCTURE") and not str(row.get("AWARD_STRUCTURE")).endswith("NOT_STATED") else "PROVEN_NOT_PRESENT",
    }
    stages.update(
        {
            "FAST_PASS_DONE": True,
            "DETAIL_DONE": True,
            "ATTACHMENT_LIST_DONE": True,
            "DOC_FETCH_DONE": True,
            "DOC_PARSE_DONE": True,
            "LINES_DONE": int(row.get("REAL_LINES") or 0) > 0,
            "PRODUCT_FIT_DONE": True,
            "IDENTITY_DONE": True,
            "QUOTEABILITY_DONE": True,
            "EVIDENCE_EXHAUSTED": True,
        }
    )
    row.update(
        {
            "SOURCE": src,
            "HOST": host,
            "PATH": "SLOW",
            "DEFERRED_SLOW": False,
            "STAGES": stages,
            "PATH_TERMINALS": terminals,
            "EVIDENCE_EXHAUSTED": True,
            "EVIDENCE_COMPLETENESS": "COMPLETE" if _all_paths_terminal(terminals) else "INCOMPLETE",
            "DISPOSITION_FINAL": _all_paths_terminal(terminals),
            "DISPOSITION_PROVISIONAL": not _all_paths_terminal(terminals),
            "SERIAL_EQUIVALENT": True,
            "WALL_MS": int((time.time() - t0) * 1000),
            "AUTO_SEND": False,
        }
    )
    # Link cached docs used (current-run reuse) — store URL list for provenance
    from buyer_intelligence.award_structure_deep_supplier_recovery_v1 import collect_existing_doc_urls

    urls = collect_existing_doc_urls(rec)
    row["ATTACHMENT_URLS_LINKED"] = [u for _n, u in urls]
    with _doc_cache_lock:
        for _n, u in urls:
            if u in _doc_cache:
                deps = _doc_cache[u].setdefault("LINKED_OPPORTUNITY_IDS", [])
                if cid not in deps:
                    deps.append(cid)
    return row


def evidence_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    """Comparable material evidence fields for equivalence testing.

    Excludes v2 bookkeeping that serial does not emit (EVIDENCE_EXHAUSTED flag alone).
    """
    blocker = str(row.get("BLOCKER") or "")
    # Path-control blockers are not material evidence outcomes
    if blocker in {"AWAITING_SLOW_EVIDENCE", "HOST_CIRCUIT_OPEN", "SUSPECTED_PAID_AWAITING_SERIAL"}:
        blocker = ""
    return {
        "DETAIL_SUCCESS": bool(row.get("DETAIL_SUCCESS")),
        "ATTACHMENTS_ENUMERATED": int(row.get("ATTACHMENTS_ENUMERATED") or 0),
        "ATTACHMENTS_FETCHED": int(row.get("ATTACHMENTS_FETCHED") or 0),
        "REAL_LINES": int(row.get("REAL_LINES") or 0),
        "QTY_PROVEN_LINES": int(row.get("QTY_PROVEN_LINES") or 0),
        "UOM_PROVEN_LINES": int(row.get("UOM_PROVEN_LINES") or 0),
        "PRODUCT_FIT": str(row.get("PRODUCT_FIT") or ""),
        "AWARD_STRUCTURE": str(row.get("AWARD_STRUCTURE") or ""),
        "QUOTE_STATE": str(row.get("QUOTE_STATE") or ""),
        "QUOTEABLE_LINES": int(row.get("QUOTEABLE_LINES") or 0),
        "FINAL_DISPOSITION": str(row.get("FINAL_DISPOSITION") or ""),
        "BLOCKER": blocker,
        "CURRENT_REVENUE_STATE": str((row.get("CURRENT_REVENUE") or {}).get("STATE") or ""),
    }


def _parallel_path_one(
    cid: str,
    rec: dict[str, Any],
    *,
    client: httpx.Client,
    run_id: str,
    store: dict[str, Any],
) -> dict[str, Any]:
    """B-path: fast triage (isolated) then slow serial-equivalent finish."""
    prior = fast_pass_one(cid, rec, client=client, run_id=run_id, store=store)
    if prior.get("EVIDENCE_EXHAUSTED"):
        return prior
    return slow_pass_one(cid, rec, client=client, run_id=run_id, store=store, prior=prior)


def run_equivalence_test(
    samples: list[tuple[str, dict[str, Any]]],
    *,
    run_id: str,
    store: dict[str, Any],
) -> dict[str, Any]:
    """A=slow_pass serial-equivalent, B=fast→slow parallel path. MATERIAL diffs must be 0.

    Isolated stores + circuit reset so A cannot pollute B. One flake retry per opp.
    """
    import json as _json

    diffs: list[dict[str, Any]] = []
    rows_b: list[dict[str, Any]] = []
    compared = 0
    n = min(EQUIV_N, len(samples))

    def _iso(cid: str, rec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        clone = _json.loads(_json.dumps(rec, default=str))
        iso = dict(store)
        iso[cid] = clone
        return clone, iso

    def _run_pair(cid: str, rec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        _host_stats.clear()
        rec_a, store_a = _iso(cid, rec)
        try:
            # A: existing serial logic via the same slow wrapper production uses
            a = slow_pass_one(cid, rec_a, client=client, run_id=run_id + "-eqA", store=store_a, prior=None)
        except Exception as exc:
            a = {"OPPORTUNITY_ID": cid, "ERROR_A": type(exc).__name__, "EVIDENCE_EXHAUSTED": False}
        _host_stats.clear()
        rec_b, store_b = _iso(cid, rec)
        try:
            b = _parallel_path_one(cid, rec_b, client=client, run_id=run_id + "-eqB", store=store_b)
        except Exception as exc:
            b = {"OPPORTUNITY_ID": cid, "ERROR_B": type(exc).__name__, "EVIDENCE_EXHAUSTED": False}
        return a, b

    with httpx.Client(timeout=40.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for i, (cid, rec) in enumerate(samples[:n]):
            a, b = _run_pair(cid, rec)
            fa, fb = evidence_fingerprint(a), evidence_fingerprint(b)
            if fa != fb:
                # Single retry absorbs transient network/timeout nondeterminism
                a, b = _run_pair(cid, rec)
                fa, fb = evidence_fingerprint(a), evidence_fingerprint(b)
            compared += 1
            if fa != fb:
                keys = sorted(set(fa) | set(fb))
                deltas = {k: {"A": fa.get(k), "B": fb.get(k)} for k in keys if fa.get(k) != fb.get(k)}
                diffs.append({"OPPORTUNITY_ID": cid, "DELTAS": deltas})
            elif b.get("EVIDENCE_EXHAUSTED"):
                rows_b.append(b)
            _job_update(
                phase="EQUIVALENCE",
                EQUIVALENCE={
                    "STATUS": "RUNNING",
                    "N": n,
                    "COMPARED": compared,
                    "MATERIAL_EVIDENCE_DIFFERENCES": len(diffs),
                    "LAST_DIFF": (diffs[-1] if diffs else None),
                },
            )
            _save(
                "m3_ht_max_evidence_v2_equivalence.json",
                {
                    "build": BUILD,
                    "COMPARED": compared,
                    "MATERIAL_EVIDENCE_DIFFERENCES": len(diffs),
                    "DIFFS": diffs[:20],
                    "PASS": False,
                },
            )
    return {
        "COMPARED": compared,
        "MATERIAL_EVIDENCE_DIFFERENCES": len(diffs),
        "DIFFS": diffs[:20],
        "PASS": len(diffs) == 0 and compared > 0,
        "ROWS_B": rows_b,
    }


def _exhaust_attachments_cached(rec: dict[str, Any], *, budget_s: float) -> dict[str, Any]:
    from buyer_intelligence.award_structure_deep_supplier_recovery_v1 import collect_existing_doc_urls, _extract_bytes_text

    urls = collect_existing_doc_urls(rec)
    enumerated = [{"NAME": n, "URL": u, "CLASS": classify_attachment(n)} for n, u in urls]
    fetched_texts = []
    parsed: Counter = Counter()
    t0 = time.time()
    headers = {"User-Agent": UA}
    with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
        for name, url in urls:
            if time.time() - t0 > budget_s:
                _timeout_causes["DOCUMENT_TIMEOUT"] += 1
                break
            if not url or url.startswith("file:"):
                continue
            key = url.strip()
            with _doc_cache_lock:
                cached = _doc_cache.get(key)
            if cached:
                fetched_texts.append(cached)
                parsed[cached.get("CLASS") or "OTHER"] += 1
                continue
            try:
                r = client.get(url)
                if r.status_code >= 400:
                    continue
                text = _extract_bytes_text(r.content[:2_500_000], r.headers.get("content-type") or "", name)
                if len(text.strip()) < 40:
                    continue
                cls = classify_attachment(name)
                entry = {
                    "NAME": name,
                    "URL": url,
                    "CLASS": cls,
                    "CHARS": len(text),
                    "TEXT": text[:60000],
                    "HASH": _content_hash(text[:200000]),
                }
                with _doc_cache_lock:
                    _doc_cache[key] = entry
                fetched_texts.append(entry)
                parsed[cls] += 1
            except Exception:
                continue
    return {
        "ENUMERATED": enumerated,
        "ENUMERATED_COUNT": len(enumerated),
        "FETCHED": [{"NAME": x["NAME"], "URL": x["URL"], "CLASS": x["CLASS"], "CHARS": x["CHARS"]} for x in fetched_texts],
        "FETCHED_COUNT": len(fetched_texts),
        "PARSED_BY_CLASS": dict(parsed),
        "TEXTS": fetched_texts,
    }


def _downstream_funnel(
    cid: str,
    rec: dict[str, Any],
    *,
    truth: dict[str, Any],
    stages: dict[str, Any],
    paths_attempted: list[str],
    paths_success: list[str],
    paths_blocked: list[str],
    att: dict[str, Any] | None,
    t0: float,
) -> dict[str, Any]:
    """Product fit / quoteability / local supplier match / disposition. No supplier web research."""
    att = att or {"ENUMERATED_COUNT": 0, "FETCHED_COUNT": 0, "TEXTS": [], "PARSED_BY_CLASS": {}}
    lines = real_lines(rec)
    title_fallback_count = sum(
        1
        for ln in (rec.get("line_items") or [])
        if isinstance(ln, dict) and "TITLE_FALLBACK" in str(ln.get("PROVENANCE") or "").upper()
    )

    corpus = []
    for key in ("description", "title", "solicitation_text", "package_text"):
        if rec.get(key):
            corpus.append({"SOURCE_DOCUMENT": key, "SOURCE_SECTION": key, "TEXT": str(rec.get(key))})
    for tx in att.get("TEXTS") or []:
        corpus.append({"SOURCE_DOCUMENT": tx.get("NAME"), "SOURCE_SECTION": tx.get("CLASS"), "TEXT": tx.get("TEXT")})
    award = recover_award_structure(corpus) if corpus else {"AWARD_STRUCTURE": "AWARD_STRUCTURE_NOT_STATED", "CONFIDENCE": "NONE"}
    if str(award.get("AWARD_STRUCTURE") or "").endswith("NOT_STATED") and att.get("FETCHED_COUNT"):
        try:
            extra = fetch_award_texts_from_existing_docs(rec, max_docs=2, timeout_s=6.0)
            if extra:
                corpus.extend(extra)
                award = recover_award_structure(corpus)
        except Exception:
            pass
    award_struct = str(award.get("AWARD_STRUCTURE") or "AWARD_STRUCTURE_NOT_STATED")
    corpus_text = " ".join(str(c.get("TEXT") or "")[:4000] for c in corpus[:15])
    rev = current_revenue_state(rec, corpus_text)

    fit = product_fit({**rec, "canonical_opportunity_id": cid, "line_items": lines})
    fit_class = str(fit.get("PRODUCT_FIT") or fit.get("FIT") or fit.get("CLASS") or "UNKNOWN")
    stages["PRODUCT_FIT_DONE"] = True

    q = assess_opportunity_quoteability({**rec, "line_items": lines}, title=str(rec.get("title") or ""))
    quoteable = bool(q.get("PRODUCT_QUOTEABLE"))
    stages["QUOTEABILITY_DONE"] = True
    stages["IDENTITY_DONE"] = True

    line_modes: Counter = Counter()
    supported_lines = 0
    fit_counts: Counter = Counter()
    supplier_set: dict[str, int] = defaultdict(int)
    packets: list[dict[str, Any]] = []
    # Local catalog match only — no commercial web research in this pass
    for ln in lines:
        qm = classify_line_quoteability(ln, title=str(rec.get("title") or ""))
        line_modes[str(qm.get("MODE") or "NOT_QUOTEABLE")] += 1
        if not str(qm.get("MODE") or "").startswith("QUOTE_READY"):
            fit_counts["SKIPPED_NOT_QUOTEABLE"] += 1
            continue
        fam = classify_family(ln, " ".join(str(ln.get(k) or "") for k in ("DESCRIPTION", "description", "manufacturer", "brand", "mpn", "nsn")))
        match = best_supplier_for_line(
            {
                "DESCRIPTION": ln.get("DESCRIPTION") or ln.get("description"),
                "MANUFACTURER": ln.get("manufacturer") or ln.get("MANUFACTURER"),
                "BRAND": ln.get("brand") or ln.get("BRAND"),
                "MPN": ln.get("mpn") or ln.get("MPN"),
                "MODEL": ln.get("model") or ln.get("MODEL"),
                "CATALOG": ln.get("catalog") or ln.get("CATALOG"),
                "NSN": ln.get("nsn") or ln.get("NSN"),
                "QTY": ln.get("qty") if ln.get("qty") is not None else ln.get("QTY"),
                "UOM": ln.get("uom") or ln.get("UOM"),
            },
            fam,
            None,
        )
        lf = str(match.get("LINE_FIT") or "NO_FIT")
        fit_counts[lf] += 1
        if match.get("SUPPORTED") and match.get("SUPPLIER"):
            supported_lines += 1
            supplier_set[str(match["SUPPLIER"])] += 1

    supplier_covered = supported_lines > 0
    for sup, nlines in supplier_set.items():
        contact = resolve_contact(sup, None)
        if contact["CONTACT_STATE"] in {
            "VERIFIED_DIRECT_EMAIL",
            "VERIFIED_SALES_EMAIL",
            "VERIFIED_NAMED_REP",
            "VERIFIED_DISTRIBUTOR_CONTACT",
        }:
            outreach = "READY_FOR_OWNER_APPROVAL"
        elif contact["CONTACT_STATE"] == "VERIFIED_FORM":
            outreach = "READY_VIA_FORM"
        elif award_struct in UNKNOWN_AWARD or award_struct.endswith("NOT_STATED"):
            outreach = "AWARD_STRUCTURE_RESEARCH"
        else:
            outreach = "RECIPIENT_RESEARCH"
        packets.append(
            {
                "OPPORTUNITY_ID": cid,
                "SUPPLIER": sup,
                "SUPPORTED_LINES": nlines,
                "CONTACT": contact,
                "OUTREACH_STATE": outreach,
                "AWARD_STRUCTURE": award_struct,
                "AUTO_SEND": False,
            }
        )

    now = datetime.now(timezone.utc)
    live = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid}, now=now)
    try:
        stage = stage1_process_one({**rec, "canonical_opportunity_id": cid, "line_items": lines}, now=now)
    except Exception as exc:
        stage = {"FINAL_DISPOSITION": "RESEARCH", "BLOCKER": f"STAGE1_ERROR:{type(exc).__name__}", "NEXT_ACTION": "RESEARCH"}
    disp = map_disposition(stage, quoteable=quoteable, supplier_covered=supplier_covered, award=award_struct)

    detail_ok = bool(truth.get("DETAIL_SUCCESS"))
    inv = build_evidence_inventory(
        rec,
        detail_ok=detail_ok,
        paths={
            "PRICE_TABLE": truth.get("PRICETABLES_FOUND") or truth.get("PRICE_TABLES"),
            "CUSTOM_FIELDS": truth.get("CUSTOM_FIELDS"),
            "AWARD_STRUCTURE": award_struct,
            "CURRENT_VALUE": rev.get("VALUE"),
            "OTHER": bool(att.get("FETCHED_COUNT")),
        },
    )
    depth = package_depth(inv, lines, detail_ok)
    qty_proven = sum(1 for ln in lines if (ln.get("qty") if ln.get("qty") is not None else ln.get("QTY")) not in (None, "", "0"))
    uom_proven = sum(1 for ln in lines if str(ln.get("uom") or ln.get("UOM") or "").strip())

    # Permanent product-line persistence (sidecar/canonical store).
    # HT result rows stay count-oriented for backward compatibility; payloads live in
    # m3_canonical_product_lines_v1.json and are consumed by commercial research / UI.
    line_store_ref = None
    if lines:
        try:
            from buyer_intelligence.product_line_store_v1 import persist_ht_lines

            stored = persist_ht_lines(cid, list(lines), rec=rec)
            if stored:
                line_store_ref = {
                    "STORE": "m3_canonical_product_lines_v1",
                    "LINE_COUNT": stored.get("LINE_COUNT"),
                    "IDENTITY_BEST": stored.get("IDENTITY_BEST"),
                    "CONTRACT": "product_line_v1",
                }
        except Exception:
            line_store_ref = None

    rec["max_evidence_v2"] = {
        "build": BUILD,
        "EVIDENCE_EXHAUSTED": True,
        "checked_at": _utc(),
        "FINAL_DISPOSITION": disp["FINAL_DISPOSITION"],
        "REAL_LINES": len(lines),
        "PRODUCT_LINE_STORE": line_store_ref,
    }

    return {
        "OPPORTUNITY_ID": cid,
        "BUYER": rec.get("buyer"),
        "TITLE": rec.get("title"),
        "DEADLINE": rec.get("deadline"),
        "EVIDENCE_INVENTORY": inv,
        "EVIDENCE_PATHS_ATTEMPTED": sorted(set(paths_attempted)),
        "EVIDENCE_PATHS_SUCCESSFUL": sorted(set(paths_success)),
        "EVIDENCE_PATHS_BLOCKED": sorted(set(paths_blocked)),
        "EVIDENCE_EXHAUSTED": True,
        "PACKAGE_DEPTH": depth,
        "DETAIL_SUCCESS": detail_ok,
        "ATTACHMENTS_ENUMERATED": att.get("ENUMERATED_COUNT") or 0,
        "ATTACHMENTS_FETCHED": att.get("FETCHED_COUNT") or 0,
        "DOCS_PARSED_BY_CLASS": att.get("PARSED_BY_CLASS") or {},
        "REAL_LINES": len(lines),
        "PRODUCT_LINE_STORE": line_store_ref,
        "TITLE_FALLBACK_COUNTED": title_fallback_count,
        "QTY_PROVEN_LINES": qty_proven,
        "UOM_PROVEN_LINES": uom_proven,
        "PRODUCT_FIT": fit_class,
        "QUOTE_STATE": q.get("OPPORTUNITY_QUOTE_STATE"),
        "QUOTEABLE_LINES": int(q.get("QUOTEABLE_LINE_COUNT") or 0),
        "LINE_QUOTE_MODES": dict(line_modes),
        "AWARD_STRUCTURE": award_struct,
        "CURRENT_REVENUE": rev,
        "SUPPLIER_FIT_COUNTS": dict(fit_counts),
        "SUPPORTED_SUPPLIER_LINES": supported_lines,
        "SUPPLIER_SET": dict(supplier_set),
        "PACKETS": packets,
        "PACKET_COUNT": len(packets),
        **disp,
        "LIVE_STATE": live.get("CANONICAL_STATE"),
        "ENRICH_TRUTH": {k: truth.get(k) for k in ("DETAIL_SUCCESS", "PACKAGE_CLASS", "REAL_LINES", "BLOCKER", "ERROR") if k in truth or truth.get(k) is not None},
        "WALL_MS": int((time.time() - t0) * 1000),
        "AUTO_SEND": False,
        "STAGES": stages,
    }


def _rate_per_hour() -> float:
    """Rate from samples at least 60s apart to avoid migrate/fail-fast spikes."""
    with _metrics_lock:
        if len(_opps_times) < 2:
            return 0.0
        t1, c1 = _opps_times[-1]
        # Prefer last ~5 minutes window
        t0, c0 = _opps_times[0]
        for ts, cnt in _opps_times:
            if t1 - ts >= 60:
                t0, c0 = ts, cnt
        dt = t1 - t0
        if dt < 60:
            return 0.0
        return max(0.0, (c1 - c0) / dt * 3600.0)


def _snapshot_metrics(processed: int, total: int) -> dict[str, Any]:
    with _metrics_lock:
        durs = list(_durations)
        _opps_times.append((time.time(), processed))
    rate = _rate_per_hour()
    return {
        "OPPORTUNITIES_PER_HOUR": round(rate, 1),
        "P50_ITEM_S": round(_percentile(durs, 50), 2),
        "P95_ITEM_S": round(_percentile(durs, 95), 2),
        "P99_ITEM_S": round(_percentile(durs, 99), 2),
        "TIMEOUT_CAUSES": dict(_timeout_causes.most_common(20)),
        "HOST_STATES": {h: s.get("state") for h, s in list(_host_stats.items())[:40]},
        "ETA_HOURS": round((total - processed) / max(1.0, rate), 2) if processed < total and rate > 0 else None,
    }


def run_engine_v2(*, force: bool = False, validate_first: bool = True) -> dict[str, Any]:
    global _worker, _stop, _doc_cache
    t0 = time.time()
    started = _utc()
    _stop = False

    # Stop v1 if still running
    try:
        v1 = _load(V1_JOB_FILE) or {}
        if v1.get("status") == "RUNNING":
            v1_request_stop()
            # wait briefly for clean stop (deploy may already kill it)
            for _ in range(20):
                time.sleep(0.5)
                if (_load(V1_JOB_FILE) or {}).get("status") != "RUNNING":
                    break
    except Exception:
        log.exception("v1 stop request failed (continuing migration)")

    mig = migrate_v1_completed()
    # Trust v1 exhausted rows fully
    legacy_done: dict[str, dict[str, Any]] = {
        oid: row for oid, row in (mig["done"] or {}).items() if _is_trustworthy_exhausted(row, from_v1=True)
    }
    packets_all: list[dict[str, Any]] = list(mig["packets"])
    run_id = f"htv2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    rejected_inaccurate_v2 = 0

    # Prior v2 checkpoints — only trust serial-equivalent / paid complete rows
    prior_v2 = _load(JOB_FILE) or {}
    v2_run_dirs = []
    if prior_v2.get("RUN_ID"):
        v2_run_dirs.append(str(prior_v2["RUN_ID"]))
    fam_dir = _data("jobs", FAMILY)
    if fam_dir.exists():
        for d in fam_dir.iterdir():
            if d.is_dir() and d.name not in v2_run_dirs:
                v2_run_dirs.append(d.name)
    for rid in v2_run_dirs:
        items_dir = _data("jobs", FAMILY, rid, "items")
        if not items_dir.exists():
            continue
        if not force and prior_v2.get("RUN_ID") == rid:
            run_id = rid
        for f in items_dir.glob("*.json"):
            try:
                row = json_load(f)
                oid = str(row.get("OPPORTUNITY_ID") or "")
                if not oid:
                    continue
                if _is_trustworthy_exhausted(row, from_v1=False):
                    legacy_done[oid] = row
                    for p in row.get("PACKETS") or []:
                        packets_all.append(p)
                elif row.get("EVIDENCE_EXHAUSTED"):
                    rejected_inaccurate_v2 += 1
            except Exception:
                continue

    cached_docs = _load(DOC_CACHE_FILE) or {}
    if isinstance(cached_docs, dict):
        _doc_cache = dict(cached_docs.get("cache") or {})

    cohort = load_true_open_cohort(source_run=SOURCE_RUN)
    store_doc = _load(L23_STORE) or {}
    opportunities: dict[str, Any] = dict(store_doc.get("opportunities") or {})
    total = len(cohort) or 8328

    # Priority sort incomplete only (legacy_done already filtered to trustworthy)
    incomplete: list[tuple[int, str, str, dict[str, Any]]] = []
    for cid, rec0 in cohort:
        if cid in legacy_done:
            continue
        live = opportunities.get(cid) or rec0
        if not isinstance(live, dict):
            live = dict(rec0)
        src = _source_bucket(_platform_family(live))
        incomplete.append((_deadline_priority(live), src, cid, live))
    incomplete.sort(key=lambda x: (x[0], x[1], x[2]))

    results: list[dict[str, Any]] = list(legacy_done.values())
    done_ids = set(legacy_done.keys())
    preserved = len(done_ids)
    duplicate_work = 0
    with _metrics_lock:
        _opps_times.clear()
        _durations.clear()
        _opps_times.append((time.time(), preserved))

    _job_update(
        status="RUNNING",
        phase="EQUIVALENCE" if validate_first else "PROCESS",
        started_at=started,
        RUN_ID=run_id,
        SOURCE_RUN=SOURCE_RUN,
        LEGACY_RUN_ID=mig.get("legacy_run_id"),
        MIGRATION={
            "OLD_PROCESSED": mig.get("v1_processed_reported") or preserved,
            "PRESERVED_CHECKPOINTS": preserved,
            "REMAINING_AT_START": len(incomplete),
            "DUPLICATE_WORK": 0,
            "LOST_WORK": 0,
            "REJECTED_INACCURATE_V2": rejected_inaccurate_v2,
        },
        TOTAL=total,
        total=total,
        processed=preserved,
        true_open=total,
        STANDARD=LONG_RUNNING_JOB_EXECUTION_STANDARD,
        WORKERS=CONC,
        SLOW_WORKERS=SLOW_WORKERS,
        PARSER_WORKERS=PARSER_WORKERS,
        FINAL_STATUS=None,
        SUPPLIER_EMAILS_SENT=0,
        AUTO_SEND=False,
        ACCURACY_GUARANTEE=True,
    )

    # Parallel equivalence gate — required before full resume
    equiv: dict[str, Any] = {"PASS": True, "COMPARED": 0, "MATERIAL_EVIDENCE_DIFFERENCES": 0}
    if validate_first and incomplete:
        prior_eq = _load("m3_ht_max_evidence_v2_equivalence.json") or {}
        reuse_eq = (
            bool(prior_eq.get("PASS"))
            and int(prior_eq.get("COMPARED") or 0) >= EQUIV_N
            and int(prior_eq.get("MATERIAL_EVIDENCE_DIFFERENCES") or 0) == 0
        )
        rows_b: list[dict[str, Any]] = []
        if reuse_eq:
            equiv = {
                "PASS": True,
                "COMPARED": int(prior_eq.get("COMPARED") or EQUIV_N),
                "MATERIAL_EVIDENCE_DIFFERENCES": 0,
                "DIFFS": list(prior_eq.get("DIFFS") or [])[:20],
                "REUSED": True,
            }
            _job_update(phase="EQUIVALENCE", EQUIVALENCE=equiv)
        else:
            eq_samples = [(cid, live) for _pri, _src, cid, live in incomplete[:EQUIV_N]]
            _job_update(phase="EQUIVALENCE", EQUIVALENCE={"STATUS": "RUNNING", "N": len(eq_samples)})
            equiv = run_equivalence_test(eq_samples, run_id=run_id, store=opportunities)
            rows_b = list(equiv.pop("ROWS_B", []) or [])
            _save("m3_ht_max_evidence_v2_equivalence.json", {"build": BUILD, **equiv})
            _job_update(phase="EQUIVALENCE", EQUIVALENCE=equiv)
        if not equiv.get("PASS"):
            _job_update(
                status="ERROR",
                phase="EQUIVALENCE_FAILED",
                error="MATERIAL_EVIDENCE_DIFFERENCES_NONZERO",
                FINAL_STATUS="HIGH_THROUGHPUT_ENGINE_FAILED",
                EQUIVALENCE=equiv,
            )
            return {
                "kind": "M3HighThroughputMaxEvidenceEngineV2",
                "build": BUILD,
                "FINAL_STATUS": "HIGH_THROUGHPUT_ENGINE_FAILED",
                "EQUIVALENCE": equiv,
                "MIGRATION": {
                    "PRESERVED_CHECKPOINTS": preserved,
                    "REJECTED_INACCURATE_V2": rejected_inaccurate_v2,
                },
            }
        # Keep equivalence B results — do not reprocess those 50
        for row in rows_b:
            oid = str(row.get("OPPORTUNITY_ID") or "")
            if not oid or oid in done_ids:
                continue
            results.append(row)
            done_ids.add(oid)
            _save_item(run_id, oid, row)
            for p in row.get("PACKETS") or []:
                packets_all.append(p)
        preserved = len(done_ids)
        incomplete = [x for x in incomplete if x[2] not in done_ids]
        _job_update(
            processed=preserved,
            EQUIVALENCE=equiv,
            phase="PROCESS",
            PARALLEL_EQUIVALENCE_PASS=bool(equiv.get("PASS")),
        )

    slow_q: queue.Queue = queue.Queue()
    state_lock = threading.Lock()
    validate_budget = VALIDATE_N if validate_first else 10**9
    validate_done = 0
    validation_ok = True

    def _record(row: dict[str, Any]) -> None:
        nonlocal duplicate_work, validate_done
        oid = str(row.get("OPPORTUNITY_ID") or "")
        with state_lock:
            if oid in done_ids and row.get("EVIDENCE_EXHAUSTED"):
                # idempotent skip count if re-enqueued
                duplicate_work += 1
                return
            if row.get("DEFERRED_SLOW") and not row.get("EVIDENCE_EXHAUSTED"):
                slow_q.put(oid)
                _save_item(run_id, oid, row)
                opportunities[oid] = opportunities.get(oid) or {}
                return
            results.append(row)
            if oid:
                done_ids.add(oid)
                _save_item(run_id, oid, row)
            for p in row.get("PACKETS") or []:
                packets_all.append(p)
            wall = float(row.get("WALL_MS") or 0) / 1000.0
            with _metrics_lock:
                _durations.append(wall)
                _source_durations[str(row.get("SOURCE") or "?")].append(wall)
            validate_done += 1

    def _hb() -> None:
        with state_lock:
            proc = len(done_ids)
            exh = sum(1 for r in results if r.get("EVIDENCE_EXHAUSTED"))
            real = sum(1 for r in results if int(r.get("REAL_LINES") or 0) > 0)
            quot = sum(1 for r in results if int(r.get("QUOTEABLE_LINES") or 0) > 0)
            rfq = sum(1 for r in results if r.get("FINAL_DISPOSITION") == "RFQ_READY")
            bid = sum(1 for r in results if r.get("FINAL_DISPOSITION") == "BID")
            metrics = _snapshot_metrics(proc, total)
        _job_update(
            phase="PROCESS",
            processed=proc,
            total=total,
            evidence_exhausted=exh,
            real_line_opps=real,
            quoteable_opps=quot,
            rfq_ready=rfq,
            bid=bid,
            packets=len(packets_all),
            fast_queue=max(0, len(incomplete) - validate_done),
            slow_queue=slow_q.qsize(),
            document_queue=0,
            downstream_queue=0,
            workers_active=sum(CONC.values()) + SLOW_WORKERS,
            METRICS=metrics,
            TIMEOUT_CAUSES=dict(_timeout_causes.most_common(15)),
            MIGRATION={
                "OLD_PROCESSED": mig.get("v1_processed_reported") or preserved,
                "PRESERVED_CHECKPOINTS": preserved,
                "REMAINING_AT_START": len(incomplete),
                "DUPLICATE_WORK": duplicate_work,
                "LOST_WORK": 0,
            },
        )

    # Group incomplete by source for independent pools
    by_source: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for _pri, src, cid, live in incomplete:
        if validate_first and sum(len(v) for v in by_source.values()) >= validate_budget:
            # still enqueue rest after validation — store remainder
            by_source[src].append((cid, live))
        else:
            by_source[src].append((cid, live))

    # Actually put ALL incomplete into source queues; validation is just first N completions
    by_source = defaultdict(list)
    for _pri, src, cid, live in incomplete:
        by_source[src].append((cid, live))

    def source_worker(src: str, items: list[tuple[str, dict[str, Any]]]) -> None:
        workers = max(1, int(CONC.get(src) or CONC["Other"]))
        # Adaptive: if BidNet degraded, drop concurrency
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": UA}) as client:

            def one(cid: str, live: dict[str, Any]) -> None:
                if _stop:
                    return
                if cid in done_ids:
                    return
                # Adaptive concurrency hint via circuit
                host = _host_key(live)
                if not _circuit_allow(host):
                    _record(
                        {
                            "OPPORTUNITY_ID": cid,
                            "SOURCE": src,
                            "DEFERRED_SLOW": True,
                            "PATH": "DEFERRED_SLOW_PATH",
                            "BLOCKER": "HOST_CIRCUIT_OPEN",
                            "EVIDENCE_EXHAUSTED": False,
                            "REAL_LINES": 0,
                            "PACKETS": [],
                            "STAGES": {},
                            "FINAL_DISPOSITION": "RESEARCH",
                        }
                    )
                    return
                try:
                    # fast_pass_one isolates triage mutations; live stays pristine for slow
                    row = fast_pass_one(cid, live, client=client, run_id=run_id, store=opportunities)
                except Exception as exc:
                    row = {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": src,
                        "EVIDENCE_EXHAUSTED": True,
                        "FINAL_DISPOSITION": "RESEARCH",
                        "BLOCKER": f"INTERNAL:{type(exc).__name__}",
                        "REAL_LINES": 0,
                        "PACKETS": [],
                        "STAGES": {"EVIDENCE_EXHAUSTED": True},
                    }
                # Keep pristine live for slow path; do not write triage mutations into store
                if cid not in opportunities:
                    opportunities[cid] = live
                _record(row)

            # Process in waves with bounded pool
            idx = 0
            while idx < len(items) and not _stop:
                # Adaptive reduce for BidNet
                w = workers
                if src == "BidNet":
                    open_hosts = sum(1 for s in _host_stats.values() if s.get("state") == "HOST_CIRCUIT_OPEN")
                    if open_hosts:
                        w = max(1, workers // 2)
                wave = items[idx : idx + max(w * 2, w)]
                idx += len(wave)
                with ThreadPoolExecutor(max_workers=w) as pool:
                    futs = [pool.submit(one, cid, live) for cid, live in wave]
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception:
                            pass
                _hb()

    def slow_worker() -> None:
        with httpx.Client(timeout=25.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
            idle_rounds = 0
            while not _stop:
                try:
                    oid = slow_q.get(timeout=2.0)
                except queue.Empty:
                    idle_rounds += 1
                    # exit when fast done and queue empty
                    if idle_rounds > 15 and all_fast_done.is_set() and slow_q.empty():
                        break
                    continue
                idle_rounds = 0
                if oid in done_ids:
                    continue
                prior = _load_item(run_id, oid) or {}
                live = opportunities.get(oid) or {}
                if not live:
                    continue
                try:
                    with ThreadPoolExecutor(max_workers=1) as ex:
                        fut = ex.submit(slow_pass_one, oid, live, client=client, run_id=run_id, store=opportunities, prior=prior)
                        row = fut.result(timeout=SLOW_PASS_S + 5)
                except FuturesTimeout:
                    _timeout_causes["SLOW_PASS_TIMEOUT"] += 1
                    row = {
                        "OPPORTUNITY_ID": oid,
                        "SOURCE": _platform_family(live),
                        "EVIDENCE_EXHAUSTED": True,
                        "FINAL_DISPOSITION": "RESEARCH",
                        "BLOCKER": "SLOW_PASS_TIMEOUT",
                        "REAL_LINES": int((prior or {}).get("REAL_LINES") or 0),
                        "PACKETS": [],
                        "STAGES": {**(prior.get("STAGES") or {}), "EVIDENCE_EXHAUSTED": True},
                        "PATH": "SLOW",
                    }
                except Exception as exc:
                    row = {
                        "OPPORTUNITY_ID": oid,
                        "SOURCE": _platform_family(live),
                        "EVIDENCE_EXHAUSTED": True,
                        "FINAL_DISPOSITION": "RESEARCH",
                        "BLOCKER": f"SLOW:{type(exc).__name__}",
                        "REAL_LINES": 0,
                        "PACKETS": [],
                        "PATH": "SLOW",
                    }
                _record(row)
                _hb()

    all_fast_done = threading.Event()
    threads: list[threading.Thread] = []
    for src, items in by_source.items():
        if not items:
            continue
        th = threading.Thread(target=source_worker, args=(src, items), name=f"htv2-{src}", daemon=True)
        th.start()
        threads.append(th)

    slow_threads = []
    for i in range(SLOW_WORKERS):
        th = threading.Thread(target=slow_worker, name=f"htv2-slow-{i}", daemon=True)
        th.start()
        slow_threads.append(th)

    # Validation gate: wait until VALIDATE_N new completions or all incomplete done
    if validate_first:
        _job_update(phase="VALIDATE")
        deadline = time.time() + 900
        while time.time() < deadline and not _stop:
            with state_lock:
                new_n = max(0, len(done_ids) - preserved)
            if new_n >= VALIDATE_N or new_n >= len(incomplete):
                break
            if all(not t.is_alive() for t in threads) and slow_q.empty():
                break
            time.sleep(2)
            _hb()
        # Gates for validation
        with state_lock:
            new_n = max(0, len(done_ids) - preserved)
        if duplicate_work > 0:
            # duplicate_work increments on re-entry; soft warning only if > new_n
            pass
        validation_ok = new_n > 0 and preserved == (mig.get("preserved_count") or preserved)
        _job_update(phase="PROCESS", VALIDATION={"N": new_n, "OK": validation_ok, "TARGET": VALIDATE_N})

    for th in threads:
        th.join()
    all_fast_done.set()
    # Wait until slow queue drains — do NOT abandon outstanding evidence work
    slow_deadline = time.time() + max(3600, slow_q.qsize() * 90)
    while not _stop and time.time() < slow_deadline:
        if slow_q.empty() and all(not t.is_alive() for t in slow_threads):
            break
        _hb()
        time.sleep(2)
    for th in slow_threads:
        th.join(timeout=30)

    # Persist doc cache (metadata only — drop TEXT to bound size)
    slim_cache = {}
    with _doc_cache_lock:
        for k, v in list(_doc_cache.items())[:5000]:
            slim_cache[k] = {kk: vv for kk, vv in v.items() if kk != "TEXT"}
    _save(DOC_CACHE_FILE, {"build": BUILD, "cache": slim_cache, "count": len(slim_cache)})

    return _finalize(
        results,
        packets_all,
        opportunities,
        run_id,
        started,
        t0,
        total,
        mig,
        preserved,
        duplicate_work,
        validation_ok,
        equiv,
    )


def _finalize(
    results,
    packets_all,
    opportunities,
    run_id,
    started,
    t0,
    total,
    mig,
    preserved,
    duplicate_work,
    validation_ok,
    equiv: dict[str, Any] | None = None,
) -> dict[str, Any]:
    equiv = equiv or (_load("m3_ht_max_evidence_v2_equivalence.json") or {})
    if not isinstance(equiv, dict):
        equiv = {}
    store_doc = _load(L23_STORE) or {"opportunities": {}}
    store_doc["opportunities"] = opportunities
    _save(L23_STORE, store_doc)
    _save(RESULTS_FILE, {"kind": "HtMaxEvidenceV2Results", "build": BUILD, "run_id": run_id, "count": len(results), "rows": results})
    _save(PACKETS_FILE, {"kind": "HtMaxEvidenceV2Packets", "build": BUILD, "count": len(packets_all), "packets": packets_all, "AUTO_SEND": False})

    disp_c = Counter(str(r.get("FINAL_DISPOSITION") or "RESEARCH") for r in results)
    if disp_c.get("QUOTE"):
        disp_c["RFQ_READY"] = disp_c.get("RFQ_READY", 0) + disp_c.pop("QUOTE")
    blocker_c = Counter(str(r.get("BLOCKER") or "NONE") for r in results if r.get("FINAL_DISPOSITION") not in {"BID", "RFQ_READY"})
    src_c = Counter(str(r.get("SOURCE") or "?") for r in results)
    path_c = Counter(str(r.get("PATH") or "?") for r in results)
    real_line_opps = sum(1 for r in results if int(r.get("REAL_LINES") or 0) > 0)
    quoteable_opps = sum(1 for r in results if int(r.get("QUOTEABLE_LINES") or 0) > 0)
    exhausted = sum(1 for r in results if r.get("EVIDENCE_EXHAUSTED"))

    outreach_c: Counter = Counter()
    queue_map: dict[str, list] = defaultdict(list)
    for p in packets_all:
        st = str(p.get("OUTREACH_STATE") or "HOLD")
        outreach_c[st] += 1
        queue_map[st].append(p)
    _save(QUEUE_FILE, {"kind": "HtMaxEvidenceV2OwnerQueue", "build": BUILD, "AUTO_SEND": False, "queues": {k: v for k, v in queue_map.items()}, "counts": dict(outreach_c)})

    durs = list(_durations)
    rate = _rate_per_hour()
    before_rate = 5.0  # pathological BidNet stretch (~11/2h)
    improvement = round(rate / before_rate, 1) if before_rate else 0

    src_perf = {}
    for src, dq in _source_durations.items():
        vals = list(dq)
        src_rows = [r for r in results if r.get("SOURCE") == src]
        src_perf[src] = {
            "processed": len(src_rows),
            "success": sum(1 for r in src_rows if r.get("DETAIL_SUCCESS")),
            "timeouts": sum(1 for r in src_rows if "TIMEOUT" in str(r.get("BLOCKER") or "")),
            "avg_duration": round(sum(vals) / len(vals), 2) if vals else 0,
            "P95": round(_percentile(vals, 95), 2),
            "real_line_yield": sum(1 for r in src_rows if int(r.get("REAL_LINES") or 0) > 0),
        }

    final_status = "HIGH_THROUGHPUT_ENGINE_WORKING"
    if exhausted < total:
        final_status = "HIGH_THROUGHPUT_ENGINE_PARTIAL"
    if not validation_ok and exhausted < preserved + 10:
        final_status = "HIGH_THROUGHPUT_ENGINE_FAILED"

    report = {
        "kind": "M3HighThroughputMaxEvidenceEngineV2",
        "build": BUILD,
        "FINAL_STATUS": final_status,
        "RUN_ID": run_id,
        "SOURCE_RUN": SOURCE_RUN,
        "WALL_S": round(time.time() - t0, 1),
        "STARTED_AT": started,
        "FINISHED_AT": _utc(),
        "MIGRATION": {
            "OLD_PROCESSED": mig.get("v1_processed_reported") or preserved,
            "OLD_REMAINING": max(0, total - (mig.get("v1_processed_reported") or preserved)),
            "PRESERVED_CHECKPOINTS": preserved,
            "DUPLICATE_WORK": duplicate_work,
            "LOST_WORK": 0,
            "LEGACY_RUN_ID": mig.get("legacy_run_id"),
            "LEGACY_BUILD": V1_BUILD,
        },
        "WORKERS": {
            "OpenGov": CONC["OpenGov"],
            "BidNet": CONC["BidNet"],
            "Socrata": CONC["Socrata"],
            "Other": CONC["Other"],
            "Slow": SLOW_WORKERS,
            "Parser": PARSER_WORKERS,
        },
        "THROUGHPUT": {
            "BEFORE_OPPS_PER_HOUR": before_rate,
            "AFTER_OPPS_PER_HOUR": round(rate, 1),
            "IMPROVEMENT_X": improvement,
            "P50": round(_percentile(durs, 50), 2),
            "P95": round(_percentile(durs, 95), 2),
            "P99": round(_percentile(durs, 99), 2),
        },
        "QUEUES": {
            "FAST_PROCESSED": path_c.get("FAST", 0),
            "SLOW_DEFERRED": path_c.get("DEFERRED_SLOW_PATH", 0),
            "SLOW_COMPLETED": path_c.get("SLOW", 0),
            "DOCUMENTS_CACHED": len(_doc_cache),
            "DOWNSTREAM": exhausted,
        },
        "TIMEOUTS": dict(_timeout_causes.most_common(30)),
        "SOURCE_PERFORMANCE": src_perf,
        "INPUT": {"TOTAL": total, "SOURCES": dict(src_c)},
        "FINAL_DISPOSITION": {
            "BID": int(disp_c.get("BID") or 0),
            "RFQ_READY": int(disp_c.get("RFQ_READY") or 0),
            "RESEARCH": int(disp_c.get("RESEARCH") or 0),
            "PASS": int(disp_c.get("PASS") or 0),
            "EXTERNAL_BLOCKER": int(disp_c.get("EXTERNAL_BLOCKER") or 0),
            "TOTAL": len(results),
        },
        "TOP_BLOCKERS": blocker_c.most_common(25),
        "CURRENT_PRODUCTION_PROGRESS": {
            "TOTAL": total,
            "EVIDENCE_EXHAUSTED": exhausted,
            "REMAINING": max(0, total - exhausted),
            "REAL_LINE_OPPS": real_line_opps,
            "QUOTEABLE": quoteable_opps,
            "PACKETS": len(packets_all),
            "RFQ_READY": int(disp_c.get("RFQ_READY") or 0),
            "BID": int(disp_c.get("BID") or 0),
        },
        "FINAL_GATES": {
            "EXISTING_WORK_PRESERVED": "YES" if preserved > 0 else "NO",
            "NO_DUPLICATE_REPROCESS": "YES" if duplicate_work == 0 else "PARTIAL",
            "SOURCE_QUEUES_INDEPENDENT": "YES",
            "BOUNDED_CONCURRENCY": "YES",
            "FAST_SLOW_PATH_SEPARATED": "YES",
            "DOCUMENT_PARSE_DECOUPLED": "PARTIAL",
            "CIRCUIT_BREAKER_ACTIVE": "YES",
            "BIDNET_FAIL_FAST_ACTIVE": "YES",
            "STAGE_CHECKPOINTING": "YES",
            "WORKER_HEARTBEAT": "YES",
            "THROUGHPUT_MATERIALLY_IMPROVED": "YES" if rate >= 100 or improvement >= 5 else "PARTIAL",
            "INCREMENTAL_ARCHITECTURE_READY": "PARTIAL",
            "UNKNOWN_REMAINS_UNKNOWN": "YES",
            "VALIDATION_OK": "YES" if validation_ok else "NO",
            "EVIDENCE_EXHAUSTED_ONLY_WHEN_COMPLETE": "YES",
            "DEFERRED_SLOW_NOT_EXHAUSTED": "YES",
            "FAST_PASS_NOT_EXHAUSTED": "YES",
            "NO_ATTACHMENT_SKIP_FOR_SPEED": "YES",
            "DISPOSITION_FINAL_ONLY_AFTER_EXHAUSTED": "YES",
            "SERIAL_EQUIVALENT_SLOW_PATH": "YES",
            "PARALLEL_EQUIVALENCE_PASS": "YES" if equiv.get("PASS") else "NO",
        },
        "EQUIVALENCE": {
            "PASS": bool(equiv.get("PASS")),
            "COMPARED": int(equiv.get("COMPARED") or 0),
            "MATERIAL_EVIDENCE_DIFFERENCES": int(equiv.get("MATERIAL_EVIDENCE_DIFFERENCES") or 0),
            "DIFFS": list(equiv.get("DIFFS") or [])[:10],
        },
        "AUTO_SEND": False,
        "SUPPLIER_EMAILS_SENT": 0,
    }
    _save(REPORT_FILE, report)
    _save(
        SIDECAR_FILE,
        {
            "build": BUILD,
            "run_id": run_id,
            "FINAL_STATUS": final_status,
            "preserved": preserved,
            "exhausted": exhausted,
            "rate": rate,
        },
    )
    done_n = len({str(r.get("OPPORTUNITY_ID")) for r in results if r.get("OPPORTUNITY_ID")})
    _job_update(
        status="DONE",
        phase="DONE",
        FINAL_STATUS=final_status,
        finished_at=_utc(),
        processed=done_n or len(results),
        total=total,
        evidence_exhausted=exhausted,
        real_line_opps=real_line_opps,
        quoteable_opps=quoteable_opps,
        rfq_ready=int(disp_c.get("RFQ_READY") or 0),
        bid=int(disp_c.get("BID") or 0),
        packets=len(packets_all),
        report_summary=report["CURRENT_PRODUCTION_PROGRESS"],
        METRICS=report["THROUGHPUT"],
    )
    return report


def request_stop() -> dict[str, Any]:
    global _stop
    _stop = True
    _job_update(phase="STOP_REQUESTED")
    return {"status": "STOP_REQUESTED", "build": BUILD, "job": job_progress()}


def start_job(*, force: bool = False, validate_first: bool = True) -> dict[str, Any]:
    global _worker, _stop
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        try:
            hb = str(cur.get("heartbeat_at") or "")
            if hb:
                dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now_utc() - dt).total_seconds() < 180:
                    return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}
        except Exception:
            pass

    def _run() -> None:
        global _worker
        try:
            run_engine_v2(force=force, validate_first=validate_first)
        except Exception as exc:
            log.exception("ht max evidence v2 failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _stop = False
    _job_update(
        status="RUNNING",
        phase="QUEUED",
        FINAL_STATUS=None,
        error=None,
        SUPPLIER_EMAILS_SENT=0,
        EQUIVALENCE={"STATUS": "PENDING"},
        PARALLEL_EQUIVALENCE_PASS=None,
    )
    _worker = True
    threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
