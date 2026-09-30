"""Phase L.9 — positive-economics quality audit + defensible quote gate."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_SPECIALTY,
    STAGE3_NO_ROW_CAP,
)
from phase_l.evidence_recovery import (
    expanded_category_benchmark,
    lookup_enrichment_cache_history,
    recover_government_value,
    recover_quantity,
    recover_suppliers,
)
from phase_l.quote_economics import (
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    _f,
)
from phase_l.quote_readiness import (
    AUTHORIZATION_NOT_REQUIRED,
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    AUTHORIZATION_UNKNOWN,
    NOT_AUTHORIZED,
    hard_eligibility_blockers,
    quote_runway,
)

BUILD = "20260928-m3-phase-l10-exact-evidence-workflow"

assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

# Quality outcome states
VALIDATED_QUOTE_TARGET = "VALIDATED_QUOTE_TARGET"
SECONDARY_QUOTE_TARGET = "SECONDARY_QUOTE_TARGET"
PROMISING_NEEDS_BETTER_GOV_VALUE = "PROMISING_NEEDS_BETTER_GOV_VALUE"
PROMISING_NEEDS_SUPPLIER_CONFIRMATION = "PROMISING_NEEDS_SUPPLIER_CONFIRMATION"
PROMISING_NEEDS_QUANTITY = "PROMISING_NEEDS_QUANTITY"
PROMISING_NEEDS_CONFIGURATION = "PROMISING_NEEDS_CONFIGURATION"
PROMISING_NEEDS_FREIGHT = "PROMISING_NEEDS_FREIGHT"
RECON_ONLY_CATEGORY_BENCHMARK = "RECON_ONLY_CATEGORY_BENCHMARK"
RECON_ONLY_SUPPLIER_SEED = "RECON_ONLY_SUPPLIER_SEED"
ECONOMIC_CASE_TOO_WEAK = "ECONOMIC_CASE_TOO_WEAK"
HARD_BLOCKED = "HARD_BLOCKED"

VALIDATED_QUOTE_DEPENDENT = "VALIDATED_QUOTE_DEPENDENT"
UNIT_MARGIN_POSITIVE = "UNIT_MARGIN_POSITIVE"
QUOTE_TARGET_ONLY = "QUOTE_TARGET_ONLY"
MANUAL_EVIDENCE_REVIEW = "MANUAL_EVIDENCE_REVIEW"

# Gov grades
GOV_VALUE_A = "GOV_VALUE_A"
GOV_VALUE_B = "GOV_VALUE_B"
GOV_VALUE_C = "GOV_VALUE_C"
GOV_VALUE_D = "GOV_VALUE_D"
GOV_VALUE_U = "GOV_VALUE_UNKNOWN"

# Supplier grades
SUPPLIER_A = "SUPPLIER_A"
SUPPLIER_B = "SUPPLIER_B"
SUPPLIER_C = "SUPPLIER_C"
SUPPLIER_D = "SUPPLIER_D"
SUPPLIER_UNKNOWN = "SUPPLIER_UNKNOWN"

# Quantity grades
QUANTITY_A_EXACT = "QUANTITY_A_EXACT"
QUANTITY_B_RANGE = "QUANTITY_B_RANGE"
QUANTITY_C_UNIT_ONLY = "QUANTITY_C_UNIT_ONLY"
QUANTITY_UNKNOWN = "QUANTITY_UNKNOWN"

# Config grades
CONFIG_A_EXACT = "CONFIG_A_EXACT"
CONFIG_B_STRONG = "CONFIG_B_STRONG"
CONFIG_C_PARTIAL = "CONFIG_C_PARTIAL"
CONFIG_UNKNOWN = "CONFIG_UNKNOWN"

# Freight
FREIGHT_LOW_IMPACT = "FREIGHT_LOW_IMPACT"
FREIGHT_RESERVE_SUFFICIENT = "FREIGHT_RESERVE_SUFFICIENT"
FREIGHT_MATERIAL_UNRESOLVED = "FREIGHT_MATERIAL_UNRESOLVED"
FREIGHT_QUOTE_REQUIRED = "FREIGHT_QUOTE_REQUIRED"

_SEED_DOMAINS = {
    "grainger.com",
    "zoro.com",
    "mscdirect.com",
    "fastenal.com",
    "mcmaster.com",
    "sourcewell-mn.gov",
    "naspovaluepoint.org",
    "staples.com",
    "officedepot.com",
    "machinerytrader.com",
    "newegg.com",
    "bhphotovideo.com",
    "chevy.com",
}


def _utc() -> str:
    return now_utc().isoformat()


def _now() -> datetime:
    return now_utc() if hasattr(now_utc(), "tzinfo") else datetime.now(timezone.utc)


def grade_government_value(
    gov: dict[str, Any] | None,
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit A–D grades. Category benchmarks are always D."""
    gov = gov or {}
    commercial = commercial or {}
    history = history or {}
    source = str(gov.get("source") or "")
    state = gov.get("state")
    tier = gov.get("tier")
    match = str(gov.get("match_rationale") or history.get("identity_match_type") or "").upper()

    if state in {None, GOV_VALUE_UNKNOWN} or not (_f(gov.get("unit_value")) or _f(gov.get("total_value"))):
        return {
            "grade": GOV_VALUE_U,
            "freshness": "unknown",
            "rationale": "no_usable_gov_value",
            "is_category_benchmark": False,
        }

    is_benchmark = "BENCHMARK" in source.upper() or source.startswith("GOVERNMENT_CATEGORY")
    if is_benchmark:
        label = source.split(":")[-1].strip().lower() if ":" in source else ""
        generic_labels = {
            "generic_vehicle",
            "fleet",
            "generic",
            "category",
            "generic_equipment",
            "",
        }
        has_model = bool(commercial.get("model") or commercial.get("mpn"))
        # L.10: model-specific award bands are Tier-3 comparables (Gov C), not Tier-4 D.
        # Generic category bands without exact model remain Gov D / recon-only.
        if (
            label
            and label not in generic_labels
            and "generic" not in label
            and has_model
        ):
            return {
                "grade": GOV_VALUE_C,
                "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
                "rationale": "model_specific_award_band_tier3",
                "is_category_benchmark": False,
                "upgraded_from_benchmark": True,
                "benchmark_label": label,
                "source": source,
            }
        return {
            "grade": GOV_VALUE_D,
            "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
            "rationale": "category_benchmark",
            "is_category_benchmark": True,
            "source": source,
        }

    # Exact / nearly exact
    if state == GOV_VALUE_EXACT or tier == "A" or match in {"EXACT", "NSN", "MPN", "MODEL"}:
        if "same buyer" in str(gov.get("match_rationale") or "").lower() or source == "BUYER_PRICE_HISTORY_AVAILABLE":
            grade = GOV_VALUE_A
            rationale = "buyer_exact_or_exact_award"
        else:
            grade = GOV_VALUE_A
            rationale = "exact_or_nsn_award"
        return {
            "grade": grade,
            "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
            "rationale": rationale,
            "is_category_benchmark": False,
            "source": source,
        }

    if state == GOV_VALUE_STRONG or tier == "B" or source in {"budget", "estimated_value", "ceiling", "BUYER_HISTORICAL_VALUE"}:
        return {
            "grade": GOV_VALUE_B,
            "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
            "rationale": "strong_close_match_or_budget",
            "is_category_benchmark": False,
            "source": source,
        }

    if state in {GOV_VALUE_COMPARABLE, GOV_VALUE_RANGE} or tier == "C":
        # Narrow range with model → C; broad without model → closer to D
        if commercial.get("model") or commercial.get("mpn"):
            return {
                "grade": GOV_VALUE_C,
                "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
                "rationale": "comparable_or_range_with_model",
                "is_category_benchmark": False,
                "source": source,
            }
        return {
            "grade": GOV_VALUE_C,
            "freshness": grade_freshness(gov.get("date") or history.get("award_date")),
            "rationale": "comparable_family",
            "is_category_benchmark": False,
            "source": source,
        }

    return {
        "grade": GOV_VALUE_U,
        "freshness": "unknown",
        "rationale": "unclassified",
        "is_category_benchmark": False,
    }


def grade_freshness(date_str: Any) -> str:
    if not date_str:
        return "unknown"
    try:
        s = str(date_str)[:10]
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return "unknown"
    days = (_now() - dt).days
    if days <= 365:
        return "current"
    if days <= 730:
        return "recent"
    if days <= 1460:
        return "aging"
    return "stale"


def grade_supplier(candidate: dict[str, Any], *, commercial: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    domain = str(candidate.get("supplier_domain") or candidate.get("name") or "").lower()
    auth = str(candidate.get("authorization_state") or candidate.get("authorized_status") or "").upper()
    fit = str(candidate.get("product_fit") or "").upper()
    stype = str(candidate.get("source_type") or "").upper()
    mfr = str(commercial.get("manufacturer") or "").lower()
    model = str(commercial.get("model") or commercial.get("mpn") or "").lower()

    # Generic seed without product confirmation
    is_generic_seed = domain in _SEED_DOMAINS and fit not in {"EXACT"} and not candidate.get("exact_product_evidence")
    oem_match = bool(mfr and mfr.split()[0] in domain and stype == "OEM")

    if auth == AUTHORIZED_CONFIRMED and (fit == "EXACT" or candidate.get("exact_product_evidence") or oem_match):
        return SUPPLIER_A
    if auth == AUTHORIZED_CONFIRMED:
        return SUPPLIER_B
    if (auth == AUTHORIZED_LIKELY or oem_match) and (fit in {"EXACT", "FAMILY", "STRONG"} or model or mfr):
        if fit == "EXACT" or candidate.get("exact_product_evidence"):
            return SUPPLIER_B
        return SUPPLIER_B if oem_match else SUPPLIER_C
    if auth == AUTHORIZATION_NOT_REQUIRED and fit in {"EXACT", "FAMILY"} and not is_generic_seed:
        return SUPPLIER_C
    if is_generic_seed or (stype in {"DISTRIBUTOR", "RESELLER", "DEALER", "COOPERATIVE", "OEM"} and fit in {"", "FAMILY"} and not model):
        # OEM with manufacturer still B/C above; pure seeds without mfr/model → D
        if oem_match and mfr:
            return SUPPLIER_B
        if mfr and domain and mfr.split()[0] in domain:
            return SUPPLIER_C
        return SUPPLIER_D
    if candidate.get("prior_awardee_lead"):
        return SUPPLIER_C
    if domain:
        return SUPPLIER_C if mfr or model else SUPPLIER_D
    return SUPPLIER_UNKNOWN


def best_supplier_grade(suppliers: list[dict[str, Any]], *, commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    if not suppliers:
        return {"grade": SUPPLIER_UNKNOWN, "count": 0, "graded": [], "a_b_count": 0, "c_count": 0, "d_count": 0}
    graded = []
    for s in suppliers:
        g = grade_supplier(s, commercial=commercial)
        graded.append({**s, "supplier_grade": g})
    order = {SUPPLIER_A: 0, SUPPLIER_B: 1, SUPPLIER_C: 2, SUPPLIER_D: 3, SUPPLIER_UNKNOWN: 4}
    graded.sort(key=lambda x: order.get(x.get("supplier_grade"), 9))
    best = graded[0].get("supplier_grade")
    return {
        "grade": best,
        "count": len(graded),
        "graded": graded,
        "a_b_count": sum(1 for g in graded if g.get("supplier_grade") in {SUPPLIER_A, SUPPLIER_B}),
        "c_count": sum(1 for g in graded if g.get("supplier_grade") == SUPPLIER_C),
        "d_count": sum(1 for g in graded if g.get("supplier_grade") == SUPPLIER_D),
        "only_d": all(g.get("supplier_grade") == SUPPLIER_D for g in graded),
    }


def grade_quantity(qty_info: dict[str, Any] | None, row: dict[str, Any] | None = None) -> dict[str, Any]:
    qty_info = qty_info or {}
    q = _f(qty_info.get("quantity") or (row or {}).get("quantity"))
    quality = str(qty_info.get("quality") or "").upper()
    unit_only = bool(qty_info.get("unit_only") or quality == "UNIT_ONLY")
    if q and q > 0 and (quality in {"EXACT", ""} or (not unit_only and quality != "RANGE")):
        if quality == "RANGE":
            return {"grade": QUANTITY_B_RANGE, "quantity": q, "unit_only": False}
        return {"grade": QUANTITY_A_EXACT, "quantity": q, "unit_only": False}
    if quality == "RANGE" and q:
        return {"grade": QUANTITY_B_RANGE, "quantity": q, "unit_only": False}
    # Inherent single-unit from title
    title = str((row or {}).get("title") or "")
    if re.search(r"(?i)\b(one\s*\(\s*1\s*\)|purchase of one|qty\.?\s*1\b|\(1\))", title) and not unit_only:
        return {"grade": QUANTITY_A_EXACT, "quantity": 1.0, "unit_only": False, "inferred_single": True}
    if unit_only or quality == "UNIT_ONLY":
        return {"grade": QUANTITY_C_UNIT_ONLY, "quantity": q, "unit_only": True}
    return {"grade": QUANTITY_UNKNOWN, "quantity": None, "unit_only": False}


def grade_configuration(row: dict[str, Any], commercial: dict[str, Any] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    cfg = cfg or {}
    blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    configurable = bool(
        re.search(
            r"\b(vehicle|truck|suv|server|upfit|option|configur|bobcat|excavator|loader|ambulance|bus)\b",
            blob,
            re.I,
        )
    )
    bundled = bool(cfg.get("bundled") or re.search(r"\b(warranty package|install|turnkey|upfit|accessory)\b", blob))
    has_model = bool(commercial.get("model") or commercial.get("mpn"))
    brand_or_equal = bool(cfg.get("brand_or_equal") or re.search(r"\bor\s+equal\b", blob))

    if has_model and not bundled and not configurable:
        grade = CONFIG_A_EXACT
    elif has_model and configurable and not bundled:
        grade = CONFIG_B_STRONG
    elif has_model and bundled:
        grade = CONFIG_C_PARTIAL  # options may destroy bare-vs-loaded compare
    elif brand_or_equal and has_model:
        grade = CONFIG_B_STRONG
    elif has_model:
        grade = CONFIG_B_STRONG
    else:
        grade = CONFIG_UNKNOWN

    return {
        "grade": grade,
        "configurable": configurable,
        "bundled_or_options": bundled,
        "brand_or_equal": brand_or_equal,
        "bare_vs_loaded_risk": bool(bundled and configurable),
    }


def grade_freight(row: dict[str, Any], freight: dict[str, Any] | None = None, headroom: float | None = None) -> str:
    freight = freight or {}
    title = str(row.get("title") or "").lower()
    status = str(freight.get("status") or "")
    heavy = bool(re.search(r"\b(vehicle|truck|trailer|excavator|loader|forklift|bus|rail)\b", title))
    if status == "FREIGHT_QUOTE_REQUIRED" or re.search(r"\b(alaska|hawaii)\b", title):
        return FREIGHT_QUOTE_REQUIRED
    if heavy:
        if headroom is not None and headroom < 5000:
            return FREIGHT_MATERIAL_UNRESOLVED
        return FREIGHT_RESERVE_SUFFICIENT
    if freight.get("amount") and float(freight.get("amount") or 0) <= 500:
        return FREIGHT_LOW_IMPACT
    return FREIGHT_RESERVE_SUFFICIENT


def revise_profit_tiers(
    *,
    qdep_tiers: dict[str, Any] | None,
    quantity_grade: str,
    max_buy: dict[str, Any] | None,
    gov_unit: float | None,
) -> dict[str, Any]:
    """Unit-only cannot retain unsupported total-profit tiers."""
    qdep_tiers = qdep_tiers or {}
    if quantity_grade == QUANTITY_C_UNIT_ONLY:
        # Unit margin only
        unit_positive = bool(_f((max_buy or {}).get("supplier_quote_target")) and gov_unit)
        return {
            "basis": UNIT_MARGIN_POSITIVE,
            "positive": unit_positive and bool(qdep_tiers.get("quote_dependent_positive")),
            "ge_5k": False,
            "ge_10k": False,
            "ge_25k": False,
            "ge_50k": False,
            "ge_100k": False,
            "note": "total_profit_tiers_suppressed_unit_only",
        }
    if quantity_grade == QUANTITY_UNKNOWN:
        return {
            "basis": "UNKNOWN_QUANTITY",
            "positive": False,
            "ge_5k": False,
            "ge_10k": False,
            "ge_25k": False,
            "ge_50k": False,
            "ge_100k": False,
            "note": "quantity_unknown",
        }
    return {
        "basis": "TOTAL",
        "positive": bool(qdep_tiers.get("quote_dependent_positive")),
        "ge_5k": bool(qdep_tiers.get("ge_5k")),
        "ge_10k": bool(qdep_tiers.get("ge_10k")),
        "ge_25k": bool(qdep_tiers.get("ge_25k")),
        "ge_50k": bool(qdep_tiers.get("ge_50k")),
        "ge_100k": bool(qdep_tiers.get("ge_100k")),
    }


def confidence_matrix(gov_grade: str, supplier_grade: str) -> dict[str, Any]:
    """Deterministic Gov×Supplier → quote confidence (L.10 economic confidence levels)."""
    if gov_grade == GOV_VALUE_D:
        return {
            "confidence": "recon",
            "economic_confidence": "RECON_ONLY",
            "rule": "D_any_recon_unless_upgraded",
            "allows_validated": False,
        }
    if supplier_grade == SUPPLIER_D or supplier_grade == SUPPLIER_UNKNOWN:
        return {
            "confidence": "supplier_validation_required",
            "economic_confidence": "LOW",
            "rule": "any_D_supplier",
            "allows_validated": False,
        }
    if gov_grade == GOV_VALUE_A and supplier_grade in {SUPPLIER_A, SUPPLIER_B}:
        return {
            "confidence": "high",
            "economic_confidence": "HIGH",
            "rule": "A_plus_AB",
            "allows_validated": True,
        }
    if gov_grade in {GOV_VALUE_A, GOV_VALUE_B} and supplier_grade == SUPPLIER_C:
        return {
            "confidence": "medium_high",
            "economic_confidence": "MEDIUM",
            "rule": "AB_plus_C",
            "allows_validated": False,
            "allows_secondary": True,
        }
    if gov_grade == GOV_VALUE_B and supplier_grade in {SUPPLIER_A, SUPPLIER_B}:
        return {
            "confidence": "high",
            "economic_confidence": "HIGH",
            "rule": "B_plus_AB",
            "allows_validated": True,
        }
    if gov_grade == GOV_VALUE_C and supplier_grade in {SUPPLIER_A, SUPPLIER_B}:
        return {
            "confidence": "medium",
            "economic_confidence": "MEDIUM",
            "rule": "C_plus_AB",
            "allows_validated": False,
            "allows_secondary": True,
        }
    if gov_grade == GOV_VALUE_C and supplier_grade == SUPPLIER_C:
        return {
            "confidence": "medium_low",
            "economic_confidence": "LOW",
            "rule": "C_plus_C",
            "allows_validated": False,
            "allows_secondary": True,
        }
    return {
        "confidence": "low",
        "economic_confidence": "LOW",
        "rule": "default_weak",
        "allows_validated": False,
    }


def attempt_gov_upgrade(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    prior_grade: str | None = None,
) -> dict[str, Any]:
    """L.10 exact Gov D upgrade loop (buyer-history first, category last)."""
    from phase_l.buyer_history_workflow import run_gov_value_upgrade_loop

    commercial = commercial or {}
    before = prior_grade
    loop = run_gov_value_upgrade_loop(
        row,
        commercial=commercial,
        history=history,
        buyer_memory=buyer_memory,
        stage3=stage3,
    )
    upgraded = loop.get("gov") or {}
    # Prefer cache / history if stronger than loop result
    cache_hit = lookup_enrichment_cache_history(row, commercial)
    if cache_hit and (not upgraded.get("recovered") or str(upgraded.get("tier")) in {"C", "D", ""}):
        upgraded = {**upgraded, **cache_hit, "recovered": True, "upgraded_from_cache": True}
        graded = grade_government_value(upgraded, commercial=commercial, history=history)
        loop["graded"] = graded
        loop["grade_after"] = graded["grade"]
        loop["upgraded"] = graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}
    graded = loop.get("graded") or grade_government_value(upgraded, commercial=commercial, history=history)
    return {
        "gov": upgraded,
        "grade_before": before,
        "grade_after": graded["grade"],
        "upgraded": before == GOV_VALUE_D and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
        "freshness": graded.get("freshness"),
        "graded": graded,
        "upgrade_loop": loop,
    }


def attempt_supplier_upgrade(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """L.10 exact supplier upgrade loop with product-fit validation."""
    from phase_l.supplier_upgrade import run_supplier_upgrade_loop

    commercial = commercial or {}
    loop = run_supplier_upgrade_loop(
        row,
        commercial=commercial,
        history=history,
        supplier_memory=supplier_memory,
    )
    return {
        "suppliers_rec": {"suppliers": loop.get("suppliers") or []},
        "best": loop.get("best") or best_supplier_grade([], commercial=commercial),
        "upgrade_loop": loop,
    }


def confidence_adjusted_opportunity_value(
    *,
    profit_tier: dict[str, Any],
    gov_grade: str,
    supplier_grade: str,
    quantity_grade: str,
    config_grade: str,
    freight: str,
    deadline_days: float | None,
    recurring: bool = False,
) -> dict[str, Any]:
    """Prefer strong $20K exact over speculative $100K category benchmark."""
    # Base points from revised profit
    if profit_tier.get("ge_100k"):
        base = 100
    elif profit_tier.get("ge_50k"):
        base = 70
    elif profit_tier.get("ge_25k"):
        base = 45
    elif profit_tier.get("ge_10k"):
        base = 25
    elif profit_tier.get("ge_5k"):
        base = 15
    elif profit_tier.get("positive"):
        base = 8
    else:
        base = 0

    gov_w = {GOV_VALUE_A: 1.0, GOV_VALUE_B: 0.85, GOV_VALUE_C: 0.55, GOV_VALUE_D: 0.15, GOV_VALUE_U: 0.0}.get(
        gov_grade, 0.0
    )
    sup_w = {SUPPLIER_A: 1.0, SUPPLIER_B: 0.85, SUPPLIER_C: 0.55, SUPPLIER_D: 0.15, SUPPLIER_UNKNOWN: 0.0}.get(
        supplier_grade, 0.0
    )
    qty_w = {
        QUANTITY_A_EXACT: 1.0,
        QUANTITY_B_RANGE: 0.8,
        QUANTITY_C_UNIT_ONLY: 0.35,
        QUANTITY_UNKNOWN: 0.1,
    }.get(quantity_grade, 0.1)
    cfg_w = {
        CONFIG_A_EXACT: 1.0,
        CONFIG_B_STRONG: 0.85,
        CONFIG_C_PARTIAL: 0.45,
        CONFIG_UNKNOWN: 0.25,
    }.get(config_grade, 0.25)
    freight_w = {
        FREIGHT_LOW_IMPACT: 1.0,
        FREIGHT_RESERVE_SUFFICIENT: 0.85,
        FREIGHT_MATERIAL_UNRESOLVED: 0.4,
        FREIGHT_QUOTE_REQUIRED: 0.35,
    }.get(freight, 0.7)

    conf = gov_w * 0.35 + sup_w * 0.30 + qty_w * 0.20 + cfg_w * 0.10 + freight_w * 0.05
    value = base * conf
    if recurring:
        value *= 1.15
    if deadline_days is not None:
        if deadline_days < 3:
            value *= 0.2
        elif deadline_days < 5:
            value *= 0.5
        elif deadline_days >= 14:
            value *= 1.05

    return {
        "ConfidenceAdjustedOpportunityValue": round(value, 2),
        "confidence_factor": round(conf, 3),
        "base_profit_points": base,
    }


def quote_priority_score_v2(
    *,
    cav: dict[str, Any],
    gov_grade: str,
    supplier_grade: str,
    quantity_grade: str,
    config_grade: str,
    freight: str,
    deadline_days: float | None,
    eligibility_ok: bool,
    original_complete: bool,
    recurring: bool = False,
) -> dict[str, Any]:
    score = int(min(100, (cav.get("ConfidenceAdjustedOpportunityValue") or 0) * 1.2))
    factors = [f"cav:{cav.get('ConfidenceAdjustedOpportunityValue')}"]
    score += {"GOV_VALUE_A": 15, "GOV_VALUE_B": 10, "GOV_VALUE_C": 4, "GOV_VALUE_D": 0}.get(gov_grade, 0)
    score += {"SUPPLIER_A": 15, "SUPPLIER_B": 10, "SUPPLIER_C": 4, "SUPPLIER_D": 0}.get(supplier_grade, 0)
    if quantity_grade == QUANTITY_A_EXACT:
        score += 8
    if config_grade in {CONFIG_A_EXACT, CONFIG_B_STRONG}:
        score += 5
    if freight == FREIGHT_MATERIAL_UNRESOLVED:
        score -= 10
    if freight == FREIGHT_QUOTE_REQUIRED:
        score -= 8
    runway = quote_runway(deadline_days)
    score -= int(runway.get("penalty") or 0)
    if not eligibility_ok:
        score -= 40
    if original_complete:
        score += 5
    if recurring:
        score += 8
        factors.append("recurring")
    return {"score": max(0, min(100, score)), "factors": factors, "runway": runway}


def classify_l9_quality_state(
    *,
    gov_grade: str,
    supplier_info: dict[str, Any],
    quantity_grade: str,
    config_info: dict[str, Any],
    freight: str,
    matrix: dict[str, Any],
    eligibility_blockers: list[str],
    deadline_days: float | None,
    original_verified: bool,
    lane: str | None,
    is_quote_dependent: bool,
) -> dict[str, Any]:
    """Deterministic final L.9 quality state."""
    blockers: list[str] = []
    supplier_grade = supplier_info.get("grade") or SUPPLIER_UNKNOWN
    config_grade = (config_info or {}).get("grade") or CONFIG_UNKNOWN

    if eligibility_blockers:
        return {"state": HARD_BLOCKED, "blockers": eligibility_blockers, "reason": "eligibility"}

    if lane == MILSPEC_SPECIALTY and supplier_grade in {SUPPLIER_D, SUPPLIER_UNKNOWN}:
        return {"state": HARD_BLOCKED, "blockers": ["specialty_no_credible_path"], "reason": "specialty"}

    if deadline_days is not None and deadline_days < 0:
        return {"state": HARD_BLOCKED, "blockers": ["expired"], "reason": "deadline_expired"}

    if not original_verified:
        blockers.append("original_source_unverified")

    if not is_quote_dependent and gov_grade == GOV_VALUE_U:
        return {"state": ECONOMIC_CASE_TOO_WEAK, "blockers": blockers, "reason": "not_positive"}

    # Category benchmark alone
    if gov_grade == GOV_VALUE_D:
        return {
            "state": RECON_ONLY_CATEGORY_BENCHMARK,
            "blockers": blockers + ["gov_category_benchmark_only"],
            "reason": "gov_D",
        }

    # Generic supplier seed alone
    if supplier_info.get("only_d") or supplier_grade == SUPPLIER_D:
        return {
            "state": RECON_ONLY_SUPPLIER_SEED,
            "blockers": blockers + ["supplier_seed_only"],
            "reason": "supplier_D",
        }

    if supplier_grade == SUPPLIER_UNKNOWN:
        return {
            "state": PROMISING_NEEDS_SUPPLIER_CONFIRMATION,
            "blockers": blockers + ["no_supplier"],
            "reason": "supplier_unknown",
        }

    runway = quote_runway(deadline_days)
    if deadline_days is not None and not runway.get("enough_for_quote"):
        return {
            "state": HARD_BLOCKED,
            "blockers": blockers + ["deadline_too_short"],
            "reason": "deadline",
        }

    if (config_info or {}).get("bare_vs_loaded_risk") and config_grade == CONFIG_C_PARTIAL:
        # Can still be secondary if otherwise strong
        if not (matrix.get("allows_validated") and gov_grade in {GOV_VALUE_A, GOV_VALUE_B}):
            return {
                "state": PROMISING_NEEDS_CONFIGURATION,
                "blockers": blockers + ["bare_vs_loaded"],
                "reason": "configuration",
            }

    if quantity_grade == QUANTITY_UNKNOWN:
        return {
            "state": PROMISING_NEEDS_QUANTITY,
            "blockers": blockers + ["quantity_unknown"],
            "reason": "quantity",
        }

    if freight == FREIGHT_MATERIAL_UNRESOLVED and gov_grade != GOV_VALUE_A:
        return {
            "state": PROMISING_NEEDS_FREIGHT,
            "blockers": blockers + ["freight_material"],
            "reason": "freight",
        }

    if freight == FREIGHT_QUOTE_REQUIRED and matrix.get("confidence") not in {"high"}:
        # secondary possible
        pass

    # Validated gate
    qty_ok = quantity_grade in {QUANTITY_A_EXACT, QUANTITY_B_RANGE} or (
        quantity_grade == QUANTITY_C_UNIT_ONLY and True  # unit quote basis OK
    )
    config_ok = config_grade in {CONFIG_A_EXACT, CONFIG_B_STRONG} or (
        config_grade == CONFIG_C_PARTIAL and not (config_info or {}).get("bare_vs_loaded_risk")
    )
    supplier_ok = supplier_grade in {SUPPLIER_A, SUPPLIER_B} or (
        supplier_grade == SUPPLIER_C and int(supplier_info.get("c_count") or 0) >= 2
    )
    gov_ok = gov_grade in {GOV_VALUE_A, GOV_VALUE_B} or (
        gov_grade == GOV_VALUE_C and supplier_grade in {SUPPLIER_A, SUPPLIER_B}
    )

    if (
        matrix.get("allows_validated")
        and gov_ok
        and supplier_ok
        and qty_ok
        and config_ok
        and original_verified
        and (deadline_days is None or runway.get("enough_for_quote"))
        and freight != FREIGHT_MATERIAL_UNRESOLVED
    ):
        # Strong C with conservative + Supplier A/B already in matrix allows_secondary mostly;
        # only A/B gov with A/B supplier validates per matrix
        if gov_grade == GOV_VALUE_C:
            return {
                "state": SECONDARY_QUOTE_TARGET,
                "blockers": blockers,
                "reason": "gov_C_secondary",
            }
        return {
            "state": VALIDATED_QUOTE_TARGET,
            "blockers": blockers,
            "reason": "validated_gate_pass",
        }

    if matrix.get("allows_secondary") or (
        gov_grade in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}
        and supplier_grade in {SUPPLIER_A, SUPPLIER_B, SUPPLIER_C}
        and qty_ok
        and original_verified
    ):
        if gov_grade in {GOV_VALUE_A, GOV_VALUE_B} and supplier_grade == SUPPLIER_C:
            return {"state": SECONDARY_QUOTE_TARGET, "blockers": blockers, "reason": "gov_AB_supplier_C"}
        if gov_grade == GOV_VALUE_C and supplier_grade in {SUPPLIER_A, SUPPLIER_B}:
            return {"state": SECONDARY_QUOTE_TARGET, "blockers": blockers, "reason": "gov_C_supplier_AB"}
        if freight in {FREIGHT_MATERIAL_UNRESOLVED, FREIGHT_QUOTE_REQUIRED} and gov_grade in {
            GOV_VALUE_A,
            GOV_VALUE_B,
        }:
            return {"state": SECONDARY_QUOTE_TARGET, "blockers": blockers + ["freight_watch"], "reason": "freight_secondary"}
        if gov_grade in {GOV_VALUE_A, GOV_VALUE_B} and supplier_ok and qty_ok:
            # almost validated but missing something minor
            if not config_ok:
                return {"state": PROMISING_NEEDS_CONFIGURATION, "blockers": blockers, "reason": "config"}
            return {"state": SECONDARY_QUOTE_TARGET, "blockers": blockers, "reason": "near_validated"}

    if gov_grade in {GOV_VALUE_C} and supplier_grade in {SUPPLIER_C, SUPPLIER_D}:
        return {"state": PROMISING_NEEDS_BETTER_GOV_VALUE, "blockers": blockers, "reason": "weak_pair"}

    if supplier_grade == SUPPLIER_C and gov_grade in {GOV_VALUE_A, GOV_VALUE_B}:
        return {"state": PROMISING_NEEDS_SUPPLIER_CONFIRMATION, "blockers": blockers, "reason": "need_supplier_ab"}

    return {"state": ECONOMIC_CASE_TOO_WEAK, "blockers": blockers, "reason": "weak_overall"}


def audit_quote_positive(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    gov: dict[str, Any] | None = None,
    suppliers: list[dict[str, Any]] | None = None,
    qty_info: dict[str, Any] | None = None,
    max_buy: dict[str, Any] | None = None,
    qdep: dict[str, Any] | None = None,
    freight: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
    lane: str | None = None,
    deadline_days: float | None = None,
    buyer_memory: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    recurring: bool = False,
    attempt_upgrades: bool = True,
) -> dict[str, Any]:
    """Full L.9/L.10 audit for one quote-dependent (or candidate) row."""
    commercial = dict(commercial or {})
    original = original or {}
    upgrades: dict[str, Any] = {}

    # L.10: fill model/manufacturer from title when Stage-2 commercial is thin
    if not commercial.get("model") or not commercial.get("manufacturer"):
        try:
            from phase_l.commercial_identity import extract_commercial_model, infer_manufacturer

            blob = f"{row.get('title') or ''} {row.get('description') or ''}"
            if not commercial.get("model"):
                mh = extract_commercial_model(blob)
                if mh and mh.get("model"):
                    commercial["model"] = mh["model"]
            if not commercial.get("manufacturer"):
                mf = infer_manufacturer(blob, model=commercial.get("model"))
                if mf and mf.get("manufacturer"):
                    commercial["manufacturer"] = mf["manufacturer"]
        except Exception:
            pass

    gov_grade_info = grade_government_value(gov, commercial=commercial, history=history)
    if attempt_upgrades and gov_grade_info.get("grade") == GOV_VALUE_D:
        up = attempt_gov_upgrade(
            row,
            commercial=commercial,
            history=history,
            buyer_memory=buyer_memory,
            stage3=stage3,
            prior_grade=GOV_VALUE_D,
        )
        upgrades["gov"] = {"before": GOV_VALUE_D, "after": up["grade_after"], "upgraded": up["upgraded"]}
        if up["upgraded"] or up["grade_after"] != GOV_VALUE_D:
            gov = up["gov"]
            gov_grade_info = up["graded"]

    supplier_info = best_supplier_grade(suppliers or [], commercial=commercial)
    if attempt_upgrades and (supplier_info.get("only_d") or supplier_info.get("grade") in {SUPPLIER_D, SUPPLIER_C}):
        up_s = attempt_supplier_upgrade(
            row, commercial=commercial, history=history, supplier_memory=supplier_memory
        )
        upgrades["supplier"] = {
            "before": supplier_info.get("grade"),
            "after": up_s["best"].get("grade"),
            "upgraded": order_supplier(up_s["best"].get("grade")) < order_supplier(supplier_info.get("grade")),
        }
        supplier_info = up_s["best"]
        suppliers = up_s["best"].get("graded") or suppliers

    if not qty_info:
        qty_info = recover_quantity(row, commercial=commercial)
    qty_grade_info = grade_quantity(qty_info, row)
    config_info = grade_configuration(row, commercial)

    headroom = None
    target = _f((max_buy or {}).get("supplier_quote_target"))
    # No observed acquisition → QUOTE_TARGET_ONLY
    acquisition_headroom_status = QUOTE_TARGET_ONLY

    freight_grade = grade_freight(row, freight, headroom=headroom)
    revised = revise_profit_tiers(
        qdep_tiers=(qdep or {}).get("tiers"),
        quantity_grade=qty_grade_info["grade"],
        max_buy=max_buy,
        gov_unit=_f((gov or {}).get("unit_value")),
    )
    matrix = confidence_matrix(gov_grade_info["grade"], supplier_info.get("grade") or SUPPLIER_UNKNOWN)
    elig = hard_eligibility_blockers(row, lane=lane, commercial=commercial)

    is_pos = bool((qdep or {}).get("tiers", {}).get("quote_dependent_positive")) or bool(
        target and gov_grade_info["grade"] not in {GOV_VALUE_U}
    )

    quality = classify_l9_quality_state(
        gov_grade=gov_grade_info["grade"],
        supplier_info=supplier_info,
        quantity_grade=qty_grade_info["grade"],
        config_info=config_info,
        freight=freight_grade,
        matrix=matrix,
        eligibility_blockers=elig,
        deadline_days=deadline_days,
        original_verified=bool(original.get("original_source_verified")),
        lane=lane,
        is_quote_dependent=is_pos,
    )

    cav = confidence_adjusted_opportunity_value(
        profit_tier=revised,
        gov_grade=gov_grade_info["grade"],
        supplier_grade=supplier_info.get("grade") or SUPPLIER_UNKNOWN,
        quantity_grade=qty_grade_info["grade"],
        config_grade=config_info["grade"],
        freight=freight_grade,
        deadline_days=deadline_days,
        recurring=recurring,
    )
    priority = quote_priority_score_v2(
        cav=cav,
        gov_grade=gov_grade_info["grade"],
        supplier_grade=supplier_info.get("grade") or SUPPLIER_UNKNOWN,
        quantity_grade=qty_grade_info["grade"],
        config_grade=config_info["grade"],
        freight=freight_grade,
        deadline_days=deadline_days,
        eligibility_ok=not elig,
        original_complete=bool(original.get("original_source_verified")),
        recurring=recurring,
    )

    manual = False
    if quality["state"] in {
        PROMISING_NEEDS_BETTER_GOV_VALUE,
        PROMISING_NEEDS_SUPPLIER_CONFIRMATION,
        PROMISING_NEEDS_CONFIGURATION,
        PROMISING_NEEDS_QUANTITY,
        PROMISING_NEEDS_FREIGHT,
    } and (revised.get("ge_10k") or revised.get("ge_25k") or cav.get("ConfidenceAdjustedOpportunityValue", 0) >= 15):
        manual = True

    return {
        "build": BUILD,
        "quality_state": quality["state"],
        "quality_reason": quality.get("reason"),
        "blockers": quality.get("blockers") or [],
        "gov_grade": gov_grade_info["grade"],
        "gov_freshness": gov_grade_info.get("freshness"),
        "gov_value": _f((gov or {}).get("unit_value")) or _f((gov or {}).get("total_value")),
        "gov_source": (gov or {}).get("source"),
        "is_category_benchmark": gov_grade_info.get("is_category_benchmark"),
        "supplier_grade": supplier_info.get("grade"),
        "supplier_a_b_count": supplier_info.get("a_b_count"),
        "supplier_c_count": supplier_info.get("c_count"),
        "supplier_d_count": supplier_info.get("d_count"),
        "supplier_count": supplier_info.get("count"),
        "authorization_best": next(
            (
                s.get("authorization_state")
                for s in (supplier_info.get("graded") or [])
                if s.get("supplier_grade") in {SUPPLIER_A, SUPPLIER_B}
            ),
            (supplier_info.get("graded") or [{}])[0].get("authorization_state")
            if supplier_info.get("graded")
            else None,
        ),
        "quantity_grade": qty_grade_info["grade"],
        "quantity": qty_grade_info.get("quantity"),
        "config_grade": config_info["grade"],
        "freight_grade": freight_grade,
        "max_buy": target,
        "acquisition_headroom_status": acquisition_headroom_status,
        "profit_tier_before": (qdep or {}).get("tiers"),
        "profit_tier_revised": revised,
        "confidence_matrix": matrix,
        "cav": cav,
        "quote_priority_v2": priority,
        "upgrades": upgrades,
        "manual_evidence_review": manual,
        "send_authorized": False,
        "outreach_authorized": False,
        "validated_quote_dependent": quality["state"] == VALIDATED_QUOTE_TARGET,
    }


def order_supplier(grade: str | None) -> int:
    return {SUPPLIER_A: 0, SUPPLIER_B: 1, SUPPLIER_C: 2, SUPPLIER_D: 3, SUPPLIER_UNKNOWN: 4}.get(grade or "", 9)
