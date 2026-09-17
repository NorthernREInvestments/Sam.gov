"""Transaction Execution + Capital Readiness — research/analysis only.

No bids, supplier contact, quotes, financing applications, or purchases.
Does NOT assume financing exists.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import SCORE_HIGH, SCORE_LOW, SCORE_MEDIUM

log = logging.getLogger("govtracker.m3_execution_intelligence")

EXEC_INDEX_KEY = "m3_execution_intelligence_v1"

CAP_LOW = "LOW_CAPITAL_NEED"
CAP_MODERATE = "MODERATE_CAPITAL_NEED"
CAP_HIGH = "HIGH_CAPITAL_NEED"

FIT_GOOD = "GOOD_FIT"
FIT_MODERATE = "MODERATE_FIT"
FIT_POOR = "POOR_FIT"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

BUCKET_A = "BUCKET_A_READY_TO_PURSUE"
BUCKET_B = "BUCKET_B_NEEDS_COMMERCIAL_WORK"
BUCKET_C = "BUCKET_C_STRATEGIC"
BUCKET_D = "BUCKET_D_NOT_CURRENTLY_SUITABLE"

SERVICE_RE = re.compile(
    r"\b(service|services|construction|installation|install|restoration|repair|assess and|consulting|janitorial)\b",
    re.I,
)
TANGIBLE_RE = re.compile(
    r"\b(switch|storage|laptop|server|valve|bearing|bushing|blade|equipment|supply|parts?|consumable|monitor|router)\b",
    re.I,
)
CUSTOM_RE = re.compile(r"\b(custom|fabricat|sole\s+source|classified|bespoke)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _si(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}


def _ci(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("commercial_intelligence") if isinstance(row.get("commercial_intelligence"), dict) else {}


def _comp(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("competitive_intelligence") if isinstance(row.get("competitive_intelligence"), dict) else {}


def _supplier_confidence(row: dict[str, Any]) -> str:
    si = _si(row)
    sc = (si.get("Supply_chain") or {}).get("supplier_confidence")
    if sc:
        return str(sc)
    return str(_ci(row).get("Supply_Confidence") or SCORE_LOW)


def _contract_value(row: dict[str, Any]) -> float | None:
    return _num(
        row.get("estimated_value")
        or row.get("government_revenue")
        or row.get("solicitation_value")
        or ((_comp(row).get("FINANCEABILITY") or {}).get("contract_size"))
    )


def _product_cost_status(row: dict[str, Any]) -> tuple[str, float | None]:
    si = _si(row)
    cost = si.get("cost_detail") or {}
    price = _num(cost.get("best_price"))
    level = str((si.get("Pricing_evidence") or {}).get("primary_level") or "")
    conf = si.get("ACQUISITION_COST_CONFIDENCE")
    if price is not None and ("LEVEL_1" in level or "LEVEL_2" in level or conf in {SCORE_HIGH, SCORE_MEDIUM}):
        return "KNOWN", price
    pricing = row.get("commercial_pricing") or {}
    alt = _num(pricing.get("verified_wholesale_unit") or pricing.get("lowest_public_new_unit"))
    if alt is not None:
        return "KNOWN", alt
    return "UNKNOWN", None


def build_transaction_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 1 — TRANSACTION_PROFILE."""
    title = str(row.get("title") or "")
    value = _contract_value(row)
    cost_status, cost = _product_cost_status(row)
    sc = _supplier_confidence(row)

    # Upfront capital: if cost known use it; else mark unknown (never invent)
    if cost_status == "KNOWN" and cost is not None:
        upfront = cost
        upfront_status = "ESTIMATED_FROM_COST_EVIDENCE"
    elif value is not None:
        upfront = None
        upfront_status = "UNKNOWN_COST_USE_CONTRACT_AS_CEILING_ONLY"
    else:
        upfront = None
        upfront_status = "UNKNOWN"

    if sc == SCORE_HIGH and cost_status == "KNOWN":
        pay_exp = "credit_terms_possible"
    elif sc in {SCORE_HIGH, SCORE_MEDIUM}:
        pay_exp = "unknown_terms_channels_exist"
    else:
        pay_exp = "unknown"

    gov_pay = "unknown"
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    if meta.get("payment_terms") or row.get("payment_terms"):
        gov_pay = "known"

    install = bool(SERVICE_RE.search(title)) or "install" in title.lower()
    shipping = "HIGH" if install or CUSTOM_RE.search(title) else ("LOW" if TANGIBLE_RE.search(title) else "MEDIUM")
    inventory = "REQUIRED_UNTIL_DELIVERY" if TANGIBLE_RE.search(title) and not install else (
        "CUSTOM_OR_SERVICE" if install or CUSTOM_RE.search(title) else "UNKNOWN"
    )

    gap = "UNKNOWN"
    if cost_status == "KNOWN" and cost is not None and value is not None:
        gap = round(max(0.0, cost - 0.0), 2)  # cash out before gov pays; receipt unknown timing
    elif value is not None and cost_status == "UNKNOWN":
        gap = "UNKNOWN_FULL_CONTRACT_MAY_NEED_BRIDGING"

    return {
        "kind": "TRANSACTION_PROFILE",
        "contract_value": value if value is not None else "UNKNOWN",
        "estimated_product_cost": cost if cost is not None else "UNKNOWN",
        "estimated_product_cost_status": cost_status,
        "required_upfront_capital": upfront if upfront is not None else "UNKNOWN",
        "upfront_capital_status": upfront_status,
        "supplier_payment_expectations": pay_exp,
        "government_payment_timing": gov_pay,
        "delivery_requirements": "installation_or_service" if install else "ship_and_deliver",
        "shipping_complexity": shipping,
        "inventory_requirements": inventory,
        "working_capital_gap": gap,
        "notes": ["financing_not_assumed", "do_not_invent_cost"],
    }


def build_capital_requirement(row: dict[str, Any], txn: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 — CAPITAL_REQUIREMENT_PROFILE."""
    title = str(row.get("title") or "")
    value = _num(txn.get("contract_value"))
    cost_status = txn.get("estimated_product_cost_status")
    reasons: list[str] = []

    if CUSTOM_RE.search(title) or (value is not None and value > 500_000):
        band = CAP_HIGH
        reasons.append("large_or_custom_inventory_risk")
    elif cost_status == "KNOWN" and value is not None and value <= 100_000 and TANGIBLE_RE.search(title):
        band = CAP_LOW
        reasons.append("common_product_manageable_size")
    elif _supplier_confidence(row) == SCORE_HIGH and TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        band = CAP_MODERATE if (value is None or value > 100_000) else CAP_LOW
        reasons.append("supplier_channels_exist_terms_unknown")
    elif SERVICE_RE.search(title) or value is not None and value > 250_000:
        band = CAP_HIGH if (value is not None and value > 250_000) else CAP_MODERATE
        reasons.append("service_or_elevated_size")
    else:
        band = CAP_MODERATE
        reasons.append("default_moderate_until_cost_verified")

    if cost_status == "UNKNOWN":
        reasons.append("product_cost_unknown_do_not_assume_financing")

    money_before = txn.get("required_upfront_capital")
    money_after = "UNKNOWN_GOVERNMENT_PAYMENT_TIMING"
    gap = txn.get("working_capital_gap")

    return {
        "kind": "CAPITAL_REQUIREMENT_PROFILE",
        "classification": band,
        "money_needed_before_delivery": money_before,
        "money_received_after_delivery": money_after,
        "estimated_cash_gap": gap,
        "reasons": reasons,
        "notes": ["financing_existence_not_assumed"],
    }


def build_financing_fit(row: dict[str, Any], txn: dict[str, Any], capital: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 — FINANCING_FIT_PROFILE."""
    title = str(row.get("title") or "")
    sc = _supplier_confidence(row)
    cost_status = txn.get("estimated_product_cost_status")
    structures: list[str] = []
    reasons: list[str] = []

    if sc in {SCORE_HIGH, SCORE_MEDIUM} and TANGIBLE_RE.search(title) and not CUSTOM_RE.search(title):
        structures.extend(["Purchase_Order_Financing", "Supplier_Terms", "Distributor_Credit"])
    if capital.get("classification") == CAP_LOW:
        structures.append("Internal_Cash")
    structures.append("Partner_Capital")
    if capital.get("classification") != CAP_HIGH:
        structures.append("Commercial_Lending")

    if CUSTOM_RE.search(title) or (sc == SCORE_LOW and cost_status == "UNKNOWN"):
        fit = FIT_POOR
        reasons.append("custom_or_uncertain_supplier_long_lead_risk")
    elif sc == SCORE_HIGH and TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        fit = FIT_GOOD
        reasons.append("government_buyer_standard_product_known_supplier_channels")
    elif sc == SCORE_MEDIUM or capital.get("classification") == CAP_MODERATE:
        fit = FIT_MODERATE
        reasons.append("partial_channel_clarity_terms_unverified")
    else:
        fit = FIT_MODERATE
        reasons.append("incomplete_data_moderate_default")

    # Competitive financeability boost/penalty
    fin = ((_comp(row).get("FINANCEABILITY") or {}).get("classification") or "")
    if "EASY" in str(fin) and fit != FIT_POOR:
        fit = FIT_GOOD
        reasons.append("competitive_layer_easy_to_finance")
    if "DIFFICULT" in str(fin):
        fit = FIT_POOR
        reasons.append("competitive_layer_difficult_finance")

    return {
        "kind": "FINANCING_FIT_PROFILE",
        "classification": fit,
        "possible_structures": structures[:6],
        "why": reasons,
        "notes": ["structures_are_candidates_not_commitments"],
    }


def build_supplier_execution(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 4 — SUPPLIER_EXECUTION_PROFILE."""
    si = _si(row)
    supply = si.get("Supply_chain") or {}
    channels = supply.get("all_channels") or supply.get("Public_distributors") or []
    n = len(channels)
    sc = _supplier_confidence(row)
    title = str(row.get("title") or "")
    reasons: list[str] = []

    risk = RISK_MEDIUM
    if sc == SCORE_HIGH and n >= 3 and not CUSTOM_RE.search(title):
        risk = RISK_LOW
        reasons.append("multiple_suppliers_stable_channels")
    elif sc == SCORE_LOW or n <= 1 or CUSTOM_RE.search(title):
        risk = RISK_HIGH
        reasons.append("limited_suppliers_or_custom_source")
    else:
        reasons.append("partial_supplier_map")

    approved = "UNKNOWN"
    if "DLA" in str(row.get("agency") or "").upper() or re.match(r"^\d{2}--", title.strip()):
        approved = "POSSIBLE_APPROVED_SOURCE_CONSTRAINTS"
        if risk == RISK_LOW:
            risk = RISK_MEDIUM
        reasons.append("dla_or_fsc_may_require_approved_source")

    return {
        "kind": "SUPPLIER_EXECUTION_PROFILE",
        "risk": risk,
        "supplier_availability": sc,
        "number_of_possible_suppliers": n,
        "product_substitution_options": "UNKNOWN",
        "lead_time": "UNKNOWN",
        "supply_chain_risk": risk,
        "approved_source_requirements": approved,
        "reasons": reasons,
    }


def build_delivery_complexity(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 5 — DELIVERY_COMPLEXITY_SCORE."""
    title = str(row.get("title") or "")
    score = 20
    reasons: list[str] = []
    if SERVICE_RE.search(title) or "install" in title.lower():
        score += 40
        reasons.append("installation_or_service_required")
    if CUSTOM_RE.search(title):
        score += 25
        reasons.append("custom_manufacturing")
    if TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        score = max(10, score - 10)
        reasons.append("standard_ship_and_deliver_signal")
    if any(x in title.lower() for x in ("inspect", "acceptance", "technical support")):
        score += 15
        reasons.append("inspection_or_support_burden")

    score = max(0, min(100, score))
    band = RISK_HIGH if score >= 60 else (RISK_MEDIUM if score >= 35 else RISK_LOW)
    return {"DELIVERY_COMPLEXITY_SCORE": score, "band": band, "reasons": reasons}


def build_contract_risk(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 6 — contract risk."""
    title = str(row.get("title") or "")
    chars: list[str] = []
    if SERVICE_RE.search(title):
        chars.append("services_mixed_or_primary")
    else:
        chars.append("product_only_signal")
    if "install" in title.lower():
        chars.append("installation_required")
    if CUSTOM_RE.search(title):
        chars.append("custom_manufacturing")
    awards = row.get("historical_awards") or []
    if isinstance(awards, list) and len(awards) >= 3:
        chars.append("recurring_purchase_signal")
    else:
        chars.append("one_time_or_unknown_frequency")

    if "custom_manufacturing" in chars or "installation_required" in chars:
        risk = RISK_HIGH
    elif "services_mixed_or_primary" in chars:
        risk = RISK_MEDIUM
    else:
        risk = RISK_LOW
    return {"classification": risk, "characteristics": chars}


def build_first_transaction_profile(
    row: dict[str, Any],
    *,
    capital: dict[str, Any],
    financing: dict[str, Any],
    delivery: dict[str, Any],
    contract_risk: dict[str, Any],
) -> dict[str, Any]:
    """Phase 7 — best first executed contract signals."""
    score = 50
    why: list[str] = []
    value = _contract_value(row)
    title = str(row.get("title") or "")

    if value is not None and 5_000 <= value <= 150_000:
        score += 15
        why.append("manageable_size")
    elif value is not None and value > 500_000:
        score -= 15
        why.append("too_large_for_first_transaction")

    if TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        score += 15
        why.append("understandable_product_simple_fulfillment")
    if financing.get("classification") == FIT_GOOD:
        score += 15
        why.append("financeable")
    elif financing.get("classification") == FIT_POOR:
        score -= 15
        why.append("poor_financing_fit")
    if capital.get("classification") == CAP_LOW:
        score += 10
        why.append("low_capital_need")
    elif capital.get("classification") == CAP_HIGH:
        score -= 10
        why.append("high_capital_need")
    if delivery.get("band") == RISK_LOW:
        score += 10
        why.append("simple_delivery")
    if contract_risk.get("classification") == RISK_HIGH:
        score -= 15
        why.append("high_contract_risk")

    # Never prioritize largest value alone — explicit note
    why.append("largest_contract_value_not_used_as_primary_rank")
    score = max(0, min(100, score))
    return {"FIRST_TRANSACTION_SCORE": score, "why": why}


def score_execution(
    row: dict[str, Any],
    *,
    capital: dict[str, Any],
    financing: dict[str, Any],
    supplier_exec: dict[str, Any],
    delivery: dict[str, Any],
    contract_risk: dict[str, Any],
) -> dict[str, Any]:
    """Phase 8 — EXECUTION_SCORE."""
    score = 40
    why: list[str] = []
    sc = _supplier_confidence(row)
    if sc == SCORE_HIGH:
        score += 15
        why.append("strong_supplier_confidence")
    elif sc == SCORE_LOW:
        score -= 15
        why.append("weak_supplier_confidence")

    if capital.get("classification") == CAP_LOW:
        score += 12
        why.append("low_capital_requirement")
    elif capital.get("classification") == CAP_HIGH:
        score -= 12
        why.append("high_capital_requirement")

    if financing.get("classification") == FIT_GOOD:
        score += 12
        why.append("good_financing_fit")
    elif financing.get("classification") == FIT_POOR:
        score -= 12
        why.append("poor_financing_fit")

    if delivery.get("band") == RISK_LOW:
        score += 10
        why.append("low_delivery_complexity")
    elif delivery.get("band") == RISK_HIGH:
        score -= 12
        why.append("high_delivery_complexity")

    if contract_risk.get("classification") == RISK_LOW:
        score += 8
        why.append("low_contract_risk")
    elif contract_risk.get("classification") == RISK_HIGH:
        score -= 12
        why.append("high_contract_risk")

    if supplier_exec.get("risk") == RISK_LOW:
        score += 8
        why.append("low_supplier_execution_risk")
    elif supplier_exec.get("risk") == RISK_HIGH:
        score -= 10
        why.append("high_supplier_execution_risk")

    # Competition from competitive layer — context only
    comp_prof = ((_comp(row).get("COMPETITION_PROFILE") or {}).get("profile") or "")
    if comp_prof == "LOW_CONCERN":
        score += 5
        why.append("competition_low_concern")
    elif comp_prof == "HIGH":
        score -= 5
        why.append("incumbent_heavy_competition")

    # Profit potential — commercial band only, never invent margin
    band = _ci(row).get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score")
    if band == SCORE_HIGH:
        score += 6
        why.append("commercial_high_signal")
    elif band == SCORE_MEDIUM:
        score += 3
        why.append("commercial_medium_signal")

    score = max(0, min(100, score))
    out = SCORE_HIGH if score >= 65 else (SCORE_MEDIUM if score >= 40 else SCORE_LOW)
    return {"EXECUTION_SCORE": score, "band": out, "why": why}


def assign_execution_bucket(
    *,
    execution: dict[str, Any],
    financing: dict[str, Any],
    capital: dict[str, Any],
    txn: dict[str, Any],
    row: dict[str, Any],
) -> str:
    """Phase 9 — reclassification buckets."""
    band = execution.get("band")
    sc = _supplier_confidence(row)
    cost_unknown = txn.get("estimated_product_cost_status") == "UNKNOWN"
    value = _num(txn.get("contract_value"))

    if band == SCORE_HIGH and financing.get("classification") in {FIT_GOOD, FIT_MODERATE} and sc in {
        SCORE_HIGH,
        SCORE_MEDIUM,
    }:
        return BUCKET_A
    if band in {SCORE_HIGH, SCORE_MEDIUM} and cost_unknown:
        return BUCKET_B
    if band == SCORE_MEDIUM and (value is not None and value > 250_000):
        return BUCKET_C
    if band == SCORE_LOW or financing.get("classification") == FIT_POOR or capital.get("classification") == CAP_HIGH:
        if value is not None and value > 500_000 and sc == SCORE_HIGH:
            return BUCKET_C
        return BUCKET_D
    if band == SCORE_MEDIUM:
        return BUCKET_B
    return BUCKET_D


def recommended_path(bucket: str, execution: dict[str, Any], txn: dict[str, Any]) -> str:
    if bucket == BUCKET_A:
        return "Good first transaction candidate"
    if bucket == BUCKET_B or txn.get("estimated_product_cost_status") == "UNKNOWN":
        return "Needs supplier verification"
    if bucket == BUCKET_C:
        return "High upside but capital intensive"
    return "Not currently suitable — excessive execution risk"


def action_priority_score(row: dict[str, Any], execution: dict[str, Any], first_txn: dict[str, Any]) -> dict[str, Any]:
    """Phase 11 — what Brian should investigate next."""
    first_deal = int(((_comp(row).get("FIRST_DEAL") or {}).get("FIRST_DEAL_SCORE") or 0))
    exec_score = int(execution.get("EXECUTION_SCORE") or 0)
    commercial = _ci(row).get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score")
    commercial_n = 70 if commercial == SCORE_HIGH else (50 if commercial == SCORE_MEDIUM else 20)
    strategic = 20
    value = _contract_value(row)
    if value is not None and value > 250_000:
        strategic += 15
    if ((_comp(row).get("NEW_ENTRANT_ADVANTAGE") or {}).get("band") == SCORE_HIGH):
        strategic += 15

    total = int(round(0.35 * first_deal + 0.35 * exec_score + 0.20 * commercial_n + 0.10 * strategic))
    total = max(0, min(100, total))
    return {
        "ACTION_PRIORITY_SCORE": total,
        "components": {
            "first_deal": first_deal,
            "execution": exec_score,
            "commercial": commercial_n,
            "strategic": strategic,
            "first_transaction": first_txn.get("FIRST_TRANSACTION_SCORE"),
        },
        "question": "What should Brian investigate next?",
    }


def build_execution_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    txn = build_transaction_profile(row)
    capital = build_capital_requirement(row, txn)
    financing = build_financing_fit(row, txn, capital)
    supplier_exec = build_supplier_execution(row)
    delivery = build_delivery_complexity(row)
    contract_risk = build_contract_risk(row)
    first_txn = build_first_transaction_profile(
        row, capital=capital, financing=financing, delivery=delivery, contract_risk=contract_risk
    )
    execution = score_execution(
        row,
        capital=capital,
        financing=financing,
        supplier_exec=supplier_exec,
        delivery=delivery,
        contract_risk=contract_risk,
    )
    bucket = assign_execution_bucket(
        execution=execution, financing=financing, capital=capital, txn=txn, row=row
    )
    path = recommended_path(bucket, execution, txn)
    action = action_priority_score(row, execution, first_txn)

    return {
        "kind": "M3ExecutionIntelligence",
        "generated_at": _utc(),
        "TRANSACTION": txn,
        "CAPITAL_REQUIREMENT": capital,
        "FINANCING_FIT": financing,
        "SUPPLIER_EXECUTION": supplier_exec,
        "DELIVERY": delivery,
        "CONTRACT_RISK": contract_risk,
        "FIRST_TRANSACTION": first_txn,
        "EXECUTION": execution,
        "BUCKET": bucket,
        "Recommended_Path": path,
        "ACTION_PRIORITY": action,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def load_execution_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == EXEC_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("load execution index failed")
        return {}


def save_execution_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {"kind": "M3ExecutionIntelligenceIndex", "updated_at": _utc(), "by_id": by_id, "count": len(by_id)}
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == EXEC_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=EXEC_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save execution index failed")
        return False


def get_persisted_execution(canonical_id: str) -> dict[str, Any] | None:
    data = load_execution_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None


def analyze_execution_top_opportunities(store: Any, *, limit: int = 25) -> dict[str, Any]:
    """TOP N local execution scoring — no paid research."""
    rows = store.all() if hasattr(store, "all") else list(store)
    by_id = {r["canonical_id"]: r for r in rows if r.get("canonical_id")}

    seed_ids: list[str] = []
    try:
        from m3_competitive_intelligence import load_competitive_index, get_persisted_competitive

        cidx = load_competitive_index()
        cby = cidx.get("by_id") if isinstance(cidx.get("by_id"), dict) else {}
        ranked = sorted(
            cby.items(),
            key=lambda kv: -int(((kv[1] or {}).get("FIRST_DEAL") or {}).get("FIRST_DEAL_SCORE") or 0),
        )
        seed_ids = [cid for cid, _ in ranked]
    except Exception:
        get_persisted_competitive = None  # type: ignore

    try:
        from m3_supplier_intelligence import get_persisted_supplier_intelligence, build_supplier_research_queue

        sq = build_supplier_research_queue(rows, limit=limit)
        seed_ids = [i["canonical_id"] for i in sq.get("queue") or []] + seed_ids
    except Exception:
        get_persisted_supplier_intelligence = None  # type: ignore

    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for cid in seed_ids:
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        targets.append(by_id[cid])
        if len(targets) >= limit:
            break
    for r in rows:
        if len(targets) >= limit:
            break
        cid = r.get("canonical_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        targets.append(r)

    results = []
    index_updates: dict[str, Any] = {}
    buckets: Counter[str] = Counter()

    for row in targets:
        # Attach persisted layers
        if not row.get("supplier_intelligence") and get_persisted_supplier_intelligence:
            try:
                psi = get_persisted_supplier_intelligence(str(row.get("canonical_id") or ""))
                if psi:
                    row = {**row, "supplier_intelligence": psi}
            except Exception:
                pass
        if not row.get("competitive_intelligence") and get_persisted_competitive:
            try:
                pci = get_persisted_competitive(str(row.get("canonical_id") or ""))
                if pci:
                    row = {**row, "competitive_intelligence": pci}
            except Exception:
                pass

        pkg = build_execution_intelligence(row)
        index_updates[row["canonical_id"]] = pkg
        full = {**(store.get(row["canonical_id"]) or row)}
        full["execution_intelligence"] = pkg
        store._rows[row["canonical_id"]] = full
        buckets[pkg["BUCKET"]] += 1

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Contract_value": pkg["TRANSACTION"]["contract_value"],
                "Product": ((_si(row).get("Product") or {}).get("Technical_description") or row.get("title")),
                "Supplier_confidence": _supplier_confidence(row),
                "Capital_requirement": pkg["CAPITAL_REQUIREMENT"]["classification"],
                "Financing_fit": pkg["FINANCING_FIT"]["classification"],
                "Execution_risk": pkg["SUPPLIER_EXECUTION"]["risk"],
                "Delivery_risk": pkg["DELIVERY"]["band"],
                "Execution_score": pkg["EXECUTION"]["EXECUTION_SCORE"],
                "Execution_band": pkg["EXECUTION"]["band"],
                "Profit_potential": _ci(row).get("COMMERCIAL_OPPORTUNITY_SCORE")
                or row.get("commercial_opportunity_score")
                or "UNKNOWN",
                "BUCKET": pkg["BUCKET"],
                "Recommended_Path": pkg["Recommended_Path"],
                "ACTION_PRIORITY_SCORE": pkg["ACTION_PRIORITY"]["ACTION_PRIORITY_SCORE"],
                "Why": pkg["EXECUTION"]["why"][:6],
                "CATEGORY": (_comp(row).get("CATEGORY") or row.get("product_category") or "UNKNOWN"),
            }
        )

    results.sort(
        key=lambda r: (
            -int(r.get("ACTION_PRIORITY_SCORE") or 0),
            -int(r.get("Execution_score") or 0),
        )
    )

    try:
        existing = load_execution_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_execution_index(by)
    except Exception:
        log.exception("execution index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    top = [r for r in results if r.get("BUCKET") == BUCKET_A][:10]
    if len(top) < 10:
        for r in results:
            if r in top:
                continue
            top.append(r)
            if len(top) >= 10:
                break

    fin_rank = sorted(
        results,
        key=lambda r: (
            0 if r.get("Financing_fit") == FIT_GOOD else (1 if r.get("Financing_fit") == FIT_MODERATE else 2),
            -int(r.get("Execution_score") or 0),
        ),
    )

    # Category insights
    cat_map: dict[str, list[int]] = {}
    for r in results:
        cat_map.setdefault(str(r.get("CATEGORY") or "UNKNOWN"), []).append(int(r.get("Execution_score") or 0))
    cat_ranked = sorted(
        (
            {"category": c, "avg_execution": round(sum(v) / len(v), 1), "count": len(v)}
            for c, v in cat_map.items()
        ),
        key=lambda x: -x["avg_execution"],
    )

    return {
        "kind": "M3ExecutionAnalysisRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "buckets": {
            "READY_TO_PURSUE": buckets.get(BUCKET_A, 0),
            "NEEDS_COMMERCIAL_WORK": buckets.get(BUCKET_B, 0),
            "STRATEGIC": buckets.get(BUCKET_C, 0),
            "NOT_CURRENTLY_SUITABLE": buckets.get(BUCKET_D, 0),
        },
        "TOP_FIRST_TRANSACTIONS": top,
        "ALL_SCORED": results,
        "MOST_FINANCEABLE": fin_rank[:5],
        "MOST_DIFFICULT_FINANCE": list(reversed(fin_rank[-5:])) if fin_rank else [],
        "CATEGORY_INSIGHTS": {"best": cat_ranked[:5], "highest_risk": list(reversed(cat_ranked[-5:])) if cat_ranked else []},
        "OpenAI": 0,
        "paid": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_execution_section(row: dict[str, Any]) -> dict[str, Any]:
    ei = row.get("execution_intelligence")
    if not isinstance(ei, dict) or ei.get("kind") != "M3ExecutionIntelligence":
        persisted = get_persisted_execution(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3ExecutionIntelligence":
            ei = persisted
        else:
            ei = build_execution_intelligence(row)
    txn = ei.get("TRANSACTION") or {}
    return {
        "kind": "M3DealRoomExecutionIntelligence",
        "Contract_Value": txn.get("contract_value"),
        "Estimated_Capital_Needed": txn.get("required_upfront_capital"),
        "Capital_Need_Band": (ei.get("CAPITAL_REQUIREMENT") or {}).get("classification"),
        "Financing_Fit": (ei.get("FINANCING_FIT") or {}).get("classification"),
        "Supplier_Risk": (ei.get("SUPPLIER_EXECUTION") or {}).get("risk"),
        "Delivery_Risk": (ei.get("DELIVERY") or {}).get("band"),
        "Execution_Score": (ei.get("EXECUTION") or {}).get("EXECUTION_SCORE"),
        "Execution_Band": (ei.get("EXECUTION") or {}).get("band"),
        "BUCKET": ei.get("BUCKET"),
        "Recommended_Path": ei.get("Recommended_Path"),
        "ACTION_PRIORITY_SCORE": (ei.get("ACTION_PRIORITY") or {}).get("ACTION_PRIORITY_SCORE"),
        "Why": (ei.get("EXECUTION") or {}).get("why") or [],
        "full": ei,
    }
