"""Immediate two-deal execution validation: Liberty + Burks (City of Tampa).

Verifies RFQ packages, as-needed economics, executability, competitiveness.
Does NOT touch BidNet enrichment, L23, production history, or operator UI chrome.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from buyer_intelligence.as_needed_contract_screening_v1 import screen_as_needed_contract_text

BUILD = "20261010-m3-tampa-two-deal-execution-validation-v1"
OUT_DIR = "_og_commercial_id_val_out"

REPORT = "m3_tampa_two_deal_execution_validation_v1_report.json"
RESULTS = "m3_tampa_two_deal_execution_validation_v1_results.json"
PROGRESS = "m3_tampa_two_deal_execution_validation_v1_progress.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Proposed sell prices use 20% margin on revenue (bid = cost/0.8) — clearly PROPOSED
TARGET_MARGIN = 0.20
PROFIT_FLOOR = 5000.0
PROFIT_TARGET = 10000.0


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    p = _out() / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def money(value: float | None, state: str, **extra: Any) -> dict[str, Any]:
    st = state if state in {"VERIFIED", "ESTIMATED", "PROPOSED", "UNKNOWN", "PROVEN"} else "UNKNOWN"
    if st == "UNKNOWN":
        return {"VALUE": None, "STATE": "UNKNOWN", **extra}
    return {"VALUE": None if value is None else round(float(value), 2), "STATE": st, **extra}


DEALS: list[dict[str, Any]] = [
    {
        "KEY": "LIBERTY",
        "TITLE": "Liberty Sewage Pumps",
        "SOLICITATION": "26-P-00561",
        "PROJECT_ID": "301110",
        "SLUG": "cityoftampa",
        "URL": "https://procurement.opengov.com/portal/cityoftampa/projects/301110",
        "LINES": [
            {
                "SKU": "LE51A-2",
                "DESCRIPTION": "PUMP, SEWAGE, SUBMERSIBLE, LIBERTY LE51A-2 W/FLOAT",
                "QTY_EST": 12,
                "UOM": "Each",
                "UNIT_ACQ": 459.10,
                "ACQ_URL": "https://www.homedepot.com/p/Liberty-Pumps-LE-50-Series-1-2-HP-Submersible-Automatic-Sewage-Pump-25-ft-Cord-LE51A-2/330506462",
                "ACQ_STATE": "VERIFIED",
            },
            {
                "SKU": "LE51M-2",
                "DESCRIPTION": "PUMP, SEWAGE, LIBERTY LE51M-2",
                "QTY_EST": 1,
                "UOM": "Each",
                "UNIT_ACQ": 468.46,
                "ACQ_URL": "https://pump-stop.com/products/le51m-2",
                "ACQ_STATE": "VERIFIED",
            },
            {
                "SKU": "257-2",
                "DESCRIPTION": "PUMP, SUBMERSIBLE, LIBERTY 257-2 W/FLOAT",
                "QTY_EST": 20,
                "UOM": "Each",
                "UNIT_ACQ": 232.50,
                "ACQ_URL": "https://www.pollardwater.com/product/_/R-2370193",
                "ACQ_STATE": "VERIFIED",
            },
        ],
        # Per-unit freight ESTIMATED (FOB dest required; not carrier quote)
        "UNIT_FREIGHT_EST": {"LE51A-2": 35.0, "LE51M-2": 35.0, "257-2": 28.0},
        "STOCK_EVIDENCE": [
            {
                "SKU": "LE51A-2",
                "SOURCE": "https://www.bondsupply.com/buy/le51a-2-liberty/180276",
                "CLAIM": "Bond Supply FL branches list 16 ea in stock (Miami/Palm Bay/South Dade/Port St Lucie)",
                "STATE": "PROVEN_LISTING",
                "NOTE": "Stock listing ≠ reserved inventory for M3; Tampa-local TSC page showed sold out",
            },
            {
                "SKU": "LE51A-2",
                "SOURCE": "https://tsctampa.com/products/liberty-le51a-3",
                "CLAIM": "Technical Sales Corp Tampa lists LE51A-2 sold out; policy says stock ships 1-3 days",
                "STATE": "PROVEN_LISTING",
            },
            {
                "SKU": "257-2",
                "SOURCE": "https://www.bondsupply.com/buy/257-2-liberty/352350",
                "CLAIM": "Bond Supply lists 257-2 stock (4 ea across FL branches) at prior research",
                "STATE": "PROVEN_LISTING",
            },
        ],
    },
    {
        "KEY": "BURKS",
        "TITLE": "Burks Centrifugal Pumps",
        "SOLICITATION": "26-P-00589",
        "PROJECT_ID": "307080",
        "SLUG": "cityoftampa",
        "URL": "https://procurement.opengov.com/portal/cityoftampa/projects/307080",
        "LINES": [
            {
                "SKU": "T310WT5",
                "DESCRIPTION": "PUMP,1 HP 230/460V 3 PH BURK T310WT5 TEFC MOTOR",
                "QTY_EST": 10,
                "UOM": "Each",
                "UNIT_ACQ": 3140.25,
                "ACQ_URL": "https://www.pumpagents.com/BurksPumps/T310WT5.html",
                "ACQ_STATE": "VERIFIED",
            },
        ],
        "UNIT_FREIGHT_EST": {"T310WT5": 120.0},
        "STOCK_EVIDENCE": [
            {
                "SKU": "T310WT5",
                "SOURCE": "https://www.pumpagents.com/BurksPumps/T310WT5.html",
                "CLAIM": "PumpAgents lists Same Day (2 in stock) at $3140.25",
                "STATE": "PROVEN_LISTING",
                "NOTE": "Only 2 units listed — below estimated schedule of 10; 5-day ARO for all 10 not proven",
            },
            {
                "SKU": "T310WT5",
                "SOURCE": "https://tencarva.com/crane-pumps-systems/",
                "CLAIM": "Hudson Pump & Equipment (Tencarva) is authorized Crane/Burks distributor for Florida",
                "STATE": "PROVEN_LISTING",
                "NOTE": "Authorization path exists; M3 not confirmed as authorized reseller; lead times UNKNOWN",
            },
        ],
    },
]


def download_package_text(slug: str, pid: str, *, client: httpx.Client, key: str) -> dict[str, Any]:
    from buyer_intelligence.opengov_live_deep_enrichment_v1 import fetch_project_detail

    fr = fetch_project_detail(slug, pid, client=client, use_cache=False)
    payload = fr.get("payload") or {}
    da = payload.get("documentAttachment") or {}
    url = da.get("url")
    addenda = payload.get("addendums") or payload.get("addenda") or []
    meta = {
        "route": fr.get("route_status"),
        "status": payload.get("status"),
        "deadline": payload.get("proposalDeadline"),
        "financialId": payload.get("financialId"),
        "doc_name": da.get("filename") or da.get("name"),
        "addenda_count": len(addenda) if isinstance(addenda, list) else 0,
        "addenda": addenda if isinstance(addenda, list) else [],
    }
    text = ""
    if url:
        r = client.get(url)
        pdf_path = _out() / f"_pkg_{key}_{pid}.pdf"
        pdf_path.write_bytes(r.content)
        meta["pdf_bytes"] = len(r.content)
        meta["pdf_path"] = str(pdf_path)
        try:
            from pdfminer.high_level import extract_text as _pdfminer_extract

            text = _pdfminer_extract(str(pdf_path)) or ""
        except Exception as exc:
            meta["extract_error_pdfminer"] = type(exc).__name__
            text = ""
        if len(text.strip()) < 500:
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(pdf_path))
                text = "\n".join((p.extract_text() or "") for p in reader.pages)
            except Exception as exc:
                meta["extract_error_pypdf"] = type(exc).__name__
        (_out() / f"_pkg_{key}_{pid}.txt").write_text(text, encoding="utf-8")
    meta["text_chars"] = len(text)
    return {"meta": meta, "text": text}


def proposed_unit_sell(unit_acq: float, unit_freight_est: float) -> dict[str, Any]:
    unit_exec = unit_acq + unit_freight_est
    # 20% margin on revenue: sell = exec / (1 - 0.20)
    sell = unit_exec / (1.0 - TARGET_MARGIN)
    profit = sell - unit_exec
    return {
        "UNIT_ACQ": money(unit_acq, "VERIFIED"),
        "UNIT_FREIGHT": money(unit_freight_est, "ESTIMATED", NOTE="Not a carrier quote"),
        "UNIT_EXECUTION_COST": money(unit_exec, "ESTIMATED"),
        "PROPOSED_UNIT_SELL": money(sell, "PROPOSED", NOTE=f"{int(TARGET_MARGIN*100)}% margin on revenue"),
        "PROPOSED_UNIT_PROFIT": money(profit, "PROPOSED"),
    }


def drawdown_scenarios(lines: list[dict[str, Any]], freight_map: dict[str, float]) -> dict[str, Any]:
    """Profit at 1 unit (cheapest line), 25/50/100% of estimated schedule."""
    priced = []
    for ln in lines:
        fr = float(freight_map.get(ln["SKU"], 0))
        u = proposed_unit_sell(float(ln["UNIT_ACQ"]), fr)
        priced.append({**ln, "PRICING": u})

    def scale(frac: float) -> dict[str, Any]:
        acq = 0.0
        freight = 0.0
        sell = 0.0
        units = 0.0
        for ln in priced:
            q = float(ln["QTY_EST"]) * frac
            # for fractional display keep float; round money
            units += q
            ua = float(ln["UNIT_ACQ"])
            uf = float(freight_map.get(ln["SKU"], 0))
            ue = ua + uf
            us = ue / (1.0 - TARGET_MARGIN)
            acq += ua * q
            freight += uf * q
            sell += us * q
        profit = sell - (acq + freight)
        return {
            "FRACTION_OF_ESTIMATE": frac,
            "UNITS_ORDERED_ASSUMED": round(units, 2),
            "ACQUISITION": money(acq, "VERIFIED" if frac == 1 else "PROPOSED"),
            "FREIGHT": money(freight, "ESTIMATED"),
            "EXECUTION_COST": money(acq + freight, "ESTIMATED"),
            "PROPOSED_REVENUE": money(sell, "PROPOSED"),
            "PROPOSED_PROFIT": money(profit, "PROPOSED"),
            "MEETS_5K": profit >= PROFIT_FLOOR,
            "MEETS_10K": profit >= PROFIT_TARGET,
            "NOTE": "Assumes city orders this fraction of EACH line estimate at proposed unit prices",
        }

    # 1-unit: order one of highest-margin or first line — use each SKU and the max profit single unit
    one_unit = []
    for ln in priced:
        p = ln["PRICING"]["PROPOSED_UNIT_PROFIT"]["VALUE"] or 0
        one_unit.append(
            {
                "SKU": ln["SKU"],
                "PROPOSED_UNIT_PROFIT": ln["PRICING"]["PROPOSED_UNIT_PROFIT"],
                "PROPOSED_UNIT_SELL": ln["PRICING"]["PROPOSED_UNIT_SELL"],
                "MEETS_5K": p >= PROFIT_FLOOR,
                "MEETS_10K": p >= PROFIT_TARGET,
            }
        )
    best_one = max(one_unit, key=lambda x: x["PROPOSED_UNIT_PROFIT"]["VALUE"] or 0)

    # Units needed of best SKU alone to hit $5k / $10k
    best_ln = next(ln for ln in priced if ln["SKU"] == best_one["SKU"])
    unit_profit = best_ln["PRICING"]["PROPOSED_UNIT_PROFIT"]["VALUE"] or 0
    need_5k = int((PROFIT_FLOOR / unit_profit) + 0.999) if unit_profit > 0 else None
    need_10k = int((PROFIT_TARGET / unit_profit) + 0.999) if unit_profit > 0 else None

    return {
        "PER_UNIT": priced,
        "ONE_UNIT_BY_SKU": one_unit,
        "BEST_SINGLE_UNIT": best_one,
        "UNITS_OF_BEST_SKU_TO_HIT_5K": need_5k,
        "UNITS_OF_BEST_SKU_TO_HIT_10K": need_10k,
        "AT_25PCT_ESTIMATE": scale(0.25),
        "AT_50PCT_ESTIMATE": scale(0.50),
        "AT_100PCT_ESTIMATE": scale(1.0),
        "GUARANTEED_MINIMUM_REVENUE": money(0.0, "PROVEN", NOTE="RFQ: City not required to purchase any minimum"),
        "GUARANTEED_MINIMUM_PROFIT": money(0.0, "PROVEN", NOTE="Zero guaranteed draw"),
        "NOTE_20PCT_MARGIN": (
            "Default proposed unit sells use 20% margin on revenue. "
            "Liberty full-estimate profit at that margin is below $5k — absolute $5k/$10k bids require higher markup IF full draw occurs."
        ),
    }


def search_hist_exact(skus: list[str]) -> dict[str, Any]:
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import BidIndex

    idx = BidIndex(_canon() / "opengov_bid_price_index.json")
    idx.load()
    if not idx.loaded:
        return {"STATE": "UNKNOWN", "REASON": idx.load_error or "INDEX_MISSING", "MATCHES": []}
    matches = []
    for sku in skus:
        hit = idx.lookup(catalog=sku, model=sku, description=sku)
        if hit.get("HISTORICAL_STATE") == "PROVEN":
            matches.append({"SKU": sku, **hit})
    # Tampa buyer scan for pump
    tampa_pumps = []
    for r in idx.records:
        if str(r.get("buyer") or "").lower() not in {"cityoftampa", "tampa"}:
            continue
        desc = str(r.get("line_description") or "").lower()
        if "pump" in desc:
            try:
                p = float(r.get("bid_unit_price") or 0)
            except (TypeError, ValueError):
                p = 0
            if p > 0:
                tampa_pumps.append(
                    {
                        "unit_price": p,
                        "description": (r.get("line_description") or "")[:120],
                        "source_url": r.get("source_url"),
                        "MATCH_CLASS": "TAMPA_PUMP_NOT_EXACT_SKU",
                    }
                )
    return {
        "STATE": "PROVEN" if matches else "UNKNOWN",
        "EXACT_SKU_MATCHES": matches,
        "TAMPA_OTHER_PUMP_AWARDS": tampa_pumps[:8],
        "NOTE": "No fabricate — exact SKU hist absent means UNKNOWN competitiveness from awards",
    }


def executability_assessment(deal: dict[str, Any], screen: dict[str, Any]) -> dict[str, Any]:
    flags = screen.get("FLAGS") or {}
    stock = deal.get("STOCK_EVIDENCE") or []
    checks = {
        "SUPPLIER_STOCK_CONFIRMED_FOR_FULL_ESTIMATE": {
            "STATUS": "UNKNOWN",
            "EVIDENCE": stock,
            "NOTE": "Public stock listings exist for some SKUs but not reserved; Burks listing shows only 2 vs est. 10",
        },
        "DELIVERY_TAMPA_WITHIN_5_DAYS_ARO": {
            "STATUS": "UNKNOWN",
            "NOTE": "5-day ARO is contractual; retail same-day claims ≠ drop-ship to City Tampa under our PO",
        },
        "SUPPLIER_ACCEPTS_PO_OR_NET_TERMS": {
            "STATUS": "UNKNOWN",
            "NOTE": "Not verified with any supplier for M3 entity",
        },
        "SUPPLIER_WILL_DROPSHIP_TO_GOVERNMENT": {
            "STATUS": "UNKNOWN",
            "NOTE": "Not confirmed",
        },
        "MANUFACTURER_AUTHORIZATION": {
            "STATUS": "UNKNOWN",
            "NOTE": (
                "Liberty: commonly available through wholesale/retail — auth not proven required. "
                "Burks/Crane: FL authorized distributors exist (Hudson/Tencarva, Arroyo); M3 authorization UNKNOWN"
            ),
        },
        "FL_BUSINESS_LICENSE_INSURANCE_REGISTRATION": {
            "STATUS": "UNKNOWN",
            "NOTE": "RFQ requires insurance if work on City property; awardee must be able to do business in FL — M3 status not confirmed in this run",
        },
        "SUBCONTRACTING_51_PERCENT_RULE": {
            "STATUS": "PROVEN_CONSTRAINT",
            "NOTE": "RFQ §2.12: perform with own forces at least 51% unless City consents — drop-ship-only model may conflict; UNKNOWN if City treats supply-only differently",
        },
        "FREIGHT_COST_VERIFIED": {
            "STATUS": "UNKNOWN",
            "NOTE": "Only ESTIMATED unit freight used in model",
        },
        "FINANCING_ZERO_UPFRONT_NO_PG_NO_PERSONAL_CREDIT": {
            "STATUS": "UNKNOWN",
            "NOTE": (
                "City pays ~30 days after acceptance; supplier typically wants payment before/at ship. "
                "Float required. Positive modeled profit does NOT confirm financing. "
                "Hard rules: $0 owner cash before gov payment, no personal credit, no PG — NOT VERIFIED available"
            ),
        },
    }
    if flags.get("SHORT_DELIVERY_LE_5_DAYS"):
        checks["DELIVERY_TAMPA_WITHIN_5_DAYS_ARO"]["CONTRACTUAL"] = True
    # Harden Burks stock note
    if deal["KEY"] == "BURKS":
        checks["SUPPLIER_STOCK_CONFIRMED_FOR_FULL_ESTIMATE"]["STATUS"] = "FAILED_PUBLIC_EVIDENCE"
        checks["SUPPLIER_STOCK_CONFIRMED_FOR_FULL_ESTIMATE"]["NOTE"] = (
            "Public listing shows 2 in stock vs estimated 10; cannot confirm 5-day fulfillment of full estimate"
        )
    return checks


def decide(deal_row: dict[str, Any]) -> dict[str, Any]:
    """BID CANDIDATE | EXECUTION READY | REJECT | UNKNOWN"""
    dd = deal_row["DRAWDOWN"]
    exe = deal_row["EXECUTABLE"]
    hist = deal_row["COMPETITIVENESS"]
    screen = deal_row["PACKAGE_SCREEN"]

    reasons = []
    # Hard reject: none yet on pure economics of as-needed alone — but short-term $5-10k goal conflict
    guaranteed = (dd.get("GUARANTEED_MINIMUM_PROFIT") or {}).get("VALUE")
    if guaranteed == 0:
        reasons.append("NO_MINIMUM_PURCHASE — $5k–$10k is NOT contractually assured")

    at100 = dd["AT_100PCT_ESTIMATE"]
    at25 = dd["AT_25PCT_ESTIMATE"]
    best1 = dd["BEST_SINGLE_UNIT"]

    # Execution blockers that are FAILED
    failed_exec = [k for k, v in exe.items() if v.get("STATUS") == "FAILED_PUBLIC_EVIDENCE"]
    unknown_exec = [k for k, v in exe.items() if v.get("STATUS") == "UNKNOWN"]

    # Absolute $5k/$10k bids IF 100% estimate is ordered (higher than 20% margin for Liberty)
    exec100 = float((at100.get("EXECUTION_COST") or {}).get("VALUE") or 0)
    abs_bid_5k = exec100 + PROFIT_FLOOR
    abs_bid_10k = exec100 + PROFIT_TARGET
    deal_row["ABSOLUTE_BIDS_IF_100PCT_DRAW"] = {
        "EXECUTION_COST_EST": money(exec100, "ESTIMATED"),
        "MIN_BID_FOR_5K_PROFIT": money(abs_bid_5k, "PROPOSED"),
        "MIN_BID_FOR_10K_PROFIT": money(abs_bid_10k, "PROPOSED"),
        "NOTE": "Only relevant if City actually orders 100% of estimate — which is NOT guaranteed",
    }

    decision = "BID CANDIDATE"
    if deal_row["KEY"] == "LIBERTY" and not at100.get("MEETS_5K"):
        reasons.append(
            f"At 20% margin, even 100% estimate proposed profit "
            f"${(at100.get('PROPOSED_PROFIT') or {}).get('VALUE')} < $5k — needs higher markup AND full draw"
        )
    if failed_exec:
        reasons.append(f"Public executability failure: {failed_exec}")
    if not at25.get("MEETS_5K"):
        reasons.append("At 25% of estimate, proposed 20% margin profit is below $5k")
    reasons.append("Economics depend on unrealized as-needed drawdown")

    # Short-term goal fitness
    short_term = {
        "GOAL": "Earn $5,000–$10,000 per contract in short term",
        "FIT": "POOR_WITHOUT_DRAW_ASSURANCE",
        "EVIDENCE": [
            f"Guaranteed profit = $0 (RFQ estimate-only / no minimum)",
            f"1-unit best profit ≈ ${best1['PROPOSED_UNIT_PROFIT']['VALUE']} (PROPOSED) — below $5k",
            f"Need ~{dd.get('UNITS_OF_BEST_SKU_TO_HIT_5K')} units of best SKU at proposed margin to hit $5k",
            f"At 25% of estimate proposed profit ≈ ${at25['PROPOSED_PROFIT']['VALUE']}",
            f"At 100% of estimate proposed profit ≈ ${at100['PROPOSED_PROFIT']['VALUE']} (NOT guaranteed)",
        ],
        "CONCLUSION": (
            "Absence of minimum purchase quantities makes these unsuitable as *assured* short-term $5–10k earners. "
            "They may still be BID CANDIDATEs if M3 accepts speculative draw risk and closes execution gaps."
        ),
    }

    # Never EXECUTION READY here
    if decision == "EXECUTION READY":
        decision = "BID CANDIDATE"
    # REJECT only if we find confirmed economic impossibility at any draw — we don't
    if hist.get("STATE") == "UNKNOWN":
        reasons.append("No exact-SKU historical Tampa awards found — competitiveness UNKNOWN")
    reasons.append(f"Executable unknowns: {len(unknown_exec)}; failed: {failed_exec}")
    reasons.append("Financing path for $0 upfront / no PG / no personal credit: UNVERIFIED")

    return {
        "DECISION": decision,
        "NOT_EXECUTION_READY": True,
        "REASONS": reasons,
        "SHORT_TERM_5K_10K_GOAL": short_term,
        "PACKAGE_FLAGS": (screen.get("FLAGS") or {}),
    }


def evaluate_deal(seed: dict[str, Any], *, client: httpx.Client) -> dict[str, Any]:
    pkg = download_package_text(seed["SLUG"], seed["PROJECT_ID"], client=client, key=seed["KEY"])
    screen = screen_as_needed_contract_text(pkg["text"], title=seed["TITLE"])
    # Extract key clause citations
    text = pkg["text"]
    citations = {}
    for label, pat in [
        ("QUANTITIES", r"2\.2\.\s*Quantities.{0,400}"),
        ("SHIPPING", r"2\.3\.\s*Shipping/Delivery.{0,400}"),
        ("PRICES", r"2\.9\.\s*Prices.{0,350}"),
        ("AWARD", r"2\.10\.\s*Basis of Award.{0,350}"),
        ("PAYMENT", r"2\.14\.\s*Payment.{0,300}"),
        ("SUBCONTRACT", r"2\.12\.\s*Subcontracting.{0,350}"),
    ]:
        m = re.search(pat, text, re.I | re.S)
        citations[label] = (m.group(0).replace("\n", " ")[:320] if m else None)

    dd = drawdown_scenarios(seed["LINES"], seed["UNIT_FREIGHT_EST"])
    hist = search_hist_exact([ln["SKU"] for ln in seed["LINES"]])
    exe = executability_assessment(seed, screen)

    # Competitiveness summary
    best_sell = max(
        (ln["PRICING"]["PROPOSED_UNIT_SELL"]["VALUE"] or 0) for ln in dd["PER_UNIT"]
    )
    competitiveness = {
        **hist,
        "OUR_PROPOSED_UNIT_SELL_RANGE": {
            ln["SKU"]: ln["PRICING"]["PROPOSED_UNIT_SELL"] for ln in dd["PER_UNIT"]
        },
        "PRIOR_WINNING_UNIT_PRICES": None,
        "PRIOR_WINNING_UNIT_PRICES_STATE": "UNKNOWN",
        "PRIOR_CONTRACTORS": None,
        "COMPETITION_COUNTS": None,
        "ASSESSMENT": (
            "Cannot establish whether proposed sells compete — no exact-SKU award tabs found. "
            "Market retail exists; government award prices UNKNOWN."
        ),
        "BEST_PROPOSED_UNIT_SELL": best_sell,
    }

    row = {
        "KEY": seed["KEY"],
        "TITLE": seed["TITLE"],
        "SOLICITATION": seed["SOLICITATION"],
        "URL": seed["URL"],
        "LIVE": {
            "STATUS": pkg["meta"].get("status"),
            "DEADLINE": pkg["meta"].get("deadline"),
            "OPEN": str(pkg["meta"].get("status") or "").lower() == "open",
            "ADDENDA_COUNT": pkg["meta"].get("addenda_count"),
            "DOC": pkg["meta"].get("doc_name"),
        },
        "PACKAGE_VERIFICATION": {
            "SOURCE_PDF": pkg["meta"].get("pdf_path"),
            "ADDENDA": "NONE_ON_PORTAL" if pkg["meta"].get("addenda_count") == 0 else pkg["meta"].get("addenda"),
            "CITATIONS": citations,
            "EXTERNAL_CONFIRMATION": {
                "LIBERTY_PDF_MIRROR": "https://bqohpheioaljycjpwbjd.supabase.co/storage/v1/object/public/documents/documents/unique/8dd36cb6e587b67e891387070678666d3d3d27c63c6cf79f50f2e7852efeab94?download=Liberty-Sewage-Pumps.pdf",
                "BURKS_SECONDARY": "https://app.govly.com/public/opportunities/17198090",
            },
        },
        "PACKAGE_SCREEN": screen,
        "DRAWDOWN": dd,
        "EXECUTABLE": exe,
        "COMPETITIVENESS": competitiveness,
        "PRIOR_MODEL_ERROR": {
            "ASSUMED_FULL_ESTIMATE_AS_REVENUE": True,
            "CORRECTED": True,
            "NOTE": "Full-schedule profit is a PROPOSED scenario only; guaranteed revenue/profit = $0",
        },
    }
    row["DECISION"] = decide(row)
    return row


def run_tampa_two_deal_execution_validation_v1() -> dict[str, Any]:
    t0 = time.time()
    run_id = f"tampa-2deal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _save(PROGRESS, {"status": "RUNNING", "RUN_ID": run_id, "build": BUILD, "at": _utc()})

    rows = []
    with httpx.Client(timeout=45.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for seed in DEALS:
            rows.append(evaluate_deal(seed, client=client))
            time.sleep(0.2)

    report = {
        "BUILD": BUILD,
        "RUN_ID": run_id,
        "STATUS": "COMPLETE",
        "CREATED_AT": _utc(),
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "ISOLATION": {
            "TOUCHED_BIDNET": False,
            "TOUCHED_L23": False,
            "TOUCHED_PRODUCTION_HISTORY": False,
            "SAM_API_CALLS": 0,
            "OUTPUT_DIR": OUT_DIR,
        },
        "SCREENING_MODULE": "as_needed_contract_screening_v1",
        "QUANTITY_ASSUMPTION_FIX": {
            "PROBLEM": "Prior profitability paths treated estimated schedule quantities as if purchased",
            "FIX": "Guaranteed min revenue/profit = $0; model 1-unit / 25% / 50% / 100% drawdowns as PROPOSED only",
        },
        "DECISIONS": {r["KEY"]: r["DECISION"] for r in rows},
        "SHORT_TERM_GOAL_VERDICT": (
            "Both opportunities are unsuitable as *assured* short-term $5k–$10k earners because "
            "the City guarantees $0 purchases. They remain possible BID CANDIDATEs only with "
            "accepted draw risk + closed execution/financing gaps — not EXECUTION READY."
        ),
        "ROWS": rows,
        "GO_NO_GO": [
            {
                "DEAL": r["TITLE"],
                "SOLICITATION": r["SOLICITATION"],
                "DECISION": r["DECISION"]["DECISION"],
                "CLOSING": r["LIVE"]["DEADLINE"],
                "GUARANTEED_PROFIT": 0,
                "PROPOSED_PROFIT_AT_100PCT_EST": r["DRAWDOWN"]["AT_100PCT_ESTIMATE"]["PROPOSED_PROFIT"],
                "PROPOSED_PROFIT_AT_25PCT_EST": r["DRAWDOWN"]["AT_25PCT_ESTIMATE"]["PROPOSED_PROFIT"],
                "BEST_1_UNIT_PROFIT": r["DRAWDOWN"]["BEST_SINGLE_UNIT"]["PROPOSED_UNIT_PROFIT"],
                "UNITS_NEEDED_FOR_5K": r["DRAWDOWN"]["UNITS_OF_BEST_SKU_TO_HIT_5K"],
                "PRIMARY_BLOCKERS": r["DECISION"]["REASONS"],
            }
            for r in rows
        ],
    }
    _save(RESULTS, {"build": BUILD, "RUN_ID": run_id, "rows": rows})
    _save(REPORT, report)
    _save(PROGRESS, {"status": "DONE", "RUN_ID": run_id, "build": BUILD, "at": _utc()})
    return report


if __name__ == "__main__":
    rep = run_tampa_two_deal_execution_validation_v1()
    print(
        json.dumps(
            {
                "STATUS": rep["STATUS"],
                "SHORT_TERM_GOAL_VERDICT": rep["SHORT_TERM_GOAL_VERDICT"],
                "GO_NO_GO": rep["GO_NO_GO"],
            },
            indent=2,
        )
    )
