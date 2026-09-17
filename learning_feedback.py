"""Explainable learning feedback from transaction outcomes — no black-box scoring."""

from __future__ import annotations

from typing import Any

from transaction_learning import TransactionLearningStore, get_transaction_learning_store


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_learning_feedback(store: TransactionLearningStore | None = None) -> dict[str, Any]:
    """Evidence-based adjustments for rankings — fully explainable."""
    store = store or get_transaction_learning_store()
    records = store.all()
    adjustments: list[dict[str, Any]] = []

    source_hits: dict[str, dict[str, int]] = {}
    category_hits: dict[str, dict[str, int]] = {}
    pricing_errors: list[float] = []
    freight_errors: list[float] = []
    financing_unknown_ok = 0
    financing_incompatible = 0
    supplier_success = 0
    supplier_total = 0

    for r in records:
        src = (r.get("discovery") or {}).get("source") or "UNKNOWN"
        cat = (r.get("discovery") or {}).get("category") or "UNKNOWN"
        outcome = (r.get("outcome") or {}).get("status") or "IN_PROGRESS"
        source_hits.setdefault(src, {"pursued": 0, "won": 0, "lost": 0, "abandoned": 0})
        category_hits.setdefault(cat, {"pursued": 0, "won": 0, "lost": 0})
        source_hits[src]["pursued"] += 1
        category_hits[cat]["pursued"] += 1
        if outcome == "WON":
            source_hits[src]["won"] += 1
            category_hits[cat]["won"] += 1
        elif outcome == "LOST":
            source_hits[src]["lost"] += 1
            category_hits[cat]["lost"] += 1
        elif outcome == "ABANDONED":
            source_hits[src]["abandoned"] += 1

        pv = _num((r.get("pricing") or {}).get("variance"))
        if pv is not None:
            pricing_errors.append(abs(pv))
        fv = _num((r.get("freight") or {}).get("variance"))
        if fv is not None:
            freight_errors.append(abs(fv))

        for fv_rec in r.get("financing_verifications") or []:
            st = str(fv_rec.get("state") or "UNKNOWN").upper()
            if st == "UNKNOWN":
                financing_unknown_ok += 1
            elif st == "INCOMPATIBLE":
                financing_incompatible += 1

        for sv in r.get("supplier_verifications") or []:
            supplier_total += 1
            if sv.get("price") not in {None, "UNKNOWN", ""}:
                supplier_success += 1

    # Source ranking adjustments
    for src, stats in source_hits.items():
        delta = 0
        reasons = []
        if stats["won"] > 0:
            delta += 5 * stats["won"]
            reasons.append(f"{stats['won']} win(s)")
        if stats["lost"] > stats["won"]:
            delta -= 3 * (stats["lost"] - stats["won"])
            reasons.append(f"more losses than wins ({stats['lost']}>{stats['won']})")
        if stats["abandoned"] >= 2:
            delta -= 2
            reasons.append("repeated abandonments")
        if delta != 0:
            adjustments.append(
                {
                    "target": "source_ranking",
                    "key": src,
                    "delta": delta,
                    "evidence": reasons,
                    "stats": stats,
                }
            )

    # Product classification confidence
    for cat, stats in category_hits.items():
        if stats["pursued"] >= 1:
            conf = "LOW"
            if stats["won"] >= 1 and stats["lost"] == 0:
                conf = "HIGH"
            elif stats["won"] >= 1:
                conf = "MEDIUM"
            adjustments.append(
                {
                    "target": "product_classification",
                    "key": cat,
                    "confidence_label": conf,
                    "delta": 2 if conf == "HIGH" else (0 if conf == "MEDIUM" else -1),
                    "evidence": [f"pursued={stats['pursued']}", f"won={stats['won']}", f"lost={stats['lost']}"],
                }
            )

    # Pricing confidence
    if pricing_errors:
        avg_err = sum(pricing_errors) / len(pricing_errors)
        adjustments.append(
            {
                "target": "pricing_confidence",
                "key": "acquisition_cost",
                "avg_abs_variance": round(avg_err, 2),
                "delta": 3 if avg_err < 500 else (-2 if avg_err > 5000 else 0),
                "evidence": [f"n={len(pricing_errors)}", f"avg_abs_variance={round(avg_err, 2)}"],
            }
        )

    if freight_errors:
        avg_f = sum(freight_errors) / len(freight_errors)
        adjustments.append(
            {
                "target": "pricing_confidence",
                "key": "freight",
                "avg_abs_variance": round(avg_f, 2),
                "delta": 2 if avg_f < 200 else (-1 if avg_f > 2000 else 0),
                "evidence": [f"n={len(freight_errors)}", f"avg_abs_variance={round(avg_f, 2)}"],
            }
        )

    # Supplier confidence
    if supplier_total:
        rate = supplier_success / supplier_total
        adjustments.append(
            {
                "target": "supplier_confidence",
                "key": "quote_capture",
                "success_rate": round(rate, 3),
                "delta": 4 if rate >= 0.7 else (-2 if rate < 0.3 else 1),
                "evidence": [f"usable_quotes={supplier_success}/{supplier_total}"],
            }
        )

    # Financing-path confidence — UNKNOWN ≠ bad
    adjustments.append(
        {
            "target": "financing_path_confidence",
            "key": "unknown_vs_incompatible",
            "unknown_observations": financing_unknown_ok,
            "incompatible_observations": financing_incompatible,
            "delta": 0,
            "evidence": [
                "UNKNOWN financing preserved separately from INCOMPATIBLE",
                "missing information is not treated as rejection",
            ],
            "rule": "do_not_penalize_UNKNOWN",
        }
    )

    # Risk identification
    risks = []
    for r in records:
        for p in (r.get("execution") or {}).get("problems_encountered") or []:
            risks.append({"canonical_id": r.get("canonical_id"), "problem": p})
        for m in (r.get("execution") or {}).get("missing_information") or []:
            risks.append({"canonical_id": r.get("canonical_id"), "missing": m})

    return {
        "kind": "M3LearningFeedback",
        "method": "explainable_evidence_adjustments",
        "black_box_scoring": False,
        "records_analyzed": len(records),
        "adjustments": adjustments,
        "risk_signals": risks[:50],
        "pursuit_ranking_note": "Apply deltas as additive evidence modifiers on first-pursuit score",
        "source_stats": source_hits,
        "category_stats": category_hits,
    }


def apply_feedback_to_pursuit_score(
    base_score: int,
    *,
    source_id: str | None,
    category: str | None,
    feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply explainable deltas to a pursuit score."""
    feedback = feedback or compute_learning_feedback()
    delta = 0
    applied = []
    for adj in feedback.get("adjustments") or []:
        if adj.get("target") == "source_ranking" and adj.get("key") == source_id:
            delta += int(adj.get("delta") or 0)
            applied.append(adj)
        if adj.get("target") == "product_classification" and adj.get("key") == category:
            delta += int(adj.get("delta") or 0)
            applied.append(adj)
    return {
        "base_score": base_score,
        "learning_delta": delta,
        "adjusted_score": base_score + delta,
        "applied_adjustments": applied,
        "explainable": True,
    }


def first_five_contract_learning_report(store: TransactionLearningStore | None = None) -> dict[str, Any]:
    """Reporting for first five transactions — repeatable acquisition process metrics."""
    store = store or get_transaction_learning_store()
    records = store.all()[:5]
    rows = []
    for r in records:
        pricing = r.get("pricing") or {}
        freight = r.get("freight") or {}
        financing = r.get("financing") or {}
        outcome = r.get("outcome") or {}
        rows.append(
            {
                "record_id": r.get("record_id"),
                "canonical_id": r.get("canonical_id"),
                "source_quality": {
                    "source": (r.get("discovery") or {}).get("source"),
                    "platform": (r.get("discovery") or {}).get("platform"),
                },
                "qualification_accuracy": {
                    "why_pursued": (r.get("qualification") or {}).get("why_pursued"),
                    "why_rejected": (r.get("qualification") or {}).get("why_rejected"),
                },
                "pricing_accuracy": {
                    "estimated": pricing.get("estimated_acquisition_cost"),
                    "actual": pricing.get("actual_acquisition_cost"),
                    "variance": pricing.get("variance"),
                },
                "financing_accuracy": {
                    "path": financing.get("funding_path"),
                    "state": financing.get("state"),
                    "result": financing.get("result"),
                    "unknown_preserved": financing.get("unknown_is_not_rejection"),
                },
                "execution_issues": r.get("execution") or {},
                "actual_margin": {
                    "revenue": outcome.get("revenue"),
                    "actual_profit": outcome.get("actual_profit"),
                    "status": outcome.get("status"),
                },
            }
        )
    feedback = compute_learning_feedback(store)
    return {
        "kind": "M3FirstFiveContractLearning",
        "goal": "repeatable_acquisition_process",
        "transactions_recorded": len(records),
        "slots_remaining": max(0, 5 - len(records)),
        "transactions": rows,
        "learning_feedback": feedback,
    }
