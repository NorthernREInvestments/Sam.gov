"""Micro-Purchase Economics Lab — queue, CRUD orchestration, dashboard."""

from __future__ import annotations

import re
from typing import Any

from micro_purchase_lab_config import (
    CLASS_ABOVE,
    CLASS_MICRO,
    CLASS_NEAR,
    CLASS_SMALL_SA,
    CLASS_UNKNOWN,
    classify_opportunity_size,
    is_micro_experiment_band,
    threshold_config,
)
from micro_purchase_lab_economics import (
    build_quote_request_text,
    enrich_test,
)
from micro_purchase_lab_store import MicroPurchaseLabStore

_PRODUCT_HINT = re.compile(
    r"\b(NSN|NIIN|supply|supplies|equipment|hardware|tool|tools|parts?|PPE|safety|"
    r"pump|motor|filter|cable|hose|valve|fastener|meter|scanner|printer|laptop|"
    r"monitor|server|janitorial|consumable|kit|bearing|gasket|battery)\b",
    re.I,
)
_SERVICE_HINT = re.compile(
    r"\b(services?|consulting|staffing|janitorial\s+services|landscap|mowing|"
    r"construction|installation\s+only|professional\s+services)\b",
    re.I,
)


def get_store() -> MicroPurchaseLabStore:
    return MicroPurchaseLabStore()


def list_tests(*, classification: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    rows = [enrich_test(t) for t in get_store().all() if not t.get("archived")]
    if classification:
        rows = [r for r in rows if r.get("classification") == classification]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


def get_test(test_id: str) -> dict[str, Any] | None:
    row = get_store().get(test_id)
    return enrich_test(row) if row else None


def save_test(payload: dict[str, Any]) -> dict[str, Any]:
    store = get_store()
    row = enrich_test(dict(payload))
    return enrich_test(store.upsert(row))


def create_manual_test(payload: dict[str, Any]) -> dict[str, Any]:
    body = {
        "linked_opportunity_id": None,
        "solicitation": payload.get("solicitation") or payload.get("solicitation_number"),
        "source": payload.get("source") or payload.get("source_id") or "MANUAL",
        "agency": payload.get("agency"),
        "product": payload.get("product") or payload.get("product_description") or payload.get("title"),
        "manufacturer": payload.get("manufacturer"),
        "part_number": payload.get("part_number"),
        "nsn": payload.get("nsn"),
        "quantity": payload.get("quantity"),
        "government_unit": payload.get("government_unit") or payload.get("unit_of_issue") or "EA",
        "commercial_units_per_gov_unit": payload.get("commercial_units_per_gov_unit") or 1,
        "deadline": payload.get("deadline") or payload.get("response_deadline"),
        "delivery_location": payload.get("delivery_location"),
        "opportunity_url": payload.get("opportunity_url") or payload.get("url"),
        "estimated_opportunity_size": payload.get("estimated_opportunity_size") or payload.get("estimated_value"),
        "identity_confidence": payload.get("identity_confidence") or "UNKNOWN",
        "notes": payload.get("notes"),
        "historical_awards": payload.get("historical_awards") or [],
        "historical_market_costs": payload.get("historical_market_costs") or [],
        "current_market_prices": payload.get("current_market_prices") or [],
        "supplier_quotes": payload.get("supplier_quotes") or [],
        "min_gross_profit_target": payload.get("min_gross_profit_target") or 0,
        "min_gross_margin_pct_target": payload.get("min_gross_margin_pct_target"),
        "opportunity_status": "OPEN",
        "quote_status": "NONE",
        "validation_status": "NOT_TESTED",
    }
    body["classification"] = payload.get("classification") or classify_opportunity_size(
        body.get("estimated_opportunity_size")
    )
    return save_test(body)


def load_from_opportunity(canonical_id: str) -> dict[str, Any]:
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    row = store.get(canonical_id)
    if not row:
        raise KeyError(canonical_id)
    est = row.get("estimated_value") or row.get("government_revenue") or row.get("historical_award_amount")
    payload = {
        "linked_opportunity_id": canonical_id,
        "solicitation": row.get("solicitation_number") or row.get("external_id"),
        "source": row.get("source_id") or row.get("preferred_source_id"),
        "agency": row.get("agency"),
        "product": row.get("title"),
        "manufacturer": row.get("manufacturer") or (row.get("product_identity") or {}).get("manufacturer"),
        "part_number": row.get("exact_part_number")
        or row.get("part_number")
        or (row.get("product_identity") or {}).get("part_number"),
        "nsn": row.get("exact_nsn") or row.get("nsn") or (row.get("dla_product_structure") or {}).get("nsn"),
        "quantity": row.get("quantity") or (row.get("dla_product_structure") or {}).get("quantity"),
        "government_unit": row.get("unit_of_issue") or "EA",
        "commercial_units_per_gov_unit": 1,
        "deadline": (row.get("deadline_evaluation") or {}).get("deadline")
        or row.get("deadline")
        or row.get("response_deadline"),
        "delivery_location": row.get("delivery_location") or row.get("place_of_performance"),
        "opportunity_url": row.get("detail_url") or row.get("source_url"),
        "estimated_opportunity_size": est,
        "identity_confidence": "STRONG"
        if (row.get("exact_nsn") or row.get("exact_part_number"))
        else "POSSIBLE",
        "historical_awards": _awards_from_row(row),
        "opportunity_status": row.get("status") or "OPEN",
        "notes": "Loaded from M3 opportunity " + canonical_id,
    }
    return create_manual_test(payload)


def build_test_queue(*, limit: int = 40) -> dict[str, Any]:
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    existing = {t.get("linked_opportunity_id") for t in get_store().all() if t.get("linked_opportunity_id")}
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in store.all():
        title = str(row.get("title") or "")
        if _SERVICE_HINT.search(title) and not _PRODUCT_HINT.search(title):
            continue
        if not _PRODUCT_HINT.search(title) and not row.get("exact_nsn") and not row.get("nsn"):
            # keep UNKNOWN small-dollar DLA-ish
            sid = str(row.get("source_id") or "")
            if "dla" not in sid and "dibbs" not in sid and not str(row.get("solicitation_number") or "").upper().startswith(("SPE", "SPR")):
                continue
        est = row.get("estimated_value") or row.get("government_revenue") or row.get("historical_award_amount")
        klass = classify_opportunity_size(est)
        if klass == CLASS_ABOVE:
            # secondary only — still show but low priority
            pass
        score = _queue_score(row, klass)
        cid = row.get("canonical_id")
        scored.append(
            (
                score,
                {
                    "canonical_id": cid,
                    "source": row.get("source_id"),
                    "solicitation": row.get("solicitation_number") or row.get("external_id"),
                    "agency": row.get("agency"),
                    "classification": klass,
                    "product_title": title,
                    "manufacturer": row.get("manufacturer"),
                    "part_number": row.get("exact_part_number") or row.get("part_number"),
                    "nsn": row.get("exact_nsn") or row.get("nsn"),
                    "quantity": row.get("quantity"),
                    "unit_of_issue": row.get("unit_of_issue") or "EA",
                    "estimated_opportunity_size": est if est is not None else "UNKNOWN",
                    "deadline": row.get("deadline") or row.get("response_deadline"),
                    "deadline_runway": (row.get("deadline_evaluation") or {}).get("calendar_days_remaining"),
                    "last_government_award_price": row.get("historical_award_amount") or "UNKNOWN",
                    "historical_award_count": len(row.get("government_price_history") or row.get("historical_awards") or [])
                    or (1 if row.get("historical_award_amount") else 0),
                    "current_public_price": "UNKNOWN",
                    "supplier_channel_identified": bool(row.get("supplier_candidates") or row.get("suppliers")),
                    "quote_status": "LOADED" if cid in existing else "NONE",
                    "validation_status": "IN_LAB" if cid in existing else "NOT_TESTED",
                    "next_action": "Open Economics Lab test" if cid in existing else "Load into Lab",
                    "detail_url": row.get("detail_url") or row.get("source_url"),
                    "is_dla": bool(row.get("is_dla"))
                    or "dla" in str(row.get("source_id") or "").lower()
                    or "dibbs" in str(row.get("source_id") or "").lower(),
                    "queue_score": score,
                },
            )
        )
    scored.sort(key=lambda x: (-x[0], str(x[1].get("deadline") or "9999")))
    items = [x[1] for x in scored[:limit]]
    return {
        "count": len(items),
        "items": items,
        "thresholds": threshold_config(),
        "note": "Priority favors micro/near-micro tangible products with identity + history — not max gross profit",
    }


def experiment_dashboard() -> dict[str, Any]:
    tests = [enrich_test(t) for t in get_store().all() if not t.get("archived")]
    micro = [t for t in tests if t.get("classification") in {CLASS_MICRO, CLASS_NEAR, CLASS_UNKNOWN}]
    larger = [t for t in tests if t.get("classification") in {CLASS_SMALL_SA, CLASS_ABOVE}]

    def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        quotes_req = sum(1 for t in rows if str(t.get("quote_status") or "").upper() in {"REQUESTED", "RECEIVED"} or (t.get("supplier_quotes")))
        quotes_recv = sum(1 for t in rows if (t.get("supplier_quotes") or []))
        econ_pass = sum(1 for t in rows if t.get("status") in {"ECONOMIC_PASS", "BID_CANDIDATE"})
        econ_fail = sum(1 for t in rows if t.get("status") == "ECONOMIC_FAIL")
        exec_fail = sum(1 for t in rows if t.get("status") == "EXECUTION_FAIL")
        bid_cand = sum(1 for t in rows if t.get("status") == "BID_CANDIDATE")
        hist_recon = sum(1 for t in rows if (t.get("historical_awards") and t.get("historical_market_costs")))
        profits = []
        margins = []
        for t in rows:
            snap = t.get("economics_snapshot") or {}
            try:
                if snap.get("estimated_gross_profit") is not None:
                    profits.append(float(snap["estimated_gross_profit"]))
                if snap.get("estimated_gross_margin_pct") is not None:
                    margins.append(float(snap["estimated_gross_margin_pct"]))
            except (TypeError, ValueError):
                pass
        profits_sorted = sorted(profits)
        margins_sorted = sorted(margins)
        med_p = profits_sorted[len(profits_sorted) // 2] if profits_sorted else None
        med_m = margins_sorted[len(margins_sorted) // 2] if margins_sorted else None
        return {
            "opportunities_tested": len(rows),
            "historical_economics_reconstructed": hist_recon,
            "supplier_quotes_requested": quotes_req,
            "supplier_quotes_received": quotes_recv,
            "economic_passes": econ_pass,
            "economic_fails": econ_fail,
            "execution_fails": exec_fail,
            "bid_candidates": bid_cand,
            "quote_pass_rate": round(econ_pass / quotes_recv, 4) if quotes_recv else None,
            "overall_bid_candidate_rate": round(bid_cand / len(rows), 4) if rows else None,
            "median_gross_profit": med_p,
            "median_gross_margin_pct": med_m,
            "total_potential_gross_profit": round(sum(profits), 2) if profits else None,
        }

    quotes_completed = sum(1 for t in tests if t.get("supplier_quotes"))
    return {
        "go_no_go": {
            "target_supplier_quotes": 10,
            "supplier_quotes_completed": quotes_completed,
            "progress_label": f"{quotes_completed} / 10 supplier quotes completed",
            "profitable_quotes": sum(1 for t in tests if t.get("status") in {"ECONOMIC_PASS", "BID_CANDIDATE"}),
            "unprofitable_quotes": sum(1 for t in tests if t.get("status") == "ECONOMIC_FAIL"),
            "execution_failures": sum(1 for t in tests if t.get("status") == "EXECUTION_FAIL"),
            "executable_bid_candidates": sum(1 for t in tests if t.get("status") == "BID_CANDIDATE"),
            "note": "Factual progress only — does not auto-pronounce business viability",
        },
        "micro_purchase_results": _stats(micro),
        "larger_comparison_results": _stats(larger),
        "all_results": _stats(tests),
        "thresholds": threshold_config(),
    }


def quote_request_for(test_id: str) -> dict[str, Any]:
    test = get_test(test_id)
    if not test:
        raise KeyError(test_id)
    return {"test_id": test_id, "text": build_quote_request_text(test)}


def search_opportunities(q: str, *, limit: int = 25) -> list[dict[str, Any]]:
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pipeline_store import M3PipelineStore

    needle = (q or "").strip().lower()
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    out = []
    for row in store.all():
        blob = " ".join(
            str(x or "")
            for x in (
                row.get("canonical_id"),
                row.get("title"),
                row.get("solicitation_number"),
                row.get("agency"),
                row.get("nsn"),
                row.get("part_number"),
            )
        ).lower()
        if needle and needle not in blob:
            continue
        out.append(
            {
                "canonical_id": row.get("canonical_id"),
                "title": row.get("title"),
                "solicitation": row.get("solicitation_number"),
                "agency": row.get("agency"),
                "source": row.get("source_id"),
                "nsn": row.get("exact_nsn") or row.get("nsn"),
            }
        )
        if len(out) >= limit:
            break
    return out


def _awards_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    hist = row.get("government_price_history") or {}
    awards = []
    if isinstance(hist, dict):
        for a in hist.get("awards") or hist.get("observations") or []:
            if isinstance(a, dict):
                awards.append(
                    {
                        "award_date": a.get("date") or a.get("award_date"),
                        "awarded_vendor": a.get("vendor") or a.get("awardee"),
                        "unit_price": a.get("unit_price") or a.get("price"),
                        "total_award": a.get("amount") or a.get("total"),
                        "quantity": a.get("quantity"),
                        "source_url": a.get("url"),
                        "source_type": a.get("source") or "M3",
                        "confidence": a.get("confidence") or "MODERATE",
                    }
                )
    if row.get("historical_award_amount") and not awards:
        awards.append(
            {
                "award_date": row.get("historical_award_date"),
                "unit_price": row.get("historical_award_amount"),
                "total_award": row.get("historical_award_amount"),
                "source_type": "M3_PIPELINE",
                "confidence": "MODERATE",
            }
        )
    return awards


def _queue_score(row: dict[str, Any], klass: str) -> int:
    score = 0
    status = str(row.get("status") or "OPEN").upper()
    if status in {"OPEN", "ACTIVE", "NEW"}:
        score += 40
    runway = (row.get("deadline_evaluation") or {}).get("calendar_days_remaining")
    try:
        if runway is not None and int(runway) >= 3:
            score += 20
        if runway is not None and int(runway) >= 7:
            score += 10
    except (TypeError, ValueError):
        pass
    if klass == CLASS_MICRO:
        score += 35
    elif klass == CLASS_NEAR:
        score += 28
    elif klass == CLASS_SMALL_SA:
        score += 12
    elif klass == CLASS_UNKNOWN:
        score += 15
    else:
        score += 2
    if row.get("exact_nsn") or row.get("nsn"):
        score += 20
    if row.get("exact_part_number") or row.get("part_number"):
        score += 15
    if row.get("historical_award_amount") or row.get("government_price_history"):
        score += 15
    sid = str(row.get("source_id") or "").lower()
    if "dla" in sid or "dibbs" in sid or str(row.get("solicitation_number") or "").upper().startswith(("SPE", "SPR")):
        score += 25
    if _PRODUCT_HINT.search(str(row.get("title") or "")):
        score += 10
    return score
