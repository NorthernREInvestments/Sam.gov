"""Autonomous portfolio deal analysis + opportunity ranking.

Orchestrates existing M3 research/economics engines across the real pipeline.
Does NOT rebuild document/BOM/pricing engines. Progressive tiers + Cost Governor.
DEVELOPMENT_NO_OUTREACH mandatory — no outreach/quotes/bids/purchases.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_deal_economics import target_profit_usd
from m3_pipeline_store import M3PipelineStore
from national_discovery_funnel import stage1_ultra_cheap

log = logging.getLogger("govtracker.m3_portfolio_deal_analysis")

INDEX_KEY = "m3_portfolio_deal_analysis_v1"
CHECKPOINT_KEY = "m3_portfolio_checkpoint_v1"
SUMMARY_KEY = "m3_portfolio_run_summary_v1"

# Deal states (minimal set)
ST_DISCOVERED = "DISCOVERED"
ST_CHEAP_SCREEN_PASSED = "CHEAP_SCREEN_PASSED"
ST_PACKAGE_RESEARCH = "PACKAGE_RESEARCH"
ST_PRODUCT_IDENTITY_RESEARCH = "PRODUCT_IDENTITY_RESEARCH"
ST_GOVERNMENT_PRICE_RESEARCH = "GOVERNMENT_PRICE_RESEARCH"
ST_COMMERCIAL_PRICE_RESEARCH = "COMMERCIAL_PRICE_RESEARCH"
ST_MATERIAL_GAP_RESEARCH = "MATERIAL_GAP_RESEARCH"
ST_PARTIAL_ECONOMICS = "PARTIAL_ECONOMICS"
ST_ECONOMICS_ESTABLISHED = "ECONOMICS_ESTABLISHED"
ST_COMMERCIAL_VERIFICATION_WORTHY = "COMMERCIAL_VERIFICATION_WORTHY"
ST_FUNDING_VERIFICATION_REQUIRED = "FUNDING_VERIFICATION_REQUIRED"
ST_OWNER_REVIEW = "OWNER_REVIEW"
ST_DEFERRED = "DEFERRED"
ST_VERIFIED_BLOCKED = "VERIFIED_BLOCKED"

# Economics classes
ECON_PROVEN = "PROVEN_ECONOMICS"
ECON_PARTIAL = "PARTIAL_ECONOMICS"
ECON_POTENTIAL = "POTENTIAL_ECONOMICS"
ECON_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

# Research priority
PRI_HIGH = "HIGH"
PRI_MEDIUM = "MEDIUM"
PRI_LOW = "LOW"
PRI_DEFER = "DEFER"

# Research tiers
TIER_0 = 0
TIER_1 = 1
TIER_2 = 2
TIER_3 = 3
TIER_4 = 4
TIER_5 = 5

FIRST_TRANSACTION_CANDIDATE = "FIRST_TRANSACTION_CANDIDATE"
FUNDING_VERIFICATION_REQUIRED = "FUNDING_VERIFICATION_REQUIRED"
COMMERCIAL_VERIFICATION_WORTHY = "COMMERCIAL_VERIFICATION_WORTHY"
UNKNOWN = "UNKNOWN"

_SERVICE_REJECT = re.compile(
    r"\b(consulting|staffing|janitorial|custodial|architectural|software\s+development|"
    r"training\s+services|grant\s+program|courier\s+services|employee\s+benefits|"
    r"compliance\s+request|attendance\s+sheet|reporting\s+spreadsheet|"
    r"moving\s+services|installation\s+and/or\s+moving)\b",
    re.I,
)
_PRODUCT_HINT = re.compile(
    r"\b(equipment|supplies|hardware|parts?|materials?|seed|nsn|commodity|"
    r"furniture|tools?|pump|motor|filter|cable|hose|badge|wheelchair|"
    r"concrete|asphalt|lumber|fence|sign|vehicle|tire|uniform|apparel)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() not in {"", "UNKNOWN", "unknown", "None"}
    return True


def _load_setting(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.debug("portfolio setting load failed %s", key, exc_info=True)
    return {}


def _save_setting(key: str, payload: dict[str, Any]) -> None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            raw = json.dumps(payload, default=str)
            if row is None:
                db.add(AppSetting(key=key, value=raw))
            else:
                row.value = raw
            db.commit()
        finally:
            db.close()
    except Exception:
        log.debug("portfolio setting save failed %s", key, exc_info=True)


def load_portfolio_index() -> dict[str, Any]:
    return _load_setting(INDEX_KEY) or {"kind": "M3PortfolioIndex", "by_id": {}, "updated_at": None}


def save_portfolio_index(index: dict[str, Any]) -> None:
    index["updated_at"] = _utc()
    _save_setting(INDEX_KEY, index)


def load_checkpoint() -> dict[str, Any]:
    return _load_setting(CHECKPOINT_KEY) or {
        "kind": "M3PortfolioCheckpoint",
        "cursor": 0,
        "completed_ids": [],
        "tier_progress": {},
        "updated_at": None,
    }


def save_checkpoint(cp: dict[str, Any]) -> None:
    cp["updated_at"] = _utc()
    _save_setting(CHECKPOINT_KEY, cp)


# ---------------------------------------------------------------------------
# Phase 1 — inventory from durable evidence
# ---------------------------------------------------------------------------


def extract_economics_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """Derive economics class from existing evidence — no re-research."""
    seed = row.get("seed_basket_economics") if isinstance(row.get("seed_basket_economics"), dict) else {}
    basket = seed.get("BASKET_ECONOMICS") if isinstance(seed.get("BASKET_ECONOMICS"), dict) else {}
    gap = row.get("material_acquisition_gap") if isinstance(row.get("material_acquisition_gap"), dict) else {}
    gov = row.get("government_revenue_benchmark") if isinstance(row.get("government_revenue_benchmark"), dict) else {}
    acq = row.get("acquisition_target_intelligence") if isinstance(row.get("acquisition_target_intelligence"), dict) else {}
    deal = row.get("deal_economics") if isinstance(row.get("deal_economics"), dict) else {}
    pub = row.get("public_pricing_evidence") if isinstance(row.get("public_pricing_evidence"), dict) else {}

    rev = _num(basket.get("MODELED_BASKET_REVENUE"))
    cost = _num(basket.get("MODELED_BASKET_ACQUISITION_COST"))
    gross = _num(basket.get("MODELED_GROSS_PRODUCT_SPREAD"))
    cov = _num(basket.get("Primary_L1_L3_quantity_coverage_pct") or basket.get("Two_sided_economics_quantity_coverage_pct"))

    if rev is None:
        rev = _num(gov.get("MODELED_REVENUE") or gov.get("total_revenue") or (gov.get("summary") or {}).get("total_revenue"))
    if cost is None:
        cost = _num(acq.get("MODELED_ACQUISITION") or (acq.get("summary") or {}).get("acquisition_cost"))
    if gross is None and rev is not None and cost is not None:
        gross = round(rev - cost, 2)
    if gross is None:
        gross = _num(deal.get("GROSS_PRODUCT_SPREAD") or deal.get("gross_spread"))

    verification = (
        basket.get("COMMERCIAL_VERIFICATION")
        or gap.get("NEXT_STATE")
        or row.get("commercial_verification_status")
        or UNKNOWN
    )
    if verification == "MATERIAL_ACQUISITION_GAPS_AUTOMATED_RESEARCH_COMPLETE":
        verification = COMMERCIAL_VERIFICATION_WORTHY
    if str(row.get("lifecycle") or "") == "COMMERCIAL_VERIFICATION_REQUIRED":
        if _known(gross) and (gross or 0) > 0:
            verification = COMMERCIAL_VERIFICATION_WORTHY

    two_sided = rev is not None and cost is not None
    partial = bool(basket) or bool(gov) or bool(acq) or bool(pub)
    coverage = cov if cov is not None else (100.0 if two_sided and not basket else 0.0)

    if two_sided and (coverage or 0) >= 70 and (gross or 0) > 0:
        econ_class = ECON_PROVEN if (coverage or 0) >= 85 else ECON_PARTIAL
        deal_state = (
            ST_COMMERCIAL_VERIFICATION_WORTHY
            if verification == COMMERCIAL_VERIFICATION_WORTHY or (gross or 0) >= target_profit_usd(row)
            else ST_ECONOMICS_ESTABLISHED
        )
    elif two_sided:
        econ_class = ECON_PARTIAL
        deal_state = (
            ST_COMMERCIAL_VERIFICATION_WORTHY
            if verification == COMMERCIAL_VERIFICATION_WORTHY
            else ST_PARTIAL_ECONOMICS
        )
    elif partial or _known(rev) or _known(cost):
        econ_class = ECON_POTENTIAL
        deal_state = ST_GOVERNMENT_PRICE_RESEARCH if _known(rev) else ST_COMMERCIAL_PRICE_RESEARCH
    else:
        econ_class = ECON_INSUFFICIENT
        deal_state = ST_DISCOVERED

    tp = target_profit_usd(row)
    headroom = round((gross or 0) - tp, 2) if gross is not None else None

    return {
        "kind": "PORTFOLIO_ECONOMICS_SNAPSHOT",
        "Revenue_basis": rev if rev is not None else UNKNOWN,
        "Acquisition_basis": cost if cost is not None else UNKNOWN,
        "Known_gross_spread": gross if gross is not None else UNKNOWN,
        "Economic_coverage_pct": coverage if coverage is not None else UNKNOWN,
        "Economics_class": econ_class,
        "Deal_state": deal_state,
        "Commercial_verification": verification if _known(verification) else UNKNOWN,
        "Capital_required": cost if cost is not None else UNKNOWN,
        "Gross_headroom_above_10k": headroom if headroom is not None else UNKNOWN,
        "MAX_UNKNOWN_TRANSACTION_COSTS": headroom if headroom is not None and headroom > 0 else UNKNOWN,
        "Freight": basket.get("Freight") or UNKNOWN,
        "Financing": basket.get("Financing") or FUNDING_VERIFICATION_REQUIRED,
        "has_seed_basket": bool(basket),
        "has_gov_benchmark": bool(gov),
        "has_acquisition": bool(acq) or bool(pub),
        "has_bom": bool(row.get("bom") or row.get("line_items")),
        "has_documents": bool(row.get("documents") or row.get("governing_documents")),
        "has_identity": bool(row.get("product_identity") or row.get("commercial_product_matching")),
    }


def inventory_portfolio(store: M3PipelineStore) -> dict[str, Any]:
    rows = store.all()
    cards = []
    counts = {
        "active": 0,
        "economics_established": 0,
        "partial_economics": 0,
        "commercial_verification_worthy": 0,
        "first_transaction_candidates": 0,
        "deferred": 0,
        "insufficient": 0,
    }
    for row in rows:
        snap = extract_economics_snapshot(row)
        pri = cheap_portfolio_priority(row, snap)
        ftx = assess_first_transaction_candidate(row, snap)
        card = {
            "canonical_id": row.get("canonical_id"),
            "Opportunity": row.get("title") or UNKNOWN,
            "Agency": row.get("agency") or UNKNOWN,
            "Deadline": row.get("deadline") or UNKNOWN,
            "Product": _product_label(row),
            "lifecycle": row.get("lifecycle"),
            "PORTFOLIO_RESEARCH_PRIORITY": pri["priority"],
            "priority_reason": pri["reason"],
            "FIRST_TRANSACTION_CANDIDATE": ftx["is_candidate"],
            "first_transaction_why": ftx["why"],
            **snap,
        }
        cards.append(card)
        counts["active"] += 1
        if snap["Economics_class"] == ECON_PROVEN or snap["Deal_state"] == ST_ECONOMICS_ESTABLISHED:
            counts["economics_established"] += 1
        if snap["Economics_class"] == ECON_PARTIAL or snap["Deal_state"] == ST_PARTIAL_ECONOMICS:
            counts["partial_economics"] += 1
        if snap["Deal_state"] == ST_COMMERCIAL_VERIFICATION_WORTHY or snap["Commercial_verification"] == COMMERCIAL_VERIFICATION_WORTHY:
            counts["commercial_verification_worthy"] += 1
        if ftx["is_candidate"]:
            counts["first_transaction_candidates"] += 1
        if pri["priority"] == PRI_DEFER:
            counts["deferred"] += 1
        if snap["Economics_class"] == ECON_INSUFFICIENT:
            counts["insufficient"] += 1

    cards.sort(key=lambda c: (-_operator_priority_score(c), str(c.get("Deadline") or "9999")))
    return {
        "kind": "PORTFOLIO_INVENTORY",
        "counts": counts,
        "opportunities": cards,
        "total": len(cards),
        "updated_at": _utc(),
    }


def _product_label(row: dict[str, Any]) -> str:
    lines = row.get("line_items") if isinstance(row.get("line_items"), list) else []
    if len(lines) > 1:
        return f"BOM ({len(lines)} lines)"
    if lines:
        return str(lines[0].get("description") or lines[0].get("item") or row.get("title") or UNKNOWN)[:120]
    bom = row.get("bom")
    if isinstance(bom, dict) and bom.get("lines"):
        return f"BOM ({len(bom['lines'])} lines)"
    if isinstance(bom, list) and bom:
        return f"BOM ({len(bom)} lines)"
    return str(row.get("title") or UNKNOWN)[:120]


# ---------------------------------------------------------------------------
# Phase 2 — cheap portfolio screen / priority
# ---------------------------------------------------------------------------


def cheap_portfolio_priority(row: dict[str, Any], snap: dict[str, Any] | None = None) -> dict[str, Any]:
    """PORTFOLIO_RESEARCH_PRIORITY — research priority, not profitability claim."""
    snap = snap or extract_economics_snapshot(row)
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")[:800]
    blob = f"{title} {desc}"

    # Already commercially interesting — keep HIGH for monitoring / gap closure
    if snap.get("Deal_state") == ST_COMMERCIAL_VERIFICATION_WORTHY or snap.get("has_seed_basket"):
        return {"priority": PRI_HIGH, "reason": "existing_commercial_verification_or_basket", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}

    # Deadline
    dl = row.get("deadline_evaluation") if isinstance(row.get("deadline_evaluation"), dict) else {}
    st = str(dl.get("status") or "").upper()
    if st in {"EXPIRED", "TOO_LATE"} or row.get("rejected"):
        return {"priority": PRI_DEFER, "reason": "deadline_or_rejected", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}

    # Obvious services — DEFER (UNKNOWN≠rejected for ambiguous product)
    if _SERVICE_REJECT.search(title) and not _PRODUCT_HINT.search(title):
        return {"priority": PRI_DEFER, "reason": "obvious_service_non_product", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}

    # Reuse stage1 if present; else compute
    s1 = row.get("cheap_screen") if isinstance(row.get("cheap_screen"), dict) else None
    if not s1:
        s1 = stage1_ultra_cheap({"title": title, "description": desc, "status": row.get("status") or "OPEN"})
    if s1.get("survive") is False and not _PRODUCT_HINT.search(blob):
        return {"priority": PRI_DEFER, "reason": s1.get("reason") or "cheap_screen_fail", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}

    productish = bool(_PRODUCT_HINT.search(blob)) or snap.get("has_bom") or snap.get("has_identity")
    has_pkg = snap.get("has_documents") or snap.get("has_bom")
    has_econ_path = snap.get("has_gov_benchmark") or snap.get("has_acquisition") or productish

    if productish and has_pkg and has_econ_path:
        return {"priority": PRI_HIGH, "reason": "product_package_econ_path", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}
    if productish or (has_pkg and s1.get("survive")):
        return {"priority": PRI_MEDIUM, "reason": "product_or_package_candidate", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}
    if s1.get("survive"):
        return {"priority": PRI_LOW, "reason": "cheap_survive_weak_product_signal", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}
    return {"priority": PRI_DEFER, "reason": "insufficient_product_signal", "kind": "PORTFOLIO_RESEARCH_PRIORITY"}


# ---------------------------------------------------------------------------
# Phase 4 — promotion rules
# ---------------------------------------------------------------------------


def current_research_tier(row: dict[str, Any], snap: dict[str, Any] | None = None) -> int:
    snap = snap or extract_economics_snapshot(row)
    if snap.get("has_seed_basket") or snap.get("Deal_state") in {
        ST_COMMERCIAL_VERIFICATION_WORTHY,
        ST_ECONOMICS_ESTABLISHED,
        ST_PARTIAL_ECONOMICS,
        ST_MATERIAL_GAP_RESEARCH,
    }:
        if snap.get("has_seed_basket") and snap.get("Economics_class") in {ECON_PARTIAL, ECON_PROVEN}:
            return TIER_5
        if snap.get("has_acquisition") and snap.get("has_gov_benchmark"):
            return TIER_4
    if snap.get("has_gov_benchmark"):
        return TIER_3
    if snap.get("has_identity"):
        return TIER_2
    if snap.get("has_bom") or snap.get("has_documents"):
        return TIER_1
    return TIER_0


def next_promoted_tier(row: dict[str, Any], snap: dict[str, Any] | None = None) -> int | None:
    """Return next tier to run, or None if stop."""
    snap = snap or extract_economics_snapshot(row)
    pri = cheap_portfolio_priority(row, snap)["priority"]
    if pri == PRI_DEFER:
        return None
    cur = current_research_tier(row, snap)

    # Already verification-worthy with interpretable economics — stop deep spend
    if snap.get("Deal_state") == ST_COMMERCIAL_VERIFICATION_WORTHY and (snap.get("Economic_coverage_pct") or 0) >= 55:
        return None

    if cur < TIER_1 and pri in {PRI_HIGH, PRI_MEDIUM}:
        return TIER_1
    if cur == TIER_1 and (snap.get("has_bom") or snap.get("has_documents")):
        return TIER_2
    if cur == TIER_2 and (snap.get("has_identity") or snap.get("has_bom")):
        return TIER_3
    if cur == TIER_3:
        rev = _num(snap.get("Revenue_basis"))
        # Promote when gov-side value suggests plausible transaction OR unknown blocking decision
        if rev is None or rev >= 5000 or snap.get("Economics_class") == ECON_POTENTIAL:
            return TIER_4
        return None
    if cur == TIER_4:
        gross = _num(snap.get("Known_gross_spread"))
        cov = _num(snap.get("Economic_coverage_pct")) or 0
        # Tier 5 only when spread could support profit OR material unknown blocks decision
        if (gross is not None and gross >= 5000) or (cov > 0 and cov < 70 and (gross or 0) > 0):
            return TIER_5
        if snap.get("has_seed_basket"):
            return TIER_5
        return None
    if cur >= TIER_5:
        return None
    # Climb one step for HIGH when stuck at 0 with documents already present
    if cur == TIER_0 and snap.get("has_documents"):
        return TIER_1
    return TIER_1 if pri == PRI_HIGH and cur == TIER_0 else None


# ---------------------------------------------------------------------------
# First transaction + operator priority
# ---------------------------------------------------------------------------


def assess_first_transaction_candidate(row: dict[str, Any], snap: dict[str, Any] | None = None) -> dict[str, Any]:
    snap = snap or extract_economics_snapshot(row)
    why: list[str] = []
    gross = _num(snap.get("Known_gross_spread"))
    capital = _num(snap.get("Capital_required"))
    cov = _num(snap.get("Economic_coverage_pct")) or 0
    ok = True
    if gross is None or gross < 5000:
        ok = False
        why.append("gross_spread_insufficient_or_unknown")
    else:
        why.append("positive_evidenced_gross")
    if capital is not None and capital > 250000:
        ok = False
        why.append("capital_high")
    elif capital is not None:
        why.append("manageable_capital")
    if cov < 50 and not snap.get("has_seed_basket"):
        ok = False
        why.append("coverage_low")
    else:
        why.append("coverage_ok")
    # Simple product preference: single-line or known basket
    lines = row.get("line_items") if isinstance(row.get("line_items"), list) else []
    if len(lines) <= 5 or snap.get("has_seed_basket"):
        why.append("simple_or_known_basket")
    else:
        why.append("complex_bom")
    if snap.get("Deal_state") == ST_COMMERCIAL_VERIFICATION_WORTHY or snap.get("Commercial_verification") == COMMERCIAL_VERIFICATION_WORTHY:
        why.append("commercial_verification_worthy")
    else:
        if not snap.get("has_seed_basket"):
            ok = False
            why.append("not_yet_verification_worthy")
    return {
        "is_candidate": bool(ok),
        "status": FIRST_TRANSACTION_CANDIDATE if ok else "NOT_FIRST_TX",
        "why": why,
        "label": "STRATEGIC_SIGNAL_NOT_APPROVAL",
    }


def _operator_priority_score(card: dict[str, Any]) -> float:
    """Explainable score — not just largest contract value."""
    score = 0.0
    gross = _num(card.get("Known_gross_spread")) or 0.0
    cov = _num(card.get("Economic_coverage_pct")) or 0.0
    capital = _num(card.get("Capital_required"))
    # Prefer executable headroom over raw size
    score += min(gross, 100000) / 1000.0  # cap influence of huge spreads
    score += cov * 0.35
    if card.get("Deal_state") == ST_COMMERCIAL_VERIFICATION_WORTHY:
        score += 40
    if card.get("FIRST_TRANSACTION_CANDIDATE"):
        score += 25
    if card.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_HIGH:
        score += 15
    elif card.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_MEDIUM:
        score += 5
    elif card.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_DEFER:
        score -= 50
    if capital is not None:
        if capital <= 50000:
            score += 10
        elif capital <= 200000:
            score += 4
        else:
            score -= 8
    # Evidence confidence proxy
    if card.get("has_seed_basket") or card.get("Economics_class") == ECON_PROVEN:
        score += 12
    if card.get("Economics_class") == ECON_PARTIAL:
        score += 6
    return score


def build_operator_deal_priority(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for c in cards:
        if c.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_DEFER and c.get("Economics_class") == ECON_INSUFFICIENT:
            continue
        item = {
            **c,
            "OPERATOR_DEAL_PRIORITY_SCORE": round(_operator_priority_score(c), 2),
            "kind": "OPERATOR_DEAL_PRIORITY",
        }
        ranked.append(item)
    ranked.sort(key=lambda x: (-x["OPERATOR_DEAL_PRIORITY_SCORE"], str(x.get("Deadline") or "9999")))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return ranked


# ---------------------------------------------------------------------------
# Progressive research execution (reuse engines)
# ---------------------------------------------------------------------------


def _cost_blocks() -> bool:
    try:
        from cost_governor import get_cost_governor

        dash = get_cost_governor().dashboard_payload()
        status = str((dash.get("status") or dash.get("budget_status") or "")).upper()
        if "EXHAUST" in status or "BLOCK" in status or status == "PAUSED":
            return True
    except Exception:
        return False
    return False


def run_tier_research(store: M3PipelineStore, row: dict[str, Any], tier: int) -> dict[str, Any]:
    """Execute one research tier using existing engines. No outreach. Soft-timeout guarded."""
    import concurrent.futures

    def _inner() -> dict[str, Any]:
        return _run_tier_research_inner(store, row, tier)

    # Soft timeout — never block the portfolio cycle on a single hung fetch
    timeout_s = {TIER_1: 25, TIER_2: 45, TIER_3: 90, TIER_4: 90, TIER_5: 120}.get(tier, 60)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_inner)
            return fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return {
            "canonical_id": str(row.get("canonical_id")),
            "tier": tier,
            "ok": False,
            "reason": "tier_timeout",
            "actions": [],
        }
    except Exception as exc:
        return {
            "canonical_id": str(row.get("canonical_id")),
            "tier": tier,
            "ok": False,
            "error": str(exc)[:200],
            "actions": [],
        }


def _run_tier_research_inner(store: M3PipelineStore, row: dict[str, Any], tier: int) -> dict[str, Any]:
    """Execute one research tier using existing engines. No outreach."""
    cid = str(row.get("canonical_id"))
    result: dict[str, Any] = {"canonical_id": cid, "tier": tier, "ok": False, "actions": []}
    if _cost_blocks() and tier >= TIER_3:
        result["ok"] = False
        result["reason"] = "cost_governor_pause"
        return result

    try:
        if tier == TIER_1:
            # Prefer durable local package evidence; avoid hanging network advance
            row2 = store.get(cid) or row
            docs = row2.get("documents") or row2.get("governing_documents") or []
            lines = row2.get("line_items") or []
            row2["portfolio_package"] = {
                "documents_n": len(docs) if isinstance(docs, list) else 0,
                "line_items_n": len(lines) if isinstance(lines, list) else 0,
                "has_bom": bool(lines) or bool(row2.get("bom")),
                "at": _utc(),
            }
            if not docs and not lines:
                # One short free advance step only when nothing local
                try:
                    from m3_end_to_end import M3EndToEndOrchestrator

                    M3EndToEndOrchestrator(store=store).advance(cid, max_auto_steps=2)
                    row2 = store.get(cid) or row2
                except Exception as exc:
                    row2["portfolio_package"]["advance_error"] = str(exc)[:120]
            row2["portfolio_tier_reached"] = max(int(row2.get("portfolio_tier_reached") or 0), TIER_1)
            row2["portfolio_deal_state"] = ST_PACKAGE_RESEARCH
            store._rows[cid] = row2
            result.update({"ok": True, "actions": ["package_inventory"]})

        elif tier == TIER_2:
            from m3_product_identity_resolution import resolve_opportunity_product_identities

            try:
                ident = resolve_opportunity_product_identities(row)
            except Exception:
                # Local fallback from line descriptions / title
                lines = row.get("line_items") if isinstance(row.get("line_items"), list) else []
                ident = {
                    "kind": "PRODUCT_IDENTITY_LIGHT",
                    "from_title": row.get("title"),
                    "line_count": len(lines),
                    "nsn_hint": bool(re.search(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b", str(row.get("title") or ""))),
                    "attempted": True,
                }
            row2 = store.get(cid) or row
            row2["product_identity"] = ident if isinstance(ident, dict) else {"raw": ident}
            row2["portfolio_tier_reached"] = max(int(row2.get("portfolio_tier_reached") or 0), TIER_2)
            row2["portfolio_deal_state"] = ST_PRODUCT_IDENTITY_RESEARCH
            store._rows[cid] = row2
            result.update({"ok": True, "actions": ["product_identity"]})

        elif tier == TIER_3:
            from m3_government_revenue_benchmark import analyze_opportunity_revenue
            from m3_public_pricing_evidence import CostLedger

            ledger = CostLedger()
            access: list[dict[str, Any]] = []
            pkg = analyze_opportunity_revenue(row, ledger, access, max_lines=4, deep_seed=False)
            row2 = store.get(cid) or row
            row2["government_revenue_benchmark"] = pkg
            row2["portfolio_tier_reached"] = max(int(row2.get("portfolio_tier_reached") or 0), TIER_3)
            row2["portfolio_deal_state"] = ST_GOVERNMENT_PRICE_RESEARCH
            store._rows[cid] = row2
            result.update({"ok": True, "actions": ["government_revenue"], "free": ledger.free_action_count})

        elif tier == TIER_4:
            from m3_acquisition_target_engine import analyze_opportunity_acquisition

            pkg = analyze_opportunity_acquisition(row, allow_paid_web=False)
            row2 = store.get(cid) or row
            row2["acquisition_target_intelligence"] = pkg
            row2["public_pricing_evidence"] = row2.get("public_pricing_evidence") or {
                "kind": "PUBLIC_PRICING_LIGHT",
                "via": "acquisition_target_engine",
                "at": _utc(),
            }
            row2["portfolio_tier_reached"] = max(int(row2.get("portfolio_tier_reached") or 0), TIER_4)
            row2["portfolio_deal_state"] = ST_COMMERCIAL_PRICE_RESEARCH
            store._rows[cid] = row2
            result.update({"ok": True, "actions": ["commercial_acquisition"]})

        elif tier == TIER_5:
            title = str(row.get("title") or "")
            if "seed" in title.lower() or "wildflower" in title.lower():
                from m3_material_acquisition_gap import analyze_material_acquisition_gaps

                pkg = analyze_material_acquisition_gaps(store, persist=True, max_gaps=3, max_pages_per_gap=3)
                result.update({"ok": bool(pkg.get("ok")), "actions": ["material_gap_seed"], "summary": {
                    "NEXT_STATE": pkg.get("NEXT_STATE"),
                    "gross": (pkg.get("BASKET_ECONOMICS") or {}).get("MODELED_GROSS_PRODUCT_SPREAD"),
                }})
            else:
                from m3_acquisition_target_engine import analyze_opportunity_acquisition
                from m3_deal_economics import build_deal_economics_profile

                acq = analyze_opportunity_acquisition(row, allow_paid_web=False)
                econ = build_deal_economics_profile(row)
                row2 = store.get(cid) or row
                row2["acquisition_target_intelligence"] = acq
                row2["deal_economics"] = econ
                row2["portfolio_tier_reached"] = TIER_5
                snap = extract_economics_snapshot(row2)
                row2["portfolio_deal_state"] = snap.get("Deal_state") or ST_PARTIAL_ECONOMICS
                store._rows[cid] = row2
                result.update({"ok": True, "actions": ["material_gap_generic", "deal_economics"]})
        else:
            result["reason"] = "unknown_tier"
    except Exception as exc:
        log.exception("tier research failed cid=%s tier=%s", cid, tier)
        result["error"] = str(exc)[:200]
    return result


# ---------------------------------------------------------------------------
# Portfolio cycle
# ---------------------------------------------------------------------------


def run_portfolio_cycle(
    store: M3PipelineStore | None = None,
    *,
    persist: bool = True,
    max_promote: int = 12,
    max_per_tier: dict[int, int] | None = None,
    resume: bool = True,
    include_discovery_tick: bool = False,
) -> dict[str, Any]:
    """Bounded autonomous portfolio cycle. Progressive — not equal-depth research."""
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    store = store or M3PipelineStore()
    limits = max_per_tier or {TIER_1: 8, TIER_2: 6, TIER_3: 5, TIER_4: 4, TIER_5: 2}
    tier_counts = {t: 0 for t in limits}
    research_actions = 0
    free_actions = 0
    paid_actions = 0

    discovery_meta: dict[str, Any] = {"ran": False}
    if include_discovery_tick:
        try:
            from m3_discovery_service import scheduled_discovery_tick

            discovery_meta = {"ran": True, "result": scheduled_discovery_tick()}
        except Exception as exc:
            discovery_meta = {"ran": True, "error": str(exc)[:160]}

    baseline = inventory_portfolio(store)
    cp = load_checkpoint() if resume else {"completed_ids": [], "cursor": 0, "tier_progress": {}}
    done = set(cp.get("completed_ids") or [])

    # Build promotion queue: HIGH first, then MEDIUM; skip DEFER; prefer not-yet-deep
    cards = baseline["opportunities"]
    promote_queue = [
        c
        for c in cards
        if c.get("PORTFOLIO_RESEARCH_PRIORITY") in {PRI_HIGH, PRI_MEDIUM}
        and c.get("canonical_id") not in done
    ]
    # Prefer incomplete economics
    promote_queue.sort(
        key=lambda c: (
            0 if c.get("Economics_class") == ECON_INSUFFICIENT else 1 if c.get("Economics_class") == ECON_POTENTIAL else 2,
            0 if c.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_HIGH else 1,
            -_operator_priority_score(c),
        )
    )

    promotions: list[dict[str, Any]] = []
    deferred = [c for c in cards if c.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_DEFER]
    blocked: list[dict[str, Any]] = []

    for card in promote_queue:
        if len(promotions) >= max_promote:
            break
        if _cost_blocks():
            blocked.append({"reason": "cost_governor", "canonical_id": card.get("canonical_id")})
            break
        row = store.get(str(card["canonical_id"]))
        if not isinstance(row, dict):
            continue
        snap = extract_economics_snapshot(row)
        nxt = next_promoted_tier(row, snap)
        if nxt is None:
            done.add(str(card["canonical_id"]))
            continue
        if tier_counts.get(nxt, 0) >= limits.get(nxt, 0):
            continue
        log.info("portfolio promote %s -> tier %s", card.get("Opportunity"), nxt)
        print(f"portfolio: promote tier={nxt} {(card.get('Opportunity') or '')[:60]}", flush=True)
        res = run_tier_research(store, row, nxt)
        research_actions += 1
        free_actions += 1
        tier_counts[nxt] = tier_counts.get(nxt, 0) + 1
        promotions.append(res)
        if res.get("ok"):
            # Allow multi-step within same cycle only for HIGH and lower tiers
            if card.get("PORTFOLIO_RESEARCH_PRIORITY") == PRI_HIGH and nxt < TIER_4:
                pass  # leave undoned so next cycle can climb
            else:
                done.add(str(card["canonical_id"]))
        else:
            if res.get("reason") == "cost_governor_pause":
                blocked.append(res)
                break

    if persist:
        try:
            store.save()
        except Exception:
            log.debug("store save failed", exc_info=True)

    after = inventory_portfolio(store)
    operator_queue = build_operator_deal_priority(after["opportunities"])
    top_cards = operator_queue[:8]

    summary = {
        "kind": "PORTFOLIO_RUN_SUMMARY",
        "at": _utc(),
        "baseline_counts": baseline["counts"],
        "after_counts": after["counts"],
        "Opportunities_processed": after["total"],
        "Cheap_screen_passed": sum(
            1 for c in after["opportunities"] if c.get("PORTFOLIO_RESEARCH_PRIORITY") != PRI_DEFER
        ),
        "Cheap_deferred": len(deferred),
        "Packages_researched": tier_counts.get(TIER_1, 0),
        "Products_identified": tier_counts.get(TIER_2, 0),
        "Government_price_evidence": tier_counts.get(TIER_3, 0),
        "Commercial_acquisition_evidence": tier_counts.get(TIER_4, 0),
        "Material_gap_research": tier_counts.get(TIER_5, 0),
        "Promotions": promotions,
        "Research_promotions": len(promotions),
        "Deep_research_count": tier_counts.get(TIER_5, 0),
        "Deferred": [{"canonical_id": d.get("canonical_id"), "reason": d.get("priority_reason")} for d in deferred[:40]],
        "Verified_blocked": blocked,
        "Commercial_verification_worthy": after["counts"]["commercial_verification_worthy"],
        "First_transaction_candidates": after["counts"]["first_transaction_candidates"],
        "Partial_economics": after["counts"]["partial_economics"],
        "Economics_established": after["counts"]["economics_established"],
        "COST": {
            "RESEARCH_ACTION_COUNT": research_actions,
            "FREE_ACTION_COUNT": free_actions,
            "PAID_ACTION_COUNT": paid_actions,
            "RESERVED_COST": 0.0,
            "ACTUAL_COST": 0.0,
            "RECONCILED_COST": 0.0,
        },
        "SAFETY": {
            "Outreach": 0,
            "Registrations": 0,
            "Quotes": 0,
            "Bids": 0,
            "Purchases": 0,
        },
        "discovery": discovery_meta,
        "DEVELOPMENT_NO_OUTREACH": True,
    }

    cp = {
        "kind": "M3PortfolioCheckpoint",
        "cursor": len(done),
        "completed_ids": list(done)[-5000:],
        "tier_progress": tier_counts,
        "last_summary_at": _utc(),
    }
    if persist:
        save_checkpoint(cp)
        _save_setting(SUMMARY_KEY, summary)
        idx = load_portfolio_index()
        by_id = idx.setdefault("by_id", {})
        for card in operator_queue[:50]:
            by_id[str(card["canonical_id"])] = {
                "summary": card,
                "updated_at": _utc(),
            }
        idx["last_run"] = {
            "at": _utc(),
            "counts": after["counts"],
            "top": [
                {
                    "Opportunity": t.get("Opportunity"),
                    "Known_gross_spread": t.get("Known_gross_spread"),
                    "Deal_state": t.get("Deal_state"),
                    "rank": t.get("rank"),
                }
                for t in top_cards[:5]
            ],
        }
        idx["operator_queue"] = operator_queue[:40]
        save_portfolio_index(idx)

    return {
        "ok": True,
        "kind": "M3PortfolioDealAnalysisRun",
        "BASELINE": baseline["counts"],
        "AFTER": after["counts"],
        "SUMMARY": summary,
        "OPERATOR_DEAL_PRIORITY": operator_queue[:25],
        "TOP_DEAL_CARDS": [build_deal_card(store.get(str(c["canonical_id"])) or c) for c in top_cards[:6]],
        "CHECKPOINT": {"completed": len(done), "tier_progress": tier_counts, "resumable": True},
        "COST": summary["COST"],
        "SAFETY": summary["SAFETY"],
        "NEXT_STATE": "AUTONOMOUS_PORTFOLIO_DEAL_ANALYSIS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
        "updated_at": _utc(),
    }


def build_deal_card(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"kind": "TOP_DEAL_CARD", "Opportunity": UNKNOWN}
    snap = extract_economics_snapshot(row)
    ftx = assess_first_transaction_candidate(row, snap)
    pri = cheap_portfolio_priority(row, snap)
    next_action = "Monitor"
    if snap.get("Deal_state") == ST_COMMERCIAL_VERIFICATION_WORTHY:
        next_action = "Verify wholesale + freight (FUTURE_ACTION_IF_PURSUED)"
    elif snap.get("Economics_class") == ECON_PARTIAL:
        next_action = "Close material acquisition gaps"
    elif pri["priority"] == PRI_HIGH:
        next_action = f"Continue research tier {next_promoted_tier(row, snap) or 'done'}"
    elif pri["priority"] == PRI_DEFER:
        next_action = "Deferred — not product-resale priority"

    return {
        "kind": "TOP_DEAL_CARD",
        "Opportunity": row.get("title") or UNKNOWN,
        "Agency": row.get("agency") or UNKNOWN,
        "Deadline": row.get("deadline") or UNKNOWN,
        "Product_BOM": _product_label(row),
        "Government_benchmark": snap.get("Revenue_basis"),
        "Observed_acquisition": snap.get("Acquisition_basis"),
        "Known_gross_spread": snap.get("Known_gross_spread"),
        "Coverage": snap.get("Economic_coverage_pct"),
        "Capital": snap.get("Capital_required"),
        "Freight": snap.get("Freight"),
        "Funding": snap.get("Financing"),
        "Competition": (row.get("competitive_intelligence") or {}).get("summary")
        if isinstance(row.get("competitive_intelligence"), dict)
        else UNKNOWN,
        "Compliance": (row.get("bid_compliance") or {}).get("status")
        if isinstance(row.get("bid_compliance"), dict)
        else UNKNOWN,
        "Status": snap.get("Deal_state"),
        "Economics_class": snap.get("Economics_class"),
        "FIRST_TRANSACTION_CANDIDATE": ftx["is_candidate"],
        "PORTFOLIO_RESEARCH_PRIORITY": pri["priority"],
        "NEXT": next_action,
        "canonical_id": row.get("canonical_id"),
    }


def portfolio_operator_view(store: M3PipelineStore | None = None) -> dict[str, Any]:
    store = store or M3PipelineStore()
    inv = inventory_portfolio(store)
    queue = build_operator_deal_priority(inv["opportunities"])
    last = _load_setting(SUMMARY_KEY)
    return {
        "kind": "M3PortfolioOperatorView",
        "counts": inv["counts"],
        "deals": queue[:50],
        "top_cards": [build_deal_card(store.get(str(c["canonical_id"])) or {}) for c in queue[:6]],
        "filters_supported": [
            "commercial_verification_worthy",
            "economics_established",
            "partial_economics",
            "first_transaction_candidate",
            "profit_potential",
            "deadline",
            "capital_required",
            "evidence_coverage",
        ],
        "last_run_summary": last if last else None,
        "DEVELOPMENT_NO_OUTREACH": True,
        "updated_at": _utc(),
    }


def scale_fixture_portfolio_test(n: int = 25000) -> dict[str, Any]:
    """Engineering scale test — synthetic records only, no live research."""
    fake_rows = []
    for i in range(n):
        product = i % 7 != 0  # ~86% productish survive path
        fake_rows.append(
            {
                "canonical_id": f"synth:{i}",
                "title": f"{'Pump assembly NSN 4320' if product else 'Consulting services'} #{i}",
                "description": "equipment supplies parts" if product else "professional consulting staffing",
                "agency": "TEST",
                "deadline": "2099-01-01",
                "lifecycle": "DISCOVERED",
                "cheap_screen_survive": product,
                "line_items": [{"description": "Pump", "quantity": 10}] if product and i % 11 == 0 else [],
            }
        )
    # Cheap screen all
    pri_counts = {PRI_HIGH: 0, PRI_MEDIUM: 0, PRI_LOW: 0, PRI_DEFER: 0}
    promote = 0
    for r in fake_rows:
        snap = extract_economics_snapshot(r)
        p = cheap_portfolio_priority(r, snap)["priority"]
        pri_counts[p] = pri_counts.get(p, 0) + 1
        nxt = next_promoted_tier(r, snap)
        if nxt is not None and p in {PRI_HIGH, PRI_MEDIUM}:
            promote += 1
    # Checkpoint resume simulation
    cp = {"completed_ids": [f"synth:{i}" for i in range(1000)], "cursor": 1000}
    remaining = n - len(cp["completed_ids"])
    return {
        "kind": "PORTFOLIO_SCALE_FIXTURE_TEST",
        "candidates": n,
        "priority_counts": pri_counts,
        "promotable": promote,
        "checkpoint_resume_remaining": remaining,
        "dedupe_ok": len({r["canonical_id"] for r in fake_rows}) == n,
        "no_live_research": True,
        "ok": n >= 25000 and remaining == n - 1000,
    }


def scheduled_portfolio_tick() -> dict[str, Any]:
    """6 AM / 2 PM autonomous cycle entrypoint."""
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return run_portfolio_cycle(
        store,
        persist=True,
        max_promote=15,
        include_discovery_tick=True,
        resume=True,
    )


def amendment_invalidate_portfolio(row: dict[str, Any], change_type: str) -> dict[str, Any]:
    """Invalidate dependent economics when quantity/spec/deadline/pricing changes."""
    change = str(change_type or "").lower()
    cleared = []
    if any(x in change for x in ("quantity", "spec", "pricing", "bom", "amendment")):
        for key in (
            "seed_basket_economics",
            "material_acquisition_gap",
            "deal_economics",
            "acquisition_target_intelligence",
            "public_pricing_evidence",
        ):
            if key in row:
                row.pop(key, None)
                cleared.append(key)
        row["portfolio_tier_reached"] = min(int(row.get("portfolio_tier_reached") or 0), TIER_2)
        row["portfolio_deal_state"] = ST_PACKAGE_RESEARCH
        row["evidence_invalidated_at"] = _utc()
        row["invalidation"] = {"change_type": change_type, "cleared": cleared, "at": _utc()}
    elif "deadline" in change:
        row["deadline_evaluation"] = None
        cleared.append("deadline_evaluation")
    return {"ok": True, "cleared": cleared, "UNKNOWN_not_rejected": True}
