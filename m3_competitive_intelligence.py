"""Competitive Intelligence + New Entrant Opportunity Selection — research only.

Does NOT reject solely because competition exists.
Does NOT invent margins or assume low bidder count = good.
No outreach / supplier contact.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import (
    SCORE_HIGH,
    SCORE_LOW,
    SCORE_MEDIUM,
    build_commercial_intelligence,
    build_commercial_research_queue,
    build_winner_intelligence,
)

log = logging.getLogger("govtracker.m3_competitive_intelligence")

COMP_INDEX_KEY = "m3_competitive_intelligence_v1"

PROFILE_LOW = "LOW_CONCERN"
PROFILE_MEDIUM = "MEDIUM"
PROFILE_HIGH = "HIGH"
PROFILE_UNKNOWN = "UNKNOWN"

MARKET_OPEN = "OPEN_MARKET"
MARKET_INCUMBENT = "INCUMBENT_CONTROLLED"
MARKET_UNKNOWN = "UNKNOWN"

BUCKET_A = "BUCKET_A_FIRST_DEAL_TARGET"
BUCKET_B = "BUCKET_B_GROWTH_TARGET"
BUCKET_C = "BUCKET_C_WATCH_LIST"
BUCKET_D = "BUCKET_D_LOW_PRIORITY"

PRIORITY_HIGH = "HIGH_PRIORITY"
PRIORITY_MEDIUM = "MEDIUM_PRIORITY"
PRIORITY_LOW = "LOW_PRIORITY"

FIN_EASY = "EASY_TO_FINANCE"
FIN_MODERATE = "MODERATE"
FIN_DIFFICULT = "DIFFICULT"

SERVICE_RE = re.compile(
    r"\b(service|services|construction|installation|install|restoration|design.?bid.?build|janitorial|consulting)\b",
    re.I,
)
TANGIBLE_RE = re.compile(
    r"\b(switch|storage|laptop|server|valve|bearing|bushing|blade|equipment|supply|parts?|consumable|monitor|router)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _awards(row: dict[str, Any]) -> list[dict[str, Any]]:
    awards = row.get("historical_awards") or row.get("award_history") or []
    return [a for a in awards if isinstance(a, dict)] if isinstance(awards, list) else []


def build_competition_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 1 — COMPETITION_PROFILE. Not bidder-count-alone."""
    awards = _awards(row)
    winners_intel = build_winner_intelligence(row)

    names: list[str] = []
    amounts: list[float] = []
    for a in awards:
        name = str(a.get("winner") or a.get("vendor") or a.get("awardee") or "").strip()
        if name:
            names.append(name)
        amt = _num(a.get("amount") or a.get("award_amount") or a.get("dollars"))
        if amt is not None:
            amounts.append(amt)

    bidder_count = _num(row.get("historical_bidder_count") or row.get("bidder_count"))
    if bidder_count is None:
        meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
        bidder_count = _num(meta.get("bidder_count") or meta.get("number_of_offers"))

    unique = len({n.lower() for n in names})
    counts = Counter(n.lower() for n in names)
    top_wins = counts.most_common(1)[0][1] if counts else 0
    total_awards = len(names)
    concentration = (top_wins / total_awards) if total_awards else None
    repeat_freq = sum(1 for _, c in counts.items() if c >= 2)
    avg_award = round(sum(amounts) / len(amounts), 2) if amounts else "UNKNOWN"

    reasons: list[str] = []
    if total_awards == 0:
        profile = PROFILE_UNKNOWN
        reasons.append("no_historical_award_data")
    elif unique >= 3 and (concentration is None or concentration <= 0.5):
        profile = PROFILE_LOW
        reasons.append("many_winners_rotating_vendors")
    elif unique >= 2 and (concentration is None or concentration < 0.75):
        profile = PROFILE_MEDIUM
        reasons.append("some_repeat_vendors_not_fully_locked")
    elif concentration is not None and concentration >= 0.75:
        profile = PROFILE_HIGH
        reasons.append("same_vendor_repeatedly_dominates")
    elif unique == 1 and total_awards >= 2:
        profile = PROFILE_HIGH
        reasons.append("single_repeat_winner")
    else:
        profile = PROFILE_MEDIUM
        reasons.append("limited_history_moderate_signal")

    if bidder_count is not None and bidder_count >= 8 and unique >= 3:
        reasons.append("high_bidder_count_but_fragmented_winners_not_auto_reject")

    return {
        "kind": "COMPETITION_PROFILE",
        "profile": profile,
        "historical_bidder_count": bidder_count if bidder_count is not None else "UNKNOWN",
        "number_of_awardees": total_awards,
        "unique_winners": unique,
        "repeat_winner_frequency": repeat_freq,
        "incumbent_concentration": round(concentration, 3) if concentration is not None else "UNKNOWN",
        "average_award_value": avg_award,
        "award_frequency": winners_intel.get("award_frequency") or total_awards,
        "previous_pricing": winners_intel.get("unit_pricing_samples") or "UNKNOWN",
        "repeat_winners": winners_intel.get("repeat_winners") or [],
        "reasons": reasons,
        "notes": ["bidder_count_alone_never_rejects"],
    }


def build_incumbent_analysis(row: dict[str, Any], competition: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 — OPEN_MARKET vs INCUMBENT_CONTROLLED + INCUMBENT_RISK_SCORE."""
    unique = int(competition.get("unique_winners") or 0)
    conc = competition.get("incumbent_concentration")
    conc_f = conc if isinstance(conc, (int, float)) else None
    profile = competition.get("profile")

    score = 0
    signals: list[str] = []
    if unique >= 3 and (conc_f is None or conc_f <= 0.45):
        market = MARKET_OPEN
        score = 15
        signals.append("multiple_vendors_win")
    elif profile == PROFILE_HIGH or (conc_f is not None and conc_f >= 0.75):
        market = MARKET_INCUMBENT
        score = 80
        signals.append("same_vendor_repeatedly_wins")
    elif unique == 0:
        market = MARKET_UNKNOWN
        score = 40
        signals.append("insufficient_history")
    else:
        market = MARKET_OPEN if unique >= 2 else MARKET_UNKNOWN
        score = 45 if unique >= 2 else 50
        signals.append("mixed_or_thin_history")

    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    supply = (si.get("Supply_chain") or {}) if si else {}
    channels = supply.get("all_channels") or supply.get("Public_distributors") or []
    if len(channels) >= 3:
        score = max(0, score - 15)
        signals.append("suppliers_vary_public_channels")
    if supply.get("supplier_confidence") == SCORE_LOW and market == MARKET_INCUMBENT:
        score = min(100, score + 10)
        signals.append("limited_supplier_access_signal")

    band = SCORE_HIGH if score >= 65 else (SCORE_MEDIUM if score >= 35 else SCORE_LOW)
    return {
        "kind": "INCUMBENT_ANALYSIS",
        "market_structure": market,
        "INCUMBENT_RISK_SCORE": score,
        "risk_band": band,
        "signals": signals,
    }


def _supplier_confidence(row: dict[str, Any]) -> str:
    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    if si:
        sc = (si.get("Supply_chain") or {}).get("supplier_confidence")
        if sc:
            return str(sc)
    ci = row.get("commercial_intelligence") if isinstance(row.get("commercial_intelligence"), dict) else {}
    return str(ci.get("Supply_Confidence") or SCORE_LOW)


def build_new_entrant_advantage(
    row: dict[str, Any],
    *,
    competition: dict[str, Any],
    incumbent: dict[str, Any],
) -> dict[str, Any]:
    """Phase 3 — NEW_ENTRANT_ADVANTAGE_SCORE."""
    score = 0
    reasons: list[str] = []
    title = str(row.get("title") or "")
    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    product = (si.get("Product") or {}) if si else {}
    ci = row.get("commercial_intelligence") if isinstance(row.get("commercial_intelligence"), dict) else {}

    if product.get("sufficient_for_pricing_research") or product.get("identity_status") == "COMPLETE":
        score += 12
        reasons.append("product_identity_usable")
    if product.get("Part_number") not in {None, "UNKNOWN"} or product.get("NSN") not in {None, "UNKNOWN"}:
        score += 10
        reasons.append("exact_part_or_nsn")
    if TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        score += 12
        reasons.append("standard_commercial_tangible_goods")
    if not any(x in title.lower() for x in ("install", "installation", "custom", "fabricat")):
        score += 10
        reasons.append("no_installation_easy_shipping_signal")
    else:
        score -= 15
        reasons.append("installation_or_custom_risk")

    sc = _supplier_confidence(row)
    if sc == SCORE_HIGH:
        score += 15
        reasons.append("multiple_suppliers")
    elif sc == SCORE_MEDIUM:
        score += 8
        reasons.append("some_supplier_access")

    if competition.get("profile") == PROFILE_LOW:
        score += 15
        reasons.append("fragmented_winners")
    elif competition.get("profile") == PROFILE_MEDIUM:
        score += 8
        reasons.append("moderate_competition_not_locked")
    elif competition.get("profile") == PROFILE_HIGH:
        score -= 10
        reasons.append("incumbent_dominance_reduces_advantage")
    if incumbent.get("market_structure") == MARKET_OPEN:
        score += 10
        reasons.append("open_market_structure")
    if int(competition.get("award_frequency") or 0) >= 3:
        score += 8
        reasons.append("repeat_demand_signal")

    val = _num(row.get("estimated_value") or row.get("government_revenue") or row.get("solicitation_value"))
    if val is not None:
        if 5_000 <= val <= 250_000:
            score += 12
            reasons.append("manageable_first_transaction_size")
        elif val > 1_000_000:
            score -= 8
            reasons.append("large_contract_harder_first_deal")
    fin = str(ci.get("Financing_Difficulty") or ci.get("FINANCING_DIFFICULTY") or "")
    if "EASY" in fin.upper():
        score += 8
        reasons.append("financing_friendly_signal")
    elif "DIFFICULT" in fin.upper():
        score -= 8
        reasons.append("financing_difficult_signal")

    score = max(0, min(100, score))
    band = SCORE_HIGH if score >= 60 else (SCORE_MEDIUM if score >= 35 else SCORE_LOW)
    return {"NEW_ENTRANT_ADVANTAGE_SCORE": score, "band": band, "reasons": reasons}


def build_financeability_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 8 — FINANCEABILITY_PROFILE."""
    title = str(row.get("title") or "").lower()
    val = _num(row.get("estimated_value") or row.get("government_revenue") or row.get("solicitation_value"))
    sc = _supplier_confidence(row)
    reasons: list[str] = []
    score = 50

    if TANGIBLE_RE.search(title) and not SERVICE_RE.search(title):
        score += 15
        reasons.append("product_liquidity_resale_signal")
    if sc in {SCORE_HIGH, SCORE_MEDIUM}:
        score += 15
        reasons.append("supplier_availability")
    if val is not None:
        if val <= 100_000:
            score += 15
            reasons.append("contract_size_lender_friendly")
        elif val <= 500_000:
            score += 5
            reasons.append("moderate_contract_size")
        else:
            score -= 15
            reasons.append("large_contract_inventory_risk")
    else:
        reasons.append("contract_value_unknown")
        score -= 5
    if any(x in title for x in ("install", "custom", "sole source", "classified")):
        score -= 20
        reasons.append("execution_or_compliance_hard_to_finance")

    score = max(0, min(100, score))
    if score >= 65:
        cls = FIN_EASY
    elif score >= 40:
        cls = FIN_MODERATE
    else:
        cls = FIN_DIFFICULT
    return {
        "kind": "FINANCEABILITY_PROFILE",
        "classification": cls,
        "score": score,
        "contract_size": val if val is not None else "UNKNOWN",
        "reasons": reasons,
    }


def score_first_deal(
    row: dict[str, Any],
    *,
    competition: dict[str, Any],
    incumbent: dict[str, Any],
    advantage: dict[str, Any],
    finance: dict[str, Any],
) -> dict[str, Any]:
    """Phase 5 — FIRST_DEAL_SCORE."""
    score = 0
    why: list[str] = []
    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    ci = row.get("commercial_intelligence") if isinstance(row.get("commercial_intelligence"), dict) else {}

    sc = _supplier_confidence(row)
    if sc == SCORE_HIGH:
        score += 18
        why.append("strong_supplier_confidence")
    elif sc == SCORE_MEDIUM:
        score += 10
        why.append("moderate_supplier_confidence")

    if competition.get("profile") == PROFILE_LOW:
        score += 15
        why.append("competition_low_concern_rotating_winners")
    elif competition.get("profile") == PROFILE_MEDIUM:
        score += 8
        why.append("competition_manageable")
    elif competition.get("profile") == PROFILE_HIGH:
        score -= 12
        why.append("incumbent_heavy_but_not_auto_reject")
    else:
        why.append("competition_history_unknown_neutral")

    risk = int(incumbent.get("INCUMBENT_RISK_SCORE") or 50)
    if risk <= 30:
        score += 12
        why.append("low_incumbent_risk")
    elif risk >= 70:
        score -= 12
        why.append("high_incumbent_risk")

    adv = int(advantage.get("NEW_ENTRANT_ADVANTAGE_SCORE") or 0)
    score += min(20, adv // 5)
    if advantage.get("band") == SCORE_HIGH:
        why.append("high_new_entrant_advantage")

    if finance.get("classification") == FIN_EASY:
        score += 12
        why.append("easy_to_finance")
    elif finance.get("classification") == FIN_DIFFICULT:
        score -= 12
        why.append("difficult_to_finance")

    title = str(row.get("title") or "").lower()
    if SERVICE_RE.search(title):
        score -= 15
        why.append("service_or_construction_execution_heavy")
    elif TANGIBLE_RE.search(title):
        score += 10
        why.append("straightforward_tangible_execution")

    if int(competition.get("award_frequency") or 0) >= 3:
        score += 8
        why.append("repeat_opportunity_signal")

    band = ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score")
    if band == SCORE_MEDIUM:
        score += 6
        why.append("commercial_medium_pending_cost")
    elif band == SCORE_HIGH:
        score += 10
        why.append("commercial_high_signal")

    fd = (si.get("FIRST_DEAL_FIT") or {}).get("band") if si else None
    if fd == SCORE_HIGH:
        score += 8
        why.append("supplier_first_deal_fit_high")

    score = max(0, min(100, score))
    if score >= 65:
        priority = PRIORITY_HIGH
    elif score >= 40:
        priority = PRIORITY_MEDIUM
    else:
        priority = PRIORITY_LOW
    return {"FIRST_DEAL_SCORE": score, "priority": priority, "why": why}


def assign_bucket(
    *,
    first_deal: dict[str, Any],
    finance: dict[str, Any],
    sc: str,
    competition: dict[str, Any],
    row: dict[str, Any],
) -> str:
    """Phase 6 — buckets. Never delete."""
    pri = first_deal.get("priority")
    title = str(row.get("title") or "").lower()
    val = _num(row.get("estimated_value") or row.get("government_revenue"))

    if pri == PRIORITY_HIGH and finance.get("classification") in {FIN_EASY, FIN_MODERATE} and sc in {
        SCORE_HIGH,
        SCORE_MEDIUM,
    }:
        return BUCKET_A
    if pri == PRIORITY_HIGH and (val is not None and val > 250_000):
        return BUCKET_B
    if pri == PRIORITY_MEDIUM and not SERVICE_RE.search(title):
        if sc in {SCORE_HIGH, SCORE_MEDIUM} or competition.get("profile") in {PROFILE_LOW, PROFILE_MEDIUM}:
            return BUCKET_A if finance.get("classification") == FIN_EASY else BUCKET_C
        return BUCKET_C
    if pri == PRIORITY_MEDIUM:
        return BUCKET_C
    if competition.get("profile") == PROFILE_UNKNOWN and TANGIBLE_RE.search(title):
        return BUCKET_C
    return BUCKET_D


def category_key(row: dict[str, Any]) -> str:
    cat = str(row.get("product_category") or "").upper()
    title = str(row.get("title") or "").lower()
    agency = str(row.get("agency") or "").lower()
    if "defense logistics" in agency or re.match(r"^\d{2}--", str(row.get("title") or "")):
        return "DLA_PARTS"
    if any(x in title for x in ("cisco", "dell", "switch", "storage", "laptop", "server")):
        return "IT_EQUIPMENT"
    if any(x in title for x in ("valve", "bearing", "bushing", "blade", "fastener")):
        return "MAINTENANCE_COMPONENTS"
    if any(x in title for x in ("safety", "ppe", "glove", "helmet")):
        return "SAFETY_SUPPLIES"
    if any(x in title for x in ("consumable", "supply", "seed", "chemical")):
        return "INDUSTRIAL_CONSUMABLES"
    if SERVICE_RE.search(title) or "SERVICE" in cat:
        return "SERVICES"
    if "IT_" in cat or "ELECTRONIC" in cat:
        return "IT_EQUIPMENT"
    if "PARTS" in cat or "INDUSTRIAL" in cat:
        return "MAINTENANCE_COMPONENTS"
    return cat or "OTHER"


def build_competitive_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row.get("commercial_intelligence"), dict):
        try:
            row = {**row, "commercial_intelligence": build_commercial_intelligence(row)}
        except Exception:
            pass

    competition = build_competition_profile(row)
    incumbent = build_incumbent_analysis(row, competition)
    advantage = build_new_entrant_advantage(row, competition=competition, incumbent=incumbent)
    finance = build_financeability_profile(row)
    first_deal = score_first_deal(
        row, competition=competition, incumbent=incumbent, advantage=advantage, finance=finance
    )
    sc = _supplier_confidence(row)
    bucket = assign_bucket(
        first_deal=first_deal, finance=finance, sc=sc, competition=competition, row=row
    )

    next_action = "Review first-deal bucket"
    if bucket == BUCKET_A:
        next_action = "Prepare supplier pricing verification"
    elif competition.get("profile") == PROFILE_UNKNOWN:
        next_action = "Gather historical award history"
    elif incumbent.get("market_structure") == MARKET_INCUMBENT:
        next_action = "Assess whether incumbent lock is beatable via alternate channel"
    elif sc == SCORE_LOW:
        next_action = "Improve supplier channel confidence"

    return {
        "kind": "M3CompetitiveIntelligence",
        "generated_at": _utc(),
        "COMPETITION_PROFILE": competition,
        "INCUMBENT": incumbent,
        "NEW_ENTRANT_ADVANTAGE": advantage,
        "FINANCEABILITY": finance,
        "FIRST_DEAL": first_deal,
        "BUCKET": bucket,
        "CATEGORY": category_key(row),
        "Supplier_confidence": sc,
        "Next_Action": next_action,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def load_competitive_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == COMP_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("Failed loading competitive intelligence index")
        return {}


def save_competitive_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {
            "kind": "M3CompetitiveIntelligenceIndex",
            "updated_at": _utc(),
            "by_id": by_id,
            "count": len(by_id),
        }
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == COMP_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=COMP_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("Failed saving competitive intelligence index")
        return False


def get_persisted_competitive(canonical_id: str) -> dict[str, Any] | None:
    data = load_competitive_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    si = by_id.get(canonical_id)
    return si if isinstance(si, dict) else None


def build_category_intelligence(packages: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for p in packages:
        cat = p.get("CATEGORY") or "OTHER"
        buckets.setdefault(cat, []).append(p)

    ranked = []
    for cat, items in buckets.items():
        scores = [int((i.get("FIRST_DEAL") or {}).get("FIRST_DEAL_SCORE") or 0) for i in items]
        adv = [int((i.get("NEW_ENTRANT_ADVANTAGE") or {}).get("NEW_ENTRANT_ADVANTAGE_SCORE") or 0) for i in items]
        a_count = sum(1 for i in items if i.get("BUCKET") == BUCKET_A)
        avg = round(sum(scores) / len(scores), 1) if scores else 0
        ranked.append(
            {
                "category": cat,
                "count": len(items),
                "avg_first_deal_score": avg,
                "avg_new_entrant_advantage": round(sum(adv) / len(adv), 1) if adv else 0,
                "bucket_a_count": a_count,
                "FIRST_DEAL_FIT": SCORE_HIGH if avg >= 60 else (SCORE_MEDIUM if avg >= 40 else SCORE_LOW),
            }
        )
    ranked.sort(key=lambda x: (-x["avg_first_deal_score"], -x["bucket_a_count"]))
    return {
        "kind": "CATEGORY_INTELLIGENCE",
        "ranked": ranked,
        "best": ranked[:5],
        "worst": list(reversed(ranked[-5:])) if ranked else [],
    }


def analyze_competitive_top_opportunities(store: Any, *, limit: int = 25) -> dict[str, Any]:
    """TOP N free local analysis — no paid research."""
    rows = store.all() if hasattr(store, "all") else list(store)
    try:
        cq = build_commercial_research_queue(rows, limit=max(limit, 50))
        ordered = [c["canonical_id"] for c in cq.get("queue") or []]
    except Exception:
        ordered = []
    by_id = {r["canonical_id"]: r for r in rows if r.get("canonical_id")}

    get_persisted_supplier_intelligence = None
    try:
        from m3_supplier_intelligence import build_supplier_research_queue, get_persisted_supplier_intelligence as _gps

        get_persisted_supplier_intelligence = _gps
        sq = build_supplier_research_queue(rows, limit=limit)
        seed_ids = [i["canonical_id"] for i in sq.get("queue") or []]
    except Exception:
        seed_ids = []

    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for cid in seed_ids + ordered:
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        targets.append(by_id[cid])
        if len(targets) >= limit:
            break
    if len(targets) < limit:
        for r in rows:
            cid = r.get("canonical_id")
            if not cid or cid in seen:
                continue
            title = str(r.get("title") or "")
            if SERVICE_RE.search(title) and not TANGIBLE_RE.search(title):
                continue
            seen.add(cid)
            targets.append(r)
            if len(targets) >= limit:
                break

    results = []
    index_updates: dict[str, Any] = {}
    profiles: Counter[str] = Counter()
    risks: Counter[str] = Counter()
    advantages: Counter[str] = Counter()
    buckets: Counter[str] = Counter()

    for row in targets:
        if not row.get("supplier_intelligence") and get_persisted_supplier_intelligence:
            try:
                psi = get_persisted_supplier_intelligence(str(row.get("canonical_id") or ""))
                if psi:
                    row = {**row, "supplier_intelligence": psi}
            except Exception:
                pass

        ci_pkg = build_competitive_intelligence(row)
        index_updates[row["canonical_id"]] = ci_pkg
        full = {**(store.get(row["canonical_id"]) or row)}
        full["competitive_intelligence"] = ci_pkg
        store._rows[row["canonical_id"]] = full

        profiles[ci_pkg["COMPETITION_PROFILE"]["profile"]] += 1
        risks[ci_pkg["INCUMBENT"]["risk_band"]] += 1
        advantages[ci_pkg["NEW_ENTRANT_ADVANTAGE"]["band"]] += 1
        buckets[ci_pkg["BUCKET"]] += 1

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Product": ((row.get("supplier_intelligence") or {}).get("Product") or {}).get(
                    "Technical_description"
                )
                or row.get("title"),
                "Contract_value": _num(row.get("estimated_value") or row.get("government_revenue")) or "UNKNOWN",
                "Historical_competition": ci_pkg["COMPETITION_PROFILE"]["profile"],
                "Winner_pattern": ci_pkg["INCUMBENT"]["market_structure"],
                "Supplier_confidence": ci_pkg["Supplier_confidence"],
                "Financeability": ci_pkg["FINANCEABILITY"]["classification"],
                "First_deal_score": ci_pkg["FIRST_DEAL"]["FIRST_DEAL_SCORE"],
                "First_deal_priority": ci_pkg["FIRST_DEAL"]["priority"],
                "BUCKET": ci_pkg["BUCKET"],
                "CATEGORY": ci_pkg["CATEGORY"],
                "Why": ci_pkg["FIRST_DEAL"]["why"][:6],
                "Next_Action": ci_pkg["Next_Action"],
                "New_entrant_advantage": ci_pkg["NEW_ENTRANT_ADVANTAGE"]["NEW_ENTRANT_ADVANTAGE_SCORE"],
                "Incumbent_risk": ci_pkg["INCUMBENT"]["INCUMBENT_RISK_SCORE"],
            }
        )

    results.sort(key=lambda r: (-int(r.get("First_deal_score") or 0), str(r.get("Opportunity") or "")))

    try:
        existing = load_competitive_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_competitive_index(by)
    except Exception:
        log.exception("competitive index save failed")

    try:
        store.save()
    except Exception:
        log.exception("pipeline save after competitive analyze failed")

    cat_intel = build_category_intelligence(list(index_updates.values()))
    top10 = [r for r in results if r.get("BUCKET") == BUCKET_A][:10]
    if len(top10) < 10:
        for r in results:
            if r in top10:
                continue
            top10.append(r)
            if len(top10) >= 10:
                break

    financeable = sorted(
        results,
        key=lambda r: (
            0 if r.get("Financeability") == FIN_EASY else (1 if r.get("Financeability") == FIN_MODERATE else 2),
            -int(r.get("First_deal_score") or 0),
        ),
    )

    return {
        "kind": "M3CompetitiveAnalysisRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "competition_profiles": dict(profiles),
        "incumbent_risks": dict(risks),
        "new_entrant_advantages": dict(advantages),
        "buckets": dict(buckets),
        "TOP_10_FIRST_DEAL": top10,
        "ALL_SCORED": results,
        "CATEGORY_INTELLIGENCE": cat_intel,
        "MOST_FINANCEABLE": financeable[:5],
        "MOST_DIFFICULT_FINANCE": list(reversed(financeable[-5:])) if financeable else [],
        "OpenAI": 0,
        "paid": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_competitive_section(row: dict[str, Any]) -> dict[str, Any]:
    ci = row.get("competitive_intelligence")
    if not isinstance(ci, dict) or ci.get("kind") != "M3CompetitiveIntelligence":
        persisted = get_persisted_competitive(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3CompetitiveIntelligence":
            ci = persisted
        else:
            ci = build_competitive_intelligence(row)
    comp = ci.get("COMPETITION_PROFILE") or {}
    inc = ci.get("INCUMBENT") or {}
    adv = ci.get("NEW_ENTRANT_ADVANTAGE") or {}
    fd = ci.get("FIRST_DEAL") or {}
    return {
        "kind": "M3DealRoomCompetitiveIntelligence",
        "Competition": comp.get("profile"),
        "Historical_bidders": comp.get("historical_bidder_count"),
        "Winner_concentration": comp.get("incumbent_concentration"),
        "Incumbent_risk": inc.get("INCUMBENT_RISK_SCORE"),
        "Incumbent_market": inc.get("market_structure"),
        "New_entrant_advantage": adv.get("NEW_ENTRANT_ADVANTAGE_SCORE"),
        "First_deal_score": fd.get("FIRST_DEAL_SCORE"),
        "First_deal_priority": fd.get("priority"),
        "BUCKET": ci.get("BUCKET"),
        "Why": fd.get("why") or [],
        "Next_Action": ci.get("Next_Action"),
        "Financeability": (ci.get("FINANCEABILITY") or {}).get("classification"),
        "full": ci,
    }
