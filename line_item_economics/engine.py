"""Orchestrate line-item extraction → identity → normalize → price → rollup."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from line_item_economics.extract import extract_line_items
from line_item_economics.freight import build_freight_block, classify_lead_time
from line_item_economics.identity import classify_all
from line_item_economics.models import BUILD
from line_item_economics.normalize import normalize_all
from line_item_economics.profit import (
    attach_historical_evidence,
    attach_retail_evidence,
    compute_line_economics,
    rollup_contract,
)
from line_item_economics.recurring import categorize_text, recurring_buyer_intelligence
from line_item_economics.simple_score import simple_resale_score
from line_item_economics.store import load_analysis as _load
from line_item_economics.store import save_analysis
from line_item_economics.supplier_coverage import compute_supplier_coverage


def owner_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    roll = analysis.get("rollup") or {}
    return {
        "lines": roll.get("total_line_count"),
        "priced": roll.get("lines_priced"),
        "historical_matched": roll.get("lines_historical_matched"),
        "retail_cost": roll.get("TOTAL_KNOWN_RETAIL_COST"),
        "historical_gov_value": roll.get("TOTAL_KNOWN_HISTORICAL_VALUE"),
        "retail_spread": roll.get("TOTAL_KNOWN_RETAIL_SPREAD"),
        "estimated_freight": roll.get("estimated_freight"),
        "post_financing_profit": roll.get("post_financing_profit"),
        "status": roll.get("profit_bucket"),
        "confidence": roll.get("completeness_grade"),
        "proof_label": roll.get("proof_label"),
        "next_action": analysis.get("next_action"),
        "simple_resale_score": (analysis.get("simple_resale") or {}).get("simple_resale_score"),
    }


def analyze_line_item_economics(
    *,
    opportunity_id: str,
    title: str | None = None,
    buyer: str | None = None,
    schedule_rows: list[dict[str, Any]] | None = None,
    csv_text: str | None = None,
    body_text: str | None = None,
    existing_lines: list[dict[str, Any]] | None = None,
    retail_by_line: dict[str, dict[str, Any]] | None = None,
    retail_equal_by_line: dict[str, dict[str, Any]] | None = None,
    historical_by_line: dict[str, dict[str, Any]] | None = None,
    suppliers_by_line: dict[str, list[Any]] | None = None,
    freight: dict[str, Any] | None = None,
    financing_cost: float | None = None,
    buyer_history: list[dict[str, Any]] | None = None,
    flags: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Full line-item economics analysis. Evidence must be supplied — never invented."""
    extracted = extract_line_items(
        schedule_rows=schedule_rows,
        csv_text=csv_text,
        body_text=body_text,
        existing_lines=existing_lines,
    )
    lines = extracted["lines"]
    classify_all(lines)
    normalize_all(lines)

    retail_by_line = retail_by_line or {}
    retail_equal_by_line = retail_equal_by_line or {}
    historical_by_line = historical_by_line or {}
    suppliers_by_line = suppliers_by_line or {}

    for line in lines:
        lid = str(line.get("line_id") or "")
        clin = str(line.get("clin") or "")
        keys = [lid, clin, str(line.get("line_number") or "")]
        retail = None
        retail_eq = None
        hist = None
        suppliers = None
        for k in keys:
            if k and k in retail_by_line and retail is None:
                retail = retail_by_line[k]
            if k and k in retail_equal_by_line and retail_eq is None:
                retail_eq = retail_equal_by_line[k]
            if k and k in historical_by_line and hist is None:
                hist = historical_by_line[k]
            if k and k in suppliers_by_line and suppliers is None:
                suppliers = suppliers_by_line[k]
        if retail:
            attach_retail_evidence(line, retail)
        if retail_eq:
            attach_retail_evidence(line, {**retail_eq, "kind": "permitted_equal"})
        if hist:
            attach_historical_evidence(line, hist)
        if suppliers:
            line["suppliers_covering"] = suppliers
        # Lead time from retail stock if present
        r = line.get("retail") or {}
        line["lead_time"] = classify_lead_time(
            stock_status=r.get("stock_status"),
            lead_time_days=r.get("lead_time_days") if isinstance(r, dict) else None,
            required_delivery_date=line.get("delivery_date"),
            days_until_required=r.get("days_until_required") if isinstance(r, dict) else None,
        )
        compute_line_economics(line)

    freight_in = freight or {}
    freight_block = build_freight_block(
        estimated_weight_lb=freight_in.get("estimated_weight_lb"),
        dimensions=freight_in.get("dimensions"),
        delivery_zip=freight_in.get("delivery_zip"),
        delivery_location=freight_in.get("delivery_location"),
        public_shipping_quote=freight_in.get("public_shipping_quote") or freight_in.get("estimated_freight"),
        supplier_included_freight=freight_in.get("supplier_included_freight"),
        fob_terms=freight_in.get("fob_terms"),
        line_count=len(lines),
        oversized=bool(freight_in.get("oversized")),
        explicit_mode=freight_in.get("mode"),
    )

    rollup = rollup_contract(
        lines,
        freight_estimate=freight_block.get("estimated_freight"),
        financing_cost=financing_cost,
    )

    coverage = compute_supplier_coverage(lines)
    flags = flags or {}
    priced_ratio = (rollup["lines_priced"] / rollup["total_line_count"]) if rollup["total_line_count"] else 0
    simple = simple_resale_score(
        lines=lines,
        title=title,
        delivery_locations=flags.get("delivery_locations") or freight_in.get("delivery_locations"),
        public_pricing_available=priced_ratio >= 0.5,
        multiple_public_suppliers=len(coverage.get("suppliers_ranked") or []) >= 2,
        install_required=flags.get("install_required"),
        bonding_required=flags.get("bonding_required"),
        special_certification=flags.get("special_certification"),
        manufacturer_authorization=flags.get("manufacturer_authorization"),
        source_approval=flags.get("source_approval"),
        exact_source_restriction=flags.get("exact_source_restriction"),
    )

    cats = categorize_text(title or "")
    for line in lines[:20]:
        cats.extend(categorize_text(str(line.get("product_description") or "")))
    cats = sorted(set(cats))
    recurring = recurring_buyer_intelligence(
        buyer=buyer,
        current_categories=cats,
        history_rows=buyer_history,
    )

    # Next action
    bucket = rollup.get("profit_bucket")
    discounts = rollup.get("required_discounts") or {}
    d5 = discounts.get("profit_5000_post_freight")
    if d5 is None:
        d5 = discounts.get("profit_5000")
    if rollup.get("lines_unresolved") and rollup.get("coverage_pct", 0) < 80:
        next_action = "Complete retail/historical pricing on unresolved lines"
    elif bucket in {"YELLOW", "YELLOW_PLUS", "RED"}:
        next_action = "Request supplier quote"
        if d5 is not None:
            next_action = f"Request supplier quote (need ~{d5}% discount for $5K post-freight profit)"
    elif bucket in {"GREEN", "GREEN_PLUS"}:
        next_action = "Verify freight + request supplier confirmation"
    else:
        next_action = "Gather retail and historical price evidence"

    table = []
    for line in lines:
        econ = line.get("economics") or {}
        table.append(
            {
                "line": line.get("clin") or line.get("line_number") or line.get("line_id"),
                "item": (line.get("product_description") or "")[:120],
                "qty": econ.get("quantity") or line.get("quantity"),
                "uom": line.get("unit_of_measure"),
                "retail_unit": econ.get("retail_unit"),
                "retail_total": econ.get("retail_extended_cost"),
                "gov_hist_unit": econ.get("historical_gov_unit"),
                "gov_hist_total": econ.get("historical_gov_extended"),
                "spread": econ.get("line_retail_spread"),
                "match": (line.get("historical") or {}).get("match_quality") or line.get("identity_class"),
                "identity": line.get("identity_class"),
                "unresolved": econ.get("unresolved"),
                "requested_brand_retail_extended": econ.get("requested_brand_retail_extended"),
                "permitted_equal_retail_extended": econ.get("permitted_equal_retail_extended"),
            }
        )

    analysis = {
        "kind": "LineItemEconomicsAnalysis",
        "build": BUILD,
        "opportunity_id": opportunity_id,
        "title": title,
        "buyer": buyer,
        "analyzed_at": now_utc().isoformat(),
        "extraction": {
            "source_used": extracted.get("source_used"),
            "line_count": extracted.get("line_count"),
        },
        "lines": lines,
        "line_table": table,
        "rollup": rollup,
        "freight": freight_block,
        "supplier_coverage": coverage,
        "simple_resale": simple,
        "recurring_buyer": recurring,
        "next_action": next_action,
        "owner_summary": None,
    }
    analysis["owner_summary"] = owner_summary(analysis)

    if persist:
        save_analysis(opportunity_id, analysis)
    return analysis


def load_analysis(opportunity_id: str) -> dict[str, Any] | None:
    return _load(opportunity_id)
