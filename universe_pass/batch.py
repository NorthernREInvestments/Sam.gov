"""Full-universe product classification + BidNet freshness + profit-first routing.

Batch-safe with checkpoints. Does not add discovery sources or redesign economics.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from universe_pass.classify import (
    CONSTRUCTION,
    ELIGIBLE_FOR_PROFIT,
    MIXED_PRODUCT_SERVICE,
    PURE_SERVICE,
    TANGIBLE_PRODUCT,
    UNKNOWN,
    classify_universe_opportunity,
)
from universe_pass.freshness import (
    DUPLICATE,
    EXPIRED,
    LIVE_CONFIRMED,
    LIVE_PROBABLE,
    STALE,
    UNKNOWN_FRESHNESS,
    amendment_or_duplicate_key,
    assess_freshness,
)

log = logging.getLogger("govtracker.universe_pass")

CHECKPOINT_NAME = "m3_universe_pass_checkpoint.json"
REPORT_NAME = "m3_universe_pass_last_report.json"


def _checkpoint_path():
    from m3_data_root import data_path

    return data_path(CHECKPOINT_NAME)


def _report_path():
    from m3_data_root import data_path

    return data_path(REPORT_NAME)


def _load_checkpoint() -> dict[str, Any]:
    path = _checkpoint_path()
    if not path.exists():
        return {"kind": "UniversePassCheckpoint", "classified_ids": [], "profit_ids": [], "done": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "UniversePassCheckpoint", "classified_ids": [], "profit_ids": [], "done": False}


def _save_checkpoint(payload: dict[str, Any]) -> None:
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _platform_bucket(rec: dict[str, Any]) -> str:
    plat = str(rec.get("platform") or "").lower()
    if "bidnet" in plat:
        return "BidNet"
    if "socrata" in plat or "structured" in plat:
        return "Socrata"
    if "opengov" in plat:
        return "OpenGov"
    if "bonfire" in plat:
        return "Bonfire"
    if "jaggaer" in plat or "sciquest" in plat:
        return "Jaggaer"
    if "planetbids" in plat:
        return "PlanetBids"
    if "sam" in plat:
        return "SAM"
    if "dibbs" in plat:
        return "DIBBS"
    if "ionwave" in plat:
        return "IonWave"
    if "public_purchase" in plat or "publicpurchase" in plat:
        return "PublicPurchase"
    return plat or "Other"


def _apply_freshness_to_store(rec: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Mutate rec; return True if removed from live (expired/stale/duplicate)."""
    state = fresh["freshness_state"]
    rec["discovery_freshness_state"] = state
    rec["discovery_freshness_detail"] = {
        "reason": fresh.get("reason"),
        "deadline": fresh.get("deadline"),
        "metadata_only": fresh.get("metadata_only"),
        "actionable": fresh.get("actionable"),
        "assessed_at": now_utc().isoformat(),
    }
    changed_live = False
    if state in {EXPIRED, STALE}:
        if state == EXPIRED:
            rec["freshness"] = "EXPIRED"
            if str(rec.get("current_funnel_state") or "") not in {
                "FAST_REJECT",
                "HARD_REJECT",
                "CANCELED",
                "CANCELLED",
            }:
                rec["current_funnel_state"] = "EXPIRED"
        else:
            rec["freshness"] = "CANCELED" if fresh.get("reason") == "cancelled" else "STALE"
            if str(rec.get("current_funnel_state") or "") not in {"EXPIRED", "HARD_REJECT"}:
                rec["current_funnel_state"] = "CANCELED" if fresh.get("reason") == "cancelled" else "EXPIRED"
        changed_live = True
    elif state == DUPLICATE:
        rec["freshness"] = "DUPLICATE"
        rec["current_funnel_state"] = "EXPIRED"
        changed_live = True
    elif state == LIVE_CONFIRMED:
        # Do not overwrite richer freshness enums; keep LIVE
        if str(rec.get("freshness") or "").upper() in {"", "LAST_KNOWN_RECENT", "UNKNOWN"}:
            rec["freshness"] = "LIVE"
    elif state == LIVE_PROBABLE:
        # Explicit: not confirmed — leave available but flag
        if str(rec.get("freshness") or "").upper() in {"LIVE", "LIVE_FRESH"}:
            # Downgrade silent LIVE_FRESH when no deadline
            rec["freshness"] = "LIVE"
        rec["freshness_confidence"] = "PROBABLE"
    elif state == UNKNOWN_FRESHNESS:
        rec["freshness_confidence"] = "UNKNOWN"
        # Keep in live count until proven stale — but never claim confirmed
        if str(rec.get("freshness") or "").upper() in {"LIVE_FRESH"}:
            rec["freshness"] = "LIVE"
    return changed_live


def run_universe_pass(
    *,
    classify: bool = True,
    freshness: bool = True,
    profit_route: bool = True,
    limit: int | None = None,
    profit_limit: int | None = None,
    resume: bool = True,
    persist: bool = True,
    force_reclassify: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Order: classify → freshness/dedupe → recalculate live → profit-first on products.
    Checkpointed; safe to resume.
    """
    from m3_canonical_discovery_bridge import available_count, refresh_expiry_status
    from phase_l.l23_full_population_funnel import load_store, save_store
    from phase_l.owner_ui_service import _is_available_rec
    from profit_first.router import evaluate_opportunity_profit

    run_id = run_id or f"UNI-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    store = load_store()
    before_live = available_count(store)
    ck = _load_checkpoint() if resume else {
        "kind": "UniversePassCheckpoint",
        "classified_ids": [],
        "profit_ids": [],
        "done": False,
    }
    classified_done = set(ck.get("classified_ids") or [])
    profit_done = set(ck.get("profit_ids") or [])

    class_counts: Counter = Counter()
    fresh_counts: Counter = Counter()
    by_source: dict[str, Counter] = defaultdict(Counter)
    bidnet_audit: Counter = Counter()
    removed = 0
    dupes_collapsed = 0
    classified_n = 0
    profit_n = 0
    product_ids: list[str] = []

    # --- Phase A: classify + freshness ---
    seen_keys: dict[str, str] = {}  # amendment key → canonical id kept
    items = list(store.items())
    if limit is not None:
        items = items[:limit]

    for cid, rec in items:
        if not isinstance(rec, dict):
            continue
        # Skip already dead unless force
        was_available = _is_available_rec(rec)

        if classify and (force_reclassify or cid not in classified_done or not rec.get("universe_class")):
            result = classify_universe_opportunity(rec)
            rec["universe_class"] = result["class"]
            rec["product_service_classification"] = result["class"]
            rec["universe_classification"] = result
            rec["eligible_for_profit_research"] = result["eligible_for_profit_research"]
            classified_done.add(cid)
            classified_n += 1
        else:
            result = rec.get("universe_classification") or {
                "class": rec.get("universe_class") or rec.get("product_service_classification") or UNKNOWN
            }

        cls = str(result.get("class") or UNKNOWN)
        if was_available or _is_available_rec(rec):
            class_counts[cls] += 1

        if freshness:
            fresh = assess_freshness(rec)
            # Amendment / duplicate reconciliation
            key = amendment_or_duplicate_key(rec)
            if key and key in seen_keys and seen_keys[key] != cid:
                # Prefer the one already classified as product, else earlier
                keep_id = seen_keys[key]
                keep = store.get(keep_id) or {}
                keep_cls = str(keep.get("universe_class") or "")
                if cls in ELIGIBLE_FOR_PROFIT and keep_cls not in ELIGIBLE_FOR_PROFIT:
                    # Swap: mark old as duplicate, keep this
                    old = store[keep_id]
                    old_fresh = {**fresh, "freshness_state": DUPLICATE, "reason": "amendment_or_duplicate"}
                    if _apply_freshness_to_store(old, old_fresh):
                        removed += 1
                        dupes_collapsed += 1
                    seen_keys[key] = cid
                    fresh = assess_freshness(rec)
                else:
                    fresh = {**fresh, "freshness_state": DUPLICATE, "reason": "amendment_or_duplicate"}
                    dupes_collapsed += 1
            elif key:
                seen_keys[key] = cid

            if _apply_freshness_to_store(rec, fresh):
                if was_available and not _is_available_rec(rec):
                    removed += 1
            fresh_counts[fresh["freshness_state"]] += 1
            if fresh.get("is_bidnet"):
                bidnet_audit["checked"] += 1
                bidnet_audit[fresh["freshness_state"]] += 1

            plat = _platform_bucket(rec)
            by_source[plat]["live"] += 1 if _is_available_rec(rec) else 0
            by_source[plat][cls] += 1 if _is_available_rec(rec) else 0
            if fresh["freshness_state"] in {STALE, EXPIRED, DUPLICATE}:
                by_source[plat]["stale"] += 1

        if cls in ELIGIBLE_FOR_PROFIT and _is_available_rec(rec):
            product_ids.append(cid)

        rec["updated_at"] = now_utc().isoformat()

        if persist and classified_n and classified_n % 2000 == 0:
            ck.update(
                {
                    "run_id": run_id,
                    "classified_ids": list(classified_done)[-50000:],
                    "profit_ids": list(profit_done),
                    "updated_at": now_utc().isoformat(),
                }
            )
            _save_checkpoint(ck)
            save_store(store)
            log.info("universe_pass checkpoint classified=%s", classified_n)

    # Deadline-based expiry sweep (existing)
    expiry = refresh_expiry_status(store)
    after_freshness_live = available_count(store)

    # --- Phase B: profit-first on product subset ---
    econ_counts: Counter = Counter()
    acquisition_found = 0
    gov_found = 0
    both_sides = 0
    public_retail_profit = 0
    unresolved = 0

    try:
        from line_item_economics.engine import load_analysis

        _load_lie = load_analysis
    except Exception:
        _load_lie = lambda _oid: None  # noqa: E731

    targets_ids = product_ids
    if profit_limit is not None:
        targets_ids = product_ids[:profit_limit]

    if profit_route:
        for cid in targets_ids:
            if cid in profit_done and not force_reclassify:
                # Still count from attached
                pf = (store.get(cid) or {}).get("profit_first") or {}
                st = pf.get("profit_status") or "UNPROVEN"
                econ_counts[st] += 1
                continue
            rec = store.get(cid)
            if not isinstance(rec, dict) or not _is_available_rec(rec):
                continue
            lie = _load_lie(cid)
            # Prefer multi-line engine when schedule present
            if lie is None:
                # Detect attachment schedules — do not download; mark research next
                pass
            signals = {
                "exact_identity": bool(rec.get("solicitation_event_id") or rec.get("authoritative_url")),
                "public_retail_available": bool(
                    ((rec.get("row_ref") or {}) if isinstance(rec.get("row_ref"), dict) else {})
                    .get("economics", {})
                    .get("public_retail_total")
                    if isinstance(((rec.get("row_ref") or {}) if isinstance(rec.get("row_ref"), dict) else {}).get("economics"), dict)
                    else False
                ),
            }
            ev = evaluate_opportunity_profit(
                opportunity_id=cid,
                rec=rec,
                title=rec.get("title"),
                buyer=rec.get("buyer"),
                line_item_analysis=lie,
                ranking_signals=signals,
                execution_pass=None,
            )
            econ = ev.get("economics") or {}
            status = econ.get("profit_status") or "UNPROVEN"
            econ_counts[status] += 1
            signals_list = econ.get("proof_signals") or []
            if "PROFITABLE_AT_PUBLIC_RETAIL" in signals_list:
                public_retail_profit += 1
                econ_counts["PROFITABLE_AT_PUBLIC_RETAIL"] += 1
            rev = econ.get("expected_revenue")
            cost = econ.get("product_cost")
            if cost is not None:
                acquisition_found += 1
            if rev is not None:
                gov_found += 1
            if cost is not None and rev is not None:
                both_sides += 1
            else:
                unresolved += 1

            rec["profit_first"] = {
                "profit_status": status,
                "expected_profit": econ.get("expected_profit"),
                "post_financing_profit": econ.get("post_financing_profit"),
                "route": ev.get("route"),
                "owner_card": ev.get("owner_card"),
                "ranking_score": (ev.get("ranking") or {}).get("profit_probability_score"),
                "research_priority": (ev.get("research") or {}).get("priority"),
                "proof_signals": signals_list,
                "missing_facts": econ.get("missing_facts"),
                "evaluated_at": ev.get("evaluated_at"),
                "universe_pass_run_id": run_id,
            }
            profit_done.add(cid)
            profit_n += 1

            if persist and profit_n % 500 == 0:
                ck.update(
                    {
                        "run_id": run_id,
                        "classified_ids": list(classified_done)[-50000:],
                        "profit_ids": list(profit_done),
                        "updated_at": now_utc().isoformat(),
                    }
                )
                _save_checkpoint(ck)
                save_store(store)

    after_live = available_count(store)

    # Rebuild class counts on post-freshness available
    class_counts_live: Counter = Counter()
    fresh_unknown_live = 0
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_available_rec(rec):
            continue
        cls = str(rec.get("universe_class") or rec.get("product_service_classification") or UNKNOWN)
        class_counts_live[cls] += 1
        if str(rec.get("discovery_freshness_state") or "") == UNKNOWN_FRESHNESS:
            fresh_unknown_live += 1

    source_table = []
    for plat, ctr in sorted(by_source.items(), key=lambda x: -x[1].get("live", 0)):
        source_table.append(
            {
                "source": plat,
                "live": ctr.get("live", 0),
                "product": ctr.get(TANGIBLE_PRODUCT, 0),
                "mixed": ctr.get(MIXED_PRODUCT_SERVICE, 0),
                "service": ctr.get(PURE_SERVICE, 0),
                "construction": ctr.get(CONSTRUCTION, 0),
                "unknown": ctr.get(UNKNOWN, 0),
                "stale": ctr.get("stale", 0),
            }
        )

    report = {
        "kind": "UniversePassReport",
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "before_canonical_live": before_live,
        "after_freshness_cleanup": after_freshness_live,
        "after_canonical_live": after_live,
        "removed_from_live": removed,
        "duplicates_amendments_collapsed": dupes_collapsed,
        "expiry_sweep": expiry,
        "classified": classified_n,
        "classification": {
            TANGIBLE_PRODUCT: class_counts_live.get(TANGIBLE_PRODUCT, 0),
            MIXED_PRODUCT_SERVICE: class_counts_live.get(MIXED_PRODUCT_SERVICE, 0),
            PURE_SERVICE: class_counts_live.get(PURE_SERVICE, 0),
            CONSTRUCTION: class_counts_live.get(CONSTRUCTION, 0),
            UNKNOWN: class_counts_live.get(UNKNOWN, 0),
        },
        "product_candidates": class_counts_live.get(TANGIBLE_PRODUCT, 0)
        + class_counts_live.get(MIXED_PRODUCT_SERVICE, 0),
        "freshness": dict(fresh_counts),
        "freshness_unknown_still_live": fresh_unknown_live,
        "bidnet_audit": {
            "checked": bidnet_audit.get("checked", 0),
            "live_confirmed": bidnet_audit.get(LIVE_CONFIRMED, 0),
            "live_probable": bidnet_audit.get(LIVE_PROBABLE, 0),
            "stale": bidnet_audit.get(STALE, 0),
            "expired": bidnet_audit.get(EXPIRED, 0),
            "duplicates": bidnet_audit.get(DUPLICATE, 0),
            "unknown_freshness": bidnet_audit.get(UNKNOWN_FRESHNESS, 0),
        },
        "by_source": source_table,
        "product_economics": {
            "product_candidates_routed": profit_n,
            "product_candidates_total": len(product_ids),
            "acquisition_evidence_found": acquisition_found,
            "government_value_evidence_found": gov_found,
            "both_sides_known": both_sides,
            "profitable_at_public_retail": public_retail_profit,
            "proven_profitable": econ_counts.get("PROVEN_PROFITABLE", 0),
            "likely_profitable": econ_counts.get("LIKELY_PROFITABLE", 0),
            "possible_profit": econ_counts.get("POSSIBLE_PROFIT", 0),
            "unproven": econ_counts.get("UNPROVEN", 0),
            "unprofitable": econ_counts.get("UNPROFITABLE", 0),
            "execution_blocked": econ_counts.get("EXECUTION_BLOCKED", 0),
            "unresolved": unresolved,
            "status_counts": dict(econ_counts),
        },
        "SAM_API_CALLS": 0,
    }

    if persist:
        save_store(store)
        ck.update(
            {
                "run_id": run_id,
                "classified_ids": list(classified_done)[-80000:],
                "profit_ids": list(profit_done),
                "done": True,
                "updated_at": now_utc().isoformat(),
                "last_report_summary": {
                    "after_canonical_live": after_live,
                    "product_candidates": report["product_candidates"],
                    "proven": report["product_economics"]["proven_profitable"],
                },
            }
        )
        _save_checkpoint(ck)
        _report_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def universe_funnel_dashboard() -> dict[str, Any]:
    """Owner-facing funnel counts from store + last report."""
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import _is_available_rec
    from m3_canonical_discovery_bridge import available_count

    store = load_store()
    live = available_count(store)
    classes: Counter = Counter()
    fresh_unknown = 0
    two_sided = 0
    public_retail = 0
    proven = 0
    likely = 0
    possible = 0
    for rec in store.values():
        if not isinstance(rec, dict) or not _is_available_rec(rec):
            continue
        cls = str(rec.get("universe_class") or rec.get("product_service_classification") or UNKNOWN)
        classes[cls] += 1
        if str(rec.get("discovery_freshness_state") or "") == UNKNOWN_FRESHNESS:
            fresh_unknown += 1
        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        st = str(pf.get("profit_status") or "")
        if st == "PROVEN_PROFITABLE":
            proven += 1
        elif st == "LIKELY_PROFITABLE":
            likely += 1
        elif st == "POSSIBLE_PROFIT":
            possible += 1
        signals = pf.get("proof_signals") or []
        if "PROFITABLE_AT_PUBLIC_RETAIL" in signals:
            public_retail += 1
        card = pf.get("owner_card") or {}
        if card.get("expected_revenue") is not None and card.get("product_cost") is not None:
            two_sided += 1
        elif pf.get("expected_profit") is not None and st not in {"", "UNPROVEN"}:
            # profit computed implies both sides were known at eval
            if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT", "UNPROFITABLE"}:
                two_sided += 1

    last = {}
    try:
        path = _report_path()
        if path.exists():
            last = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        last = {}

    return {
        "kind": "UniverseFunnelDashboard",
        "generated_at": now_utc().isoformat(),
        "Canonical_Live": live,
        "Product_Candidates": classes.get(TANGIBLE_PRODUCT, 0),
        "Mixed_Product_Candidates": classes.get(MIXED_PRODUCT_SERVICE, 0),
        "Pure_Services": classes.get(PURE_SERVICE, 0),
        "Construction": classes.get(CONSTRUCTION, 0),
        "Unknown_Classification": classes.get(UNKNOWN, 0),
        "Freshness_Unknown": fresh_unknown,
        "Two_Sided_Economics": two_sided,
        "Profitable_at_Public_Retail": public_retail,
        "Proven_Profitable": proven,
        "Likely_Profitable": likely,
        "Possible_Profit": possible,
        "last_pass": {
            "run_id": last.get("run_id"),
            "completed_at": last.get("completed_at"),
            "by_source": last.get("by_source"),
            "bidnet_audit": last.get("bidnet_audit"),
            "product_economics": last.get("product_economics"),
        }
        if last
        else None,
    }
