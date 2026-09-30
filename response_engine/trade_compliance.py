"""R3 trade compliance — Buy American / TAA / COO. Line-level. Never brand=COO."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r3_constants import (
    BUY_AMERICAN,
    FAIL,
    PASS_VERIFIED,
    REVIEW_REQUIRED,
    TRADE_AGREEMENTS,
    TRADE_NONE,
    TRADE_UNKNOWN,
    UNKNOWN,
)

# Illustrative designated-country set for classification helpers only —
# final certification still requires evidence + owner/legal review.
_DESIGNATED_HINTS = {
    "US",
    "USA",
    "UNITED STATES",
    "CA",
    "CANADA",
    "MX",
    "MEXICO",
    "JP",
    "JAPAN",
    "KR",
    "KOREA",
    "SOUTH KOREA",
    "AU",
    "AUSTRALIA",
    "GB",
    "UK",
    "UNITED KINGDOM",
    "DE",
    "GERMANY",
    "FR",
    "FRANCE",
    "IL",
    "ISRAEL",
}

_TAA_CLAUSE = re.compile(
    r"52\.225[-–]?5|Trade\s+Agreements\s+Act|\bTAA\b|designated\s+country\s+end\s+product",
    re.I,
)
_BAA_CLAUSE = re.compile(
    r"52\.225[-–]?1|Buy\s+American|domestic\s+end\s+product|FAR\s+25\.1",
    re.I,
)
_FTA_CLAUSE = re.compile(r"52\.225[-–]?3|Free\s+Trade\s+Agreements?", re.I)


def detect_trade_regime(text: str | None, clauses: list[str] | None = None) -> dict[str, Any]:
    blob = " ".join([text or "", " ".join(clauses or [])])
    if not blob.strip():
        return {"regime": TRADE_UNKNOWN, "confidence": "LOW", "signals": []}
    signals = []
    regime = TRADE_NONE
    if _TAA_CLAUSE.search(blob):
        regime = TRADE_AGREEMENTS
        signals.append("TAA/52.225-5")
    if _BAA_CLAUSE.search(blob):
        if regime == TRADE_AGREEMENTS:
            signals.append("BuyAmerican_also_present")
        else:
            regime = BUY_AMERICAN
            signals.append("BuyAmerican/52.225-1")
    if _FTA_CLAUSE.search(blob) and regime == TRADE_NONE:
        regime = "FTA"
        signals.append("FTA/52.225-3")
    conf = "HIGH" if signals else "LOW"
    if not signals:
        regime = TRADE_UNKNOWN
    return {"regime": regime, "confidence": conf, "signals": signals}


def classify_coo(country: str | None) -> str:
    if not country or str(country).strip().upper() in ("", "UNKNOWN", "N/A"):
        return "unknown"
    c = str(country).strip().upper()
    if c in ("US", "USA", "UNITED STATES", "U.S.", "U.S.A."):
        return "U.S.-made"
    if c in _DESIGNATED_HINTS:
        return "designated-country"
    return "non-designated-country"


def evaluate_line_trade(
    *,
    line: dict[str, Any],
    regime: str,
    evidence_quality: str | None = None,
) -> dict[str, Any]:
    """Per-CLIN trade result. Brand HQ is never used as COO."""
    origin = (
        line.get("country_of_origin")
        or (line.get("offered_product") or {}).get("country_of_origin")
        or line.get("coo")
    )
    manufacture = line.get("country_of_manufacture") or (line.get("offered_product") or {}).get(
        "country_of_manufacture"
    )
    eq = evidence_quality or line.get("origin_evidence_quality") or (
        (line.get("offered_product") or {}).get("origin_evidence_quality")
    )
    # Reject HQ/distributor as origin
    if line.get("brand_headquarters") and not origin:
        origin = None
        eq = eq or "INVALID_HQ_AS_ORIGIN"
    cls = classify_coo(origin or manufacture)
    status = REVIEW_REQUIRED
    if regime in (TRADE_NONE,):
        status = "NOT_APPLICABLE"
    elif cls == "unknown" or not (origin or manufacture):
        status = UNKNOWN
    elif regime == TRADE_AGREEMENTS:
        if cls in ("U.S.-made", "designated-country"):
            status = REVIEW_REQUIRED  # still need legal evidence quality
            if eq in ("OEM_DECLARATION", "CERTIFICATE_OF_ORIGIN", "VERIFIED"):
                status = PASS_VERIFIED
        elif cls == "non-designated-country":
            status = FAIL
    elif regime == BUY_AMERICAN:
        if cls == "U.S.-made" and eq in ("OEM_DECLARATION", "CERTIFICATE_OF_ORIGIN", "VERIFIED"):
            status = PASS_VERIFIED
        elif cls == "U.S.-made":
            status = REVIEW_REQUIRED
        else:
            status = FAIL if cls != "unknown" else UNKNOWN
    else:
        status = UNKNOWN

    return {
        "line_id": line.get("line_id") or line.get("id"),
        "clin": line.get("clin") or line.get("line_number"),
        "country_of_origin": origin or UNKNOWN,
        "country_of_manufacture": manufacture or UNKNOWN,
        "classification": cls,
        "regime": regime,
        "evidence_quality": eq or UNKNOWN,
        "status": status,
        "unresolved": status in (UNKNOWN, REVIEW_REQUIRED),
        "notes": "Do not use brand headquarters as country of origin",
    }


def evaluate_trade_compliance(
    *,
    lines: list[dict[str, Any]] | None = None,
    solicitation_text: str | None = None,
    clauses: list[str] | None = None,
    all_or_none: bool = False,
) -> dict[str, Any]:
    regime_info = detect_trade_regime(solicitation_text, clauses)
    regime = regime_info["regime"]
    line_results = [evaluate_line_trade(line=ln, regime=regime) for ln in (lines or [])]
    fails = [r for r in line_results if r["status"] == FAIL]
    unknowns = [r for r in line_results if r["status"] == UNKNOWN]
    reviews = [r for r in line_results if r["status"] == REVIEW_REQUIRED]

    overall = PASS_VERIFIED
    if regime == TRADE_UNKNOWN and not line_results:
        overall = UNKNOWN
    elif fails:
        overall = FAIL
    elif unknowns:
        overall = UNKNOWN
    elif reviews:
        overall = REVIEW_REQUIRED
    elif not line_results and regime not in (TRADE_NONE,):
        overall = UNKNOWN
    elif regime == TRADE_NONE:
        overall = "NOT_APPLICABLE"

    if all_or_none and fails:
        overall = FAIL

    return {
        "kind": "TradeComplianceDecision",
        "decision_id": new_id("TRD"),
        "regime": regime,
        "regime_detection": regime_info,
        "line_results": line_results,
        "overall_status": overall,
        "fail_count": len(fails),
        "unknown_count": len(unknowns),
        "review_count": len(reviews),
        "all_or_none": all_or_none,
        "trace": {
            "clause_signals": regime_info.get("signals"),
            "rule": "solicitation clause controls; unknown origin never auto-passes",
        },
        "LIVE_API_REQUESTS": 0,
    }
