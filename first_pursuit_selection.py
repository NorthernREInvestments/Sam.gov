"""First-pursuit selection — rank for repeatable controlled real-world testing."""

from __future__ import annotations

from typing import Any

from m3_lifecycle import readiness_summary
from m3_pipeline_store import M3PipelineStore


# Prefer repeatability over max revenue
_REASONABLE_REVENUE_MIN = 5_000
_REASONABLE_REVENUE_MAX = 500_000
_MIN_DEADLINE_DAYS = 7


def _money(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _score_factor(name: str, points: int, evidence: str) -> dict[str, Any]:
    return {"factor": name, "points": points, "evidence": evidence}


def score_first_pursuit_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Explainable score — higher = better first controlled pursuit."""
    factors: list[dict[str, Any]] = []
    score = 0
    readiness = row.get("readiness_summary") or readiness_summary(row)
    package = str(row.get("package_access") or "").upper()
    category = str(row.get("product_category") or row.get("product_classification") or "UNKNOWN")
    bom = row.get("line_items") or row.get("bom") or []
    days = (row.get("deadline_evaluation") or {}).get("calendar_days_remaining")
    if days is None:
        days = readiness.get("time_left")
    econ = row.get("transaction_economics") or row.get("economics") or {}
    revenue = _money(
        econ.get("revenue")
        or econ.get("government_revenue")
        or row.get("government_revenue")
        or row.get("revenue")
    )
    compliance = row.get("bid_compliance") or row.get("compliance") or {}
    blockers = compliance.get("unresolved") or compliance.get("critical") or row.get("compliance_blockers") or []
    funding = str(row.get("funding_status") or (row.get("funding_requirement") or {}).get("status") or "UNKNOWN")

    # Tangible product fit
    if category not in {"UNKNOWN", "", "SERVICE", "SERVICES"} and "SERVICE" not in category.upper():
        score += 15
        factors.append(_score_factor("tangible_product_fit", 15, f"category={category}"))
    else:
        factors.append(_score_factor("tangible_product_fit", 0, f"category={category or 'UNKNOWN'}"))

    # Simple requirements / BOM
    if isinstance(bom, list) and 1 <= len(bom) <= 8:
        score += 12
        factors.append(_score_factor("simple_requirements", 12, f"bom_lines={len(bom)}"))
    elif isinstance(bom, list) and len(bom) > 8:
        score += 4
        factors.append(_score_factor("simple_requirements", 4, f"bom_lines={len(bom)} (complex)"))
    else:
        factors.append(_score_factor("simple_requirements", 0, "bom_UNKNOWN"))

    # Public package
    if package in {"PUBLIC", "PUBLIC_PACKAGE", "OPEN"}:
        score += 18
        factors.append(_score_factor("public_package", 18, package))
    elif package in {"AUTH_GATED", "REGISTRATION_REQUIRED"}:
        score -= 10
        factors.append(_score_factor("public_package", -10, package))
    else:
        factors.append(_score_factor("public_package", 0, package or "UNKNOWN"))

    # Sufficient deadline
    if isinstance(days, (int, float)):
        if days >= 14:
            score += 14
            factors.append(_score_factor("sufficient_deadline", 14, f"{days} days"))
        elif days >= _MIN_DEADLINE_DAYS:
            score += 8
            factors.append(_score_factor("sufficient_deadline", 8, f"{days} days"))
        elif days < _MIN_DEADLINE_DAYS:
            score -= 20
            factors.append(_score_factor("sufficient_deadline", -20, f"{days} days (tight)"))
    else:
        factors.append(_score_factor("sufficient_deadline", 0, "UNKNOWN"))

    # Supplier availability likelihood (proxy: commodity/IT categories)
    cat_u = category.upper()
    if any(k in cat_u for k in ("COMPUTER", "IT_", "LAPTOP", "MONITOR", "OFFICE", "EQUIPMENT", "SUPPLY")):
        score += 10
        factors.append(_score_factor("supplier_availability_likelihood", 10, cat_u))
    else:
        score += 3
        factors.append(_score_factor("supplier_availability_likelihood", 3, "generic/UNKNOWN"))

    # Financing simplicity — smaller capital / UNKNOWN not punished
    capital = _money(
        (row.get("funding_requirement") or {}).get("capital_amount")
        or row.get("working_capital_required")
        or readiness.get("capital_requirement")
    )
    if capital is not None and capital <= 75_000:
        score += 10
        factors.append(_score_factor("financing_simplicity", 10, f"capital={capital}"))
    elif capital is not None and capital <= 200_000:
        score += 5
        factors.append(_score_factor("financing_simplicity", 5, f"capital={capital}"))
    elif funding.upper() in {"UNKNOWN", "FUNDING_VERIFICATION_REQUIRED"}:
        score += 2
        factors.append(
            _score_factor(
                "financing_simplicity",
                2,
                "UNKNOWN financing preserved (not rejection)",
            )
        )
    else:
        factors.append(_score_factor("financing_simplicity", 0, f"funding={funding}"))

    # Compliance simplicity
    if not blockers:
        score += 10
        factors.append(_score_factor("compliance_simplicity", 10, "no_blockers_listed"))
    elif len(blockers) <= 2:
        score += 4
        factors.append(_score_factor("compliance_simplicity", 4, f"blockers={len(blockers)}"))
    else:
        score -= 8
        factors.append(_score_factor("compliance_simplicity", -8, f"blockers={len(blockers)}"))

    # Reasonable transaction size (not max revenue)
    if revenue is None:
        score += 2
        factors.append(_score_factor("reasonable_transaction_size", 2, "revenue=UNKNOWN"))
    elif _REASONABLE_REVENUE_MIN <= revenue <= _REASONABLE_REVENUE_MAX:
        score += 12
        factors.append(_score_factor("reasonable_transaction_size", 12, f"revenue={revenue}"))
    elif revenue < _REASONABLE_REVENUE_MIN:
        score += 3
        factors.append(_score_factor("reasonable_transaction_size", 3, f"revenue={revenue} (small)"))
    else:
        score -= 5
        factors.append(_score_factor("reasonable_transaction_size", -5, f"revenue={revenue} (large/complex)"))

    # Low execution complexity
    lifecycle = str(row.get("lifecycle") or "")
    if lifecycle in {
        "ECONOMICS_ATTRACTIVE",
        "ECONOMICS_PRELIMINARY",
        "BOM_READY",
        "FUNDING_VERIFICATION_REQUIRED",
        "COMMERCIAL_VERIFICATION_REQUIRED",
        "READY_FOR_OPERATOR",
        "READY_FOR_OPERATOR_ACTION",
    }:
        score += 8
        factors.append(_score_factor("low_execution_complexity", 8, lifecycle))
    elif lifecycle.startswith("REJECTED") or lifecycle in {"CANCELLED", "EXPIRED"}:
        score -= 50
        factors.append(_score_factor("low_execution_complexity", -50, lifecycle))
    else:
        factors.append(_score_factor("low_execution_complexity", 0, lifecycle or "UNKNOWN"))

    return {
        "canonical_id": row.get("canonical_id"),
        "title": row.get("title"),
        "buyer": row.get("agency") or row.get("buyer"),
        "source_id": row.get("source_id") or "UNKNOWN",
        "category": category,
        "score": score,
        "factors": factors,
        "rationale": "Ranked for first controlled real-world repeatability, not max revenue",
        "recommend_for_first_pursuit": score >= 40,
    }


def rank_first_pursuits(
    store: M3PipelineStore | None = None,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    store = store or M3PipelineStore()
    ranked = [score_first_pursuit_candidate(r) for r in store.all()]
    ranked.sort(key=lambda x: (-int(x.get("score") or 0), str(x.get("canonical_id"))))
    top = ranked[:limit]
    return {
        "kind": "M3FirstPursuitSelection",
        "objective": "repeatability",
        "count": len(top),
        "candidates": top,
        "selection_rule": "tangible_fit + public_package + deadline + simple_requirements + reasonable_size",
    }
