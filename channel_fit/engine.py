"""Orchestrate channel + MSRP screen into PRE_QUOTE_DECISION. Score-only; no new pricing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from channel_fit.channel import score_channel
from channel_fit.coverage import compute_public_basket
from channel_fit.pre_quote import pre_quote_decision, quote_priority_score

BUILD = "20261007-m3-channel-fit-ranking-v1"


def _gov_value(opportunity: dict[str, Any]) -> float | None:
    for key in (
        "government_value",
        "estimated_value",
        "contract_value",
        "award_amount",
        "total_value",
        "gov_value",
    ):
        v = opportunity.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    econ = opportunity.get("economics") if isinstance(opportunity.get("economics"), dict) else {}
    for key in ("government_value", "estimated_value", "contract_value"):
        v = econ.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _lines(opportunity: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("line_items", "lines", "priced_lines"):
        raw = opportunity.get(key)
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return [x for x in raw if isinstance(x, dict)]
    # money_path stores material_lines as a count — skip non-list
    econ = opportunity.get("economics") if isinstance(opportunity.get("economics"), dict) else {}
    for key in ("line_items", "lines"):
        raw = econ.get(key)
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return [x for x in raw if isinstance(x, dict)]
    # Load durable line-item economics if present (no re-pricing)
    oid = str(
        opportunity.get("stable_key")
        or opportunity.get("opportunity_id")
        or opportunity.get("canonical_opportunity_id")
        or ""
    )
    if oid:
        try:
            from line_item_economics.engine import load_analysis

            analysis = load_analysis(oid) or {}
            extraction = analysis.get("extraction") if isinstance(analysis.get("extraction"), dict) else {}
            loaded = extraction.get("lines") or analysis.get("lines") or []
            if isinstance(loaded, list) and loaded:
                return [x for x in loaded if isinstance(x, dict)]
        except Exception:
            pass
    return []


def _days_remaining(opportunity: dict[str, Any]) -> float | None:
    for key in ("days_remaining", "runway_days"):
        v = opportunity.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    deadline = opportunity.get("deadline") or opportunity.get("due_date") or opportunity.get("bid_due")
    if not deadline:
        return None
    try:
        if isinstance(deadline, (int, float)):
            dt = datetime.fromtimestamp(float(deadline), tz=timezone.utc)
        else:
            text = str(deadline).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return round((dt - now).total_seconds() / 86400.0, 1)
    except Exception:
        return None


def score_opportunity_channel_fit(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Full channel-fit + MSRP pre-quote score for one opportunity dict."""
    gov = _gov_value(opportunity)
    lines = _lines(opportunity)
    public_basket = compute_public_basket(lines, government_value=gov)
    channel = score_channel(opportunity, public_basket=public_basket)

    days = _days_remaining(opportunity)
    supplier_breadth = int(
        opportunity.get("supplier_breadth")
        or opportunity.get("supplier_count")
        or len(opportunity.get("suppliers") or [])
        or 0
    )
    financing = opportunity.get("financing_plausible")
    if financing is None and isinstance(opportunity.get("financing"), dict):
        fin = opportunity["financing"]
        financing = fin.get("plausible") if "plausible" in fin else fin.get("financing_plausible")

    priority, priority_parts = quote_priority_score(
        headroom_pct=public_basket.get("VISIBLE_HEADROOM_PERCENT"),
        coverage_class=str(public_basket.get("PUBLIC_PRICE_COVERAGE_CLASS") or "INSUFFICIENT"),
        channel_score=int(channel.get("CHANNEL_DOMINANCE_SCORE") or 0),
        days_remaining=days,
        supplier_breadth=supplier_breadth,
        competition_dist_pct=float(
            (channel.get("bidder_structure") or {}).get("DISTRIBUTOR_BIDDER_PERCENT") or 0
        ),
        financing_plausible=financing if isinstance(financing, bool) else None,
    )

    exception_reason = opportunity.get("channel_exception_reason") or (
        (channel.get("owner_override_applied") and "owner_override") or None
    )

    decision, decision_reasons = pre_quote_decision(
        channel_class=str(channel.get("CHANNEL_COMPETITION_CLASS") or "UNKNOWN"),
        channel_score=int(channel.get("CHANNEL_DOMINANCE_SCORE") or 0),
        coverage_class=str(public_basket.get("PUBLIC_PRICE_COVERAGE_CLASS") or "INSUFFICIENT"),
        headroom=public_basket.get("VISIBLE_HEADROOM"),
        headroom_pct=public_basket.get("VISIBLE_HEADROOM_PERCENT"),
        priority=priority,
        days_remaining=days,
        eligibility=opportunity.get("eligibility") or opportunity.get("eligibility_state"),
        package_state=opportunity.get("package_state") or opportunity.get("package_status"),
        exception_reason=exception_reason if isinstance(exception_reason, str) else None,
    )

    # Likely supplier strategy (context-aware)
    klass = channel.get("CHANNEL_COMPETITION_CLASS")
    if klass == "D_CHANNEL_DOMINATED":
        strategy = "avoid_competing_distribution_channel_unless_verified_terms"
    elif klass == "C_DISTRIBUTOR_ADVANTAGED":
        strategy = "selective_outreach_only_if_msrp_headroom_strong"
    elif klass in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL"}:
        strategy = "aggregate_multi_supplier_quotes_on_fragmented_basket"
    else:
        strategy = "gather_bidder_history_and_public_prices"

    next_action = {
        "CALL_TODAY": "call_suppliers_today_confirm_landed_cost",
        "QUOTE_IF_CAPACITY": "queue_for_quote_desk_when_capacity",
        "WATCH": "monitor_coverage_or_headroom_improve",
        "PASS": "do_not_deep_outreach",
        "INSUFFICIENT_EVIDENCE": "price_more_of_basket_before_decision",
    }.get(decision, "review")

    return {
        "build": BUILD,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "title": opportunity.get("title"),
        "buyer": opportunity.get("buyer") or opportunity.get("agency") or opportunity.get("buyer_name"),
        "deadline": opportunity.get("deadline") or opportunity.get("due_date"),
        "days_remaining": days,
        "CHANNEL_COMPETITION_CLASS": channel.get("CHANNEL_COMPETITION_CLASS"),
        "CHANNEL_DOMINANCE_SCORE": channel.get("CHANNEL_DOMINANCE_SCORE"),
        "channel_reasons": channel.get("channel_reasons"),
        "vertical": channel.get("vertical"),
        "bidder_structure": channel.get("bidder_structure"),
        "PUBLIC_BASKET_VALUE": public_basket.get("PUBLIC_BASKET_VALUE"),
        "PUBLIC_BASKET_LINE_COVERAGE": public_basket.get("PUBLIC_BASKET_LINE_COVERAGE"),
        "PUBLIC_BASKET_VALUE_COVERAGE": public_basket.get("PUBLIC_BASKET_VALUE_COVERAGE"),
        "PUBLIC_PRICE_COVERAGE_CLASS": public_basket.get("PUBLIC_PRICE_COVERAGE_CLASS"),
        "VISIBLE_HEADROOM": public_basket.get("VISIBLE_HEADROOM"),
        "VISIBLE_HEADROOM_PERCENT": public_basket.get("VISIBLE_HEADROOM_PERCENT"),
        "government_value": public_basket.get("government_value"),
        "price_match_grades": public_basket.get("price_match_grades"),
        "QUOTE_PRIORITY_SCORE": priority,
        "quote_priority_parts": priority_parts,
        "PRE_QUOTE_DECISION": decision,
        "decision_reasons": decision_reasons,
        "likely_supplier_strategy": strategy,
        "next_action": next_action,
        "owner_override_applied": channel.get("owner_override_applied"),
        "provenance": {
            "channel": channel.get("provenance"),
            "coverage": "channel_fit.coverage.compute_public_basket",
            "pre_quote": "channel_fit.pre_quote",
        },
    }


def score_money_sprint_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-score existing money-sprint results without discarding prior research."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        scored = score_opportunity_channel_fit(row)
        merged = dict(row)
        merged["channel_fit"] = scored
        merged["CHANNEL_COMPETITION_CLASS"] = scored["CHANNEL_COMPETITION_CLASS"]
        merged["CHANNEL_DOMINANCE_SCORE"] = scored["CHANNEL_DOMINANCE_SCORE"]
        merged["PUBLIC_PRICE_COVERAGE_CLASS"] = scored["PUBLIC_PRICE_COVERAGE_CLASS"]
        merged["VISIBLE_HEADROOM"] = scored["VISIBLE_HEADROOM"]
        merged["QUOTE_PRIORITY_SCORE"] = scored["QUOTE_PRIORITY_SCORE"]
        merged["PRE_QUOTE_DECISION"] = scored["PRE_QUOTE_DECISION"]
        out.append(merged)
    return out


def queue_buckets(scored_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "CALL_TODAY": [],
        "QUOTE_IF_CAPACITY": [],
        "WATCH": [],
        "PASS": [],
        "INSUFFICIENT_EVIDENCE": [],
    }
    for row in scored_rows:
        cf = row.get("channel_fit") if isinstance(row.get("channel_fit"), dict) else row
        decision = str(cf.get("PRE_QUOTE_DECISION") or "INSUFFICIENT_EVIDENCE")
        buckets.setdefault(decision, []).append(row)
    for key in buckets:
        buckets[key].sort(
            key=lambda r: int(
                (r.get("channel_fit") or r).get("QUOTE_PRIORITY_SCORE") or 0
            ),
            reverse=True,
        )
    return buckets


def persist_scores(scored_rows: list[dict[str, Any]], path: Path | None = None) -> Path:
    try:
        from m3_data_root import data_path

        dest = path or data_path("m3_channel_fit_scores_v1.json")
    except Exception:
        dest = path or Path("m3_channel_fit_scores_v1.json")
    doc = {
        "build": BUILD,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(scored_rows),
        "rows": scored_rows,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return dest
