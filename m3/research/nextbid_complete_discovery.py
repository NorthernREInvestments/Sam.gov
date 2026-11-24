"""BUILD 002Q — Complete state/local discovery (listing-only) + offline qualify.

Owner-authorized max 123 metered listing calls. Detail/docs metered = ZERO.
If daily allowance insufficient, offline work only; preserve resume position.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.nextbid_controller import (
    BUILD_002Q_SCOPE,
    acquisition_locked,
    controller_status,
    load_budget_state,
    refresh_account_usage,
    revoke_owner_authorization,
    set_usage_reconcile_baseline,
    write_owner_authorization,
)
from m3.research.nextbid_controlled_production import (
    _load_coverage,
    run_listing_discovery,
)
from m3.research.nextbid_free_doc_recovery import recover_free_documents
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop
from m3.research.nextbid_status_reconciliation import (
    _controlled_pages_remaining,
    assign_category,
    detect_duplicates,
    load_retained_provider_index,
    reprocess_active_state_local,
    try_free_coverage_stats,
)
from m3.research.nextbid_acquire import EXCLUDED_SOURCES, _normalize_listing
from m3.research.nextbid_recovery import collect_population

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
LISTING_CAP = 123
PAGE_SIZE = 25


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[002q-complete-discovery] {msg}", flush=True)


def _authorize_listing_only(*, max_calls: int) -> dict[str, Any]:
    write_owner_authorization(
        authorized=True,
        max_additional_calls=max_calls,
        note="002Q_owner_authorized_complete_state_local_discovery_listing_only",
        scope=BUILD_002Q_SCOPE,
        test_call_cap=max_calls,
        allowed_endpoints=["search"],
        daily_limit_override=100,
        reset_test_used=False,  # never reset cumulative counters
    )
    os.environ["NEXTBID_ENABLED"] = "true"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    assert acquisition_locked() is False
    return controller_status()


def _relock(*, reason: str) -> None:
    engage_emergency_stop(reason=reason)
    revoke_owner_authorization()
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()


def assess_allowance() -> dict[str, Any]:
    """Verify daily reset and remaining allowance without unlocking."""
    # Roll day key the same way the controller does (local calendar date)
    st = load_budget_state()
    today = str(date.today())
    day_rolled = st.get("day") != today
    if day_rolled:
        st["day"] = today
        st["day_used"] = 0
        from m3.research.nextbid_controller import save_budget_state

        save_budget_state(st)

    cs = controller_status()
    bs = cs.get("budget_state") or {}
    limits = cs.get("limits") or {}
    day_used = int(bs.get("day_used") or 0)
    daily = int(limits.get("daily") or 100)
    daily_left = max(0, daily - day_used)
    job = int(limits.get("job") or 50)
    account_rem = int(bs.get("account_remaining") or 0)
    reserve = int(limits.get("monthly_reserve") or 1000)
    spendable = max(0, account_rem - reserve)
    campaign_listing_used = int(load_budget_state().get("campaign_listing_used") or 0)
    # This directive: max 123 additional listing; do not exceed campaign leftover either
    campaign_listing_left = max(0, 400 - campaign_listing_used)
    authorized_cap = min(LISTING_CAP, campaign_listing_left, spendable)
    effective = min(daily_left, job, authorized_cap, int(bs.get("effective_available") or 0) or daily_left)
    # Prefer explicit mins
    max_callable_now = min(daily_left, job, authorized_cap)

    controlled = _controlled_pages_remaining()
    pages_needed = int(controlled.get("pages_remaining") or 0)
    insufficient = max_callable_now < 1 or (
        pages_needed > 0 and max_callable_now < min(pages_needed, 10) and daily_left <= 1
    )
    # Owner: today's allowance effectively exhausted if daily_left <= 1 and pages_needed >> that
    if daily_left <= 1 and pages_needed > daily_left:
        insufficient = True

    resume_when = None
    if insufficient and daily_left < pages_needed:
        resume_when = (
            f"After local calendar day rolls past {today} "
            f"(Central), day_used resets to 0 and up to {daily} listing calls/day "
            f"can resume from controlled coverage checkpoints. "
            f"Do not auto-schedule."
        )

    return {
        "local_date": today,
        "utc_now": _utc(),
        "day_rolled_on_check": day_rolled,
        "day_used": day_used,
        "daily_limit": daily,
        "daily_left": daily_left,
        "job_limit": job,
        "account_remaining": account_rem,
        "protected_reserve": reserve,
        "spendable": spendable,
        "campaign_listing_used": campaign_listing_used,
        "campaign_listing_left": campaign_listing_left,
        "authorized_listing_cap": LISTING_CAP,
        "max_callable_now": max_callable_now,
        "pages_remaining_controlled": pages_needed,
        "pages_by_source": controlled.get("by_source"),
        "insufficient_daily_allowance": insufficient,
        "acquisition_can_resume": resume_when,
        "locked": acquisition_locked(),
    }


def _active_state_local_items() -> list[dict[str, Any]]:
    """Build working set: inventory Category A + open state/local from population."""
    inv = load_inventory()
    index = load_retained_provider_index()
    by_nb = index["by_nb"]
    search_hits = index["search_hits"]
    dup_of = detect_duplicates(inv, by_nb)
    now = datetime.now(timezone.utc)

    # Refresh categories for any new inventory rows lacking reconciliation_category
    patches: list[dict[str, Any]] = []
    active_ids: set[str] = set()
    for nb, row in inv.items():
        cat = row.get("reconciliation_category")
        if not cat:
            cat, patch = assign_category(
                nb_id=nb,
                inv_row=row,
                provider=by_nb.get(nb) or {},
                search_hits=search_hits.get(nb) or [],
                now=now,
                duplicate_of=dup_of.get(nb),
            )
            patches.append(patch)
        if cat == "A" or row.get("reconciliation_category") == "A":
            active_ids.add(nb)
        elif not cat:
            if patches and patches[-1].get("nb_id") == nb and patches[-1].get("reconciliation_category") == "A":
                active_ids.add(nb)

    if patches:
        upsert_inventory(patches)
        for p in patches:
            if p.get("reconciliation_category") == "A":
                active_ids.add(str(p["nb_id"]))

    # Also include inventory marked A already
    for nb, row in inv.items():
        if row.get("reconciliation_category") == "A":
            active_ids.add(nb)

    pop = collect_population()
    pop_by = pop.get("by_id") or {}
    items: list[dict[str, Any]] = []
    for nb in sorted(active_ids):
        base = dict(pop_by.get(nb) or by_nb.get(nb) or inv.get(nb) or {})
        inv_row = inv.get(nb) or {}
        for k, v in inv_row.items():
            if v not in (None, "", [], {}) and base.get(k) in (None, "", [], {}, "unknown"):
                base[k] = v
        src = str(base.get("source") or base.get("nextbid_source") or inv_row.get("source") or "").lower()
        if src in EXCLUDED_SOURCES or src in {"sam.gov", "dibbs"}:
            continue
        if base.get("response_deadline") and not base.get("deadline_at"):
            base["deadline_at"] = base["response_deadline"]
        if not base.get("provider_open_filter"):
            base["provider_open_filter"] = True
        if not base.get("solicitation_status") or base.get("solicitation_status") == "unknown":
            try:
                norm = _normalize_listing({**base, "provider_open_filter": True})
                for k, v in norm.items():
                    if base.get(k) in (None, "", [], {}, "unknown") and v not in (None, "", [], {}):
                        base[k] = v
            except Exception:
                base["solicitation_status"] = base.get("solicitation_status") or "open"
                base["lifecycle_status"] = base.get("lifecycle_status") or "open"
        base["nb_id"] = nb
        base["reconciliation_category"] = "A"
        items.append(base)
    return items


def _slim_actionable(it: dict[str, Any]) -> dict[str, Any]:
    pipe = it.get("procurement_pipeline") or {}
    bid = it.get("bid_eligibility") or {}
    return {
        "nb_id": it.get("nb_id"),
        "solicitation_number": it.get("solicitation_number"),
        "agency": it.get("buyer_name") or it.get("agency"),
        "title": (it.get("title") or "")[:200],
        "official_url": it.get("official_url") or it.get("source_url_original"),
        "deadline_at": it.get("deadline_at"),
        "requirement_class": it.get("requirement_class"),
        "required_products": it.get("line_items")
        or it.get("product_summary")
        or (it.get("title") or "")[:120],
        "quantity": it.get("quantity"),
        "quantity_semantic": it.get("quantity_semantic"),
        "specifications": {
            k: it.get(k)
            for k in (
                "manufacturer",
                "model_number",
                "part_number",
                "brand_restrictions",
                "approved_equivalents",
            )
            if it.get(k)
        },
        "submission_method": it.get("submission_method"),
        "submission_instructions": (str(it.get("submission_instructions") or "")[:300] or None),
        "procurement_eligible": bool(bid.get("bid_eligible")),
        "primary_disposition": it.get("primary_disposition"),
        "missing_flags": pipe.get("unknown_flags") or bid.get("unknown_flags"),
        "package_status": it.get("package_status"),
        "source": it.get("source") or it.get("nextbid_source"),
    }


def build_paid_recovery_candidates(classified: list[dict[str, Any]]) -> dict[str, Any]:
    """Prioritize product/promising rows where ONE paid detail/doc call could unblock."""
    candidates: list[dict[str, Any]] = []
    for it in classified:
        req = str(it.get("requirement_class") or "").lower()
        pipe = it.get("procurement_pipeline") or {}
        primary = it.get("primary_disposition") or ""
        if not pipe.get("product_opportunity") and req not in {
            "product_supply",
            "product_with_incidental_services",
            "unknown",
            "unknown_mixed",
            "pending_classification",
            "",
        }:
            continue
        if primary not in {
            "verified_product_information_needed",
            "potential_product_classification_needed",
            "verified_bid_opportunities",
        } and not pipe.get("product_opportunity"):
            continue

        missing: list[str] = []
        min_calls = 0
        # Prefer free already tried — only propose paid if thin text / no qty / no submission
        doclen = len(str(it.get("document_text") or it.get("description") or ""))
        if not pipe.get("quantity_defined") and not it.get("quantity"):
            missing.append("verified_quantity")
        if not (it.get("submission_method") or it.get("submission_instructions")):
            missing.append("submission_instructions")
        if doclen < 400 or (it.get("package_status") or "") in {"", "FAILED", "ACCESS_RESTRICTED"}:
            missing.append("specification_or_document_text")
        if not missing:
            continue
        # Estimate minimum metered: get_opportunity (1) + list_documents (1) optional
        # Owner: detail/document metered ZERO this job — this is a plan only
        if "specification_or_document_text" in missing or "verified_quantity" in missing:
            min_calls = 1  # get_opportunity often carries summary/qty
            if "specification_or_document_text" in missing:
                min_calls = 2  # + list_documents; read_file only if needed later
        elif "submission_instructions" in missing:
            min_calls = 1

        score = 0
        if pipe.get("product_opportunity"):
            score += 40
        if req in {"product_supply", "product_with_incidental_services"}:
            score += 30
        if pipe.get("quantity_defined"):
            score += 10
        if doclen < 200:
            score += 15
        score += max(0, 20 - min_calls * 5)
        candidates.append(
            {
                "nb_id": it.get("nb_id"),
                "title": (it.get("title") or "")[:160],
                "official_url": it.get("official_url"),
                "deadline_at": it.get("deadline_at"),
                "requirement_class": req,
                "missing_information": missing,
                "minimum_metered_calls": min_calls,
                "recommended_endpoints": (
                    ["get_opportunity", "list_documents"]
                    if min_calls >= 2
                    else ["get_opportunity"]
                ),
                "priority_score": score,
                "primary_disposition": primary,
            }
        )
    candidates.sort(key=lambda x: (-int(x["priority_score"]), str(x.get("deadline_at") or "9999")))
    total_min = sum(int(c["minimum_metered_calls"]) for c in candidates[:50])
    return {
        "count": len(candidates),
        "top": candidates[:40],
        "estimated_minimum_credits_top50": total_min,
        "note": "Plan only — this job spent ZERO detail/document credits.",
    }


def _write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    md = ARCHIVE / "BUILD_002Q_COMPLETE_STATE_LOCAL_DISCOVERY_ACCEPTANCE.md"
    js = ARCHIVE / "BUILD_002Q_COMPLETE_STATE_LOCAL_DISCOVERY_ACCEPTANCE.json"
    js.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    cov = payload.get("coverage") or {}
    allow = payload.get("allowance") or {}
    recon = payload.get("reprocess") or {}
    act = payload.get("actionable") or {}
    paid = payload.get("paid_recovery_plan") or {}
    listing = payload.get("listing") or {}
    resume = payload.get("resume_position") or {}

    lines = [
        "# BUILD 002Q — Complete State/Local Discovery",
        "",
        f"Generated: `{payload.get('generated_at')}`",
        f"Runtime: **{payload.get('elapsed_sec')}s**",
        f"Mode: **{payload.get('mode')}**",
        f"Metered delta: **{payload.get('metered_delta')}**",
        f"Acquisition locked: `{payload.get('acquisition_locked')}`",
        "",
        "## Allowance gate",
        "",
        f"- Local date: `{allow.get('local_date')}` (day_used={allow.get('day_used')}/{allow.get('daily_limit')})",
        f"- Daily left: **{allow.get('daily_left')}** · Job limit: {allow.get('job_limit')}",
        f"- Max callable now: **{allow.get('max_callable_now')}** · Authorized listing cap: {allow.get('authorized_listing_cap')}",
        f"- Insufficient daily allowance: `{allow.get('insufficient_daily_allowance')}`",
        f"- Resume: {allow.get('acquisition_can_resume') or 'n/a'}",
        "",
        "## Coverage",
        "",
        f"- Provider active state/local: **{cov.get('provider_active_state_local_total')}**",
        f"- M3 active state/local (Category A): **{cov.get('local_active_sl')}**",
        f"- Coverage: **{cov.get('coverage_percentage')}%**",
        f"- Remaining unacquired: **{cov.get('missing_or_unacquired_sl')}**",
        f"- By source: `{json.dumps(cov.get('provider_open_by_authorized_source'), default=str)}`",
        "",
        "## Listing acquisition",
        "",
        f"- Metered search calls: **{listing.get('search_calls_metered', 0)}**",
        f"- Unique new: {listing.get('unique_new', 0)}",
        f"- Cache hits: {listing.get('cache_hits', 0)}",
        f"- Stopped: {listing.get('stopped_reason')}",
        "",
        "## Resume position (preserved)",
        "",
        f"```json",
        json.dumps(resume, indent=2, default=str)[:2000],
        "```",
        "",
        "## Qualification (active state/local)",
        "",
        f"| Metric | Count |",
        f"|---|---:|",
        f"| Active SL reprocessed | {recon.get('active_reprocessed')} |",
        f"| Confirmed product | {recon.get('confirmed_product')} |",
        f"| Potential product | {recon.get('potential_product')} |",
        f"| Mixed product/service | {recon.get('mixed_product_service')} |",
        f"| Incompatible | {recon.get('incompatible')} |",
        f"| Verified quantities | {recon.get('verified_quantities')} |",
        f"| Procurement-eligible | {recon.get('procurement_eligible')} |",
        f"| Ready to submit (B) | {(act.get('counts') or {}).get('ready_to_submit')} |",
        f"| Registration (C) | {(act.get('counts') or {}).get('registration_required')} |",
        f"| Supplier pricing (A) | {(act.get('counts') or {}).get('supplier_pricing')} |",
        f"| Information recovery (D) | {(act.get('counts') or {}).get('information_recovery')} |",
        "",
        "## Actionable contracts",
        "",
        "### B — Ready to submit",
        "",
    ]
    ready = act.get("ready_to_submit") or []
    if not ready:
        lines.append("_None._")
    else:
        lines.append("| nb_id | sol# | qty | deadline | agency | title | URL |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in ready:
            lines.append(
                f"| {r.get('nb_id')} | {r.get('solicitation_number')} | {r.get('quantity')} | "
                f"{r.get('deadline_at')} | {str(r.get('agency') or '')[:40]} | "
                f"{str(r.get('title') or '')[:60].replace('|','/')} | {r.get('official_url')} |"
            )
    lines += ["", "### C — Registration required", ""]
    for r in (act.get("registration_required") or [])[:25]:
        lines.append(
            f"- {r.get('nb_id')} qty={r.get('quantity')} — {str(r.get('title') or '')[:90]}"
        )
    if not (act.get("registration_required") or []):
        lines.append("_None._")
    lines += ["", "### A — Supplier-pricing candidates (sample)", ""]
    for r in (act.get("supplier_pricing") or [])[:30]:
        lines.append(
            f"- {r.get('nb_id')} qty={r.get('quantity')} elig={r.get('procurement_eligible')} "
            f"— {str(r.get('title') or '')[:90]}"
        )
    lines += ["", "### D — Information recovery (sample)", ""]
    for r in (act.get("information_recovery") or [])[:30]:
        lines.append(
            f"- {r.get('nb_id')} missing={r.get('missing_flags')} — {str(r.get('title') or '')[:80]}"
        )
    lines += [
        "",
        "## Prioritized paid detail/document recovery (NOT executed)",
        "",
        f"- Candidates: {paid.get('count')}",
        f"- Estimated minimum credits (top 50): **{paid.get('estimated_minimum_credits_top50')}**",
        f"- {paid.get('note')}",
        "",
    ]
    for c in (paid.get("top") or [])[:20]:
        lines.append(
            f"- {c.get('nb_id')} calls={c.get('minimum_metered_calls')} "
            f"need={c.get('missing_information')} — {str(c.get('title') or '')[:70]}"
        )
    lines += [
        "",
        "## Free document recovery",
        "",
        f"`{json.dumps(payload.get('free_docs'), default=str)[:500]}`",
        "",
        "## Provider usage",
        "",
        f"- Before: `{json.dumps(payload.get('usage_before'), default=str)}`",
        f"- After: `{json.dumps(payload.get('usage_after'), default=str)}`",
        f"- Remaining balance: **{(payload.get('usage_after') or {}).get('remaining')}**",
        "",
        "## STOP",
        "",
        "Awaiting owner review. No Railway. No Build 003. No auto-scheduled acquisition.",
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    return md, js


def run_complete_state_local_discovery(db: Session) -> dict[str, Any]:
    t0 = time.time()
    assert acquisition_locked(), "must start locked"

    try:
        usage_before = refresh_account_usage(allow_when_locked=True)
        if usage_before.get("used") is not None:
            set_usage_reconcile_baseline(
                account_used=int(usage_before["used"]),
                note="002q_complete_discovery_start",
            )
    except Exception as exc:
        st0 = load_budget_state()
        usage_before = {
            "ok": False,
            "error": type(exc).__name__,
            "used": st0.get("account_used"),
            "remaining": st0.get("account_remaining"),
            "allowance": st0.get("account_allowance"),
            "from_budget_state": True,
        }
        _log(f"usage refresh failed (non-fatal offline): {exc}")

    allowance = assess_allowance()
    _log(
        f"allowance daily_left={allowance['daily_left']} max_now={allowance['max_callable_now']} "
        f"insufficient={allowance['insufficient_daily_allowance']} pages={allowance['pages_remaining_controlled']}"
    )

    listing: dict[str, Any] = {
        "search_calls_metered": 0,
        "unique_new": 0,
        "cache_hits": 0,
        "stopped_reason": "offline_only_insufficient_daily_allowance",
        "skipped": True,
    }
    mode = "offline_only"

    if not allowance["insufficient_daily_allowance"] and allowance["max_callable_now"] > 0:
        mode = "listing_then_offline"
        budget = int(allowance["max_callable_now"])
        try:
            _authorize_listing_only(max_calls=budget)
            # Ensure open=true rows carry provider_open_filter via normalize path:
            # run_listing_discovery already uses open=true params + _normalize_listing
            listing = run_listing_discovery(max_calls=budget)
            listing["skipped"] = False
        finally:
            _relock(reason="002Q_complete_state_local_discovery_listing_job_complete")
            assert acquisition_locked()
    else:
        _log("OFFLINE ONLY — daily allowance insufficient; resume position preserved")

    # Free official docs (zero metered NextBid)
    try:
        free_docs = recover_free_documents(db, limit=100)
    except Exception as exc:
        free_docs = {"ok": False, "error": type(exc).__name__, "metered_nextbid": 0}

    # Reprocess entire active state/local population
    active_items = _active_state_local_items()
    _log(f"reprocessing active SL n={len(active_items)}")
    recon = reprocess_active_state_local(db, active_items)

    # Enrich reprocess stats: mixed / incompatible from population file
    mixed = 0
    incompatible = 0
    classified_rows: list[dict[str, Any]] = []
    pop_path = Path(str(recon.get("population_path") or ""))
    if pop_path.exists():
        for line in pop_path.open(encoding="utf-8"):
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            classified_rows.append(it)
            req = str(it.get("requirement_class") or "").lower()
            if req == "product_with_incidental_services":
                mixed += 1
            if req in {
                "service",
                "construction",
                "not_applicable_non_solicitation",
                "incompatible",
            } or (it.get("procurement_pipeline") or {}).get("excluded"):
                incompatible += 1
    recon["mixed_product_service"] = mixed
    recon["incompatible"] = incompatible

    # Actionable queues from classified rows
    actionable = {
        "ready_to_submit": [],
        "registration_required": [],
        "supplier_pricing": [],
        "information_recovery": [],
    }
    for it in classified_rows:
        row = _slim_actionable(it)
        primary = it.get("primary_disposition")
        pipe = it.get("procurement_pipeline") or {}
        if primary == "verified_bid_opportunities":
            actionable["ready_to_submit"].append(row)
        elif primary == "easy_vendor_registration":
            actionable["registration_required"].append(row)
        elif primary in {
            "verified_product_information_needed",
            "potential_product_classification_needed",
        }:
            actionable["information_recovery"].append(row)
            if pipe.get("product_opportunity"):
                actionable["supplier_pricing"].append(row)
        elif pipe.get("product_opportunity") and pipe.get("quantity_defined"):
            actionable["supplier_pricing"].append(row)
    actionable["counts"] = {k: len(v) for k, v in actionable.items()}

    paid_plan = build_paid_recovery_candidates(classified_rows)

    # Coverage vs free stats (equivalent sources)
    a_count = sum(
        1 for r in load_inventory().values() if r.get("reconciliation_category") == "A"
    )
    # Also count newly acquired still marked A via reprocess length if inventory lag
    a_count = max(a_count, len(active_items))
    coverage = try_free_coverage_stats(usage_before, local_active_sl=a_count)

    # Resume position from controlled coverage
    cov_file = _load_coverage()
    resume_position = {
        "coverage_file": str(
            get_settings().data_root() / "nextbid_checkpoints" / "build002q_controlled_coverage.json"
        ),
        "pages_remaining": allowance.get("pages_remaining_controlled"),
        "by_source": allowance.get("pages_by_source"),
        "searches": {
            k: {
                "source": v.get("source"),
                "last_successful_page": v.get("last_successful_page"),
                "provider_total_pages": v.get("provider_total_pages"),
                "completion_status": v.get("completion_status"),
                "unique_records": v.get("unique_records"),
            }
            for k, v in (cov_file.get("searches") or {}).items()
            if v.get("completion_status") != "complete"
        },
        "next_authorized_listing_budget": LISTING_CAP,
        "do_not_auto_schedule": True,
    }

    try:
        usage_after = refresh_account_usage(allow_when_locked=True)
        if usage_after.get("used") is not None:
            set_usage_reconcile_baseline(
                account_used=int(usage_after["used"]),
                note="002q_complete_discovery_end",
            )
    except Exception as exc:
        st1 = load_budget_state()
        usage_after = {
            "ok": False,
            "error": type(exc).__name__,
            "used": st1.get("account_used"),
            "remaining": st1.get("account_remaining"),
            "allowance": st1.get("account_allowance"),
            "from_budget_state": True,
        }
        _log(f"usage refresh after failed (non-fatal): {exc}")

    metered_delta = 0
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])
    if mode == "offline_only":
        # Offline path must not unlock; treat unknown after as 0 when still locked
        if metered_delta not in (0, None) and usage_after.get("ok") is not False:
            raise RuntimeError(f"unexpected metered burn in offline mode: {metered_delta}")
        metered_delta = 0
    if metered_delta < 0:
        metered_delta = 0

    # Trim actionable lists in payload for JSON size
    payload = {
        "build": "002Q",
        "directive": "COMPLETE_STATE_LOCAL_DISCOVERY",
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "mode": mode,
        "allowance": allowance,
        "listing": listing,
        "resume_position": resume_position,
        "coverage": coverage,
        "free_docs": {
            "recovered_count": free_docs.get("recovered_count") or (free_docs.get("stats") or {}).get("recovered"),
            "restricted_count": free_docs.get("restricted_count") or (free_docs.get("stats") or {}).get("restricted"),
            "stats": free_docs.get("stats"),
            "metered_nextbid": free_docs.get("metered_nextbid", 0),
            "elapsed_sec": free_docs.get("elapsed_sec"),
        },
        "reprocess": recon,
        "actionable": {
            "counts": actionable["counts"],
            "ready_to_submit": actionable["ready_to_submit"],
            "registration_required": actionable["registration_required"][:50],
            "supplier_pricing": actionable["supplier_pricing"][:80],
            "information_recovery": actionable["information_recovery"][:80],
        },
        "paid_recovery_plan": paid_plan,
        "usage_before": usage_before,
        "usage_after": usage_after,
        "metered_delta": metered_delta,
        "acquisition_locked": acquisition_locked(),
        "stop": "AWAIT_OWNER_REVIEW",
    }
    md_path, json_path = _write_report(payload)
    payload["acceptance_report_md"] = str(md_path)
    payload["acceptance_report_json"] = str(json_path)
    _log(
        f"DONE mode={mode} metered={metered_delta} A={a_count} "
        f"coverage={coverage.get('coverage_percentage')} ready={actionable['counts'].get('ready_to_submit')}"
    )
    return payload
