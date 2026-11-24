"""Build 002K — offline recovery, safeguard tests, optional ≤100-call live test, re-lock.

ZERO metered calls until safeguard tests + usage verification pass.
Always re-locks paid acquisition in finally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from m3 import BUILD_ID
from m3.research.nextbid_acquire import _normalize_listing, _looks_productish
from m3.research.nextbid_client import NextBidClient, load_checkpoint, save_checkpoint
from m3.research.nextbid_controller import (
    acquisition_locked,
    controller_status,
    refresh_account_usage,
    revoke_owner_authorization,
    write_owner_authorization,
    load_budget_state,
    save_budget_state,
)
from m3.research.nextbid_inventory import load_inventory, upsert_inventory, needs_enrichment
from m3.research.nextbid_killswitch import (
    clear_emergency_stop,
    engage_emergency_stop,
    emergency_stop_status,
)
from m3.research.nextbid_recovery import listing_prefilter, reprocess_full_population
from m3.research.nextbid_schedule import pause_scheduler
from m3.research.classify import classify_batch
from m3.research.commercial_criteria import STAGE_C, evaluate_procurement_pipeline


PRIORITY_SOURCES = "opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_pytest() -> dict:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_nextbid_controller_002k.py", "-q", "--tb=line"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "returncode": r.returncode,
        "passed": r.returncode == 0,
        "stdout_tail": (r.stdout or "")[-1500:],
        "stderr_tail": (r.stderr or "")[-800:],
        "metered_calls": 0,
    }


def dry_run_plan(recovery: dict, usage: dict) -> dict:
    inv = load_inventory()
    already = len(inv)
    queries = [
        {"tradelines": "commodities", "sources": "opengov,bonfire,planetbids", "pages": 2},
        {"tradelines": "commodities", "sources": "calprocure,publicpurchase,instantmarkets", "pages": 2},
        {"q": "janitorial supplies", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "industrial supplies", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "furniture", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "safety equipment PPE", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "tools equipment", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "generator", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "plumbing supplies", "sources": PRIORITY_SOURCES, "pages": 1},
        {"q": "electrical supplies", "sources": PRIORITY_SOURCES, "pages": 1},
    ]
    est_search = sum(q["pages"] for q in queries)  # ≤12
    est_detail_budget = min(50, 100 - est_search)
    return {
        "strategy": "listing-first max page_size=25; skip cached nb_ids; detail only promote/investigate new",
        "page_size": 25,
        "estimated_search_calls_max": est_search,
        "estimated_detail_calls_max": est_detail_budget,
        "estimated_total_max": est_search + est_detail_budget,
        "hard_test_cap": 100,
        "hard_job_cap": 50,
        "inventory_already": already,
        "offline_stage_c": recovery.get("strict_fits") and len(recovery["strict_fits"]),
        "offline_stage_b": recovery.get("stage_b_count"),
        "provider_remaining_last_reported": usage.get("remaining"),
        "monthly_reserve_protected": True,
        "queries": queries,
        "execute_if": [
            "safeguard_tests_passed",
            "usage_verified",
            "estimated_total_max<=100",
            "remaining_after_reserve_ok",
        ],
    }


def unlock_for_bounded_test() -> None:
    # Clear kill file + write auth; env still emergency by default — auth scope allows unlock path
    clear_emergency_stop(owner_authorized=True)
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    os.environ["NEXTBID_ENABLED"] = "true"
    from m3.config import get_settings

    get_settings.cache_clear()
    write_owner_authorization(
        authorized=True,
        scope="build_002k_bounded_test",
        test_call_cap=100,
        max_additional_calls=100,
        note="BUILD_002K owner-authorized bounded production test (max 100 collective)",
    )
    # Reset test_used for this authorized session only; do NOT reset day_used if same day spent
    st = load_budget_state()
    st["test_used"] = 0
    st["test_reserved"] = 0
    # Keep day_used as-is (do not reset existing daily usage)
    today = str(__import__("datetime").date.today())
    if st.get("day") != today:
        st["day"] = today
        st["day_used"] = 0
    save_budget_state(st)


def relock() -> None:
    revoke_owner_authorization()
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    os.environ["NEXTBID_ENABLED"] = "false"
    from m3.config import get_settings

    get_settings.cache_clear()
    engage_emergency_stop(reason="BUILD_002K_TEST_COMPLETE_RELOCK")
    pause_scheduler(reason="BUILD_002K_TEST_COMPLETE_RELOCK")


def live_bounded_test(plan: dict) -> dict:
    """Execute ≤100 metered calls: discovery job ≤50, detail job ≤50."""
    started = time.time()
    usage_before = refresh_account_usage()
    inv = load_inventory()
    known = set(inv.keys())
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    seen = set(state.get("seen_nb_ids") or [])
    listings_by_id = {str(x.get("nb_id")): x for x in (state.get("listings") or []) if x.get("nb_id")}

    discovery_job = "002k-discovery"
    detail_job = "002k-detail"
    new_rows: list[dict] = []
    search_calls = 0
    detail_calls = 0
    blocked = 0
    cache_hits = 0
    errors: list[str] = []

    # --- Discovery job (≤50) ---
    with NextBidClient(job_id=discovery_job) as client:
        client.usage()
        for q in plan["queries"]:
            if search_calls >= 40:
                break
            pages = int(q.get("pages") or 1)
            for page in range(1, pages + 1):
                if search_calls >= 40:
                    break
                params = {
                    "open": "true",
                    "page": page,
                    "page_size": 25,
                    "sort": "deadline",
                }
                if q.get("tradelines"):
                    params["tradelines"] = q["tradelines"]
                    params["sources"] = q.get("sources")
                else:
                    params["q"] = q.get("q")
                    params["sources"] = q.get("sources")
                    params["sort"] = "relevance"
                try:
                    res = client.search(params)
                    search_calls += 1
                except Exception as exc:
                    errors.append(f"search:{type(exc).__name__}")
                    blocked += 1
                    break
                if res.get("_cache_hit"):
                    cache_hits += 1
                    # cached search still shouldn't happen often; count as zero spend already
                for row in res.get("results") or []:
                    if not isinstance(row, dict):
                        continue
                    nb = str(row.get("nb_id") or "")
                    if not nb or nb in seen:
                        continue
                    src = str(row.get("source") or "").lower()
                    if src in {"sam.gov", "dibbs"}:
                        continue
                    seen.add(nb)
                    item = _normalize_listing(row)
                    listings_by_id[nb] = item
                    if nb not in known:
                        new_rows.append(item)

    # Checkpoint discovery
    state["seen_nb_ids"] = sorted(seen)
    state["listings"] = list(listings_by_id.values())
    state["completed_queries"] = list(
        set(list(state.get("completed_queries") or []) + [json.dumps(q, sort_keys=True) for q in plan["queries"]])
    )
    save_checkpoint(state, "nextbid_acquire_v1")
    upsert_inventory(list(listings_by_id.values()))

    # Candidates for detail: new + promote, not already detailed
    candidates = []
    for it in new_rows:
        disp = listing_prefilter(it)
        if disp == "exclude":
            continue
        if not _looks_productish(it) and disp != "promote":
            continue
        nb = it.get("nb_id")
        if nb and not needs_enrichment(nb):
            # inventory says unchanged complete — skip
            continue
        if it.get("nextbid_detail_fetched"):
            continue
        candidates.append(it)

    # --- Detail job (≤50, total test ≤100) ---
    st = load_budget_state()
    test_used = int(st.get("test_used") or 0)
    detail_budget = min(50, 100 - test_used)
    enriched = []
    with NextBidClient(job_id=detail_job) as client:
        for it in candidates[:detail_budget]:
            if detail_calls >= detail_budget:
                break
            try:
                from m3.research.nextbid_acquire import enrich_with_detail, try_original_gov_documents

                row = enrich_with_detail(client, dict(it))
                detail_calls += 1
                row = try_original_gov_documents(row)
                enriched.append(row)
            except Exception as exc:
                errors.append(f"detail:{type(exc).__name__}:{it.get('nb_id')}")
                blocked += 1
                if "BudgetExhausted" in type(exc).__name__ or "Unauthorized" in type(exc).__name__:
                    break

    # Reconcile actual metered from controller state
    st_after = load_budget_state()
    test_used_after = int(st_after.get("test_used") or 0)
    usage_after = refresh_account_usage()

    # Qualify new enriched + keep offline fits
    batch = classify_batch(enriched)
    classified = batch.get("items") if isinstance(batch, dict) else enriched
    live_fits = []
    stage_counts: Counter = Counter()
    for it in classified or []:
        pipe = evaluate_procurement_pipeline(it, document_text=it.get("document_text"))
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe["quantity"]
        stage = pipe.get("stage") or "none"
        stage_counts[stage] += 1
        if stage == STAGE_C and it.get("quantity") not in (None, "", "unknown"):
            live_fits.append(
                {
                    "nb_id": it.get("nb_id"),
                    "title": it.get("title"),
                    "solicitation_number": it.get("solicitation_number"),
                    "agency": it.get("buyer_name"),
                    "source": it.get("nextbid_source"),
                    "official_url": it.get("official_url"),
                    "deadline_at": it.get("deadline_at"),
                    "quantity": it.get("quantity"),
                    "quantity_evidence": it.get("quantity_evidence") or pipe.get("quantity_evidence"),
                    "requirement_class": it.get("requirement_class"),
                    "stage": STAGE_C,
                    "evidence": {
                        "nextbid_nb_id": it.get("nb_id"),
                        "source_url": it.get("official_url"),
                        "pipeline_reasons": pipe.get("reasons"),
                        "origin": "002k_bounded_live_test",
                    },
                }
            )

    # Merge enriched into checkpoint
    existing_enriched = {e.get("nb_id"): e for e in (state.get("enriched") or []) if e.get("nb_id")}
    for e in enriched:
        if e.get("nb_id"):
            existing_enriched[e["nb_id"]] = {
                k: e.get(k)
                for k in (
                    "nb_id",
                    "external_key",
                    "title",
                    "solicitation_number",
                    "buyer_name",
                    "official_url",
                    "source_url_original",
                    "nextbid_source",
                    "deadline_at",
                    "solicitation_status",
                    "requirement_class",
                    "quantity",
                    "quantity_semantic",
                    "description",
                    "nextbid_detail_fetched",
                    "verified_individual_solicitation",
                    "record_type",
                )
                if e.get(k) is not None
            }
            existing_enriched[e["nb_id"]]["document_text_len"] = len(str(e.get("document_text") or ""))
    state["enriched"] = list(existing_enriched.values())
    save_checkpoint(state, "nextbid_acquire_v1")
    upsert_inventory(list(existing_enriched.values()))

    provider_delta = None
    if isinstance(usage_before.get("used"), int) and isinstance(usage_after.get("used"), int):
        provider_delta = usage_after["used"] - usage_before["used"]

    return {
        "ok": True,
        "duration_seconds": round(time.time() - started, 1),
        "usage_before": usage_before,
        "usage_after": usage_after,
        "provider_used_delta": provider_delta,
        "local_test_used": test_used_after,
        "search_calls_attempted": search_calls,
        "detail_candidates": len(candidates),
        "detail_enriched": len(enriched),
        "new_listings": len(new_rows),
        "cache_hits": cache_hits,
        "blocked_or_errors": blocked,
        "errors": errors[:30],
        "live_stage_counts": dict(stage_counts),
        "live_strict_fits": live_fits,
        "jobs": {"discovery": discovery_job, "detail": detail_job},
        "within_100": (provider_delta is None or provider_delta <= 100)
        and test_used_after <= 100,
    }


def main() -> int:
    started = time.time()
    engage_emergency_stop(reason="BUILD_002K_START")
    pause_scheduler(reason="BUILD_002K_START")

    # 1) Offline recovery FIRST
    recovery = reprocess_full_population()

    # 2) Safeguard tests
    tests = run_pytest()

    # 3) Usage verification (free)
    usage = None
    usage_error = None
    try:
        usage = refresh_account_usage()
    except Exception as exc:
        usage_error = type(exc).__name__

    plan = dry_run_plan(recovery, usage or {})
    conditions = {
        "safeguard_tests_passed": bool(tests.get("passed")),
        "usage_verified": bool(usage and usage.get("ok") and usage.get("remaining") is not None),
        "estimated_total_max_le_100": plan["estimated_total_max"] <= 100,
        "monthly_reserve_ok": bool(
            usage
            and isinstance(usage.get("remaining"), int)
            and usage["remaining"] - plan["estimated_total_max"] >= 1000
        ),
    }
    can_run = all(conditions.values())

    live = None
    live_blocker = None
    try:
        if can_run:
            unlock_for_bounded_test()
            # Re-verify unlock + usage
            usage2 = refresh_account_usage()
            if not usage2.get("ok"):
                live_blocker = "usage_unreliable_after_unlock"
            else:
                live = live_bounded_test(plan)
        else:
            live_blocker = {
                "reason": "conditions_not_met",
                "conditions": conditions,
                "usage_error": usage_error,
            }
    finally:
        relock()

    # Post-lock usage probe
    usage_final = None
    try:
        usage_final = refresh_account_usage()
    except Exception as exc:
        usage_final = {"ok": False, "error": type(exc).__name__}

    # Coverage
    coverage = {
        "status": "PARTIAL",
        "benchmark_portals_claimed": None,
        "benchmark_note": "Do not claim ~3899 portal coverage without source-level evidence",
        "nextbid_sources_targeted": [
            "opengov",
            "bonfire",
            "planetbids",
            "calprocure",
            "publicpurchase",
            "instantmarkets",
        ],
        "sources_excluded": ["sam.gov", "dibbs"],
        "previously_acquired_unique": recovery.get("population", {}).get("unique_nextbid"),
        "newly_discovered_in_test": (live or {}).get("new_listings"),
        "offline_stage_b": recovery.get("stage_b_count"),
        "offline_stage_c": len(recovery.get("strict_fits") or []),
        "live_stage_c": len((live or {}).get("live_strict_fits") or []),
        "prior_free_source_fits": recovery.get("prior_free_source_count"),
        "gaps": [
            "Full commodities tradeline not paged (budget-bounded)",
            "Public Purchase historically low yield",
            "Portal auth shells for many attachments",
            "Nationwide free-source coverage separate from NextBid",
        ],
        "continuation_checkpoint": "data/nextbid_checkpoints/nextbid_acquire_v1.json",
    }

    # Combine fits for owner list (dedupe by nb_id / solicitation)
    all_fits = []
    seen_fit = set()
    for f in (recovery.get("strict_fits") or []) + ((live or {}).get("live_strict_fits") or []):
        key = f.get("nb_id") or f.get("solicitation_number") or f.get("title")
        if key in seen_fit:
            continue
        seen_fit.add(key)
        all_fits.append(f)
    for f in recovery.get("prior_free_source") or []:
        key = f.get("solicitation_number") or f.get("official_url")
        if key in seen_fit:
            continue
        seen_fit.add(key)
        all_fits.append(
            {
                "source": "free_source_prior",
                "solicitation_number": f.get("solicitation_number"),
                "title": f.get("product_description") or f.get("title"),
                "official_url": f.get("official_url"),
                "quantity": f.get("ordered_quantity"),
                "stage": "VERIFIED_M3_PROCUREMENT_FIT",
                "evidence": f.get("qualification_evidence") or f.get("quantity_evidence_location"),
            }
        )

    acceptance = {
        "build": "002K",
        "build_id": BUILD_ID,
        "started_at": _utc(),
        "finished_at": _utc(),
        "duration_seconds": round(time.time() - started, 1),
        "emergency_stop_final": emergency_stop_status(),
        "acquisition_locked_final": acquisition_locked(),
        "controller_final": controller_status(),
        "offline_recovery": {
            "unique_nextbid": recovery.get("population", {}).get("unique_nextbid"),
            "stage_b": recovery.get("stage_b_count"),
            "stage_c": len(recovery.get("strict_fits") or []),
            "quantity_defined": recovery.get("quantity_defined"),
            "listing_prefilter": recovery.get("listing_prefilter"),
            "stage_counts": recovery.get("stage_counts"),
            "prior_free_source_count": recovery.get("prior_free_source_count"),
            "metered_calls": 0,
            "artifact": recovery.get("artifact_path"),
        },
        "safeguard_tests": tests,
        "conditions": conditions,
        "can_run_live_test": can_run,
        "live_blocker": live_blocker,
        "dry_run_plan": plan,
        "live_test": live,
        "coverage": coverage,
        "qualifying_opportunity_list": all_fits,
        "api_consumption": {
            "provider_before_test": usage,
            "provider_after_test": usage_final,
            "local_test_used": (live or {}).get("local_test_used"),
            "provider_delta": (live or {}).get("provider_used_delta"),
            "within_100": (live or {}).get("within_100") if live else None,
            "calls_blocked": (live or {}).get("blocked_or_errors"),
        },
        "performance": {
            "offline_recovery_seconds": None,
            "live_test_seconds": (live or {}).get("duration_seconds"),
            "total_wall_seconds": round(time.time() - started, 1),
            "targets": {
                "incremental_target_min": 5,
                "incremental_max_min": 15,
                "daily_cycle_max_min": 30,
            },
            "note": "Incremental daily cycle not fully exercised as ongoing scheduler; bounded test only.",
        },
        "do_not_begin_build_003": True,
        "paid_acquisition_relocked": True,
        "await_owner_acceptance": True,
    }

    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "BUILD_002K_ACCEPTANCE.json").write_text(
        json.dumps(acceptance, indent=2, default=str), encoding="utf-8"
    )

    # Markdown reports
    (docs / "BUILD_002K_API_AUDIT.md").write_text(
        f"""# Build 002K — API Governance Audit

**Build ID:** `{BUILD_ID}`  
**Paid acquisition final state:** LOCKED  
**Safeguard tests:** {"PASSED" if tests.get("passed") else "FAILED"}

## Permanent hard limits (enforced in code)

| Limit | Value |
|-------|------:|
| Daily system-wide | 100 |
| Per job | 50 |
| Authorized test session | 100 |
| Monthly reserve floor | 1000 |

Effective available = lowest remaining applicable limit.

## Live test consumption

| Metric | Result |
|--------|--------|
| Provider before | used={ (usage or {}).get('used') } remaining={ (usage or {}).get('remaining') } |
| Provider after | used={ (usage_final or {}).get('used') } remaining={ (usage_final or {}).get('remaining') } |
| Provider delta | { (live or {}).get('provider_used_delta') } |
| Local test_used | { (live or {}).get('local_test_used') } |
| Within 100 | { (live or {}).get('within_100') } |
| Live executed | { can_run } |

## Conditions

```json
{json.dumps(conditions, indent=2)}
```

## Blocker (if any)

```json
{json.dumps(live_blocker, indent=2, default=str)}
```

Controller ledger: `data/nextbid_ledger/`. Cache: `data/nextbid_cache/`.
""",
        encoding="utf-8",
    )

    (docs / "BUILD_002K_RECOVERY_REPORT.md").write_text(
        f"""# Build 002K — Recovery Report

**Metered calls for offline recovery:** 0

| Metric | Count |
|--------|------:|
| Unique NextBid records recovered | {recovery.get('population', {}).get('unique_nextbid')} |
| Stage B product | {recovery.get('stage_b_count')} |
| Stage C strict (offline) | {len(recovery.get('strict_fits') or [])} |
| Prior free-source fits | {recovery.get('prior_free_source_count')} |
| Quantity defined | {recovery.get('quantity_defined')} |

## Live test additions

| Metric | Count |
|--------|------:|
| New listings | { (live or {}).get('new_listings') } |
| Details enriched | { (live or {}).get('detail_enriched') } |
| Live Stage C | { len((live or {}).get('live_strict_fits') or []) } |

## Qualifying opportunity list

See `BUILD_002K_ACCEPTANCE.json` → `qualifying_opportunity_list` ({len(all_fits)} entries).
""",
        encoding="utf-8",
    )

    (docs / "BUILD_002K_COVERAGE_REPORT.md").write_text(
        f"""# Build 002K — Coverage Report

**Status:** PARTIAL / honest — not nationwide-complete.

## NextBid source targets

Searched (bounded): OpenGov, Bonfire, PlanetBids, CalProcure, Public Purchase, InstantMarkets  
Excluded: sam.gov, dibbs (CAGE hold)

## Population

- Previously acquired unique: {recovery.get('population', {}).get('unique_nextbid')}
- Newly discovered in bounded test: { (live or {}).get('new_listings') }
- Offline Stage B: {recovery.get('stage_b_count')}
- Offline Stage C: {len(recovery.get('strict_fits') or [])}
- Live Stage C: {len((live or {}).get('live_strict_fits') or [])}
- Free-source prior fits: {recovery.get('prior_free_source_count')}

## Gaps

- Commodities tradeline not fully paged (budget)
- Attachment auth shells on many portals
- ~3899 portal benchmark is NOT claimed as M3 coverage

Checkpoint: `data/nextbid_checkpoints/nextbid_acquire_v1.json`
""",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "build": BUILD_ID,
                "tests_passed": tests.get("passed"),
                "can_run": can_run,
                "provider_delta": (live or {}).get("provider_used_delta"),
                "local_test_used": (live or {}).get("local_test_used"),
                "offline_stage_c": len(recovery.get("strict_fits") or []),
                "live_stage_c": len((live or {}).get("live_strict_fits") or []),
                "fits_total_listed": len(all_fits),
                "locked": acquisition_locked(),
            }
        )
    )
    return 0 if tests.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
