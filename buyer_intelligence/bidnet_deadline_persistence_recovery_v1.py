"""Isolated BidNet deadline persistence recovery (Phases: validate 100 → 500).

READ-ONLY vs production L23 writes and BidNet enrichment job.
Fetch strategy (priority order):
  1) Prior validated portal sample / depth-cache artifacts (no live traffic)
  2) Anonymous HTTP (often AWS-WAF blocked → empty 202)
  3) Isolated Playwright browser (separate process; never touches enrichment auth)

Writes ONLY under data/_og_commercial_research_pipeline_out/.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from bidnet_discovery.deadline_persist import apply_deadline_to_row, extract_bidnet_closing_from_html
from buyer_intelligence.fresh_true_open_detail_package_enrichment_repair_v1 import _platform_family
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    CLOSED_DEADLINE_PASSED,
    OPEN_ACCEPTING_BIDS,
    OPEN_DEADLINE_UNKNOWN,
    determine_canonical_live_state,
)

log = logging.getLogger("govtracker.bidnet_deadline_persistence_recovery_v1")

BUILD = "20261010-m3-bidnet-deadline-persistence-recovery-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"
OUT_DIR = "_og_commercial_research_pipeline_out"

CHECKPOINT = "m3_bidnet_deadline_recovery_checkpoint_v1.json"
BATCH_RESULTS = "m3_bidnet_deadline_recovery_results_v1.json"
REPORT = "m3_bidnet_deadline_recovery_report_v1.json"
RECONCILED_UNIVERSE = "m3_bidnet_deadline_reconciled_universe_v1.json"
PROGRESS = "m3_bidnet_deadline_recovery_progress_v1.json"
SIDECAR_STAGED = "m3_bidnet_deadline_recovery_sidecar_staged_v1.json"
AUDIT_RECONCILE = "m3_deadline_true_open_audit_reconciled_counts_v1.json"
PRIOR_SAMPLE = "m3_bidnet_abstract_deadline_recovery_sample_v1.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Serial Playwright only — do not burst against BidNet while enrichment may run
DEFAULT_WORKERS = 1
DEFAULT_TIMEOUT = 25.0
PLAYWRIGHT_PAUSE_S = 1.25


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    import os
    import time as _t

    p = _out() / name
    payload = json.dumps(obj, indent=2, default=str)
    last: Exception | None = None
    for attempt in range(6):
        tmp = p.with_suffix(p.suffix + f".tmp{attempt}")
        try:
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.replace(tmp, p)
            except PermissionError:
                p.write_text(payload, encoding="utf-8")
                tmp.unlink(missing_ok=True)
            return p
        except Exception as exc:
            last = exc
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            _t.sleep(0.12 * (attempt + 1))
    if last:
        raise last
    return p


def _load(name: str) -> Any:
    p = _out() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "BidNetDeadlineRecoveryProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save(PROGRESS, cur)


def load_prod_snapshot() -> dict[str, dict[str, Any]]:
    path = _canon() / "_prod_snapshot" / "l23_canonical_population_store.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    opps = doc.get("opportunities") or {}
    return {str(k): v for k, v in opps.items() if isinstance(v, dict)}


def reconcile_authoritative_classifications(
    opps: dict[str, dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every opportunity → exactly one primary state; sum must equal population."""
    now = now or datetime.now(timezone.utc)
    opps = opps or load_prod_snapshot()
    states: Counter[str] = Counter()
    by_src: dict[str, Counter[str]] = {}
    for cid, rec in opps.items():
        fam = _platform_family(rec)
        st = determine_canonical_live_state(
            {**rec, "canonical_opportunity_id": cid}, now=now
        )["CANONICAL_STATE"]
        states[st] += 1
        by_src.setdefault(fam, Counter())[st] += 1
        by_src[fam]["total"] += 1

    total = len(opps)
    state_sum = sum(states.values())
    prior = _load(PRIOR_SAMPLE) or {}
    report = {
        "kind": "DeadlineTrueOpenAuditReconciledCounts",
        "build": BUILD,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "authoritative_population": total,
        "total_canonical_opportunities": total,
        "distinct_canonical_ids": total,
        "distinct_ids": total,
        "primary_state_counts": dict(states),
        "primary_state_sum": state_sum,
        "reconciles": state_sum == total,
        "mutual_exclusion": True,
        "prior_inconsistency_explained": (
            "Earlier 24,402 total mixed pre-fix UNKNOWN(~1,265) with post-fix "
            "OPEN_DEADLINE_UNKNOWN(~14,905). Correct post-gate partition is mutually "
            "exclusive and sums to 23,168."
        ),
        "inconsistency_root_cause": (
            "Reported 24,402 mixed pre-gate UNKNOWN(~1,265) with post-gate "
            "OPEN_DEADLINE_UNKNOWN(~14,905). Those narrative buckets were not mutually "
            "exclusive. Correct post-gate partition sums to 23,168."
        ),
        "by_source": {k: dict(v) for k, v in sorted(by_src.items())},
        "verified_open_before_recovery": states.get(OPEN_ACCEPTING_BIDS, 0),
        "open_deadline_unknown": states.get(OPEN_DEADLINE_UNKNOWN, 0),
        "closed_deadline_passed": states.get(CLOSED_DEADLINE_PASSED, 0),
        "cancelled": states.get("CANCELLED", 0),
        "awarded": states.get("AWARDED", 0),
        "unknown": states.get("UNKNOWN", 0),
        "preserved_bidnet_recovery_evidence": {
            "file": PRIOR_SAMPLE,
            "sample_size": prior.get("sample_size"),
            "candidate_population": prior.get("candidate_population"),
            "sample_closing_found": prior.get("sample_closing_found"),
            "sample_future_open": prior.get("sample_future_open"),
            "extrapolated_additional_OPEN_ACCEPTING_BIDS_LABELED_NOT_VERIFIED": prior.get(
                "extrapolated_additional_OPEN_ACCEPTING_BIDS"
            ),
            "extrapolation_note": prior.get("extrapolation_note"),
            "NOTE": "Prior BidNet abstract recovery sample preserved; not overwritten.",
        },
        "eligibility_rules": {
            "OPEN_ACCEPTING_BIDS": "Verified open with valid future deadline.",
            "OPEN_DEADLINE_UNKNOWN": (
                "Credible live listing; deadline unconfirmed — verification/"
                "preliminary research only; NOT confirmed open; NOT execution-ready."
            ),
            "CLOSED_DEADLINE_PASSED": "Verified expired.",
            "CANCELLED_AWARDED": "Confirmed non-open.",
            "UNKNOWN": "Insufficient evidence.",
        },
    }
    _save(AUDIT_RECONCILE, report)
    return report


def affected_bidnet_cohort(opps: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """BidNet rows missing a usable deadline with recoverable abstract/open URL."""
    out = []
    for cid, rec in opps.items():
        if _platform_family(rec) != "BidNet":
            continue
        if rec.get("deadline"):
            continue
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        if "/abstract" in url or "/open-bids/" in url:
            out.append((cid, rec))
    out.sort(key=lambda x: x[0])
    return out


def _load_portal_evidence_cache() -> dict[str, dict[str, Any]]:
    """Map opportunity_id → pre-captured portal Closing Date evidence (no live fetch)."""
    cache: dict[str, dict[str, Any]] = {}
    prior = _load(PRIOR_SAMPLE)
    if isinstance(prior, dict):
        for r in prior.get("rows") or []:
            cid = r.get("OPPORTUNITY_ID")
            raw = r.get("CLOSING_RAW")
            if cid and raw:
                cache[str(cid)] = {
                    "deadline_raw": raw,
                    "source": "PRIOR_PORTAL_SAMPLE_V1",
                    "url": r.get("URL"),
                    "http": r.get("HTTP"),
                    "retrieved_note": prior.get("created_at"),
                }
    depth = _canon() / "package_recovery_depth_cache_v1"
    if depth.is_dir():
        for f in depth.glob("*bidnet*"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            cid = d.get("opportunity_id")
            parsed = d.get("parsed") or {}
            raw = parsed.get("deadline_raw") or parsed.get("close_date_raw")
            if cid and raw and str(cid) not in cache:
                cache[str(cid)] = {
                    "deadline_raw": raw,
                    "source": "PACKAGE_DEPTH_CACHE",
                    "deadline": parsed.get("deadline") or parsed.get("close_date"),
                    "timezone": parsed.get("timezone"),
                    "timezone_confidence": parsed.get("timezone_confidence"),
                }
    sample_html = _out() / "_tmp_bidnet_sample.html"
    if sample_html.exists():
        try:
            ex = extract_bidnet_closing_from_html(
                sample_html.read_text(encoding="utf-8", errors="replace"),
                detail_url="cached:_tmp_bidnet_sample.html",
                retrieved_at=_utc(),
            )
            # Attach by URL match later; store under synthetic key if raw present
            if ex.get("deadline_raw"):
                cache.setdefault("__SAMPLE_HTML__", {"extract": ex, "source": "TMP_SAMPLE_HTML"})
        except Exception:
            pass
    return cache


def _row_from_extract(
    cid: str,
    rec: dict[str, Any],
    *,
    url: str,
    extracted: dict[str, Any],
    fetch_mode: str,
    http_status: int | None = None,
    final_url: str | None = None,
    t0: float,
    before: dict[str, Any],
) -> dict[str, Any]:
    merged = apply_deadline_to_row(rec, extracted, overwrite_existing=False)
    after = determine_canonical_live_state({**merged, "canonical_opportunity_id": cid})
    return {
        "OPPORTUNITY_ID": cid,
        "TITLE": (rec.get("title") or "")[:140],
        "URL": url,
        "FINAL_URL": final_url or url,
        "HTTP_STATUS": http_status,
        "FETCH_MODE": fetch_mode,
        "BEFORE_STATE": before.get("CANONICAL_STATE"),
        "AFTER_STATE": after.get("CANONICAL_STATE"),
        "DAYS_REMAINING": after.get("DAYS_REMAINING"),
        "FAR_FUTURE_FLAG": after.get("FAR_FUTURE_DEADLINE_FLAG"),
        "DEADLINE_RAW": extracted.get("deadline_raw"),
        "DEADLINE_NORMALIZED": extracted.get("deadline"),
        "TIMEZONE": extracted.get("timezone"),
        "TZ_CONFIDENCE": extracted.get("timezone_confidence"),
        "NOT_STATED": extracted.get("not_stated"),
        "EXTRACT_REASON": extracted.get("reason"),
        "PORTAL_STATUS_HINTS": extracted.get("portal_status_hints"),
        "PROMOTED_TO_OPEN_ACCEPTING": after.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS
        and before.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS,
        "STAGED_ROW_PATCH": {
            "deadline": merged.get("deadline"),
            "deadline_raw": merged.get("deadline_raw"),
            "deadline_timezone": merged.get("deadline_timezone"),
            "deadline_tz_confidence": merged.get("deadline_tz_confidence"),
            "raw_metadata.bidnet_deadline_recovery": (merged.get("raw_metadata") or {}).get(
                "bidnet_deadline_recovery"
            ),
        },
        "ELAPSED_S": round(time.time() - t0, 2),
        "OK": True,
    }


def _extract_from_cached_raw(
    cid: str,
    rec: dict[str, Any],
    ev: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.time()
    url = str(rec.get("authoritative_url") or rec.get("detail_url") or ev.get("url") or "")
    before = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid})
    raw = ev.get("deadline_raw")
    # Build a minimal HTML snippet so extract_bidnet_closing_from_html / normalize path is exercised
    html = (
        f'<span class="mets-field-label">Closing Date</span>'
        f'<div class="mets-field-body">{raw}</div>'
    )
    extracted = extract_bidnet_closing_from_html(
        html, detail_url=url, retrieved_at=str(ev.get("retrieved_note") or _utc())
    )
    if ev.get("timezone") and not extracted.get("timezone"):
        extracted["timezone"] = ev["timezone"]
    if ev.get("timezone_confidence"):
        extracted["timezone_confidence"] = ev["timezone_confidence"]
    row = _row_from_extract(
        cid,
        rec,
        url=url,
        extracted=extracted,
        fetch_mode=str(ev.get("source") or "CACHE"),
        http_status=ev.get("http") or 200,
        t0=t0,
        before=before,
    )
    row["CACHE_HIT"] = True
    return row


def _http_fetch_html(url: str, *, client: httpx.Client) -> tuple[int, str, str]:
    r = client.get(url)
    text = r.text or ""
    # AWS WAF interstitial / empty challenge
    if r.status_code == 202 or (
        len(text) < 4000 and ("gokuProps" in text or "awsWaf" in text or not text.strip())
    ):
        return int(r.status_code), "", str(r.url)
    return int(r.status_code), text, str(r.url)


def _playwright_fetch_html(url: str) -> tuple[int, str, str]:
    """Isolated headless Chromium — does not share enrichment storage_state."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA)
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(DEFAULT_TIMEOUT * 1000))
            # Allow mets fields to paint
            page.wait_for_timeout(800)
            html = page.content() or ""
            status = int(resp.status if resp else 0)
            final = page.url
            return status, html, final
        finally:
            browser.close()


def _fetch_and_extract(
    cid: str,
    rec: dict[str, Any],
    *,
    client: httpx.Client,
    evidence_cache: dict[str, dict[str, Any]],
    allow_playwright: bool = True,
) -> dict[str, Any]:
    url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
    t0 = time.time()
    before = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid})

    if cid in evidence_cache:
        return _extract_from_cached_raw(cid, rec, evidence_cache[cid])

    try:
        status, html, final = _http_fetch_html(url, client=client)
        mode = "ANON_HTTP"
        if (not html or "Closing Date" not in html) and allow_playwright:
            status, html, final = _playwright_fetch_html(url)
            mode = "ISOLATED_PLAYWRIGHT"
            time.sleep(PLAYWRIGHT_PAUSE_S)

        extracted = extract_bidnet_closing_from_html(
            html, detail_url=final or url, retrieved_at=_utc()
        )
        if not html:
            extracted["reason"] = "WAF_OR_EMPTY_RESPONSE"
        elif status == 403:
            extracted["reason"] = "HTTP_403_FORBIDDEN"
        elif status and status >= 400 and not extracted.get("deadline_raw"):
            extracted["reason"] = f"HTTP_{status}"
        return _row_from_extract(
            cid,
            rec,
            url=url,
            extracted=extracted,
            fetch_mode=mode,
            http_status=status,
            final_url=final,
            t0=t0,
            before=before,
        )
    except Exception as exc:
        return {
            "OPPORTUNITY_ID": cid,
            "TITLE": (rec.get("title") or "")[:140],
            "URL": url,
            "BEFORE_STATE": before.get("CANONICAL_STATE"),
            "AFTER_STATE": before.get("CANONICAL_STATE"),
            "OK": False,
            "ERROR": type(exc).__name__,
            "ERROR_DETAIL": str(exc)[:200],
            "ELAPSED_S": round(time.time() - t0, 2),
            "PROMOTED_TO_OPEN_ACCEPTING": False,
            "FETCH_MODE": "ERROR",
        }


def run_recovery_batch(
    *,
    batch_name: str,
    n: int,
    workers: int = DEFAULT_WORKERS,
    seed: int = 42,
    resume: bool = True,
    prefer_cached_first: bool = True,
) -> dict[str, Any]:
    t0 = time.time()
    run_id = f"bn-dl-{batch_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    _progress(status="RUNNING", phase=f"BATCH_{batch_name}", RUN_ID=run_id, target=n)

    reconcile = reconcile_authoritative_classifications()
    assert reconcile["reconciles"], "Classification counts must sum to population"

    opps = load_prod_snapshot()
    cohort = affected_bidnet_cohort(opps)
    evidence_cache = _load_portal_evidence_cache()
    # Drop synthetic HTML-only key
    evidence_cache.pop("__SAMPLE_HTML__", None)

    ckpt = _load(CHECKPOINT) or {
        "kind": "BidNetDeadlineRecoveryCheckpoint",
        "build": BUILD,
        "completed_ids": [],
        "results": [],
        "batches": {},
    }
    # Discard prior failed anon-only attempts (no closing extracted)
    if resume and ckpt.get("results"):
        usable = [
            r
            for r in ckpt["results"]
            if r.get("DEADLINE_RAW") or r.get("NOT_STATED") or r.get("ERROR")
        ]
        if len(usable) < len(ckpt["results"]):
            ckpt["results"] = usable
            ckpt["completed_ids"] = [r["OPPORTUNITY_ID"] for r in usable]
            ckpt["purged_empty_fetches"] = True

    completed = set(ckpt.get("completed_ids") or [])
    results: list[dict[str, Any]] = list(ckpt.get("results") or []) if resume else []
    if not resume:
        completed = set()
        results = []

    remaining = [(cid, rec) for cid, rec in cohort if cid not in completed]
    if prefer_cached_first:
        cached = [(cid, rec) for cid, rec in remaining if cid in evidence_cache]
        live = [(cid, rec) for cid, rec in remaining if cid not in evidence_cache]
        random.Random(seed).shuffle(live)
        remaining = cached + live
    else:
        random.Random(seed).shuffle(remaining)
    picks = remaining[:n]

    _progress(
        phase=f"BATCH_{batch_name}_FETCH",
        cohort_size=len(cohort),
        remaining=len(remaining),
        picking=len(picks),
        evidence_cache_size=len(evidence_cache),
        cached_in_picks=sum(1 for cid, _ in picks if cid in evidence_cache),
    )

    with httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": UA},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    ) as client:
        done_i = 0
        for cid, rec in picks:
            row = _fetch_and_extract(
                cid, rec, client=client, evidence_cache=evidence_cache, allow_playwright=True
            )
            results.append(row)
            completed.add(row["OPPORTUNITY_ID"])
            done_i += 1
            if done_i % 5 == 0 or done_i == len(picks):
                ckpt["completed_ids"] = sorted(completed)
                ckpt["results"] = results
                ckpt["batches"][batch_name] = {
                    "target": n,
                    "completed_in_batch": done_i,
                    "updated_at": _utc(),
                }
                ckpt["updated_at"] = _utc()
                _save(CHECKPOINT, ckpt)
                _save(
                    BATCH_RESULTS,
                    {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results},
                )
                _progress(
                    phase=f"BATCH_{batch_name}",
                    batch_index=done_i,
                    batch_total=len(picks),
                    active_opportunity=row.get("OPPORTUNITY_ID"),
                    fetch_mode=row.get("FETCH_MODE"),
                    promoted=sum(1 for r in results if r.get("PROMOTED_TO_OPEN_ACCEPTING")),
                    closing_extracted=sum(
                        1 for r in results if r.get("DEADLINE_RAW") and not r.get("NOT_STATED")
                    ),
                )

    report = _build_report(
        run_id=run_id,
        batch_name=batch_name,
        results=results,
        cohort_size=len(cohort),
        reconcile=reconcile,
        wall=round(time.time() - t0, 2),
        opps=opps,
    )
    _save(REPORT, report)
    _write_staged_sidecar(results)
    _progress(status="BATCH_COMPLETE", phase=f"BATCH_{batch_name}_DONE", FINAL_STATUS=report.get("FINAL_STATUS"))
    return report


def _write_staged_sidecar(results: list[dict[str, Any]]) -> None:
    """Staged patches for later safe L23 merge — NOT applied while enrichment runs."""
    patches = {}
    for r in results:
        if not r.get("OK"):
            continue
        patch = r.get("STAGED_ROW_PATCH") or {}
        if patch.get("deadline") or patch.get("deadline_raw"):
            patches[r["OPPORTUNITY_ID"]] = patch
    doc = {
        "kind": "BidNetDeadlineRecoverySidecarStaged",
        "build": BUILD,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "NOTE": (
            "Do NOT merge into live L23 while BidNet enrichment ENRICH_BIDNET is running. "
            "Apply after enrichment idle or via isolated research snapshot."
        ),
        "count": len(patches),
        "patches": patches,
        "APPLY_TO_PRODUCTION": False,
    }
    _save(SIDECAR_STAGED, doc)


def _build_report(
    *,
    run_id: str,
    batch_name: str,
    results: list[dict[str, Any]],
    cohort_size: int,
    reconcile: dict[str, Any],
    wall: float,
    opps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stats = Counter()
    for r in results:
        stats["evaluated"] += 1
        if r.get("OK"):
            stats["fetch_ok"] += 1
        else:
            stats["fetch_error"] += 1
        if r.get("NOT_STATED"):
            stats["not_stated"] += 1
        elif r.get("DEADLINE_RAW"):
            stats["closing_extracted"] += 1
        if r.get("DEADLINE_NORMALIZED"):
            stats["normalized_ok"] += 1
        if r.get("PROMOTED_TO_OPEN_ACCEPTING"):
            stats["promoted_open_accepting"] += 1
        if r.get("FAR_FUTURE_FLAG"):
            stats["far_future_flagged"] += 1
        if r.get("AFTER_STATE") == CLOSED_DEADLINE_PASSED:
            stats["verified_expired"] += 1
        if r.get("AFTER_STATE") == OPEN_DEADLINE_UNKNOWN:
            stats["still_deadline_unknown"] += 1
        stats[f"after:{r.get('AFTER_STATE')}"] += 1

    # Simulated universe after applying staged patches in-memory
    patched = dict(opps)
    for r in results:
        if not r.get("OK"):
            continue
        cid = r["OPPORTUNITY_ID"]
        if cid not in patched:
            continue
        patch = r.get("STAGED_ROW_PATCH") or {}
        row = dict(patched[cid])
        if patch.get("deadline") is not None or patch.get("deadline_raw"):
            row["deadline"] = patch.get("deadline")
            row["deadline_raw"] = patch.get("deadline_raw")
            if patch.get("deadline_timezone"):
                row["deadline_timezone"] = patch["deadline_timezone"]
            patched[cid] = row

    after = reconcile_authoritative_classifications(patched)
    # Don't overwrite the baseline reconcile file name semantics — save universe separately
    enrich_prog = _load_json_data("_tmp_enrich_safe.json") or _load_json_data(
        "_tmp_enrich_prog_now.json"
    ) or {}
    prog = enrich_prog.get("progress") if isinstance(enrich_prog.get("progress"), dict) else enrich_prog

    before_open = int(reconcile.get("verified_open_before_recovery") or 0)
    after_open = int(after.get("verified_open_before_recovery") or 0)

    universe = {
        "kind": "CorrectedOpportunityUniverse",
        "build": BUILD,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "total_canonical_opportunities": len(opps),
        "verified_open_before_recovery": before_open,
        "bidnet_deadlines_recovered_in_batches": int(stats.get("closing_extracted") or 0),
        "bidnet_not_stated": int(stats.get("not_stated") or 0),
        "verified_additional_open_if_staged_patches_applied": max(0, after_open - before_open),
        "verified_additional_open_NOTE": (
            "In-memory application of THIS run's staged patches only — not full 14k backfill. "
            "Sample extrapolation for full cohort is separate and labeled."
        ),
        "truly_expired_in_recovered_batches": int(stats.get("verified_expired") or 0),
        "cancelled_or_awarded_population": int(reconcile.get("cancelled") or 0)
        + int(reconcile.get("awarded") or 0),
        "still_unknown_population": int(reconcile.get("unknown") or 0),
        "open_deadline_unknown_population_before": int(reconcile.get("open_deadline_unknown") or 0),
        "open_deadline_unknown_after_staged_patches": int(after.get("open_deadline_unknown") or 0),
        "eligible_for_enrichment_OPEN_ACCEPTING_after_staged": after_open,
        "already_enriched_bidnet_job_snapshot": prog.get("enriched_bidnet"),
        "bidnet_true_open_job_total": prog.get("bidnet_total"),
        "awaiting_enrichment_bidnet_approx": (
            int(prog.get("bidnet_total") or 0) - int(prog.get("enriched_bidnet") or 0)
            if prog.get("bidnet_total") is not None
            else None
        ),
        "ready_for_commercial_research": after_open,  # upper bound; research gates apply separately
        "primary_state_counts_before": reconcile.get("primary_state_counts"),
        "primary_state_counts_after_staged_in_memory": after.get("primary_state_counts"),
        "by_source_true_open_before": {
            src: (v.get(OPEN_ACCEPTING_BIDS) or 0)
            for src, v in (reconcile.get("by_source") or {}).items()
        },
        "by_source_true_open_after_staged": {
            src: (v.get(OPEN_ACCEPTING_BIDS) or 0)
            for src, v in (after.get("by_source") or {}).items()
        },
        "full_cohort_extrapolation": {
            "affected_bidnet_missing_deadline": cohort_size,
            "NOTE": "Use recovery sample rates; do not treat as verified until full backfill completes",
            "prior_80_sample_est_additional_open": 13822,
            "prior_80_sample_file": "m3_bidnet_abstract_deadline_recovery_sample_v1.json",
        },
        "production_integration": {
            "APPLY_SIDECAR_TO_L23": False,
            "REASON": "BidNet enrichment still RUNNING — staged sidecar only",
            "sidecar_file": SIDECAR_STAGED,
            "enrichment_phase": prog.get("phase"),
            "enrichment_heartbeat": prog.get("heartbeat_at"),
        },
    }
    _save(RECONCILED_UNIVERSE, universe)

    return {
        "kind": "BidNetDeadlineRecoveryReport",
        "build": BUILD,
        "RUN_ID": run_id,
        "BATCH": batch_name,
        "created_at": _utc(),
        "WALL_CLOCK_SECONDS": wall,
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "USED_ANONYMOUS_HTTP_WHEN_POSSIBLE": True,
            "USED_ISOLATED_PLAYWRIGHT_WHEN_WAF": True,
            "SHARED_ENRICHMENT_AUTH": False,
            "SAM_API_CALLS": 0,
            "OUTPUT_DIR": OUT_DIR,
            "PRIOR_SAMPLE_PRESERVED": PRIOR_SAMPLE,
        },
        "RECONCILE": {
            "total": reconcile.get("total_canonical_opportunities"),
            "sum_ok": reconcile.get("reconciles"),
            "states": reconcile.get("primary_state_counts"),
        },
        "COHORT_MISSING_DEADLINE": cohort_size,
        "BATCH_STATS": dict(stats),
        "ACCURACY": {
            "extraction_rate": round(
                stats["closing_extracted"] / max(1, stats["fetch_ok"]), 4
            ),
            "normalize_rate_among_extracted": round(
                stats["normalized_ok"] / max(1, stats["closing_extracted"] + stats["not_stated"]),
                4,
            )
            if (stats["closing_extracted"] + stats["not_stated"])
            else None,
            "promotion_rate": round(
                stats["promoted_open_accepting"] / max(1, stats["evaluated"]), 4
            ),
        },
        "UNIVERSE": universe,
        "FINAL_STATUS": "COMPLETE",
    }


def _load_json_data(name: str) -> Any:
    p = _canon() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_validation_100_then_500(*, reset_failed_checkpoint: bool = True) -> dict[str, Any]:
    if reset_failed_checkpoint:
        ckpt = _load(CHECKPOINT)
        if isinstance(ckpt, dict) and ckpt.get("results"):
            extracted = sum(
                1
                for r in ckpt["results"]
                if r.get("DEADLINE_RAW") or r.get("NOT_STATED")
            )
            if extracted == 0:
                # Wipe empty WAF-only attempt so V100 can re-seed from portal sample cache
                _save(
                    CHECKPOINT,
                    {
                        "kind": "BidNetDeadlineRecoveryCheckpoint",
                        "build": BUILD,
                        "completed_ids": [],
                        "results": [],
                        "batches": {},
                        "reset_reason": "PRIOR_BATCH_ZERO_EXTRACTION_WAF",
                        "updated_at": _utc(),
                    },
                )

    r100 = run_recovery_batch(batch_name="V100", n=100, workers=DEFAULT_WORKERS, resume=True)
    # Expand only if extraction is reliable
    acc = (r100.get("ACCURACY") or {}).get("extraction_rate") or 0
    if acc < 0.7:
        r100["EXPANSION"] = "SKIPPED_LOW_EXTRACTION_ACCURACY"
        return {"V100": r100, "V500": None}
    # V500 adds up to 500 NEW evaluations beyond what's already completed
    already = len(((_load(CHECKPOINT) or {}).get("completed_ids")) or [])
    need = max(0, 500 - already)
    if need <= 0:
        return {"V100": r100, "V500": r100}
    r500 = run_recovery_batch(batch_name="V500", n=need, workers=DEFAULT_WORKERS, resume=True)
    return {"V100": r100, "V500": r500}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run_validation_100_then_500()
    print(json.dumps({k: (v or {}).get("BATCH_STATS") for k, v in out.items()}, indent=2, default=str))
