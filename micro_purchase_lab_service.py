"""Micro-Purchase Economics Lab — queue, CRUD orchestration, dashboard."""

from __future__ import annotations

from typing import Any

from micro_purchase_lab_config import (
    CLASS_ABOVE,
    CLASS_MICRO,
    CLASS_NEAR,
    CLASS_SMALL_SA,
    CLASS_UNKNOWN,
    classify_opportunity_size,
    threshold_config,
)
from micro_purchase_lab_economics import (
    build_quote_request_text,
    enrich_test,
)
from micro_purchase_lab_store import MicroPurchaseLabStore


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
    # Record real supplier quotes into internal intelligence (never estimated)
    try:
        from micro_purchase_lab_quote_intel import ingest_quotes_from_test

        ingest_quotes_from_test(row)
    except Exception:
        pass
    saved = enrich_test(store.upsert(row))
    try:
        store.merge_quote_queue_from_test(saved)
    except Exception:
        pass
    return saved


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


def build_test_queue(
    *,
    limit: int = 40,
    filter_state: str = "COMPLETE",
    raw_search_target: int | None = None,
    complete_target: int | None = None,
) -> dict[str, Any]:
    """Large-pool µLab funnel → operator-facing candidates.

    Default filter_state=COMPLETE. UNKNOWN never auto-qualifies.
    """
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pipeline_store import M3PipelineStore
    from micro_purchase_lab_pipeline import run_micro_lab_funnel

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    existing = {t.get("linked_opportunity_id") for t in get_store().all() if t.get("linked_opportunity_id")}
    rows = list(store.all())
    out = run_micro_lab_funnel(
        rows,
        raw_search_target=raw_search_target,
        complete_target=complete_target or max(limit, 15),
        filter_state=filter_state,
    )
    items = []
    for it in out.get("items") or []:
        cid = it.get("canonical_id")
        card = dict(it)
        card["quote_status"] = "LOADED" if cid in existing else it.get("quote_status") or "NONE"
        card["validation_status"] = "IN_LAB" if cid in existing else "NOT_TESTED"
        if cid in existing:
            card["next_action"] = "Open Economics Lab test"
        items.append(card)
    return {
        "count": len(items),
        "items": items[:limit],
        "funnel": out.get("funnel"),
        "thresholds": out.get("thresholds") or threshold_config(),
        "filter_state": out.get("filter_state"),
        "stopped_reason": out.get("stopped_reason"),
        "build": out.get("build"),
        "note": out.get("note"),
    }


def experiment_dashboard() -> dict[str, Any]:
    tests = [enrich_test(t) for t in get_store().all() if not t.get("archived")]
    micro = [t for t in tests if t.get("classification") in {CLASS_MICRO, CLASS_NEAR, CLASS_UNKNOWN}]
    larger = [t for t in tests if t.get("classification") in {CLASS_SMALL_SA, CLASS_ABOVE}]
    queue = get_store().all_quote_queue()

    def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        quotes_req = sum(
            1
            for t in rows
            if str(t.get("quote_status") or "").upper() in {"REQUESTED", "RECEIVED", "READY_TO_REQUEST"}
            or (t.get("supplier_quotes"))
            or (t.get("quote_queue_items"))
        )
        quotes_recv = sum(1 for t in rows if (t.get("supplier_quotes") or []))
        econ_pass = sum(1 for t in rows if t.get("status") in {"ECONOMIC_PASS", "BID_CANDIDATE"})
        econ_fail = sum(1 for t in rows if t.get("status") == "ECONOMIC_FAIL")
        exec_fail = sum(1 for t in rows if t.get("status") == "EXECUTION_FAIL")
        bid_cand = sum(1 for t in rows if t.get("status") == "BID_CANDIDATE")
        hist_recon = sum(1 for t in rows if (t.get("historical_awards") and t.get("historical_market_costs")))
        researched = sum(1 for t in rows if t.get("automated_research") or t.get("research_completed_at"))
        prepared = sum(1 for t in rows if t.get("quote_packets") or t.get("quote_queue_items"))
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
            "automated_research_completed": researched,
            "research_to_quote_conversion": round(prepared / researched, 4) if researched else None,
            "supplier_quotes_requested": quotes_req,
            "supplier_quotes_received": quotes_recv,
            "economic_passes": econ_pass,
            "economic_fails": econ_fail,
            "execution_fails": exec_fail,
            "bid_candidates": bid_cand,
            "economic_pass_rate": round(econ_pass / quotes_recv, 4) if quotes_recv else None,
            "execution_pass_rate": round((quotes_recv - exec_fail) / quotes_recv, 4) if quotes_recv else None,
            "quote_pass_rate": round(econ_pass / quotes_recv, 4) if quotes_recv else None,
            "overall_bid_candidate_rate": round(bid_cand / len(rows), 4) if rows else None,
            "median_gross_profit": med_p,
            "median_gross_margin_pct": med_m,
            "total_potential_gross_profit": round(sum(profits), 2) if profits else None,
        }

    quotes_completed = sum(1 for t in tests if t.get("supplier_quotes"))
    discount_stats = _supplier_discount_dashboard()
    try:
        from micro_purchase_lab_quote_intel import supplier_performance_stats

        supplier_perf = supplier_performance_stats(min_sample=1)
    except Exception:
        supplier_perf = []

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
        "automation": {
            "automated_research_completed": sum(1 for t in tests if t.get("research_completed_at")),
            "research_to_quote_conversion": _stats(tests).get("research_to_quote_conversion"),
            "quotes_requested": sum(
                1 for q in queue if str(q.get("quote_status") or "").upper() in {"REQUESTED", "RECEIVED", "READY_TO_REQUEST"}
            ),
            "quotes_received": sum(1 for q in queue if str(q.get("quote_status") or "").upper() == "RECEIVED")
            + sum(1 for t in tests if t.get("supplier_quotes")),
            "average_supplier_discount_vs_public": discount_stats.get("average"),
            "median_supplier_discount": discount_stats.get("median"),
            "economic_pass_rate": _stats(tests).get("economic_pass_rate"),
            "execution_pass_rate": _stats(tests).get("execution_pass_rate"),
            "bid_candidate_rate": _stats(tests).get("overall_bid_candidate_rate"),
            "median_gross_profit": _stats(tests).get("median_gross_profit"),
            "median_gross_margin": _stats(tests).get("median_gross_margin_pct"),
            "supplier_performance": supplier_perf,
        },
        "micro_purchase_results": _stats(micro),
        "larger_comparison_results": _stats(larger),
        "all_results": _stats(tests),
        "thresholds": threshold_config(),
    }


def _supplier_discount_dashboard() -> dict[str, Any]:
    try:
        from micro_purchase_lab_quote_intel import get_quote_intel_store
        from micro_purchase_lab_economics import D

        discounts = []
        for q in get_quote_intel_store().all():
            d = D(q.get("quote_discount_vs_public_pct"))
            if d is not None:
                discounts.append(float(d))
        if not discounts:
            return {"average": None, "median": None, "count": 0}
        discounts.sort()
        return {
            "average": round(sum(discounts) / len(discounts), 2),
            "median": discounts[len(discounts) // 2],
            "count": len(discounts),
        }
    except Exception:
        return {"average": None, "median": None, "count": 0}


def research_test(test_id: str, *, allow_paid_research: bool = False) -> dict[str, Any]:
    from micro_purchase_lab_research import research_opportunity

    row = get_test(test_id)
    if not row:
        raise KeyError(test_id)
    out = research_opportunity(row, allow_paid_research=allow_paid_research)
    return save_test(out)


def prepare_quotes(test_id: str, *, top_n: int = 4) -> dict[str, Any]:
    from micro_purchase_lab_research import prepare_quotes_for_test

    row = get_test(test_id)
    if not row:
        raise KeyError(test_id)
    out = prepare_quotes_for_test(row, top_n=top_n)
    saved = save_test(out)
    get_store().merge_quote_queue_from_test(saved)
    return saved


def build_quote_queue() -> dict[str, Any]:
    store = get_store()
    items = store.all_quote_queue()
    # Also surface in-test queue items not yet merged
    for t in store.all():
        if t.get("archived"):
            continue
        for qi in t.get("quote_queue_items") or []:
            if not isinstance(qi, dict):
                continue
            if not any(x.get("id") == qi.get("id") for x in items):
                items.append(qi)

    def _priority(it: dict[str, Any]) -> tuple:
        runway = None
        try:
            from micro_purchase_lab_research import _days_until

            runway = _days_until(it.get("deadline"))
        except Exception:
            pass
        # sooner deadline first (but viable)
        r = runway if runway is not None else 9999
        he = 0.0
        try:
            he = -float(it.get("historical_equivalent_price") or 0)
        except (TypeError, ValueError):
            pass
        return (r if r >= 0 else 9999, he, str(it.get("supplier") or ""))

    items = sorted(items, key=_priority)
    return {"count": len(items), "items": items}


def todays_quote_work() -> dict[str, Any]:
    q = build_quote_queue()
    today = []
    for it in q.get("items") or []:
        st = str(it.get("quote_status") or "").upper()
        if st in {"READY_TO_REQUEST", "REQUESTED", "NOT_PREPARED"}:
            today.append(
                {
                    "supplier": it.get("supplier"),
                    "part_number": it.get("part_number"),
                    "quantity": it.get("quantity"),
                    "opportunity": it.get("opportunity"),
                    "deadline": it.get("deadline"),
                    "next_action": it.get("next_action") or "REQUEST_SUPPLIER_QUOTE",
                    "quote_status": st,
                    "test_id": it.get("test_id"),
                    "id": it.get("id"),
                }
            )
    return {"count": len(today), "items": today[:40], "label": "Today's Quote Work"}


def update_queue_item_status(item_id: str, status: str) -> dict[str, Any]:
    row = get_store().update_quote_queue_status(item_id, status)
    if not row:
        raise KeyError(item_id)
    return row


def research_queue_batch(*, limit: int = 5, allow_paid_research: bool = False) -> dict[str, Any]:
    from micro_purchase_lab_research import research_batch

    tests = [t for t in get_store().all() if not t.get("archived") and not t.get("research_completed_at")]
    ids = [t["id"] for t in tests[: max(1, min(limit, 5))]]
    return research_batch(ids, limit=limit, allow_paid_research=allow_paid_research)


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
                qty = a.get("quantity")
                unit = a.get("unit_price") or a.get("price")
                total = a.get("amount") or a.get("total")
                # Never present total contract value as unit price without quantity
                if unit is None and total is not None and qty not in (None, "", 0, "0"):
                    try:
                        from decimal import Decimal

                        unit = Decimal(str(total)) / Decimal(str(qty))
                    except Exception:
                        unit = None
                awards.append(
                    {
                        "award_date": a.get("date") or a.get("award_date"),
                        "awarded_vendor": a.get("vendor") or a.get("awardee"),
                        "unit_price": unit,
                        "total_award": total,
                        "quantity": qty,
                        "source_url": a.get("url"),
                        "source_type": a.get("source") or "M3",
                        "confidence": a.get("confidence") or "MODERATE",
                        "unit_price_status": "EXPLICIT_UNIT"
                        if a.get("unit_price") is not None
                        else ("INFERRED_FROM_TOTAL_QTY" if unit is not None else "TOTAL_ONLY_NOT_UNIT"),
                    }
                )
    ham = row.get("historical_award_amount")
    if ham is not None and not awards:
        qty = row.get("quantity")
        unit = None
        if qty not in (None, "", 0, "0"):
            try:
                from decimal import Decimal

                unit = Decimal(str(ham)) / Decimal(str(qty))
            except Exception:
                unit = None
        awards.append(
            {
                "award_date": row.get("historical_award_date"),
                "unit_price": unit,
                "total_award": ham,
                "quantity": qty,
                "source_type": "M3_PIPELINE",
                "confidence": "WEAK" if unit is None else "MODERATE",
                "unit_price_status": "INFERRED_FROM_TOTAL_QTY" if unit is not None else "TOTAL_ONLY_NOT_UNIT",
                "notes": "historical_award_amount is total — not assumed unit price",
            }
        )
    return awards
