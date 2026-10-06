"""Per-opportunity process: inventory → research → strict economics → UI."""

from __future__ import annotations

from typing import Any

from line_basket_completion_strict_economics.cluster import build_clusters, cluster_key
from line_basket_completion_strict_economics.collier import audit_collier_revenue, validate_collier
from line_basket_completion_strict_economics.corpus import record_stage_transition
from line_basket_completion_strict_economics.economics import classify_basket, compute_strict_economics
from line_basket_completion_strict_economics.inventory import (
    apply_conservation,
    coverage_metrics,
    enumerate_opportunity_lines,
)
from line_basket_completion_strict_economics.models import (
    BASKET_BLOCKED,
    BASKET_READY_PUBLIC_PRICE,
    BASKET_READY_QUOTE_DEPENDENT,
    COLLIER_DEADLINE_S,
    COLLIER_OID,
    ECONOMICS_NOT_READY,
    ECONOMICS_READY,
    ITEM_DEADLINE_S,
    PRIOR_REV,
)
from line_basket_completion_strict_economics.owner_ui import owner_view
from line_basket_completion_strict_economics.research import research_opportunity_lines
from m3_data_root import data_path
import json


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _revenue_for(oid: str, corpus_row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    if oid == COLLIER_OID:
        audit = audit_collier_revenue()
        if not audit.get("usable_as_revenue"):
            return 0.0, audit
        return float(audit.get("government_expected_revenue") or 0), audit

    rev_store = _load(PRIOR_REV)
    row = (rev_store.get("by_opportunity") or {}).get(oid) or {}
    revenue_block = row.get("revenue") or {}
    best = revenue_block.get("best") or {}
    val = best.get("reference_value") or best.get("value")
    if val is None:
        val = (corpus_row.get("revenue_evidence") or {}).get("value")
    # Reject bonding-threshold style if snippet present
    from line_basket_completion_strict_economics.collier import _BOND_THRESHOLD

    snippet = str(best.get("snippet") or "")
    if val and _BOND_THRESHOLD.search(snippet):
        return 0.0, {
            "usable_as_revenue": False,
            "evidence_type": "BONDING_THRESHOLD_LANGUAGE_NOT_CONTRACT_VALUE",
            "reported_prior_value": val,
            "snippet": snippet[:200],
        }
    return float(val or 0), {
        "usable_as_revenue": bool(val),
        "value": val,
        "evidence_type": best.get("revenue_evidence_type"),
        "source": best.get("source"),
        "value_class": "current_explicit_budget" if best.get("revenue_evidence_type") == "CURRENT_VALUE_EXPLICIT" else "estimate",
    }


def process_opportunity(
    corpus_row: dict[str, Any],
    *,
    run_id: str,
    stats: dict[str, Any] | None = None,
    cluster_prices: dict[str, dict[str, Any]] | None = None,
    deadline_s: float | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    cluster_prices = cluster_prices if cluster_prices is not None else {}
    oid = corpus_row["opportunity_id"]
    print(f"[lbc] inventory {oid}", flush=True)

    raw_lines = enumerate_opportunity_lines(oid)
    # Assign provisional cluster ids before research
    for ln in raw_lines:
        key = cluster_key(ln)
        if key:
            import hashlib

            ln["cluster_id"] = "CL-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]

    revenue, rev_ev = _revenue_for(oid, corpus_row)
    dl = deadline_s or (COLLIER_DEADLINE_S if oid == COLLIER_OID else ITEM_DEADLINE_S)
    # Adaptive deadline
    if revenue >= 50000:
        dl = max(dl, 180.0)
    elif revenue >= 10000:
        dl = max(dl, 140.0)

    print(f"[lbc] research {oid} lines={len(raw_lines)} revenue_hint={revenue} deadline={dl}s", flush=True)
    researched = research_opportunity_lines(
        raw_lines,
        opportunity_id=oid,
        revenue_hint=revenue,
        stats=stats,
        deadline_s=dl,
        cluster_prices=cluster_prices,
    )
    cons = apply_conservation(researched)
    cov = coverage_metrics(researched)
    basket = classify_basket(researched, cov)
    print(
        f"[lbc] coverage {oid} material={cov.get('MATERIAL_VALUE_COVERAGE')} "
        f"priced={cov.get('priced_executable')}/{cov.get('total_lines')} class={basket.get('basket_class')}",
        flush=True,
    )

    prior_state = "LINES_EXTRACTED"
    if basket.get("basket_class") in {BASKET_READY_PUBLIC_PRICE, BASKET_READY_QUOTE_DEPENDENT}:
        record_stage_transition(
            opportunity_id=oid,
            prior_state=prior_state,
            new_state="BASKET_READY",
            reason=str(basket.get("reason")),
            run_id=run_id,
        )
        prior_state = "BASKET_READY"

    economics = compute_strict_economics(
        oid,
        lines=researched,
        coverage=cov,
        basket=basket,
        revenue=revenue,
        revenue_evidence=rev_ev,
    )
    print(
        f"[lbc] econ {oid} status={economics.get('economics_status')} "
        f"confidence={economics.get('profit_confidence')} headline={economics.get('headline')}",
        flush=True,
    )

    if economics.get("economics_status") == ECONOMICS_READY:
        record_stage_transition(
            opportunity_id=oid,
            prior_state=prior_state,
            new_state="ECONOMICS_READY",
            reason=str(economics.get("economics_path") or "ready"),
            run_id=run_id,
        )
    else:
        record_stage_transition(
            opportunity_id=oid,
            prior_state=prior_state,
            new_state="ECONOMICS_NOT_READY",
            reason=str((economics.get("bounds") or {}).get("unresolved_material_lines") or "not_ready"),
            run_id=run_id,
        )

    # Execution / delivery check (lightweight)
    install_n = sum(1 for l in researched if (l.get("category") == "CONSTRUCTION_INSTALL"))
    execution = {
        "delivery_deadline": corpus_row.get("deadline"),
        "destination": next((l.get("delivery_destination") for l in researched if l.get("delivery_destination")), None),
        "install_lines": install_n,
        "delivery_risk": "HIGH_INSTALL_LABOR" if install_n >= 5 else "STANDARD",
        "bond_insurance": "REVIEW_REQUIRED",
        "oem_authorization": "UNKNOWN",
        "owner_cash_required": economics.get("owner_cash_required"),
        "financing_status": economics.get("financing_status"),
    }
    if float(economics.get("owner_cash_required") or 0) > 0:
        execution["financing_status"] = "FINANCING_BLOCKED"

    result = {
        "opportunity_id": oid,
        "buyer": corpus_row.get("buyer"),
        "source": corpus_row.get("source"),
        "lines": {
            "opportunity_id": oid,
            "TOTAL_LINES": len(researched),
            "PRICED_LINES": cov.get("priced_executable"),
            "lines": researched,
            "conservation": cons,
        },
        "line_rows": researched,
        "coverage": cov,
        "basket": basket,
        "economics": economics,
        "execution": execution,
        "conservation": cons,
        "revenue_evidence": rev_ev,
    }

    if oid == COLLIER_OID:
        result["collier_validation"] = validate_collier(result)

    # Lender-ready object (do not contact lender)
    if (
        economics.get("economics_status") == ECONOMICS_READY
        and economics.get("profit_confidence") in {"PROFIT_PROVEN", "PROFIT_LIKELY"}
        and float(economics.get("expected_profit") or 0) >= 5000
        and float(economics.get("owner_cash_required") or 0) == 0
    ):
        result["lender_ready_object"] = {
            "solicitation": corpus_row.get("solicitation_id"),
            "buyer": corpus_row.get("buyer"),
            "deadline": corpus_row.get("deadline"),
            "government_value_evidence": rev_ev,
            "product_basket_summary": {
                "total_lines": len(researched),
                "material_coverage": cov.get("MATERIAL_VALUE_COVERAGE"),
                "priced": cov.get("priced_executable"),
            },
            "acquisition_cost": economics.get("product_acquisition_cost"),
            "freight": economics.get("freight"),
            "financing_need": (economics.get("financing_detail") or {}).get("amount_requiring_financing"),
            "profit": economics.get("expected_profit"),
            "margin": economics.get("margin_pct"),
            "delivery": execution,
            "risks": ["install_labor"] if install_n else [],
            "provenance": "line_basket_completion_strict_economics",
        }
        record_stage_transition(
            opportunity_id=oid,
            prior_state="ECONOMICS_READY",
            new_state="LENDER_READY",
            reason="profit_proven_or_likely_owner_cash_0",
            run_id=run_id,
        )

    result["owner_view"] = owner_view(result)
    # Priority score for remaining research
    result["basket_completion_priority"] = round(
        float(revenue or 0) * 0.0001
        + float(cov.get("MATERIAL_VALUE_COVERAGE") or 0) * 100
        - float(bounds_unresolved(economics)) * 0.0001
        + (50 if oid == COLLIER_OID else 0),
        4,
    )
    return result


def bounds_unresolved(economics: dict[str, Any]) -> float:
    return float(economics.get("unresolved_cost_exposure") or 0)
