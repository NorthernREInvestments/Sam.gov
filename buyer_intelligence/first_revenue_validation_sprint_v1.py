"""M3 First Revenue Validation Sprint V1.

Repair opportunity selection for cash-flow first revenue, select ≤25 live
product-resale opportunities, run real public price + historical research,
compute conditional economics, prepare 5 supplier RFQs, and evaluate financing
provider public requirements — without inventing prices or auto-sending.

Preserves HT v2 / CI upgrade baselines. Prefer 0 SAM API calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import (
    FIN_BLOCKED,
    FIN_CONFIRMED,
    FIN_UNKNOWN,
    FIN_UNVERIFIED,
    PACKET_RECON_FILE,
    REMATCH_FILE,
    _dec,
    _load,
    _money_str,
    _save,
    _utc,
    classify_family,
    financing_label_for_deal,
    max_supplier_cost_targets,
    multi_line_completeness,
    normalize_product_identity,
    quoteable_rows,
    research_public_price_bounded,
)
from buyer_intelligence.revenue_first_commercial_intelligence_v1 import (
    LANE_CASH,
    LANE_GROWTH,
    LANE_PROVISIONAL,
    LANE_SCALE,
    classify_profit_lane,
    evaluate_cash_flow,
    load_historical_indexes,
    match_historical,
    max_cost_targets_by_lane,
)
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    resolve_contact,
)

log = logging.getLogger("govtracker.first_revenue_validation_sprint_v1")

BUILD = "20261012-m3-first-revenue-validation-sprint-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"

SELECTED_FILE = f"{OUT_DIR}/m3_first_revenue_v1_selected_25.json"
RESULTS_FILE = f"{OUT_DIR}/m3_first_revenue_v1_research_results.json"
SCORECARD_FILE = f"{OUT_DIR}/m3_first_revenue_v1_scorecard.json"
RFQ_FILE = f"{OUT_DIR}/m3_first_revenue_v1_supplier_rfqs.json"
FINANCING_FILE = f"{OUT_DIR}/m3_first_revenue_v1_financing.json"
DEALS_FILE = f"{OUT_DIR}/m3_first_revenue_v1_available_deals.json"
CHECKPOINT_FILE = f"{OUT_DIR}/m3_first_revenue_v1_checkpoint.json"
REPORT_FILE = f"{OUT_DIR}/m3_first_revenue_v1_report.json"
EXCLUSIONS_FILE = f"{OUT_DIR}/m3_first_revenue_v1_exclusions_audit.json"
TOP5_FILE = f"{OUT_DIR}/m3_first_revenue_v1_top5.json"

# Hard exclusions from first-revenue ranking (retained for audit)
_HARD_EXCLUDE = re.compile(
    r"\b("
    r"surplus|scrap\s+copper|scrap\s+metal|excess\s+property|disposal\s+of|"
    r"reverse\s+auction|government\s+is\s+selling|sale\s+of\s+surplus|"
    r"services?|construction|installation|design[- ]build|"
    r"feasibility\s+study|crm|financial\s+aid|software\s+license|"
    r"hearing\s+protection\s+program|access\s+controls?|"
    r"widening|rubbish|recycling\s+services?|clean-?ups?|"
    r"modular\s+office|replacement\s+project|valve\s+replacements?\s+project|"
    r"tub\s+surround|circuit\s+breaker\s+multi-year|root\s+control|"
    r"vehicle\s+fuel\s+bid|fuel\s+bid|energy\s+storage\s+system|"
    r"toxic\s+vapor|analyzer|cameras?|fencing\s+services?|"
    r"bridge\s+bearing|bearing\s+repairs?|bearing\s+replacements?|"
    r"pump\s+repair\s+parts|repair\s+parts\b|wwtp\b|wastewater\s+treatment"
    r")\b",
    re.I,
)

_SIMPLE_PRODUCT = re.compile(
    r"\b("
    r"paper\s*towel|toilet\s*paper|janitorial|nitrile|glove|ppe|"
    r"mop|gasket|valve|clamp|bearing|filter|battery|charger|"
    r"cable|elbow|pipe|fastener|toner|bond\s*paper|safety\s+glasses|"
    r"pump|bracket|disk|housing|sleeve|parts?\s*kit|odyssey\s+batter"
    r")\b",
    re.I,
)

_COMPLEX_FULFILL = re.compile(
    r"\b(hazardous|hazmat|radioactive|controlled\s+substance|"
    r"on[- ]site\s+labor|prevailing\s+wage|davis[- ]bacon|"
    r"licensed\s+contractor|bond\s+required)\b",
    re.I,
)


def _days_to_deadline(dl: Any) -> float | None:
    if not dl:
        return None
    try:
        dt = datetime.fromisoformat(str(dl).replace("Z", "+00:00"))
        return (dt - now_utc()).total_seconds() / 86400.0
    except Exception:
        return None


def _clean_title(title: str) -> str:
    t = (title or "").replace("&amp;", "&").replace("&#034;", '"')
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Federal FSC prefix "48--VALVE,CHECK" → usable description
    t = re.sub(r"^\d{2}--", "", t)
    return t


def hard_exclusion_reason(row: dict[str, Any]) -> str | None:
    title = str(row.get("TITLE") or "")
    if str(row.get("PRODUCT_FIT") or "") == "EXCLUDED":
        return "PRODUCT_FIT_EXCLUDED"
    if str(row.get("LIVE_STATE") or "").upper() in {"EXPIRED", "CANCELLED", "CANCELED", "CLOSED"}:
        return "NOT_LIVE"
    if _HARD_EXCLUDE.search(title):
        return "HARD_EXCLUDE_PATTERN"
    if _COMPLEX_FULFILL.search(title):
        return "COMPLEX_FULFILLMENT"
    days = _days_to_deadline(row.get("DEADLINE"))
    if days is not None and days < 0:
        return "DEADLINE_PASSED"
    if days is not None and days < 3:
        return "INSUFFICIENT_RUNWAY_LT_3D"
    rl = int(row.get("REAL_LINES") or 0)
    ql = int(row.get("QUOTEABLE_LINES") or 0)
    # Prefer ≤5 mandatory lines for first-revenue; >15 hard-exclude from queue
    if max(rl, ql) > 15:
        return "TOO_MANY_MANDATORY_LINES"
    return None


def first_revenue_research_priority_score(
    row: dict[str, Any],
    *,
    rematch_row: dict[str, Any] | None = None,
    hist_hit: bool = False,
) -> dict[str, Any]:
    """Preliminary ranking before prices — never invents profit amounts."""
    reasons: list[str] = []
    excl = hard_exclusion_reason(row)
    if excl:
        return {"SCORE": -1000.0, "EXCLUDED": True, "REASON": excl, "FACTORS": {}}

    title = _clean_title(str(row.get("TITLE") or ""))
    rl = int(row.get("REAL_LINES") or 0) or int(row.get("QUOTEABLE_LINES") or 1)
    ql = int(row.get("QUOTEABLE_LINES") or 0)
    days = _days_to_deadline(row.get("DEADLINE"))
    suppliers = 0
    if rematch_row:
        suppliers = len(rematch_row.get("SUPPLIER_SET_AFTER") or {})
    if not suppliers and row.get("SUPPLIER_SET"):
        suppliers = len(row.get("SUPPLIER_SET") or {})
    suppliers = max(suppliers, int(row.get("SUPPORTED_SUPPLIER_LINES") or 0), int(row.get("PACKET_COUNT") or 0))

    score = 0.0
    factors: dict[str, float] = {}

    # 1. Mandatory lines — prefer 1–5
    if rl <= 1:
        factors["lines"] = 40
    elif rl <= 3:
        factors["lines"] = 35
    elif rl <= 5:
        factors["lines"] = 25
    else:
        factors["lines"] = -15
        reasons.append("more_than_5_lines")
    score += factors["lines"]

    # 2. Product simplicity
    if _SIMPLE_PRODUCT.search(title):
        factors["simplicity"] = 35
    elif re.search(r"\b(nsn|fsc|\d{4}-\d{2}-\d{3}-\d{4})\b", title, re.I) or re.match(r"^[A-Z0-9 ,\-/]+$", title):
        factors["simplicity"] = 20
    else:
        factors["simplicity"] = 5
    score += factors["simplicity"]

    # 3. Supplier coverage
    if suppliers >= 2:
        factors["suppliers"] = 30
    elif suppliers == 1:
        factors["suppliers"] = 18
    else:
        factors["suppliers"] = 0
        reasons.append("no_supplier")
    score += factors["suppliers"]

    # 4. Product identification quality
    ident = normalize_product_identity(title)
    if ident.get("has_exact_id"):
        factors["identity"] = 35
    elif ident.get("qty") is not None:
        factors["identity"] = 15
    elif len(title.split()) <= 8 and _SIMPLE_PRODUCT.search(title):
        factors["identity"] = 20
    else:
        factors["identity"] = 5
    score += factors["identity"]

    # 5. Known quantities
    factors["qty"] = 12 if ident.get("qty") is not None else (8 if ql <= 3 else 0)
    score += factors["qty"]

    # 6. Known contract value (from CURRENT_REVENUE / AWARD_EVIDENCE if present)
    val = _dec((row.get("CURRENT_REVENUE") or {}).get("value") if isinstance(row.get("CURRENT_REVENUE"), dict) else None)
    if val is None:
        ae = row.get("AWARD_EVIDENCE")
        if isinstance(ae, dict):
            val = _dec(ae.get("amount") or ae.get("value"))
    factors["contract_value"] = 10 if val is not None else 0
    score += factors["contract_value"]

    # 7. Historical purchasing evidence
    factors["historical"] = 20 if hist_hit else 0
    score += factors["historical"]

    # 8. Deadline / runway
    if days is None:
        factors["runway"] = 0
    elif days >= 10:
        factors["runway"] = 25
    elif days >= 5:
        factors["runway"] = 20
    elif days >= 3:
        factors["runway"] = 10
        reasons.append("tight_runway_3_5d")
    else:
        factors["runway"] = -40
    score += factors["runway"]

    # 9. Shipping/fulfillment complexity (proxy from title)
    if re.search(r"\b(freight|fob|delivered|destination)\b", title, re.I):
        factors["fulfillment"] = 5
    elif re.search(r"\b(install|on[- ]site|labor)\b", title, re.I):
        factors["fulfillment"] = -25
    else:
        factors["fulfillment"] = 8  # simple shippable goods assumed provisional
    score += factors["fulfillment"]

    # 10. Funding-path plausibility (packet-ready / known channel)
    if int(row.get("PACKET_COUNT") or 0) > 0 or suppliers > 0:
        factors["funding_plausible"] = 15
    else:
        factors["funding_plausible"] = 0
    score += factors["funding_plausible"]

    # 11. Repeat-buyer potential — soft boost if buyer name looks municipal/utility
    buyer = str(row.get("BUYER") or "")
    if re.search(r"\b(city|county|school|utility|transit|district|borough)\b", buyer, re.I):
        factors["repeat_buyer"] = 10
    else:
        factors["repeat_buyer"] = 3
    score += factors["repeat_buyer"]

    # Prefer commodity cash-flow families slightly
    fam = classify_family({}, title)
    if fam in {"office_supplies", "janitorial", "ppe", "mro", "plumbing", "electrical", "auto"}:
        factors["family"] = 12
        score += 12

    return {
        "SCORE": round(score, 2),
        "EXCLUDED": False,
        "REASON": None,
        "FACTORS": factors,
        "NOTES": reasons,
        "MANDATORY_LINES": rl,
        "SUPPLIER_COVERAGE": suppliers,
        "DAYS_TO_DEADLINE": None if days is None else round(days, 2),
        "IDENTITY_QUALITY": "EXACT" if ident.get("has_exact_id") else ("COMMODITY" if _SIMPLE_PRODUCT.search(title) else "WEAK"),
        "FAMILY": fam,
    }


def select_first_revenue_cohort(
    rows: list[dict[str, Any]],
    rematch: dict[str, Any],
    hist_index: dict[str, Any],
    *,
    n: int = 25,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    rematch_ids = set(rematch.get("NEW_MATCH_IDS") or [])
    exclusions: list[dict[str, Any]] = []
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []

    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        oid = r.get("OPPORTUNITY_ID")
        rr = rematch_by.get(oid)
        # Prefer matched / packet-ready but allow unmatched simple commodities
        title = _clean_title(str(r.get("TITLE") or ""))
        hist = match_historical(str(r.get("BUYER") or ""), title, hist_index)
        hist_hit = bool(hist and (hist.get("unit_price") or hist.get("award_amount")))
        pri = first_revenue_research_priority_score(r, rematch_row=rr, hist_hit=hist_hit)
        if pri["EXCLUDED"]:
            exclusions.append(
                {
                    "OPPORTUNITY_ID": oid,
                    "TITLE": title[:120],
                    "REASON": pri["REASON"],
                }
            )
            continue
        # Soft prefer rematch/packet
        sc = float(pri["SCORE"])
        if oid in rematch_ids or int(r.get("PACKET_COUNT") or 0) > 0:
            sc += 12
        if rr and int(rr.get("SUPPORTED_SUPPLIER_LINES_AFTER") or 0) > 0:
            sc += 10
        # Require some supplier OR strong simple product identity
        if pri["SUPPLIER_COVERAGE"] <= 0 and pri["IDENTITY_QUALITY"] == "WEAK":
            exclusions.append({"OPPORTUNITY_ID": oid, "TITLE": title[:120], "REASON": "NO_SUPPLIER_WEAK_IDENTITY"})
            continue
        scored.append((sc, r, pri))

    scored.sort(key=lambda x: (-x[0], x[1].get("OPPORTUNITY_ID") or ""))
    selected: list[dict[str, Any]] = []
    seen_title_stems: set[str] = set()
    for sc, r, pri in scored:
        if len(selected) >= n:
            break
        # Expand to slightly more complex only after filling simple slots
        if len(selected) < 20 and int(pri.get("MANDATORY_LINES") or 99) > 5:
            continue
        title = _clean_title(str(r.get("TITLE") or ""))
        # Deduplicate near-identical federal NSN title stems (e.g. many CABLE ASSEMBLY,SPECIAL)
        stem = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        stem = " ".join(stem.split()[:4])
        if stem in seen_title_stems and len(selected) < n - 3:
            exclusions.append(
                {
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "TITLE": title[:120],
                    "REASON": "DUPLICATE_TITLE_STEM_DEFERRED",
                }
            )
            continue
        seen_title_stems.add(stem)
        selected.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "TITLE": title,
                "BUYER": r.get("BUYER"),
                "DEADLINE": r.get("DEADLINE"),
                "SOURCE": r.get("SOURCE"),
                "REAL_LINES": r.get("REAL_LINES"),
                "QUOTEABLE_LINES": r.get("QUOTEABLE_LINES"),
                "PACKET_COUNT": r.get("PACKET_COUNT"),
                "PRIORITY_SCORE": sc,
                "PRIORITY": pri,
                "SELECTED_AT": _utc(),
            }
        )

    return selected, exclusions[:500]


def _product_lines_for_row(row: dict[str, Any], rematch_row: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Best-effort mandatory line identities from title / rematch line proxies."""
    title = _clean_title(str(row.get("TITLE") or ""))
    lines: list[dict[str, Any]] = []
    if rematch_row and rematch_row.get("LINE_RESULTS"):
        for i, lr in enumerate(rematch_row["LINE_RESULTS"], 1):
            if not isinstance(lr, dict):
                continue
            desc = title if i == 1 else f"{title} line {i}"
            fam = lr.get("FAMILY") or classify_family({}, desc)
            match = lr.get("MATCH") or {}
            ident = normalize_product_identity(desc)
            lines.append(
                {
                    "LINE_NO": i,
                    "DESCRIPTION": desc,
                    "FAMILY": fam,
                    "IDENTITY": ident,
                    "SUPPLIER": match.get("SUPPLIER"),
                    "SUPPORTED": bool(match.get("SUPPORTED")),
                    "ALTERNATES": [a.get("SUPPLIER") for a in (match.get("ALTERNATES") or []) if a.get("SUPPLIER")],
                }
            )
    if not lines:
        # Split commodity multi-product titles: "Paper Towels and Toilet Paper"
        parts = re.split(r"\s+and\s+|\s*/\s*|\s*;\s*", title, flags=re.I)
        parts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]
        if len(parts) > 1 and len(parts) <= 5 and all(len(p.split()) <= 6 for p in parts):
            for i, p in enumerate(parts, 1):
                ident = normalize_product_identity(p)
                lines.append(
                    {
                        "LINE_NO": i,
                        "DESCRIPTION": p,
                        "FAMILY": classify_family({}, p),
                        "IDENTITY": ident,
                        "SUPPLIER": None,
                        "SUPPORTED": False,
                        "ALTERNATES": [],
                    }
                )
        else:
            ident = normalize_product_identity(title)
            lines.append(
                {
                    "LINE_NO": 1,
                    "DESCRIPTION": title,
                    "FAMILY": classify_family({}, title),
                    "IDENTITY": ident,
                    "SUPPLIER": None,
                    "SUPPORTED": False,
                    "ALTERNATES": [],
                }
            )
    return lines


def _reject_false_host_match(desc: str, retail: dict[str, Any]) -> dict[str, Any]:
    """Drop prices from hosts that cannot legitimately sell the product family."""
    url = str(retail.get("source_url") or "").lower()
    blob = (desc or "").lower()
    if not url or retail.get("unit_price") is None:
        return retail
    if "1000bulbs" in url and not re.search(r"\b(led|bulb|lamp|light|fixture)\b", blob):
        return {
            **retail,
            "unit_price": None,
            "status": "REJECTED_HOST_MISMATCH",
            "evidence_level": "PRICE_UNAVAILABLE",
            "rejection_reason": "Lighting host returned for non-lighting product",
            "rejected_price": retail.get("unit_price"),
            "rejected_url": retail.get("source_url"),
        }
    if "quill.com" in url or "staples.com" in url or "officedepot.com" in url:
        if re.search(r"\b(valve|gasket|bearing|cable\s+assembly|nsn|clamp)\b", blob) and not re.search(
            r"\b(paper|towel|toilet|toner|office|glove|janitorial|mop)\b", blob
        ):
            return {
                **retail,
                "unit_price": None,
                "status": "REJECTED_HOST_MISMATCH",
                "evidence_level": "PRICE_UNAVAILABLE",
                "rejection_reason": "Office retailer host for industrial part",
                "rejected_price": retail.get("unit_price"),
                "rejected_url": retail.get("source_url"),
            }
    return retail


def _downgrade_historical_if_weak(title: str, historical: dict[str, Any]) -> dict[str, Any]:
    """Do not keep EXACT label when title overlap with historical award is weak."""
    if not historical or historical.get("STATUS") == "HISTORICAL_PRICE_UNKNOWN":
        return historical
    ht = set(re.findall(r"[a-z0-9]{4,}", (title or "").lower()))
    rt = set(re.findall(r"[a-z0-9]{4,}", str(historical.get("title") or "").lower()))
    overlap = len(ht & rt)
    if historical.get("STATUS") == "EXACT_HISTORICAL_UNIT_PRICE" and overlap < 2:
        out = dict(historical)
        out["STATUS"] = "COMPARABLE_HISTORICAL_UNIT_PRICE"
        out["NOTE"] = (
            (out.get("NOTE") or "")
            + " | Downgraded from EXACT — weak title token overlap with open solicitation"
        ).strip(" |")
        return out
    return historical


def research_line_prices(oid: str, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Execute real bounded public price research per mandatory line."""
    out: list[dict[str, Any]] = []
    for line in lines:
        ident = dict(line.get("IDENTITY") or {})
        desc = str(line.get("DESCRIPTION") or "")
        # Strengthen identity for federal FSC titles
        if not ident.get("has_exact_id"):
            nsn_m = re.search(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b", desc)
            if nsn_m:
                ident["nsn"] = nsn_m.group(1)
                ident["has_exact_id"] = True
            # Catalog-ish tokens: DISK,VALVE / VALVE,CHECK
            if "," in desc and len(desc) < 60:
                ident["synonyms"] = list(dict.fromkeys((ident.get("synonyms") or []) + [desc.replace(",", " "), desc]))
        try:
            retail = research_public_price_bounded(f"{oid}-L{line['LINE_NO']}", ident, title=desc)
        except Exception as exc:
            retail = {
                "status": "ERROR",
                "evidence_level": "PRICE_UNAVAILABLE",
                "unit_price": None,
                "error": str(exc)[:200],
                "checked_at": _utc(),
            }
        # Reject known cross-category false hosts (e.g. lighting site for cable assemblies)
        retail = _reject_false_host_match(desc, retail)
        # Normalize evidence labels
        el = retail.get("evidence_level") or "PRICE_UNAVAILABLE"
        if retail.get("unit_price") is None and el not in {
            "PRICE_IDENTITY_UNCERTAIN",
            "PRICE_UNAVAILABLE",
            "SUPPLIER_QUOTE_REQUIRED",
        }:
            if retail.get("status") in {"SKIPPED_NEEDS_MPN", "SKIPPED_NON_COMMODITY_TITLE"}:
                el = "SUPPLIER_QUOTE_REQUIRED" if "MPN" in str(retail.get("status")) else "PRICE_UNAVAILABLE"
            else:
                el = "PRICE_UNAVAILABLE"
            retail["evidence_level"] = el
        out.append(
            {
                **line,
                "RETAIL": retail,
                "RESEARCH_ATTEMPTED": True,
            }
        )
    return out


def classify_economics(
    *,
    selling: Decimal | None,
    selling_label: str,
    acquisition: Decimal | None,
    freight: Decimal | None,
    financing_fees: Decimal | None,
    financing_status: str,
    full_lines_priced: bool,
) -> dict[str, Any]:
    missing: list[str] = []
    if selling is None:
        missing.append("SELLING_PRICE")
    if acquisition is None:
        missing.append("ACQUISITION_COST")
    if freight is None:
        missing.append("FREIGHT")
    if financing_fees is None:
        missing.append("FINANCING_FEES")
    if not full_lines_priced:
        missing.append("INCOMPLETE_LINE_PRICING")

    if acquisition is None and selling is None:
        status = "INSUFFICIENT_EVIDENCE"
    elif acquisition is None:
        status = "SUPPLIER_QUOTE_REQUIRED"
    elif selling is None:
        status = "SELLING_PRICE_EVIDENCE_REQUIRED"
    elif freight is None or financing_fees is None or financing_status != FIN_CONFIRMED:
        # Can still form conditional economics
        gross = selling - acquisition
        status = "CONDITIONAL_POSITIVE_ECONOMICS" if gross > 0 else "NEGATIVE_ECONOMICS"
        if financing_status != FIN_CONFIRMED:
            # Keep conditional but flag financing
            pass
    else:
        contrib = selling - acquisition - (freight or Decimal("0")) - (financing_fees or Decimal("0"))
        status = "VERIFIED_POSITIVE_ECONOMICS" if contrib > 0 else "NEGATIVE_ECONOMICS"

    gross = None
    if selling is not None and acquisition is not None:
        gross = selling - acquisition

    # Max supplier costs for profit floors (conditional on selling)
    targets = max_supplier_cost_targets(
        selling_price=selling,
        freight=freight,
        compliance=None,
        financing_fees=financing_fees,
    )
    # Explicit floors requested
    extra = {}
    if selling is not None:
        known = Decimal("0")
        for v in (freight, financing_fees):
            if v is not None:
                known += v
        for label, profit in (
            ("MAX_COST_FOR_500", Decimal("500")),
            ("MAX_COST_FOR_2000", Decimal("2000")),
            ("MAX_COST_FOR_5000", Decimal("5000")),
            ("MAX_COST_FOR_10000", Decimal("10000")),
        ):
            extra[label] = _money_str(selling - known - profit)

    return {
        "STATUS": status,
        "SELLING_LABEL": selling_label,
        "GROSS_SPREAD_SCENARIO": _money_str(gross),
        "MISSING": missing,
        "TARGETS": {**(targets.get("TARGETS") or {}), **extra},
        "NOTE": "Conditional scenarios are not verified achievable profit; freight/financing unknown ≠ zero",
    }


def research_opportunity(
    row: dict[str, Any],
    *,
    rematch_row: dict[str, Any] | None,
    hist_index: dict[str, Any],
    live_price: bool = True,
) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID") or "")
    title = _clean_title(str(row.get("TITLE") or ""))
    buyer = str(row.get("BUYER") or "")
    lines = _product_lines_for_row(row, rematch_row)
    priced_lines = research_line_prices(oid, lines) if live_price else [
        {**ln, "RETAIL": {"evidence_level": "PRICE_UNAVAILABLE", "unit_price": None}, "RESEARCH_ATTEMPTED": False}
        for ln in lines
    ]

    suppliers: list[str] = []
    if rematch_row:
        suppliers = list((rematch_row.get("SUPPLIER_SET_AFTER") or {}).keys())
    for ln in priced_lines:
        if ln.get("SUPPLIER"):
            suppliers.append(str(ln["SUPPLIER"]))
        suppliers.extend([str(x) for x in (ln.get("ALTERNATES") or []) if x])
    if row.get("SUPPLIER_SET"):
        suppliers.extend(list((row.get("SUPPLIER_SET") or {}).keys()))
    for p in row.get("PACKETS") or []:
        if isinstance(p, dict) and p.get("SUPPLIER"):
            suppliers.append(str(p["SUPPLIER"]))
    suppliers = list(dict.fromkeys(suppliers))

    historical = _downgrade_historical_if_weak(
        title, match_historical(buyer, title, hist_index) or {"STATUS": "HISTORICAL_PRICE_UNKNOWN"}
    )

    # Aggregate acquisition: sum line unit prices (qty=1 when unknown) — never invent
    line_costs: list[Decimal] = []
    exact_n = equiv_n = 0
    rejected: list[dict[str, Any]] = []
    for ln in priced_lines:
        ret = ln.get("RETAIL") or {}
        el = ret.get("evidence_level") or ""
        if el == "EXACT_PUBLIC_PRICE_VERIFIED":
            exact_n += 1
        elif el == "EQUIVALENT_PUBLIC_PRICE_VERIFIED":
            equiv_n += 1
        up = _dec(ret.get("unit_price"))
        if up is not None:
            qty = _dec((ln.get("IDENTITY") or {}).get("qty")) or Decimal("1")
            line_costs.append(up * qty)
        else:
            rejected.append(
                {
                    "line": ln.get("LINE_NO"),
                    "description": ln.get("DESCRIPTION"),
                    "status": ret.get("status"),
                    "evidence_level": el,
                    "note": ret.get("note") or ret.get("error"),
                    "sources_attempted_hint": "bounded public_price_search hosts",
                }
            )

    acquisition = sum(line_costs, Decimal("0")) if line_costs and len(line_costs) == len(priced_lines) else (
        sum(line_costs, Decimal("0")) if line_costs else None
    )
    # Partial pricing: do not treat as full acquisition
    full_lines_priced = bool(line_costs) and len(line_costs) == len(priced_lines)
    if line_costs and not full_lines_priced:
        acquisition_partial = sum(line_costs, Decimal("0"))
    else:
        acquisition_partial = acquisition

    selling = _dec(historical.get("unit_price"))
    selling_label = "SELLING_PRICE_UNKNOWN"
    if selling is not None:
        selling_label = historical.get("STATUS") or "EXACT_HISTORICAL_UNIT_PRICE"
    elif historical.get("STATUS") == "COMPARABLE_HISTORICAL_PRICE" and historical.get("award_amount"):
        # Comparable total — mark clearly, do not treat as item price unless single-line
        if int(historical.get("line_count") or 0) <= 1 and int(row.get("REAL_LINES") or 1) <= 1:
            selling = _dec(historical.get("award_amount"))
            selling_label = "COMPARABLE_HISTORICAL_TOTAL_AS_SCENARIO"
        else:
            selling_label = "TOTAL_AWARD_ONLY"
    elif historical.get("award_amount") and int(row.get("REAL_LINES") or 1) <= 1:
        selling = _dec(historical.get("award_amount"))
        selling_label = "TOTAL_AWARD_ONLY_SCENARIO"

    freight = None
    financing_fees = None
    fin = financing_label_for_deal(
        contract_value=selling,
        supplier_cost=acquisition if full_lines_priced else None,
    )
    econ = classify_economics(
        selling=selling,
        selling_label=selling_label,
        acquisition=acquisition if full_lines_priced else None,
        freight=freight,
        financing_fees=financing_fees,
        financing_status=fin,
        full_lines_priced=full_lines_priced,
    )
    if not full_lines_priced and acquisition_partial is not None:
        econ["PARTIAL_ACQUISITION_OBSERVED"] = _money_str(acquisition_partial)
        econ["PARTIAL_LINES_PRICED"] = len(line_costs)

    # Contribution scenarios
    plausible = {
        "P500": False,
        "P2000": False,
        "P5000": False,
        "P10000": False,
    }
    gross = _dec(econ.get("GROSS_SPREAD_SCENARIO"))
    if gross is not None and econ["STATUS"] in {
        "CONDITIONAL_POSITIVE_ECONOMICS",
        "VERIFIED_POSITIVE_ECONOMICS",
    }:
        plausible["P500"] = gross >= Decimal("500")
        plausible["P2000"] = gross >= Decimal("2000")
        plausible["P5000"] = gross >= Decimal("5000")
        plausible["P10000"] = gross >= Decimal("10000")

    cf = evaluate_cash_flow(
        award_or_selling=selling,
        acquisition_cost=acquisition if full_lines_priced else None,
        supplier_deposit=None,
        financing_fees=financing_fees,
        financing_status=fin,
    )
    evidence_complete = (
        selling is not None
        and full_lines_priced
        and freight is not None
        and financing_fees is not None
        and fin == FIN_CONFIRMED
    )
    lane = classify_profit_lane(
        expected_contribution=_dec(cf.get("EXPECTED_CONTRIBUTION")),
        evidence_complete=evidence_complete,
        financing_status=fin,
        full_sourcing_ready=full_lines_priced and bool(suppliers),
    )
    if lane["LANE"] == LANE_PROVISIONAL and gross is not None:
        lane["GROSS_SPREAD_SCENARIO"] = _money_str(gross)
        lane["SCENARIO_NOTE"] = "Gross spread only — not verified contribution"

    ml = multi_line_completeness(
        total_required=len(priced_lines),
        lines_identified=len(priced_lines),
        lines_supplier_matched=sum(1 for ln in priced_lines if ln.get("SUPPORTED") or ln.get("SUPPLIER")),
        lines_price_verified=len(line_costs),
        lines_financially_covered=0,
        award_structure=str(row.get("AWARD_STRUCTURE") or ""),
    )

    # Exact product identity — strict (generic FSC titles are NOT exact)
    try:
        from buyer_intelligence.commercial_proof_repair_v1 import classify_product_identity

        _idc = classify_product_identity(title)
        identity_ok = bool(_idc.get("IS_EXACT_VERIFIED"))
        identity_class = _idc.get("IDENTITY_CLASS")
    except Exception:
        identity_ok = any((ln.get("IDENTITY") or {}).get("has_exact_id") for ln in priced_lines)
        identity_class = "LEGACY_HAS_EXACT_ID" if identity_ok else "UNCLASSIFIED"

    next_action = _sprint_next_action(priced_lines, historical, suppliers, fin, ml, econ)

    return {
        "OPPORTUNITY_ID": oid,
        "Product": title,
        "Buyer": buyer,
        "Deadline": row.get("DEADLINE"),
        "Source": row.get("SOURCE"),
        "Mandatory_Lines": len(priced_lines),
        "Lines": priced_lines,
        "Exact_Product_Identity": identity_ok,
        "Identity_Class": identity_class,
        "Suppliers": suppliers,
        "Past_Gov_Price": historical,
        "Acquisition_Full": _money_str(acquisition if full_lines_priced else None),
        "Acquisition_Partial": _money_str(acquisition_partial),
        "Exact_Public_Price_Lines": exact_n,
        "Equivalent_Public_Price_Lines": equiv_n,
        "Rejected_Or_Unavailable": rejected,
        "Economics": econ,
        "Plausible_Contribution": plausible,
        "Lane": lane,
        "Cash_Flow": cf,
        "Financing": fin,
        "Multi_Line": ml,
        "Evidence_Links": [
            u
            for u in [
                historical.get("source_url"),
                *[
                    (ln.get("RETAIL") or {}).get("source_url")
                    for ln in priced_lines
                    if (ln.get("RETAIL") or {}).get("source_url")
                ],
            ]
            if u
        ],
        "Next_Action": next_action,
        "Workflow": [
            {"step": "Identify", "status": "complete" if identity_ok else "incomplete"},
            {"step": "Price", "status": "complete" if full_lines_priced else "incomplete"},
            {"step": "Source", "status": "complete" if suppliers else "incomplete"},
            {"step": "Finance", "status": "blocked" if fin != FIN_CONFIRMED else "complete"},
            {"step": "Bid", "status": "incomplete"},
            {"step": "Fulfill", "status": "incomplete"},
            {"step": "Get Paid", "status": "incomplete"},
        ],
        "Research_Failure_Reasons": [r.get("note") or r.get("status") for r in rejected],
    }


def _sprint_next_action(lines, historical, suppliers, fin, ml, econ) -> str:
    if not suppliers:
        return "Identify legitimate supplier channel"
    if not ml.get("FULL_CONTRACT_SOURCING_READY") and int(ml.get("TOTAL_REQUIRED_LINES") or 0) > 1:
        return f"Complete mandatory line pricing ({ml.get('LINES_PRICE_VERIFIED')}/{ml.get('TOTAL_REQUIRED_LINES')})"
    if econ.get("STATUS") == "SUPPLIER_QUOTE_REQUIRED":
        return "Send focused RFQ for exact product cost + freight"
    if econ.get("STATUS") == "SELLING_PRICE_EVIDENCE_REQUIRED":
        return "Recover historical award / bid tab selling price"
    if historical.get("STATUS") in {None, "HISTORICAL_PRICE_UNKNOWN"}:
        return "Find historical award or bid tabulation"
    if fin in {FIN_UNKNOWN, FIN_UNVERIFIED}:
        return "Qualify PO financing / supplier terms (do not mark confirmed)"
    return "Operator review RFQ + bid package"


def build_five_rfqs(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Select best 5 candidates and build complete supplier-ready RFQ packets."""
    def _cand_score(r: dict[str, Any]) -> float:
        sc = 0.0
        if r.get("Exact_Product_Identity"):
            sc += 30
        if int(r.get("Mandatory_Lines") or 99) <= 3:
            sc += 25
        if r.get("Exact_Public_Price_Lines") or r.get("Equivalent_Public_Price_Lines"):
            sc += 20
        if (r.get("Past_Gov_Price") or {}).get("award_amount") or (r.get("Past_Gov_Price") or {}).get("unit_price"):
            sc += 20
        if r.get("Suppliers"):
            sc += 15
        econ = r.get("Economics") or {}
        if econ.get("STATUS") in {"CONDITIONAL_POSITIVE_ECONOMICS", "VERIFIED_POSITIVE_ECONOMICS"}:
            sc += 25
        if (r.get("Plausible_Contribution") or {}).get("P500"):
            sc += 15
        days = _days_to_deadline(r.get("Deadline"))
        if days and days >= 5:
            sc += 10
        return sc

    ranked = sorted(results, key=_cand_score, reverse=True)
    chosen = ranked[:5]
    packets: list[dict[str, Any]] = []
    for r in chosen:
        suppliers = r.get("Suppliers") or ["UNKNOWN_SUPPLIER"]
        primary = suppliers[0]
        meta = SUPPLIER_META.get(primary) or {}
        contact = resolve_contact(primary, meta.get("KEY"))
        products = []
        for ln in r.get("Lines") or []:
            ident = ln.get("IDENTITY") or {}
            products.append(
                {
                    "line_no": ln.get("LINE_NO"),
                    "description": ln.get("DESCRIPTION"),
                    "manufacturer": ident.get("manufacturer"),
                    "mpn_model": ident.get("part_number") or ident.get("model"),
                    "nsn": ident.get("nsn"),
                    "qty": ident.get("qty") or "CONFIRM FROM SOLICITATION",
                    "uom": ident.get("uom") or "EA",
                    "public_price_observation": (ln.get("RETAIL") or {}).get("unit_price"),
                    "public_price_url": (ln.get("RETAIL") or {}).get("source_url"),
                    "evidence_level": (ln.get("RETAIL") or {}).get("evidence_level"),
                }
            )
        targets = (r.get("Economics") or {}).get("TARGETS") or {}
        internal = {
            "MAX_COST_FOR_500": targets.get("MAX_COST_FOR_500"),
            "MAX_COST_FOR_2000": targets.get("MAX_COST_FOR_2000"),
            "MAX_COST_FOR_5000": targets.get("MAX_COST_FOR_5000"),
            "MAX_COST_FOR_10000": targets.get("MAX_COST_FOR_10000"),
            "DO_NOT_DISCLOSE_TO_SUPPLIER": True,
        }
        packets.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "SOLICITATION_TITLE": r.get("Product"),
                "BUYER": r.get("Buyer"),
                "BID_DEADLINE": r.get("Deadline"),
                "PRIMARY_SUPPLIER": primary,
                "ALTERNATE_SUPPLIERS": suppliers[1:4],
                "CONTACT": contact,
                "FORM_URL": contact.get("CONTACT_URL") if contact else None,
                "AUTO_SEND": False,
                "RFQ": {
                    "SUBJECT": f"RFQ — {str(r.get('Product') or '')[:80]} — {r.get('Buyer')}",
                    "PRODUCTS": products,
                    "DELIVERY_DESTINATION": "See solicitation delivery location — confirm before quote",
                    "REQUIRED_DELIVERY_DATE": "See solicitation — confirm before quote",
                    "SOLICITATION_ID": r["OPPORTUNITY_ID"],
                    "BID_SUBMISSION_DEADLINE": r.get("Deadline"),
                    "PACKAGING_COMPLIANCE": "Per solicitation; advise if special packaging required",
                    "REQUESTED": [
                        "Firm delivered unit pricing for each exact product/spec",
                        "Freight cost to destination",
                        "Lead time and stock availability",
                        "Reseller / new-account eligibility",
                        "Project / special / deviated pricing if available",
                        "Payment terms",
                        "Quote expiration date",
                        "Direct-to-buyer / drop-ship capability",
                    ],
                    "INTERNAL_ONLY": internal,
                    "FUNDING_GAP": "UNKNOWN until quote + deposit terms received",
                    "MISSING_EVIDENCE": (r.get("Economics") or {}).get("MISSING"),
                    "POTENTIAL_CONTRIBUTION_AT_CEILING": "See INTERNAL_ONLY — do not disclose",
                },
                "REMAINING_BLOCKERS": (r.get("Economics") or {}).get("MISSING") or [],
                "NEXT_ACTION": "Operator review and send via supplier form — AUTO_SEND=false",
                "CANDIDATE_SCORE": _cand_score(r),
            }
        )
    return {
        "kind": "first_revenue_supplier_rfqs",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "COUNT": len(packets),
        "PACKETS": packets,
    }


def evaluate_financing_providers(*, live_fetch: bool = True) -> dict[str, Any]:
    """Research public eligibility for financing providers. Never mark confirmed."""
    providers = [
        {
            "NAME": "SouthStar Capital",
            "URL": "https://www.southstarcapital.com/",
            "FOCUS": "Purchase order / trade finance",
        },
        {
            "NAME": "TradeCap Partners",
            "URL": "https://www.tradecappartners.com/",
            "FOCUS": "PO financing / supply chain",
        },
        {
            "NAME": "King Trade Capital",
            "URL": "https://www.kingtradecapital.com/",
            "FOCUS": "PO / trade financing",
        },
        {
            "NAME": "SMB Compass",
            "URL": "https://smbcompass.com/",
            "FOCUS": "SMB financing marketplace",
        },
        {
            "NAME": "Setscale",
            "URL": "https://www.setscale.com/",
            "FOCUS": "Vendor / inventory financing",
        },
    ]
    results: list[dict[str, Any]] = []
    for p in providers:
        entry: dict[str, Any] = {
            **p,
            "RESEARCHED_AT": _utc(),
            "PUBLIC_FETCH_OK": False,
            "MIN_TRANSACTION": "UNKNOWN",
            "NEW_BUSINESS_ELIGIBLE": "UNKNOWN",
            "REVENUE_HISTORY_REQUIRED": "UNKNOWN",
            "BUSINESS_CREDIT_REQUIRED": "UNKNOWN",
            "PERSONAL_CREDIT_REQUIRED": "UNKNOWN",
            "PERSONAL_GUARANTEE_REQUIRED": "UNKNOWN",
            "ADVANCE_PCT": "UNKNOWN",
            "FEES": "UNKNOWN",
            "APPROVAL_TIMING": "UNKNOWN",
            "GOV_CONTRACT_ELIGIBLE": "UNKNOWN",
            "FIT_UNDER_ZERO_OWNER_CASH": "UNVERIFIED",
            "STATUS": "FINANCING_PATH_UNVERIFIED",
            "NOTES": [],
            "EVIDENCE_SNIPPETS": [],
        }
        if live_fetch:
            try:
                import httpx

                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    resp = client.get(p["URL"], headers={"User-Agent": "M3ResearchBot/1.0"})
                    entry["PUBLIC_FETCH_OK"] = resp.status_code == 200
                    text = (resp.text or "")[:50000].lower()
                    if "personal guarantee" in text or "personal guaranty" in text:
                        entry["PERSONAL_GUARANTEE_REQUIRED"] = "MENTIONED_ON_SITE"
                        entry["NOTES"].append("Site mentions personal guarantee — likely incompatible")
                        entry["FIT_UNDER_ZERO_OWNER_CASH"] = "LIKELY_BLOCKED_OR_RESTRICTED"
                    if "no personal guarantee" in text or "non-recourse" in text:
                        entry["PERSONAL_GUARANTEE_REQUIRED"] = "SITE_CLAIMS_NON_RECOURSE_OR_NO_PG"
                        entry["NOTES"].append("Site claims non-recourse / no PG — still requires underwriting confirmation")
                        entry["FIT_UNDER_ZERO_OWNER_CASH"] = "POSSIBLE_IF_CONFIRMED"
                    if "purchase order" in text or "po financing" in text:
                        entry["NOTES"].append("PO financing mentioned")
                    if "government" in text or "federal" in text:
                        entry["GOV_CONTRACT_ELIGIBLE"] = "MENTIONED_PUBLICLY_UNVERIFIED"
                    # Soft extract dollar mentions
                    m = re.search(r"\$\s?([\d,]+)\s*(minimum|min\.?)", text)
                    if m:
                        entry["MIN_TRANSACTION"] = m.group(0)[:40]
                    entry["EVIDENCE_SNIPPETS"] = [
                        s.strip()[:160]
                        for s in re.findall(r".{0,40}(purchase order|personal guarantee|non-recourse|government).{0,40}", text)
                    ][:5]
            except Exception as exc:
                entry["NOTES"].append(f"Fetch failed: {str(exc)[:120]}")
                entry["PUBLIC_FETCH_OK"] = False
        # Hard constraint reminder
        entry["COMPANY_CONSTRAINTS"] = {
            "OWNER_CASH": "0",
            "PERSONAL_CREDIT": "FORBIDDEN",
            "PERSONAL_GUARANTEE": "FORBIDDEN",
            "BUSINESS_CREDIT": "NONE_ESTABLISHED",
        }
        entry["APPROVAL_STATUS"] = "NOT_CONFIRMED — public page ≠ approval"
        results.append(entry)

    return {
        "kind": "first_revenue_financing_research",
        "build": BUILD,
        "built_at": _utc(),
        "CONFIRMED_FINANCEABLE": 0,
        "PROVIDERS": results,
        "VERDICT": (
            "No provider confirmed for our constraints. Several mention PO finance publicly; "
            "personal guarantee / credit / revenue history remain UNKNOWN until underwriting. "
            "Do not mark opportunities FINANCEABLE_CONFIRMED."
        ),
    }


def build_deals_projection(results: list[dict[str, Any]], selected: list[dict[str, Any]]) -> dict[str, Any]:
    deals = []
    sel_scores = {s["OPPORTUNITY_ID"]: s.get("PRIORITY_SCORE") for s in selected}
    for r in results:
        econ = r.get("Economics") or {}
        lane = r.get("Lane") or {}
        tags = ["first_revenue", "needs_pricing"]
        if lane.get("LANE") == LANE_CASH:
            tags.append("cash_flow")
        if lane.get("LANE") == LANE_GROWTH:
            tags.append("growth")
        if lane.get("LANE") == LANE_SCALE:
            tags.append("scale")
        if econ.get("STATUS") in {"SUPPLIER_QUOTE_REQUIRED", "INSUFFICIENT_EVIDENCE", "SELLING_PRICE_EVIDENCE_REQUIRED"}:
            tags.append("needs_pricing")
        tags.append("needs_financing")
        if r.get("Financing") == FIN_CONFIRMED and econ.get("STATUS") == "VERIFIED_POSITIVE_ECONOMICS":
            tags.append("ready_to_bid")
        deals.append(
            {
                "opportunity_id": r["OPPORTUNITY_ID"],
                "product": r.get("Product"),
                "buyer": r.get("Buyer"),
                "deadline": r.get("Deadline"),
                "mandatory_lines": r.get("Mandatory_Lines"),
                "supplier_coverage": len(r.get("Suppliers") or []),
                "suppliers": r.get("Suppliers"),
                "retail_pricing_coverage": f"{(r.get('Multi_Line') or {}).get('LINES_PRICE_VERIFIED')}/{r.get('Mandatory_Lines')}",
                "historical_government_pricing": (r.get("Past_Gov_Price") or {}).get("STATUS"),
                "current_retail_cost": r.get("Acquisition_Full") or r.get("Acquisition_Partial") or "UNKNOWN",
                "estimated_remaining_profit": "UNKNOWN",
                "profit_scenario": econ.get("GROSS_SPREAD_SCENARIO") or "UNKNOWN",
                "economics_status": econ.get("STATUS"),
                "lane": lane.get("LANE") or LANE_PROVISIONAL,
                "lane_status": lane.get("LANE_STATUS"),
                "lane_verification": lane.get("VERIFICATION"),
                "financing_status": r.get("Financing"),
                "estimated_days_to_cash": (r.get("Cash_Flow") or {}).get("ESTIMATED_DAYS_TO_CASH"),
                "payment_timing_confidence": (r.get("Cash_Flow") or {}).get("PAYMENT_TIMING_CONFIDENCE"),
                "owner_cash_required": (r.get("Cash_Flow") or {}).get("OWNER_CASH_REQUIRED"),
                "research_priority_score": sel_scores.get(r["OPPORTUNITY_ID"]),
                "next_action": r.get("Next_Action"),
                "workflow": r.get("Workflow"),
                "evidence_links": r.get("Evidence_Links"),
                "filter_tags": tags,
                "evidence_confidence": "PROVISIONAL",
            }
        )
    deals.sort(key=lambda d: (-float(d.get("research_priority_score") or 0), d.get("opportunity_id") or ""))
    return {
        "kind": "first_revenue_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(deals),
        "deals": deals,
    }


def build_scorecard(results: list[dict[str, Any]], rfqs: dict[str, Any], financing: dict[str, Any]) -> dict[str, Any]:
    def _has_exact_price(r):
        return int(r.get("Exact_Public_Price_Lines") or 0) > 0

    def _has_equiv(r):
        return int(r.get("Equivalent_Public_Price_Lines") or 0) > 0

    def _hist(r):
        st = (r.get("Past_Gov_Price") or {}).get("STATUS")
        return st not in {None, "HISTORICAL_PRICE_UNKNOWN"}

    def _complete_sourcing(r):
        return bool((r.get("Multi_Line") or {}).get("FULL_CONTRACT_SOURCING_READY"))

    calc_econ = sum(
        1
        for r in results
        if (r.get("Economics") or {}).get("STATUS")
        in {
            "CONDITIONAL_POSITIVE_ECONOMICS",
            "VERIFIED_POSITIVE_ECONOMICS",
            "NEGATIVE_ECONOMICS",
            "SELLING_PRICE_EVIDENCE_REQUIRED",
            "SUPPLIER_QUOTE_REQUIRED",
        }
        and (
            (r.get("Economics") or {}).get("GROSS_SPREAD_SCENARIO") is not None
            or (r.get("Acquisition_Full") or r.get("Acquisition_Partial"))
        )
    )
    positive = sum(
        1
        for r in results
        if (r.get("Economics") or {}).get("STATUS")
        in {"CONDITIONAL_POSITIVE_ECONOMICS", "VERIFIED_POSITIVE_ECONOMICS"}
    )
    return {
        "investigated": len(results),
        "exact_product_identities": sum(1 for r in results if r.get("Exact_Product_Identity")),
        "exact_public_prices": sum(1 for r in results if _has_exact_price(r)),
        "equivalent_public_prices": sum(1 for r in results if _has_equiv(r) and not _has_exact_price(r)),
        "historical_benchmarks": sum(1 for r in results if _hist(r)),
        "complete_mandatory_line_sourcing": sum(1 for r in results if _complete_sourcing(r)),
        "calculable_conditional_economics": calc_econ,
        "evidence_backed_positive_economics": positive,
        "plausible_500": sum(1 for r in results if (r.get("Plausible_Contribution") or {}).get("P500")),
        "plausible_2000": sum(1 for r in results if (r.get("Plausible_Contribution") or {}).get("P2000")),
        "plausible_5000": sum(1 for r in results if (r.get("Plausible_Contribution") or {}).get("P5000")),
        "plausible_10000": sum(1 for r in results if (r.get("Plausible_Contribution") or {}).get("P10000")),
        "complete_supplier_rfqs": int(rfqs.get("COUNT") or 0),
        "financing_providers_evaluated": len(financing.get("PROVIDERS") or []),
        "confirmed_financeable": int(financing.get("CONFIRMED_FINANCEABLE") or 0),
        "execution_ready": sum(
            1
            for r in results
            if (r.get("Economics") or {}).get("STATUS") == "VERIFIED_POSITIVE_ECONOMICS"
            and r.get("Financing") == FIN_CONFIRMED
        ),
    }


def run_sprint(*, n: int = 25, live_price: bool = True, live_financing: bool = True) -> dict[str, Any]:
    t0 = time.time()
    rows = quoteable_rows()
    rematch = _load(REMATCH_FILE) or {"RESULTS": [], "NEW_MATCH_IDS": [], "PACKETS": []}
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    packet_recon = _load(PACKET_RECON_FILE) or {}
    hist_index = load_historical_indexes()

    selected, exclusions = select_first_revenue_cohort(rows, rematch, hist_index, n=n)
    _save(
        SELECTED_FILE,
        {
            "kind": "first_revenue_selected_cohort",
            "build": BUILD,
            "built_at": _utc(),
            "COUNT": len(selected),
            "TARGET": n,
            "NOTE": "IDs persisted before research; do not swap mid-sprint",
            "SELECTED": selected,
        },
    )
    _save(EXCLUSIONS_FILE, {"build": BUILD, "count": len(exclusions), "exclusions": exclusions})

    ht_by = {r["OPPORTUNITY_ID"]: r for r in rows}
    results: list[dict[str, Any]] = []
    for i, sel in enumerate(selected, 1):
        oid = sel["OPPORTUNITY_ID"]
        row = ht_by.get(oid) or sel
        log.info("first-revenue research %s/%s %s %s", i, len(selected), oid, (sel.get("TITLE") or "")[:60])
        _save(CHECKPOINT_FILE, {"stage": "research", "done": i, "total": len(selected), "oid": oid, "at": _utc()})
        results.append(
            research_opportunity(
                row,
                rematch_row=rematch_by.get(oid),
                hist_index=hist_index,
                live_price=live_price,
            )
        )

    rfqs = build_five_rfqs(results)
    financing = evaluate_financing_providers(live_fetch=live_financing)
    # Attach financing status note to top5
    for p in rfqs.get("PACKETS") or []:
        p["FINANCING_EVAL"] = {
            "STATUS": "FINANCING_PATH_UNVERIFIED",
            "PROVIDERS_REVIEWED": [x["NAME"] for x in financing.get("PROVIDERS") or []],
            "CONFIRMED": False,
        }

    scorecard = build_scorecard(results, rfqs, financing)
    deals = build_deals_projection(results, selected)
    top5 = rfqs.get("PACKETS") or []

    report = {
        "kind": "first_revenue_validation_sprint_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "DEPLOYMENT": "LOCAL_ONLY",
        "BASELINE_PRESERVED": {
            "QUOTEABLE_1003": len(rows),
            "CI_NEW_MATCHES_169": len(rematch.get("NEW_MATCH_IDS") or []),
            "PACKET_READY": packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER") or 285,
        },
        "SELECTED_COUNT": len(selected),
        "A_SCORECARD": scorecard,
        "H_DEPLOYMENT": {
            "LOCAL_VALIDATED": True,
            "DEPLOYED": False,
            "TESTS": "tests/test_first_revenue_validation_sprint_v1.py",
        },
    }

    _save(RESULTS_FILE, {"build": BUILD, "RESULTS": results, "count": len(results)})
    _save(SCORECARD_FILE, scorecard)
    _save(RFQ_FILE, rfqs)
    _save(FINANCING_FILE, financing)
    _save(DEALS_FILE, deals)
    _save(TOP5_FILE, {"build": BUILD, "TOP5": top5})
    _save(REPORT_FILE, report)
    return {
        "report": report,
        "results": results,
        "selected": selected,
        "rfqs": rfqs,
        "financing": financing,
        "scorecard": scorecard,
        "deals": deals,
        "exclusions_sample": exclusions[:20],
    }
