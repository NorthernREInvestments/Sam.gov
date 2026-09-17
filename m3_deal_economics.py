"""Dynamic Deal Economics + Profit Target Status — research/analysis only.

Never invents acquisition costs or margins.
UNKNOWN pricing stays visible and is not auto-rejected from rankings.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from application_clock import now_utc
from economic_integrity import min_actual_profit_usd
from m3_commercial_engine import (
    PRICE_LEVEL_1_ACTUAL,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_3_COMPARABLE,
    PRICE_LEVEL_4_UNKNOWN,
    SCORE_HIGH,
    SCORE_LOW,
    SCORE_MEDIUM,
)

log = logging.getLogger("govtracker.m3_deal_economics")

ECONOMICS_INDEX_KEY = "m3_deal_economics_v1"

STATUS_UNKNOWN = "UNKNOWN"
STATUS_EXCEEDS = "EXCEEDS_TARGET"
STATUS_MEETS = "MEETS_TARGET"
STATUS_WITHIN = "WITHIN_ACCEPTABLE_RANGE"
STATUS_BELOW = "BELOW_TARGET"
STATUS_UNVIABLE = "UNVIABLE"

CONF_HIGH = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW = "LOW"
CONF_UNKNOWN = "UNKNOWN"


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


def _exec(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("execution_intelligence") if isinstance(row.get("execution_intelligence"), dict) else {}


def _comp(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("competitive_intelligence") if isinstance(row.get("competitive_intelligence"), dict) else {}


def target_profit_usd(row: dict[str, Any] | None = None) -> float:
    """Operator/env profit target — default economic integrity floor."""
    if row:
        override = _num((row.get("operator_economics") or {}).get("target_profit_usd"))
        if override is not None and override >= 0:
            return float(override)
    return float(min_actual_profit_usd())


def acceptable_profit_floor(target: float) -> float:
    """Strategically reasonable band starts at 60% of target (still positive)."""
    return max(0.0, target * 0.6)


def resolve_revenue(row: dict[str, Any]) -> float | None:
    return _num(
        row.get("estimated_value")
        or row.get("government_revenue")
        or row.get("solicitation_value")
        or ((_exec(row).get("TRANSACTION") or {}).get("contract_value"))
    )


def resolve_quantity(row: dict[str, Any]) -> float | None:
    si = _si(row)
    q = _num((si.get("Product") or {}).get("quantity"))
    if q is not None:
        return q
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            qq = _num(li.get("quantity"))
            if qq is not None:
                return qq
    ident = _ci(row).get("IDENTIFICATION") or {}
    return _num(ident.get("quantity"))


def resolve_pricing_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Pull current acquisition cost only from evidence — never invent."""
    si = _si(row)
    pricing = si.get("Pricing_evidence") or {}
    level = str(pricing.get("primary_level") or PRICE_LEVEL_4_UNKNOWN)
    items = pricing.get("items") if isinstance(pricing.get("items"), list) else []
    cost = _num((si.get("cost_detail") or {}).get("best_price"))
    if cost is None and items:
        for pref in (PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE):
            cands = [e for e in items if isinstance(e, dict) and e.get("level") == pref and _num(e.get("amount")) is not None]
            if cands:
                cost = min(_num(e["amount"]) for e in cands)  # type: ignore[type-var]
                level = pref
                break

    # Row-level commercial pricing fallbacks
    cp = row.get("commercial_pricing") or row.get("public_pricing") or {}
    if cost is None:
        cost = _num(cp.get("verified_wholesale_unit") or row.get("supplier_unit_cost"))
        if cost is not None:
            level = PRICE_LEVEL_1_ACTUAL
    if cost is None:
        cost = _num(cp.get("lowest_public_new_unit") or cp.get("public_unit_price"))
        if cost is not None:
            level = PRICE_LEVEL_2_PUBLIC
    if cost is None:
        cost = _num(cp.get("comparable_unit") or cp.get("msrp"))
        if cost is not None:
            level = PRICE_LEVEL_3_COMPARABLE

    if cost is None or "LEVEL_4" in level or level == PRICE_LEVEL_4_UNKNOWN:
        return {
            "current_acquisition_cost": None,
            "pricing_evidence_level": PRICE_LEVEL_4_UNKNOWN,
            "PRICE_CONFIDENCE": CONF_UNKNOWN,
            "source": None,
        }

    if level == PRICE_LEVEL_1_ACTUAL:
        conf = CONF_HIGH
    elif level == PRICE_LEVEL_2_PUBLIC:
        conf = CONF_MEDIUM
    elif level == PRICE_LEVEL_3_COMPARABLE:
        conf = CONF_LOW
    else:
        conf = CONF_UNKNOWN

    source = None
    if items:
        for e in items:
            if isinstance(e, dict) and _num(e.get("amount")) == cost:
                source = e.get("Source")
                break

    return {
        "current_acquisition_cost": float(cost),
        "pricing_evidence_level": level,
        "PRICE_CONFIDENCE": conf,
        "source": source,
    }


def classify_profit_target_status(
    *,
    projected_profit: float | None,
    target: float,
    has_cost: bool,
) -> str:
    """Phase 1 — PROFIT_TARGET_STATUS."""
    if not has_cost or projected_profit is None:
        return STATUS_UNKNOWN
    if projected_profit <= 0:
        return STATUS_UNVIABLE
    if projected_profit >= target * 1.5:
        return STATUS_EXCEEDS
    if projected_profit >= target:
        return STATUS_MEETS
    if projected_profit >= acceptable_profit_floor(target):
        return STATUS_WITHIN
    return STATUS_BELOW


def build_deal_economics_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Phases 2–5 — DEAL_ECONOMICS_PROFILE + gap + adjusted scenarios."""
    revenue = resolve_revenue(row)
    qty = resolve_quantity(row)
    target = target_profit_usd(row)
    pricing = resolve_pricing_evidence(row)
    current_cost = pricing["current_acquisition_cost"]
    has_cost = current_cost is not None

    # Target acquisition: revenue - target profit - known fees (fees unknown → 0, noted)
    known_fees = _num((row.get("operator_economics") or {}).get("known_fees_usd")) or 0.0
    target_acq = None
    if revenue is not None:
        target_acq = round(revenue - target - known_fees, 2)

    # If costs are unit prices and qty known, scale for total comparison
    cost_basis = "total_or_unknown"
    current_total = current_cost
    target_unit = None
    current_unit = current_cost
    if has_cost and qty is not None and qty > 1 and current_cost is not None and current_cost < 50_000:
        # Heuristic: small unit prices with qty → treat as unit
        current_total = round(current_cost * qty, 2)
        cost_basis = "unit_x_quantity"
        current_unit = current_cost
        if target_acq is not None:
            target_unit = round(target_acq / qty, 2)
    elif has_cost:
        cost_basis = "as_stated_total_or_single"
        if target_acq is not None and qty is not None and qty > 0:
            target_unit = round(target_acq / qty, 2)

    projected_profit = None
    projected_margin = None
    if revenue is not None and current_total is not None:
        projected_profit = round(revenue - current_total - known_fees, 2)
        projected_margin = round((projected_profit / revenue) * 100, 2) if revenue else None

    status = classify_profit_target_status(
        projected_profit=projected_profit, target=target, has_cost=has_cost
    )

    # Target acquisition gap
    gap = None
    gap_pct = None
    if target_acq is not None and current_total is not None:
        gap = round(current_total - target_acq, 2)  # positive = over target cost
        if current_total > 0:
            gap_pct = round((gap / current_total) * 100, 2)

    # Original target scenario vs current
    original = {
        "revenue": revenue if revenue is not None else "UNKNOWN",
        "cost": target_acq if target_acq is not None else "UNKNOWN",
        "profit": target if revenue is not None else "UNKNOWN",
    }
    current_scenario = {
        "revenue": revenue if revenue is not None else "UNKNOWN",
        "cost": current_total if current_total is not None else "UNKNOWN",
        "profit": projected_profit if projected_profit is not None else "UNKNOWN",
    }
    profit_delta = None
    if isinstance(original["profit"], (int, float)) and projected_profit is not None:
        profit_delta = round(projected_profit - float(original["profit"]), 2)

    next_action = "Find supplier pricing" if not has_cost else (
        "Negotiate toward target acquisition cost" if status in {STATUS_BELOW, STATUS_WITHIN, STATUS_UNVIABLE}
        else "Verify supplier terms and proceed evaluation"
    )

    return {
        "kind": "DEAL_ECONOMICS_PROFILE",
        "generated_at": _utc(),
        "Revenue": revenue if revenue is not None else "UNKNOWN",
        "Quantity": qty if qty is not None else "UNKNOWN",
        "Target_profit": target,
        "Target_acquisition_cost": target_acq if target_acq is not None else "UNKNOWN",
        "Target_acquisition_unit": target_unit if target_unit is not None else "UNKNOWN",
        "Current_acquisition_cost": current_total if current_total is not None else "UNKNOWN",
        "Current_acquisition_unit": current_unit if current_unit is not None else "UNKNOWN",
        "Pricing_evidence_level": pricing["pricing_evidence_level"],
        "PRICE_CONFIDENCE": pricing["PRICE_CONFIDENCE"],
        "pricing_source": pricing.get("source") or "UNKNOWN",
        "Projected_profit": projected_profit if projected_profit is not None else "UNKNOWN",
        "Projected_margin_pct": projected_margin if projected_margin is not None else "UNKNOWN",
        "Profit_variance_vs_target": profit_delta if profit_delta is not None else "UNKNOWN",
        "PROFIT_TARGET_STATUS": status,
        "Target_acquisition_gap": {
            "required_acquisition_cost": target_acq if target_acq is not None else "UNKNOWN",
            "current_cost": current_total if current_total is not None else "UNKNOWN",
            "difference": gap if gap is not None else "UNKNOWN",
            "required_improvement_pct": gap_pct if gap_pct is not None else "UNKNOWN",
            "notes": ["positive_difference_means_current_cost_above_target"],
        },
        "Adjusted_scenarios": {
            "original_target": original,
            "current_scenario": current_scenario,
            "profit_lost_or_gained": profit_delta if profit_delta is not None else "UNKNOWN",
        },
        "cost_basis": cost_basis,
        "known_fees": known_fees,
        "known_fees_note": "fees_unknown_not_invented" if known_fees == 0 else "operator_known_fees_applied",
        "Next_Action": next_action,
        "notes": [
            "do_not_invent_costs",
            "unknown_pricing_not_auto_rejected",
            "financing_not_assumed",
        ],
    }


def apply_operator_override(profile: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Phase 9 — strategic override notes; never hide actual economics."""
    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    notes = op.get("strategic_notes") or op.get("notes")
    strategic_value = op.get("strategic_value")  # High / Medium / Low
    accept_below = bool(op.get("accept_below_target"))
    out = dict(profile)
    out["operator_override"] = {
        "strategic_value": strategic_value or "UNKNOWN",
        "accept_below_target": accept_below,
        "notes": notes or None,
        "economics_still_shown": True,
    }
    # Flag strategic value when below/within target — do not change PROFIT_TARGET_STATUS
    if accept_below and profile.get("PROFIT_TARGET_STATUS") in {STATUS_BELOW, STATUS_WITHIN}:
        out["strategic_context"] = "below_or_within_target_but_operator_marked_strategic"
        out["Next_Action"] = "Operator accepts below-ideal economics — verify supply then pursue carefully"
    elif profile.get("PROFIT_TARGET_STATUS") == STATUS_UNKNOWN:
        out["strategic_context"] = None
    return out


def economics_priority_score(row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Phase 8 — combined ranking; not by contract value alone; UNKNOWN not dumped."""
    score = 0
    why: list[str] = []

    exec_score = int(((_exec(row).get("EXECUTION") or {}).get("EXECUTION_SCORE") or 0))
    score += min(25, exec_score // 4)
    if exec_score:
        why.append("execution_score_weighted")

    first_deal = int(((_comp(row).get("FIRST_DEAL") or {}).get("FIRST_DEAL_SCORE") or 0))
    score += min(20, first_deal // 5)
    if first_deal:
        why.append("first_deal_weighted")

    sc = str(
        ((_si(row).get("Supply_chain") or {}).get("supplier_confidence"))
        or _ci(row).get("Supply_Confidence")
        or SCORE_LOW
    )
    if sc == SCORE_HIGH:
        score += 15
        why.append("supplier_confidence_high")
    elif sc == SCORE_MEDIUM:
        score += 8
        why.append("supplier_confidence_medium")

    conf = profile.get("PRICE_CONFIDENCE")
    if conf == CONF_HIGH:
        score += 15
        why.append("pricing_confidence_high")
    elif conf == CONF_MEDIUM:
        score += 10
        why.append("pricing_confidence_medium")
    elif conf == CONF_LOW:
        score += 4
        why.append("pricing_confidence_low")
    else:
        score += 6  # keep unknowns visible in mid ranks
        why.append("unknown_pricing_kept_visible")

    status = profile.get("PROFIT_TARGET_STATUS")
    if status == STATUS_EXCEEDS:
        score += 15
        why.append("exceeds_profit_target")
    elif status == STATUS_MEETS:
        score += 12
        why.append("meets_profit_target")
    elif status == STATUS_WITHIN:
        score += 8
        why.append("within_acceptable_profit_range")
    elif status == STATUS_BELOW:
        score += 3
        why.append("below_target_still_positive")
    elif status == STATUS_UNVIABLE:
        score -= 20
        why.append("unviable_economics")
    else:
        why.append("profit_status_unknown_not_penalized_hard")

    # Commercial signal
    band = _ci(row).get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score")
    if band == SCORE_HIGH:
        score += 8
    elif band == SCORE_MEDIUM:
        score += 4

    # Competition low concern slight boost
    if ((_comp(row).get("COMPETITION_PROFILE") or {}).get("profile") == "LOW_CONCERN"):
        score += 4

    score = max(0, min(100, score))
    return {"ECONOMICS_PRIORITY_SCORE": score, "why": why}


def build_deal_economics(row: dict[str, Any]) -> dict[str, Any]:
    profile = build_deal_economics_profile(row)
    profile = apply_operator_override(profile, row)
    priority = economics_priority_score(row, profile)
    return {
        "kind": "M3DealEconomics",
        "generated_at": _utc(),
        "DEAL_ECONOMICS_PROFILE": profile,
        "PROFIT_TARGET_STATUS": profile.get("PROFIT_TARGET_STATUS"),
        "PRICE_CONFIDENCE": profile.get("PRICE_CONFIDENCE"),
        "ECONOMICS_PRIORITY": priority,
        "Next_Action": profile.get("Next_Action"),
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def load_economics_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == ECONOMICS_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("load economics index failed")
        return {}


def save_economics_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {"kind": "M3DealEconomicsIndex", "updated_at": _utc(), "by_id": by_id, "count": len(by_id)}
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == ECONOMICS_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=ECONOMICS_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save economics index failed")
        return False


def get_persisted_economics(canonical_id: str) -> dict[str, Any] | None:
    data = load_economics_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None


def analyze_deal_economics_top(store: Any, *, limit: int = 25) -> dict[str, Any]:
    """Score TOP N — local only, no paid research."""
    rows = store.all() if hasattr(store, "all") else list(store)
    by_id = {r["canonical_id"]: r for r in rows if r.get("canonical_id")}

    seed: list[str] = []
    try:
        from m3_execution_intelligence import load_execution_index

        eidx = load_execution_index()
        eby = eidx.get("by_id") if isinstance(eidx.get("by_id"), dict) else {}
        ranked = sorted(
            eby.items(),
            key=lambda kv: -int(((kv[1] or {}).get("ACTION_PRIORITY") or {}).get("ACTION_PRIORITY_SCORE") or 0),
        )
        seed.extend(cid for cid, _ in ranked)
    except Exception:
        pass
    get_psi = get_pci = get_pei = None
    try:
        from m3_supplier_intelligence import get_persisted_supplier_intelligence as get_psi
        from m3_supplier_intelligence import build_supplier_research_queue
        from m3_competitive_intelligence import get_persisted_competitive as get_pci
        from m3_execution_intelligence import get_persisted_execution as get_pei

        sq = build_supplier_research_queue(rows, limit=limit)
        seed = [i["canonical_id"] for i in sq.get("queue") or []] + seed
    except Exception:
        pass

    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for cid in seed:
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
        if cid and cid not in seen:
            seen.add(cid)
            targets.append(r)

    results = []
    index_updates: dict[str, Any] = {}
    status_counts: Counter[str] = Counter()
    known = 0
    unknown = 0

    for row in targets:
        if not row.get("supplier_intelligence") and get_psi:
            try:
                psi = get_psi(str(row.get("canonical_id") or ""))
                if psi:
                    row = {**row, "supplier_intelligence": psi}
            except Exception:
                pass
        if not row.get("competitive_intelligence") and get_pci:
            try:
                pci = get_pci(str(row.get("canonical_id") or ""))
                if pci:
                    row = {**row, "competitive_intelligence": pci}
            except Exception:
                pass
        if not row.get("execution_intelligence") and get_pei:
            try:
                pei = get_pei(str(row.get("canonical_id") or ""))
                if pei:
                    row = {**row, "execution_intelligence": pei}
            except Exception:
                pass

        pkg = build_deal_economics(row)
        index_updates[row["canonical_id"]] = pkg
        full = {**(store.get(row["canonical_id"]) or row)}
        full["deal_economics"] = pkg
        store._rows[row["canonical_id"]] = full

        prof = pkg["DEAL_ECONOMICS_PROFILE"]
        st = pkg["PROFIT_TARGET_STATUS"]
        status_counts[st] += 1
        if st == STATUS_UNKNOWN:
            unknown += 1
        else:
            known += 1

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Revenue": prof.get("Revenue"),
                "Target_cost": prof.get("Target_acquisition_cost"),
                "Current_cost": prof.get("Current_acquisition_cost"),
                "Projected_profit": prof.get("Projected_profit"),
                "Status": st,
                "Confidence": prof.get("PRICE_CONFIDENCE"),
                "Next_action": pkg.get("Next_Action"),
                "ECONOMICS_PRIORITY_SCORE": pkg["ECONOMICS_PRIORITY"]["ECONOMICS_PRIORITY_SCORE"],
                "gap": (prof.get("Target_acquisition_gap") or {}).get("difference"),
                "improvement_pct": (prof.get("Target_acquisition_gap") or {}).get("required_improvement_pct"),
            }
        )

    results.sort(key=lambda r: (-int(r.get("ECONOMICS_PRIORITY_SCORE") or 0), str(r.get("Opportunity") or "")))

    try:
        existing = load_economics_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_economics_index(by)
    except Exception:
        log.exception("economics index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    return {
        "kind": "M3DealEconomicsRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "known_pricing": known,
        "unknown_pricing": unknown,
        "profit_statuses": {
            "EXCEEDS": status_counts.get(STATUS_EXCEEDS, 0),
            "MEETS": status_counts.get(STATUS_MEETS, 0),
            "WITHIN_RANGE": status_counts.get(STATUS_WITHIN, 0),
            "BELOW": status_counts.get(STATUS_BELOW, 0),
            "UNVIABLE": status_counts.get(STATUS_UNVIABLE, 0),
            "UNKNOWN": status_counts.get(STATUS_UNKNOWN, 0),
        },
        "TOP_DEALS": results[:10],
        "ALL_SCORED": results,
        "OpenAI": 0,
        "paid": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_economics_section(row: dict[str, Any]) -> dict[str, Any]:
    de = row.get("deal_economics")
    if not isinstance(de, dict) or de.get("kind") != "M3DealEconomics":
        persisted = get_persisted_economics(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3DealEconomics":
            de = persisted
        else:
            de = build_deal_economics(row)
    prof = de.get("DEAL_ECONOMICS_PROFILE") or {}
    return {
        "kind": "M3DealRoomDealEconomics",
        "Contract_Value": prof.get("Revenue"),
        "Target_Profit": prof.get("Target_profit"),
        "Required_Acquisition_Cost": prof.get("Target_acquisition_cost"),
        "Current_Pricing_Evidence": {
            "cost": prof.get("Current_acquisition_cost"),
            "level": prof.get("Pricing_evidence_level"),
            "source": prof.get("pricing_source"),
        },
        "Projected_Profit": prof.get("Projected_profit"),
        "Profit_Target_Status": de.get("PROFIT_TARGET_STATUS"),
        "Pricing_Confidence": de.get("PRICE_CONFIDENCE"),
        "Target_Acquisition_Gap": prof.get("Target_acquisition_gap"),
        "Adjusted_Scenarios": prof.get("Adjusted_scenarios"),
        "Operator_Override": prof.get("operator_override"),
        "ECONOMICS_PRIORITY_SCORE": (de.get("ECONOMICS_PRIORITY") or {}).get("ECONOMICS_PRIORITY_SCORE"),
        "Next_Action": de.get("Next_Action"),
        "full": de,
    }


def simulate_cost_change(row: dict[str, Any], new_cost: float) -> dict[str, Any]:
    """What-if: if I can buy for X, does this deal work? Does not persist invented base cost."""
    revenue = resolve_revenue(row)
    target = target_profit_usd(row)
    fees = _num((row.get("operator_economics") or {}).get("known_fees_usd")) or 0.0
    profit = None if revenue is None else round(revenue - float(new_cost) - fees, 2)
    status = classify_profit_target_status(projected_profit=profit, target=target, has_cost=True)
    target_acq = None if revenue is None else round(revenue - target - fees, 2)
    return {
        "kind": "M3DealEconomicsSimulation",
        "assumed_acquisition_cost": float(new_cost),
        "Revenue": revenue if revenue is not None else "UNKNOWN",
        "Target_profit": target,
        "Target_acquisition_cost": target_acq if target_acq is not None else "UNKNOWN",
        "Projected_profit": profit if profit is not None else "UNKNOWN",
        "PROFIT_TARGET_STATUS": status if revenue is not None else STATUS_UNKNOWN,
        "notes": ["simulation_only_not_persisted_as_evidence"],
    }
