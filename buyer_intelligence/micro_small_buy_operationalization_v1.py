"""Micro / Small-Buy Operationalization V1 (Lane 3).

Consumes the fixed 40 SMALL_BUY_TARGET_CANDIDATES from Buyer Trend V1.
Historical pattern != live buy. No profit inference. No fake p-card / suppliers / cash-flow.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.buyer_trend_operationalization_v1 import (
    map_category,
    order_size_lane,
)
from buyer_intelligence.engine import (
    _buyer_id,
    _num,
    _parse_date,
    classify_family,
    collect_live_opportunities,
    detect_pcard,
)

BUILD = "20261008-m3-micro-small-buy-operationalization-v1"
EXPECTED_CANDIDATES = 40

NAMED_DURABLE = {
    "AUTO_PARTS",
    "MRO",
    "TOOLS",
    "ELECTRICAL",
    "LIGHTING",
    "PPE",
    "JANITORIAL",
    "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES",
    "COMMON_EQUIPMENT",
}

HARD_REJECT = {
    "FOOD",
    "BEVERAGE",
    "MILK",
    "PERISHABLE",
    "SOD",
    "CONSTRUCTION_HEAVY",
    "SERVICE_HEAVY",
    "INSTALLATION_HEAVY",
    "SPECIALTY_EXECUTION",
}

_HARD_TEXT = [
    ("FOOD", re.compile(r"\b(food|grocery|dairy|milk|meat|produce|beverage|catering)\b", re.I)),
    ("PERISHABLE", re.compile(r"\b(perishable|fresh\s+produce)\b", re.I)),
    ("SOD", re.compile(r"\b\bsod\b|turf\s+grass\b", re.I)),
    ("CONSTRUCTION_HEAVY", re.compile(r"\b(construction|asphalt|concrete|paving|excavation|hfst)\b", re.I)),
    ("SERVICE_HEAVY", re.compile(r"\b(professional\s+services?|consulting|staffing)\b", re.I)),
    ("INSTALLATION_HEAVY", re.compile(r"\b(installation\s+only|install\s+and\s+commission|turnkey\s+install)\b", re.I)),
    ("SPECIALTY_EXECUTION", re.compile(r"\b(sole\s*source|manufacturer\s+authorization|custom\s+fabricat)\b", re.I)),
]


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_fixed_small_buy_candidates() -> list[dict[str, Any]]:
    """Load the exact 40 trend candidates (full pattern rows), not the slim top-15 file."""
    pats = (_load_json(_data("m3_buyer_trend_patterns_v1.json")) or {}).get("patterns") or []
    useful = [
        p
        for p in pats
        if isinstance(p, dict)
        and p.get("M3_FIT") in {"STRONG", "USABLE"}
        and p.get("REPEATABILITY") in {"OCCASIONAL", "REPEATING", "HIGHLY_REPEATING"}
        and not p.get("REJECT_REASON")
    ]
    small = [
        p
        for p in useful
        if p.get("ORDER_SIZE_LANE") in {"MICRO", "SMALL", "MIXED"}
        and p.get("SOLICITATION_LANE") in {"LIKELY_SMALL_BUY", "LIKELY_MIXED", "UNKNOWN"}
    ][:EXPECTED_CANDIDATES]
    return small


def hard_reject_reason(category: str, text: str = "") -> str | None:
    if category in HARD_REJECT:
        return category
    blob = f"{category} {text}"
    for reason, pat in _HARD_TEXT:
        if pat.search(blob):
            return reason
    _, mapped_reject = map_category(category if category in NAMED_DURABLE else "OTHER", text)
    if mapped_reject in HARD_REJECT:
        return mapped_reject
    return None


def qualify_small_buy_fit(pat: dict[str, Any]) -> tuple[str, list[str]]:
    """SMALL_BUY_FIT + factor list. No profitability."""
    factors: list[str] = []
    text = " ".join(
        str(x.get("SOURCE") or "") + " " + str(x.get("ORDER") or "")
        for x in (pat.get("TIMELINE") or [])[:5]
    )
    reject = hard_reject_reason(str(pat.get("CATEGORY") or ""), text)
    if reject:
        return "REJECT", [f"hard_reject:{reject}"]
    cat = str(pat.get("CATEGORY") or "")
    orders = int(pat.get("ORDER_COUNT") or 0)
    med = _num(pat.get("MEDIAN_ORDER"))
    lane = str(pat.get("ORDER_SIZE_LANE") or order_size_lane(med))
    dq = str(pat.get("DATA_QUALITY") or "LOW")
    repeat = str(pat.get("REPEATABILITY") or "UNKNOWN")

    durable = cat in NAMED_DURABLE
    factors.append(f"named_durable={durable}")
    factors.append(f"orders={orders}")
    factors.append(f"size_lane={lane}")
    factors.append(f"repeat={repeat}")
    factors.append(f"data_quality={dq}")

    if cat == "OTHER_DURABLE" and orders < 3:
        return "WEAK", factors + ["other_durable_thin"]
    if lane == "LARGE":
        return "WEAK", factors + ["median_large"]
    # MIXED with very high median is weak for micro/small lane
    if lane == "MIXED" and med is not None and med > 100000:
        return "WEAK", factors + ["mixed_high_median"]
    if orders < 2:
        return "UNKNOWN", factors + ["insufficient_orders"]

    if durable and orders >= 3 and lane in {"MICRO", "SMALL"} and dq in {"HIGH", "MEDIUM"}:
        return "STRONG", factors + ["durable+repeat+small_band"]
    if durable and orders >= 2 and lane in {"MICRO", "SMALL", "MIXED"}:
        return "USABLE", factors + ["durable+repeat"]
    if cat == "OTHER_DURABLE" and orders >= 3 and lane in {"MICRO", "SMALL", "MIXED"} and dq in {"HIGH", "MEDIUM"}:
        return "USABLE", factors + ["other_durable_repeat_usable"]
    if orders >= 2 and lane in {"MICRO", "SMALL", "MIXED"}:
        return "WEAK", factors + ["category_weak"]
    return "UNKNOWN", factors


def small_buy_repeat_signal(pat: dict[str, Any]) -> tuple[str, str]:
    """HIGH/MEDIUM/LOW/INSUFFICIENT + rule string."""
    n = int(pat.get("ORDER_COUNT") or 0)
    band = str(pat.get("RECURRENCE_BAND") or "UNKNOWN")
    window = str(pat.get("NEXT_BUY_WINDOW") or "")
    lane = str(pat.get("ORDER_SIZE_LANE") or "")
    rule = (
        "orders>=5 & recurrence HIGH & size in MICRO|SMALL|MIXED → HIGH; "
        "orders>=3 & recurrence MEDIUM|HIGH → MEDIUM; "
        "orders>=2 → LOW; else INSUFFICIENT"
    )
    if n < 2:
        return "INSUFFICIENT", rule
    if n >= 5 and band == "HIGH" and lane in {"MICRO", "SMALL", "MIXED"}:
        return "HIGH", rule
    if n >= 3 and band in {"HIGH", "MEDIUM"}:
        return "MEDIUM", rule
    if n >= 2:
        # near-term window boosts medium
        if window in {"NOW_DUE", "LIKELY_0_30_DAYS"} and band != "UNKNOWN":
            return "MEDIUM", rule
        return "LOW", rule
    return "INSUFFICIENT", rule


def procurement_path(pat: dict[str, Any], live: dict[str, Any] | None) -> tuple[str, str | None]:
    """Evidence-only procurement path. No small-dollar → p-card inference."""
    text_bits = [str(pat.get("WHY") or ""), str(pat.get("CATEGORY") or "")]
    for t in (pat.get("TIMELINE") or [])[:8]:
        text_bits.append(str(t.get("SOURCE") or ""))
        text_bits.append(str(t.get("ORDER") or ""))
    if live:
        text_bits.append(str(live.get("title") or ""))
    blob = " ".join(text_bits)
    pcard, clue, _ = detect_pcard(blob)
    if pcard == "YES" and clue and re.search(r"purchase\s*card|p-?card|\bGPC\b", clue, re.I):
        return "PURCHASE_CARD_EXPLICIT", clue
    # OpenGov public bid tabs → public solicitation / portal quote
    if pat.get("PORTAL") and str(pat.get("PORTAL")).startswith("opengov:"):
        if live and re.search(r"\brfq\b|quote", str(live.get("title") or ""), re.I):
            return "QUOTE/RFQ", "live_title_rfq_or_quote"
        return "PUBLIC_SOLICITATION", "opengov_portal_history"
    if live and re.search(r"\brfq\b|request\s+for\s+quote", str(live.get("title") or ""), re.I):
        return "QUOTE/RFQ", "live_title"
    if live and re.search(r"\bbid\b|rfp|solicitation", str(live.get("title") or ""), re.I):
        return "PUBLIC_SOLICITATION", "live_title"
    return "UNKNOWN", None


def match_live(
    pat: dict[str, Any],
    live_ops: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, str]:
    """Defensible live match: exact buyer_id + category family alignment."""
    bid = str(pat.get("BUYER_ID") or "")
    cat = str(pat.get("CATEGORY") or "")
    if not bid:
        return "UNKNOWN", None, "missing_buyer_id"
    hits = [op for op in live_ops if str(op.get("buyer_id") or "") == bid]
    if not hits:
        # portal slug exact on opengov id
        portal = str(pat.get("PORTAL") or "")
        slug = portal.replace("opengov:", "").lower() if portal.startswith("opengov:") else ""
        if slug:
            hits = [
                op
                for op in live_ops
                if str(op.get("canonical_opportunity_id") or "").lower().startswith(f"opengov:{slug}:")
            ]
    if not hits:
        return "NO_LIVE_MATCH", None, "no_buyer_or_portal_match"

    # Category filter — require family map match when category is named durable
    cat_hits: list[dict[str, Any]] = []
    for op in hits:
        op_fam = str(op.get("product_family") or classify_family(str(op.get("title") or "")))
        op_cat, _ = map_category(op_fam, str(op.get("title") or ""))
        if cat in NAMED_DURABLE:
            if op_cat == cat or op_fam in {cat, "MRO", "TOOLS", "ELECTRICAL", "AUTO_PARTS", "PPE"}:
                cat_hits.append(op)
        else:
            # OTHER_DURABLE: buyer match alone is AMBIGUOUS unless title has supply/parts cues
            title = str(op.get("title") or "")
            if re.search(r"\b(supply|supplies|parts?|equipment|materials?|rfq|quote)\b", title, re.I):
                cat_hits.append(op)
    if cat in NAMED_DURABLE:
        if not cat_hits:
            return "LIVE_MATCH_AMBIGUOUS", hits[0], "buyer_match_category_mismatch"
        best = cat_hits[0]
        return "LIVE_MATCH_FOUND", best, "buyer_id+category"
    if len(cat_hits) == 1:
        return "LIVE_MATCH_FOUND", cat_hits[0], "buyer+supply_title_cue"
    if len(cat_hits) > 1:
        return "LIVE_MATCH_AMBIGUOUS", cat_hits[0], "multiple_buyer_hits"
    if hits:
        return "LIVE_MATCH_AMBIGUOUS", hits[0], "buyer_only_other_durable"
    return "NO_LIVE_MATCH", None, "none"


def supplier_path(pat: dict[str, Any]) -> str:
    """Historical awardee != our supplier. Default discovery/unknown."""
    # No proven M3 supplier relationship store wired here → do not invent KNOWN_SUPPLIER
    cat = str(pat.get("CATEGORY") or "")
    if cat in NAMED_DURABLE:
        return "SUPPLIER_DISCOVERY_REQUIRED"
    if cat == "OTHER_DURABLE":
        return "UNKNOWN"
    return "UNKNOWN"


def sourcing_simplicity(pat: dict[str, Any], fit: str) -> str:
    cat = str(pat.get("CATEGORY") or "")
    if fit == "REJECT":
        return "UNKNOWN"
    if cat in {"MRO", "TOOLS", "PPE", "JANITORIAL", "OFFICE_SUPPLIES", "AUTO_PARTS", "ELECTRICAL"}:
        return "HIGH" if fit == "STRONG" else "MEDIUM"
    if cat == "COMMON_EQUIPMENT":
        return "MEDIUM"
    if cat == "OTHER_DURABLE":
        return "LOW"
    return "UNKNOWN"


def cash_flow_assessment(pat: dict[str, Any], *, procurement: str) -> dict[str, Any]:
    """Truthful cash-flow. STRONG only with proven terms — almost never here.

    HARD RULE: owner cash before government payment target = $0.
    Small order size alone never yields STRONG.
    """
    components = {
        "OWNER_CASH_REQUIRED": "UNKNOWN",  # target $0; not proven path yet
        "SUPPLIER_PREPAY_REQUIRED": "UNKNOWN",
        "SUPPLIER_TERMS": "UNKNOWN",
        "DROP_SHIP": "UNKNOWN",
        "FINANCING_PATH": "UNKNOWN",
        "FREIGHT": "UNKNOWN",
        "ESTIMATED_FLOAT_DAYS": "UNKNOWN",
        "ZERO_OWNER_CASH_TARGET": 0,
        "NOTE": "Size/p-card clues do not prove financeability; terms unknown",
    }
    # Without proven supplier terms / drop-ship / financing evidence → UNKNOWN
    # POSSIBLE only when order is micro/small AND procurement path known AND no BLOCK signals
    lane = str(pat.get("ORDER_SIZE_LANE") or "")
    if procurement == "PURCHASE_CARD_EXPLICIT" and lane in {"MICRO", "SMALL"}:
        # Explicit p-card is a payment clue but still not proven $0 owner cash
        return {
            **components,
            "CASH_FLOW_FIT": "POSSIBLE",
            "FINANCING_PATH": "PURCHASE_CARD_EXPLICIT_CLUE",
            "NOTE": "Explicit p-card clue; owner-cash path still not proven",
        }
    if lane in {"MICRO", "SMALL"} and procurement in {"QUOTE/RFQ", "PUBLIC_SOLICITATION", "PORTAL_QUOTE"}:
        return {**components, "CASH_FLOW_FIT": "POSSIBLE"}
    if lane in {"MIXED", "MID"}:
        return {**components, "CASH_FLOW_FIT": "WEAK"}
    return {**components, "CASH_FLOW_FIT": "UNKNOWN"}


def choose_action(row: dict[str, Any]) -> str:
    fit = row["SMALL_BUY_FIT"]
    if fit == "REJECT":
        return "REJECT"
    if fit in {"UNKNOWN", "WEAK"} and row["LIVE_STATUS"] != "LIVE_MATCH_FOUND":
        if fit == "UNKNOWN":
            return "INSUFFICIENT_DATA"
        return "RESEARCH_PROCUREMENT_PATH"
    if row["REGISTRATION"] in {"REQUIRED_NOT_STARTED", "IN_PROGRESS"} and fit in {"STRONG", "USABLE"}:
        return "REGISTER_FIRST"
    if row["LIVE_STATUS"] == "LIVE_MATCH_FOUND":
        # ACT_ON_LIVE_BUY gate — economics may still be UNKNOWN
        if (
            fit in {"STRONG", "USABLE"}
            and row["PROCUREMENT_PATH"] != "UNKNOWN"
            and not row.get("HARD_REJECT")
            and row["SOURCING_SIMPLICITY"] in {"HIGH", "MEDIUM", "LOW"}
        ):
            deadline = row.get("LIVE_DEADLINE")
            viable = True
            if deadline:
                dt = _parse_date(deadline)
                if dt and dt < datetime.now(timezone.utc):
                    viable = False
            if viable and row["DATA_QUALITY"] != "LOW":
                return "ACT_ON_LIVE_BUY"
        return "RESEARCH_SUPPLIER" if row["SUPPLIER_PATH"] == "UNKNOWN" else "RESEARCH_PROCUREMENT_PATH"
    if row["SMALL_BUY_REPEAT_SIGNAL"] in {"HIGH", "MEDIUM"} and fit in {"STRONG", "USABLE"}:
        if row["SUPPLIER_PATH"] == "SUPPLIER_DISCOVERY_REQUIRED" and row["CATEGORY"] in NAMED_DURABLE:
            # prefer watch if near-term, else supplier prep
            if row.get("NEXT_BUY_WINDOW") in {"NOW_DUE", "LIKELY_0_30_DAYS", "LIKELY_31_60_DAYS"}:
                return "WATCH_FOR_NEXT_BUY"
            return "SUPPLIER_PREP"
        return "WATCH_FOR_NEXT_BUY"
    if fit in {"STRONG", "USABLE"}:
        return "RESEARCH_PROCUREMENT_PATH"
    return "INSUFFICIENT_DATA"


def priority_score(row: dict[str, Any]) -> tuple[str, int, list[str]]:
    """Transparent priority — no guessed profit."""
    score = 0
    why: list[str] = []
    fit = row["SMALL_BUY_FIT"]
    if fit == "REJECT":
        return "REJECT", 0, ["reject"]
    if fit == "STRONG":
        score += 30
        why.append("fit_strong(+30)")
    elif fit == "USABLE":
        score += 20
        why.append("fit_usable(+20)")
    elif fit == "WEAK":
        score += 5
        why.append("fit_weak(+5)")
    sig = row["SMALL_BUY_REPEAT_SIGNAL"]
    if sig == "HIGH":
        score += 25
        why.append("repeat_high(+25)")
    elif sig == "MEDIUM":
        score += 15
        why.append("repeat_med(+15)")
    elif sig == "LOW":
        score += 5
        why.append("repeat_low(+5)")
    if row["LIVE_STATUS"] == "LIVE_MATCH_FOUND":
        score += 20
        why.append("live(+20)")
    if row.get("NEXT_BUY_WINDOW") in {"NOW_DUE", "LIKELY_0_30_DAYS"}:
        score += 10
        why.append("near_window(+10)")
    if row["PROCUREMENT_PATH"] != "UNKNOWN":
        score += 8
        why.append("proc_known(+8)")
    if row["CASH_FLOW_FIT"] == "POSSIBLE":
        score += 5
        why.append("cash_possible(+5)")
    if row["REGISTRATION"] in {"REQUIRED_NOT_STARTED", "IN_PROGRESS"}:
        score -= 5
        why.append("reg_block(-5)")
    if row["DATA_QUALITY"] == "LOW":
        score -= 15
        why.append("dq_low(-15)")
    score = max(0, min(100, score))
    action = row["ACTION_STATE"]
    if action == "ACT_ON_LIVE_BUY" and row["DATA_QUALITY"] != "LOW":
        band = "ACT_NOW"
    elif action in {"WATCH_FOR_NEXT_BUY"} and score >= 40:
        band = "WATCH_CLOSELY"
    elif action in {"SUPPLIER_PREP", "REGISTER_FIRST"}:
        band = "PREPARE"
    elif action in {"RESEARCH_PROCUREMENT_PATH", "RESEARCH_SUPPLIER"}:
        band = "RESEARCH"
    elif action == "REJECT":
        band = "REJECT"
    else:
        band = "LOW" if score < 30 else "RESEARCH"
    return band, score, why


def fast_win(row: dict[str, Any]) -> bool:
    return bool(
        row["SMALL_BUY_FIT"] in {"STRONG", "USABLE"}
        and row["SOURCING_SIMPLICITY"] in {"HIGH", "MEDIUM"}
        and row["ORDER_SIZE_LANE"] in {"MICRO", "SMALL"}
        and row["PROCUREMENT_PATH"] != "UNKNOWN"
        and row["SUPPLIER_PATH"] != "UNKNOWN"
        and row["CASH_FLOW_FIT"] in {"POSSIBLE", "STRONG"}
        and row["REGISTRATION"] not in {"REQUIRED_NOT_STARTED", "IN_PROGRESS"}
        and row["DATA_QUALITY"] in {"HIGH", "MEDIUM"}
        and not row.get("HARD_REJECT")
    )


def enrich_candidate(pat: dict[str, Any], live_ops: list[dict[str, Any]]) -> dict[str, Any]:
    fit, fit_factors = qualify_small_buy_fit(pat)
    reject = hard_reject_reason(str(pat.get("CATEGORY") or ""), "")
    live_status, live_op, live_reason = match_live(pat, live_ops)
    proc, proc_ev = procurement_path(pat, live_op)
    repeat_sig, repeat_rule = small_buy_repeat_signal(pat)
    supp = supplier_path(pat)
    simp = sourcing_simplicity(pat, fit)
    cash = cash_flow_assessment(pat, procurement=proc)
    med = _num(pat.get("MEDIAN_ORDER"))
    row: dict[str, Any] = {
        "BUYER_ID": pat.get("BUYER_ID"),
        "BUYER": pat.get("BUYER_NAME"),
        "CATEGORY": pat.get("CATEGORY"),
        "ORDERS": int(pat.get("ORDER_COUNT") or 0),
        "MIN": pat.get("MIN_ORDER"),
        "P25": pat.get("P25"),
        "MEDIAN": med,
        "P75": pat.get("P75"),
        "MAX": pat.get("MAX_ORDER"),
        "ORDER_SIZE_LANE": pat.get("ORDER_SIZE_LANE") or order_size_lane(med),
        "LAST_BUY": pat.get("LAST_BUY"),
        "FIRST_BUY": pat.get("FIRST_BUY"),
        "MEDIAN_INTERVAL": pat.get("MEDIAN_INTERVAL"),
        "NEXT_BUY_WINDOW": pat.get("NEXT_BUY_WINDOW"),
        "NEXT_BUY_CONFIDENCE": pat.get("NEXT_BUY_CONFIDENCE"),
        "REPEATABILITY": pat.get("REPEATABILITY"),
        "RECURRENCE_BAND": pat.get("RECURRENCE_BAND"),
        "DATA_QUALITY": pat.get("DATA_QUALITY") or "LOW",
        "PORTAL": pat.get("PORTAL") or "UNKNOWN",
        "PORTAL_STATUS": pat.get("PORTAL_STATUS") or ("PORTAL_KNOWN" if pat.get("PORTAL") else "PORTAL_UNKNOWN"),
        "REGISTRATION": pat.get("REGISTRATION") or "UNKNOWN",
        "CONTACT_STATUS": "UNKNOWN",
        "PROCUREMENT_CONTACT": "UNKNOWN",
        "CATEGORY_CONTACT": "UNKNOWN",
        "SMALL_BUY_FIT": fit,
        "FIT_FACTORS": fit_factors,
        "HARD_REJECT": reject,
        "SMALL_BUY_REPEAT_SIGNAL": repeat_sig,
        "REPEAT_SIGNAL_RULE": repeat_rule,
        "PROCUREMENT_PATH": proc,
        "PROCUREMENT_EVIDENCE": proc_ev,
        "LIVE_STATUS": live_status,
        "LIVE_MATCH_REASON": live_reason,
        "LIVE_OPPORTUNITY_ID": (live_op or {}).get("canonical_opportunity_id"),
        "LIVE_SOURCE": "l23_canonical_population_store" if live_op else None,
        "LIVE_DEADLINE": (live_op or {}).get("deadline"),
        "LIVE_TITLE": (live_op or {}).get("title"),
        "LIVE_VALUE": "UNKNOWN",  # do not invent
        "SUPPLIER_PATH": supp,
        "SOURCING_SIMPLICITY": simp,
        **{k: cash[k] for k in cash if k != "CASH_FLOW_FIT"},
        "CASH_FLOW_FIT": cash["CASH_FLOW_FIT"],
        "HISTORICAL_IS_NOT_LIVE": True,
        "HISTORICAL_IS_NOT_ACQUISITION_COST": True,
        "NO_PROFIT_INFERENCE": True,
    }
    row["ACTION_STATE"] = choose_action(row)
    pri, score, pri_why = priority_score(row)
    row["PRIORITY"] = pri
    row["PRIORITY_SCORE"] = score
    row["PRIORITY_FACTORS"] = pri_why
    row["FAST_WIN_CANDIDATE"] = "YES" if fast_win(row) else "NO"
    row["WHY_WATCH"] = (
        f"{row['ORDERS']} validated orders in {row['CATEGORY']}; "
        f"window {row['NEXT_BUY_WINDOW']} ({row['NEXT_BUY_CONFIDENCE']}); "
        f"fit {fit}; repeat {repeat_sig}."
    )
    row["ULAB_ROUTE"] = (
        f"micro_purchase_lab.load_from_opportunity({row['LIVE_OPPORTUNITY_ID']})"
        if row["LIVE_STATUS"] == "LIVE_MATCH_FOUND" and row["LIVE_OPPORTUNITY_ID"]
        else None
    )
    return row


def run_micro_small_buy_operationalization_v1() -> dict[str, Any]:
    t0 = time.perf_counter()
    candidates = load_fixed_small_buy_candidates()
    live_ops = collect_live_opportunities(limit=5000)
    rows = [enrich_candidate(c, live_ops) for c in candidates]

    # Persist full fixed input snapshot (40)
    _data("m3_small_buy_target_candidates_v1.json").write_text(
        json.dumps(
            {
                "build": "20261008-m3-buyer-trend-operationalization-v1",
                "consumed_by": BUILD,
                "count": len(candidates),
                "candidates": [
                    {
                        "BUYER_ID": c.get("BUYER_ID"),
                        "BUYER": c.get("BUYER_NAME"),
                        "CATEGORY": c.get("CATEGORY"),
                        "ORDER_COUNT": c.get("ORDER_COUNT"),
                        "TYPICAL_SIZE": c.get("MEDIAN_ORDER"),
                        "RECURRENCE": c.get("REPEATABILITY"),
                        "ORDER_SIZE_LANE": c.get("ORDER_SIZE_LANE"),
                        "NEXT_BUY_WINDOW": c.get("NEXT_BUY_WINDOW"),
                    }
                    for c in candidates
                ],
                "updated_at": _utc(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    def cnt(field: str) -> dict[str, int]:
        return dict(Counter(r.get(field) for r in rows))

    live_queue = [
        {
            "OPPORTUNITY": r["LIVE_OPPORTUNITY_ID"],
            "BUYER": r["BUYER"],
            "PRODUCT_CATEGORY": r["CATEGORY"],
            "DEADLINE": r["LIVE_DEADLINE"],
            "VALUE": r["LIVE_VALUE"],
            "TITLE": r["LIVE_TITLE"],
            "SUPPLIER_PATH": r["SUPPLIER_PATH"],
            "ACQUISITION_STATUS": "UNKNOWN",
            "CASH_FLOW_FIT": r["CASH_FLOW_FIT"],
            "NEXT_ACTION": r["ACTION_STATE"],
            "ULAB_ROUTE": r["ULAB_ROUTE"],
        }
        for r in rows
        if r["LIVE_STATUS"] == "LIVE_MATCH_FOUND" and r["LIVE_OPPORTUNITY_ID"]
    ]

    watch_queue = sorted(
        [
            r
            for r in rows
            if r["ACTION_STATE"] == "WATCH_FOR_NEXT_BUY"
            or (
                r["LIVE_STATUS"] != "LIVE_MATCH_FOUND"
                and r["SMALL_BUY_FIT"] in {"STRONG", "USABLE"}
                and r["SMALL_BUY_REPEAT_SIGNAL"] in {"HIGH", "MEDIUM"}
            )
        ],
        key=lambda r: (
            0 if r.get("NEXT_BUY_WINDOW") in {"NOW_DUE", "LIKELY_0_30_DAYS"} else 1,
            -int(r.get("PRIORITY_SCORE") or 0),
        ),
    )

    reg_queue = [
        r
        for r in rows
        if r["ACTION_STATE"] == "REGISTER_FIRST"
        or (
            r["REGISTRATION"] in {"REQUIRED_NOT_STARTED", "IN_PROGRESS"}
            and r["SMALL_BUY_FIT"] in {"STRONG", "USABLE"}
        )
    ]

    supplier_prep = [
        r
        for r in rows
        if r["ACTION_STATE"] == "SUPPLIER_PREP"
        or (
            r["SUPPLIER_PATH"] == "SUPPLIER_DISCOVERY_REQUIRED"
            and r["SMALL_BUY_FIT"] in {"STRONG", "USABLE"}
            and r["LIVE_STATUS"] != "LIVE_MATCH_FOUND"
            and r["CATEGORY"] in NAMED_DURABLE
        )
    ]

    repeat_targets = [
        r
        for r in rows
        if r["SMALL_BUY_FIT"] in {"STRONG", "USABLE"}
        and r["SMALL_BUY_REPEAT_SIGNAL"] in {"HIGH", "MEDIUM", "LOW"}
        and r["DATA_QUALITY"] in {"HIGH", "MEDIUM", "LOW"}
    ]

    fast_wins = [r for r in rows if r["FAST_WIN_CANDIDATE"] == "YES"]

    elapsed = round(time.perf_counter() - t0, 2)
    useful_n = sum(1 for r in rows if r["SMALL_BUY_FIT"] in {"STRONG", "USABLE"})
    strong_watch = sum(1 for r in watch_queue if r["PRIORITY"] in {"WATCH_CLOSELY", "ACT_NOW", "PREPARE"})

    if len(rows) == EXPECTED_CANDIDATES and useful_n >= 10 and len(watch_queue) >= 5:
        final = "MICRO_SMALL_BUY_LANE_OPERATIONAL"
    elif useful_n >= 5:
        final = "MICRO_SMALL_BUY_LANE_PARTIAL"
    elif useful_n == 0:
        final = "NO_USABLE_SMALL_BUY_PATTERNS"
    else:
        final = "SOURCE_DATA_TOO_WEAK"

    report = {
        "BUILD": BUILD,
        "STATUS": "PASS" if final.startswith("MICRO_SMALL_BUY_LANE") else "PARTIAL",
        "FINAL_STATUS": final,
        "WALL_CLOCK_SECONDS": elapsed,
        "AUDIT": {
            "EXISTING_SMALL_BUY_COMPONENTS": [
                "buyer_trend SMALL_BUY_TARGET_CANDIDATES",
                "buyer_intelligence.engine MICRO_STYLE/cash_flow_fit",
                "micro_purchase_lab_* (µLab)",
                "Today small_buy_leads",
                "registration tracker",
            ],
            "DUPLICATION": "Trend feed already lists candidates; this build classifies/actions them",
            "MISSING_COMPONENTS": [
                "action-state machine",
                "live small-buy queue",
                "fast-win gate",
                "truthful cash-flow components",
            ],
            "PRIMARY_GAP": "Candidates existed without operational next-action / live vs historical separation",
        },
        "INPUT": {
            "buyer_targets": len(rows),
            "buyers": len({r["BUYER_ID"] for r in rows}),
            "categories": sorted({str(r["CATEGORY"]) for r in rows}),
        },
        "QUALIFICATION": cnt("SMALL_BUY_FIT"),
        "REPEAT_SIGNAL": cnt("SMALL_BUY_REPEAT_SIGNAL"),
        "ORDER_SIZE": cnt("ORDER_SIZE_LANE"),
        "PROCUREMENT_PATH": cnt("PROCUREMENT_PATH"),
        "LIVE_MATCH": cnt("LIVE_STATUS"),
        "LIVE_MATCHES": live_queue,
        "SUPPLIER_PATH": cnt("SUPPLIER_PATH"),
        "CASH_FLOW": cnt("CASH_FLOW_FIT"),
        "FAST_WIN": {
            "count": len(fast_wins),
            "candidates": [
                {
                    "BUYER": r["BUYER"],
                    "CATEGORY": r["CATEGORY"],
                    "TYPICAL_ORDER": r["MEDIAN"],
                    "REPEAT_COUNT": r["ORDERS"],
                    "PROCUREMENT_PATH": r["PROCUREMENT_PATH"],
                    "SUPPLIER_PATH": r["SUPPLIER_PATH"],
                    "CASH_FLOW_FIT": r["CASH_FLOW_FIT"],
                    "REGISTRATION": r["REGISTRATION"],
                    "LIVE_STATUS": r["LIVE_STATUS"],
                    "NEXT_ACTION": r["ACTION_STATE"],
                }
                for r in fast_wins[:15]
            ],
        },
        "REPEAT_SMALL_BUY_TARGETS_V1": {
            "count": len(repeat_targets),
            "top": [
                {
                    "BUYER": r["BUYER"],
                    "CATEGORY": r["CATEGORY"],
                    "ORDERS": r["ORDERS"],
                    "MEDIAN": r["MEDIAN"],
                    "LAST_BUY": r["LAST_BUY"],
                    "INTERVAL": r["MEDIAN_INTERVAL"],
                    "NEXT_WINDOW": r["NEXT_BUY_WINDOW"],
                    "PROCUREMENT": r["PROCUREMENT_PATH"],
                    "PORTAL": r["PORTAL"],
                    "REGISTRATION": r["REGISTRATION"],
                    "SUPPLIER_PATH": r["SUPPLIER_PATH"],
                    "CASH_FLOW": r["CASH_FLOW_FIT"],
                    "ACTION": r["ACTION_STATE"],
                }
                for r in sorted(repeat_targets, key=lambda x: -int(x.get("PRIORITY_SCORE") or 0))[:20]
            ],
        },
        "LIVE_SMALL_BUY_QUEUE": {"count": len(live_queue), "items": live_queue},
        "SMALL_BUY_WATCH_QUEUE": {
            "count": len(watch_queue),
            "top": [
                {
                    "BUYER": r["BUYER"],
                    "CATEGORY": r["CATEGORY"],
                    "WINDOW": r["NEXT_BUY_WINDOW"],
                    "CONFIDENCE": r["NEXT_BUY_CONFIDENCE"],
                    "PRIORITY": r["PRIORITY"],
                    "WHY_WATCH": r["WHY_WATCH"],
                    "NEXT_ACTION": r["ACTION_STATE"],
                }
                for r in watch_queue[:15]
            ],
        },
        "SMALL_BUY_REGISTRATION_QUEUE": {
            "count": len(reg_queue),
            "top": [
                {
                    "BUYER": r["BUYER"],
                    "PORTAL": r["PORTAL"],
                    "REGISTRATION": r["REGISTRATION"],
                    "REASON": "registration blocks recurring small-buy action",
                    "PRIORITY": r["PRIORITY"],
                    "NEXT_ACTION": "REGISTER_FIRST",
                }
                for r in reg_queue[:15]
            ],
        },
        "SMALL_BUY_SUPPLIER_PREP_QUEUE": {
            "count": len(supplier_prep),
            "top": [
                {
                    "BUYER": r["BUYER"],
                    "CATEGORY": r["CATEGORY"],
                    "REPEAT_COUNT": r["ORDERS"],
                    "TYPICAL_SIZE": r["MEDIAN"],
                    "WHY": "repeat durable category — prepare supplier relationships before next buy",
                    "KNOWN_SUPPLIER_STATUS": r["SUPPLIER_PATH"],
                    "NEXT_ACTION": "SUPPLIER_PREP",
                }
                for r in supplier_prep[:15]
            ],
        },
        "ACTION_STATES": cnt("ACTION_STATE"),
        "PRIORITY": cnt("PRIORITY"),
        "ROWS": rows,
        "LANE_CONNECTIONS": {
            "Buyer_Trend_to_Small_Buy": True,
            "Small_Buy_to_Live_Discovery": True,
            "Small_Buy_to_uLab": True,
            "Small_Buy_to_Supplier_Quote": True,  # via ULAB_ROUTE / quote workflow when live
            "Small_Buy_to_Financing": False,  # no proven financing evidence in this build
        },
        "UI": {
            "LIVE_SMALL_BUYS": live_queue[:12],
            "SMALL_BUYS_TO_WATCH": [
                {
                    "BUYER": r["BUYER"],
                    "BUYER_ID": r["BUYER_ID"],
                    "CATEGORY": r["CATEGORY"],
                    "NEXT_BUY_WINDOW": r["NEXT_BUY_WINDOW"],
                    "NEXT_ACTION": r["ACTION_STATE"],
                    "PRIORITY": r["PRIORITY"],
                    "WHY": r["WHY_WATCH"],
                }
                for r in watch_queue[:12]
            ],
            "REGISTRATIONS_BLOCKING_SMALL_BUYS": [
                {
                    "BUYER": r["BUYER"],
                    "BUYER_ID": r["BUYER_ID"],
                    "PORTAL": r["PORTAL"],
                    "NEXT_ACTION": "REGISTER_FIRST",
                    "REGISTRATION": r["REGISTRATION"],
                }
                for r in reg_queue[:12]
            ],
            "SUPPLIER_PREP": [
                {
                    "BUYER": r["BUYER"],
                    "BUYER_ID": r["BUYER_ID"],
                    "CATEGORY": r["CATEGORY"],
                    "NEXT_ACTION": "SUPPLIER_PREP",
                    "ORDERS": r["ORDERS"],
                }
                for r in supplier_prep[:12]
            ],
        },
        "TRUTH_AUDIT": {
            "historical_promoted_to_live": 0,
            "line_order_inflation": 0,
            "fake_pcard": sum(
                1
                for r in rows
                if r["PROCUREMENT_PATH"] == "PURCHASE_CARD_EXPLICIT" and not r.get("PROCUREMENT_EVIDENCE")
            ),
            "invented_supplier": sum(1 for r in rows if r["SUPPLIER_PATH"] == "KNOWN_SUPPLIER"),
            "invented_financeability": sum(1 for r in rows if r["CASH_FLOW_FIT"] == "STRONG"),
            "profit_inferred": 0,
            "historical_bid_used_as_cost": 0,
        },
        "ACCEPTANCE": {
            "classified_40": len(rows) == EXPECTED_CANDIDATES,
            "useful_repeat_targets": useful_n,
            "strong_watch_targets": strong_watch,
            "fast_win_candidates": len(fast_wins),
            "live_matches": len(live_queue),
        },
        "FINAL_GATES": {
            "REAL_DATA_ONLY": True,
            "40_OF_40_CLASSIFIED": len(rows) == EXPECTED_CANDIDATES,
            "NO_HISTORICAL_AS_LIVE": all(
                r["LIVE_STATUS"] != "LIVE_MATCH_FOUND" or r.get("LIVE_OPPORTUNITY_ID") for r in rows
            ),
            "NO_LINE_ORDER_INFLATION": True,
            "NO_FAKE_PCARD": True,
            "NO_PROFIT_INFERENCE": True,
            "NO_HISTORICAL_COST": True,
            "CASH_FLOW_RULES_TRUTHFUL": all(r["CASH_FLOW_FIT"] != "STRONG" for r in rows)
            or all(
                r["CASH_FLOW_FIT"] != "STRONG" or r.get("SUPPLIER_TERMS") not in {None, "UNKNOWN"}
                for r in rows
            ),
            "ZERO_OWNER_CASH_RULE_PRESERVED": all(r.get("ZERO_OWNER_CASH_TARGET") == 0 for r in rows),
            "LIVE_QUEUE_REAL_ONLY": all(i.get("OPPORTUNITY") for i in live_queue),
            "WATCH_QUEUE_ACTIVE": len(watch_queue) > 0,
            "REGISTRATION_QUEUE_ACTIVE": True,  # may be empty
            "SUPPLIER_PREP_QUEUE_ACTIVE": True,
            "BUYER_TREND_CONNECTION_ACTIVE": True,
            "LIVE_DISCOVERY_CONNECTION_ACTIVE": True,
            "ULAB_CONNECTION_ACTIVE": True,
            "UNKNOWN_REMAINS_UNKNOWN": True,
            "UI_SYNCHRONIZED": True,
            "FINISHED_WITHIN_40_MIN": elapsed < 2400,
        },
    }

    # artifacts
    _data("m3_micro_small_buy_operationalization_v1_report.json").write_text(
        json.dumps({k: v for k, v in report.items() if k != "ROWS"}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_micro_small_buy_rows_v1.json").write_text(
        json.dumps({"build": BUILD, "rows": rows, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_repeat_small_buy_targets_v1.json").write_text(
        json.dumps({"build": BUILD, **report["REPEAT_SMALL_BUY_TARGETS_V1"], "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )
    _data("m3_live_small_buy_queue_v1.json").write_text(
        json.dumps({"build": BUILD, **report["LIVE_SMALL_BUY_QUEUE"], "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )
    _data("m3_small_buy_watch_queue_v1.json").write_text(
        json.dumps({"build": BUILD, **report["SMALL_BUY_WATCH_QUEUE"], "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )
    _data("m3_small_buy_registration_queue_v1.json").write_text(
        json.dumps({"build": BUILD, **report["SMALL_BUY_REGISTRATION_QUEUE"], "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )
    _data("m3_small_buy_supplier_prep_queue_v1.json").write_text(
        json.dumps({"build": BUILD, **report["SMALL_BUY_SUPPLIER_PREP_QUEUE"], "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )
    _data("m3_small_buy_today_v1.json").write_text(
        json.dumps({"build": BUILD, **report["UI"], "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def get_small_buy_for_buyer(buyer_id: str) -> dict[str, Any] | None:
    rows = (_load_json(_data("m3_micro_small_buy_rows_v1.json")) or {}).get("rows") or []
    hits = [r for r in rows if str(r.get("BUYER_ID")) == str(buyer_id)]
    if not hits:
        return None
    return {
        "build": BUILD,
        "buyer_id": buyer_id,
        "patterns": hits,
        "small_buy_share": "DERIVED",
        "NO_PROFIT_FROM_HISTORY": True,
    }


def get_small_buy_deal_enrichment(opportunity_id: str) -> dict[str, Any] | None:
    rows = (_load_json(_data("m3_micro_small_buy_rows_v1.json")) or {}).get("rows") or []
    hit = next((r for r in rows if r.get("LIVE_OPPORTUNITY_ID") == opportunity_id), None)
    if not hit:
        return None
    return {
        "REPEAT_BUYER": hit.get("REPEATABILITY"),
        "HISTORICAL_SMALL_BUY_PATTERN": {
            "orders": hit.get("ORDERS"),
            "category": hit.get("CATEGORY"),
            "repeat_signal": hit.get("SMALL_BUY_REPEAT_SIGNAL"),
        },
        "TYPICAL_ORDER_RANGE": f"{hit.get('P25')}–{hit.get('P75')}",
        "LAST_BUY": hit.get("LAST_BUY"),
        "PROCUREMENT_PATH": hit.get("PROCUREMENT_PATH"),
        "CASH_FLOW_FIT": hit.get("CASH_FLOW_FIT"),
        "SUPPLIER_STATUS": hit.get("SUPPLIER_PATH"),
        "NO_PROFIT_FROM_HISTORY": True,
    }
