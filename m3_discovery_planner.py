"""BUILD 7 — Discovery Planner (thin intelligence layer).

Converts validated demand signals into bounded DiscoveryPlan objects.
Does not crawl, does not replace SAM/DLA/BidNet, does not change scoring.
Existing discovery engines remain the executors.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_demand_signal import (
    DETECTED,
    EXPIRED as SIGNAL_EXPIRED,
    HISTORICAL_PATTERN,
    RECOMPETE,
    SOURCES_SOUGHT,
    UNKNOWN as SIGNAL_UNKNOWN,
    VALIDATED,
    WATCHLIST,
    build_demand_signals,
)

BUILD_TAG = "20260918-m3-discovery-planner-1"
PLAN_INDEX_KEY = "m3_discovery_plan_index_v1"

# Plan states
DRAFT = "DRAFT"
APPROVED = "APPROVED"
ACTIVE = "ACTIVE"
COMPLETED = "COMPLETED"
EXPIRED = "EXPIRED"

# Bounded defaults (Cost Governor respectful — not unlimited)
DEFAULT_MAX_PLANS = 25
DEFAULT_MAX_SOURCES_PER_PLAN = 4
DEFAULT_MAX_SEARCH_TERMS = 8
# Estimated free/paid units — SAM search pages are the primary unit
SAM_CALL_COST_PER_SOURCE = 1
PAID_SOURCE_COST_HINT = 0.0  # planner does not authorize paid fetch

# Signal types allowed to seed plans when strong enough
_PLANABLE_TYPES = {
    HISTORICAL_PATTERN,
    RECOMPETE,
    SOURCES_SOUGHT,
    WATCHLIST,
    "EXPIRATION",
    "FORECAST",
}


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _conf_rank(c: Any) -> int:
    return {"VALIDATED": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(str(c or "").upper(), 0)


def empty_plan(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "M3DiscoveryPlan",
        "build": BUILD_TAG,
        "plan_id": None,
        "product": None,
        "category": None,
        "related_demand_signals": [],
        "target_agencies": [],
        "target_sources": [],
        "search_terms": [],
        "identifiers": {},
        "priority": 0,
        "priority_label": "LOW",
        "reason": None,
        "confidence": "UNKNOWN",
        "estimated_search_cost": {
            "sam_api_calls": 0,
            "paid_usd_estimate": 0.0,
            "unit": "sam_search_pages",
            "note": "Estimate only — Cost Governor authorizes execution",
        },
        "expected_value": "UNKNOWN",
        "cost_governor": {
            "respects_limits": True,
            "auto_execute": False,
            "budget_check": None,
        },
        "status": DRAFT,
        "created_at": _utc(),
        "updated_at": _utc(),
        "executes_discovery": False,
    }
    base.update(overrides)
    return base


def plan_id_for(*, product_key: str, agencies: list[str], sources: list[str]) -> str:
    ag = ",".join(sorted({a for a in agencies if a})[:3]) or "any"
    src = ",".join(sorted({s for s in sources if s})[:3]) or "sam"
    return f"plan|{product_key}|{ag}|{src}"[:220]


def _product_identity_sufficient(product: dict[str, Any] | None, identifiers: dict[str, Any]) -> bool:
    if identifiers.get("nsn") or identifiers.get("part_number"):
        return True
    if product and (product.get("nsn") or product.get("part_number") or product.get("product_dedupe_key")):
        return True
    # Category-only without identifiers is insufficient for ACTIVE/APPROVED
    return False


def _signal_strong_enough(signal: dict[str, Any]) -> bool:
    status = str(signal.get("status") or "").upper()
    conf = str(signal.get("confidence") or "").upper()
    if status in {SIGNAL_UNKNOWN, SIGNAL_EXPIRED}:
        return False
    if conf in {"", "UNKNOWN", "LOW"}:
        return False
    if status == DETECTED and conf == "MEDIUM":
        return True  # may become DRAFT only
    if status == VALIDATED or conf in {"HIGH", "VALIDATED"}:
        return True
    return False


def _extract_identifiers(signal: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    product = signal.get("related_product") if isinstance(signal.get("related_product"), dict) else {}
    row = row or {}
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}
    nsn = product.get("nsn") or struct.get("nsn")
    if isinstance(fields.get("nsn"), dict):
        nsn = nsn or fields["nsn"].get("value")
    pn = product.get("part_number") or struct.get("part_number")
    if isinstance(fields.get("part_number"), dict):
        pn = pn or fields["part_number"].get("value")
    cage = struct.get("cage")
    if isinstance(fields.get("cage"), dict):
        cage = cage or fields["cage"].get("value")
    mfr = struct.get("oem") or struct.get("manufacturer") or row.get("manufacturer")
    naics = row.get("naics") or row.get("naics_code")
    return {
        "nsn": nsn or None,
        "part_number": pn or None,
        "cage": cage or None,
        "manufacturer": mfr or None,
        "naics": naics or None,
        "product_dedupe_key": product.get("product_dedupe_key")
        or (f"nsn:{str(nsn).upper()}" if nsn else None),
        "product_id": product.get("product_id") or row.get("knowledge_product_id"),
    }


def _search_terms(identifiers: dict[str, Any], signal: dict[str, Any], category: str | None) -> list[str]:
    terms: list[str] = []
    if identifiers.get("nsn"):
        terms.append(str(identifiers["nsn"]))
    if identifiers.get("part_number"):
        terms.append(str(identifiers["part_number"]))
    if identifiers.get("manufacturer"):
        terms.append(str(identifiers["manufacturer"]))
    if identifiers.get("cage"):
        terms.append(f"CAGE {identifiers['cage']}")
    product = signal.get("related_product") if isinstance(signal.get("related_product"), dict) else {}
    title = product.get("title")
    if title and len(str(title)) >= 4:
        # Keep short — first meaningful chunk
        terms.append(str(title)[:80])
    if category:
        terms.append(category.replace("_", " "))
    # Dedupe preserve order
    out: list[str] = []
    for t in terms:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out[:DEFAULT_MAX_SEARCH_TERMS]


def _resolve_sources(signal: dict[str, Any], agency: str | None) -> list[str]:
    recipe = signal.get("monitoring_recipe") if isinstance(signal.get("monitoring_recipe"), dict) else {}
    sources = list(recipe.get("monitor_sources") or [])
    if not sources:
        sources = ["fed_sam_contract_opportunities"]
    agency_l = (agency or "").upper()
    st = str(signal.get("signal_type") or "")
    if st == SOURCES_SOUGHT and "fed_sam_contract_opportunities" not in sources:
        sources.insert(0, "fed_sam_contract_opportunities")
    if "DLA" in agency_l and "dla_via_sam_spe_spr" not in sources:
        sources.append("dla_via_sam_spe_spr")
    # Prefer operational source recipes when available
    try:
        from product_resale_source_intelligence import SOURCE_RECIPES

        active_ids = {
            r["source_id"]
            for r in SOURCE_RECIPES
            if r.get("m3_status") in {"OPERATIONAL", "STATE_MODELED_NO_BYPASS"}
            or r.get("coverage_state") == "ACTIVE"
        }
        # Keep planner sources that are known; always allow SAM
        filtered = [s for s in sources if s in active_ids or s.startswith("fed_sam") or s.startswith("dla_")]
        if filtered:
            sources = filtered
    except Exception:
        pass
    # Bound
    return list(dict.fromkeys(sources))[:DEFAULT_MAX_SOURCES_PER_PLAN]


def _infer_category(row: dict[str, Any] | None, signal: dict[str, Any]) -> str | None:
    try:
        from product_resale_source_intelligence import infer_category

        if row:
            return infer_category(row)
        product = signal.get("related_product") if isinstance(signal.get("related_product"), dict) else {}
        return infer_category({"title": product.get("title") or "", "description": ""})
    except Exception:
        return None


def _reason_for(signal: dict[str, Any], identifiers: dict[str, Any]) -> str:
    st = str(signal.get("signal_type") or "")
    if st == HISTORICAL_PATTERN:
        return "Recurring government demand detected."
    if st == RECOMPETE:
        return "Recompete / renewal window indicated — monitor for follow-on solicitation."
    if st == SOURCES_SOUGHT:
        return "Early market research (Sources Sought / RFI) — watch for forthcoming solicitation."
    if st == WATCHLIST:
        return "Operator / watchlist interest — monitor for related postings."
    if st == "EXPIRATION":
        return "Contract/order lifecycle ending — monitor for replacement demand."
    if st == "FORECAST":
        return "Agency forecast signal — monitor listed sources for related opportunities."
    if identifiers.get("nsn"):
        return f"Validated demand with exact NSN {identifiers['nsn']}."
    return "Demand intelligence supports bounded monitoring."


def _priority_score(signal: dict[str, Any], identifiers: dict[str, Any], expected_value: Any) -> tuple[int, str]:
    score = 20
    st = str(signal.get("signal_type") or "")
    type_boost = {
        RECOMPETE: 25,
        HISTORICAL_PATTERN: 22,
        SOURCES_SOUGHT: 18,
        WATCHLIST: 16,
        "EXPIRATION": 20,
        "FORECAST": 10,
    }
    score += type_boost.get(st, 8)
    score += _conf_rank(signal.get("confidence")) * 8
    if identifiers.get("nsn"):
        score += 15
    elif identifiers.get("part_number"):
        score += 10
    ev = _num(expected_value)
    if ev is not None:
        if ev >= 50000:
            score += 10
        elif ev >= 10000:
            score += 5
    # Cap
    score = max(1, min(100, score))
    label = "HIGH" if score >= 70 else ("MEDIUM" if score >= 45 else "LOW")
    return score, label


def _expected_value_from_signal(signal: dict[str, Any], row: dict[str, Any] | None) -> Any:
    row = row or {}
    proj = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    vals = []
    for d in proj.get("demand_evidence") or []:
        if isinstance(d, dict):
            n = _num(d.get("value"))
            if n is not None:
                vals.append(n)
    if vals:
        return round(sum(vals) / len(vals), 2)
    for key in ("historical_award_amount", "estimated_value", "award_amount"):
        n = _num(row.get(key))
        if n is not None:
            return n
    # evidence snippets rarely hold value — leave UNKNOWN
    return "UNKNOWN"


def _budget_snapshot() -> dict[str, Any]:
    """Read-only Cost Governor / SAM budget view — does not authorize spend."""
    snap: dict[str, Any] = {
        "sam_can_spend": None,
        "governor_mode": None,
        "note": "Planner does not call authorize(); execution remains with discovery + Cost Governor",
    }
    try:
        from api_budget import can_spend_sam

        snap["sam_can_spend"] = bool(can_spend_sam(1))
    except Exception:
        snap["sam_can_spend"] = None
    try:
        from cost_governor import get_cost_governor

        gov = get_cost_governor()
        dash = gov.dashboard() if hasattr(gov, "dashboard") else {}
        snap["governor_mode"] = dash.get("mode") or dash.get("budget_mode") or getattr(gov, "mode", None)
        snap["sources_paused_by_budget"] = list(dash.get("sources_paused_by_budget") or [])[:10]
    except Exception:
        pass
    return snap


def plan_from_signal(
    signal: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
    force_draft: bool = False,
) -> dict[str, Any] | None:
    """
    Build one DiscoveryPlan from a demand signal.

    VALIDATED/HIGH + sufficient identity → APPROVED (not auto-executed).
    DETECTED/MEDIUM → DRAFT.
    Weak/UNKNOWN → None (no active plan).
    """
    if not isinstance(signal, dict):
        return None
    if str(signal.get("signal_type")) not in _PLANABLE_TYPES and not force_draft:
        return None
    if not _signal_strong_enough(signal) and not force_draft:
        return None

    identifiers = _extract_identifiers(signal, row)
    identity_ok = _product_identity_sufficient(
        signal.get("related_product") if isinstance(signal.get("related_product"), dict) else {},
        identifiers,
    )
    conf = str(signal.get("confidence") or "").upper()
    status_sig = str(signal.get("status") or "").upper()

    # Weak identity: only DRAFT if signal otherwise medium+; never APPROVED/ACTIVE
    if not identity_ok:
        if conf in {"HIGH", "VALIDATED"} and status_sig == VALIDATED:
            # Still allow DRAFT so operator can enrich identity — not APPROVED
            plan_status = DRAFT
        elif conf == "MEDIUM" or status_sig == DETECTED:
            plan_status = DRAFT
        else:
            return None
    else:
        if status_sig == VALIDATED or conf in {"HIGH", "VALIDATED"}:
            plan_status = APPROVED
        else:
            plan_status = DRAFT

    if force_draft:
        plan_status = DRAFT

    agency = _clean(signal.get("related_agency") or signal.get("related_buyer"))
    agencies = [a for a in [agency] if a]
    sources = _resolve_sources(signal, agency)
    if not sources:
        return None

    category = _infer_category(row, signal)
    terms = _search_terms(identifiers, signal, category)
    if not terms and not identifiers.get("nsn") and not identifiers.get("part_number"):
        # No searchable path
        if plan_status == APPROVED:
            plan_status = DRAFT
        if not category:
            return None

    product = signal.get("related_product") if isinstance(signal.get("related_product"), dict) else {}
    product_key = (
        identifiers.get("product_dedupe_key")
        or product.get("nsn")
        or product.get("part_number")
        or product.get("title")
        or signal.get("opportunity_id")
        or signal.get("signal_id")
        or "unknown"
    )
    expected_value = _expected_value_from_signal(signal, row)
    priority, label = _priority_score(signal, identifiers, expected_value)
    sam_calls = max(1, len(sources) * SAM_CALL_COST_PER_SOURCE)

    pid = plan_id_for(product_key=str(product_key), agencies=agencies, sources=sources)
    return empty_plan(
        plan_id=pid,
        product={
            "product_id": identifiers.get("product_id"),
            "dedupe_key": identifiers.get("product_dedupe_key"),
            "nsn": identifiers.get("nsn"),
            "part_number": identifiers.get("part_number"),
            "title": product.get("title"),
        },
        category=category,
        related_demand_signals=[
            {
                "signal_id": signal.get("signal_id"),
                "signal_type": signal.get("signal_type"),
                "status": signal.get("status"),
                "confidence": signal.get("confidence"),
                "expected_timing": signal.get("expected_timing"),
            }
        ],
        target_agencies=agencies,
        target_sources=sources,
        search_terms=terms,
        identifiers={k: v for k, v in identifiers.items() if v not in (None, "")},
        priority=priority,
        priority_label=label,
        reason=_reason_for(signal, identifiers),
        confidence="HIGH" if plan_status == APPROVED else ("MEDIUM" if plan_status == DRAFT else "UNKNOWN"),
        estimated_search_cost={
            "sam_api_calls": sam_calls,
            "paid_usd_estimate": PAID_SOURCE_COST_HINT,
            "unit": "sam_search_pages",
            "sources_count": len(sources),
            "note": "Estimate only — Cost Governor authorizes execution",
        },
        expected_value=expected_value,
        cost_governor={
            "respects_limits": True,
            "auto_execute": False,
            "budget_check": _budget_snapshot(),
            "tier_hint": "TIER_4_NEW_DISCOVERY",
        },
        status=plan_status,
        opportunity_id=signal.get("opportunity_id"),
    )


def load_plan_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PLAN_INDEX_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_plan_id", {})
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"kind": "M3DiscoveryPlanIndex", "by_plan_id": {}, "build": BUILD_TAG}


def save_plan_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PLAN_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=PLAN_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def upsert_plans(
    plans: list[dict[str, Any]],
    *,
    index: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    index = index if index is not None else {"by_plan_id": {}}
    by = index.setdefault("by_plan_id", {})
    for p in plans:
        pid = p.get("plan_id")
        if not pid:
            continue
        prev = by.get(pid)
        # Preserve COMPLETED/EXPIRED/ACTIVE operator states
        if prev and str(prev.get("status")) in {COMPLETED, EXPIRED, ACTIVE}:
            merged = deepcopy(p)
            merged["status"] = prev["status"]
            merged["created_at"] = prev.get("created_at") or merged.get("created_at")
            merged["updated_at"] = _utc()
            by[pid] = merged
            continue
        stored = deepcopy(p)
        if prev and prev.get("created_at"):
            stored["created_at"] = prev["created_at"]
        stored["updated_at"] = _utc()
        by[pid] = stored
    if persist:
        save_plan_index(index)
    return index


def build_discovery_plans(
    *,
    signals: list[dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    store: Any | None = None,
    max_plans: int = DEFAULT_MAX_PLANS,
    persist: bool = False,
    include_draft: bool = True,
) -> dict[str, Any]:
    """
    Build bounded discovery plans from demand intelligence.

    Does not execute searches. Caps plan count. Attaches Cost Governor snapshot.
    """
    max_plans = max(1, min(100, int(max_plans or DEFAULT_MAX_PLANS)))
    row_by_oid: dict[str, dict[str, Any]] = {}

    if signals is None:
        if rows is None and store is not None:
            rows = list(store.all()) if hasattr(store, "all") else []
        bundle = build_demand_signals(rows=rows, store=store, persist=False, limit=300)
        signals = list(bundle.get("signals") or [])
        for r in rows or []:
            if isinstance(r, dict) and r.get("canonical_id"):
                row_by_oid[str(r["canonical_id"])] = r
    else:
        for r in rows or []:
            if isinstance(r, dict) and r.get("canonical_id"):
                row_by_oid[str(r["canonical_id"])] = r

    budget = _budget_snapshot()
    plans: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Prefer stronger signals first
    ordered = sorted(
        [s for s in signals if isinstance(s, dict)],
        key=lambda s: (
            -_conf_rank(s.get("confidence")),
            0 if str(s.get("status")) == VALIDATED else 1,
            str(s.get("signal_id") or ""),
        ),
    )

    for signal in ordered:
        if len(plans) >= max_plans:
            skipped.append({"reason": "max_plans_cap", "signal_id": signal.get("signal_id")})
            continue
        # If SAM budget exhausted, still create plans but mark cost deferral — do not invent spend
        row = row_by_oid.get(str(signal.get("opportunity_id") or ""))
        plan = plan_from_signal(signal, row=row)
        if plan is None:
            skipped.append(
                {
                    "reason": "weak_or_insufficient_identity",
                    "signal_id": signal.get("signal_id"),
                    "signal_type": signal.get("signal_type"),
                    "status": signal.get("status"),
                    "confidence": signal.get("confidence"),
                }
            )
            continue
        if budget.get("sam_can_spend") is False:
            plan["cost_governor"] = {
                **(plan.get("cost_governor") or {}),
                "budget_check": budget,
                "deferred_hint": "SAM_BUDGET_EXHAUSTED_PLAN_ONLY",
            }
        plans.append(plan)

    if not include_draft:
        plans = [p for p in plans if p.get("status") != DRAFT]

    index = load_plan_index() if persist else {"by_plan_id": {}}
    upsert_plans(plans, index=index, persist=persist)

    # Merge persisted operator statuses
    if persist or index.get("by_plan_id"):
        by = index.get("by_plan_id") or {}
        merged = []
        for p in plans:
            stored = by.get(p["plan_id"])
            merged.append(stored if stored else p)
        plans = merged

    plans.sort(key=lambda p: (-int(p.get("priority") or 0), str(p.get("plan_id") or "")))

    by_status: dict[str, int] = {}
    for p in plans:
        st = str(p.get("status") or DRAFT)
        by_status[st] = by_status.get(st, 0) + 1

    total_sam = sum(int((p.get("estimated_search_cost") or {}).get("sam_api_calls") or 0) for p in plans)

    return {
        "kind": "M3DiscoveryPlanBundle",
        "build": BUILD_TAG,
        "question": "What should we search for, where should we search, and why?",
        "count": len(plans),
        "max_plans": max_plans,
        "by_status": by_status,
        "plans": plans,
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:15],
        "cost_summary": {
            "total_estimated_sam_api_calls": total_sam,
            "budget_snapshot": budget,
            "auto_execute": False,
            "unlimited_plans_forbidden": True,
        },
        "executes_discovery": False,
        "note": "Plans are recommendations for existing discovery engines — Cost Governor authorizes any paid run",
        "generated_at": _utc(),
    }


def set_plan_status(plan_id: str, status: str, *, persist: bool = True) -> dict[str, Any]:
    if status not in {DRAFT, APPROVED, ACTIVE, COMPLETED, EXPIRED}:
        return {"ok": False, "error": "invalid_status"}
    index = load_plan_index()
    by = index.setdefault("by_plan_id", {})
    plan = by.get(plan_id)
    if not plan:
        return {"ok": False, "error": "plan_not_found"}
    plan = dict(plan)
    plan["status"] = status
    plan["updated_at"] = _utc()
    by[plan_id] = plan
    if persist:
        save_plan_index(index)
    return {"ok": True, "plan": plan, "build": BUILD_TAG}
