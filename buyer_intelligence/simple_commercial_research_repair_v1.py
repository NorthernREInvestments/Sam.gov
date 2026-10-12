"""Simple-product commercial research repair — selection, identity, Path A/B, controlled test.

Isolated. Does NOT touch BidNet enrichment, L23 writes, sidecars, or operator UI.
SAM target: zero. Cold-start: no manual product-specific price seeds.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc

log = logging.getLogger("govtracker.simple_commercial_research_repair_v1")

BUILD = "20261010-m3-simple-commercial-research-repair-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"
PROFIT_OUT = "_og_profit_val_out"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

EASY_CATS = {
    "JANITORIAL",
    "OFFICE_SUPPLIES",
    "OFFICE_FURNITURE",
    "PPE",
    "SAFETY",
    "TOOLS",
    "LIGHTING",
    "MRO",
    "FASTENERS",
    "HOSES_FITTINGS",
}
HARD_INDUSTRIAL = re.compile(
    r"\b("
    r"pumps?|hydrants?|analyzers?|turbines?|grinder\s+pumps?|excavators?|"
    r"circuit\s+breakers?|sludge|watermain|scrubbers?|valve\s+exercisers?|"
    r"dechlorinat\w*|turnout\s+gear|street\s+light\s+poles?|submersible|"
    r"lift\s+station|gas\s+turbine|burks|liberty\s+sewage"
    r")\b",
    re.I,
)
ALREADY_MODELED = {"dfe807fb49739bb6", "4ebf7c589d19cbcd"}  # Liberty / Burks Tampa
SIMPLE_SIGNAL = re.compile(
    r"\b("
    r"paper|towel|tissue|janitorial\s+suppl|office\s+suppl|office\s+furniture|"
    r"chair|desk|ppe|glove|safety\s+(glasses|vest|kit)|hard\s+hat|tool(?:s|\s+kit)|"
    r"bulb|lamp|led\b|lighting\s+suppl|toilet|soap|disinfectant|trash\s+bag|"
    r"battery|fastener|copy\s+paper|cleaner|broom|mop|filter|hardware|supply|supplies"
    r")\b",
    re.I,
)
SERVICE_DOMINANT = re.compile(
    r"\b(services?|maintenance\s+contractor|inspection|repair|rental|"
    r"management|demolition|agreement|design[-\s]?build)\b",
    re.I,
)
IT_FOOD = re.compile(
    r"\b(computer|laptop|server|tablet|radio|walkie|food|grocery|perishable|fuel|diesel)\b",
    re.I,
)


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
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return p


def _load(name: str) -> Any:
    p = _out() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_data(*parts: str) -> Any:
    p = _canon().joinpath(*parts)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _days_to_deadline(deadline: Any) -> float | None:
    if not deadline:
        return None
    try:
        raw = str(deadline).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load("m3_simple_commercial_repair_progress_v1.json") or {
        "kind": "SimpleCommercialRepairProgress",
        "build": BUILD,
    }
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save("m3_simple_commercial_repair_progress_v1.json", cur)


def diagnose_prior_selection() -> dict[str, Any]:
    """Explain why Batch B favored hard industrial short scopes."""
    picks = _load("m3_scalable_batch_b_picks_v1.json") or {}
    rows = picks.get("rows") or []
    reasons = Counter()
    for r in rows:
        title = str(r.get("TITLE") or "")
        n = int(r.get("MANDATORY_LINE_COUNT") or 0)
        if HARD_INDUSTRIAL.search(title):
            reasons["hard_industrial_title"] += 1
        if 1 <= n <= 5:
            reasons["short_scope_1_to_5"] += 1
        if r.get("CATEGORY") in EASY_CATS:
            reasons["easy_category_label"] += 1
        else:
            reasons["non_easy_category"] += 1
    return {
        "kind": "PriorSelectionDiagnosis",
        "build": BUILD,
        "created_at": _utc(),
        "BATCH_B_PICKS": len(rows),
        "REASON_COUNTS": dict(reasons),
        "ROOT_CAUSE": (
            "prefer_short_scopes ranked by line count first. Specialized industrial ITQs "
            "(Myers pumps, AFC hydrants, poles, analyzers) often have 1–14 mandatory lines "
            "and match product_title_signal (pump/hydrant/parts), so they outranked "
            "janitorial/office catalogs with 68+ lines. Category labels like COMMON_EQUIPMENT "
            "and PLUMBING were treated as preferred without a hard industrial penalty."
        ),
        "SAMPLE_HARD_PICKS": [
            {"TITLE": r.get("TITLE"), "LINES": r.get("MANDATORY_LINE_COUNT"), "CATEGORY": r.get("CATEGORY")}
            for r in rows
            if HARD_INDUSTRIAL.search(str(r.get("TITLE") or ""))
        ][:8],
    }


def build_simple_product_shortlist(*, exclude_ids: set[str] | None = None) -> dict[str, Any]:
    """Select high-confidence common-product opportunities from accessible enriched sources."""
    exclude_ids = exclude_ids or set()
    snap = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_snapshot.json") or {}
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    queue = _load("m3_scalable_research_queue_v1.json") or {}
    ckpt = _load("m3_scalable_batch_checkpoint_v1.json") or {}
    done = set(ckpt.get("completed_ids") or []) | exclude_ids
    # Also skip exhausted prior research outcomes (do not re-top janitorial/industrial failures)
    try:
        from buyer_intelligence.scalable_commercial_research_pipeline_v1 import (
            _exhausted_research_ids,
        )

        done |= _exhausted_research_ids()
    except Exception:
        pass

    # BidNet snapshot coverage check (read-only)
    bn_simple_titles = 0
    bn_with_lines = 0
    store = _load_data("_prod_snapshot", "l23_canonical_population_store.json")
    if isinstance(store, dict):
        for oid, o in (store.get("opportunities") or {}).items():
            title = str(o.get("title") or "")
            url = str(o.get("authoritative_url") or "")
            if "bidnet" not in url.lower() and "bidnet" not in str(o.get("platform") or "").lower():
                continue
            if SIMPLE_SIGNAL.search(title) and not (
                SERVICE_DOMINANT.search(title) and "suppl" not in title.lower()
            ):
                bn_simple_titles += 1
            lines = o.get("line_items") or []
            if isinstance(lines, list) and lines:
                bn_with_lines += 1

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in queue.get("rows") or []:
        oid = str(entry.get("OPPORTUNITY_ID"))
        if oid in done or oid in seen or oid in ALREADY_MODELED:
            continue
        if entry.get("RESEARCH_STATUS") == "PRICE_VERIFIED":
            continue
        title = str(entry.get("TITLE") or "")
        days = entry.get("DAYS_TO_DEADLINE")
        if days is not None and days < 3:
            continue
        if IT_FOOD.search(title):
            continue
        if HARD_INDUSTRIAL.search(title):
            continue
        # Service-dominant titles out unless clearly product-supply (janitorial/office/PPE/paper)
        if SERVICE_DOMINANT.search(title) and not re.search(
            r"\b(janitorial\s+suppl|office\s+suppl|paper|towel|tissue|ppe|glove|tool\s+kit)\b",
            title,
            re.I,
        ):
            continue
        # Require an actual simple-common signal — do not fill with industrial leftovers
        if not SIMPLE_SIGNAL.search(title):
            continue
        s = by_id.get(oid) or {}
        lines = [ln for ln in (s.get("line_items") or []) if isinstance(ln, dict)]
        n = len(lines) or int(entry.get("MANDATORY_LINE_COUNT") or 0)
        if n < 1:
            continue
        # Score simplicity / public-retail likelihood
        score = 0
        reasons: list[str] = []
        if SIMPLE_SIGNAL.search(title):
            score += 5
            reasons.append("simple_title_signal")
        else:
            continue  # refuse non-simple titles even if category tagged
        cat = entry.get("CATEGORY")
        if cat in EASY_CATS:
            score += 3
            reasons.append(f"easy_cat:{cat}")
        if 1 <= n <= 5:
            score += 4
            reasons.append("lines_1_to_5")
        elif 6 <= n <= 15:
            score += 2
            reasons.append("lines_6_to_15")
        elif n <= 80 and cat == "JANITORIAL":
            score += 3
            reasons.append("janitorial_common_catalog")
        else:
            score -= 2
            reasons.append("long_or_complex_scope")
        if days is not None and days >= 5:
            score += 2
            reasons.append("deadline_ge_5")
        # Identity hints in line text
        id_hints = 0
        for ln in lines[:12]:
            d = str(ln.get("description") or "")
            if re.search(
                r"#\s*[A-Z0-9]{3,}|Make\s*:|Model\s*:|Simple\s+Green|Gojo|part\s*#|"
                r"\b[A-Z]{2,}\s+\d{3,}[A-Z0-9.\-]*\b",
                d,
                re.I,
            ):
                id_hints += 1
        if id_hints:
            score += min(3, id_hints)
            reasons.append(f"identity_hints:{id_hints}")
        if score < 6:
            continue
        seen.add(oid)
        candidates.append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": title,
                "BUYER": entry.get("BUYER") or s.get("buyer"),
                "DEADLINE": entry.get("DEADLINE") or s.get("deadline"),
                "DAYS_TO_DEADLINE": days,
                "SOLICITATION_URL": entry.get("SOLICITATION_URL") or s.get("authoritative_url"),
                "CATEGORY": cat,
                "MANDATORY_LINE_COUNT": n,
                "SIMPLE_SCORE": score,
                "RANK_REASONS": reasons,
                "SOURCE": "OpenGov_enriched_snapshot",
            }
        )

    candidates.sort(key=lambda r: (-int(r["SIMPLE_SCORE"]), int(r["MANDATORY_LINE_COUNT"])))
    report = {
        "kind": "SimpleProductShortlist",
        "build": BUILD,
        "created_at": _utc(),
        "QUEUE_COVERAGE": {
            "OpenGov_enriched_with_lines": len(by_id),
            "BidNet_simple_titles_in_snapshot": bn_simple_titles,
            "BidNet_with_extracted_lines_in_snapshot": bn_with_lines,
            "NOTE": (
                "BidNet has many simple-product titles but 0 extracted mandatory lines in the "
                "accessible read-only snapshot — cannot research complete scopes from titles alone."
            ),
        },
        "QUALIFYING_COUNT": len(candidates),
        "rows": candidates,
        "QUEUE_COVERAGE_FAILURE": len(candidates) < 3,
    }
    _save("m3_simple_product_shortlist_v1.json", report)
    return report


def _money(v: float | None, state: str, **extra: Any) -> dict[str, Any]:
    return {"VALUE": v, "STATE": state, **extra}


def _model_bids(acq: float) -> dict[str, Any]:
    freight = round(max(75.0, acq * 0.04), 2)
    exec_cost = round(acq + freight, 2)
    return {
        "VERIFIED_ACQUISITION_COST": _money(acq, "VERIFIED"),
        "FREIGHT": _money(freight, "ESTIMATED", note="Not a carrier quote"),
        "TOTAL_ESTIMATED_EXECUTION_COST": _money(exec_cost, "ESTIMATED"),
        "MIN_BID_5K_PROFIT": _money(round(exec_cost + 5000, 2), "PROPOSED"),
        "MIN_BID_10K_PROFIT": _money(round(exec_cost + 10000, 2), "PROPOSED"),
        "MIN_BID_20PCT_MARGIN": _money(round(exec_cost / 0.80, 2), "PROPOSED"),
        "MIN_BID_30PCT_MARGIN": _money(round(exec_cost / 0.70, 2), "PROPOSED"),
        "NOTE": "Modeled margins are not guaranteed profit",
    }


def _build_rfq(*, title: str, buyer: Any, url: Any, lines: list[dict[str, Any]]) -> dict[str, Any]:
    missing = []
    for ln in lines:
        retail = ln.get("RETAIL") or {}
        if retail.get("RETAIL_STATE") == "PROVEN":
            continue
        ident = ln.get("IDENTITY") or {}
        missing.append(
            {
                "DESCRIPTION": (ln.get("DESCRIPTION") or "")[:200],
                "MANUFACTURER": ident.get("manufacturer"),
                "MODEL": ident.get("model"),
                "PART_NUMBER": ident.get("part_number"),
                "QTY": ident.get("qty") or ln.get("QTY"),
                "UOM": ident.get("uom") or ln.get("UOM"),
                "BLOCKER": retail.get("REASON"),
            }
        )
    return {
        "STATUS": "QUOTE_READY",
        "SUBJECT": f"RFQ: {title}",
        "BUYER_CONTEXT": buyer,
        "SOLICITATION": url,
        "REQUEST": (
            "Please quote firm unit price, stock on hand, lead time, freight to deliver "
            "direct to the government buyer, and payment terms (net terms preferred; "
            "buyer pays government — supplier must accept purchase-order / net terms "
            "compatible with $0 owner cash / no personal guarantee)."
        ),
        "MISSING_ITEMS": missing,
        "NOTE": "Unreturned RFQ is NOT verified acquisition cost",
    }


def quick_screen(entry: dict[str, Any], snap_row: dict[str, Any]) -> dict[str, Any]:
    """Cheap gate: is this worth deep research?"""
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import (
        identity_strength,
        is_fee_or_service_line,
        parse_line_identity,
    )

    lines = [ln for ln in (snap_row.get("line_items") or []) if isinstance(ln, dict)]
    title = str(entry.get("TITLE") or "")
    identifiable = 0
    fees = 0
    for ln in lines:
        desc = str(ln.get("description") or "")
        if is_fee_or_service_line(desc):
            fees += 1
            continue
        ident = parse_line_identity(ln)
        if identity_strength(ident) in {"NSN", "MFR_PART", "MFR_MODEL", "PART_ONLY"}:
            identifiable += 1
    product_lines = max(0, len(lines) - fees)
    ratio = (identifiable / product_lines) if product_lines else 0.0
    worth = (
        bool(SIMPLE_SIGNAL.search(title) or entry.get("CATEGORY") in EASY_CATS)
        and not HARD_INDUSTRIAL.search(title)
        and product_lines >= 1
        and (ratio >= 0.25 or entry.get("CATEGORY") == "JANITORIAL" or identifiable >= 2)
    )
    return {
        "WORTH_DEEP": worth,
        "MANDATORY_LINES": len(lines),
        "PRODUCT_LINES": product_lines,
        "IDENTIFIABLE_LINES": identifiable,
        "IDENTITY_RATIO": round(ratio, 3),
        "REASON": "PASS" if worth else "FAIL_IDENTITY_OR_HARD_SCOPE",
    }


def deep_research_opportunity(
    entry: dict[str, Any],
    snap_row: dict[str, Any],
    *,
    client: httpx.Client,
    cache: dict[str, Any],
    live_budget: list[int],
    bid_index: Any,
    max_queries: int = 5,
    max_pages: int = 10,
    blocked_stop_loss: int = 6,
) -> dict[str, Any]:
    """Deep Path A/B research of every mandatory line with stop-loss → RFQ."""
    from buyer_intelligence.as_needed_contract_screening_v1 import screen_as_needed_contract_text
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import (
        classify_line,
        identity_strength,
        is_fee_or_service_line,
        lookup_retail,
        parse_line_identity,
    )

    t0 = time.time()
    oid = entry["OPPORTUNITY_ID"]
    title = str(entry.get("TITLE") or "")
    lines_in = [ln for ln in (snap_row.get("line_items") or []) if isinstance(ln, dict)]
    out_lines: list[dict[str, Any]] = []
    acq = 0.0
    acq_known = True
    blocked_streak = 0
    force_quote_rest = False
    stats = Counter()
    requests = 0

    for i, ln in enumerate(lines_in, start=1):
        desc = str(ln.get("description") or ln.get("DESCRIPTION") or "")
        ident = parse_line_identity(ln)
        strength = identity_strength(ident)
        is_fee = is_fee_or_service_line(desc)
        qty = ident.get("qty")
        try:
            qty_f = float(qty) if qty is not None else 1.0
        except (TypeError, ValueError):
            qty_f = 1.0
        if qty_f <= 0:
            qty_f = 1.0

        _progress(
            phase="DEEP_RESEARCH",
            active_opportunity=oid,
            active_title=title,
            line_index=i,
            line_total=len(lines_in),
            live_budget_remaining=live_budget[0],
        )

        hist = bid_index.lookup(
            catalog=str(ident.get("catalog_number") or "") or None,
            model=str(ident.get("model") or "") or None,
            description=desc,
        )

        retail: dict[str, Any]
        path = "SKIP"
        if is_fee:
            retail = {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "REASON": "FEE_LINE_QUOTE_OR_INCLUDED",
                "PRICE_ORIGIN": "NONE",
            }
            path = "PATH_B_FEE"
        elif force_quote_rest or live_budget[0] <= 0:
            retail = {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "REASON": "PATH_B_QUOTE_REQUIRED_STOPLOSS" if force_quote_rest else "LIVE_BUDGET_EXHAUSTED",
                "PRICE_ORIGIN": "NONE",
            }
            path = "PATH_B"
        elif strength in {"NONE", "BRAND_ONLY"} and not (ident.get("manufacturer") and ident.get("model")):
            retail = {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "REASON": "INSUFFICIENT_IDENTITY_FOR_PUBLIC_LOOKUP",
                "PRICE_ORIGIN": "NONE",
            }
            path = "PATH_B_IDENTITY"
        else:
            # Path A — public price (multi-supplier via lookup_retail)
            path = "PATH_A"
            before = live_budget[0]
            # Temporarily raise resolver depth by calling multi_supplier directly for deep mode
            try:
                from buyer_intelligence.multi_supplier_price_discovery_v1 import (
                    discover_multi_supplier_prices,
                )

                multi = discover_multi_supplier_prices(
                    {
                        "manufacturer": ident.get("manufacturer"),
                        "model": ident.get("model"),
                        "part_number": ident.get("part_number"),
                        "catalog_number": ident.get("catalog_number"),
                        "sku": ident.get("sku"),
                        "raw_description": ident.get("raw_description"),
                    },
                    client=client,
                    max_queries=max_queries,
                    max_pages=max_pages,
                    min_suppliers_target=2,
                    use_budget=True,
                    qty=qty_f,
                    update_intel=True,
                )
                requests += 1
                live_budget[0] = max(0, live_budget[0] - 1)
                best = multi.get("BEST_OFFER") or {}
                price_f = multi.get("LOWEST_VERIFIED_UNIT_PRICE")
                url = str(multi.get("LOWEST_VERIFIED_URL") or "")
                seller = str(multi.get("LOWEST_VERIFIED_SUPPLIER") or "").lower()
                # Reject search-result pages and category-mismatched automotive sellers
                searchish = any(x in url.lower() for x in ("/search", "?q=", "?s=", "keywords="))
                auto_seller = any(
                    x in seller
                    for x in ("diesel", "cummins", "truck", "alliantpower", "finditparts")
                )
                janitorialish = bool(
                    re.search(
                        r"\b(cleaner|soap|deodor|disinfectant|broom|mop|towel|tissue|janitor)\b",
                        desc,
                        re.I,
                    )
                )
                if (
                    price_f
                    and float(price_f) > 0
                    and best.get("MATCH_CLASS") == "EXACT_VERIFIED"
                    and url
                    and not searchish
                    and not (janitorialish and auto_seller)
                ):
                    retail = {
                        "RETAIL_UNIT_PRICE": float(price_f),
                        "RETAIL_STATE": "PROVEN",
                        "RETAIL_SELLER": multi.get("LOWEST_VERIFIED_SUPPLIER"),
                        "RETAIL_URL": url,
                        "REASON": f"MULTI_SUPPLIER:{multi.get('RESOLVER_STATUS')}",
                        "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
                        "CACHE_WARM": False,
                        "SUPPLIER_COUNT": int(multi.get("SUPPLIER_COUNT") or 0),
                        "EXACT_VERIFIED_COUNT": int(multi.get("EXACT_VERIFIED_COUNT") or 0),
                        "ALL_CANDIDATES": [
                            {
                                "url": o.get("PRODUCT_URL"),
                                "unit_price": o.get("UNIT_PRICE"),
                                "seller": o.get("SUPPLIER_NAME"),
                                "match_class": o.get("MATCH_CLASS"),
                            }
                            for o in (multi.get("OFFERS") or [])[:6]
                        ],
                    }
                    blocked_streak = 0
                else:
                    status = str(multi.get("RESOLVER_STATUS") or "")
                    retail = {
                        "RETAIL_UNIT_PRICE": None,
                        "RETAIL_STATE": "UNKNOWN",
                        "RETAIL_SELLER": multi.get("LOWEST_VERIFIED_SUPPLIER"),
                        "RETAIL_URL": multi.get("LOWEST_VERIFIED_URL"),
                        "REASON": f"MULTI_SUPPLIER_NO_PRICE:{status}",
                        "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
                        "SUPPLIER_COUNT": int(multi.get("SUPPLIER_COUNT") or 0),
                        "SEARCH_TRACE_SUMMARY": multi.get("SEARCH_TRACE_SUMMARY"),
                    }
                    # Stop-loss only on repeated source blocks — keep trying distinct SKUs
                    if "BLOCKED" in status.upper():
                        blocked_streak += 1
                    else:
                        blocked_streak = 0
                    if blocked_streak >= blocked_stop_loss:
                        force_quote_rest = True
                        stats["STOP_LOSS_BLOCKED_SOURCES"] += 1
            except Exception as exc:
                retail = lookup_retail(ident, client=client, allow_live=True, cache=cache)
                requests += 1
                if live_budget[0] == before:
                    live_budget[0] = max(0, live_budget[0] - 1)
                retail.setdefault("PRICE_ORIGIN", "FRESH_INDEPENDENT_DISCOVERY")
                retail["FALLBACK_ERROR"] = type(exc).__name__

        cls = classify_line(
            ident=ident,
            strength=strength,
            hist=hist,
            retail=retail,
            package_missed=False,
            is_fee=is_fee,
        )
        stats[cls] += 1
        stats[f"PATH:{path}"] += 1
        ext = None
        if retail.get("RETAIL_STATE") == "PROVEN" and retail.get("RETAIL_UNIT_PRICE"):
            ext = round(float(retail["RETAIL_UNIT_PRICE"]) * qty_f, 2)
            acq += ext
        elif not is_fee:
            acq_known = False

        out_lines.append(
            {
                "LINE_INDEX": i,
                "DESCRIPTION": desc,
                "QTY": qty_f,
                "UOM": ident.get("uom") or ln.get("uom") or ln.get("unit"),
                "IDENTITY": ident,
                "IDENTITY_STRENGTH": strength,
                "CLASS": cls,
                "RETAIL": retail,
                "HISTORICAL": hist,
                "EXTENDED": ext,
                "RESEARCH_PATH": path,
            }
        )

    product_lines = [x for x in out_lines if x["CLASS"] != "SUPPLIER_QUOTE_REQUIRED" or x.get("EXTENDED")]
    # Fee lines may remain quote; full priced means every non-fee product line has PROVEN retail
    non_fee = [x for x in out_lines if not is_fee_or_service_line(x["DESCRIPTION"])]
    priced = [x for x in non_fee if (x.get("RETAIL") or {}).get("RETAIL_STATE") == "PROVEN"]
    identified = [
        x
        for x in non_fee
        if x.get("IDENTITY_STRENGTH") in {"NSN", "MFR_PART", "MFR_MODEL", "PART_ONLY"}
    ]
    fully = bool(non_fee) and len(priced) == len(non_fee) and acq_known and acq > 0
    quote_needed = [x for x in non_fee if (x.get("RETAIL") or {}).get("RETAIL_STATE") != "PROVEN"]
    rfq = None
    if quote_needed and identified:
        rfq = _build_rfq(
            title=title,
            buyer=entry.get("BUYER"),
            url=entry.get("SOLICITATION_URL"),
            lines=quote_needed,
        )

    bids = _model_bids(acq) if fully else None
    as_needed = screen_as_needed_contract_text(
        f"{title}\n{snap_row.get('description') or ''}", title=title
    )
    status = "DATA_MISSING"
    if fully and bids:
        status = "READY_FOR_OWNER_REVIEW" if acq < 200000 else "PROFIT_MODELED"
    elif priced:
        status = "PRICE_VERIFIED"
    elif rfq:
        status = "QUOTE_READY"
    elif not identified:
        status = "IDENTITY_UNRESOLVED"

    wall = round(time.time() - t0, 2)
    return {
        "OPPORTUNITY_ID": oid,
        "TITLE": title,
        "BUYER": entry.get("BUYER"),
        "DEADLINE": entry.get("DEADLINE"),
        "SOLICITATION_URL": entry.get("SOLICITATION_URL"),
        "CATEGORY": entry.get("CATEGORY"),
        "SOURCE": entry.get("SOURCE"),
        "RESEARCH_STATUS": status,
        "MANDATORY_LINE_COUNT": len(lines_in),
        "LINES_IDENTIFIED": len(identified),
        "LINES_VERIFIED_RETAIL": len(priced),
        "LINES_QUOTE_REQUIRED": len(quote_needed),
        "FULLY_PRICED": fully,
        "ACQUISITION_COST": round(acq, 2) if priced else None,
        "BID_MODEL": bids,
        "AS_NEEDED": as_needed,
        "RFQ": rfq,
        "OWNER_REVIEW_CANDIDATE": bool(fully and bids and acq < 200000),
        "EXECUTION_READY": False,
        "FINANCING_STATUS": "UNVERIFIED",
        "BLOCKERS": (
            []
            if fully
            else (
                ["FULL_SCOPE_ACQUISITION_NOT_VERIFIED"]
                + (["QUOTE_REQUIRED_FOR_REMAINING_LINES"] if quote_needed else [])
                + (["FINANCING_NOT_CONFIRMED"] if fully else [])
            )
        ),
        "CLASS_COUNTS": dict(stats),
        "LINES": out_lines,
        "WALL_CLOCK_SECONDS": wall,
        "RESEARCH_REQUESTS": requests,
        "STOP_LOSS_TRIGGERED": force_quote_rest,
        "RESEARCHED_AT": _utc(),
        "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
        "NOTE": "Modeled profit is not guaranteed; quantities may be estimated/as-needed",
    }


def run_controlled_simple_test(*, max_opportunities: int = 10, max_live: int = 80) -> dict[str, Any]:
    """Execute controlled simple-product test with quick screen + deep research."""
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import (
        BidIndex,
        restore_historical_pricing_access,
    )

    t0 = time.time()
    run_id = f"scr-simple-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    _progress(status="RUNNING", phase="START", RUN_ID=run_id)

    diagnosis = diagnose_prior_selection()
    _save("m3_simple_prior_selection_diagnosis_v1.json", diagnosis)

    shortlist = build_simple_product_shortlist()
    picks = (shortlist.get("rows") or [])[:max_opportunities]
    _save(
        "m3_simple_controlled_test_picks_v1.json",
        {"build": BUILD, "RUN_ID": run_id, "count": len(picks), "rows": picks},
    )

    snap = _load_data(PROFIT_OUT, "m3_opengov_enriched_profitability_validation_v1_snapshot.json") or {}
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    hist = restore_historical_pricing_access()
    bid_index = BidIndex(_canon() / "opengov_bid_price_index.json")
    bid_index.load()
    cache = _load_data(PROFIT_OUT, "m3_commercial_acquisition_cache_v1.json") or {"entries": {}}
    # Cold-start: do not seed manual Liberty/Burks prices for this test
    live_budget = [max_live]
    results: list[dict[str, Any]] = []
    screened_out: list[dict[str, Any]] = []

    with httpx.Client(timeout=25.0, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for i, entry in enumerate(picks, start=1):
            oid = entry["OPPORTUNITY_ID"]
            snap_row = by_id.get(oid) or {}
            _progress(
                phase="QUICK_SCREEN",
                batch_index=i,
                batch_total=len(picks),
                active_opportunity=oid,
                active_title=entry.get("TITLE"),
            )
            screen = quick_screen(entry, snap_row)
            if not screen.get("WORTH_DEEP"):
                screened_out.append({**entry, "QUICK_SCREEN": screen})
                results.append(
                    {
                        **entry,
                        "RESEARCH_STATUS": "SCREENED_OUT",
                        "QUICK_SCREEN": screen,
                        "FULLY_PRICED": False,
                        "OWNER_REVIEW_CANDIDATE": False,
                        "EXECUTION_READY": False,
                        "BLOCKERS": ["FAILED_QUICK_SCREEN"],
                    }
                )
                continue
            print(f"[SIMPLE_TEST] deep {i}/{len(picks)} {entry.get('TITLE')[:60]}", flush=True)
            researched = deep_research_opportunity(
                entry,
                snap_row,
                client=client,
                cache=cache,
                live_budget=live_budget,
                bid_index=bid_index,
                max_queries=5,
                max_pages=10,
                blocked_stop_loss=6,
            )
            researched["QUICK_SCREEN"] = screen
            researched["BATCH"] = "SIMPLE_CONTROLLED"
            results.append(researched)
            _save(
                "m3_simple_controlled_test_checkpoint_v1.json",
                {
                    "build": BUILD,
                    "RUN_ID": run_id,
                    "completed": i,
                    "total": len(picks),
                    "live_remaining": live_budget[0],
                    "results": results,
                    "updated_at": _utc(),
                },
            )
            print(
                f"[SIMPLE_TEST] checkpoint {i}/{len(picks)} "
                f"full={researched.get('FULLY_PRICED')} priced={researched.get('LINES_VERIFIED_RETAIL')} "
                f"id={researched.get('LINES_IDENTIFIED')} wall={researched.get('WALL_CLOCK_SECONDS')}",
                flush=True,
            )

    table = []
    for r in results:
        bids = r.get("BID_MODEL") or {}
        priced_lines = [
            {
                "desc": (ln.get("DESCRIPTION") or "")[:100],
                "unit": (ln.get("RETAIL") or {}).get("RETAIL_UNIT_PRICE"),
                "seller": (ln.get("RETAIL") or {}).get("RETAIL_SELLER"),
                "url": (ln.get("RETAIL") or {}).get("RETAIL_URL"),
                "suppliers": (ln.get("RETAIL") or {}).get("SUPPLIER_COUNT"),
            }
            for ln in (r.get("LINES") or [])
            if (ln.get("RETAIL") or {}).get("RETAIL_STATE") == "PROVEN"
        ]
        hist_ev = next(
            (
                (ln.get("HISTORICAL") or {})
                for ln in (r.get("LINES") or [])
                if (ln.get("HISTORICAL") or {}).get("UNIT_PRICE")
            ),
            None,
        )
        table.append(
            {
                "Solicitation": r.get("TITLE"),
                "Source URL": r.get("SOLICITATION_URL"),
                "Buyer": r.get("BUYER"),
                "Closing date": r.get("DEADLINE"),
                "Mandatory lines": r.get("MANDATORY_LINE_COUNT"),
                "Lines identified": r.get("LINES_IDENTIFIED"),
                "Verified retail lines": r.get("LINES_VERIFIED_RETAIL"),
                "Verified prices sample": priced_lines[:8],
                "Complete acquisition cost": r.get("ACQUISITION_COST") if r.get("FULLY_PRICED") else None,
                "Previous government award": hist_ev,
                "Proposed bid (10k profit)": (bids.get("MIN_BID_10K_PROFIT") or {}).get("VALUE"),
                "Proposed bid (5k profit)": (bids.get("MIN_BID_5K_PROFIT") or {}).get("VALUE"),
                "Estimated potential profit": 10000 if bids.get("MIN_BID_10K_PROFIT") else None,
                "Profit state": "PROPOSED_NOT_GUARANTEED" if bids else None,
                "Supplier quote requirements": (r.get("RFQ") or {}).get("STATUS"),
                "Financing status": r.get("FINANCING_STATUS") or "UNVERIFIED",
                "Exact remaining blockers": r.get("BLOCKERS"),
                "Research status": r.get("RESEARCH_STATUS"),
                "Wall seconds": r.get("WALL_CLOCK_SECONDS"),
                "Research requests": r.get("RESEARCH_REQUESTS"),
            }
        )

    fully = sum(1 for r in results if r.get("FULLY_PRICED"))
    quote_ready = sum(1 for r in results if r.get("RESEARCH_STATUS") == "QUOTE_READY" or r.get("RFQ"))
    competitive = sum(1 for r in results if (r.get("BID_MODEL") or {}).get("MIN_BID_5K_PROFIT"))
    report = {
        "kind": "SimpleCommercialControlledTestReport",
        "build": BUILD,
        "RUN_ID": run_id,
        "created_at": _utc(),
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "TOUCHED_OPERATOR_UI": False,
            "SAM_API_CALLS": 0,
            "LIVE_PUBLIC_PRICE_CALLS": max_live - live_budget[0],
            "MANUAL_PRODUCT_SEEDS": False,
            "COLD_START": True,
        },
        "HISTORICAL_ACCESS": hist,
        "PRIOR_SELECTION_DIAGNOSIS": diagnosis.get("ROOT_CAUSE"),
        "QUEUE_COVERAGE": shortlist.get("QUEUE_COVERAGE"),
        "METRICS": {
            "Opportunities available for simple-product test": shortlist.get("QUALIFYING_COUNT"),
            "Opportunities researched": len(results),
            "Screened out at quick screen": len(screened_out),
            "Mandatory lines extracted correctly": sum(int(r.get("LINES_IDENTIFIED") or 0) for r in results),
            "Lines with verified public prices": sum(int(r.get("LINES_VERIFIED_RETAIL") or 0) for r in results),
            "Complete solicitations acquisition-priced": fully,
            "Supplier quotes prepared": quote_ready,
            "Competitive proposed-bid candidates": competitive,
            "Execution-ready deals": sum(1 for r in results if r.get("EXECUTION_READY")),
            "Target complete scopes": 3,
            "TARGET_MET": fully >= 3,
        },
        "RESULTS_TABLE": table,
        "rows": results,
        "EXPAND_TO_LARGE_BATCH": False,
        "NEXT_IF_BELOW_TARGET": (
            "Queue-coverage failure: BidNet simple titles lack extracted lines in snapshot; "
            "OpenGov has almost no remaining 1–5 line common-product scopes. "
            "Next: mine BidNet enrichment packages as they complete (read-only) for copy paper / "
            "janitorial / PPE with REAL_LINES>0; deepen Path A for janitorial brand SKUs on "
            "Grainger/Uline/Zoro; do not refill tests with industrial ITQs."
            if fully < 3
            else "Target met — consider bounded expansion only on simple-product shortlist."
        ),
    }
    _save("m3_simple_controlled_test_report_v1.json", report)
    _save("m3_simple_controlled_test_results_v1.json", {"build": BUILD, "RUN_ID": run_id, "rows": results})
    _progress(status="COMPLETE", phase="DONE", FINAL_STATUS="COMPLETE", fully_priced=fully)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("SIMPLE_CONTROLLED_TEST_START", flush=True)
    out = run_controlled_simple_test(max_opportunities=10, max_live=80)
    print("SIMPLE_CONTROLLED_TEST_DONE", flush=True)
    print(json.dumps(out.get("METRICS"), indent=2))
    print("QUALIFYING", (out.get("QUEUE_COVERAGE") or {}))
    print("fully", out.get("METRICS", {}).get("Complete solicitations acquisition-priced"))
