"""Correct the revenue gate: model proposed bids without published buyer budgets.

Isolated three-opportunity test (Liberty, Burks, paper).
Does NOT touch BidNet enrichment, L23 writes, production volume, or operator UI.
Never fabricates budgets, competitors, or historical prices.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc

BUILD = "20261010-m3-opengov-bid-economics-revenue-gate-v1"
OUT_DIR_NAME = "_og_commercial_id_val_out"

REPORT_FILE = "m3_opengov_bid_economics_revenue_gate_v1_report.json"
RESULTS_FILE = "m3_opengov_bid_economics_revenue_gate_v1_results.json"
TABLE_FILE = "m3_opengov_bid_economics_revenue_gate_v1_table.json"
PROGRESS_FILE = "m3_opengov_bid_economics_revenue_gate_v1_progress.json"

# Status ladder — unpublished buyer price ≠ automatic reject
STATUSES = {
    "COST_RESEARCH_INCOMPLETE",
    "ACQUISITION_COST_VERIFIED",
    "BID_PRICE_MODELED",
    "COMPETITIVENESS_UNVERIFIED",
    "COMPETITIVE_BID_CANDIDATE",
    "FINANCING_UNVERIFIED",
    "READY_FOR_OWNER_REVIEW",
    "EXECUTION_READY",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FINANCING_HARD_RULES = [
    "NO_OWNER_CASH_BEFORE_GOVERNMENT_PAYMENT",
    "NO_PERSONAL_CREDIT",
    "NO_PERSONAL_GUARANTEES",
    "POSITIVE_ESTIMATED_PROFIT_DOES_NOT_PROVE_FINANCING",
]


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    p = _out() / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def money_field(value: float | None, state: str, *, source: str | None = None, note: str | None = None) -> dict[str, Any]:
    """VALUE + STATE. UNKNOWN forces null. ESTIMATED keeps value but is not VERIFIED."""
    st = state if state in {"VERIFIED", "ESTIMATED", "PROPOSED", "UNKNOWN", "PROVEN"} else "UNKNOWN"
    if st == "UNKNOWN":
        return {"VALUE": None, "STATE": "UNKNOWN", "SOURCE": source, "NOTE": note}
    return {"VALUE": None if value is None else round(float(value), 2), "STATE": st, "SOURCE": source, "NOTE": note}


def bid_for_absolute_profit(execution_cost: float, profit: float) -> float:
    return round(execution_cost + profit, 2)


def bid_for_margin_on_revenue(execution_cost: float, margin: float) -> float:
    """profit / bid = margin → bid = cost / (1 - margin)."""
    if margin >= 1.0 or margin <= 0:
        raise ValueError("margin must be in (0,1)")
    return round(execution_cost / (1.0 - margin), 2)


def profit_at_bid(bid: float, execution_cost: float) -> float:
    return round(bid - execution_cost, 2)


def margin_at_bid(bid: float, execution_cost: float) -> float | None:
    if bid <= 0:
        return None
    return round((bid - execution_cost) / bid, 4)


# --- Seed facts from prior commercial research + live/solicitation evidence ---

THREE_OPPS: list[dict[str, Any]] = [
    {
        "KEY": "LIBERTY",
        "TITLE": "Liberty Sewage Pumps",
        "BUYER": "City of Tampa",
        "PORTAL_SLUG": "cityoftampa",
        "PROJECT_ID": "301110",
        "SOLICITATION_NUMBER": "26-P-00561",
        "SOLICITATION_URL": "https://procurement.opengov.com/portal/cityoftampa/projects/301110",
        "DOCUMENT_EVIDENCE_URL": "https://bqohpheioaljycjpwbjd.supabase.co/storage/v1/object/public/documents/documents/unique/8dd36cb6e587b67e891387070678666d3d3d27c63c6cf79f50f2e7852efeab94?download=Liberty-Sewage-Pumps.pdf",
        "LINES": [
            {
                "DESCRIPTION": "PUMP, SEWAGE, SUBMERSIBLE, LIBERTY LE51A-2 W/FLOAT",
                "QTY": 12,
                "UOM": "Each",
                "UNIT_COST": 459.10,
                "EXT": 5509.20,
                "COST_URL": "https://www.homedepot.com/p/Liberty-Pumps-LE-50-Series-1-2-HP-Submersible-Automatic-Sewage-Pump-25-ft-Cord-LE51A-2/330506462",
                "COST_STATE": "VERIFIED",
            },
            {
                "DESCRIPTION": "PUMP, SEWAGE, LIBERTY LE51M-2",
                "QTY": 1,
                "UOM": "Each",
                "UNIT_COST": 468.46,
                "EXT": 468.46,
                "COST_URL": "https://pump-stop.com/products/le51m-2",
                "COST_STATE": "VERIFIED",
            },
            {
                "DESCRIPTION": "PUMP, SUBMERSIBLE, LIBERTY 257-2 W/FLOAT",
                "QTY": 20,
                "UOM": "Each",
                "UNIT_COST": 232.50,
                "EXT": 4650.00,
                "COST_URL": "https://www.pollardwater.com/product/_/R-2370193",
                "COST_STATE": "VERIFIED",
            },
        ],
        "VERIFIED_ACQUISITION_COST": 10627.66,
        # FOB destination required; retail pages often free/unknown ship — label ESTIMATED
        "FREIGHT_ESTIMATE": 850.0,
        "FREIGHT_NOTE": "ESTIMATED LTL/pallet freight Tampa FL for ~33 pumps; not a carrier quote",
        "FEES_ESTIMATE": None,
        "FEES_NOTE": "No separate fee schedule found; shipping must be inside bid per RFQ",
        "AWARD_STRUCTURE": {
            "TYPE": "RFQ_AS_NEEDED_ONE_YEAR",
            "QUANTITIES_GUARANTEED": False,
            "QUANTITY_NOTE": "RFQ §2.2: quantities are estimates; City not required to purchase min/max",
            "SHIPPING_IN_BID": True,
            "DELIVERY": "FOB Destination within 5 days ARO",
            "PAYMENT": "Approx 30 days after acceptance (Florida Prompt Payment Act)",
            "AWARD_BASIS": "May award by division, line item, or aggregate; aggregate requires all lines quoted",
            "EVIDENCE": [
                "Liberty-Sewage-Pumps.pdf General Conditions 2.2–2.3, 2.9–2.10, 2.14",
                "https://bidscopeai.com/opportunities/5379e8bd-a359-493e-9f05-25b8b1dd43df",
            ],
        },
    },
    {
        "KEY": "BURKS",
        "TITLE": "Burks Centrifugal Pumps",
        "BUYER": "City of Tampa",
        "PORTAL_SLUG": "cityoftampa",
        "PROJECT_ID": "307080",
        "SOLICITATION_NUMBER": "26-P-00589",
        "SOLICITATION_URL": "https://procurement.opengov.com/portal/cityoftampa/projects/307080",
        "LINES": [
            {
                "DESCRIPTION": "PUMP,1 HP 230/460V 3 PH BURK T310WT5 TEFC MOTOR",
                "QTY": 10,
                "UOM": "Each",
                "UNIT_COST": 3140.25,
                "EXT": 31402.50,
                "COST_URL": "https://www.pumpagents.com/BurksPumps/T310WT5.html",
                "COST_STATE": "VERIFIED",
            },
        ],
        "VERIFIED_ACQUISITION_COST": 31402.50,
        "FREIGHT_ESTIMATE": 1200.0,
        "FREIGHT_NOTE": "ESTIMATED freight for 10 ~65 lb pump-motor units to Tampa; not a carrier quote",
        "FEES_ESTIMATE": None,
        "FEES_NOTE": "Shipping/handling/freight must be included in quote per solicitation summaries",
        "AWARD_STRUCTURE": {
            "TYPE": "RFQ_AS_NEEDED_ONE_YEAR",
            "QUANTITIES_GUARANTEED": False,
            "QUANTITY_NOTE": "Estimated qty 10; no min/max purchase commitment; as-needed over 1 year",
            "SHIPPING_IN_BID": True,
            "DELIVERY": "FOB Destination within 5 days ARO",
            "PAYMENT": "Approx 30 days after acceptance",
            "AWARD_BASIS": "May award by division, line item, or aggregate",
            "EVIDENCE": [
                "https://app.govly.com/public/opportunities/17198090",
                "https://www.cleat.ai/government/contracts/equipment-transport-and-delivery-cujk",
            ],
        },
    },
    {
        "KEY": "PAPER",
        "TITLE": "Paper Towels and Toilet Paper (Bid-Discount)",
        "BUYER": "City of Tampa",
        "PORTAL_SLUG": "cityoftampa",
        "PROJECT_ID": "313435",
        "SOLICITATION_NUMBER": "27-P-00004",
        "SOLICITATION_URL": "https://procurement.opengov.com/portal/cityoftampa/projects/313435",
        "LINES": [
            {
                "DESCRIPTION": "TOILET TISSUE, 2-PLY SCA #TM1616S, 3-Z BPP230",
                "QTY": 15360,
                "UOM": "Roll (s)",
                "UNIT_COST": 0.4942,
                "EXT": 7588.0,
                "COST_URL": "https://www.hecdistribution.com/shop/housekeeping-and-janitorial-products/paper-products/bathroom-tissue/essity-tork-bath-tissue-tm1616s/",
                "COST_STATE": "VERIFIED",
                "UOM_NOTE": "Public price $47.44/case of 96 → $0.4942/roll",
            },
            {
                "DESCRIPTION": "TOWELS, MULTI-FOLD UNBLEACHED ESSITY #MK520, 4M / CASE",
                "QTY": 1160,
                "UOM": "Case (s)",
                "UNIT_COST": 34.99,
                "EXT": 40588.4,
                "COST_URL": "https://www.webstaurantstore.com/tork-universal-natural-kraft-multifold-paper-towel-h2-case/500MFTMK520A.html",
                "COST_STATE": "VERIFIED",
                "UOM_NOTE": "Public listings MK520A treated as commercially acceptable equivalent to MK520",
            },
        ],
        "VERIFIED_ACQUISITION_COST": 48176.4,
        "FREIGHT_ESTIMATE": 2500.0,
        "FREIGHT_NOTE": "ESTIMATED bulk freight for tissue+towel cases to Tampa; not a carrier quote",
        "FEES_ESTIMATE": None,
        "FEES_NOTE": "Bid-Discount structure may price as discount %; fee schedule not separately verified",
        "PRIOR_COMPETITIVE_EVIDENCE": {
            "SOURCE": "https://www.floridabids.net/bid-result/paper__towels_and_toilet__bid_discount_-14433907",
            "NOTE": "Prior City of Tampa Paper Towels and Toilet Bid-Discount opening 2025-10-22",
            "APPARENT_LOW_BID_TOTAL": 24315.20,
            "OTHER_BIDS": [
                {"VENDOR": "United Sales USA Corp", "TOTAL": 24929.49},
                {"VENDOR": "Clean and Safe Global, Inc.", "TOTAL": 25252.00},
                {"VENDOR": "Dry 24 Water Restoration, LLC", "TOTAL": 27720.00},
            ],
            "SCOPE_MATCH": "SAME_CATEGORY_PRIOR_CYCLE_NOT_PROVEN_IDENTICAL_QTY",
            "CAUTION": "Prior total ~$24.3k vs current retail acquisition ~$48.2k — if scope/qty similar, retail cost is above prior competitive band",
        },
        "AWARD_STRUCTURE": {
            "TYPE": "BID_DISCOUNT_PURCHASE",
            "QUANTITIES_GUARANTEED": False,
            "QUANTITY_NOTE": "Schedule quantities from portal lines; guarantee not proven from public overview — treat as non-guaranteed until docs confirm",
            "SHIPPING_IN_BID": True,
            "DELIVERY": "UNKNOWN_DETAIL_PENDING_FULL_SPEC",
            "PAYMENT": "UNKNOWN_DETAIL_PENDING_FULL_SPEC",
            "AWARD_BASIS": "Bid-Discount competitive pricing",
            "EVIDENCE": ["OpenGov project 313435 financialId 27-P-00004"],
        },
    },
]


def fetch_live_status(slug: str, pid: str, *, client: httpx.Client) -> dict[str, Any]:
    try:
        from buyer_intelligence.opengov_live_deep_enrichment_v1 import (
            extract_value_fields,
            fetch_project_detail,
        )
    except Exception as exc:
        return {"STATE": "UNKNOWN", "REASON": type(exc).__name__}

    fr = fetch_project_detail(slug, pid, client=client, use_cache=False)
    if fr.get("route_status") != "PUBLIC_DETAIL_OK" or not fr.get("payload"):
        return {"STATE": "UNKNOWN", "REASON": fr.get("route_status"), "OPEN": None}
    payload = fr["payload"]
    val = extract_value_fields(payload)
    status = str(payload.get("status") or "").lower()
    deadline = payload.get("proposalDeadline")
    return {
        "STATE": "PROVEN",
        "OPEN": status == "open",
        "STATUS_RAW": payload.get("status"),
        "DEADLINE": deadline,
        "SOLICITATION_NUMBER": payload.get("financialId"),
        "TITLE": payload.get("title"),
        "PUBLISHED_BUDGET_OR_CEILING": (
            money_field(
                float((val.get("CURRENT_VALUE") or {}).get("VALUE")),
                "PROVEN",
                source="opengov_extract_value_fields",
            )
            if val.get("ECONOMIC_REVENUE_USABLE") == "YES" and (val.get("CURRENT_VALUE") or {}).get("VALUE")
            else money_field(
                None,
                "UNKNOWN",
                source="opengov_extract_value_fields",
                note="Absent published budget is normal for competitive RFQ — not a reject",
            )
        ),
        "ECONOMIC_REVENUE_USABLE": val.get("ECONOMIC_REVENUE_USABLE"),
    }


def search_historical_comparables(seed: dict[str, Any]) -> dict[str, Any]:
    """Read-only bid index — exact SKU first, then loose comparable (clearly labeled)."""
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import BidIndex

    idx = BidIndex(_canon() / "opengov_bid_price_index.json")
    idx.load()
    if not idx.loaded:
        return {
            "HISTORICAL_AWARD_PRICE": money_field(None, "UNKNOWN", note=idx.load_error or "INDEX_MISSING"),
            "MATCHES": [],
            "EVIDENCE_CLASS": "MISSING_EVIDENCE",
        }

    exact: list[dict[str, Any]] = []
    for ln in seed["LINES"]:
        desc = ln["DESCRIPTION"]
        # try part tokens
        tokens = []
        for tok in ("LE51A-2", "LE51M-2", "257-2", "T310WT5", "TM1616S", "MK520", "MK520A"):
            if tok.lower() in desc.lower() or tok.replace("-", "").lower() in desc.lower().replace("-", ""):
                tokens.append(tok)
        for tok in tokens:
            hit = idx.lookup(catalog=tok, model=tok, description=desc)
            if hit.get("HISTORICAL_STATE") == "PROVEN":
                exact.append({"LINE": desc[:80], "TOKEN": tok, **hit})

    # Loose category scan (not exact) — for competitiveness context only
    loose_keys = {
        "LIBERTY": ["sewage pump", "submersible pump"],
        "BURKS": ["centrifugal pump", "self priming"],
        "PAPER": ["toilet tissue", "paper towel", "multifold", "bath tissue"],
    }.get(seed["KEY"], [])
    loose: list[dict[str, Any]] = []
    for r in idx.records:
        blob = (str(r.get("line_description") or "")).lower()
        if not any(k in blob for k in loose_keys):
            continue
        try:
            price = float(r.get("bid_unit_price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        # Skip obvious construction lump sums
        if price > 20000 and seed["KEY"] in {"LIBERTY", "PAPER"}:
            continue
        loose.append(
            {
                "buyer": r.get("buyer"),
                "unit_price": price,
                "description": (r.get("line_description") or "")[:120],
                "source_url": r.get("source_url"),
                "award_date": r.get("award_date"),
                "MATCH_CLASS": "LOOSE_CATEGORY_NOT_EXACT_SKU",
            }
        )
        if len(loose) >= 8:
            break

    if exact:
        return {
            "HISTORICAL_AWARD_PRICE": money_field(exact[0]["HISTORICAL_GOV_PRICE"], "PROVEN", source=exact[0].get("HISTORICAL_AWARD_LINK")),
            "MATCHES": exact,
            "LOOSE_COMPARABLES": loose[:5],
            "EVIDENCE_CLASS": "EXACT_OR_TOKEN_MATCH",
        }
    return {
        "HISTORICAL_AWARD_PRICE": money_field(None, "UNKNOWN", note="No exact SKU award in read-only index"),
        "MATCHES": [],
        "LOOSE_COMPARABLES": loose[:5],
        "EVIDENCE_CLASS": "NO_EXACT_MATCH",
    }


def assess_financing_gate(*, acquisition: float, proposed_bid: float | None) -> dict[str, Any]:
    """Hard rules preserved — never CONFIRMED from profit alone."""
    return {
        "FINANCING_STATUS": "FINANCING_UNVERIFIED",
        "HARD_RULES": FINANCING_HARD_RULES,
        "OWNER_CASH_BEFORE_GOV_PAYMENT": "MUST_BE_ZERO_NOT_YET_PROVEN",
        "PERSONAL_CREDIT": "FORBIDDEN",
        "PERSONAL_GUARANTEE": "FORBIDDEN",
        "NOTE": (
            "City pays ~30 days after acceptance; supplier must be paid or floated until then. "
            "Positive modeled profit does not confirm financing availability."
        ),
        "ACQUISITION_TO_FLOAT": acquisition,
        "PROPOSED_BID": proposed_bid,
        "ASSUMED_AVAILABLE": False,
    }


def derive_status(row: dict[str, Any]) -> list[str]:
    """Ordered status tags — never reject solely for unpublished budget."""
    tags: list[str] = []
    if row["VERIFIED_ACQUISITION_COST"]["STATE"] != "VERIFIED":
        tags.append("COST_RESEARCH_INCOMPLETE")
        return tags
    tags.append("ACQUISITION_COST_VERIFIED")
    if row.get("BID_SCENARIOS"):
        tags.append("BID_PRICE_MODELED")
    comp = row.get("PRICE_COMPETITIVENESS") or {}
    if comp.get("STATUS") == "COMPETITIVE_RANGE_SUPPORTED":
        tags.append("COMPETITIVE_BID_CANDIDATE")
    else:
        tags.append("COMPETITIVENESS_UNVERIFIED")
    tags.append("FINANCING_UNVERIFIED")
    # Owner review allowed for modeled bids with disclosed unknowns — not execution-ready
    if "BID_PRICE_MODELED" in tags and row.get("LIVE", {}).get("OPEN") is True:
        tags.append("READY_FOR_OWNER_REVIEW")
    # EXECUTION_READY never without financing + supplier + compliance confirm
    return tags


def model_opportunity(seed: dict[str, Any], *, live: dict[str, Any], hist: dict[str, Any]) -> dict[str, Any]:
    acq = float(seed["VERIFIED_ACQUISITION_COST"])
    freight = seed.get("FREIGHT_ESTIMATE")
    fees = seed.get("FEES_ESTIMATE")

    verified_acq = money_field(acq, "VERIFIED", source="public_retail_research", note="Sourced product cost for full listed qty")
    freight_f = money_field(
        freight,
        "ESTIMATED" if freight is not None else "UNKNOWN",
        source="internal_estimate",
        note=seed.get("FREIGHT_NOTE"),
    )
    fees_f = money_field(fees, "UNKNOWN" if fees is None else "ESTIMATED", note=seed.get("FEES_NOTE"))
    financing_cost_f = money_field(None, "UNKNOWN", note="Financing cost unknown until facility confirmed")

    # Complete estimated execution = verified acq + estimated freight; unknown fees/financing disclosed
    exec_known = acq + (float(freight) if freight is not None else 0.0)
    exec_complete = money_field(
        exec_known,
        "ESTIMATED",
        note=(
            "Includes VERIFIED acquisition + ESTIMATED freight. "
            "Fees and financing cost are UNKNOWN and may increase required bids."
        ),
    )

    scenarios = {
        "MIN_BID_PROFIT_5000": {
            "PROPOSED_BID_PRICE": money_field(bid_for_absolute_profit(exec_known, 5000), "PROPOSED"),
            "ESTIMATED_PROFIT_IF_AWARDED": money_field(5000, "PROPOSED"),
            "MARGIN_ON_REVENUE": margin_at_bid(bid_for_absolute_profit(exec_known, 5000), exec_known),
        },
        "MIN_BID_PROFIT_10000": {
            "PROPOSED_BID_PRICE": money_field(bid_for_absolute_profit(exec_known, 10000), "PROPOSED"),
            "ESTIMATED_PROFIT_IF_AWARDED": money_field(10000, "PROPOSED"),
            "MARGIN_ON_REVENUE": margin_at_bid(bid_for_absolute_profit(exec_known, 10000), exec_known),
        },
        "MIN_BID_MARGIN_20PCT": {
            "PROPOSED_BID_PRICE": money_field(bid_for_margin_on_revenue(exec_known, 0.20), "PROPOSED"),
            "ESTIMATED_PROFIT_IF_AWARDED": money_field(
                profit_at_bid(bid_for_margin_on_revenue(exec_known, 0.20), exec_known), "PROPOSED"
            ),
            "MARGIN_ON_REVENUE": 0.20,
        },
        "MIN_BID_MARGIN_30PCT": {
            "PROPOSED_BID_PRICE": money_field(bid_for_margin_on_revenue(exec_known, 0.30), "PROPOSED"),
            "ESTIMATED_PROFIT_IF_AWARDED": money_field(
                profit_at_bid(bid_for_margin_on_revenue(exec_known, 0.30), exec_known), "PROPOSED"
            ),
            "MARGIN_ON_REVENUE": 0.30,
        },
    }

    # Competitiveness
    prior = seed.get("PRIOR_COMPETITIVE_EVIDENCE")
    hist_price = hist.get("HISTORICAL_AWARD_PRICE") or money_field(None, "UNKNOWN")
    competitiveness: dict[str, Any]
    if prior and prior.get("APPARENT_LOW_BID_TOTAL"):
        low = float(prior["APPARENT_LOW_BID_TOTAL"])
        high = max(float(b["TOTAL"]) for b in prior.get("OTHER_BIDS") or [{"TOTAL": low}])
        # If our execution cost exceeds prior winning band, cannot claim competitive profitable bid
        if exec_known > high:
            competitiveness = {
                "STATUS": "COMPETITIVE_PRICING_NOT_ESTABLISHED",
                "REASON": "VERIFIED_ACQUISITION_PLUS_FREIGHT_ABOVE_PRIOR_BID_BAND",
                "PRIOR_BID_BAND": {"LOW": low, "HIGH": high, "SOURCE": prior.get("SOURCE")},
                "OUR_EXECUTION_COST_ESTIMATED": exec_known,
                "GAP_VS_PRIOR_LOW": round(exec_known - low, 2),
                "COMPETITIVE_BID_RANGE": None,
                "NOTE": prior.get("CAUTION"),
                "SCOPE_MATCH": prior.get("SCOPE_MATCH"),
            }
        else:
            competitiveness = {
                "STATUS": "COMPETITIVE_RANGE_SUPPORTED",
                "PRIOR_BID_BAND": {"LOW": low, "HIGH": high, "SOURCE": prior.get("SOURCE")},
                "COMPETITIVE_BID_RANGE": {"LOW": low, "HIGH": high},
                "NOTE": "Prior-cycle totals only; qty/spec identity not proven identical",
            }
    elif hist.get("EVIDENCE_CLASS") == "EXACT_OR_TOKEN_MATCH" and hist_price.get("VALUE"):
        competitiveness = {
            "STATUS": "COMPETITIVE_RANGE_SUPPORTED",
            "HISTORICAL_UNIT": hist_price,
            "COMPETITIVE_BID_RANGE": {
                "NOTE": "Unit-level hist only — scale to qty carefully",
                "HIST_UNIT": hist_price.get("VALUE"),
            },
            "MATCHES": hist.get("MATCHES"),
        }
    else:
        competitiveness = {
            "STATUS": "COMPETITIVE_PRICING_NOT_ESTABLISHED",
            "REASON": "NO_EXACT_HISTORICAL_AWARD_AND_NO_PRIOR_BID_TAB_FOR_THIS_SCOPE",
            "COMPETITIVE_BID_RANGE": None,
            "LOOSE_COMPARABLES_ONLY": hist.get("LOOSE_COMPARABLES") or [],
            "NOTE": "Loose category hits in index are not usable as competitive unit evidence",
        }

    ref_bid = scenarios["MIN_BID_PROFIT_5000"]["PROPOSED_BID_PRICE"]["VALUE"]
    financing = assess_financing_gate(acquisition=acq, proposed_bid=ref_bid)

    blockers = []
    if live.get("OPEN") is not True:
        blockers.append("SOLICITATION_NOT_CONFIRMED_OPEN")
    if not seed["AWARD_STRUCTURE"].get("QUANTITIES_GUARANTEED"):
        blockers.append("QUANTITIES_NOT_GUARANTEED_AS_NEEDED_ESTIMATES")
    if freight_f["STATE"] != "VERIFIED":
        blockers.append("FREIGHT_NOT_VERIFIED_ESTIMATED_ONLY")
    if fees_f["STATE"] == "UNKNOWN":
        blockers.append("FEES_UNKNOWN")
    if financing_cost_f["STATE"] == "UNKNOWN":
        blockers.append("FINANCING_COST_UNKNOWN")
    blockers.append("FINANCING_NOT_CONFIRMED")
    if competitiveness.get("STATUS") != "COMPETITIVE_RANGE_SUPPORTED":
        blockers.append("COMPETITIVENESS_UNVERIFIED_OR_UNSUPPORTABLE")
    if competitiveness.get("REASON") == "VERIFIED_ACQUISITION_PLUS_FREIGHT_ABOVE_PRIOR_BID_BAND":
        blockers.append("RETAIL_COST_ABOVE_PRIOR_COMPETITIVE_BAND")
    blockers.append("SUPPLIER_TERMS_NOT_CONFIRMED")
    blockers.append("COMPLIANCE_PACKET_NOT_CONFIRMED")
    # Explicitly NOT a blocker:
    not_blockers = ["PUBLISHED_BUDGET_ABSENT_IS_NORMAL_FOR_OPEN_COMPETITIVE_RFQ"]

    row = {
        "KEY": seed["KEY"],
        "TITLE": seed["TITLE"],
        "BUYER": seed["BUYER"],
        "SOLICITATION_NUMBER": live.get("SOLICITATION_NUMBER") or seed["SOLICITATION_NUMBER"],
        "SOLICITATION_URL": seed["SOLICITATION_URL"],
        "CLOSING_DATE": live.get("DEADLINE"),
        "LIVE": live,
        "LINES": seed["LINES"],
        "AWARD_STRUCTURE": seed["AWARD_STRUCTURE"],
        "VERIFIED_ACQUISITION_COST": verified_acq,
        "ESTIMATED_EXECUTION_COST": {
            "FREIGHT": freight_f,
            "FEES": fees_f,
            "FINANCING_COST": financing_cost_f,
            "TOTAL_USED_FOR_BID_MODEL": exec_complete,
        },
        "HISTORICAL_AWARD_PRICE": hist_price,
        "HISTORICAL_SEARCH": {
            "EVIDENCE_CLASS": hist.get("EVIDENCE_CLASS"),
            "EXACT_MATCHES": hist.get("MATCHES") or [],
            "LOOSE_COMPARABLES": hist.get("LOOSE_COMPARABLES") or [],
        },
        "PUBLISHED_BUDGET_OR_CEILING": live.get("PUBLISHED_BUDGET_OR_CEILING")
        or money_field(None, "UNKNOWN", note="Not published"),
        "BID_SCENARIOS": scenarios,
        "PRICE_COMPETITIVENESS": competitiveness,
        "FINANCING": financing,
        "ESTIMATED_PROFIT_IF_AWARDED_NOTE": (
            "Profit figures assume full schedule quantities are ordered. "
            "Award structure is as-needed/estimate — actual purchases may be less."
        ),
        "BLOCKERS": blockers,
        "NOT_BLOCKERS": not_blockers,
        "PRIOR_COMPETITIVE_EVIDENCE": prior,
    }
    row["STATUS_TAGS"] = derive_status(row)
    row["PRIMARY_STATUS"] = (
        "READY_FOR_OWNER_REVIEW"
        if "READY_FOR_OWNER_REVIEW" in row["STATUS_TAGS"]
        and "RETAIL_COST_ABOVE_PRIOR_COMPETITIVE_BAND" not in blockers
        else (
            "BID_PRICE_MODELED"
            if "BID_PRICE_MODELED" in row["STATUS_TAGS"]
            else row["STATUS_TAGS"][0]
        )
    )
    # Paper with cost above prior band: still modeled, but not a competitive owner candidate
    if "RETAIL_COST_ABOVE_PRIOR_COMPETITIVE_BAND" in blockers:
        row["PRIMARY_STATUS"] = "BID_PRICE_MODELED"
        row["STATUS_TAGS"] = [t for t in row["STATUS_TAGS"] if t != "READY_FOR_OWNER_REVIEW"]
        row["STATUS_TAGS"].append("COMPETITIVENESS_UNVERIFIED")
    return row


def table_row(r: dict[str, Any]) -> dict[str, Any]:
    sc = r["BID_SCENARIOS"]
    comp = r.get("PRICE_COMPETITIVENESS") or {}
    return {
        "OPPORTUNITY": r["TITLE"],
        "SOLICITATION": r["SOLICITATION_NUMBER"],
        "CLOSING_DATE": r.get("CLOSING_DATE"),
        "OPEN": (r.get("LIVE") or {}).get("OPEN"),
        "QTY_SCOPE": [
            f"{ln['QTY']} {ln['UOM']} — {ln['DESCRIPTION'][:60]}" for ln in r.get("LINES") or []
        ],
        "QUANTITIES_GUARANTEED": r["AWARD_STRUCTURE"].get("QUANTITIES_GUARANTEED"),
        "VERIFIED_ACQUISITION_COST": r["VERIFIED_ACQUISITION_COST"],
        "ESTIMATED_EXECUTION_COST_TOTAL": r["ESTIMATED_EXECUTION_COST"]["TOTAL_USED_FOR_BID_MODEL"],
        "MIN_BID_5K_PROFIT": sc["MIN_BID_PROFIT_5000"]["PROPOSED_BID_PRICE"],
        "MIN_BID_10K_PROFIT": sc["MIN_BID_PROFIT_10000"]["PROPOSED_BID_PRICE"],
        "MIN_BID_20PCT_MARGIN": sc["MIN_BID_MARGIN_20PCT"]["PROPOSED_BID_PRICE"],
        "MIN_BID_30PCT_MARGIN": sc["MIN_BID_MARGIN_30PCT"]["PROPOSED_BID_PRICE"],
        "EXPECTED_PROFIT_AT_5K_SCENARIO": sc["MIN_BID_PROFIT_5000"]["ESTIMATED_PROFIT_IF_AWARDED"],
        "EXPECTED_PROFIT_AT_10K_SCENARIO": sc["MIN_BID_PROFIT_10000"]["ESTIMATED_PROFIT_IF_AWARDED"],
        "EXPECTED_PROFIT_AT_20PCT_SCENARIO": sc["MIN_BID_MARGIN_20PCT"]["ESTIMATED_PROFIT_IF_AWARDED"],
        "EXPECTED_PROFIT_AT_30PCT_SCENARIO": sc["MIN_BID_MARGIN_30PCT"]["ESTIMATED_PROFIT_IF_AWARDED"],
        "HISTORICAL_GOVERNMENT_PAID_PRICE": r.get("HISTORICAL_AWARD_PRICE"),
        "COMPETITIVE_BID_RANGE": comp.get("COMPETITIVE_BID_RANGE"),
        "COMPETITIVENESS_STATUS": comp.get("STATUS"),
        "PUBLISHED_BUDGET": r.get("PUBLISHED_BUDGET_OR_CEILING"),
        "FINANCING_STATUS": (r.get("FINANCING") or {}).get("FINANCING_STATUS"),
        "PRIMARY_STATUS": r.get("PRIMARY_STATUS"),
        "STATUS_TAGS": r.get("STATUS_TAGS"),
        "REMAINING_BLOCKERS": r.get("BLOCKERS"),
        "SOLICITATION_URL": r.get("SOLICITATION_URL"),
    }


def overlap_hist_vs_acquisition() -> dict[str, Any]:
    """Check overlap between hist-matched opps and fully acquisition-priced three."""
    audit_path = _out() / "m3_opengov_commercial_id_pricing_repair_v1_audit87.json"
    if not audit_path.exists():
        return {"STATE": "UNKNOWN", "NOTE": "Prior audit87 not found"}
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    hist_opps = [
        {"OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"), "TITLE": r.get("TITLE"), "HIST_LINES": r.get("LINES_WITH_HISTORICAL")}
        for r in (audit.get("rows") or [])
        if (r.get("LINES_WITH_HISTORICAL") or 0) > 0
    ]
    three_titles = {s["TITLE"] for s in THREE_OPPS}
    overlap = [h for h in hist_opps if h.get("TITLE") in three_titles]
    return {
        "HIST_MATCHED_OPP_COUNT": len(hist_opps),
        "FULL_ACQUISITION_THREE": sorted(three_titles),
        "OVERLAP_TITLES": [h["TITLE"] for h in overlap],
        "OVERLAP_COUNT": len(overlap),
        "NOTE": "Exact-SKU hist for Liberty/Burks/paper was UNKNOWN; any prior audit hist hits were loose and later tightened",
    }


def run_bid_economics_revenue_gate_v1() -> dict[str, Any]:
    t0 = time.time()
    run_id = f"og-bid-econ-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _save(PROGRESS_FILE, {"status": "RUNNING", "RUN_ID": run_id, "build": BUILD, "heartbeat_at": _utc()})

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for seed in THREE_OPPS:
            live = fetch_live_status(seed["PORTAL_SLUG"], seed["PROJECT_ID"], client=client)
            hist = search_historical_comparables(seed)
            row = model_opportunity(seed, live=live, hist=hist)
            results.append(row)
            time.sleep(0.2)

    table = [table_row(r) for r in results]
    _save(RESULTS_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results})
    _save(TABLE_FILE, {"build": BUILD, "RUN_ID": run_id, "rows": table})

    overlap = overlap_hist_vs_acquisition()

    report = {
        "BUILD": BUILD,
        "RUN_ID": run_id,
        "STATUS": "COMPLETE",
        "CREATED_AT": _utc(),
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "TOUCHED_PRODUCTION_VOLUME_HISTORY": False,
            "SAM_API_CALLS": 0,
            "OUTPUT_DIR": OUT_DIR_NAME,
        },
        "REVENUE_GATE_CORRECTION": {
            "RULE": "Missing published government revenue/budget does NOT block bid modeling",
            "PUBLISHED_BUDGET_ABSENT_COUNT": sum(
                1 for r in results if (r.get("PUBLISHED_BUDGET_OR_CEILING") or {}).get("STATE") == "UNKNOWN"
            ),
            "STILL_MODELED": len(results),
        },
        "PRICING_CONCEPTS": [
            "VERIFIED_ACQUISITION_COST",
            "ESTIMATED_EXECUTION_COST",
            "HISTORICAL_AWARD_PRICE",
            "PUBLISHED_BUDGET_OR_CEILING",
            "PROPOSED_BID_PRICE",
            "ESTIMATED_PROFIT_IF_AWARDED",
            "PRICE_COMPETITIVENESS",
            "FINANCING_STATUS",
        ],
        "STATUS_LADDER": sorted(STATUSES),
        "FINANCING_HARD_RULES": FINANCING_HARD_RULES,
        "HIST_VS_ACQUISITION_OVERLAP": overlap,
        "TABLE": table,
        "SUMMARY": {
            "ALL_THREE_OPEN": all((r.get("LIVE") or {}).get("OPEN") for r in results),
            "ALL_THREE_ACQUISITION_VERIFIED": all(
                (r.get("VERIFIED_ACQUISITION_COST") or {}).get("STATE") == "VERIFIED" for r in results
            ),
            "COMPETITIVE_BID_CANDIDATES": [
                r["TITLE"] for r in results if "COMPETITIVE_BID_CANDIDATE" in (r.get("STATUS_TAGS") or [])
            ],
            "OWNER_REVIEW_CANDIDATES": [
                r["TITLE"] for r in results if r.get("PRIMARY_STATUS") == "READY_FOR_OWNER_REVIEW"
            ],
            "EXECUTION_READY": [],
            "KEY_FINDING": (
                "Liberty and Burks can be bid-modeled from verified acquisition without a published budget; "
                "competitiveness remains unverified (no exact hist awards). "
                "Paper can be modeled but prior-cycle bid totals (~$24–28k) sit below current retail acquisition (~$48k), "
                "so a competitive profitable bid is not supported at verified retail cost. "
                "All three are as-needed/estimate quantity — profit-if-awarded assumes full schedule draw."
            ),
        },
        "OUTPUT_LOCATIONS": {
            "report": REPORT_FILE,
            "results": RESULTS_FILE,
            "table": TABLE_FILE,
        },
    }
    _save(REPORT_FILE, report)
    _save(PROGRESS_FILE, {"status": "DONE", "RUN_ID": run_id, "build": BUILD, "heartbeat_at": _utc()})
    return report


if __name__ == "__main__":
    rep = run_bid_economics_revenue_gate_v1()
    print(
        json.dumps(
            {
                "STATUS": rep.get("STATUS"),
                "SUMMARY": rep.get("SUMMARY"),
                "TABLE": [
                    {
                        "OPPORTUNITY": t["OPPORTUNITY"],
                        "OPEN": t["OPEN"],
                        "ACQ": t["VERIFIED_ACQUISITION_COST"],
                        "EXEC": t["ESTIMATED_EXECUTION_COST_TOTAL"],
                        "BID_5K": t["MIN_BID_5K_PROFIT"],
                        "BID_10K": t["MIN_BID_10K_PROFIT"],
                        "BID_20": t["MIN_BID_20PCT_MARGIN"],
                        "BID_30": t["MIN_BID_30PCT_MARGIN"],
                        "HIST": t["HISTORICAL_GOVERNMENT_PAID_PRICE"],
                        "COMP": t["COMPETITIVENESS_STATUS"],
                        "FIN": t["FINANCING_STATUS"],
                        "STATUS": t["PRIMARY_STATUS"],
                        "BLOCKERS": t["REMAINING_BLOCKERS"],
                    }
                    for t in (rep.get("TABLE") or [])
                ],
            },
            indent=2,
        )
    )
