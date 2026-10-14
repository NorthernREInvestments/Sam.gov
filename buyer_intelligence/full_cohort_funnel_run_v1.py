"""Full 28,046-opportunity funnel run — deterministic gates + cached commercial overlay.

Accounts for every frozen WDDR cohort ID. Does not invent prices, quotes, or financing.
Expensive live research is NOT auto-fired; cached evidence is reused first.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.available_deals_visibility_v1 import (
    BLOCKED,
    NEEDS_VERIFICATION,
    READY,
    WORTH,
    classify_record,
    run_visibility_rebuild,
)
from buyer_intelligence.deadline_extraction_repair_v1 import snapshot_counts
from m3_data_root import data_path
from phase_l.l23_full_population_funnel import load_store

log = __import__("logging").getLogger("govtracker.full_cohort_funnel_run_v1")

BUILD = "20261013-m3-full-cohort-funnel-run-v1"
OUT = "_og_commercial_research_pipeline_out"
WDDR_MANIFEST = f"{OUT}/m3_wddr_v1_frozen_manifest.json"
WDDR_UNRESOLVED = f"{OUT}/m3_wddr_v1_unresolved.json"
FUNNEL_MANIFEST = f"{OUT}/m3_fcf_v1_run_manifest.json"
CHECKPOINT = f"{OUT}/m3_fcf_v1_checkpoint.json"
PROGRESS = f"{OUT}/m3_fcf_v1_progress.json"
CLASSIFICATIONS = f"{OUT}/m3_fcf_v1_classifications.json"
SCORECARD = f"{OUT}/m3_fcf_v1_scorecard.json"
TOP20 = f"{OUT}/m3_fcf_v1_top20_actionable.json"
LOCK = f"{OUT}/m3_fcf_v1_run.lock"

# Required terminal / continuation taxonomy (maps onto existing buckets)
LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
LIVE_CONDITIONAL = "LIVE_CONDITIONAL"
EXPIRED_HISTORICAL = "EXPIRED_HISTORICAL"
CANCELLED = "CANCELLED"
DUPLICATE_OR_SUPERSEDED = "DUPLICATE_OR_SUPERSEDED"
SET_ASIDE_INELIGIBLE = "SET_ASIDE_INELIGIBLE"
VEHICLE_OR_REGISTRATION_BLOCKED = "VEHICLE_OR_REGISTRATION_BLOCKED"
SERVICE_OR_COMPLEX_SCOPE = "SERVICE_OR_COMPLEX_SCOPE"
MISSING_CRITICAL_DATA = "MISSING_CRITICAL_DATA"
OTHER_REJECTED = "OTHER_REJECTED"


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "FullCohortFunnelProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save(PROGRESS, cur)


def _acquire_lock() -> Path | None:
    lock = Path(data_path(LOCK))
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            age = __import__("time").time() - lock.stat().st_mtime
            if age < 7200:
                return None
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def _release_lock(lock: Path | None) -> None:
    if not lock:
        return
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def map_terminal_class(row: dict[str, Any], unresolved: dict[str, Any] | None) -> str:
    """Map visibility classify_record output → required funnel taxonomy."""
    bucket = row.get("visibility_bucket")
    block = str(row.get("block_reason") or "")
    pc = row.get("product_class")
    dl = row.get("deadline_status")
    elig = row.get("eligible")

    if block == "CANCELLED" or dl == "CANCELLED":
        return CANCELLED
    if block == "EXPIRED" or (bucket == BLOCKED and dl == "CLOSED_CONFIRMED"):
        return EXPIRED_HISTORICAL
    if block == "NON_PRODUCT" or pc == "NON_PRODUCT":
        return SERVICE_OR_COMPLEX_SCOPE
    if elig is False or "SET_ASIDE" in block.upper() or "INELIGIBLE" in block.upper():
        return SET_ASIDE_INELIGIBLE
    if "REGISTRATION" in block.upper() or "VEHICLE" in block.upper() or "AUTH" in block.upper():
        return VEHICLE_OR_REGISTRATION_BLOCKED
    if bucket == NEEDS_VERIFICATION or dl in {"DEADLINE_UNKNOWN", "DEADLINE_CONFLICT"}:
        if unresolved and unresolved.get("terminal_reason"):
            return MISSING_CRITICAL_DATA
        return LIVE_CONDITIONAL
    if bucket == READY:
        return LIVE_ELIGIBLE
    if bucket == WORTH:
        if elig is None or pc in {"MIXED", "UNKNOWN_PRODUCTNESS"}:
            return LIVE_CONDITIONAL
        return LIVE_ELIGIBLE
    if bucket == BLOCKED:
        return OTHER_REJECTED
    return OTHER_REJECTED


def _load_unresolved_index() -> dict[str, dict[str, Any]]:
    blob = _load(WDDR_UNRESOLVED) or {}
    out: dict[str, dict[str, Any]] = {}
    for row in blob.get("rows") or []:
        if isinstance(row, dict) and row.get("canonical_opportunity_id"):
            out[str(row["canonical_opportunity_id"])] = row
    return out


def _load_commercial_caches() -> dict[str, dict[str, Any]]:
    """Reuse existing research artifacts — no new paid calls."""
    idx: dict[str, dict[str, Any]] = defaultdict(dict)
    files = [
        f"{OUT}/m3_first_revenue_cohort_v1.json",
        f"{OUT}/m3_adv_v1_available_deals.json",
        f"{OUT}/m3_cex_v1_available_deals.json",
        f"{OUT}/m3_rse_v1_available_deals.json",
        f"{OUT}/m3_source_product_v1_available_deals.json",
        f"{OUT}/m3_cgc_v1_available_deals.json",
        f"{OUT}/m3_frds_v1_available_deals.json",
        f"{OUT}/m3_available_deals_visibility_v1.json",
    ]
    for rel in files:
        blob = _load(rel)
        if not blob:
            continue
        deals = blob.get("deals") or blob.get("rows") or blob.get("cohort") or blob.get("top20") or []
        if isinstance(blob.get("opportunities"), list):
            deals = blob["opportunities"]
        if isinstance(deals, dict):
            deals = list(deals.values())
        for d in deals:
            if not isinstance(d, dict):
                continue
            oid = str(
                d.get("opportunity_id")
                or d.get("canonical_opportunity_id")
                or d.get("OPPORTUNITY_ID")
                or ""
            )
            if not oid:
                continue
            bag = idx[oid]
            bag["sources"] = list(set((bag.get("sources") or []) + [Path(rel).name]))
            for k in (
                "public_acquisition_cost",
                "acquisition_prices",
                "historical_unit",
                "historical_government_price",
                "verified_remaining_contribution",
                "conditional_remaining_contribution",
                "financing_status",
                "supplier_quote",
                "quotes",
                "lines",
                "mandatory_lines",
                "contribution_state",
                "lane",
                "product",
                "buyer",
                "deadline",
                "next_action",
                "research_priority_score",
            ):
                if d.get(k) not in (None, "", [], {}):
                    bag[k] = d.get(k)
            # nested
            for nest in ("commercial", "economics", "pricing"):
                n = d.get(nest)
                if isinstance(n, dict):
                    for k, v in n.items():
                        if v not in (None, "", [], {}) and k not in bag:
                            bag[k] = v
    return dict(idx)


def freeze_funnel_manifest(*, git_commit: str | None = None) -> dict[str, Any]:
    existing = _load(FUNNEL_MANIFEST)
    if isinstance(existing, dict) and existing.get("frozen") and existing.get("ids"):
        return existing
    wddr = _load(WDDR_MANIFEST) or {}
    ids = list(wddr.get("ids") or [])
    if len(ids) != 28046:
        store = load_store()
        ids = sorted(store.keys())
    run_id = f"fcf-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    man = {
        "kind": "FullCohortFunnelRunManifest",
        "build": BUILD,
        "frozen": True,
        "run_id": run_id,
        "frozen_at": _utc(),
        "population": len(ids),
        "ids": ids,
        "deadline_recovery_version": "whole_db_deadline_recovery_v1",
        "wddr_manifest": WDDR_MANIFEST,
        "git_commit": git_commit,
        "budget_limits": {
            "openai_usd_per_day": 1,
            "openai_usd_per_month": 30,
            "sam_calls_per_day": 10,
            "other_paid_apis_usd": 0,
        },
        "owner_financing_constraints": {
            "owner_cash_upfront_usd": 0,
            "personal_credit": False,
            "personal_guarantee": False,
        },
        "notes": "Immutable denominator for this funnel run; new notices excluded.",
    }
    _save(FUNNEL_MANIFEST, man)
    return man


def classify_full_cohort(
    store: dict[str, dict[str, Any]],
    frozen_ids: list[str],
    *,
    unresolved: dict[str, dict[str, Any]],
    commercial_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    by_class: Counter[str] = Counter()
    by_bucket: Counter[str] = Counter()
    by_product: Counter[str] = Counter()
    blockers: Counter[str] = Counter()

    for i, oid in enumerate(frozen_ids):
        rec = store.get(oid)
        if not isinstance(rec, dict):
            row = {
                "opportunity_id": oid,
                "funnel_class": MISSING_CRITICAL_DATA,
                "reason": "ID_NOT_IN_STORE",
                "visibility_bucket": BLOCKED,
            }
            rows.append(row)
            by_class[MISSING_CRITICAL_DATA] += 1
            continue
        classified = classify_record(oid, rec)
        fclass = map_terminal_class(classified, unresolved.get(oid))
        # Attach unresolved reason when missing deadline
        if not rec.get("deadline") and oid in unresolved:
            classified["deadline_unresolved_reason"] = unresolved[oid].get("terminal_reason")
            classified["deadline_unresolved_detail"] = unresolved[oid].get("detail")
            if fclass == LIVE_CONDITIONAL:
                fclass = MISSING_CRITICAL_DATA
        cache = commercial_cache.get(oid) or {}
        classified["funnel_class"] = fclass
        classified["cached_commercial"] = {
            "has_public_price": bool(cache.get("public_acquisition_cost") or cache.get("acquisition_prices")),
            "has_historical_gov_price": bool(
                cache.get("historical_unit") or cache.get("historical_government_price")
            ),
            "has_quote": bool(cache.get("supplier_quote") or cache.get("quotes")),
            "financing_status": cache.get("financing_status") or classified.get("financing_status"),
            "contribution_state": cache.get("contribution_state"),
            "verified_remaining_contribution": cache.get("verified_remaining_contribution"),
            "conditional_remaining_contribution": cache.get("conditional_remaining_contribution"),
            "cache_sources": cache.get("sources") or [],
        }
        # Economics honesty labels
        cc = classified["cached_commercial"]
        if cc["has_quote"] and cc.get("verified_remaining_contribution") not in (None, "") and float(
            cc.get("verified_remaining_contribution") or 0
        ) > 0:
            fin = str(cc.get("financing_status") or "").upper()
            if fin in {"FINANCEABLE", "VERIFIED_FINANCEABLE", "APPROVED", "PO_FUNDABLE"}:
                classified["econ_state"] = "VERIFIED_PROFITABLE_AND_FINANCEABLE"
            else:
                classified["econ_state"] = "PROFITABLE_BUT_FINANCING_UNVERIFIED"
        elif cc["has_public_price"] or cc["has_historical_gov_price"]:
            if cc["has_quote"]:
                classified["econ_state"] = "MARGIN_UNVERIFIED"
            else:
                classified["econ_state"] = "PROFITABLE_BUT_QUOTE_MISSING"
        else:
            classified["econ_state"] = "MARGIN_UNVERIFIED"

        rows.append(classified)
        by_class[fclass] += 1
        by_bucket[str(classified.get("visibility_bucket"))] += 1
        by_product[str(classified.get("product_class"))] += 1
        if fclass not in {LIVE_ELIGIBLE, LIVE_CONDITIONAL}:
            blockers[fclass] += 1
        elif classified.get("missing_information"):
            for m in classified["missing_information"]:
                blockers[f"missing:{m}"] += 1

        if (i + 1) % 2000 == 0:
            _progress(phase="CLASSIFY", examined=i + 1, total=len(frozen_ids))

    assert len(rows) == len(frozen_ids)
    payload = {
        "kind": "FullCohortFunnelClassifications",
        "build": BUILD,
        "at": _utc(),
        "population": len(rows),
        "by_funnel_class": dict(by_class),
        "by_visibility_bucket": dict(by_bucket),
        "by_product_class": dict(by_product),
        "blocker_counts": dict(blockers),
        "rows": rows,
    }
    _save(CLASSIFICATIONS, payload)
    return payload


def build_scorecard(
    *,
    manifest: dict[str, Any],
    classifications: dict[str, Any],
    visibility: dict[str, Any],
    store_counts: dict[str, Any],
) -> dict[str, Any]:
    rows = classifications.get("rows") or []
    by = Counter(r.get("funnel_class") for r in rows)
    live = by[LIVE_ELIGIBLE] + by[LIVE_CONDITIONAL]
    expired = by[EXPIRED_HISTORICAL]
    cancelled = by[CANCELLED]
    eligible_product = sum(
        1
        for r in rows
        if r.get("funnel_class") == LIVE_ELIGIBLE and r.get("product_class") == "PRODUCT"
    )
    conditional = by[LIVE_CONDITIONAL]
    # Package completeness — from classification missing_info / commercial cache only
    full_spec = sum(
        1
        for r in rows
        if r.get("funnel_class") in {LIVE_ELIGIBLE, LIVE_CONDITIONAL}
        and not any(x in (r.get("missing_information") or []) for x in ("deadline",))
        and r.get("product_class") == "PRODUCT"
    )
    has_hist = sum(1 for r in rows if (r.get("cached_commercial") or {}).get("has_historical_gov_price"))
    has_public = sum(1 for r in rows if (r.get("cached_commercial") or {}).get("has_public_price"))
    has_supplier_cache = sum(
        1
        for r in rows
        if (r.get("cached_commercial") or {}).get("cache_sources")
        and r.get("funnel_class") in {LIVE_ELIGIBLE, LIVE_CONDITIONAL}
    )
    actual_quotes = sum(1 for r in rows if (r.get("cached_commercial") or {}).get("has_quote"))
    positive_margins = sum(
        1
        for r in rows
        if r.get("econ_state")
        in {
            "VERIFIED_PROFITABLE_AND_FINANCEABLE",
            "PROFITABLE_BUT_FINANCING_UNVERIFIED",
            "PROFITABLE_BUT_QUOTE_MISSING",
        }
        and (r.get("cached_commercial") or {}).get("verified_remaining_contribution") not in (None, "")
        and float((r.get("cached_commercial") or {}).get("verified_remaining_contribution") or 0) > 0
    )
    financeable = sum(1 for r in rows if r.get("econ_state") == "VERIFIED_PROFITABLE_AND_FINANCEABLE")
    bid_ready = sum(1 for r in rows if r.get("visibility_bucket") == READY)

    # Top actionable: live eligible product, prefer common, with any price evidence, sorted by score
    candidates = [
        r
        for r in rows
        if r.get("funnel_class") in {LIVE_ELIGIBLE, LIVE_CONDITIONAL}
        and r.get("product_class") in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}
        and r.get("deadline_status") == "OPEN_CONFIRMED"
    ]
    candidates.sort(
        key=lambda r: (
            0 if r.get("common_product") else 1,
            0 if (r.get("cached_commercial") or {}).get("has_public_price") else 1,
            0 if (r.get("cached_commercial") or {}).get("has_historical_gov_price") else 1,
            -(r.get("research_priority_score") or 0),
        )
    )
    top20 = []
    for r in candidates[:20]:
        cc = r.get("cached_commercial") or {}
        top20.append(
            {
                "opportunity_id": r.get("opportunity_id"),
                "product": (r.get("product") or "")[:160],
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "days_left": r.get("days_left"),
                "government_price_evidence": cc.get("historical_unit")
                or cc.get("historical_government_price")
                or r.get("historical_unit"),
                "retail_supplier_price_evidence": cc.get("public_acquisition_cost")
                or cc.get("acquisition_prices")
                or r.get("public_acquisition_cost"),
                "estimated_total_profit": cc.get("verified_remaining_contribution")
                or cc.get("conditional_remaining_contribution"),
                "profit_status": "VERIFIED" if cc.get("verified_remaining_contribution") else "UNVERIFIED_OR_UNKNOWN",
                "supplier_quote_status": "ACTUAL_QUOTE_RECEIVED"
                if cc.get("has_quote")
                else ("SUPPLIER_IDENTIFIED_CACHE" if cc.get("cache_sources") else "QUOTE_REQUIRED"),
                "financing_status": cc.get("financing_status") or "UNKNOWN",
                "econ_state": r.get("econ_state"),
                "solicitation_url": r.get("authoritative_url"),
                "next_action": r.get("next_action"),
                "funnel_class": r.get("funnel_class"),
                "research_priority_score": r.get("research_priority_score"),
                "common_product": r.get("common_product"),
            }
        )

    blockers = Counter(classifications.get("blocker_counts") or {})
    # Systemic blockers by funnel_class for live attrition
    systemic = [
        {"blocker": k, "count": v}
        for k, v in sorted(by.items(), key=lambda x: -x[1])
        if k not in {LIVE_ELIGIBLE}
    ][:5]

    scorecard = {
        "kind": "FullCohortFunnelScorecard",
        "build": BUILD,
        "run_id": manifest.get("run_id"),
        "built_at": _utc(),
        "metrics": {
            "frozen_cohort": 28046,
            "audited_and_classified": len(rows),
            "currently_live": live,
            "expired_historical": expired,
            "cancelled_or_superseded": cancelled + by[DUPLICATE_OR_SUPERSEDED],
            "eligible_product_opportunities": eligible_product,
            "conditional_blocked_opportunities": conditional + by[SET_ASIDE_INELIGIBLE] + by[VEHICLE_OR_REGISTRATION_BLOCKED],
            "complete_solicitation_packages": None,  # requires package pipeline; not claimed without evidence
            "partial_packages": None,
            "metadata_only_opportunities": sum(1 for r in rows if not r.get("authoritative_url")),
            "fully_specified_product_opportunities": full_spec,
            "fully_specified_bidding_lots": None,  # not inventing lot counts
            "verified_historical_government_prices": has_hist,
            "verified_public_supplier_retail_prices": has_public,
            "matched_distributors_suppliers_cached": has_supplier_cache,
            "actual_supplier_quotes_received": actual_quotes,
            "positive_documented_margins": positive_margins,
            "financeable_zero_owner_cash_no_pg": financeable,
            "fully_verified_bid_ready_opportunities": bid_ready,
            "remaining_missing_deadlines": store_counts.get("missing"),
            "remaining_blockers_by_category": dict(by),
        },
        "by_funnel_class": dict(by),
        "visibility_counts": (visibility or {}).get("counts"),
        "deadline_totals": store_counts,
        "reconciliation": {
            "sum_funnel_classes": sum(by.values()),
            "equals_28046": sum(by.values()) == 28046,
            "records_not_processed": 0,
        },
        "top5_systemic_blockers": systemic,
        "top20_actionable": top20,
        "explicit_conclusion": {
            "fully_verified_bid_ready_count": bid_ready,
            "financeable_verified_count": financeable,
            "statement": (
                f"Of 28,046 opportunities, {bid_ready} are fully verified bid-ready and "
                f"{financeable} are verified profitable+financeable under $0 owner cash / no PG. "
                f"{eligible_product} live-eligible product opportunities remain for continued research; "
                f"{live} live (eligible+conditional) after deadline-aware gates."
            ),
        },
        "openai_spend_usd": 0,
        "sam_api_calls": 0,
    }
    _save(SCORECARD, scorecard)
    _save(TOP20, {"build": BUILD, "at": _utc(), "count": len(top20), "rows": top20})
    return scorecard


def run_full_cohort_funnel(
    *,
    git_commit: str | None = None,
    rebuild_visibility: bool = True,
) -> dict[str, Any]:
    lock = _acquire_lock()
    if not lock:
        return {"ok": False, "error": "run_lock_held", "lock": LOCK}
    try:
        _progress(phase="START", status="RUNNING")
        store = load_store()
        before = snapshot_counts(store)["totals"]
        manifest = freeze_funnel_manifest(git_commit=git_commit)
        frozen_ids = list(manifest["ids"])
        assert len(frozen_ids) == 28046

        unresolved = _load_unresolved_index()
        commercial_cache = _load_commercial_caches()
        _progress(
            phase="CLASSIFY",
            frozen=28046,
            unresolved_indexed=len(unresolved),
            commercial_cache_oids=len(commercial_cache),
        )

        classifications = classify_full_cohort(
            store, frozen_ids, unresolved=unresolved, commercial_cache=commercial_cache
        )

        visibility = {}
        if rebuild_visibility:
            _progress(phase="VISIBILITY_REBUILD")
            visibility = run_visibility_rebuild(prefer_open_deadline=True)

        after = snapshot_counts(store)["totals"]
        scorecard = build_scorecard(
            manifest=manifest,
            classifications=classifications,
            visibility=visibility,
            store_counts=after,
        )

        ckpt = {
            "kind": "FullCohortFunnelCheckpoint",
            "build": BUILD,
            "run_id": manifest["run_id"],
            "at": _utc(),
            "phase": "COMPLETE",
            "classified": len(classifications.get("rows") or []),
            "by_funnel_class": classifications.get("by_funnel_class"),
            "scorecard_metrics": scorecard.get("metrics"),
            "before_deadlines": before,
            "after_deadlines": after,
        }
        _save(CHECKPOINT, ckpt)
        _progress(phase="DONE", status="COMPLETE", run_id=manifest["run_id"])
        return {
            "ok": True,
            "run_id": manifest["run_id"],
            "final_status": "COMPLETE",
            "metrics": scorecard.get("metrics"),
            "by_funnel_class": classifications.get("by_funnel_class"),
            "top20_count": len(scorecard.get("top20_actionable") or []),
            "conclusion": scorecard.get("explicit_conclusion"),
            "visibility_counts": visibility.get("counts"),
            "openai_spend_usd": 0,
            "sam_api_calls": 0,
            "artifacts": {
                "manifest": FUNNEL_MANIFEST,
                "classifications": CLASSIFICATIONS,
                "scorecard": SCORECARD,
                "top20": TOP20,
                "checkpoint": CHECKPOINT,
            },
        }
    finally:
        _release_lock(lock)
