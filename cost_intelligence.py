"""On-demand cost intelligence — no catalog mirrors; compact observations with provenance."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from pursuit_qualification_constants import (
    COST_COMPARABLE_PUBLIC,
    COST_ESTIMATED_RANGE,
    COST_EXACT_PUBLIC,
    COST_HISTORICAL_COMPARABLE,
    COST_HISTORICAL_EXACT,
    COST_QUOTE_REQUIRED,
    COST_UNKNOWN,
    DEFAULT_FREIGHT_PCT_BASE,
    DEFAULT_FREIGHT_PCT_HIGH,
    DEFAULT_FREIGHT_PCT_LOW,
)
from reusable_knowledge import ReusableKnowledgeStore

# Category comparable unit ranges for preliminary screening only (not bid economics).
# Labeled ESTIMATED_RANGE / COMPARABLE — never CURRENT_EXACT_PUBLIC_PRICE.
_CATEGORY_UNIT_RANGES: dict[str, dict[str, Any]] = {
    "native_seed_lb": {
        "unit": "LB",
        "low": 12.0,
        "base": 35.0,
        "high": 85.0,
        "source": "specialty_native_seed_market_comparable_range",
        "notes": "Retail/wholesale native seed often $12–$85/lb depending on species rarity; not exact SKU quotes",
    },
    "industrial_equipment_ea": {
        "unit": "EA",
        "low": 500.0,
        "base": 5000.0,
        "high": 25000.0,
        "source": "industrial_equipment_comparable_order_of_magnitude",
        "notes": "Wide equipment band — weak without model match",
    },
    "hardware_parts_ea": {
        "unit": "EA",
        "low": 5.0,
        "base": 50.0,
        "high": 400.0,
        "source": "industrial_hardware_comparable_range",
        "notes": "Generic hardware/parts order-of-magnitude only",
    },
    "it_hardware_ea": {
        "unit": "EA",
        "low": 100.0,
        "base": 800.0,
        "high": 3500.0,
        "source": "it_hardware_comparable_range",
        "notes": "Monitors/PCs/network gear typical public catalog band",
    },
}


def _utc() -> str:
    return now_utc().isoformat()


def cost_observation(
    *,
    state: str,
    unit_price: float | None = None,
    unit_price_low: float | None = None,
    unit_price_high: float | None = None,
    source: str,
    product_identity: str,
    quantity_basis: float | None = None,
    unit_basis: str | None = None,
    shipping_included: bool | None = None,
    confidence: str = "LOW",
    exact: bool = False,
    suitable_preliminary: bool = False,
    suitable_bid: bool = False,
) -> dict[str, Any]:
    return {
        "state": state,
        "unit_price": unit_price,
        "unit_price_low": unit_price_low,
        "unit_price_high": unit_price_high,
        "source": source,
        "observed_at": _utc(),
        "product_identity": product_identity,
        "quantity_basis": quantity_basis,
        "unit_basis": unit_basis,
        "shipping_included": shipping_included,
        "confidence": confidence,
        "staleness": "CURRENT_ASSESSMENT",
        "exact": exact,
        "comparable": not exact,
        "suitable_for_preliminary_economics": suitable_preliminary,
        "suitable_for_bid_economics": suitable_bid and exact,
    }


def infer_category(title: str | None, line: dict[str, Any]) -> str | None:
    blob = f"{title or ''} {line.get('description') or ''}".lower()
    if any(x in blob for x in ("seed", "bluestem", "grama", "wildflower", "prairie", "andropogon")):
        return "native_seed_lb"
    if any(x in blob for x in ("monitor", "laptop", "computer", "server", "switch", "router")):
        return "it_hardware_ea"
    if any(x in blob for x in ("bolt", "washer", "filter", "bearing", "hose", "valve", "part")):
        return "hardware_parts_ea"
    if any(x in blob for x in ("equipment", "planer", "lift", "pump", "motor", "hvac", "tank")):
        return "industrial_equipment_ea"
    return None


def prioritize_lines(lines: list[dict[str, Any]], *, max_sample: int = 25) -> list[dict[str, Any]]:
    """Pareto-ish: prefer high quantity / descriptive lines for sampling."""
    scored = []
    for li in lines:
        if li.get("is_service_line"):
            continue
        qty = float(li.get("quantity") or 0)
        desc_len = len(str(li.get("description") or ""))
        scored.append((qty * max(desc_len, 1), li))
    scored.sort(key=lambda x: -x[0])
    return [li for _, li in scored[:max_sample]]


def research_bom_cost_intelligence(
    *,
    line_items: list[dict[str, Any]],
    title: str | None = None,
    reusable: ReusableKnowledgeStore | None = None,
    max_sample: int = 25,
    max_external: int = 0,
) -> dict[str, Any]:
    """
    Bounded multi-line cost research.
    Default max_external=0 in development screening uses category ranges + reusable knowledge only.
    """
    reusable = reusable or ReusableKnowledgeStore()
    lines = [li for li in line_items if isinstance(li, dict)]
    sample = prioritize_lines(lines, max_sample=max_sample)
    observations: list[dict[str, Any]] = []
    line_estimates: list[dict[str, Any]] = []
    external_used = 0
    reused = 0

    # Reuse prior supplier price facts if present
    for fact in reusable.suppliers:
        if fact.get("last_unit_price") is not None:
            reused += 1
            observations.append(
                cost_observation(
                    state=COST_HISTORICAL_COMPARABLE
                    if not fact.get("exact")
                    else COST_HISTORICAL_EXACT,
                    unit_price=float(fact["last_unit_price"]),
                    source=str(fact.get("source") or "reusable_supplier"),
                    product_identity=str(fact.get("category") or fact.get("supplier")),
                    confidence="MEDIUM",
                    exact=bool(fact.get("exact")),
                    suitable_preliminary=True,
                    suitable_bid=False,
                )
            )

    covered_qty_proxy = 0.0
    total_qty_proxy = 0.0
    low_total = 0.0
    base_total = 0.0
    high_total = 0.0
    priced_lines = 0

    for li in lines:
        qty = float(li.get("quantity") or 0)
        total_qty_proxy += max(qty, 0)

    for li in sample:
        qty = float(li.get("quantity") or 0)
        desc = str(li.get("description") or "line")
        cat = infer_category(title, li)
        # Prefer exact public if line carries unit_cost with public maturity (rare)
        if li.get("unit_cost") is not None and li.get("cost_evidence_state") == COST_EXACT_PUBLIC:
            up = float(li["unit_cost"])
            obs = cost_observation(
                state=COST_EXACT_PUBLIC,
                unit_price=up,
                source="line_public_price",
                product_identity=desc[:120],
                quantity_basis=qty,
                unit_basis=str(li.get("unit") or li.get("unit_of_measure") or ""),
                confidence="HIGH",
                exact=True,
                suitable_preliminary=True,
                suitable_bid=True,
            )
            low = base = high = up * qty
        elif cat and cat in _CATEGORY_UNIT_RANGES:
            band = _CATEGORY_UNIT_RANGES[cat]
            obs = cost_observation(
                state=COST_ESTIMATED_RANGE,
                unit_price=band["base"],
                unit_price_low=band["low"],
                unit_price_high=band["high"],
                source=band["source"],
                product_identity=desc[:120],
                quantity_basis=qty,
                unit_basis=band["unit"],
                confidence="LOW",
                exact=False,
                suitable_preliminary=True,
                suitable_bid=False,
            )
            obs["notes"] = band["notes"]
            low = band["low"] * qty
            base = band["base"] * qty
            high = band["high"] * qty
        else:
            obs = cost_observation(
                state=COST_QUOTE_REQUIRED,
                source="no_defensible_public_band",
                product_identity=desc[:120],
                quantity_basis=qty,
                confidence="NONE",
                exact=False,
                suitable_preliminary=False,
                suitable_bid=False,
            )
            low = base = high = 0.0

        observations.append(obs)
        if obs["state"] != COST_QUOTE_REQUIRED and obs["state"] != COST_UNKNOWN:
            priced_lines += 1
            covered_qty_proxy += qty
            low_total += low
            base_total += base
            high_total += high
            line_estimates.append(
                {
                    "description": desc[:120],
                    "quantity": qty,
                    "low": low,
                    "base": base,
                    "high": high,
                    "state": obs["state"],
                    "exact": obs["exact"],
                }
            )

    # Extrapolate sample to full BOM only when category-homogeneous and sample large enough
    coverage_qty = (covered_qty_proxy / total_qty_proxy) if total_qty_proxy else 0.0
    extrapolated = False
    if sample and priced_lines >= max(3, int(0.15 * len(sample))) and coverage_qty > 0:
        # Scale by qty coverage of sampled priced lines vs full BOM
        scale = 1.0 / coverage_qty if 0 < coverage_qty < 1 else 1.0
        if scale > 1 and scale < 8:
            low_total *= scale
            base_total *= scale
            high_total *= scale
            extrapolated = True
            coverage_qty = min(1.0, coverage_qty * scale) if False else coverage_qty

    coverage_lines = priced_lines / len(sample) if sample else 0.0
    # Report sample coverage honestly
    sample_value_share = coverage_qty

    if priced_lines == 0:
        cost_status = COST_QUOTE_REQUIRED
        ranges = None
    else:
        cost_status = COST_ESTIMATED_RANGE
        ranges = {
            "low_cost_estimate": round(low_total, 2),
            "base_cost_estimate": round(base_total, 2),
            "high_cost_estimate": round(high_total, 2),
            "extrapolated_from_sample": extrapolated,
        }

    return {
        "kind": "CostIntelligence",
        "status": cost_status,
        "line_count": len(lines),
        "sample_size": len(sample),
        "priced_sample_lines": priced_lines,
        "coverage_of_sampled_lines": round(coverage_lines, 3),
        "coverage_of_bom_qty_proxy": round(sample_value_share, 3),
        "coverage_note": "Coverage is quantity-proxy of sampled priced lines — not guaranteed value share",
        "ranges": ranges,
        "observations": observations[:80],
        "line_estimates": line_estimates[:40],
        "external_requests": external_used,
        "supplier_facts_reused": reused,
        "exact_vs_comparable": {
            "exact_count": sum(1 for o in observations if o.get("exact")),
            "comparable_or_estimate_count": sum(1 for o in observations if not o.get("exact")),
        },
        "suitable_for_bid_economics": False,
        "suitable_for_preliminary_economics": priced_lines > 0,
    }


def freight_range_from_cost(
    *,
    cost_base: float | None,
    fob_terms: str | None = None,
    freight_included: bool | None = None,
) -> dict[str, Any]:
    if freight_included is True:
        return {
            "low": 0.0,
            "base": 0.0,
            "high": 0.0,
            "status": "VERIFIED_INCLUDED",
            "note": "freight included in supplier delivered pricing",
        }
    if cost_base is None:
        return {
            "low": None,
            "base": None,
            "high": None,
            "status": "UNKNOWN",
            "note": "freight not assumed zero",
        }
    # FOB destination often still has cost embedded or separate — keep uncertainty
    dest = bool(fob_terms and "destination" in str(fob_terms).lower())
    low = cost_base * (DEFAULT_FREIGHT_PCT_LOW * (0.5 if dest else 1.0))
    base = cost_base * DEFAULT_FREIGHT_PCT_BASE
    high = cost_base * DEFAULT_FREIGHT_PCT_HIGH
    return {
        "low": round(low, 2),
        "base": round(base, 2),
        "high": round(high, 2),
        "status": "ESTIMATED_RANGE",
        "note": "screening freight allowance — not a carrier quote; not zeroed",
        "fob_terms": fob_terms,
    }
