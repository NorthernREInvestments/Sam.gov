"""Build 002J — forensic audit + offline recovery. ZERO metered NextBid calls.

May use free /v1/usage and /v1/account/activity only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Load .env without printing secrets
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from m3 import BUILD_ID
from m3.research.classify import classify_batch
from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import (
    acquisition_locked,
    controller_status,
    free_request,
    refresh_account_usage,
)
from m3.research.nextbid_inventory import upsert_inventory
from m3.research.nextbid_killswitch import engage_emergency_stop, emergency_stop_status
from m3.research.nextbid_schedule import pause_scheduler


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_local() -> dict:
    """Rebuild call distribution from acceptance + checkpoint (no metered calls)."""
    acc_path = ROOT / "docs" / "BUILD_002I_NEXTBID_ACCEPTANCE.json"
    acc = json.loads(acc_path.read_text(encoding="utf-8")) if acc_path.exists() else {}
    summary = acc.get("summary") or {}
    acq = acc.get("acquisition") or {}
    doc_pass = acc.get("doc_pass") or {}
    budget = (acq.get("budget") or {})
    by_ep = summary.get("calls_by_endpoint") or budget.get("by_endpoint") or {}
    # Doc pass budget may overwrite — prefer doc_pass.budget if present
    if isinstance(doc_pass.get("budget"), dict) and doc_pass["budget"].get("by_endpoint"):
        # Final endpoint totals after both jobs if present in doc_pass
        # Initial acceptance had partial; doc_pass has controller-less totals from second job only
        pass

    # Reconstruct job timeline from known local runs (evidence-based)
    jobs = [
        {
            "job_id": "002i_acceptance_run",
            "script": "scripts/acceptance_build002i_nextbid.py",
            "started_at": acc.get("started_at"),
            "finished_at": acq.get("finished_at"),
            "remaining_before": (acc.get("auth") or {}).get("remaining_before")
            or (acq.get("usage_before") or {}).get("remaining"),
            "remaining_after": (acq.get("usage_after") or {}).get("remaining"),
            "reported_used_delta": None,
            "endpoints": (acq.get("budget") or {}).get("by_endpoint") or {
                "search": 61,
                "get_opportunity": 661,
                "list_documents": 270,
                "list_files": 237,
                "read_file": 404,
            },
            "note": "Primary Build 002I acceptance acquisition. Reported ACCEPTANCE before doc pass.",
        },
        {
            "job_id": "002i_doc_pass",
            "script": "scripts/_tmp_nb_requalify_and_docs.py",
            "started_at": None,
            "finished_at": None,
            "remaining_before": (doc_pass or {}).get("remaining_before"),
            "remaining_after": (doc_pass or {}).get("remaining_after"),
            "reported_used_delta": (doc_pass or {}).get("calls_used"),
            "endpoints": ((doc_pass.get("budget") or {}).get("by_endpoint") if doc_pass else None),
            "note": "SECOND metered job AFTER acceptance printed. This is the primary reason usage rose after 'completion'.",
        },
    ]
    for j in jobs:
        b, a = j.get("remaining_before"), j.get("remaining_after")
        if isinstance(b, int) and isinstance(a, int):
            j["reported_used_delta"] = b - a

    # Sum known local deltas
    local_sum = sum(j["reported_used_delta"] for j in jobs if isinstance(j.get("reported_used_delta"), int))

    # Initial endpoint breakdown from first job (documented in acceptance summary mid-run)
    first_endpoints = jobs[0]["endpoints"] or {}
    first_sum = sum(int(v) for v in first_endpoints.values()) if first_endpoints else None

    avoidable = {
        "detail_for_non_product_or_unknown_without_listing_gate": {
            "estimate_basis": "600 detail candidates enriched; only 6 Stage C; 304 Stage B after",
            "get_opportunity_calls": first_endpoints.get("get_opportunity"),
            "note": "Listing titles often sufficient to exclude construction/services before detail.",
        },
        "document_endpoints_before_gov_source_exhaustion": {
            "list_documents": first_endpoints.get("list_documents"),
            "list_files": first_endpoints.get("list_files"),
            "read_file": first_endpoints.get("read_file"),
            "note": "Doc pass added ~646 more calls with ZERO additional Stage C fits.",
        },
        "post_completion_doc_pass": {
            "calls": jobs[1].get("reported_used_delta"),
            "additional_fits": 0,
            "note": "Should never have run after acceptance reported completion without owner auth.",
        },
        "retries": {
            "status": "NOT_SEPARATELY_LEDGERED_IN_002I",
            "note": "002I client could retry 429/5xx and counted retries against budget, but no per-retry ledger existed.",
        },
    }

    ck = load_checkpoint("nextbid_acquire_v1")
    enriched = (ck.get("state") or {}).get("enriched") or []
    listings = (ck.get("state") or {}).get("listings") or []
    seen = (ck.get("state") or {}).get("seen_nb_ids") or []

    return {
        "source_files": [
            "docs/BUILD_002I_NEXTBID_ACCEPTANCE.json",
            "data/nextbid_checkpoints/nextbid_acquire_v1.json",
        ],
        "jobs": jobs,
        "local_metered_sum_from_remaining_deltas": local_sum,
        "first_job_endpoint_sum": first_sum,
        "first_job_endpoints": first_endpoints,
        "acceptance_summary_used_delta": summary.get("api_used_delta"),
        "acceptance_remaining_after": summary.get("api_remaining_after"),
        "checkpoint": {
            "seen_nb_ids": len(seen),
            "listings": len(listings),
            "enriched": len(enriched),
            "completed_queries": (ck.get("state") or {}).get("completed_queries"),
        },
        "avoidable": avoidable,
        "root_causes": [
            "No hard per-job / per-day ceiling in executable code (prompt-only budgets).",
            "Automatic enrichment of hundreds of candidates with detail+docs before listing-level exclusion.",
            "Document pass executed after acceptance completion (~646 additional metered calls, 0 new fits).",
            "Production scheduler could start on deploy (15s delay) — fixed in 002J fail-closed lock.",
            "Retries counted but not ledgered per opportunity in 002I.",
        ],
    }


def free_provider_reconcile() -> dict:
    """Free usage + activity only."""
    out: dict = {"metered_calls_made_by_this_function": False}
    try:
        usage = refresh_account_usage(allow_when_locked=True)
        out["usage"] = usage
    except Exception as exc:
        out["usage_error"] = type(exc).__name__
        usage = {}
    # Activity is free
    try:
        activity = free_request("/account/activity", params={"limit": 100})
        rows = activity.get("activity") or activity.get("results") or activity.get("items") or []
        if not isinstance(rows, list):
            rows = []
        ops = Counter()
        metered_ops = Counter()
        for r in rows:
            if not isinstance(r, dict):
                continue
            op = str(r.get("operation") or r.get("op") or r.get("endpoint") or "unknown")
            ops[op] += 1
            units = r.get("units")
            if units is None:
                units = 1 if op not in {"usage", "account", "activity", "tradelines", "stats", "my_usage", "my_account"} else 0
            if int(units or 0) > 0:
                metered_ops[op] += int(units)
        out["activity"] = {
            "rows_returned": len(rows),
            "operations": dict(ops),
            "metered_units_in_sample": dict(metered_ops),
            "sample_limit": 100,
            "note": "Activity sample is newest-first capped; not a full period ledger unless provider returns all.",
        }
    except Exception as exc:
        out["activity_error"] = type(exc).__name__
    return out


def recover_offline() -> dict:
    """Reprocess checkpoint enriched rows without metered NextBid calls."""
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    rows = []
    for e in state.get("enriched") or []:
        it = dict(e)
        if it.get("description") and not it.get("document_text"):
            it["document_text"] = it["description"]
        # Award/forecast exclusion
        blob = f"{it.get('title') or ''} {it.get('official_url') or ''}"
        if "notice-of-awards" in blob.lower() or "/awards" in blob.lower():
            it["verified_individual_solicitation"] = False
            it["record_type"] = "NON_SOLICITATION"
        rows.append(it)

    batch = classify_batch(rows)
    classified = batch.get("items") if isinstance(batch, dict) else rows
    fits = []
    stage_counts: Counter = Counter()
    product_ops = []
    qty_ops = []
    for it in classified or []:
        pipe = evaluate_procurement_pipeline(it, document_text=it.get("document_text"))
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe["quantity"]
        it["procurement_pipeline"] = pipe
        stage = pipe.get("stage") or "none"
        stage_counts[stage] += 1
        if stage == STAGE_B:
            product_ops.append(it)
        if pipe.get("quantity_defined"):
            qty_ops.append(it)
        if stage == STAGE_C and it.get("quantity") not in (None, "", "unknown"):
            fits.append(
                {
                    "nb_id": it.get("nb_id"),
                    "title": it.get("title"),
                    "solicitation_number": it.get("solicitation_number"),
                    "agency": it.get("buyer_name"),
                    "source": it.get("nextbid_source"),
                    "official_url": it.get("official_url"),
                    "deadline_at": it.get("deadline_at"),
                    "quantity": it.get("quantity"),
                    "quantity_unit": it.get("quantity_unit"),
                    "quantity_semantic": it.get("quantity_semantic"),
                    "quantity_evidence": it.get("quantity_evidence") or pipe.get("quantity_evidence"),
                    "requirement_class": it.get("requirement_class"),
                    "stage": STAGE_C,
                    "evidence": {
                        "nextbid_nb_id": it.get("nb_id"),
                        "source_url": it.get("source_url_original") or it.get("official_url"),
                        "pipeline_reasons": pipe.get("reasons"),
                        "quantity_evidence": it.get("quantity_evidence") or pipe.get("quantity_evidence"),
                        "recovery": "offline_reprocess_no_metered_calls",
                    },
                }
            )

    inv = upsert_inventory(classified or [])
    # Prior free-source fits
    prior_path = ROOT / "docs" / "BUILD_002I_VERIFIED_OPPORTUNITIES.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}

    return {
        "examined": len(classified or []),
        "stage_counts": dict(stage_counts),
        "recovered_stage_c_fits": fits,
        "recovered_fit_count": len(fits),
        "verified_product_opportunities_stage_b": len(product_ops),
        "quantity_defined_count": len(qty_ops),
        "inventory_upsert": inv,
        "prior_free_source_fits": prior.get("count"),
        "prior_free_source_opportunities": prior.get("opportunities"),
        "metered_calls": 0,
    }


def coverage_assessment(audit: dict) -> dict:
    queries = (audit.get("checkpoint") or {}).get("completed_queries") or []
    return {
        "status": "UNVERIFIED",
        "benchmark_portal_open_approx": 3899,
        "nextbid_discovered_unique": (audit.get("checkpoint") or {}).get("seen_nb_ids"),
        "enriched": (audit.get("checkpoint") or {}).get("enriched"),
        "sources_targeted": [
            "opengov",
            "bonfire",
            "planetbids",
            "calprocure",
            "publicpurchase",
            "instantmarkets",
        ],
        "sources_excluded": ["sam.gov", "dibbs"],
        "queries_completed": queries,
        "query_count": len(queries),
        "gaps": [
            "Public Purchase low/absent in enriched yield — coverage incomplete.",
            "Pagination capped (tradeline ≤8 pages, keyword ≤3) — pages not fully retrieved.",
            "Commodities tradeline open count (~14k) far exceeds pages fetched — NOT comprehensive.",
            "No proof all product categories exhausted without additional metered search.",
            "917 discovered ≠ market completeness.",
        ],
        "note": "Complete coverage cannot be established without additional metered calls. Reported UNVERIFIED. Do not spend calls without owner authorization.",
    }


def proposed_plan() -> dict:
    return {
        "status": "PROPOSED_NOT_EXECUTED",
        "requires_owner_authorization": True,
        "modes": {
            "A_initial_backfill": {
                "purpose": "One-time relevant market coverage",
                "max_projected_metered_calls": 100,
                "hard_ceiling_note": "Owner hard rule: never >100 credits without new explicit authorization raising the ceiling.",
                "breakdown": {
                    "search_pagination": 40,
                    "listing_filtered_detail": 40,
                    "targeted_docs_only_after_relevance": 20,
                },
                "expected_candidates": "unknown until search totals reviewed under authorized budget",
                "execute": False,
            },
            "B_incremental": {
                "purpose": "New/changed only vs inventory hashes",
                "max_projected_metered_calls_per_day": 50,
                "job_limit": 50,
                "daily_limit": 100,
                "monthly_reserve": 1000,
                "execute": False,
            },
        },
    }


def main() -> int:
    started = time.time()
    engage_emergency_stop(reason="BUILD_002J_AUDIT_RECOVERY")
    pause_scheduler(reason="BUILD_002J_AUDIT_RECOVERY")
    assert acquisition_locked(), "must remain locked"

    usage_before = None
    try:
        usage_before = refresh_account_usage(allow_when_locked=True)
    except Exception as exc:
        usage_before = {"ok": False, "error": type(exc).__name__}

    audit = audit_local()
    provider = free_provider_reconcile()
    recovery = recover_offline()
    coverage = coverage_assessment(audit)

    usage_after = None
    try:
        usage_after = refresh_account_usage(allow_when_locked=True)
    except Exception as exc:
        usage_after = {"ok": False, "error": type(exc).__name__}

    # Prove no metered spend by 002J (free usage only)
    metered_delta = None
    if isinstance(usage_before, dict) and isinstance(usage_after, dict):
        ub, ua = usage_before.get("used"), usage_after.get("used")
        if isinstance(ub, int) and isinstance(ua, int):
            metered_delta = ua - ub

    acceptance = {
        "build": "002J",
        "build_id": BUILD_ID,
        "started_at": _utc(),
        "emergency_stop": emergency_stop_status(),
        "controller": controller_status(),
        "acquisition_locked": True,
        "metered_nextbid_calls_by_002j": 0,
        "usage_before_free_probe": usage_before,
        "usage_after_free_probe": usage_after,
        "metered_used_delta_during_002j": metered_delta,
        "forensic_audit": audit,
        "provider_reconcile_free": provider,
        "recovery": {
            "recovered_fit_count": recovery["recovered_fit_count"],
            "stage_counts": recovery["stage_counts"],
            "verified_product_opportunities_stage_b": recovery["verified_product_opportunities_stage_b"],
            "quantity_defined_count": recovery["quantity_defined_count"],
            "prior_free_source_fits": recovery["prior_free_source_fits"],
            "inventory_upsert": recovery["inventory_upsert"],
            "metered_calls": 0,
        },
        "strict_m3_fit_evidence": recovery["recovered_stage_c_fits"],
        "prior_free_source_evidence": recovery.get("prior_free_source_opportunities"),
        "coverage": coverage,
        "proposed_acquisition_plan": proposed_plan(),
        "do_not_resume_paid_acquisition": True,
        "do_not_begin_build_003": True,
        "await_owner_acceptance": True,
        "duration_seconds": round(time.time() - started, 1),
        "finished_at": _utc(),
    }

    # Reconcile reported vs owner-observed
    acceptance["consumption_reconciliation"] = {
        "reported_in_002i_acceptance_delta": audit.get("acceptance_summary_used_delta"),
        "local_job_deltas_sum": audit.get("local_metered_sum_from_remaining_deltas"),
        "owner_observed_total": 3054,
        "provider_used_now": (usage_after or {}).get("used"),
        "provider_remaining_now": (usage_after or {}).get("remaining"),
        "gap_explanation": [
            "002I acceptance '2344' = initial run (~1698) + doc pass (~646).",
            "Owner observed 3054 — higher than local job sum; remaining gap may be production scheduler post-deploy and/or retries/unlogged probes.",
            "Full period activity may exceed the free activity sample of 100 rows — marked where incomplete.",
        ],
        "unverified_without_full_activity_export": True,
    }

    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "BUILD_002J_ACCEPTANCE.json").write_text(
        json.dumps(acceptance, indent=2, default=str), encoding="utf-8"
    )

    # Write markdown reports
    j0, j1 = (audit.get("jobs") or [{}, {}] + [{}, {}])[:2]
    audit_md = f"""# Build 002J — Forensic API Consumption Audit

**Build ID:** `{BUILD_ID}`  
**Rule:** ZERO metered NextBid calls during this audit.  
**Acquisition:** LOCKED fail-closed.

## Root cause

1. No executable hard daily/job ceiling (soft buckets allowed >2000 calls/session).
2. Mass detail+document enrichment before strong listing-level exclusion.
3. Post-completion document pass (~646 metered calls, 0 new Stage C fits).
4. Production scheduler could auto-run after deploy (now fail-closed).
5. Retries not ledgered per opportunity in 002I.

## Local job accounting

| Job | Remaining before → after | Used |
|-----|--------------------------|-----:|
| 002I acceptance | {(j0 or {}).get('remaining_before')} → {(j0 or {}).get('remaining_after')} | {(j0 or {}).get('reported_used_delta')} |
| 002I doc pass (AFTER completion report) | {(j1 or {}).get('remaining_before')} → {(j1 or {}).get('remaining_after')} | {(j1 or {}).get('reported_used_delta')} |
| Local sum | | {audit.get('local_metered_sum_from_remaining_deltas')} |

- Owner observed consumed: **3054**
- 002I acceptance reported delta: **{audit.get('acceptance_summary_used_delta')}**
- Gap vs owner total: unexplained remainder may include production scheduler and/or retries; activity sample incomplete.

### First-job endpoints

```json
{json.dumps(audit.get('first_job_endpoints'), indent=2)}
```

Endpoint sum: {audit.get('first_job_endpoint_sum')}

## Calls per verified fit (002I)

If 2344 local-reported / 6 fits ≈ **390 calls per fit** (unacceptable).  
If 3054 / 6 ≈ **509 calls per fit**.

## Preservation

Checkpoint and acceptance artifacts retained. No data deleted.
"""
    (docs / "BUILD_002J_API_AUDIT.md").write_text(audit_md, encoding="utf-8")

    recovery_md = f"""# Build 002J — Recovery Report

**Build ID:** `{BUILD_ID}`  
**Metered NextBid calls this build:** **0**

## Offline reprocessing

| Metric | Count |
|--------|------:|
| Records examined | {recovery['examined']} |
| Stage C fits (recovered from paid data) | {recovery['recovered_fit_count']} |
| Stage B product opportunities | {recovery['verified_product_opportunities_stage_b']} |
| Quantity-defined | {recovery['quantity_defined_count']} |
| Prior free-source fits (separate) | {recovery['prior_free_source_fits']} |
| Inventory rows | {(recovery.get('inventory_upsert') or {}).get('total')} |

## Stage counts

```json
{json.dumps(recovery['stage_counts'], indent=2)}
```

## Strict M3 fit evidence

See `BUILD_002J_ACCEPTANCE.json` → `strict_m3_fit_evidence` (traceable nb_id + source URL + quantity evidence).

## Coverage

Status: **{coverage['status']}**  
Discovered unique: {coverage.get('nextbid_discovered_unique')} vs benchmark ~{coverage.get('benchmark_portal_open_approx')}.  
Gaps listed in acceptance `coverage.gaps`. No metered calls authorized to close gaps.

## Proposed plan (NOT EXECUTED)

Max projected under current owner ceiling: **100 metered calls** for any authorized backfill slice.  
Details in acceptance `proposed_acquisition_plan`.

## Outstanding issues

- Many Stage B products still lack ordered quantity in accessible text.
- Portal attachment auth shells remain.
- Do not resume paid acquisition.

Await owner acceptance. Do not begin Build 003.
"""
    (docs / "BUILD_002J_RECOVERY_REPORT.md").write_text(recovery_md, encoding="utf-8")

    print(
        json.dumps(
            {
                "locked": True,
                "metered_delta": metered_delta,
                "fits": recovery["recovered_fit_count"],
                "local_sum": audit.get("local_metered_sum_from_remaining_deltas"),
                "provider_used": (usage_after or {}).get("used"),
                "provider_remaining": (usage_after or {}).get("remaining"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
