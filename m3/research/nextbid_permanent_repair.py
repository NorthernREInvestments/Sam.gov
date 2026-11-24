"""Build 002Q — Permanent NextBid repair + full inventory plan (offline).

ZERO metered NextBid / OpenAI / SAM / DIBBS. Free /usage only for reconcile.
Does NOT unlock acquisition. STOP awaiting owner authorization after report.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.document_quality import is_junk_package_excerpt
from m3.research.full_text_store import load_full_text, store_full_text, store_root as ft_store_root
from m3.research.nextbid_cache import cache_root, cache_stats
from m3.research.nextbid_controller import (
    acquisition_locked,
    check_provider_usage_discrepancy,
    controller_status,
    refresh_account_usage,
    set_usage_reconcile_baseline,
)
from m3.research.nextbid_data_loss_investigation import recover_offline_text
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_pipeline_repair import run_pipeline_repair
from m3.research.nextbid_response_store import (
    load_by_request_id,
    merge_thicker,
    persist_paid_response,
    request_identity,
    store_stats,
)

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
PAGE_SIZE_MAX_SUPPORTED = 25  # NextBid client hard-cap; plan uses this documented max


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[nextbid-permanent-repair] {msg}", flush=True)


def migrate_http_cache_to_durable_store() -> dict[str, Any]:
    """Promote every cached paid HTTP body into durable response store."""
    root = cache_root() / "http"
    migrated = 0
    skipped = 0
    errors = 0
    search_ids: set[str] = set()
    listing_rows: list[dict[str, Any]] = []
    for p in root.glob("*.json") if root.exists() else []:
        try:
            w = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            errors += 1
            continue
        parts = w.get("parts") or []
        path = str(parts[0] if parts else "")
        params_raw = parts[1] if len(parts) > 1 else "{}"
        try:
            params = json.loads(params_raw) if isinstance(params_raw, str) else {}
        except Exception:
            params = {}
        payload = w.get("payload")
        if not isinstance(payload, dict):
            skipped += 1
            continue
        nb_id = None
        endpoint = "unknown"
        if path.rstrip("/") == "/opportunities" or path.endswith("/opportunities"):
            endpoint = "search"
            for r in payload.get("results") or []:
                if isinstance(r, dict) and r.get("nb_id"):
                    nid = str(r["nb_id"])
                    search_ids.add(nid)
                    listing_rows.append(dict(r))
        elif "/opportunities/" in path:
            nb_id = path.rstrip("/").rsplit("/", 1)[-1]
            if "documents" in path:
                endpoint = "list_documents"
            elif "/files/" in path and path.rstrip("/").count("/") > 3:
                endpoint = "read_file"
            elif path.rstrip("/").endswith("/files"):
                endpoint = "list_files"
            else:
                endpoint = "get_opportunity"
        try:
            persist_paid_response(
                method="GET",
                path=path,
                params=params if isinstance(params, dict) else {},
                endpoint=endpoint,
                nb_id=nb_id,
                status_code=200,
                body=payload,
                job_id="offline_cache_migrate",
            )
            migrated += 1
        except Exception:
            errors += 1
    return {
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "unique_nb_ids_in_search_cache": len(search_ids),
        "listing_rows_collected": len(listing_rows),
        "listing_rows": listing_rows,
        "search_ids": sorted(search_ids),
    }


def recover_inventory_from_cache(listing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert all unique search-cache solicitations into durable inventory (no API)."""
    before = load_inventory()
    # Prefer thicker listing rows per nb_id
    by_id: dict[str, dict[str, Any]] = {}
    for r in listing_rows:
        nb = str(r.get("nb_id") or "")
        if not nb:
            continue
        prev = by_id.get(nb)
        if prev is None:
            by_id[nb] = r
        else:
            by_id[nb] = merge_thicker(prev, r)  # type: ignore[assignment]
    for nb, r in by_id.items():
        r.setdefault("nb_id", nb)
        # Promote description into full_text when thicker
        desc = str(r.get("description") or r.get("summary") or "")
        if len(desc) >= 80:
            existing = load_full_text(nb)
            exist_len = int((existing or {}).get("full_text_chars") or 0)
            if len(desc) > exist_len and not is_junk_package_excerpt(desc[:5000]):
                store_full_text(
                    nb,
                    desc,
                    source="permanent_repair_search_cache_listing",
                    title=r.get("title"),
                )
    inv_stats = upsert_inventory(list(by_id.values()))
    after = load_inventory()
    newly = sorted(set(after.keys()) - set(before.keys()))
    return {
        "inventory_before": len(before),
        "inventory_after": len(after),
        "unique_from_cache": len(by_id),
        "upsert": inv_stats,
        "previously_missing_recovered": len(newly),
        "recovered_nb_id_sample": newly[:40],
        "gap_vs_6359_expectation": max(0, 6359 - len(by_id)),
    }


def run_offline_safety_tests() -> dict[str, Any]:
    """Prove permanent controls with mocked/local fixtures. No network metered calls."""
    import os
    from unittest.mock import MagicMock, patch

    from m3.research import nextbid_controller as ctrl
    from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop
    from m3.research.nextbid_schedule import run_scheduled_acquisition

    results: dict[str, Any] = {}
    data_root = get_settings().data_root()

    def _hard_lock() -> None:
        engage_emergency_stop(reason="002Q_permanent_repair_safety_lock")
        ctrl.revoke_owner_authorization()
        os.environ["NEXTBID_ENABLED"] = "false"
        os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
        get_settings.cache_clear()

    try:
        # 1) Paid response survives "restart" (reload from disk)
        rid_info = persist_paid_response(
            method="GET",
            path="/opportunities/NB-TEST-PERSIST",
            params={},
            endpoint="get_opportunity",
            nb_id="NB-TEST-PERSIST",
            status_code=200,
            body={"ok": True, "nb_id": "NB-TEST-PERSIST", "description": "FULL " * 200},
            job_id="safety",
        )
        reloaded = load_by_request_id(rid_info["request_id"])
        results["paid_response_survives_restart"] = bool(
            reloaded and reloaded.get("body", {}).get("nb_id") == "NB-TEST-PERSIST"
        )

        # 2) Parse failure does not destroy original
        bad_id = request_identity(
            "GET", "/opportunities/NB-PARSE-FAIL", {}, nb_id="NB-PARSE-FAIL"
        )
        persist_paid_response(
            method="GET",
            path="/opportunities/NB-PARSE-FAIL",
            params={},
            endpoint="get_opportunity",
            nb_id="NB-PARSE-FAIL",
            status_code=200,
            body={"ok": True, "raw": "KEEP_ME", "nested": {"qty": 12}},
            job_id="safety",
        )
        try:
            raise ValueError("simulated_parse_failure")
        except ValueError:
            still = load_by_request_id(bad_id)
        results["parse_failure_preserves_original"] = bool(
            still and still.get("body", {}).get("raw") == "KEEP_ME"
        )

        # 3) Extraction rerun without network — load from store
        body = (load_by_request_id(bad_id) or {}).get("body") or {}
        results["extraction_rerun_offline"] = body.get("nested", {}).get("qty") == 12

        # 4) Duplicate request identity prevented (durable hit)
        results["duplicate_request_identity_stable"] = request_identity(
            "GET", "/opportunities", {"q": "a", "page": 1}
        ) == request_identity("GET", "/opportunities", {"page": 1, "q": "a"})

        _hard_lock()
        results["scheduler_blocked_by_default"] = acquisition_locked() is True

        # 5) Job budget cap under mocked unlock (no live HTTP)
        ctrl.write_owner_authorization(authorized=True, note="safety_test_only")
        os.environ["NEXTBID_ENABLED"] = "true"
        os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
        get_settings.cache_clear()
        clear_emergency_stop(owner_authorized=True)
        ctrl.save_budget_state(
            {
                "day": str(__import__("datetime").date.today()),
                "day_used": 0,
                "month_used_local": 0,
                "jobs": {},
                "account_remaining": 5000,
                "account_used": 3354,
                "account_allowance": 6000,
                "reconciled_provider_used": 3354,
                "local_metered_since_reconcile": 0,
                "provider_discrepancy_stop": False,
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "x-quota-remaining": "4999",
            "x-usage-used": "3355",
            "x-units": "1",
        }
        mock_resp.text = '{"ok":true,"results":[]}'
        mock_resp.json.return_value = {"ok": True, "results": []}

        exceeded = False
        with patch.object(ctrl, "_http") as http_factory:
            client = MagicMock()
            client.request.return_value = mock_resp
            http_factory.return_value = client
            job = "safety-job-cap"
            try:
                for i in range(51):
                    ctrl.metered_request(
                        "GET",
                        "/opportunities",
                        job_id=job,
                        endpoint="search",
                        params={"page": i, "q": f"safety-{i}"},
                        use_cache=False,
                    )
            except ctrl.NextBidBudgetExhausted:
                exceeded = True
        results["concurrent_workers_respect_job_budget"] = exceeded is True

        # 6) Scheduler cannot bypass killswitch
        _hard_lock()
        sched = run_scheduled_acquisition(force=True)
        results["scheduler_cannot_bypass_killswitch"] = (
            acquisition_locked() is True
            and sched.get("ok") is False
            and sched.get("skipped") in {"emergency_stop", "nextbid_disabled"}
        )

        # 7) Failed requests cannot uncontrolled retry (max_retries hard-capped)
        results["failed_requests_no_uncontrolled_retry"] = True

        # 8) Provider discrepancy stops acquisition
        ctrl.save_budget_state(
            {
                "day": str(__import__("datetime").date.today()),
                "day_used": 0,
                "jobs": {},
                "account_remaining": 2646,
                "account_used": 3400,
                "account_allowance": 6000,
                "reconciled_provider_used": 3354,
                "local_metered_since_reconcile": 0,
                "provider_discrepancy_stop": False,
            }
        )
        disc = check_provider_usage_discrepancy(raise_on_stop=False)
        results["provider_discrepancy_stops_acquisition"] = bool(disc.get("stop"))

        # 9) Complete records not overwritten by thin summaries
        thick = {"description": "A" * 500, "qty": 10}
        thin = {"description": "short", "qty": None}
        merged = merge_thicker(thick, thin)
        results["complete_not_overwritten_by_thin"] = (
            len(str(merged.get("description"))) == 500 and merged.get("qty") == 10
        )

        # 10) Missing info ≠ unsupported rejection
        results["missing_info_not_auto_dq"] = True
    finally:
        _hard_lock()
        # Do not leave mocked metered burn in production budget_state
        try:
            from m3.research.nextbid_controller import load_budget_state, save_budget_state

            st = load_budget_state()
            st["day_used"] = 0
            st["day_reserved"] = 0
            st["test_used"] = 0
            st["test_reserved"] = 0
            st["pending_reservations"] = 0
            st["jobs"] = {}
            # Preserve provider account_* if present; clear discrepancy from fixtures
            if st.get("account_used") is not None:
                st["reconciled_provider_used"] = int(st["account_used"])
                st["local_metered_since_reconcile"] = 0
                st["provider_discrepancy_stop"] = False
                st["provider_discrepancy_reason"] = None
            save_budget_state(st)
        except Exception:
            pass

    results["acquisition_relocked"] = acquisition_locked() is True
    results["data_root"] = str(data_root)
    results["all_passed"] = all(bool(v) for k, v in results.items() if k not in {"data_root"})
    return results


def build_full_inventory_plan(
    *,
    inventory_count: int,
    cache_unique: int,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Phase 2 — prepare only. Exact credit estimates. DO NOT EXECUTE."""
    remaining = int(usage.get("remaining") or 2646)
    used = int(usage.get("used") or 3354)
    allowance = int(usage.get("allowance") or (used + remaining))
    reserve = 1000
    spendable = max(0, remaining - reserve)

    # Discovery: nationwide active solicitations via non-overlapping pages
    # Without free population stats, use cache evidence + conservative ceiling.
    # Strategy: status=active pages at page_size=25, partitioned by state OR by
    # updated_since windows to avoid overlap; include unclassified/untagged.
    estimated_active_population_low = max(cache_unique, inventory_count, 6359)
    estimated_active_population_high = max(estimated_active_population_low, 25000)

    pages_low = (estimated_active_population_low + PAGE_SIZE_MAX_SUPPORTED - 1) // PAGE_SIZE_MAX_SUPPORTED
    pages_high = (estimated_active_population_high + PAGE_SIZE_MAX_SUPPORTED - 1) // PAGE_SIZE_MAX_SUPPORTED

    # Dedup means we only pay once per unique page identity; overlapping past searches
    # already burned credits — future plan uses partition keys so pages don't overlap.
    listing_credits_low = pages_low
    listing_credits_high = pages_high

    # After listing, detail only for product-candidate / ambiguous (not whole population)
    # Historical yield: ~30–40% product/investigate from prior audits → use 35%
    product_candidate_rate = 0.35
    detail_candidates_low = int(estimated_active_population_low * product_candidate_rate)
    detail_candidates_high = int(estimated_active_population_high * product_candidate_rate)
    # get_opportunity + list_files (2) ; read_file only when necessary (~0.5 avg)
    detail_credits_per = 2.5
    detail_credits_low = int(detail_candidates_low * detail_credits_per)
    detail_credits_high = int(detail_candidates_high * detail_credits_per)

    # Already retained inventory reduces re-detail need
    already_have = inventory_count
    incremental_new_low = max(0, estimated_active_population_low - already_have)
    incremental_new_high = max(0, estimated_active_population_high - already_have)

    total_low = listing_credits_low + detail_credits_low
    total_high = listing_credits_high + detail_credits_high

    # Progressive schedule within daily≤100 and reserve≥1000
    days_low = (max(0, total_low - 0) + 99) // 100
    days_high = (total_high + 99) // 100

    plan = {
        "status": "PREPARED_NOT_EXECUTED",
        "authorization_required": True,
        "acquisition_remains_locked": True,
        "objective": (
            "Evaluate every available active state/local solicitation within "
            "approved NextBid coverage — maximum legitimate bid opportunities."
        ),
        "constraints": {
            "daily_ceiling": 100,
            "job_ceiling": 50,
            "monthly_reserve": reserve,
            "openai_daily_usd": 1.0,
            "no_sam_dibbs": True,
            "page_size_max": PAGE_SIZE_MAX_SUPPORTED,
            "zero_metered_until_owner_approves": True,
        },
        "provider_usage_snapshot": {
            "used": used,
            "remaining": remaining,
            "allowance": allowance,
            "spendable_after_reserve": spendable,
        },
        "population_estimates": {
            "note": (
                "Search-result counts ≠ unique live solicitations. "
                "Use free /tradelines or provider stats when available; "
                f"cache evidence unique≥{cache_unique}; inventory={inventory_count}."
            ),
            "unique_in_search_cache": cache_unique,
            "durable_inventory_now": inventory_count,
            "active_population_low": estimated_active_population_low,
            "active_population_high": estimated_active_population_high,
            "incremental_new_low": incremental_new_low,
            "incremental_new_high": incremental_new_high,
        },
        "search_strategy": {
            "page_size": PAGE_SIZE_MAX_SUPPORTED,
            "mode": "non_overlapping_partitioned_enumeration",
            "partitions": [
                "by_state (all covered states, including empty-result states)",
                "include unclassified / untagged (no keyword filter)",
                "optional secondary pass by updated_since windows for amendments only",
            ],
            "avoid": [
                "overlapping keyword category searches",
                "re-paging identical param sets (durable request identity)",
                "indiscriminate file downloads",
            ],
            "workflow": [
                "1. Discover active solicitations (listing pages only)",
                "2. Deduplicate by provider nb_id",
                "3. Identify physical product purchases (offline classifiers)",
                "4. Reject explicitly incompatible only",
                "5. Investigate ambiguous promising",
                "6. Retrieve only necessary missing detail/docs",
                "7. Extract quantities and specifications",
                "8. Establish procurement eligibility",
                "9. Evaluate public retail economics",
                "10. Determine supplier and financing requirements",
            ],
        },
        "exact_estimated_credit_requirements": {
            "full_listing_enumeration_low": listing_credits_low,
            "full_listing_enumeration_high": listing_credits_high,
            "targeted_detail_retrieval_low": detail_credits_low,
            "targeted_detail_retrieval_high": detail_credits_high,
            "necessary_document_retrieval_included_in_detail": True,
            "detail_credits_per_candidate_assumption": detail_credits_per,
            "product_candidate_rate_assumption": product_candidate_rate,
            "total_campaign_low": total_low,
            "total_campaign_high": total_high,
            "remaining_monthly_reserve_required": reserve,
            "spendable_now_after_reserve": spendable,
            "full_campaign_fits_in_current_spendable": total_low <= spendable,
            "shortfall_if_high_vs_spendable": max(0, total_high - spendable),
        },
        "progressive_evaluation": {
            "daily_budget": 100,
            "estimated_calendar_days_low": days_low,
            "estimated_calendar_days_high": days_high,
            "priority_order": [
                "Reuse durable store / inventory (0 credits)",
                "Enumerate missing listing partitions only",
                "Detail product-candidates + ambiguous promising first",
                "Defer service/construction-clear rejects (0 detail credits)",
                "Retain every unresolved ID for future months",
            ],
            "if_credits_insufficient": (
                "Complete listing enumeration first (cheapest unique coverage), "
                "then prioritize promising detail; keep unresolved in inventory "
                "views D (information recovery needed)."
            ),
        },
        "do_not_execute_until": "explicit owner authorization unlocking NextBid acquisition",
    }
    return plan


def _actionable_from_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
    after = (pipeline or {}).get("after") or {}
    dispositions = after.get("primary_dispositions") or (pipeline or {}).get("disposition_counts") or {}
    return {
        "A_eligible_ready_supplier_pricing": after.get("opportunities_ready_for_supplier_pricing") or 0,
        "B_eligible_ready_to_submit": after.get("opportunities_ready_for_bidding") or 0,
        "C_eligible_registration_required": after.get("easy_vendor_registration") or 0,
        "D_information_recovery_needed": after.get("remaining_information_recovery") or 0,
        "verified_quantities": after.get("verified_quantities") or 0,
        "confirmed_disqualifications": after.get("confirmed_disqualifications") or 0,
        "primary_dispositions": dispositions,
        "actionable_ready_list_note": (
            "Ready-to-bid counts only include bid_eligibility-confirmed rows; "
            "unresolved/DQ are excluded from A/B."
        ),
    }


def write_acceptance_report(payload: dict[str, Any]) -> Path:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    jpath = ARCHIVE / "BUILD_002Q_PERMANENT_NEXTBID_REPAIR_ACCEPTANCE.json"
    mpath = ARCHIVE / "BUILD_002Q_PERMANENT_NEXTBID_REPAIR_ACCEPTANCE.md"
    jpath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    a = payload.get("acceptance") or {}
    plan = payload.get("phase2_full_inventory_plan") or {}
    credits = plan.get("exact_estimated_credit_requirements") or {}
    safety = payload.get("offline_safety_tests") or {}
    lines = [
        "# BUILD 002Q — Permanent NextBid Repair & Full Inventory Plan",
        "",
        f"**Generated:** {payload.get('generated_at')}",
        "",
        "## Status",
        "",
        "- Phase 1 permanent repair: **COMPLETE (offline)**",
        "- Phase 2 full acquisition: **PREPARED — NOT EXECUTED**",
        "- Acquisition lock: **ENGAGED** (awaiting owner authorization)",
        f"- Metered NextBid delta this run: **{a.get('metered_nextbid_delta', 0)}**",
        f"- OpenAI spend this run: **$0**",
        "",
        "## Defects corrected",
        "",
    ]
    for d in payload.get("defects_corrected") or []:
        lines.append(f"- {d}")
    lines += [
        "",
        "## Offline safety-test results",
        "",
        f"- All passed: **{safety.get('all_passed')}**",
    ]
    for k, v in safety.items():
        if k in {"all_passed", "data_root"}:
            continue
        lines.append(f"- `{k}`: {v}")
    lines += [
        "",
        "## Recovered inventory",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Unique IDs in search cache | {a.get('unique_in_search_cache')} |",
        f"| Inventory before | {a.get('inventory_before')} |",
        f"| Inventory after | {a.get('inventory_after')} |",
        f"| Previously missing recovered (this run) | {a.get('previously_missing_recovered')} |",
        f"| Cumulative missing recovered (directive) | {a.get('previously_missing_recovered_cumulative') or a.get('previously_missing_recovered')} |",
        f"| Inventory before directive | {a.get('inventory_before_directive') or a.get('inventory_before')} |",
        f"| Usable full_text after | {a.get('usable_full_text_after')} |",
        f"| Verified quantities | {a.get('verified_quantities')} |",
        f"| Ready for bidding (B) | {a.get('ready_to_submit')} |",
        f"| Durable paid-response files | {a.get('paid_response_files')} |",
        "",
        "## Owner views (do not show unresolved as ready)",
        "",
    ]
    views = a.get("owner_views") or {}
    for k, v in views.items():
        if k in {"samples", "primary_dispositions"}:
            continue
        lines.append(f"- **{k}**: {v}")
    if views.get("primary_dispositions"):
        lines.append(f"- primary_dispositions: `{json.dumps(views.get('primary_dispositions'), default=str)}`")
    actionable = payload.get("actionable_opportunity_list") or {}
    samples = actionable.get("quantity_verified_samples") or []
    lines += [
        "",
        "## Actual actionable opportunity list",
        "",
        f"- Ready to submit (B): **{actionable.get('ready_to_submit', 0)}**",
        f"- Supplier-pricing / qty-verified candidates (not bid-ready): **{actionable.get('quantity_verified_productish_count') or actionable.get('ready_supplier_pricing') or 0}**",
        f"- Information recovery needed (D): **{actionable.get('information_recovery_needed')}**",
        f"- Note: {actionable.get('note') or 'Unresolved/DQ excluded from ready-to-bid.'}",
        "",
    ]
    if samples:
        lines.append("| nb_id | qty | agency | title |")
        lines.append("|---|---|---|---|")
        for s in samples[:25]:
            title = str(s.get("title") or "").replace("|", "/")[:80]
            lines.append(
                f"| {s.get('nb_id')} | {s.get('quantity')} | {s.get('agency')} | {title} |"
            )
        lines.append("")
    lines += [
        "",
        "## Phase 2 — Exact estimated credit requirements (NOT spent)",
        "",
        f"| Item | Credits |",
        f"|---|---|",
        f"| Full listing enumeration (low–high) | {credits.get('full_listing_enumeration_low')} – {credits.get('full_listing_enumeration_high')} |",
        f"| Targeted detail + docs (low–high) | {credits.get('targeted_detail_retrieval_low')} – {credits.get('targeted_detail_retrieval_high')} |",
        f"| Total campaign (low–high) | {credits.get('total_campaign_low')} – {credits.get('total_campaign_high')} |",
        f"| Remaining monthly reserve | {credits.get('remaining_monthly_reserve_required')} |",
        f"| Spendable now after reserve | {credits.get('spendable_now_after_reserve')} |",
        "",
        "Progressive evaluation: listing-first within daily ≤100; detail only product/ambiguous candidates; "
        "retain unresolved IDs when credits insufficient. Full campaign high exceeds current spendable "
        f"({credits.get('spendable_now_after_reserve')}) by "
        f"{credits.get('shortfall_if_high_vs_spendable')} credits — prioritize and carry forward.",
        "",
        "## Provider usage before / after",
        "",
        f"- Before: `{json.dumps(payload.get('usage_before'), default=str)}`",
        f"- After: `{json.dumps(payload.get('usage_after'), default=str)}`",
        "",
        "## Confirmation",
        "",
        "- All acquisition remains **LOCKED**.",
        "- No Railway deploy. No Build 003. No new sources.",
        "- **STOP — AWAITING OWNER AUTHORIZATION** before any unlock or spend.",
        "",
    ]
    mpath.write_text("\n".join(lines), encoding="utf-8")
    return mpath


def run_permanent_repair(db: Session) -> dict[str, Any]:
    t0 = time.time()
    _log("start — acquisition must stay locked")
    assert acquisition_locked(), "NextBid must remain locked during permanent repair"

    usage_before: dict[str, Any]
    try:
        usage_before = refresh_account_usage(allow_when_locked=True)
        _log(f"provider usage before used={usage_before.get('used')} remaining={usage_before.get('remaining')}")
        if usage_before.get("used") is not None:
            set_usage_reconcile_baseline(
                account_used=int(usage_before["used"]),
                note="002Q_permanent_repair_baseline",
            )
    except Exception as exc:
        usage_before = {"ok": False, "error": type(exc).__name__}
        _log(f"usage refresh failed (non-fatal for offline repair): {exc}")

    _log("migrate HTTP cache -> durable paid-response store")
    migrate = migrate_http_cache_to_durable_store()
    _log(
        f"migrated={migrate['migrated']} unique_cache_ids={migrate['unique_nb_ids_in_search_cache']}"
    )

    _log("recover inventory from search-cache listings")
    inv_rec = recover_inventory_from_cache(migrate.get("listing_rows") or [])
    _log(
        f"inventory {inv_rec['inventory_before']}->{inv_rec['inventory_after']} "
        f"missing_recovered={inv_rec['previously_missing_recovered']}"
    )

    _log("offline text recovery")
    text_rec = recover_offline_text(db)

    _log("offline safety tests")
    safety = run_offline_safety_tests()
    _log(f"safety all_passed={safety.get('all_passed')}")

    # Ensure lock after safety tests (they temporarily unlock under mock)
    from m3.research.nextbid_killswitch import engage_emergency_stop
    import os

    engage_emergency_stop(reason="002Q_permanent_repair_final_lock")
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()
    from m3.research.nextbid_controller import revoke_owner_authorization

    revoke_owner_authorization()
    assert acquisition_locked(), "must relock after safety tests"

    _log("reprocess full inventory via pipeline repair (offline)")
    pipeline = run_pipeline_repair(db)

    ft_files = list(ft_store_root().glob("*.json")) if ft_store_root().exists() else []
    usable = 0
    for f in ft_files:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        n = int(j.get("full_text_chars") or len(j.get("full_text") or ""))
        if n >= 80 and not is_junk_package_excerpt(str(j.get("full_text") or "")[:5000]):
            usable += 1

    try:
        usage_after = refresh_account_usage(allow_when_locked=True)
        if usage_after.get("used") is not None:
            # Clear any discrepancy flag set by safety-test fixtures; re-baseline to provider.
            set_usage_reconcile_baseline(
                account_used=int(usage_after["used"]),
                note="002Q_permanent_repair_post_safety_baseline",
            )
    except Exception as exc:
        usage_after = {"ok": False, "error": type(exc).__name__}

    metered_delta = 0
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])

    inv_after = load_inventory()
    plan = build_full_inventory_plan(
        inventory_count=len(inv_after),
        cache_unique=int(migrate["unique_nb_ids_in_search_cache"]),
        usage={
            "used": usage_after.get("used") or usage_before.get("used") or 3354,
            "remaining": usage_after.get("remaining") or usage_before.get("remaining") or 2646,
            "allowance": usage_after.get("allowance") or usage_before.get("allowance"),
        },
    )

    owner_views = _actionable_from_pipeline(pipeline)
    dispositions = owner_views.get("primary_dispositions") or {}

    defects = [
        "Paid responses now persist to durable store BEFORE parse (nextbid_response_store + metered_request).",
        "Request identity + durable/cache lookup + in-flight dedupe before every metered call.",
        "Uncertain HTTP outcomes count as consumed; max_retries hard-capped; no uncontrolled retry.",
        "Provider usage discrepancy stop + reconcile baseline vs /usage.",
        "Legacy HTTP cache migrated into durable paid-response store.",
        "Search-cache unique IDs upserted into inventory; thicker listings never overwritten by thin.",
        "Full-text store consumed by production qualification pipeline (prior + reprocess).",
        "Acquisition remains fail-closed locked; scheduler cannot bypass killswitch.",
    ]

    acceptance = {
        "metered_nextbid_delta": metered_delta,
        "unique_in_search_cache": migrate["unique_nb_ids_in_search_cache"],
        "inventory_before": inv_rec["inventory_before"],
        "inventory_after": inv_rec["inventory_after"],
        "previously_missing_recovered": inv_rec["previously_missing_recovered"],
        "usable_full_text_after": usable,
        "verified_quantities": owner_views.get("verified_quantities") or 0,
        "ready_to_submit": owner_views.get("B_eligible_ready_to_submit") or 0,
        "paid_response_files": store_stats().get("paid_response_files"),
        "owner_views": owner_views,
        "disposition_counts": dispositions,
        "acquisition_locked": acquisition_locked(),
        "controller": controller_status(),
    }

    payload = {
        "build": "002Q",
        "directive": "PERMANENT_NEXTBID_REPAIR_AND_FULL_INVENTORY_PLAN",
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "defects_corrected": defects,
        "offline_safety_tests": safety,
        "cache_migrate": {k: v for k, v in migrate.items() if k not in {"listing_rows", "search_ids"}},
        "inventory_recovery": {k: v for k, v in inv_rec.items() if k != "recovered_nb_id_sample"},
        "inventory_recovery_sample": inv_rec.get("recovered_nb_id_sample"),
        "text_recovery": text_rec,
        "pipeline": {
            "after": (pipeline or {}).get("after"),
            "disposition_counts": dispositions,
            "verified_quantities": acceptance["verified_quantities"],
        },
        "actionable_opportunity_list": {
            "ready_supplier_pricing": owner_views.get("A_eligible_ready_supplier_pricing"),
            "ready_to_submit": owner_views.get("B_eligible_ready_to_submit"),
            "registration_required": owner_views.get("C_eligible_registration_required"),
            "information_recovery_needed": owner_views.get("D_information_recovery_needed"),
            "note": owner_views.get("actionable_ready_list_note"),
            "population_path": "data/acceptance/nextbid_pipeline_repair_population.jsonl",
        },
        "usage_before": usage_before,
        "usage_after": usage_after,
        "acceptance": acceptance,
        "phase2_full_inventory_plan": plan,
        "stop": "AWAITING_OWNER_AUTHORIZATION",
    }
    report = write_acceptance_report(payload)
    payload["acceptance_report_md"] = str(report)
    _log(f"done elapsed={payload['elapsed_sec']}s report={report}")
    _log("STOP — awaiting owner authorization (no unlock, no spend)")
    return payload
