"""Revenue-First Commercial Intelligence V1.

Upgrades M3 from discovery-centric to revenue-first product resale:
profit lanes (CASH_FLOW / GROWTH / SCALE), cash-flow timing, improved
pricing + historical recovery, supplier-first matching, repeat-buyer
intelligence, focused RFQs, supplier relationship memory, UI projection.

Preserves HT v2 results and CI upgrade v1 artifacts. No fabricated prices.
No auto-send. SAM prefer 0 calls/day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import (
    FIN_BLOCKED,
    FIN_CONFIRMED,
    FIN_UNKNOWN,
    FIN_UNVERIFIED,
    REMATCH_FILE,
    PACKET_RECON_FILE,
    PILOT_FILE as CI_PILOT_FILE,
    DEALS_FILE as CI_DEALS_FILE,
    classify_family,
    financing_label_for_deal,
    load_ht_rows,
    max_supplier_cost_targets,
    multi_line_completeness,
    normalize_product_identity,
    quoteable_rows,
    research_public_price_bounded,
    supplier_advantage_score,
    _dec,
    _money_str,
    _load,
    _save,
    _utc,
    _data,
)
from buyer_intelligence.multi_supplier_basket_line_matching_v1 import best_supplier_for_line
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    resolve_contact,
)

log = logging.getLogger("govtracker.revenue_first_commercial_intelligence_v1")

BUILD = "20261012-m3-revenue-first-commercial-intelligence-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"

REPORT_FILE = f"{OUT_DIR}/m3_revenue_first_v1_report.json"
PILOT_FILE = f"{OUT_DIR}/m3_revenue_first_v1_pilot.json"
LANES_FILE = f"{OUT_DIR}/m3_revenue_first_v1_lanes.json"
CASHFLOW_FILE = f"{OUT_DIR}/m3_revenue_first_v1_cashflow.json"
SUPPLIER_FIRST_FILE = f"{OUT_DIR}/m3_revenue_first_v1_supplier_first.json"
REPEAT_BUYER_FILE = f"{OUT_DIR}/m3_revenue_first_v1_repeat_buyers.json"
OUTREACH_FILE = f"{OUT_DIR}/m3_revenue_first_v1_monday_outreach.json"
MEMORY_FILE = f"{OUT_DIR}/m3_revenue_first_v1_supplier_memory.json"
DEALS_FILE = f"{OUT_DIR}/m3_revenue_first_v1_available_deals.json"
CHECKPOINT_FILE = f"{OUT_DIR}/m3_revenue_first_v1_checkpoint.json"
TOP20_FILE = f"{OUT_DIR}/m3_revenue_first_v1_top20.json"

LANE_CASH = "CASH_FLOW"
LANE_GROWTH = "GROWTH"
LANE_SCALE = "SCALE"
LANE_PROVISIONAL = "PROVISIONAL"
LANE_BELOW = "BELOW_THRESHOLD"
LANE_BLOCKED = "BLOCKED"

MONEY = Decimal("0.01")

# Typical Net-30 government payment is NOT guaranteed — treat as estimate only
DEFAULT_GOV_PAY_DAYS_EST = 45
DEFAULT_SUPPLIER_LEAD_DAYS_EST = 7
DEFAULT_SHIP_DAYS_EST = 5
DEFAULT_ACCEPT_DAYS_EST = 5


# ---------------------------------------------------------------------------
# Profit lanes
# ---------------------------------------------------------------------------


def classify_profit_lane(
    *,
    expected_contribution: Decimal | None,
    evidence_complete: bool,
    financing_status: str,
    full_sourcing_ready: bool,
) -> dict[str, Any]:
    """Lane from expected contribution after direct costs+financing when known.

    Without verified economics → PROVISIONAL (never claim verified profit).
    """
    if financing_status == FIN_BLOCKED:
        return {
            "LANE": LANE_BLOCKED,
            "LANE_STATUS": "BLOCKED",
            "VERIFICATION": "BLOCKED",
            "EXPECTED_CONTRIBUTION": _money_str(expected_contribution),
            "NOTE": "Financing blocked under capital constraints",
        }
    if expected_contribution is None or not evidence_complete:
        return {
            "LANE": LANE_PROVISIONAL,
            "LANE_STATUS": "PROVISIONAL",
            "VERIFICATION": "UNVERIFIED",
            "EXPECTED_CONTRIBUTION": _money_str(expected_contribution),
            "NOTE": "Missing price/cost/freight/financing evidence — not a zero-profit claim",
        }
    c = expected_contribution
    if c < Decimal("500"):
        lane = LANE_BELOW
    elif c < Decimal("5000"):
        lane = LANE_CASH
    elif c < Decimal("15000"):
        lane = LANE_GROWTH
    else:
        lane = LANE_SCALE
    return {
        "LANE": lane,
        "LANE_STATUS": "VERIFIED" if full_sourcing_ready and financing_status == FIN_CONFIRMED else "EVIDENCE_PARTIAL",
        "VERIFICATION": "VERIFIED" if evidence_complete and full_sourcing_ready else "PARTIAL",
        "EXPECTED_CONTRIBUTION": _money_str(c),
        "NOTE": None,
    }


def max_cost_targets_by_lane(selling_price: Decimal | None, **costs: Any) -> dict[str, Any]:
    """Extend max supplier-cost ceilings across CASH_FLOW / GROWTH / SCALE floors."""
    base = max_supplier_cost_targets(selling_price=selling_price, **costs)
    if selling_price is None:
        return base
    unknown = base.get("UNKNOWN_DIRECT_COSTS") or []
    known_direct = Decimal("0")
    for k in ("freight", "compliance", "financing_fees"):
        v = costs.get(k)
        if v is not None:
            known_direct += _dec(v) or Decimal("0")
    for label, profit in (
        ("LANE_CASH_FLOW_500", Decimal("500")),
        ("LANE_CASH_FLOW_2500", Decimal("2500")),
        ("LANE_GROWTH_5000", Decimal("5000")),
        ("LANE_GROWTH_10000", Decimal("10000")),
        ("LANE_SCALE_15000", Decimal("15000")),
    ):
        ceiling = selling_price - known_direct - profit
        base.setdefault("TARGETS", {})[label] = {
            "MAX_ACQUISITION_PLUS_UNRESOLVED": _money_str(ceiling),
            "PROFIT_FLOOR": _money_str(profit),
            "CONDITIONAL": bool(unknown),
        }
    return base


# ---------------------------------------------------------------------------
# Cash-flow timing
# ---------------------------------------------------------------------------


def evaluate_cash_flow(
    *,
    award_or_selling: Decimal | None,
    acquisition_cost: Decimal | None,
    supplier_deposit: Decimal | None = None,
    supplier_payment_days: int | None = None,
    inventory_lead_days: int | None = None,
    shipping_days: int | None = None,
    acceptance_days: int | None = None,
    invoice_submit_days: int | None = None,
    gov_payment_days: int | None = None,
    financing_advance_pct: Decimal | None = None,
    financing_fees: Decimal | None = None,
    financing_status: str = FIN_UNKNOWN,
    owner_cash_forbidden: bool = True,
) -> dict[str, Any]:
    """Cash-flow velocity model. Unknown timing stays UNKNOWN — never invented as zero cost."""
    missing: list[str] = []
    if award_or_selling is None:
        missing.append("AWARD_OR_SELLING_PRICE")
    if acquisition_cost is None:
        missing.append("ACQUISITION_COST")

    # Upfront cash: deposit or full acquisition if no terms/financing confirmed
    deposit = supplier_deposit
    if deposit is None and acquisition_cost is not None:
        # Unknown deposit — do not assume 0; flag unknown
        missing.append("SUPPLIER_DEPOSIT")
    upfront = deposit if deposit is not None else None
    if financing_status != FIN_CONFIRMED and acquisition_cost is not None and deposit is None:
        # Path unverified: treat full acquisition as potentially required until terms proven
        upfront_scenario = acquisition_cost
    else:
        upfront_scenario = deposit if deposit is not None else (Decimal("0") if financing_status == FIN_CONFIRMED else None)

    owner_cash = Decimal("0")
    if owner_cash_forbidden:
        if upfront_scenario is not None and upfront_scenario > 0 and financing_status != FIN_CONFIRMED:
            # Would require owner cash or unconfirmed financing
            owner_cash_required = True
        else:
            owner_cash_required = financing_status != FIN_CONFIRMED and acquisition_cost is not None
    else:
        owner_cash_required = bool(upfront_scenario and upfront_scenario > 0)

    # Timing chain (estimates only when values provided)
    timing_known = all(
        v is not None
        for v in (inventory_lead_days, shipping_days, acceptance_days, invoice_submit_days, gov_payment_days)
    )
    estimated_days = None
    confirmed_days = None
    pay_conf = "UNKNOWN"
    if timing_known:
        estimated_days = int(inventory_lead_days or 0) + int(shipping_days or 0) + int(acceptance_days or 0) + int(
            invoice_submit_days or 0
        ) + int(gov_payment_days or 0)
        pay_conf = "ESTIMATED"
        # Confirmed only when all inputs were verified (caller must pass only verified ints)
        # We never auto-promote ESTIMATED → CONFIRMED here
    elif any(
        v is not None
        for v in (inventory_lead_days, shipping_days, acceptance_days, invoice_submit_days, gov_payment_days)
    ):
        # Partial — fill gaps with labeled defaults for ESTIMATED only
        lead = inventory_lead_days if inventory_lead_days is not None else DEFAULT_SUPPLIER_LEAD_DAYS_EST
        ship = shipping_days if shipping_days is not None else DEFAULT_SHIP_DAYS_EST
        accept = acceptance_days if acceptance_days is not None else DEFAULT_ACCEPT_DAYS_EST
        inv = invoice_submit_days if invoice_submit_days is not None else 2
        gov = gov_payment_days if gov_payment_days is not None else DEFAULT_GOV_PAY_DAYS_EST
        estimated_days = int(lead) + int(ship) + int(accept) + int(inv) + int(gov)
        pay_conf = "MIXED_DEFAULTS"
        missing.append("PAYMENT_TIMING_PARTIAL")
    else:
        missing.append("TIME_TO_CASH_UNKNOWN")
        pay_conf = "TIME_TO_CASH_UNKNOWN"

    # Contribution after financing fees (freight already should be in acquisition path or separate)
    expected_contribution = None
    if award_or_selling is not None and acquisition_cost is not None and financing_fees is not None:
        expected_contribution = award_or_selling - acquisition_cost - financing_fees
    elif award_or_selling is not None and acquisition_cost is not None:
        # Gross spread only — not owner pocket
        expected_contribution = None  # keep None for verified; expose scenario separately
        gross_spread = award_or_selling - acquisition_cost
    else:
        gross_spread = None

    if award_or_selling is not None and acquisition_cost is not None:
        gross_spread = award_or_selling - acquisition_cost
    else:
        gross_spread = None

    # Cash realization to company (after financier repay if advance used)
    expected_cash_realization = None
    if award_or_selling is not None and financing_advance_pct is not None:
        advance = (award_or_selling * financing_advance_pct / Decimal("100")).quantize(MONEY)
        fee = financing_fees or Decimal("0")
        # Company keeps: contribution after fee; advance is temporary liquidity
        if expected_contribution is not None:
            expected_cash_realization = expected_contribution
        else:
            expected_cash_realization = None
            missing.append("CASH_REALIZATION_NEEDS_FEES")
    elif expected_contribution is not None:
        expected_cash_realization = expected_contribution

    return {
        "AWARD_OR_SELLING": _money_str(award_or_selling),
        "ACQUISITION_COST": _money_str(acquisition_cost),
        "SUPPLIER_DEPOSIT": _money_str(deposit),
        "UPFRONT_CASH_REQUIRED": _money_str(upfront_scenario),
        "OWNER_CASH_REQUIRED": owner_cash_required,
        "OWNER_CASH_AMOUNT": _money_str(owner_cash if not owner_cash_required else None)
        if not owner_cash_required
        else "UNKNOWN_OR_BLOCKED_WITHOUT_FINANCING",
        "GROSS_SPREAD_SCENARIO": _money_str(gross_spread),
        "EXPECTED_CONTRIBUTION": _money_str(expected_contribution),
        "EXPECTED_CASH_REALIZATION": _money_str(expected_cash_realization),
        "ESTIMATED_DAYS_TO_CASH": estimated_days,
        "CONFIRMED_DAYS_TO_CASH": confirmed_days,
        "PAYMENT_TIMING_CONFIDENCE": pay_conf,
        "FINANCING_STATUS": financing_status,
        "FINANCING_FEES": _money_str(financing_fees),
        "MISSING": missing,
        "NOTE": "Government Net terms are estimates, not guaranteed payment dates",
    }


def cash_flow_rank_score(cf: dict[str, Any], lane: dict[str, Any]) -> float:
    """Favor meaningful financeable profit arriving sooner — not largest award."""
    score = 0.0
    lane_name = lane.get("LANE")
    if lane_name == LANE_CASH:
        score += 40
    elif lane_name == LANE_GROWTH:
        score += 55
    elif lane_name == LANE_SCALE:
        score += 50  # large but not auto-first
    elif lane_name == LANE_PROVISIONAL:
        score += 10
    elif lane_name == LANE_BLOCKED:
        score -= 100

    days = cf.get("ESTIMATED_DAYS_TO_CASH")
    if isinstance(days, int):
        if days <= 30:
            score += 35
        elif days <= 60:
            score += 20
        elif days <= 90:
            score += 10
        else:
            score += 2
    else:
        score -= 5  # unknown timing

    if cf.get("FINANCING_STATUS") == FIN_CONFIRMED:
        score += 40
    elif cf.get("FINANCING_STATUS") == FIN_UNVERIFIED:
        score += 8
    elif cf.get("FINANCING_STATUS") == FIN_BLOCKED:
        score -= 50

    if cf.get("OWNER_CASH_REQUIRED"):
        score -= 25

    contrib = _dec(cf.get("EXPECTED_CONTRIBUTION") or lane.get("EXPECTED_CONTRIBUTION"))
    if contrib is not None:
        # modest boost for size within lane, not dominant
        score += min(20.0, float(contrib) / 1000.0)
    return score


# ---------------------------------------------------------------------------
# Historical recovery (broader cache)
# ---------------------------------------------------------------------------


def _norm_buyer(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def load_historical_indexes() -> dict[str, Any]:
    """Build buyer/title indexes from persisted historical corpora (no SAM)."""
    by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_title_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources_loaded: list[str] = []

    def _ingest_order(buyer: str, order: dict[str, Any], source: str) -> None:
        amt = order.get("GOVERNMENT_TOTAL") or order.get("award_amount") or order.get("TOTAL")
        unit = order.get("HISTORICAL_UNIT_PRICE") or order.get("unit_price")
        # Prefer line-level exact if single-line with amount/qty
        lines = order.get("LINES") or order.get("lines") or []
        status = "HISTORICAL_PRICE_UNKNOWN"
        unit_price = None
        award_amount = _dec(amt)
        if unit is not None:
            unit_price = _dec(unit)
            status = "EXACT_HISTORICAL_UNIT_PRICE"
        elif len(lines) == 1 and isinstance(lines[0], dict):
            la = _dec(lines[0].get("HISTORICAL_LINE_AMOUNT") or lines[0].get("amount"))
            qty = _dec(lines[0].get("QTY") or lines[0].get("qty"))
            if la is not None and qty and qty > 0:
                unit_price = (la / qty).quantize(MONEY)
                status = "EXACT_HISTORICAL_UNIT_PRICE"
            elif la is not None:
                award_amount = la
                status = "TOTAL_AWARD_ONLY"
        elif award_amount is not None:
            status = "TOTAL_AWARD_ONLY"
        rec = {
            "buyer": buyer,
            "title": order.get("TITLE") or order.get("title"),
            "award_amount": _money_str(award_amount),
            "unit_price": _money_str(unit_price),
            "STATUS": status,
            "source_url": order.get("SOURCE_URL") or order.get("source_url"),
            "order_date": order.get("ORDER_DATE") or order.get("award_date"),
            "vendor": order.get("HISTORICAL_VENDOR") or order.get("vendor"),
            "source_system": order.get("SOURCE_SYSTEM") or source,
            "line_count": order.get("LINE_COUNT") or len(lines),
            "NOTE": "Historical benchmark — not assumed current win price",
        }
        bn = _norm_buyer(buyer)
        if bn:
            by_buyer[bn].append(rec)
        for tok in re.findall(r"[a-z0-9]{4,}", str(rec.get("title") or "").lower())[:8]:
            by_title_token[tok].append(rec)

    # historical profit proof v2
    for fname in (
        "m3_historical_profit_proof_v2_buyers.json",
        "m3_multibuyer_historical_profit_proof_v1_buyers.json",
        "m3_gometro_historical_profit_proof_v1_report.json",
        "m3_historical_award_expansion_v1_candidates.json",
        "m3_dla_historical_awards.json",
    ):
        blob = _load(fname)
        if not blob:
            continue
        sources_loaded.append(fname)
        buyers = blob.get("buyers") if isinstance(blob, dict) else None
        if isinstance(buyers, list):
            for b in buyers:
                if not isinstance(b, dict):
                    continue
                buyer = str(b.get("BUYER") or b.get("BUYER_NAME") or b.get("AGENCY") or "")
                for order in b.get("orders") or b.get("ORDERS") or []:
                    if isinstance(order, dict):
                        _ingest_order(buyer, order, fname)
                # some files put awards at buyer level
                if b.get("GOVERNMENT_TOTAL") or b.get("award_amount"):
                    _ingest_order(buyer, b, fname)
        elif isinstance(blob, dict) and isinstance(blob.get("candidates"), list):
            for c in blob["candidates"]:
                if isinstance(c, dict):
                    _ingest_order(str(c.get("buyer") or c.get("BUYER") or ""), c, fname)
        elif isinstance(blob, list):
            for c in blob[:5000]:
                if isinstance(c, dict):
                    _ingest_order(str(c.get("buyer") or c.get("BUYER") or ""), c, fname)

    return {
        "by_buyer": dict(by_buyer),
        "by_title_token": {k: v[:50] for k, v in by_title_token.items()},
        "sources_loaded": sources_loaded,
        "buyer_count": len(by_buyer),
        "built_at": _utc(),
    }


def match_historical(buyer: str, title: str, index: dict[str, Any]) -> dict[str, Any] | None:
    bn = _norm_buyer(buyer)
    cands = list((index.get("by_buyer") or {}).get(bn) or [])
    if not cands:
        # fuzzy: substring buyer key
        for k, rows in (index.get("by_buyer") or {}).items():
            if bn and (bn in k or k in bn):
                cands.extend(rows[:20])
    if not cands:
        tokens = re.findall(r"[a-z0-9]{4,}", (title or "").lower())
        scored: list[tuple[int, dict[str, Any]]] = []
        for tok in tokens[:6]:
            for row in (index.get("by_title_token") or {}).get(tok) or []:
                scored.append((1, row))
        if scored:
            # need ≥2 token hits for comparable
            counts: dict[int, int] = defaultdict(int)
            row_map: dict[int, dict[str, Any]] = {}
            for _, row in scored:
                rid = id(row)
                counts[rid] += 1
                row_map[rid] = row
            best = max(counts.items(), key=lambda x: x[1]) if counts else None
            if best and best[1] >= 2:
                row = dict(row_map[best[0]])
                if row.get("STATUS") == "TOTAL_AWARD_ONLY":
                    row["STATUS"] = "COMPARABLE_HISTORICAL_PRICE"
                return row
        return None
    # Prefer exact unit price, then total award, same-ish title tokens
    title_toks = set(re.findall(r"[a-z0-9]{4,}", (title or "").lower()))
    def _score(r: dict[str, Any]) -> tuple:
        rt = set(re.findall(r"[a-z0-9]{4,}", str(r.get("title") or "").lower()))
        overlap = len(title_toks & rt)
        unit_bonus = 10 if r.get("unit_price") else 0
        return (unit_bonus + overlap, 1 if r.get("STATUS") == "EXACT_HISTORICAL_UNIT_PRICE" else 0)

    cands.sort(key=_score, reverse=True)
    best = dict(cands[0])
    if best.get("unit_price"):
        best["STATUS"] = best.get("STATUS") or "EXACT_HISTORICAL_UNIT_PRICE"
    elif best.get("award_amount"):
        # multi-line caution
        if int(best.get("line_count") or 0) > 1:
            best["STATUS"] = "TOTAL_AWARD_ONLY"
            best["NOTE"] = "Multi-line historical award total — not per-item selling price"
        else:
            best["STATUS"] = "TOTAL_AWARD_ONLY"
    return best


# ---------------------------------------------------------------------------
# Repeat-buyer intelligence
# ---------------------------------------------------------------------------


def build_repeat_buyers(ht_rows: list[dict[str, Any]], hist_index: dict[str, Any]) -> dict[str, Any]:
    profiles_blob = _load("m3_buyer_trend_profiles_v1.json") or {}
    profiles = profiles_blob.get("profiles") if isinstance(profiles_blob, dict) else []
    repeat_profiles = [
        p
        for p in (profiles or [])
        if isinstance(p, dict) and int(p.get("TOTAL_VALIDATED_ORDERS") or 0) >= 2
    ]
    # Cross-link open quoteable opps by buyer name
    open_by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ht_rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        bn = _norm_buyer(str(r.get("BUYER") or ""))
        if bn:
            open_by_buyer[bn].append(
                {
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "TITLE": r.get("TITLE"),
                    "DEADLINE": r.get("DEADLINE"),
                    "QUOTEABLE_LINES": r.get("QUOTEABLE_LINES"),
                }
            )

    results: list[dict[str, Any]] = []
    for p in sorted(repeat_profiles, key=lambda x: -int(x.get("TOTAL_VALIDATED_ORDERS") or 0)):
        name = str(p.get("NORMALIZED_NAME") or p.get("BUYER_NAME") or "")
        bn = _norm_buyer(name)
        hist_orders = (hist_index.get("by_buyer") or {}).get(bn) or []
        # Also try buyer_name variants
        if not hist_orders:
            for k, rows in (hist_index.get("by_buyer") or {}).items():
                if bn and (bn in k or k in bn):
                    hist_orders = rows
                    break
        open_opps = []
        for k, rows in open_by_buyer.items():
            if bn and (bn in k or k in bn or any(t in k for t in bn.split() if len(t) > 3)):
                open_opps.extend(rows)
        # Dedup open
        seen = set()
        uniq_open = []
        for o in open_opps:
            oid = o.get("OPPORTUNITY_ID")
            if oid in seen:
                continue
            seen.add(oid)
            uniq_open.append(o)
        if int(p.get("TOTAL_VALIDATED_ORDERS") or 0) < 2:
            continue  # never invent recurrence from single award
        results.append(
            {
                "BUYER_ID": p.get("BUYER_ID"),
                "BUYER": name,
                "VALIDATED_ORDERS": p.get("TOTAL_VALIDATED_ORDERS"),
                "VALIDATED_SPEND": p.get("TOTAL_VALIDATED_SPEND"),
                "PORTAL": p.get("KNOWN_PORTAL"),
                "HISTORICAL_AWARDS_INDEXED": len(hist_orders),
                "OPEN_QUOTEABLE_OPPS": uniq_open[:15],
                "OPEN_COUNT": len(uniq_open),
                "RECURRENCE": "CONFIRMED_MULTI_ORDER",
                "CATEGORIES_HINT": None,
            }
        )

    results.sort(key=lambda r: (-int(r.get("OPEN_COUNT") or 0), -int(r.get("VALIDATED_ORDERS") or 0)))
    return {
        "kind": "revenue_first_repeat_buyers",
        "build": BUILD,
        "built_at": _utc(),
        "REPEAT_BUYER_COUNT": len(results),
        "WITH_OPEN_OPPS": sum(1 for r in results if r.get("OPEN_COUNT")),
        "BUYERS": results[:80],
    }


# ---------------------------------------------------------------------------
# Supplier-first + relationship memory
# ---------------------------------------------------------------------------


def build_supplier_first(rematch: dict[str, Any], packet_recon: dict[str, Any], ht_by_id: dict[str, Any]) -> dict[str, Any]:
    by_sup: dict[str, dict[str, Any]] = {}

    def _ensure(sup: str) -> dict[str, Any]:
        if sup not in by_sup:
            meta = SUPPLIER_META.get(sup) or {}
            by_sup[sup] = {
                "SUPPLIER": sup,
                "CHANNEL_KEY": meta.get("KEY"),
                "CHANNEL_CLASS": meta.get("CHANNEL_CLASS") or "GENERAL_COMMERCIAL",
                "CATALOG_URL": meta.get("CATALOG_URL"),
                "CONTACT": resolve_contact(sup, meta.get("KEY")),
                "PRODUCT_NOUNS": sorted(meta.get("PRODUCT_NOUNS") or [])[:40],
                "BRANDS": sorted(meta.get("BRANDS") or [])[:40],
                "RESELLER_ELIGIBILITY": "UNKNOWN",
                "AUTHORIZED_DISTRIBUTOR": bool(meta.get("REQUIRE_BRAND_FOR_FULL")),
                "PUBLIC_PRICING": "UNKNOWN",
                "QUOTED_ACCOUNT_PRICING": "UNKNOWN",
                "PROJECT_PRICING_ELIGIBILITY": "UNKNOWN",
                "PAYMENT_TERMS": "UNKNOWN",
                "OPPORTUNITIES": [],
            }
        return by_sup[sup]

    for p in (rematch.get("PACKETS") or []) + (packet_recon.get("ACTIVE_PACKETS") or []):
        if not isinstance(p, dict):
            continue
        sup = str(p.get("SUPPLIER") or "")
        if not sup:
            continue
        bucket = _ensure(sup)
        oid = str(p.get("OPPORTUNITY_ID") or "")
        if any(o.get("OPPORTUNITY_ID") == oid for o in bucket["OPPORTUNITIES"]):
            continue
        row = ht_by_id.get(oid) or {}
        bucket["OPPORTUNITIES"].append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": row.get("TITLE"),
                "BUYER": row.get("BUYER"),
                "DEADLINE": row.get("DEADLINE"),
                "SUPPORTED_LINES": p.get("SUPPORTED_LINES"),
                "PRODUCT_FIT": row.get("PRODUCT_FIT"),
            }
        )

    ranked = sorted(by_sup.values(), key=lambda s: (-len(s["OPPORTUNITIES"]), s["SUPPLIER"]))
    for s in ranked:
        # Score commercial attractiveness: open + not excluded
        attractive = [
            o
            for o in s["OPPORTUNITIES"]
            if str(o.get("PRODUCT_FIT")) != "EXCLUDED"
            and not re.search(r"\b(services?|construction|installation)\b", str(o.get("TITLE") or ""), re.I)
        ]
        s["ATTRACTIVE_OPEN_COUNT"] = len(attractive)
        s["ATTRACTIVE_OPPORTUNITIES"] = attractive[:25]
    ranked.sort(key=lambda s: (-int(s.get("ATTRACTIVE_OPEN_COUNT") or 0), -len(s["OPPORTUNITIES"])))
    return {
        "kind": "revenue_first_supplier_first",
        "build": BUILD,
        "built_at": _utc(),
        "SUPPLIER_COUNT": len(ranked),
        "SUPPLIERS": ranked,
        "TOP": [
            {
                "SUPPLIER": s["SUPPLIER"],
                "TOTAL_OPPS": len(s["OPPORTUNITIES"]),
                "ATTRACTIVE_OPEN": s.get("ATTRACTIVE_OPEN_COUNT"),
                "CONTACT_STATE": (s.get("CONTACT") or {}).get("CONTACT_STATE"),
            }
            for s in ranked[:15]
        ],
    }


def update_supplier_memory(
    supplier_first: dict[str, Any],
    pilot: list[dict[str, Any]],
    outreach: dict[str, Any],
) -> dict[str, Any]:
    prior = _load(MEMORY_FILE) or _load("m3_supplier_intelligence_memory_v1.json") or {}
    by_sup = dict(prior.get("by_supplier") or {}) if isinstance(prior, dict) else {}

    for s in supplier_first.get("SUPPLIERS") or []:
        key = s["SUPPLIER"]
        entry = dict(by_sup.get(key) or {})
        entry.update(
            {
                "supplier": key,
                "channel_key": s.get("CHANNEL_KEY"),
                "categories": s.get("PRODUCT_NOUNS"),
                "brands": s.get("BRANDS"),
                "contact": s.get("CONTACT"),
                "opportunity_ids": [o["OPPORTUNITY_ID"] for o in (s.get("OPPORTUNITIES") or [])][:200],
                "reseller_eligibility": entry.get("reseller_eligibility") or "UNKNOWN",
                "payment_terms": entry.get("payment_terms") or "UNKNOWN",
                "project_pricing_eligibility": entry.get("project_pricing_eligibility") or "UNKNOWN",
                "quotes": list(entry.get("quotes") or []),
                "purchases": list(entry.get("purchases") or []),
                "updated_at": _utc(),
            }
        )
        by_sup[key] = entry

    # Attach pilot public prices as research observations (not supplier quotes)
    for row in pilot:
        for sup in row.get("Suppliers") or []:
            entry = by_sup.setdefault(sup, {"supplier": sup, "quotes": [], "purchases": []})
            retail = row.get("Current_Retail") or {}
            if retail.get("unit_price") is not None:
                obs = {
                    "kind": "PUBLIC_PRICE_OBSERVATION",
                    "opportunity_id": row.get("OPPORTUNITY_ID"),
                    "unit_price": retail.get("unit_price"),
                    "evidence_level": retail.get("evidence_level"),
                    "source_url": retail.get("source_url"),
                    "checked_at": retail.get("checked_at") or _utc(),
                    "expires_at": None,
                    "NOTE": "Public list observation — not a supplier quote",
                }
                quotes = list(entry.get("quotes") or [])
                # idempotent by opp+url
                sig = f"{obs['opportunity_id']}|{obs.get('source_url')}"
                if not any(f"{q.get('opportunity_id')}|{q.get('source_url')}" == sig for q in quotes):
                    quotes.append(obs)
                entry["quotes"] = quotes[-50:]
                by_sup[sup] = entry

    # Outreach draft markers
    for s in outreach.get("SUPPLIERS") or []:
        key = s.get("SUPPLIER")
        if not key:
            continue
        entry = by_sup.setdefault(key, {"supplier": key, "quotes": [], "purchases": []})
        entry["last_rfq_draft_at"] = _utc()
        entry["last_rfq_opp_count"] = len(s.get("OPPORTUNITIES") or [])
        by_sup[key] = entry

    out = {
        "kind": "revenue_first_supplier_memory",
        "build": BUILD,
        "updated_at": _utc(),
        "by_supplier": by_sup,
        "supplier_count": len(by_sup),
    }
    _save(MEMORY_FILE, out)
    return out


def find_reusable_quotes(supplier: str, *, opportunity_id: str | None = None) -> list[dict[str, Any]]:
    mem = _load(MEMORY_FILE) or {}
    entry = (mem.get("by_supplier") or {}).get(supplier) or {}
    now = now_utc()
    usable = []
    for q in entry.get("quotes") or []:
        exp = q.get("expires_at")
        if exp:
            try:
                dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if dt < now:
                    continue  # expired
            except Exception:
                pass
        if q.get("kind") == "ACTUAL_SUPPLIER_QUOTE" or q.get("evidence_level") == "ACTUAL_SUPPLIER_QUOTE":
            usable.append(q)
        elif opportunity_id and q.get("opportunity_id") == opportunity_id:
            usable.append(q)
    return usable


# ---------------------------------------------------------------------------
# Focused RFQ outreach (5–10 per supplier)
# ---------------------------------------------------------------------------


def build_focused_outreach(
    supplier_first: dict[str, Any],
    pilot_by_id: dict[str, Any],
    *,
    per_supplier: int = 8,
) -> dict[str, Any]:
    out_suppliers: list[dict[str, Any]] = []
    for s in supplier_first.get("SUPPLIERS") or []:
        attractive = list(s.get("ATTRACTIVE_OPPORTUNITIES") or s.get("OPPORTUNITIES") or [])
        # Rank by deadline proximity + pilot evidence
        def _opp_score(o: dict[str, Any]) -> float:
            sc = 0.0
            oid = o.get("OPPORTUNITY_ID")
            pr = pilot_by_id.get(oid) or {}
            if (pr.get("Current_Retail") or {}).get("unit_price") is not None:
                sc += 50
            if (pr.get("Past_Gov_Price") or {}).get("unit_price") or (pr.get("Past_Gov_Price") or {}).get("award_amount"):
                sc += 30
            dl = o.get("DEADLINE") or pr.get("Deadline")
            try:
                if dl:
                    dt = datetime.fromisoformat(str(dl).replace("Z", "+00:00"))
                    days = (dt - now_utc()).total_seconds() / 86400.0
                    if 3 <= days <= 21:
                        sc += 25
                    elif days > 21:
                        sc += 10
                    elif 0 <= days < 3:
                        sc += 5
            except Exception:
                pass
            return sc

        attractive.sort(key=_opp_score, reverse=True)
        chosen = attractive[:per_supplier]
        if not chosen:
            continue
        rfq_items = []
        for o in chosen:
            oid = o["OPPORTUNITY_ID"]
            pr = pilot_by_id.get(oid) or {}
            targets = (pr.get("Max_Supplier_Cost_Targets") or {}).get("TARGETS") or {}
            rfq_items.append(
                {
                    "OPPORTUNITY_ID": oid,
                    "SOLICITATION_TITLE": o.get("TITLE") or pr.get("Product"),
                    "BUYER": o.get("BUYER") or pr.get("Buyer"),
                    "DEADLINE": o.get("DEADLINE") or pr.get("Deadline"),
                    "PRODUCTS": o.get("TITLE") or pr.get("Product"),
                    "QUANTITY_UOM": "See solicitation / line schedule — confirm before quote",
                    "DESTINATION": "See solicitation delivery location",
                    "REQUESTED": [
                        "Exact delivered product pricing",
                        "Freight to delivery destination",
                        "Stock / lead time",
                        "Reseller / new-account eligibility",
                        "Payment terms",
                        "Quote validity period",
                        "Project/deviated/special bid pricing if available",
                        "Direct-to-government delivery capability",
                    ],
                    "INTERNAL_MAX_TARGETS": {
                        k: targets.get(k)
                        for k in (
                            "LANE_CASH_FLOW_500",
                            "LANE_GROWTH_5000",
                            "LANE_SCALE_15000",
                            "PROFIT_5K",
                            "PROFIT_10K",
                        )
                        if k in targets
                    },
                    "DO_NOT_DISCLOSE_INTERNAL_CEILINGS": True,
                    "PROJECT_PRICING_REQUEST": True,
                }
            )
        # Deduplicate identical product titles
        seen_titles: set[str] = set()
        deduped = []
        for item in rfq_items:
            t = str(item.get("PRODUCTS") or "").strip().lower()
            if t in seen_titles:
                continue
            seen_titles.add(t)
            deduped.append(item)
        contact = s.get("CONTACT") or {}
        out_suppliers.append(
            {
                "SUPPLIER": s["SUPPLIER"],
                "CONTACT": contact,
                "FORM_URL": contact.get("CONTACT_URL"),
                "OPPORTUNITY_COUNT_SELECTED": len(deduped),
                "OPPORTUNITIES": deduped,
                "RFQ_DRAFT": {
                    "TO": s["SUPPLIER"],
                    "SUBJECT": f"RFQ — government product resale quotes ({len(deduped)} solicitations)",
                    "BODY_POINTS": [
                        "We are requesting firm quotes for the listed government solicitations.",
                        "Please price exact products/specs; note approved equals if permitted.",
                        "Include freight, lead time, reseller eligibility, payment terms, quote validity.",
                        "Advise if project/deviated pricing is available for these bids.",
                    ],
                    "AUTO_SEND": False,
                },
                "AUTO_SEND": False,
            }
        )

    out_suppliers.sort(key=lambda s: -int(s.get("OPPORTUNITY_COUNT_SELECTED") or 0))
    return {
        "kind": "revenue_first_monday_outreach",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "SUPPLIER_COUNT": len(out_suppliers),
        "SUPPLIERS": out_suppliers[:12],
        "PRIORITY_TOP": [
            {
                "SUPPLIER": s["SUPPLIER"],
                "SELECTED": s["OPPORTUNITY_COUNT_SELECTED"],
                "FORM_URL": s.get("FORM_URL"),
            }
            for s in out_suppliers[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Opportunity evaluation + pilot
# ---------------------------------------------------------------------------


def evaluate_opportunity(
    row: dict[str, Any],
    *,
    rematch_row: dict[str, Any] | None,
    hist_index: dict[str, Any],
    live_price: bool = True,
) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID") or "")
    title = str(row.get("TITLE") or "")
    buyer = str(row.get("BUYER") or "")
    identity = normalize_product_identity(title)
    fam = classify_family({}, title)
    match = best_supplier_for_line({"DESCRIPTION": title}, fam, None)
    suppliers: list[str] = []
    if rematch_row:
        suppliers = list((rematch_row.get("SUPPLIER_SET_AFTER") or {}).keys())
    if not suppliers and match.get("SUPPORTED") and match.get("SUPPLIER"):
        suppliers = [str(match["SUPPLIER"])]
    if row.get("SUPPLIER_SET"):
        suppliers = list(dict.fromkeys(suppliers + list((row.get("SUPPLIER_SET") or {}).keys())))

    retail = (
        research_public_price_bounded(oid, identity, title=title)
        if live_price
        else {"evidence_level": "PRICE_UNAVAILABLE", "unit_price": None}
    )
    historical = match_historical(buyer, title, hist_index) or {
        "STATUS": "HISTORICAL_PRICE_UNKNOWN"
    }

    unit_retail = _dec(retail.get("unit_price"))
    qty = _dec(identity.get("qty")) or Decimal("1")
    acquisition = (unit_retail * qty) if unit_retail is not None else None
    selling = _dec(historical.get("unit_price"))
    # Do not use multi-line TOTAL_AWARD_ONLY as item selling price
    if selling is None and historical.get("STATUS") == "TOTAL_AWARD_ONLY":
        selling_scenario = _dec(historical.get("award_amount"))
        selling_label = "TOTAL_AWARD_ONLY_SCENARIO"
    else:
        selling_scenario = selling
        selling_label = historical.get("STATUS") if selling is not None else "SELLING_PRICE_UNKNOWN"

    freight = None  # unknown
    financing_fees = None
    fin = financing_label_for_deal(contract_value=selling_scenario, supplier_cost=acquisition)
    cf = evaluate_cash_flow(
        award_or_selling=selling_scenario,
        acquisition_cost=acquisition,
        supplier_deposit=None,
        financing_fees=financing_fees,
        financing_status=fin,
        # leave timing unknown unless we have evidence
        inventory_lead_days=None,
        shipping_days=None,
        acceptance_days=None,
        invoice_submit_days=None,
        gov_payment_days=None,
    )
    # Evidence complete only when selling + acquisition + freight + financing fees known
    evidence_complete = (
        selling is not None
        and acquisition is not None
        and freight is not None
        and financing_fees is not None
        and fin == FIN_CONFIRMED
    )
    expected_contrib = cf.get("EXPECTED_CONTRIBUTION")
    expected_contrib_dec = _dec(expected_contrib)
    # Scenario gross spread for provisional lane hint only
    gross = _dec(cf.get("GROSS_SPREAD_SCENARIO"))

    qlines = int(row.get("QUOTEABLE_LINES") or 1)
    ml = multi_line_completeness(
        total_required=qlines,
        lines_identified=1 if int(row.get("REAL_LINES") or 0) > 0 else 0,
        lines_supplier_matched=len(suppliers),
        lines_price_verified=1 if unit_retail is not None else 0,
        lines_financially_covered=0,
        award_structure=str(row.get("AWARD_STRUCTURE") or ""),
    )
    lane = classify_profit_lane(
        expected_contribution=expected_contrib_dec,
        evidence_complete=evidence_complete,
        financing_status=fin,
        full_sourcing_ready=bool(ml.get("FULL_CONTRACT_SOURCING_READY")),
    )
    # If we only have gross spread, stay PROVISIONAL but attach scenario
    if lane["LANE"] == LANE_PROVISIONAL and gross is not None:
        lane["GROSS_SPREAD_SCENARIO"] = _money_str(gross)
        lane["SCENARIO_NOTE"] = "Gross spread only — freight/financing unknown; not verified contribution"

    targets = max_cost_targets_by_lane(
        selling_scenario,
        freight=freight,
        compliance=None,
        financing_fees=financing_fees,
    )
    advantage = supplier_advantage_score(
        supplier_count=len(suppliers),
        verified_acquisition=False,
        prior_purchase_price=bool(historical.get("unit_price")),
    )
    rank = cash_flow_rank_score(cf, lane)
    if unit_retail is not None:
        rank += 15
    if historical.get("unit_price") or (
        historical.get("STATUS") == "TOTAL_AWARD_ONLY" and historical.get("award_amount")
    ):
        rank += 12
    if len(suppliers) >= 2:
        rank += 8

    next_action = _next_action(retail, historical, suppliers, fin, ml)

    return {
        "OPPORTUNITY_ID": oid,
        "Product": title,
        "Buyer": buyer,
        "Deadline": row.get("DEADLINE"),
        "Family": fam,
        "Suppliers": suppliers,
        "Past_Gov_Price": historical,
        "Current_Retail": retail,
        "Supplier_Cost": {"STATUS": "UNKNOWN", "NOTE": "No actual supplier quote"},
        "Potential_Contribution": {
            "VERIFIED": _money_str(expected_contrib_dec),
            "GROSS_SPREAD_SCENARIO": _money_str(gross),
            "SELLING_LABEL": selling_label,
        },
        "Lane": lane,
        "Cash_Flow": cf,
        "Financing": fin,
        "Multi_Line": ml,
        "Max_Supplier_Cost_Targets": targets,
        "Supplier_Advantage": advantage,
        "Rank_Score": rank,
        "Evidence_Links": [
            u
            for u in [retail.get("source_url"), historical.get("source_url")]
            if u
        ],
        "Next_Action": next_action,
        "Workflow": [
            {"step": "Identify", "status": "complete" if int(row.get("REAL_LINES") or 0) > 0 else "incomplete"},
            {"step": "Source", "status": "complete" if suppliers else "incomplete"},
            {"step": "Price", "status": "complete" if unit_retail is not None else "incomplete"},
            {"step": "Finance", "status": "blocked" if fin != FIN_CONFIRMED else "complete"},
            {"step": "Bid", "status": "incomplete"},
            {"step": "Fulfill", "status": "incomplete"},
            {"step": "Get Paid", "status": "incomplete"},
        ],
    }


def _next_action(retail, historical, suppliers, fin, ml) -> str:
    if not suppliers:
        return "Source products — find legitimate suppliers"
    if not ml.get("FULL_CONTRACT_SOURCING_READY") and int(ml.get("TOTAL_REQUIRED_LINES") or 0) > 1:
        return f"Source remaining mandatory lines ({ml.get('LINES_SUPPLIER_MATCHED')}/{ml.get('TOTAL_REQUIRED_LINES')})"
    if retail.get("unit_price") is None:
        return "Research current retail / obtain distributor quote"
    if not historical or historical.get("STATUS") in {"HISTORICAL_PRICE_UNKNOWN", None}:
        return "Find historical award or bid tabulation"
    if fin in {FIN_UNKNOWN, FIN_UNVERIFIED}:
        return "Confirm funding path (PO finance / supplier terms) — do not mark confirmed"
    return "Prepare focused RFQ / bid review"


def select_revenue_pilot(rows: list[dict[str, Any]], rematch: dict[str, Any], *, n: int = 20) -> list[dict[str, Any]]:
    rematch_ids = set(rematch.get("NEW_MATCH_IDS") or [])
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        if str(r.get("PRODUCT_FIT")) == "EXCLUDED":
            continue
        title = str(r.get("TITLE") or "").lower()
        sc = 0.0
        oid = r.get("OPPORTUNITY_ID")
        if oid in rematch_ids or int(r.get("PACKET_COUNT") or 0) > 0:
            sc += 30
        if rematch_by.get(oid, {}).get("SUPPORTED_SUPPLIER_LINES_AFTER"):
            sc += 20
        for kw in ("janitorial", "paper towel", "toilet paper", "glove", "nitrile", "toner", "office"):
            if kw in title:
                sc += 45
        for kw in ("electrical", "lighting", "tool", "ppe", "filter", "fastener", "brake", "plumbing", "pipe", "hydrant"):
            if kw in title:
                sc += 15
        if re.search(r"\b(services?|construction|installation|camera|rubbish|widening)\b", title):
            sc -= 50
        scored.append((sc, r))
    scored.sort(key=lambda x: (-x[0], x[1].get("OPPORTUNITY_ID") or ""))
    return [r for _, r in scored[:n]]


def run_revenue_pilot(*, live_price: bool = True, n: int = 20) -> dict[str, Any]:
    t0 = time.time()
    rows = quoteable_rows()
    rematch = _load(REMATCH_FILE) or {"RESULTS": [], "NEW_MATCH_IDS": [], "PACKETS": []}
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    packet_recon = _load(PACKET_RECON_FILE) or {}
    ht_by_id = {r["OPPORTUNITY_ID"]: r for r in rows}

    log.info("loading historical indexes…")
    hist_index = load_historical_indexes()
    log.info("historical buyers indexed: %s sources=%s", hist_index.get("buyer_count"), hist_index.get("sources_loaded"))

    pilot_rows = select_revenue_pilot(rows, rematch, n=n)
    results: list[dict[str, Any]] = []
    for i, row in enumerate(pilot_rows, 1):
        log.info("revenue pilot %s/%s %s", i, len(pilot_rows), row.get("OPPORTUNITY_ID"))
        results.append(
            evaluate_opportunity(
                row,
                rematch_row=rematch_by.get(row["OPPORTUNITY_ID"]),
                hist_index=hist_index,
                live_price=live_price,
            )
        )
        _save(CHECKPOINT_FILE, {"stage": "pilot", "done": i, "total": len(pilot_rows), "at": _utc()})

    # Lane counts on pilot
    lane_counts = Counter(r["Lane"]["LANE"] for r in results)
    retail_ok = sum(1 for r in results if (r.get("Current_Retail") or {}).get("unit_price") is not None)
    hist_ok = sum(
        1
        for r in results
        if (r.get("Past_Gov_Price") or {}).get("unit_price")
        or (r.get("Past_Gov_Price") or {}).get("award_amount")
    )
    exact_retail = sum(
        1
        for r in results
        if (r.get("Current_Retail") or {}).get("evidence_level") == "EXACT_PUBLIC_PRICE_VERIFIED"
    )
    equiv_retail = sum(
        1
        for r in results
        if (r.get("Current_Retail") or {}).get("evidence_level") == "EQUIVALENT_PUBLIC_PRICE_VERIFIED"
    )

    pilot_by_id = {r["OPPORTUNITY_ID"]: r for r in results}
    supplier_first = build_supplier_first(rematch, packet_recon, ht_by_id)
    repeat_buyers = build_repeat_buyers(rows, hist_index)
    outreach = build_focused_outreach(supplier_first, pilot_by_id, per_supplier=8)
    memory = update_supplier_memory(supplier_first, results, outreach)

    # Extend evaluation lightly for top rematch attractions without full live price (reuse CI prices)
    ci_pilot = _load(CI_PILOT_FILE) or {}
    for r in ci_pilot.get("RESULTS") or []:
        oid = r.get("OPPORTUNITY_ID")
        if oid and oid not in pilot_by_id and (r.get("Current_Retail") or {}).get("unit_price"):
            # merge prior CI retail into memory already handled; skip duplicate eval
            pass

    deals = build_revenue_deals(rows, rematch_by, results, supplier_first)
    top20 = sorted(results, key=lambda r: -float(r.get("Rank_Score") or 0))[:20]

    # Full quoteable lane projection (provisional for most)
    lane_projection = project_lanes(rows, rematch_by, results)

    report = {
        "kind": "revenue_first_commercial_intelligence_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "DEPLOYMENT": "LOCAL_ONLY",
        "BASELINE_PRESERVED": {
            "CI_NEW_MATCHES_169": len(rematch.get("NEW_MATCH_IDS") or []),
            "QUOTEABLE_1003": len(rows),
            "PACKET_READY_AFTER_CI": packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER"),
        },
        "A_BEFORE_AFTER": {
            "BEFORE": {
                "QUOTEABLE": 1003,
                "PACKET_READY": 285,
                "UNMATCHED": 534,
                "RETAIL_PILOT": 4,
                "HISTORICAL_PILOT": 0,
                "VERIFIED_PROFIT": 0,
                "FINANCE_CONFIRMED": 0,
                "EXECUTION_READY": 0,
            },
            "AFTER": {
                "QUOTEABLE": len(rows),
                "PACKET_READY": packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER") or 285,
                "UNMATCHED": 534,
                "RETAIL_PILOT": retail_ok,
                "RETAIL_EXACT": exact_retail,
                "RETAIL_EQUIVALENT": equiv_retail,
                "HISTORICAL_PILOT": hist_ok,
                "VERIFIED_PROFIT": sum(1 for r in results if r["Lane"].get("VERIFICATION") == "VERIFIED"),
                "FINANCE_CONFIRMED": sum(1 for r in results if r.get("Financing") == FIN_CONFIRMED),
                "EXECUTION_READY": sum(
                    1
                    for r in results
                    if r["Lane"].get("VERIFICATION") == "VERIFIED"
                    and r.get("Financing") == FIN_CONFIRMED
                    and (r.get("Multi_Line") or {}).get("FULL_CONTRACT_SOURCING_READY")
                ),
                "REPEAT_BUYERS": repeat_buyers.get("REPEAT_BUYER_COUNT"),
                "REPEAT_WITH_OPEN": repeat_buyers.get("WITH_OPEN_OPPS"),
            },
        },
        "C_LANE_COUNTS": lane_projection,
        "E_SUPPLIER_FIRST": supplier_first.get("TOP"),
        "F_REPEAT_BUYERS": [
            {
                "BUYER": b.get("BUYER"),
                "ORDERS": b.get("VALIDATED_ORDERS"),
                "OPEN": b.get("OPEN_COUNT"),
                "SPEND": b.get("VALIDATED_SPEND"),
            }
            for b in (repeat_buyers.get("BUYERS") or [])[:15]
        ],
        "G_OUTREACH": outreach.get("PRIORITY_TOP"),
        "H_DEPLOYMENT": {
            "LOCAL_VALIDATED": True,
            "DEPLOYED": False,
            "TESTS": "see test_revenue_first_commercial_intelligence_v1.py",
        },
        "PILOT_LANE_COUNTS": dict(lane_counts),
        "HISTORICAL_INDEX": {
            "buyers": hist_index.get("buyer_count"),
            "sources": hist_index.get("sources_loaded"),
        },
    }

    _save(PILOT_FILE, {"kind": "revenue_first_pilot", "build": BUILD, "RESULTS": results, "count": len(results)})
    _save(LANES_FILE, lane_projection)
    _save(CASHFLOW_FILE, {"build": BUILD, "pilot_cashflows": [r.get("Cash_Flow") for r in results]})
    _save(SUPPLIER_FIRST_FILE, supplier_first)
    _save(REPEAT_BUYER_FILE, repeat_buyers)
    _save(OUTREACH_FILE, outreach)
    _save(DEALS_FILE, deals)
    _save(TOP20_FILE, {"build": BUILD, "TOP_20": top20})
    _save(REPORT_FILE, report)
    return {
        "report": report,
        "results": results,
        "top20": top20,
        "outreach": outreach,
        "supplier_first": supplier_first,
        "repeat_buyers": repeat_buyers,
        "deals": deals,
        "memory": memory,
    }


def project_lanes(
    rows: list[dict[str, Any]],
    rematch_by: dict[str, Any],
    pilot_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Lane census: pilot verified/partial + provisional for rest of quoteable with packets."""
    pilot_by = {r["OPPORTUNITY_ID"]: r for r in pilot_results}
    counts = {
        "CASH_FLOW_VERIFIED": 0,
        "GROWTH_VERIFIED": 0,
        "SCALE_VERIFIED": 0,
        "CASH_FLOW_PROVISIONAL": 0,
        "GROWTH_PROVISIONAL": 0,
        "SCALE_PROVISIONAL": 0,
        "PROVISIONAL_UNKNOWN": 0,
        "BELOW_THRESHOLD": 0,
        "BLOCKED": 0,
        "TOTAL_QUOTEABLE": 0,
    }
    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        counts["TOTAL_QUOTEABLE"] += 1
        oid = r["OPPORTUNITY_ID"]
        if oid in pilot_by:
            lane = pilot_by[oid]["Lane"]["LANE"]
            ver = pilot_by[oid]["Lane"].get("VERIFICATION")
            if lane == LANE_CASH and ver == "VERIFIED":
                counts["CASH_FLOW_VERIFIED"] += 1
            elif lane == LANE_GROWTH and ver == "VERIFIED":
                counts["GROWTH_VERIFIED"] += 1
            elif lane == LANE_SCALE and ver == "VERIFIED":
                counts["SCALE_VERIFIED"] += 1
            elif lane == LANE_CASH:
                counts["CASH_FLOW_PROVISIONAL"] += 1
            elif lane == LANE_GROWTH:
                counts["GROWTH_PROVISIONAL"] += 1
            elif lane == LANE_SCALE:
                counts["SCALE_PROVISIONAL"] += 1
            elif lane == LANE_BELOW:
                counts["BELOW_THRESHOLD"] += 1
            elif lane == LANE_BLOCKED:
                counts["BLOCKED"] += 1
            else:
                counts["PROVISIONAL_UNKNOWN"] += 1
        else:
            # Not priced — provisional unknown (NOT zero profit)
            if str(r.get("PRODUCT_FIT")) == "EXCLUDED":
                continue
            counts["PROVISIONAL_UNKNOWN"] += 1
    counts["NOTE"] = "UNKNOWN/PROVISIONAL is not classified as zero-profit"
    return counts


def build_revenue_deals(
    rows: list[dict[str, Any]],
    rematch_by: dict[str, Any],
    pilot_results: list[dict[str, Any]],
    supplier_first: dict[str, Any],
) -> dict[str, Any]:
    pilot_by = {r["OPPORTUNITY_ID"]: r for r in pilot_results}
    cards: list[dict[str, Any]] = []
    for r in rows:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        if str(r.get("PRODUCT_FIT")) == "EXCLUDED" and int(r.get("PACKET_COUNT") or 0) == 0 and oid not in rematch_by:
            continue
        pr = pilot_by.get(oid)
        rm = rematch_by.get(oid)
        suppliers = list((r.get("SUPPLIER_SET") or {}).keys())
        if rm:
            suppliers = list(dict.fromkeys(suppliers + list((rm.get("SUPPLIER_SET_AFTER") or {}).keys())))
        if pr:
            lane = pr["Lane"]
            cf = pr["Cash_Flow"]
            rank = pr["Rank_Score"]
            retail = (pr.get("Current_Retail") or {}).get("unit_price")
            hist = pr.get("Past_Gov_Price")
            fin = pr.get("Financing")
            next_action = pr.get("Next_Action")
            workflow = pr.get("Workflow")
            contrib = pr.get("Potential_Contribution")
        else:
            lane = {
                "LANE": LANE_PROVISIONAL,
                "LANE_STATUS": "PROVISIONAL",
                "VERIFICATION": "UNVERIFIED",
                "NOTE": "Not yet in priced pilot",
            }
            cf = {"PAYMENT_TIMING_CONFIDENCE": "TIME_TO_CASH_UNKNOWN", "OWNER_CASH_REQUIRED": True, "FINANCING_STATUS": FIN_UNKNOWN}
            rank = 5.0 + (10 if suppliers else 0)
            retail = None
            hist = "UNKNOWN"
            fin = FIN_UNKNOWN
            next_action = "Research current retail price" if suppliers else "Source products"
            workflow = [
                {"step": "Identify", "status": "incomplete"},
                {"step": "Source", "status": "complete" if suppliers else "incomplete"},
                {"step": "Price", "status": "incomplete"},
                {"step": "Finance", "status": "incomplete"},
                {"step": "Bid", "status": "incomplete"},
                {"step": "Fulfill", "status": "incomplete"},
                {"step": "Get Paid", "status": "incomplete"},
            ]
            contrib = {"VERIFIED": None}

        # Primary queue: exclude below-threshold unless strategic (has suppliers + open)
        if lane.get("LANE") == LANE_BELOW:
            continue

        cards.append(
            {
                "opportunity_id": oid,
                "product": r.get("TITLE"),
                "buyer": r.get("BUYER"),
                "deadline": r.get("DEADLINE"),
                "lane": lane.get("LANE"),
                "lane_status": lane.get("LANE_STATUS"),
                "lane_verification": lane.get("VERIFICATION"),
                "estimated_remaining_profit": (contrib or {}).get("VERIFIED") or "UNKNOWN",
                "profit_scenario": (contrib or {}).get("GROSS_SPREAD_SCENARIO"),
                "current_retail_cost": retail if retail is not None else "UNKNOWN",
                "historical_government_pricing": hist,
                "verified_supplier_cost": "UNKNOWN",
                "supplier_coverage": len(suppliers),
                "suppliers": suppliers[:6],
                "financing_status": fin,
                "estimated_days_to_cash": cf.get("ESTIMATED_DAYS_TO_CASH"),
                "payment_timing_confidence": cf.get("PAYMENT_TIMING_CONFIDENCE"),
                "owner_cash_required": cf.get("OWNER_CASH_REQUIRED"),
                "evidence_confidence": "HIGH"
                if lane.get("VERIFICATION") == "VERIFIED"
                else ("MEDIUM" if retail is not None else "LOW"),
                "next_action": next_action,
                "workflow": workflow,
                "rank_score": rank,
                "filter_tags": _filter_tags(lane, fin, retail, suppliers),
            }
        )
    cards.sort(key=lambda c: (-float(c.get("rank_score") or 0), c.get("deadline") or "9999"))
    return {
        "kind": "revenue_first_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(cards),
        "deals": cards[:150],
    }


def _filter_tags(lane, fin, retail, suppliers) -> list[str]:
    tags = ["all_viable"]
    ln = lane.get("LANE")
    if ln == LANE_CASH:
        tags.append("cash_flow")
    if ln == LANE_GROWTH:
        tags.append("growth")
    if ln == LANE_SCALE:
        tags.append("scale")
    if retail is None:
        tags.append("needs_pricing")
    if fin != FIN_CONFIRMED:
        tags.append("needs_financing")
    if lane.get("VERIFICATION") == "VERIFIED" and fin == FIN_CONFIRMED:
        tags.append("ready_to_bid")
    if suppliers and retail is not None and ln == LANE_PROVISIONAL:
        tags.append("needs_pricing")  # still needs selling/financing
    return tags


def run_upgrade(*, live_price: bool = True, pilot_n: int = 20) -> dict[str, Any]:
    return run_revenue_pilot(live_price=live_price, n=pilot_n)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_upgrade(live_price=True, pilot_n=20)
    print(json.dumps(out["report"]["A_BEFORE_AFTER"], indent=2))
    print("LANES", out["report"]["C_LANE_COUNTS"])
    print("OUTREACH", out["report"]["G_OUTREACH"])
